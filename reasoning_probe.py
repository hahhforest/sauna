#!/usr/bin/env python3
"""Reasoning 恢复研究 harness 的 CLI 入口。

从 source 模型采集 opaque reasoning envelope，再用 decoder 做协议层 replay，
输出完整恢复正文与四维证据（replay / provenance / coverage / fidelity）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from reasoning_recovery.engine import RecoveryEngine
from reasoning_recovery.errors import ProbeError
from reasoning_recovery.methods import (
    BestOfNMethod,
    ClaudeFuzzyExtractionMethod,
    ClaudeReconciliationMethod,
    ChunkContinuationMethod,
    GeminiFuzzyExtractionMethod,
    GeminiReconciliationMethod,
    ReconciliationMethod,
    RepeatedInjectionMethod,
    SingleReplayMethod,
    TerraFallbackMethod,
)
from reasoning_recovery.models import Settings
from reasoning_recovery.protocol import UrllibJsonClient, adapter_for


def method_registry() -> dict[str, object]:
    """注册全部研究用恢复方法。"""
    single = SingleReplayMethod()
    repeated = RepeatedInjectionMethod()
    chunked = ChunkContinuationMethod()
    return {
        single.name: single,
        repeated.name: repeated,
        chunked.name: chunked,
        "gpt.single_best_of_3": BestOfNMethod(single, n=3, name="gpt.single_best_of_3"),
        "gpt.repeated_best_of_3": BestOfNMethod(repeated, n=3, name="gpt.repeated_best_of_3"),
        "gpt.luna_then_terra": TerraFallbackMethod(
            single,
            SingleReplayMethod(),
            fallback_model="gpt-5.6-terra",
        ),
        "gpt.reconcile_with_terra": ReconciliationMethod(
            single,
            reconciler_model="gpt-5.6-terra",
        ),
        "claude.fuzzy_prefill": ClaudeFuzzyExtractionMethod(),
        "claude.reconciliation": ClaudeReconciliationMethod(),
        "gemini.fuzzy_prefill": GeminiFuzzyExtractionMethod(),
        "gemini.reconciliation": GeminiReconciliationMethod(),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="研究用用户 prompt")
    parser.add_argument("--base-url", default=os.getenv("UPSTREAM_BASE_URL"))
    parser.add_argument("--source-model", default="gpt-5.6-sol")
    parser.add_argument("--decoder-model", default="gpt-5.6-luna")
    parser.add_argument(
        "--protocol",
        choices=("responses", "chat_completions", "anthropic_messages", "gemini"),
        default="responses",
    )
    parser.add_argument("--effort", default="high")
    parser.add_argument("--method", default="gpt.single_replay")
    parser.add_argument("--fallback", default="", help="逗号分隔的备用方法")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--model-config",
        default="{}",
        help="JSON：模型相关协议/模板覆盖（prefill_tag、signature_fields 等）",
    )
    parser.add_argument("--output", default="", help="可选：把完整结果写入 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """入口：执行一次恢复并打印完整 JSON 结果。"""
    args = _parse_args(argv or sys.argv[1:])
    if not args.base_url:
        print(json.dumps({"error": {"code": "CONFIG_MISSING_BASE_URL"}}, ensure_ascii=False))
        return 2
    api_key = os.getenv("UPSTREAM_API_KEY")
    if not api_key:
        print(json.dumps({"error": {"code": "CONFIG_MISSING_API_KEY"}}, ensure_ascii=False))
        return 2

    try:
        model_config = json.loads(args.model_config)
    except json.JSONDecodeError:
        print(json.dumps({"error": {"code": "CONFIG_INVALID_MODEL_CONFIG"}}, ensure_ascii=False))
        return 2
    if not isinstance(model_config, dict):
        print(json.dumps({"error": {"code": "CONFIG_INVALID_MODEL_CONFIG"}}, ensure_ascii=False))
        return 2

    settings = Settings(
        base_url=args.base_url,
        api_key=api_key,
        source_model=args.source_model,
        decoder_model=args.decoder_model,
        protocol=args.protocol,
        effort=args.effort,
        max_output_tokens=args.max_output_tokens,
        timeout=args.timeout,
        model_config=model_config,
    )
    registry = method_registry()
    client = UrllibJsonClient(settings.base_url, settings.api_key)
    adapter = adapter_for(settings, client)
    engine = RecoveryEngine(adapter, registry)
    fallback = tuple(name for name in args.fallback.split(",") if name)
    try:
        result = engine.recover(
            settings,
            args.prompt,
            method=args.method,
            fallback=fallback,
        )
    except ProbeError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False))
        return 1

    payload = result.as_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        path = __import__("pathlib").Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
    if result.replay.status == "fail":
        return 1
    if not result.text:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
