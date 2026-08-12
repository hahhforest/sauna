#!/usr/bin/env python3
"""对真实上游跑一次 GPT 恢复检查，输出完整恢复正文与四维证据。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reasoning_probe import method_registry
from reasoning_recovery.engine import RecoveryEngine
from reasoning_recovery.errors import ProbeError
from reasoning_recovery.models import Settings
from reasoning_recovery.protocol import UrllibJsonClient, adapter_for


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument(
        "--methods",
        default="gpt.single_replay",
        help="逗号分隔方法名；每个方法各自重新 harvest source",
    )
    parser.add_argument("--base-url", default=os.getenv("UPSTREAM_BASE_URL"))
    parser.add_argument("--source-model", default="gpt-5.6-sol")
    parser.add_argument("--decoder-model", default="gpt-5.6-luna")
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
    if not args.base_url:
        print(json.dumps({"error": {"code": "CONFIG_MISSING_BASE_URL"}}, ensure_ascii=False))
        return 2
    api_key = os.getenv("UPSTREAM_API_KEY")
    if not api_key:
        print(json.dumps({"error": {"code": "CONFIG_MISSING_API_KEY"}}, ensure_ascii=False))
        return 2

    settings = Settings(
        base_url=args.base_url,
        api_key=api_key,
        source_model=args.source_model,
        decoder_model=args.decoder_model,
        protocol="responses",
        effort=args.effort,
        max_output_tokens=args.max_output_tokens,
        timeout=args.timeout,
    )
    registry = method_registry()
    adapter = adapter_for(settings, UrllibJsonClient(settings.base_url, settings.api_key))
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
