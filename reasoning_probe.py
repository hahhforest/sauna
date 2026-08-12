#!/usr/bin/env python3
"""Reasoning 恢复研究 harness 的 CLI 入口。

配置优先读项目内 config.yaml（见 config.example.yaml），
也可用环境变量 / CLI 覆盖。不依赖 ~/.minimax。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reasoning_recovery.config import build_settings, client_from_settings, load_app_config
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
from reasoning_recovery.protocol import adapter_for


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
    parser.add_argument("--config", default="", help="配置文件路径，默认 ./config.yaml")
    parser.add_argument("--base-url", default="", help="覆盖 upstream.base_url")
    parser.add_argument("--api-key", default="", help="覆盖 upstream.api_key")
    parser.add_argument("--source-model", default="")
    parser.add_argument("--decoder-model", default="")
    parser.add_argument("--source-profile", default="", help="config.models 中的档案名")
    parser.add_argument("--decoder-profile", default="", help="config.models 中的档案名")
    parser.add_argument(
        "--protocol",
        choices=("", "responses", "chat_completions", "anthropic_messages", "gemini"),
        default="",
    )
    parser.add_argument("--effort", default="")
    parser.add_argument("--method", default="gpt.single_replay")
    parser.add_argument("--fallback", default="", help="逗号分隔的备用方法")
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument(
        "--model-config",
        default="{}",
        help="JSON：模型相关协议/模板覆盖（与 config 合并，CLI 优先）",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="Key:Value",
        help="附加 HTTP header，可重复；覆盖 config 中同名键",
    )
    parser.add_argument("--output", default="", help="可选：把完整结果写入 JSON 文件")
    return parser.parse_args(argv)


def _parse_headers(items: list[str]) -> dict[str, str]:
    """解析 --header Key:Value 列表。"""
    headers: dict[str, str] = {}
    for item in items:
        if ":" not in item:
            raise ProbeError("CONFIG_INVALID_HEADER", f"header 格式应为 Key:Value，收到: {item}")
        key, value = item.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def main(argv: list[str] | None = None) -> int:
    """入口：执行一次恢复并打印完整 JSON 结果。"""
    args = _parse_args(argv or sys.argv[1:])
    try:
        model_config = json.loads(args.model_config)
    except json.JSONDecodeError:
        print(json.dumps({"error": {"code": "CONFIG_INVALID_MODEL_CONFIG"}}, ensure_ascii=False))
        return 2
    if not isinstance(model_config, dict):
        print(json.dumps({"error": {"code": "CONFIG_INVALID_MODEL_CONFIG"}}, ensure_ascii=False))
        return 2

    try:
        app = load_app_config(args.config or None)
        if args.base_url:
            from dataclasses import replace as dc_replace

            app = dc_replace(app, upstream=dc_replace(app.upstream, base_url=args.base_url.rstrip("/")))
        if args.api_key:
            from dataclasses import replace as dc_replace

            app = dc_replace(app, upstream=dc_replace(app.upstream, api_key=args.api_key))
        settings = build_settings(
            app,
            source_model=args.source_model or None,
            decoder_model=args.decoder_model or None,
            protocol=args.protocol or None,
            effort=args.effort or None,
            max_output_tokens=args.max_output_tokens,
            timeout=args.timeout,
            model_config=model_config,
            source_profile=args.source_profile or None,
            decoder_profile=args.decoder_profile or None,
            extra_headers=_parse_headers(args.header),
        )
        client = client_from_settings(settings)
        adapter = adapter_for(settings, client)
        engine = RecoveryEngine(adapter, method_registry())
        fallback = tuple(name for name in args.fallback.split(",") if name)
        result = engine.recover(
            settings,
            args.prompt,
            method=args.method,
            fallback=fallback,
        )
    except ProbeError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False))
        return 1 if exc.code not in {
            "CONFIG_MISSING",
            "CONFIG_MISSING_BASE_URL",
            "CONFIG_MISSING_API_KEY",
            "CONFIG_INVALID_MODEL_CONFIG",
            "CONFIG_INVALID_HEADER",
            "CONFIG_INVALID_YAML",
            "CONFIG_YAML_UNAVAILABLE",
        } else 2

    payload = result.as_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
    if result.replay.status == "fail":
        return 1
    if not result.text:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
