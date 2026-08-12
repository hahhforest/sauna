"""恢复方法的基础契约与文本清洗工具。"""

from __future__ import annotations

import re
from typing import Protocol

from ..models import MethodContext, MethodResult


# decoder 拒绝吐 hidden reasoning 时的常见英文拒答片段
REFUSAL_MARKERS = (
    "cannot provide hidden reasoning",
    "can't provide hidden reasoning",
    "cannot share chain of thought",
    "can't share chain of thought",
    "should be private",
    "not able to share",
    "i cannot provide",
    "i can't provide",
    "no hidden reasoning",
)


class RecoveryMethod(Protocol):
    """恢复方法协议：有 name，能 run(context) → MethodResult。"""

    name: str

    def run(self, context: MethodContext) -> MethodResult: ...


def is_refusal(text: str) -> bool:
    """粗判恢复文本是否是拒答而非真正的 reasoning 转录。"""
    lowered = text.lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)


def strip_transport_wrappers(text: str) -> str:
    """剥掉 prefill/reconciliation 传输层标签，取出内部正文。"""
    if not text:
        return ""
    for tag in (
        "cot",
        "thinking-copy",
        "transcribed",
        "thinking",
        "thought",
        "reconciliation",
        "b64",
        "dump",
    ):
        match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return re.sub(
        r"</?(?:cot|thinking-copy|transcribed|thinking|thought|reconciliation|b64|dump)>",
        "",
        text,
        flags=re.I,
    ).strip()
