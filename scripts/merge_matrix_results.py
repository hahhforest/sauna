#!/usr/bin/env python3
"""合并多个矩阵分片 JSON，输出完整研究汇总（含恢复正文）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="矩阵 JSON 分片路径")
    parser.add_argument("--output", default="runs/provider_matrix_combined.json")
    parser.add_argument("--markdown-output", default="runs/provider_matrix_combined.md")
    return parser.parse_args(argv)


def markdown_report(records: list[dict[str, Any]]) -> str:
    """生成合并后的 Markdown 报告，含恢复正文。"""
    lines = [
        "# 跨 Provider Reasoning 恢复矩阵（合并）",
        "",
        "完整研究记录：JSON 含全文；表格为摘要。",
        "",
        "| provider | source | decoder | effort | method | status | replay | provenance | coverage | fidelity | src_tok | rec_tok | ratio | elapsed | text_len |",
        "|---|---|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in records:
        dims = row.get("dimensions", {})
        coverage = dims.get("coverage", {})
        text = row.get("text") or ""
        lines.append(
            "| {provider} | {source} | {decoder} | {effort} | {method} | {status} | {replay} | {provenance} | {coverage_status} | {fidelity} | {source_tokens} | {recovered_tokens} | {ratio} | {elapsed} | {text_len} |".format(
                provider=row.get("provider", ""),
                source=row.get("source_model", ""),
                decoder=row.get("decoder_model", ""),
                effort=row.get("effort", ""),
                method=row.get("method", ""),
                status=row.get("status", ""),
                replay=dims.get("replay", {}).get("status", "-"),
                provenance=dims.get("provenance", {}).get("status", "-"),
                coverage_status=coverage.get("status", "-"),
                fidelity=dims.get("fidelity", {}).get("status", "-"),
                source_tokens=coverage.get("source_tokens", "-"),
                recovered_tokens=coverage.get("recovered_tokens", "-"),
                ratio=coverage.get("ratio", "-"),
                elapsed=row.get("elapsed_s", "-"),
                text_len=len(text) if text else row.get("text_length", "-"),
            )
        )

    lines.extend(["", "## 完整恢复正文", ""])
    for index, row in enumerate(records, 1):
        text = row.get("text")
        if not text:
            continue
        lines.append(
            f"### {index}. {row.get('provider')} | {row.get('source_model')} → "
            f"{row.get('decoder_model')} | {row.get('effort')} | {row.get('method')}"
        )
        lines.append("")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        # 若有候选文本，一并列出
        candidates = (row.get("method_metadata") or {}).get("candidate_texts") or []
        if candidates:
            lines.append("")
            lines.append("<details><summary>候选文本</summary>")
            lines.append("")
            for c_index, candidate in enumerate(candidates, 1):
                lines.append(f"**候选 {c_index}**")
                lines.append("")
                lines.append("```")
                lines.append(str(candidate))
                lines.append("```")
                lines.append("")
            lines.append("</details>")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """入口：去重合并分片并写出结果。"""
    args = parse_args(argv)
    records: list[dict] = []
    discoveries: list[dict] = []
    for name in args.inputs:
        payload = json.loads(Path(name).read_text())
        records.extend(payload.get("records", []))
        if payload.get("models_discovery"):
            discoveries.append(payload["models_discovery"])

    # 相同配置的后写覆盖先写；effort 是 key 的一部分
    deduped: dict[tuple, dict] = {}
    for record in records:
        key = tuple(
            record.get(field, "")
            for field in ("provider", "source_model", "decoder_model", "effort", "method")
        )
        deduped[key] = record
    records = list(deduped.values())
    records.sort(
        key=lambda row: (
            row.get("provider", ""),
            row.get("source_model", ""),
            row.get("decoder_model", ""),
            row.get("effort", ""),
            row.get("method", ""),
        )
    )
    model_ids = sorted(
        {
            item
            for discovery in discoveries
            for item in discovery.get("relevant_ids", [])
            if isinstance(item, str)
        }
    )
    output_payload = {
        "source_shards": args.inputs,
        "models_discovery": {
            "shard_count": len(discoveries),
            "relevant_ids": model_ids,
            "counts": sorted({discovery.get("count") for discovery in discoveries}),
        },
        "record_count": len(records),
        "records": records,
    }
    output = Path(args.output)
    markdown = Path(args.markdown_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2) + "\n")
    markdown.write_text(markdown_report(records))
    print(
        json.dumps(
            {
                "output": str(output),
                "markdown_output": str(markdown),
                "record_count": len(records),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
