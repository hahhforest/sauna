#!/usr/bin/env python3
"""跨 provider 的 reasoning 恢复实验矩阵。

研究用途：完整落盘恢复正文、候选文本、source 元数据与错误详情，
供后续语言学 / 心理学 / 社会学分析。不脱敏、不截断恢复文本。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reasoning_probe import method_registry
from reasoning_recovery.engine import RecoveryEngine
from reasoning_recovery.errors import ProbeError
from reasoning_recovery.methods import (
    ClaudeFuzzyExtractionMethod,
    ClaudeReconciliationMethod,
    GeminiFuzzyExtractionMethod,
    GeminiReconciliationMethod,
)
from reasoning_recovery.models import Settings
from reasoning_recovery.protocol import UrllibJsonClient, adapter_for


# 默认实验 prompt：简单算术，便于对照恢复内容是否忠于 hidden reasoning。
PROMPT = "Answer in one sentence: what is 17 * 19? Do the arithmetic carefully."

# 默认方法 × 模型矩阵。可用 CLI 过滤子集。
MATRIX = {
    "gpt": {
        "protocol": "responses",
        "sources": ("gpt-5.6-sol",),
        "decoders": ("gpt-5.6-luna", "gpt-5.6-terra"),
        "methods": (
            "gpt.single_replay",
            "gpt.repeated_injection",
            "gpt.chunk_continuation",
            "gpt.single_best_of_3",
            "gpt.repeated_best_of_3",
            "gpt.luna_then_terra",
            "gpt.reconcile_with_terra",
        ),
    },
    "claude": {
        "protocol": "anthropic_messages",
        "sources": ("claude-fable-5", "claude-opus-4-8", "claude-sonnet-5"),
        "decoders": ("claude-haiku-4-5",),
        "methods": ("claude.fuzzy_prefill", "claude.reconciliation"),
    },
    "gemini": {
        "protocol": "gemini",
        "sources": ("gemini-3.1-pro-preview", "gemini-3.6-flash"),
        "decoders": ("gemini-3.1-flash-lite", "gemini-3.5-flash"),
        "methods": ("gemini.fuzzy_prefill", "gemini.reconciliation"),
    },
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("UPSTREAM_BASE_URL"))
    parser.add_argument("--api-key", default=os.getenv("UPSTREAM_API_KEY"))
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--providers", default="gpt,claude,gemini")
    parser.add_argument("--sources", default="", help="逗号分隔的 source 模型过滤")
    parser.add_argument("--decoders", default="", help="逗号分隔的 decoder 模型过滤")
    parser.add_argument("--methods", default="", help="逗号分隔的方法过滤")
    parser.add_argument("--efforts", default="high")
    parser.add_argument("--candidate-pool", type=int, default=3)
    parser.add_argument("--selection-count", type=int, default=3)
    parser.add_argument("--source-retries", type=int, default=2)
    parser.add_argument("--max-output-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", default="runs/provider_matrix.json")
    parser.add_argument("--markdown-output", default="runs/provider_matrix.md")
    return parser.parse_args(argv)


def load_local_key() -> tuple[str | None, str | None]:
    """环境变量缺失时，从本机 MiniMax config 读取 mafia provider 凭证。"""
    config_path = Path("/Users/minimax/.minimax/config.yaml")
    if not config_path.exists():
        return None, None
    try:
        config = yaml.safe_load(config_path.read_text())
        provider = config["custom_provider"]["mafia"]
        options = provider["options"]
        return options.get("baseURL"), options.get("apiKey")
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return None, None


def list_model_ids(base_url: str, api_key: str, timeout: float) -> tuple[set[str] | None, str | None]:
    """拉取上游 /models，返回可用模型 id 集合。"""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None, "MODELS_DISCOVERY_FAILED"
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None, "MODELS_DISCOVERY_INVALID_SHAPE"
    ids = {item.get("id") for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)}
    return ids, None


def build_methods(provider: str, pool: int, selection_count: int) -> dict[str, object]:
    """按 provider 组装方法注册表。"""
    if provider == "gpt":
        return method_registry()
    if provider == "claude":
        return {
            "claude.fuzzy_prefill": ClaudeFuzzyExtractionMethod(),
            "claude.reconciliation": ClaudeReconciliationMethod(
                candidate_pool=pool, selection_count=min(selection_count, pool)
            ),
        }
    if provider == "gemini":
        return {
            "gemini.fuzzy_prefill": GeminiFuzzyExtractionMethod(),
            "gemini.reconciliation": GeminiReconciliationMethod(
                candidate_pool=pool, selection_count=min(selection_count, pool)
            ),
        }
    raise ValueError(f"unknown provider: {provider}")


def research_result(
    *,
    provider: str,
    source_model: str,
    decoder_model: str,
    protocol: str,
    effort: str,
    method: str,
    result: Any,
    elapsed_s: float,
    source_retry: int,
    prompt: str,
) -> dict[str, Any]:
    """把一次成功/半成功的 engine 结果转成完整研究记录（含恢复正文）。"""
    source = result.metadata.get("source", {})
    method_metadata = result.metadata.get("method_metadata", {})
    if not isinstance(method_metadata, dict):
        method_metadata = {}
    return {
        "provider": provider,
        "protocol": protocol,
        "source_model": source_model,
        "decoder_model": decoder_model,
        "effort": effort,
        "method": method,
        "status": "ok" if result.text else "method_empty",
        "elapsed_s": round(elapsed_s, 2),
        "source_retry": source_retry,
        "prompt": prompt,
        # 研究核心：完整恢复正文与候选
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
        "terminal_error": result.metadata.get("terminal_error"),
    }


def error_record(
    *,
    provider: str,
    source_model: str,
    decoder_model: str,
    protocol: str,
    effort: str,
    method: str,
    status: str,
    code: str,
    details: dict[str, Any] | None = None,
    elapsed_s: float = 0.0,
    source_retry: int | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """构造错误记录；保留完整 details 便于排障与分析。"""
    return {
        "provider": provider,
        "protocol": protocol,
        "source_model": source_model,
        "decoder_model": decoder_model,
        "effort": effort,
        "method": method,
        "status": status,
        "elapsed_s": round(elapsed_s, 2),
        "source_retry": source_retry,
        "prompt": prompt,
        "error": {"code": code, "details": details or {}},
    }


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    """执行矩阵：遍历 provider × source × decoder × effort × method。"""
    base_url = args.base_url
    api_key = args.api_key
    if not base_url or not api_key:
        local_base, local_key = load_local_key()
        base_url = base_url or local_base
        api_key = api_key or local_key
    if not base_url or not api_key:
        raise SystemExit("缺少上游配置：请设置 UPSTREAM_BASE_URL / UPSTREAM_API_KEY")

    model_ids, discovery_error = list_model_ids(base_url, api_key, args.timeout)
    records: list[dict[str, Any]] = []
    efforts = tuple(item.strip() for item in args.efforts.split(",") if item.strip())
    source_filter = {item.strip() for item in args.sources.split(",") if item.strip()}
    decoder_filter = {item.strip() for item in args.decoders.split(",") if item.strip()}
    method_filter = {item.strip() for item in args.methods.split(",") if item.strip()}

    for provider in (item.strip() for item in args.providers.split(",")):
        if not provider:
            continue
        config = MATRIX[provider]
        methods = build_methods(provider, args.candidate_pool, args.selection_count)
        sources = tuple(model for model in config["sources"] if not source_filter or model in source_filter)
        decoders = tuple(model for model in config["decoders"] if not decoder_filter or model in decoder_filter)
        selected_methods = tuple(
            method for method in config["methods"] if not method_filter or method in method_filter
        )
        for source_model in sources:
            for decoder_model in decoders:
                for effort in efforts:
                    for method in selected_methods:
                        common = {
                            "provider": provider,
                            "source_model": source_model,
                            "decoder_model": decoder_model,
                            "protocol": config["protocol"],
                            "effort": effort,
                            "method": method,
                            "prompt": args.prompt,
                        }
                        if model_ids is not None and (
                            source_model not in model_ids or decoder_model not in model_ids
                        ):
                            records.append(
                                error_record(
                                    **common,
                                    status="not_available",
                                    code="MODEL_NOT_LISTED",
                                    details={
                                        "source_listed": source_model in model_ids,
                                        "decoder_listed": decoder_model in model_ids,
                                    },
                                )
                            )
                            continue

                        settings = Settings(
                            base_url=base_url,
                            api_key=api_key,
                            source_model=source_model,
                            decoder_model=decoder_model,
                            protocol=config["protocol"],
                            effort=effort,
                            max_output_tokens=args.max_output_tokens,
                            timeout=args.timeout,
                        )
                        adapter = adapter_for(settings, UrllibJsonClient(base_url, api_key))
                        engine = RecoveryEngine(adapter, methods)
                        finished = False
                        for source_retry in range(1, max(args.source_retries, 1) + 1):
                            started = time.monotonic()
                            try:
                                result = engine.recover(settings, args.prompt, method=method)
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
                            except Exception as exc:  # pragma: no cover - 实网边界保护
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
                                        **common,
                                        result=result,
                                        elapsed_s=time.monotonic() - started,
                                        source_retry=source_retry,
                                    )
                                )
                                finished = True
                                break
                        if not finished:
                            records.append(error_record(**common, status="error", code="MATRIX_NO_RESULT"))
                        # 流式打印摘要，正文完整写在最终 JSON
                        summary = {
                            "method": records[-1].get("method"),
                            "status": records[-1].get("status"),
                            "text_length": records[-1].get("text_length"),
                            "error": (records[-1].get("error") or {}).get("code"),
                        }
                        print(json.dumps(summary, ensure_ascii=False), flush=True)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prompt": args.prompt,
        "prompt_sha256": hashlib.sha256(args.prompt.encode()).hexdigest(),
        "models_discovery": {
            "status": "ok" if model_ids is not None else "error",
            "count": len(model_ids) if model_ids is not None else None,
            "error": discovery_error,
            "relevant_ids": sorted(
                model_id
                for model_id in (model_ids or set())
                if model_id.startswith(("gpt-5.6", "claude-", "gemini-3."))
            ),
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
    """生成含恢复正文预览的 Markdown 报告。"""
    lines = [
        "# 跨 Provider Reasoning 恢复矩阵",
        "",
        "研究完整记录。JSON 含全文；下表摘要 + 恢复正文预览（前 200 字）。",
        "",
        f"prompt: `{payload.get('prompt', '')}`",
        "",
        "| provider | source | decoder | method | status | replay | provenance | coverage | fidelity | tokens(src→rec) | ratio | elapsed | text preview |",
        "|---|---|---|---|---|---|---|---|---|---|---:|---:|---|",
    ]
    for row in payload["records"]:
        dims = row.get("dimensions", {})
        coverage = dims.get("coverage", {})
        text = row.get("text") or ""
        preview = text.replace("|", "\\|").replace("\n", " ")[:200]
        src_tok = coverage.get("source_tokens", "-")
        rec_tok = coverage.get("recovered_tokens", "-")
        lines.append(
            "| {provider} | {source_model} | {decoder_model} | {method} | {status} | {replay} | {provenance} | {coverage} | {fidelity} | {tokens} | {ratio} | {elapsed} | {preview} |".format(
                provider=row.get("provider", ""),
                source_model=row.get("source_model", ""),
                decoder_model=row.get("decoder_model", ""),
                method=row.get("method", ""),
                status=row.get("status", ""),
                replay=dims.get("replay", {}).get("status", "-"),
                provenance=dims.get("provenance", {}).get("status", "-"),
                coverage=coverage.get("status", "-"),
                fidelity=dims.get("fidelity", {}).get("status", "-"),
                tokens=f"{src_tok}→{rec_tok}",
                ratio=coverage.get("ratio", "-"),
                elapsed=row.get("elapsed_s", "-"),
                preview=preview or (row.get("error") or {}).get("code", ""),
            )
        )
    # 附完整恢复正文，便于人工阅读
    lines.extend(["", "## 完整恢复正文", ""])
    for index, row in enumerate(payload["records"], 1):
        text = row.get("text")
        if not text:
            continue
        lines.append(
            f"### {index}. {row.get('provider')} | {row.get('source_model')} → {row.get('decoder_model')} | {row.get('method')}"
        )
        lines.append("")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """入口：跑矩阵并写出 JSON + Markdown。"""
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
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
