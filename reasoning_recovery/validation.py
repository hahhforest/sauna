"""四维恢复证据的验证器。"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from .models import DimensionResult, HarvestRecord, MethodResult


TokenCounter = Callable[[str], int]


def whitespace_token_count(text: str) -> int:
    """按空白分词计数（coverage 估计用，非 provider tokenizer）。"""
    return len(text.split())


def validate_replay(method_result: MethodResult) -> DimensionResult:
    """验证 decoder 是否至少返回了响应。"""
    if not method_result.raw_outputs:
        return DimensionResult("fail", {"response_count": 0})
    return DimensionResult(
        "success",
        {
            "response_count": len(method_result.raw_outputs),
            "attempts": method_result.attempts,
            "refusal": bool(method_result.metadata.get("refusal", False)),
        },
    )


def validate_provenance(
    harvest: HarvestRecord,
    recovered_text: str,
    *,
    baseline_text: str | None = None,
    provenance_safe: bool = True,
) -> DimensionResult:
    """用 marker / planted secret 判断恢复文本是否来自 source hidden reasoning。

    marker 与 secret 只出现在 source instruction，绝不进入 replay 的明文上下文；
    命中 decoder 输出即为 source-only 证据。reconciliation 等方法会把候选写进
    prompt，此时 provenance_safe=False → invalidated。
    """
    marker_hit = bool(harvest.marker) and harvest.marker in recovered_text
    secret_hit = bool(harvest.secret) and harvest.secret in recovered_text
    baseline_hit = None if baseline_text is None else harvest.marker in baseline_text
    evidence = {
        "marker": harvest.marker,
        "marker_hit": marker_hit,
        "secret": harvest.secret,
        "secret_hit": secret_hit,
        "baseline_marker_hit": baseline_hit,
        "provenance_safe": provenance_safe,
    }
    if not provenance_safe:
        return DimensionResult("invalidated", evidence)
    if secret_hit:
        # planted secret 逐字符命中 = 真解封的最强证据
        return DimensionResult("supported", evidence)
    if marker_hit and baseline_hit is False:
        return DimensionResult("supported", evidence)
    if marker_hit:
        return DimensionResult("partial", evidence)
    if baseline_text is None:
        return DimensionResult("not_evaluated", evidence)
    return DimensionResult("fail", evidence)


def validate_coverage(
    harvest: HarvestRecord,
    recovered_text: str,
    *,
    token_counter: TokenCounter = whitespace_token_count,
) -> DimensionResult:
    """估计恢复覆盖率 = recovered_tokens / source_reasoning_tokens。

    论文 C.2 的 extraction error 同源指标：billing token 数视为精确。
    同时落字符数，避免 CJK 等空白分词偏差误导 ratio。
    """
    source_tokens = harvest.source_reasoning_tokens
    recovered_tokens = token_counter(recovered_text)
    evidence = {
        "source_tokens": source_tokens,
        "recovered_tokens": recovered_tokens,
        "recovered_text_length": len(recovered_text),
        "recovered_chars": len(recovered_text),
    }
    if not source_tokens:
        return DimensionResult("unknown", evidence)
    ratio = recovered_tokens / source_tokens
    return DimensionResult("estimated", {**evidence, "ratio": round(ratio, 4)})


def validate_fidelity(
    harvest: HarvestRecord,
    recovered_text: str,
    *,
    candidate_texts: Iterable[str] = (),
    semantic_verifier: Callable[[str, HarvestRecord], bool] | None = None,
) -> DimensionResult:
    """根据候选一致性与可选语义 verifier 评估保真度。"""
    candidates = [text for text in candidate_texts if text]
    if recovered_text and recovered_text not in candidates:
        candidates.append(recovered_text)
    consistency = _consistency(candidates)
    semantic_verified = None
    if semantic_verifier is not None:
        semantic_verified = bool(semantic_verifier(recovered_text, harvest))
    evidence: dict[str, Any] = {
        "candidate_count": len(candidates),
        "repeat_consistency": consistency,
        "semantic_verified": semantic_verified,
        # 研究用途：保留候选全文
        "candidate_texts": list(candidates),
    }
    if not recovered_text:
        return DimensionResult("fail", evidence)
    if semantic_verified is True and (consistency is None or consistency >= 0.8):
        return DimensionResult("supported", evidence)
    if consistency is not None and consistency >= 0.8:
        return DimensionResult("partial", evidence)
    return DimensionResult("unknown", evidence)


def validate_method_result(
    harvest: HarvestRecord,
    method_result: MethodResult,
    *,
    baseline_text: str | None = None,
    token_counter: TokenCounter = whitespace_token_count,
    semantic_verifier: Callable[[str, HarvestRecord], bool] | None = None,
) -> tuple[DimensionResult, DimensionResult, DimensionResult, DimensionResult]:
    """对一次方法结果跑齐四维验证。"""
    candidate_texts = method_result.metadata.get("candidate_texts", ())
    return (
        validate_replay(method_result),
        validate_provenance(
            harvest,
            method_result.text,
            baseline_text=baseline_text,
            provenance_safe=method_result.metadata.get("provenance_safe", True),
        ),
        validate_coverage(harvest, method_result.text, token_counter=token_counter),
        validate_fidelity(
            harvest,
            method_result.text,
            candidate_texts=candidate_texts,
            semantic_verifier=semantic_verifier,
        ),
    )


def _consistency(texts: list[str]) -> float | None:
    """多候选两两 Jaccard 平均；不足 2 条返回 None。"""
    if len(texts) < 2:
        return None
    scores: list[float] = []
    for index, left in enumerate(texts):
        for right in texts[index + 1 :]:
            scores.append(_token_jaccard(left, right))
    return round(sum(scores) / len(scores), 4) if scores else None


def _token_jaccard(left: str, right: str) -> float:
    """token 集合 Jaccard 相似度。"""
    left_tokens = set(re.findall(r"\S+", left))
    right_tokens = set(re.findall(r"\S+", right))
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
