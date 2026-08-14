#!/usr/bin/env python3
"""Reasoning 恢复研究 harness 的 CLI 入口。

配置 = 模型骨架（你配了哪些模型）。
方法 = 角色依赖（需要 source/decoder/reconciler…）。
缺模型会明确报错，并沿方法 fallback 链尝试下一个。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reasoning_recovery.config import (
    client_from_settings,
    list_runnable_methods,
    load_app_config,
    resolve_method_run,
)
from reasoning_recovery.engine import RecoveryEngine
from reasoning_recovery.errors import ProbeError
from reasoning_recovery.protocol import adapter_for


def method_registry() -> dict[str, object]:
    """返回名称 → 策略实例（测试/兼容用；生产路径走 resolve_method_run）。"""
    from reasoning_recovery.methods import (
        BestOfNMethod,
        ChunkContinuationMethod,
        ClaudeFuzzyExtractionMethod,
        ClaudeReconciliationMethod,
        GeminiFuzzyExtractionMethod,
        GeminiReconciliationMethod,
        ProviderSingleReplayMethod,
        ReconciliationMethod,
        RepeatedInjectionMethod,
        SingleReplayMethod,
        TerraFallbackMethod,
    )

    single = SingleReplayMethod()
    repeated = RepeatedInjectionMethod()
    return {
        "gpt.single_replay": single,
        "gpt.repeated_injection": repeated,
        "gpt.chunk_continuation": ChunkContinuationMethod(),
        "gpt.single_best_of_n": BestOfNMethod(SingleReplayMethod(), n=50, name="gpt.single_best_of_n"),
        "gpt.repeated_best_of_n": BestOfNMethod(RepeatedInjectionMethod(), n=50, name="gpt.repeated_best_of_n"),
        "gpt.luna_then_terra": TerraFallbackMethod(
            SingleReplayMethod(), SingleReplayMethod(), fallback_model="gpt-5.6-terra"
        ),
        "gpt.reconcile_with_terra": ReconciliationMethod(
            SingleReplayMethod(), reconciler_model="gpt-5.6-terra"
        ),
        "claude.single_replay": ProviderSingleReplayMethod(),
        "claude.fuzzy_prefill": ClaudeFuzzyExtractionMethod(),
        "claude.reconciliation": ClaudeReconciliationMethod(),
        "gemini.fuzzy_prefill": GeminiFuzzyExtractionMethod(),
        "gemini.reconciliation": GeminiReconciliationMethod(),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="", help="研究用用户 prompt")
    parser.add_argument("--config", default="", help="配置文件路径，默认 ./config.yaml")
    parser.add_argument("--list-methods", action="store_true", help="列出当前配置可跑的方法后退出")
    parser.add_argument("--family", default="", help="未指定 method 时的家族：gpt|claude|gemini")
    parser.add_argument("--target", default="", help="目标模型逻辑名；固定 source 并按 targets 段选方法链")
    parser.add_argument("--secret", default="", help="planted-secret 判别协议的秘密（只进 hidden reasoning）")
    parser.add_argument("--method", default="", help="方法名；缺省按家族默认链解析")
    parser.add_argument("--fallback", default="", help="逗号分隔的备用方法（方法级 fallback）")
    parser.add_argument("--effort", default="")
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument(
        "--model-config",
        default="{}",
        help="JSON：额外 model_config 覆盖",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="Key:Value",
        help="附加 HTTP header，可重复",
    )
    parser.add_argument("--output", default="", help="完整结果写入 JSON")
    return parser.parse_args(argv)


def _parse_headers(items: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items:
        if ":" not in item:
            raise ProbeError("CONFIG_INVALID_HEADER", f"header 格式应为 Key:Value，收到: {item}")
        key, value = item.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def main(argv: list[str] | None = None) -> int:
    """入口。"""
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
        app = load_app_config(args.config or None, require=True)
        if args.list_methods:
            runnable = list_runnable_methods(app)
            print(
                json.dumps(
                    {
                        "configured_models": {
                            name: {
                                "id": m.model_id,
                                "family": m.family,
                                "roles": sorted(m.roles),
                            }
                            for name, m in app.models.items()
                        },
                        "runnable_methods": runnable,
                        "families": sorted(app.families()),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if not args.prompt:
            print(json.dumps({"error": {"code": "PROMPT_REQUIRED", "message": "请提供 prompt"}}, ensure_ascii=False))
            return 2

        fallback = tuple(n for n in args.fallback.split(",") if n.strip())
        resolved = resolve_method_run(
            app,
            method=args.method or None,
            family=args.family or None,
            target=args.target or None,
            fallback_methods=fallback,
            effort=args.effort or None,
            max_output_tokens=args.max_output_tokens,
            timeout=args.timeout,
            extra_headers=_parse_headers(args.header),
            model_config=model_config,
        )
        client = client_from_settings(resolved.settings)
        adapter = adapter_for(resolved.settings, client)
        engine = RecoveryEngine(adapter, {resolved.method: resolved.strategy})
        result = engine.recover(
            resolved.settings,
            args.prompt,
            method=resolved.method,
            secret=args.secret or None,
        )
    except ProbeError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False, indent=2))
        config_codes = {
            "CONFIG_MISSING",
            "CONFIG_MISSING_BASE_URL",
            "CONFIG_MISSING_API_KEY",
            "CONFIG_INVALID_HEADER",
            "CONFIG_INVALID_YAML",
            "CONFIG_YAML_UNAVAILABLE",
            "FAMILY_NOT_CONFIGURED",
            "METHOD_UNRESOLVED",
            "ROLE_UNRESOLVED",
            "PROMPT_REQUIRED",
        }
        return 2 if exc.code in config_codes else 1

    payload = result.as_dict()
    payload["resolved"] = {
        "method": resolved.method,
        "family": resolved.family,
        "roles": resolved.role_names,
        "model_ids": resolved.role_ids,
        "log": list(resolved.resolution_log),
    }
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
