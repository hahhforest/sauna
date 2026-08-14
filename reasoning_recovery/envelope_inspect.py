"""opaque envelope 的只读取证：protobuf 头部解析 + 熵测量。

参考 5SSjw/open-open-reasoning 的 decode_signature：signature 是
base64 protobuf 信封，外层头部（model name / block type / key id 等）
是明文的，密文在信封内部。本模块**只解析外层，不做任何解密**，
用于落盘证据（绑定模型名、密文熵）与 replay 路由参考。
"""

from __future__ import annotations

import base64
import binascii
import math
from typing import Any


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """读取一个 protobuf varint，返回 (值, 新位置)。"""
    value = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return value, pos


def _shannon_entropy(data: bytes) -> float:
    """字节级 Shannon 熵（bits/byte，8.0 = 完全随机）。"""
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    total = len(data)
    entropy = 0.0
    for count in counts:
        if count:
            prob = count / total
            entropy -= prob * math.log2(prob)
    return round(entropy, 4)


def _parse(data: bytes, depth: int) -> tuple[list[dict[str, Any]], bool]:
    """把 data 当 protobuf 消息解析。

    返回 (字段树, 是否完整消费)。嵌套的 length-delimited 字段会先按
    嵌套消息试探：完整消费 ≥ 80% 视为结构化消息，否则视为密文块。
    """
    fields: list[dict[str, Any]] = []
    pos = 0
    ok = False
    while pos < len(data) - 1:
        try:
            tag, pos = _read_varint(data, pos)
        except IndexError:
            break
        field = tag >> 3
        wire = tag & 0x7
        entry: dict[str, Any] = {"field": field, "wire": wire, "depth": depth}
        if wire == 0:  # varint
            try:
                value, pos = _read_varint(data, pos)
            except IndexError:
                break
            entry["value"] = value
        elif wire == 2:  # length-delimited
            try:
                size, pos = _read_varint(data, pos)
            except IndexError:
                break
            if size < 0 or pos + size > len(data):
                entry["error"] = "bad length"
                fields.append(entry)
                break
            chunk = data[pos : pos + size]
            pos += size
            entry["length"] = size
            if depth < 3:
                nested, complete = _parse(chunk, depth + 1)
                if complete and nested:
                    entry["nested"] = nested
                elif size >= 64:
                    entry["entropy_bits_per_byte"] = _shannon_entropy(chunk)
                    entry["preview"] = chunk[:24].hex()
                else:
                    entry["text"] = _as_text(chunk)
            else:
                entry["entropy_bits_per_byte"] = _shannon_entropy(chunk)
        elif wire in (1, 5):  # 定长
            if pos + 4 > len(data):
                break
            entry["value"] = data[pos : pos + 4].hex()
            pos += 4
        else:
            entry["error"] = f"unknown wire {wire}"
            fields.append(entry)
            break
        fields.append(entry)
        if pos >= len(data):
            ok = True
            break
    return fields, ok


def _as_text(chunk: bytes) -> str:
    """尽量按可打印字符串解码。"""
    try:
        text = chunk.decode("utf-8")
        return text if text.isprintable() else chunk.hex()
    except UnicodeDecodeError:
        return chunk.hex()


def _find_texts(fields: list[dict[str, Any]]) -> dict[str, str]:
    """在已解析字段树里找绑定模型名（#6）与块类型（#8）。"""
    found: dict[str, str] = {}
    for entry in fields:
        text = entry.get("text")
        if isinstance(text, str) and 3 <= len(text) <= 80:
            if entry.get("field") == 6:
                found.setdefault("bound_model", text)
            elif entry.get("field") == 8:
                found.setdefault("block_type", text)
        found.update(_find_texts(entry.get("nested") or []))
    return found


def inspect_envelope(value: str) -> dict[str, Any]:
    """对单个 opaque envelope 做只读取证，返回结构化证据。"""
    report: dict[str, Any] = {"raw_length": len(value)}
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        report["decode_error"] = str(exc)
        return report
    report["bytes"] = len(raw)
    report["entropy_bits_per_byte"] = _shannon_entropy(raw)
    fields, ok = _parse(raw, 0)
    report["fields"] = fields
    report["parsed_ok"] = ok
    report.update(_find_texts(fields))
    return report


def envelope_report(envelopes: list[Any]) -> list[dict[str, Any]]:
    """批量取证：多个 envelope 的摘要报告。"""
    reports = []
    for envelope in envelopes:
        report = inspect_envelope(envelope.value)
        report["path"] = envelope.path
        report["field"] = envelope.field
        reports.append(report)
    return reports
