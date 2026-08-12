"""候选选择、fallback 与 reconciliation 组合方法。"""

from __future__ import annotations

from dataclasses import replace

from ..errors import ProbeError
from ..models import MethodContext, MethodResult
from ..protocol import ProtocolAdapter, visible_text
from .base import RecoveryMethod, is_refusal, strip_transport_wrappers


class BestOfNMethod:
    """同一基础方法跑 N 次，按 marker 命中与长度选最优候选。

    原理：单次 transcription 有噪声；独立采样多次后，优先选命中 marker、
    token 数接近 source reasoning tokens 的候选。
    """

    def __init__(self, base: RecoveryMethod, n: int = 3, name: str | None = None) -> None:
        """Args:
            base: 被重复调用的基础方法。
            n: 采样次数。
            name: 注册名；默认 base.name.best_of_n。
        """
        if n < 1:
            raise ValueError("best-of-n 要求 n >= 1")
        self.base = base
        self.n = n
        self.name = name or f"{base.name}.best_of_{n}"

    def run(self, context: MethodContext) -> MethodResult:
        """采样 N 次并选出最高分候选；候选全文写入 metadata。"""
        candidates: list[MethodResult] = []
        errors: list[ProbeError] = []
        for _ in range(self.n):
            try:
                candidates.append(self.base.run(context))
            except ProbeError as exc:
                errors.append(exc)
        if not candidates:
            if errors:
                raise ProbeError(
                    "BEST_OF_N_EXHAUSTED",
                    "best-of-n 全部候选失败",
                    details={
                        "candidate_count": self.n,
                        "candidate_errors": [error.code for error in errors],
                    },
                ) from errors[-1]
            return MethodResult(
                method=self.name,
                text="",
                attempts=self.n,
                metadata={"candidate_count": 0, "candidate_errors": []},
            )
        selected = max(candidates, key=lambda result: _candidate_score(result.text, context))
        return MethodResult(
            method=self.name,
            text=selected.text,
            raw_outputs=tuple(output for candidate in candidates for output in candidate.raw_outputs),
            attempts=sum(candidate.attempts for candidate in candidates) + len(errors),
            cost={"candidates": self.n},
            metadata={
                "candidate_count": self.n,
                "successful_candidates": len(candidates),
                "candidate_errors": [error.code for error in errors],
                "candidate_lengths": [len(candidate.text) for candidate in candidates],
                # 研究用途：完整保留所有候选正文
                "candidate_texts": [candidate.text for candidate in candidates],
                "selected_base_method": self.base.name,
            },
        )


