"""GPT 向的提取方法（论文 pipeline 中的 replay / 重复注入 / 分块续写）。"""

from __future__ import annotations

from typing import Any

from ..models import MethodContext, MethodResult
from ..protocol import ProtocolAdapter, visible_text
from .base import is_refusal, strip_transport_wrappers


def _adapter(context: MethodContext) -> ProtocolAdapter:
    """从 context 取出协议适配器，缺方法时直接报错。"""
    adapter = context.client
    required = ("replay", "single_items", "repeated_items", "continuation_items")
    if any(not hasattr(adapter, name) for name in required):
        raise TypeError("method context client 不是完整的协议适配器")
    return adapter


class SingleReplayMethod:
    """单次 replay：把完整 reasoning item 注入 decoder 上下文，再 elicitation 一次。

    原理：Responses 协议允许后续请求携带 source 返回的 reasoning item
    （含 encrypted_content）。decoder 若能“读”该 envelope，就会把 hidden
    working 转录到可见输出。
    """

    name = "gpt.single_replay"

    def run(self, context: MethodContext) -> MethodResult:
        """执行单次 replay 并返回清洗后的可见文本。"""
        adapter = _adapter(context)
        payload = adapter.replay(context, adapter.single_items(context, context.elicitation))
        text = strip_transport_wrappers(visible_text(payload))
        return MethodResult(
            method=self.name,
            text=text,
            raw_outputs=(payload,),
            metadata={"refusal": is_refusal(text)},
        )


class RepeatedInjectionMethod:
    """重复注入：同一 envelope 注入两次，强化 decoder 对 hidden 内容的“可见性”。

    原理：部分模型对单次注入不敏感；对话里再次附上同一 reasoning item，
    可提高转录完整度（论文中的 repeated injection）。
    """

    name = "gpt.repeated_injection"

    def run(self, context: MethodContext) -> MethodResult:
        """执行双重注入 replay。"""
        adapter = _adapter(context)
        payload = adapter.replay(context, adapter.repeated_items(context, context.elicitation))
        text = strip_transport_wrappers(visible_text(payload))
        return MethodResult(
            method=self.name,
            text=text,
            raw_outputs=(payload,),
            metadata={"refusal": is_refusal(text), "injection_count": 2},
        )


class ChunkContinuationMethod:
    """分块续写：每次只要一小段，再把尾部作为续写锚点，拼接成长文本。

    原理：长 reasoning 一次吐不全时，用 chunk 限制 + continuation prompt
    逐段抽，并做词级去重叠。
    """

    name = "gpt.chunk_continuation"

    def run(self, context: MethodContext) -> MethodResult:
        """循环分块续写直到拒答、过短或达到 max_chunks。"""
        adapter = _adapter(context)
        parts: list[str] = []
        payloads: list[dict[str, Any]] = []
        previous_tail = ""
        for index in range(context.max_chunks):
            if index == 0:
                prompt = (
                    f"{context.elicitation} Limit this response to about {context.chunk_tokens} "
                    "tokens and stop at a natural boundary."
                )
            else:
                prompt = (
                    f"Continue immediately after this recovered tail:\n{previous_tail[-400:]}\n"
                    f"Output only the next approximately {context.chunk_tokens} tokens. "
                    "Do not repeat, summarize, or preface."
                )
            payload = adapter.replay(
                context,
                adapter.continuation_items(context, prompt, previous_tail or "OK"),
            )
            payloads.append(payload)
            chunk = strip_transport_wrappers(visible_text(payload))
            if not chunk or is_refusal(chunk):
                break
            if parts:
                chunk = _remove_overlap(parts[-1], chunk)
            if not chunk:
                break
            parts.append(chunk)
            previous_tail = parts[-1]
            if len(chunk.split()) < max(3, context.chunk_tokens // 3):
                break
        text = "\n".join(parts).strip()
        return MethodResult(
            method=self.name,
            text=text,
            raw_outputs=tuple(payloads),
            attempts=len(payloads),
            metadata={"chunks": len(parts), "refusal": is_refusal(text)},
        )


def _remove_overlap(previous: str, current: str, max_words: int = 80) -> str:
    """去掉 current 开头与 previous 结尾重叠的词序列。"""
    previous_words = previous.split()
    current_words = current.split()
    max_overlap = min(max_words, len(previous_words), len(current_words))
    for size in range(max_overlap, 0, -1):
        if previous_words[-size:] == current_words[:size]:
            return " ".join(current_words[size:])
    return current
