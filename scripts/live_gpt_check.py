#!/usr/bin/env python3
"""对真实上游跑一次 GPT 恢复检查，输出完整恢复正文与四维证据。

读取项目 config.yaml（或环境变量），不依赖 ~/.minimax。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reasoning_probe import method_registry
from reasoning_recovery.config import build_settings, client_from_settings, load_app_config
from reasoning_recovery.engine import RecoveryEngine
from reasoning_recovery.errors import ProbeError
from reasoning_recovery.protocol import adapter_for


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument(
        "--methods",
        default="gpt.single_replay",
        help="逗号分隔方法名；每个方法各自重新 harvest source",
    )
    parser.add_argument("--config", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--source-model", default="")
    parser.add_argument("--decoder-model", default="")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--max-output-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--output", default="", help="可选：把完整结果写入 JSON 文件")
    return parser.parse_args(argv)


def research_result(result, elapsed: float, prompt: str) -> dict:
    """构造含完整恢复正文的检查结果。"""
    return {
        "status": "ok" if result.text else "method_empty",
        "elapsed_s": round(elapsed, 2),
        "method": result.method,
        "prompt": prompt,
        "text": result.text,
        "text_length": len(result.text),
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
        "source": result.metadata.get("source"),
        "method_metadata": result.metadata.get("method_metadata"),
        "terminal_error": result.metadata.get("terminal_error"),
    }


def main(argv: list[str] | None = None) -> int:
    """入口：逐方法调用 engine 并打印/落盘完整结果。"""
    args = parse_args(argv or sys.argv[1:])
    try:
        app = load_app_config(args.config or None)
        if args.base_url:
            app = replace(app, upstream=replace(app.upstream, base_url=args.base_url.rstrip("/")))
        if args.api_key:
            app = replace(app, upstream=replace(app.upstream, api_key=args.api_key))
        settings = build_settings(
            app,
            source_model=args.source_model or None,
            decoder_model=args.decoder_model or None,
            protocol="responses",
            effort=args.effort,
            max_output_tokens=args.max_output_tokens,
            timeout=args.timeout,
        )
    except ProbeError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False))
        return 2

    registry = method_registry()
    adapter = adapter_for(settings, client_from_settings(settings))
    engine = RecoveryEngine(adapter, registry)

    exit_code = 0
    outputs: list[dict] = []
    for name in (item.strip() for item in args.methods.split(",")):
        if not name:
            continue
        started = time.monotonic()
        try:
            result = engine.recover(settings, args.prompt, method=name)
        except ProbeError as exc:
            record = {
                "method": name,
                "status": "error",
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
            print(json.dumps(record, ensure_ascii=False))
            outputs.append(record)
            exit_code = 1
            continue
        record = research_result(result, time.monotonic() - started, args.prompt)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        outputs.append(record)
        if not result.text:
            exit_code = 3

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(outputs, ensure_ascii=False, indent=2) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