class TerraFallbackMethod:
    """主 decoder 失败后再换备用 decoder（默认 Luna → Terra）。

    原理：不同 decoder 对同一 envelope 的“可读性”不同；主模型空/拒答时
    切换备用模型，不改 source envelope。
    """

    def __init__(
        self,
        primary: RecoveryMethod,
        fallback: RecoveryMethod,
        fallback_model: str,
        name: str = "gpt.luna_then_terra",
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.fallback_model = fallback_model
        self.name = name

    def run(self, context: MethodContext) -> MethodResult:
        """先 primary，不可用再 fallback_model。"""
        primary_error: ProbeError | None = None
        try:
            primary_result = self.primary.run(context)
        except ProbeError as exc:
            primary_error = exc
            primary_result = MethodResult(
                method=self.primary.name,
                text="",
                attempts=1,
                metadata={"error": exc.code},
            )
        if _usable(primary_result.text):
            return MethodResult(
                method=self.name,
                text=primary_result.text,
                raw_outputs=primary_result.raw_outputs,
                attempts=primary_result.attempts,
                cost=primary_result.cost,
                metadata={
                    "selected_decoder": context.decoder_model,
                    "fallback_used": False,
                    "candidate_texts": [primary_result.text],
                },
            )
        fallback_context = replace(context, decoder_model=self.fallback_model)
        fallback_result = self.fallback.run(fallback_context)
        return MethodResult(
            method=self.name,
            text=fallback_result.text,
            raw_outputs=primary_result.raw_outputs + fallback_result.raw_outputs,
            attempts=primary_result.attempts + fallback_result.attempts,
            cost={"primary": primary_result.cost, "fallback": fallback_result.cost},
            metadata={
                "selected_decoder": self.fallback_model,
                "fallback_used": True,
                "primary_error": primary_error.code if primary_error else None,
                "primary_text": primary_result.text,
                "primary_text_length": len(primary_result.text),
                "candidate_texts": [primary_result.text, fallback_result.text],
            },
        )


class ReconciliationMethod:
    """多候选 noisy 转录 → 交给 reconciler 合成一份忠实文本。

    原理：单次 fuzzy 有漏词/幻觉；采 pool 个候选，按 token 误差筛 top-k，
    再让更强模型对照 envelope + 候选做合并。注意：候选进了 reconciler
    prompt，marker 命中不再是 source-only 证据 → provenance_safe=False。
    """

    def __init__(
        self,
        base: RecoveryMethod,
        reconciler_model: str,
        candidate_count: int = 3,
        candidate_pool: int | None = None,
        name: str = "reconciliation",
    ) -> None:
        if candidate_count < 1:
            raise ValueError("reconciliation 要求 candidate_count >= 1")
        pool = candidate_pool if candidate_pool is not None else candidate_count
        if pool < candidate_count:
            raise ValueError("candidate_pool 必须 >= candidate_count")
        self.base = base
        self.reconciler_model = reconciler_model
        self.candidate_count = candidate_count
        self.candidate_pool = pool
        self.name = name

    def run(self, context: MethodContext) -> MethodResult:
        """采候选 → 筛选 → reconciler replay。"""
        candidates: list[MethodResult] = []
        candidate_errors: list[ProbeError] = []
        for _ in range(self.candidate_pool):
            try:
                candidates.append(self.base.run(context))
            except ProbeError as exc:
                candidate_errors.append(exc)
        usable = [candidate for candidate in candidates if _usable(candidate.text)]
        selected = sorted(usable, key=lambda item: _reconciliation_score(item.text, context))[
            : self.candidate_count
        ]
        candidate_attempts = sum(candidate.attempts for candidate in candidates) + len(candidate_errors)
        candidate_text = "\n\n--- CANDIDATE ---\n\n".join(
            candidate.text for candidate in selected
        )
        if not candidate_text:
            return MethodResult(
                method=self.name,
                text="",
                raw_outputs=tuple(output for candidate in candidates for output in candidate.raw_outputs),
                attempts=candidate_attempts,
                metadata={
                    "reconciler_model": self.reconciler_model,
                    "candidate_pool_count": self.candidate_pool,
                    "candidate_count": 0,
                    "candidate_errors": [error.code for error in candidate_errors],
                    "candidate_texts": [candidate.text for candidate in candidates],
                    "provenance_safe": False,
                },
            )
        adapter: ProtocolAdapter = context.client
        reconcile_context = replace(
            context,
            decoder_model=self.reconciler_model,
            elicitation=(
                "Reconstruct one faithful transcription of the attached working. "
                "Correct omissions and preserve exact names, numbers, and identifiers. "
                "Output only the transcription.\n\n"
                + candidate_text
            ),
        )
        builder = getattr(adapter, "reconciliation_items", None)
        if callable(builder):
            items = builder(reconcile_context, [candidate.text for candidate in selected])
        else:
            items = adapter.single_items(reconcile_context, reconcile_context.elicitation)
        payload = adapter.replay(reconcile_context, items)
        extract_text = getattr(adapter, "visible_text", visible_text)
        text = strip_transport_wrappers(extract_text(payload))
        return MethodResult(
            method=self.name,
            text=text,
            raw_outputs=tuple(output for candidate in candidates for output in candidate.raw_outputs)
            + (payload,),
            attempts=candidate_attempts + 1,
            metadata={
                "reconciler_model": self.reconciler_model,
                "candidate_pool_count": self.candidate_pool,
                "candidate_count": len(selected),
                "candidate_errors": [error.code for error in candidate_errors],
                "candidate_lengths": [len(candidate.text) for candidate in selected],
                "candidate_texts": [candidate.text for candidate in selected],
                "all_candidate_texts": [candidate.text for candidate in candidates],
                "selection_errors": [
                    round(_reconciliation_score(candidate.text, context)[0], 4)
                    for candidate in selected
                ],
                "refusal": is_refusal(text),
                # 候选进入 reconciler prompt，marker 不再是 source-only 证据
                "provenance_safe": False,
            },
        )


def _usable(text: str) -> bool:
    """非空且非拒答。"""
    return bool(text.strip()) and not is_refusal(text)


def _candidate_score(text: str, context: MethodContext) -> tuple[int, int, int]:
    """best-of-N 排序键：marker 命中 > 长度接近 source tokens > 绝对长度。"""
    if not _usable(text):
        return (0, 0, 0)
    marker_hit = int(context.harvest.marker in text)
    target = context.harvest.source_reasoning_tokens or 0
    length_score = -abs(len(text.split()) - target) if target else len(text)
    return (marker_hit, length_score, len(text))


def _reconciliation_score(text: str, context: MethodContext) -> tuple[float, int]:
    """按论文的 token-count extraction-error 代理排序（越小越好）。"""
    target = context.harvest.source_reasoning_tokens
    if not target:
        return (0.0, -len(text))
    recovered = len(text.split())
    return (abs(recovered - target) / target, -recovered)
