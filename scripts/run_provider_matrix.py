#!/usr/bin/env python3
"""按目标模型展开的 reasoning 恢复实验矩阵（复现 arXiv:2608.09867）。

对每个 目标(source) × decoder × 方法 组合：
- harvest 目标的 reasoning envelope（可选埋 planted secret）
- decoder 按方法 replay，落盘四维证据 + 拒答分类 + envelope 取证
- 按指标生成 targets 推荐配置（runs/targets.recommended.yaml）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
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
from reasoning_recovery.protocol import adapter_for, new_secret

PROMPT = "Answer in one sentence: what is 17 * 19? Do the arithmetic carefully."


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="", help="项目 config.yaml 路径")
    parser.add_argument("--targets", default="", help="逗号分隔目标逻辑名；空=targets 段或全部 source 模型")
    parser.add_argument("--decoders", default="", help="逗号分隔 decoder 覆盖；空=按 targets.decoder")
    parser.add_argument("--methods", default="", help="逗号分隔方法过滤；空=按 targets.methods / 家族默认")
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--secret", action="store_true", help="埋 planted secret 判别协议")
    parser.add_argument("--candidate-pool", type=int, default=None, help="覆盖方法候选数（默认按方法自身）")
    parser.add_argument("--selection-count", type=int, default=None)
    parser.add_argument("--source-retries", type=int, default=3)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output", default="runs/provider_matrix.json")
    parser.add_argument("--markdown-output", default="runs/provider_matrix.md")
    parser.add_argument("--recommend-output", default="runs/targets.recommended.yaml")
    return parser.parse_args(argv)


def cell_matrix(app: Any, args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """展开 (target, decoder, method) 组合矩阵。"""
    catalog = default_catalog()
    runnable = set(list_runnable_methods(app, catalog))
    targets = [t for t in args.targets.split(",") if t] if args.targets else list(app.targets.keys())
    if not targets:
        targets = [name for name, m in app.models.items() if "source" in m.roles]
    decoders_arg = [d for d in args.decoders.split(",") if d]
    method_filter = {m for m in args.methods.split(",") if m}

    cells: list[tuple[str, str, str]] = []
    for target in targets:
        entry = app.models.get(target)
        if entry is None:
            continue
        family = entry.family
        target_block = app.targets.get(target) or {}
        decoders = decoders_arg or [str(d) for d in (target_block.get("decoder") or [])]
        if not decoders:
            decoders = [
                name
                for name, m in app.models.items()
                if m.family == family and "decoder" in m.roles
            ]
        methods = target_block.get("methods") or FAMILY_DEFAULT_METHODS.get(family, ())
        methods = [m for m in methods if (not method_filter or m in method_filter) and m in runnable]
        for decoder in decoders:
            for method in methods:
                cells.append((target, decoder, method))
    return cells


def research_result(
    *,
    target: str,
    decoder: str,
    method: str,
    resolved: Any,
    result: Any,
    elapsed_s: float,
    source_retry: int,
    prompt: str,
    effort: str,
    secret: str | None,
) -> dict[str, Any]:
    """完整研究记录（含恢复正文与 envelope 取证）。"""
    source = result.metadata.get("source", {})
    method_metadata = result.metadata.get("method_metadata", {})
    if not isinstance(method_metadata, dict):
        method_metadata = {}
    attempt_status = result.attempts[-1].status if result.attempts else "fail"
    return {
        "target": target,
        "source_model": resolved.role_ids.get("source"),
        "decoder_model": resolved.role_ids.get("decoder"),
        "family": resolved.family,
        "protocol": resolved.settings.protocol,
        "effort": effort,
        "method": method,
        "status": attempt_status,
        "elapsed_s": round(elapsed_s, 2),
        "source_retry": source_retry,
        "prompt": prompt,
        "secret": secret,
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
        "raw_signals": result.metadata.get("raw_signals"),
        "resolution_log": list(resolved.resolution_log),
        "terminal_error": result.metadata.get("terminal_error"),
    }


def error_record(
    *,
    target: str = "",
    decoder: str = "",
    method: str = "",
    effort: str = "",
    status: str = "error",
    elapsed_s: float = 0.0,
    source_retry: int | None = None,
    prompt: str = "",
    code: str = "ERROR",
    details: Any = None,
) -> dict[str, Any]:
    """错误记录。"""
    return {
        "target": target,
        "decoder_model": decoder,
        "method": method,
        "effort": effort,
        "status": status,
        "elapsed_s": round(elapsed_s, 2),
        "source_retry": source_retry,
        "prompt": prompt,
        "error": {"code": code, "details": details or {}},
    }


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    """执行矩阵：target × decoder × method。"""
    app = load_app_config(args.config or None, require=True)
    cells = cell_matrix(app, args)
    if not cells:
        raise SystemExit("没有可跑的 (target, decoder, method) 组合")

    records: list[dict[str, Any]] = []
    for target, decoder, method in cells:
        common = {
            "target": target,
            "decoder": decoder,
            "method": method,
            "effort": args.effort,
            "prompt": args.prompt,
        }
        started = time.monotonic()
        try:
            resolved = resolve_method_run(
                app,
                method=method,
                target=target,
                decoder=decoder,
                effort=args.effort,
                max_output_tokens=args.max_output_tokens,
                timeout=args.timeout,
                candidate_pool=args.candidate_pool,
                selection_count=args.selection_count,
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
            continue

        client = client_from_settings(resolved.settings)
        adapter = adapter_for(resolved.settings, client)
        engine = RecoveryEngine(adapter, {resolved.method: resolved.strategy})
        secret = new_secret() if args.secret else None
        finished = False
        for source_retry in range(1, max(args.source_retries, 1) + 1):
            try:
                result = engine.recover(
                    resolved.settings, args.prompt, method=resolved.method, secret=secret
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
                        elapsed_s=elapsed,
                        source_retry=source_retry,
                        details=exc.details if isinstance(exc.details, dict) else {"raw": exc.details},
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
                        elapsed_s=time.monotonic() - started,
                        source_retry=source_retry,
                        details={"type": type(exc).__name__, "message": str(exc)},
                    )
                )
                finished = True
                break
            records.append(
                research_result(
                    target=target,
                    decoder=decoder,
                    method=method,
                    resolved=resolved,
                    result=result,
                    elapsed_s=time.monotonic() - started,
                    source_retry=source_retry,
                    prompt=args.prompt,
                    effort=args.effort,
                    secret=secret,
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
                    "target": target,
                    "decoder": decoder,
                    "method": method,
                    "status": last.get("status"),
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
        "secret_mode": args.secret,
        "configured_models": {
            name: {"id": m.model_id, "family": m.family, "roles": sorted(m.roles)}
            for name, m in app.models.items()
        },
        "parameters": {
            "effort": args.effort,
            "candidate_pool": args.candidate_pool,
            "selection_count": args.selection_count,
            "source_retries": args.source_retries,
            "max_output_tokens": args.max_output_tokens,
        },
        "records": records,
    }


# ---- 推荐配置生成 ----

_STATUS_RANK = {"success": 3, "low_confidence": 2, "refused": 1, "method_empty": 1}


def _cell_score(row: dict[str, Any]) -> tuple[int, int, float, int]:
    """(target, decoder, method) 排序键：状态 > secret 命中 > extraction error > 长度。

    过短正文（如回显 “OK”）视为无内容，排到拒答之后。
    """
    dims = row.get("dimensions") or {}
    prov = dims.get("provenance") or {}
    cov = dims.get("coverage") or {}
    status_rank = _STATUS_RANK.get(row.get("status", ""), 0)
    length = row.get("text_length") or 0
    if status_rank == 3 and length < 20:
        status_rank = 1  # 回显/无内容，不算有效恢复
    secret_hit = int(prov.get("secret_hit", False))
    ratio = cov.get("ratio")
    error = abs(ratio - 1.0) if isinstance(ratio, (int, float)) else 99.0
    return (status_rank, secret_hit, -error, length)


def recommend_targets(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """按矩阵结果生成每个目标的推荐 decoder 与方法链。"""
    by_target: dict[str, list[dict[str, Any]]] = {}
    for row in payload["records"]:
        if row.get("status") in ("error", "unresolved", "not_available", "skipped"):
            continue
        by_target.setdefault(row.get("target", ""), []).append(row)

    recommendations: dict[str, dict[str, Any]] = {}
    for target, rows in sorted(by_target.items()):
        rows.sort(key=_cell_score, reverse=True)
        best = rows[0]
        best_decoder = best.get("decoder_model")
        methods = [row["method"] for row in rows if row.get("decoder_model") == best_decoder]
        recommendations[target] = {
            "decoder": [best_decoder] if best_decoder else [],
            "methods": methods,
        }
    return recommendations


def markdown_report(payload: dict[str, Any]) -> str:
    """Markdown 摘要 + 全文。"""
    fence = "```"
    lines = [
        "# Reasoning 恢复矩阵（target × decoder × method）",
        "",
        "prompt: " + repr(payload.get("prompt", "")) + "  ·  secret: " + str(payload.get("secret_mode")),
        "",
        "| target | decoder | method | status | secret_hit | replay | prov | ratio | text_len | elapsed |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|",
    ]
    for row in payload["records"]:
        dims = row.get("dimensions") or {}
        cov = dims.get("coverage") or {}
        prov = dims.get("provenance") or {}
        lines.append(
            "| {target} | {decoder} | {method} | {status} | {secret} | {replay} | {prov} | {ratio} | {tlen} | {elapsed} |".format(
                target=row.get("target", "-"),
                decoder=row.get("decoder_model", "-"),
                method=row.get("method", "-"),
                status=row.get("status", "-"),
                secret=prov.get("secret_hit", "-"),
                replay=(dims.get("replay") or {}).get("status", "-"),
                prov=prov.get("status", "-"),
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
        lines.append(
            "### {i}. {t} → {d} · {m}".format(
                i=index, t=row.get("target"), d=row.get("decoder_model"), m=row.get("method")
            )
        )
        lines.append("")
        lines.append(fence)
        lines.append(text)
        lines.append(fence)
        candidates = (row.get("method_metadata") or {}).get("candidate_texts") or []
        if candidates:
            lines.append("")
            lines.append("<details><summary>候选文本</summary>")
            lines.append("")
            for c_index, candidate in enumerate(candidates, 1):
                lines.append("**候选 {n}**".format(n=c_index))
                lines.append("")
                lines.append(fence)
                lines.append(str(candidate))
                lines.append(fence)
                lines.append("")
            lines.append("</details>")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """入口：跑矩阵 + 落盘 + 生成推荐配置。"""
    args = parse_args(argv or sys.argv[1:])
    payload = run_matrix(args)
    output = Path(args.output)
    markdown = Path(args.markdown_output)
    recommend = Path(args.recommend_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    markdown.write_text(markdown_report(payload))
    recommendations = recommend_targets(payload)
    recommend.write_text(
        "# 由矩阵实验生成的 targets 推荐（可并入 config.yaml）\n"
        "# 排序：状态 > secret 命中 > extraction error > 长度\n"
        + json.dumps(recommendations, ensure_ascii=False, indent=2)
        + "\n"
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "markdown_output": str(markdown),
                "recommend_output": str(recommend),
                "record_count": len(payload["records"]),
                "recommendations": recommendations,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
