#!/usr/bin/env python3
"""跨 provider 的 reasoning 恢复实验矩阵。

按 config 中的模型骨架展开：只跑「能完整解析角色依赖」的方法。
完整落盘恢复正文。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reasoning_recovery.catalog import FAMILY_DEFAULT_METHODS, default_catalog
from reasoning_recovery.config import (
    client_from_settings,
    list_runnable_methods,
    load_app_config,
    resolve_method_run,
)
from reasoning_recovery.engine import RecoveryEngine
from reasoning_recovery.errors import ProbeError
from reasoning_recovery.protocol import adapter_for


PROMPT = "Answer in one sentence: what is 17 * 19? Do the arithmetic carefully."


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="", help="项目 config.yaml 路径")
    parser.add_argument("--base-url", default="", help="覆盖 upstream.base_url")
    parser.add_argument("--api-key", default="", help="覆盖 upstream.api_key")
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--families", default="", help="逗号分隔家族过滤：gpt,claude,gemini；空=全部已配置")
    parser.add_argument("--methods", default="", help="逗号分隔方法过滤；空=该家族默认可跑方法")
    parser.add_argument("--efforts", default="high")
    parser.add_argument("--candidate-pool", type=int, default=3)
    parser.add_argument("--selection-count", type=int, default=3)
    parser.add_argument("--source-retries", type=int, default=2)
    parser.add_argument("--max-output-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", default="runs/provider_matrix.json")
    parser.add_argument("--markdown-output", default="runs/provider_matrix.md")
    return parser.parse_args(argv)


def list_model_ids(client: Any, timeout: float) -> tuple[set[str] | None, str | None]:
    """拉取上游 /models。"""
    try:
        if hasattr(client, "get"):
            payload = client.get("models", timeout)
        else:
            return None, "MODELS_DISCOVERY_UNSUPPORTED"
    except Exception:
        return None, "MODELS_DISCOVERY_FAILED"
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None, "MODELS_DISCOVERY_INVALID_SHAPE"
    ids = {item.get("id") for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)}
    return ids, None


def research_result(
    *,
    family: str,
    method: str,
    resolved: Any,
    result: Any,
    elapsed_s: float,
    source_retry: int,
    prompt: str,
    effort: str,
) -> dict[str, Any]:
    """完整研究记录。"""
    source = result.metadata.get("source", {})
    method_metadata = result.metadata.get("method_metadata", {})
    if not isinstance(method_metadata, dict):
        method_metadata = {}
    return {
        "family": family,
        "provider": family,
        "protocol": resolved.settings.protocol,
        "source_model": resolved.role_ids.get("source"),
        "decoder_model": resolved.role_ids.get("decoder"),
        "role_names": resolved.role_names,
        "role_ids": resolved.role_ids,
        "effort": effort,
        "method": method,
        "status": "ok" if result.text else "method_empty",
        "elapsed_s": round(elapsed_s, 2),
        "source_retry": source_retry,
        "prompt": prompt,
        "text": result.text,
        "text_length": len(result.text),
        "source": source,
        "dimensions": {
            "replay": {"status": result.replay.status, **result.replay.evidence},
            "provenance": {"status": result.provenance.status, **result.provenance.evidence},
            "coverage": {"status": result.coverage.status, **result.coverage.evidence},
            "fidelity": {"status": result.fidelity.status, **result.fidelity.evidence},
        },
        "attempts": [
            {
                "method": attempt.method,
                "status": attempt.status,
                "reason": attempt.reason,
                "attempts": attempt.attempts,
                "metadata": attempt.metadata if isinstance(attempt.metadata, dict) else {},
            }
            for attempt in result.attempts
        ],
        "method_metadata": method_metadata,
        "resolution_log": list(resolved.resolution_log),
        "terminal_error": result.metadata.get("terminal_error"),
    }


def error_record(**kwargs: Any) -> dict[str, Any]:
    """错误记录。"""
    return {
        "family": kwargs.get("family", ""),
        "provider": kwargs.get("family", ""),
        "method": kwargs.get("method", ""),
        "effort": kwargs.get("effort", ""),
        "status": kwargs.get("status", "error"),
        "elapsed_s": round(kwargs.get("elapsed_s", 0.0), 2),
        "source_retry": kwargs.get("source_retry"),
        "prompt": kwargs.get("prompt"),
        "error": {"code": kwargs.get("code", "ERROR"), "details": kwargs.get("details") or {}},
    }


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    """执行矩阵：已配置家族 × 可解析方法 × effort。"""
    try:
        app = load_app_config(args.config or None, require=True)
    except ProbeError as exc:
        raise SystemExit(exc.message) from exc
    if args.base_url:
        app = replace(app, upstream=replace(app.upstream, base_url=args.base_url.rstrip("/")))
    if args.api_key:
        app = replace(app, upstream=replace(app.upstream, api_key=args.api_key))

    # 探测客户端：用任意可解析方法的 settings，否则仅 upstream
    probe_client = None
    try:
        sample = resolve_method_run(app, max_output_tokens=16, timeout=args.timeout)
        probe_client = client_from_settings(sample.settings)
    except ProbeError:
        from reasoning_recovery.models import Settings
        from reasoning_recovery.protocol import UrllibJsonClient

        probe_client = UrllibJsonClient(
            app.upstream.base_url,
            app.upstream.api_key,
            headers=app.upstream.headers,
            auth=app.upstream.auth,
            auth_header=app.upstream.auth_header,
            auth_prefix=app.upstream.auth_prefix,
        )

    model_ids, discovery_error = list_model_ids(probe_client, args.timeout)
    records: list[dict[str, Any]] = []
    efforts = tuple(item.strip() for item in args.efforts.split(",") if item.strip())
    family_filter = {item.strip() for item in args.families.split(",") if item.strip()}
    method_filter = {item.strip() for item in args.methods.split(",") if item.strip()}
    catalog = default_catalog()
    runnable = set(list_runnable_methods(app, catalog))

    families = sorted(app.families())
    if family_filter:
        families = [f for f in families if f in family_filter]

    if not families:
        raise SystemExit("没有可跑的家族：请在 config.yaml models 中配置模型")

    for family in families:
        default_methods = FAMILY_DEFAULT_METHODS.get(family, ())
        methods = [
            m
            for m in default_methods
            if (not method_filter or m in method_filter) and m in runnable
        ]
        # 也允许 CLI 指定默认可跑之外的方法
        if method_filter:
            for m in method_filter:
                if m in runnable and m not in methods and catalog.get(m) and catalog[m].family == family:
                    methods.append(m)

        if not methods:
            records.append(
                error_record(
                    family=family,
                    method="*",
                    status="skipped",
                    code="NO_RUNNABLE_METHODS",
                    details={
                        "hint": f"family={family} 已配置模型，但没有方法能解析全部角色",
                        "configured": {
                            n: {"id": e.model_id, "roles": sorted(e.roles)}
                            for n, e in app.models.items()
                            if e.family == family
                        },
                    },
                    prompt=args.prompt,
                )
            )
            continue

        for effort in efforts:
            for method in methods:
                common = {
                    "family": family,
                    "method": method,
                    "effort": effort,
                    "prompt": args.prompt,
                }
                try:
                    resolved = resolve_method_run(
                        app,
                        method=method,
                        effort=effort,
                        max_output_tokens=args.max_output_tokens,
                        timeout=args.timeout,
                        candidate_pool=args.candidate_pool,
                        selection_count=args.selection_count,
                        catalog=catalog,
                    )
                except ProbeError as exc:
                    records.append(
                        error_record(
                            **common,
                            status="unresolved",
                            code=exc.code,
                            details=exc.details if isinstance(exc.details, dict) else {"raw": exc.details},
                        )
                    )
                    print(json.dumps({"method": method, "status": "unresolved", "code": exc.code}, ensure_ascii=False), flush=True)
                    continue

                # 上游模型列表校验（可选）
                if model_ids is not None:
                    missing = [
                        mid
                        for mid in resolved.role_ids.values()
                        if mid not in model_ids
                    ]
                    if missing:
                        records.append(
                            error_record(
                                **common,
                                status="not_available",
                                code="MODEL_NOT_LISTED",
                                details={"missing_ids": missing, "roles": resolved.role_ids},
                            )
                        )
                        continue

                client = client_from_settings(resolved.settings)
                adapter = adapter_for(resolved.settings, client)
                engine = RecoveryEngine(adapter, {resolved.method: resolved.strategy})
                finished = False
                for source_retry in range(1, max(args.source_retries, 1) + 1):
                    started = time.monotonic()
                    try:
                        result = engine.recover(
                            resolved.settings, args.prompt, method=resolved.method
                        )
                    except ProbeError as exc:
                        elapsed = time.monotonic() - started
                        if exc.code == "SOURCE_NO_REASONING_ENVELOPE" and source_retry < args.source_retries:
                            continue
                        records.append(
                            error_record(
                                **common,
                                status="error",
                                code=exc.code,
                                details=exc.details if isinstance(exc.details, dict) else {"raw": exc.details},
                                elapsed_s=elapsed,
                                source_retry=source_retry,
                            )
                        )
                        finished = True
                        break
                    except Exception as exc:  # pragma: no cover
                        records.append(
                            error_record(
                                **common,
                                status="error",
                                code="MATRIX_INTERNAL_ERROR",
                                details={"type": type(exc).__name__, "message": str(exc)},
                                elapsed_s=time.monotonic() - started,
                                source_retry=source_retry,
                            )
                        )
                        finished = True
                        break
                    else:
                        records.append(
                            research_result(
                                family=family,
                                method=method,
                                resolved=resolved,
                                result=result,
                                elapsed_s=time.monotonic() - started,
                                source_retry=source_retry,
                                prompt=args.prompt,
                                effort=effort,
                            )
                        )
                        finished = True
                        break
                if not finished:
                    records.append(error_record(**common, status="error", code="MATRIX_NO_RESULT"))
                last = records[-1]
                print(
                    json.dumps(
                        {
                            "method": last.get("method"),
                            "status": last.get("status"),
                            "roles": last.get("role_names"),
                            "text_length": last.get("text_length"),
                            "error": (last.get("error") or {}).get("code"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prompt": args.prompt,
        "prompt_sha256": hashlib.sha256(args.prompt.encode()).hexdigest(),
        "configured_models": {
            name: {"id": m.model_id, "family": m.family, "roles": sorted(m.roles)}
            for name, m in app.models.items()
        },
        "runnable_methods": sorted(runnable),
        "models_discovery": {
            "status": "ok" if model_ids is not None else "error",
            "count": len(model_ids) if model_ids is not None else None,
            "error": discovery_error,
        },
        "parameters": {
            "efforts": efforts,
            "candidate_pool": args.candidate_pool,
            "selection_count": min(args.selection_count, args.candidate_pool),
            "source_retries": args.source_retries,
            "max_output_tokens": args.max_output_tokens,
        },
        "records": records,
    }


def markdown_report(payload: dict[str, Any]) -> str:
    """Markdown 摘要 + 全文。"""
    lines = [
        "# Reasoning 恢复矩阵",
        "",
        "按模型骨架展开；仅跑可解析方法。",
        "",
        f"prompt: `{payload.get('prompt', '')}`",
        "",
        "configured models: "
        + ", ".join(
            f"{k}({v['family']}:{','.join(v['roles'])})"
            for k, v in (payload.get("configured_models") or {}).items()
        ),
        "",
        "| family | method | source | decoder | status | replay | ratio | text_len | elapsed |",
        "|---|---|---|---|---|---|---:|---:|---:|",
    ]
    for row in payload["records"]:
        dims = row.get("dimensions") or {}
        cov = dims.get("coverage") or {}
        lines.append(
            "| {family} | {method} | {source} | {decoder} | {status} | {replay} | {ratio} | {tlen} | {elapsed} |".format(
                family=row.get("family", ""),
                method=row.get("method", ""),
                source=row.get("source_model", "-"),
                decoder=row.get("decoder_model", "-"),
                status=row.get("status", ""),
                replay=(dims.get("replay") or {}).get("status", "-"),
                ratio=cov.get("ratio", "-"),
                tlen=row.get("text_length", "-"),
                elapsed=row.get("elapsed_s", "-"),
            )
        )
    lines.extend(["", "## 完整恢复正文", ""])
    for index, row in enumerate(payload["records"], 1):
        text = row.get("text")
        if not text:
            continue
        lines.append(f"### {index}. {row.get('method')} · {row.get('source_model')} → {row.get('decoder_model')}")
        lines.append("")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    payload = run_matrix(args)
    output = Path(args.output)
    markdown = Path(args.markdown_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    markdown.write_text(markdown_report(payload))
    print(
        json.dumps(
            {
                "output": str(output),
                "markdown_output": str(markdown),
                "record_count": len(payload["records"]),
                "runnable_methods": payload.get("runnable_methods"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
