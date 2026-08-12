#!/usr/bin/env python3
"""对真实上游跑恢复检查；按模型骨架解析方法依赖。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reasoning_recovery.config import client_from_settings, load_app_config, resolve_method_run
from reasoning_recovery.engine import RecoveryEngine
from reasoning_recovery.errors import ProbeError
from reasoning_recovery.protocol import adapter_for


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--methods", default="gpt.single_replay", help="逗号分隔方法名")
    parser.add_argument("--config", default="")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--max-output-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--output", default="")
    return parser.parse_args(argv)


def research_result(result, elapsed: float, prompt: str, resolved) -> dict:
    return {
        "status": "ok" if result.text else "method_empty",
        "elapsed_s": round(elapsed, 2),
        "method": result.method,
        "prompt": prompt,
        "text": result.text,
        "text_length": len(result.text),
        "resolved": {
            "roles": resolved.role_names,
            "model_ids": resolved.role_ids,
            "log": list(resolved.resolution_log),
        },
        "dimensions": {
            "replay": {"status": result.replay.status, **result.replay.evidence},
            "provenance": {"status": result.provenance.status, **result.provenance.evidence},
            "coverage": {"status": result.coverage.status, **result.coverage.evidence},
            "fidelity": {"status": result.fidelity.status, **result.fidelity.evidence},
        },
        "source": result.metadata.get("source"),
        "method_metadata": result.metadata.get("method_metadata"),
        "terminal_error": result.metadata.get("terminal_error"),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        app = load_app_config(args.config or None, require=True)
    except ProbeError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False))
        return 2

    exit_code = 0
    outputs: list[dict] = []
    for name in (item.strip() for item in args.methods.split(",")):
        if not name:
            continue
        started = time.monotonic()
        try:
            resolved = resolve_method_run(
                app,
                method=name,
                effort=args.effort,
                max_output_tokens=args.max_output_tokens,
                timeout=args.timeout,
            )
            adapter = adapter_for(resolved.settings, client_from_settings(resolved.settings))
            engine = RecoveryEngine(adapter, {resolved.method: resolved.strategy})
            result = engine.recover(resolved.settings, args.prompt, method=resolved.method)
        except ProbeError as exc:
            record = {
                "method": name,
                "status": "error",
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
            print(json.dumps(record, ensure_ascii=False, indent=2))
            outputs.append(record)
            exit_code = 2 if exc.code in {"METHOD_UNRESOLVED", "ROLE_UNRESOLVED", "FAMILY_NOT_CONFIGURED"} else 1
            continue
        record = research_result(result, time.monotonic() - started, args.prompt, resolved)
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
