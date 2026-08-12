"""方法目录：每个方法声明家族、角色依赖与 prefer / fallback 链。

配置只提供「有哪些模型」；本文件描述「方法需要什么」。
解析逻辑在 config.resolve_method_run。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class RoleNeed:
    """方法对某一逻辑角色的需求。"""

    # 模型必须具备的能力标签：source | decoder | reconciler
    capability: str
    # 优先尝试的逻辑模型名（config.models 的 key）；空则任意具备 capability 的同家族模型
    prefer: tuple[str, ...] = ()
    # 是否必需；False 时缺失可跳过（少见）
    required: bool = True


@dataclass(frozen=True)
class MethodSpec:
    """一个恢复方法的依赖规格。"""

    name: str
    family: str
    # 角色名 → 需求。标准角色：source, decoder；扩展：fallback_decoder, reconciler
    roles: dict[str, RoleNeed]
    # 本方法因缺模型无法解析时，改试的方法名（有序）
    on_unresolved: tuple[str, ...] = ()
    # 人类可读说明
    summary: str = ""
    # 构造策略实例的工厂（延迟绑定，避免循环 import）
    build: Callable[..., Any] | None = None
    # 额外元数据（如 candidate_pool）
    options: dict[str, Any] = field(default_factory=dict)


def _gpt_base_roles(
    decoder_prefer: tuple[str, ...] = ("luna", "terra"),
) -> dict[str, RoleNeed]:
    return {
        "source": RoleNeed("source", prefer=("sol",)),
        "decoder": RoleNeed("decoder", prefer=decoder_prefer),
    }


def default_catalog() -> dict[str, MethodSpec]:
    """内置方法目录。"""
    from .methods import (
        BestOfNMethod,
        ChunkContinuationMethod,
        ClaudeFuzzyExtractionMethod,
        ClaudeReconciliationMethod,
        GeminiFuzzyExtractionMethod,
        GeminiReconciliationMethod,
        ReconciliationMethod,
        RepeatedInjectionMethod,
        SingleReplayMethod,
        TerraFallbackMethod,
    )

    single = SingleReplayMethod()
    repeated = RepeatedInjectionMethod()
    chunked = ChunkContinuationMethod()

    return {
        "gpt.single_replay": MethodSpec(
            name="gpt.single_replay",
            family="gpt",
            roles=_gpt_base_roles(("luna", "terra")),
            on_unresolved=("gpt.repeated_injection", "gpt.chunk_continuation"),
            summary="单次 envelope replay",
            build=lambda **_: SingleReplayMethod(),
        ),
        "gpt.repeated_injection": MethodSpec(
            name="gpt.repeated_injection",
            family="gpt",
            roles=_gpt_base_roles(("luna", "terra")),
            on_unresolved=("gpt.single_replay", "gpt.chunk_continuation"),
            summary="双重注入 replay",
            build=lambda **_: RepeatedInjectionMethod(),
        ),
        "gpt.chunk_continuation": MethodSpec(
            name="gpt.chunk_continuation",
            family="gpt",
            roles=_gpt_base_roles(("luna", "terra")),
            on_unresolved=("gpt.single_replay",),
            summary="分块续写",
            build=lambda **_: ChunkContinuationMethod(),
        ),
        "gpt.single_best_of_3": MethodSpec(
            name="gpt.single_best_of_3",
            family="gpt",
            roles=_gpt_base_roles(("luna", "terra")),
            on_unresolved=("gpt.single_replay",),
            summary="single_replay × 3 选优",
            build=lambda **_: BestOfNMethod(SingleReplayMethod(), n=3, name="gpt.single_best_of_3"),
        ),
        "gpt.repeated_best_of_3": MethodSpec(
            name="gpt.repeated_best_of_3",
            family="gpt",
            roles=_gpt_base_roles(("luna", "terra")),
            on_unresolved=("gpt.repeated_injection",),
            summary="repeated_injection × 3 选优",
            build=lambda **_: BestOfNMethod(RepeatedInjectionMethod(), n=3, name="gpt.repeated_best_of_3"),
        ),
        "gpt.luna_then_terra": MethodSpec(
            name="gpt.luna_then_terra",
            family="gpt",
            roles={
                "source": RoleNeed("source", prefer=("sol",)),
                # 主 decoder 优先 luna；没有 luna 则整方法 unresolved → 走 on_unresolved
                "decoder": RoleNeed("decoder", prefer=("luna",)),
                "fallback_decoder": RoleNeed("decoder", prefer=("terra",)),
            },
            on_unresolved=("gpt.single_replay", "gpt.repeated_injection"),
            summary="主 decoder 失败后换 fallback_decoder",
            build=lambda resolved, **_: TerraFallbackMethod(
                SingleReplayMethod(),
                SingleReplayMethod(),
                fallback_model=resolved.role_ids["fallback_decoder"],
                name="gpt.luna_then_terra",
            ),
        ),
        "gpt.reconcile_with_terra": MethodSpec(
            name="gpt.reconcile_with_terra",
            family="gpt",
            roles={
                "source": RoleNeed("source", prefer=("sol",)),
                "decoder": RoleNeed("decoder", prefer=("luna", "terra")),
                "reconciler": RoleNeed("reconciler", prefer=("terra",)),
            },
            on_unresolved=("gpt.single_best_of_3", "gpt.single_replay"),
            summary="多候选 + reconciler 合并",
            build=lambda resolved, **_: ReconciliationMethod(
                SingleReplayMethod(),
                reconciler_model=resolved.role_ids["reconciler"],
                name="gpt.reconcile_with_terra",
            ),
        ),
        "claude.fuzzy_prefill": MethodSpec(
            name="claude.fuzzy_prefill",
            family="claude",
            roles={
                "source": RoleNeed("source", prefer=("opus", "fable", "sonnet")),
                "decoder": RoleNeed("decoder", prefer=("haiku",)),
            },
            on_unresolved=("claude.reconciliation",),
            summary="Claude thinking prefill",
            build=lambda **_: ClaudeFuzzyExtractionMethod(),
        ),
        "claude.reconciliation": MethodSpec(
            name="claude.reconciliation",
            family="claude",
            roles={
                "source": RoleNeed("source", prefer=("opus", "fable", "sonnet")),
                "decoder": RoleNeed("decoder", prefer=("haiku",)),
                "reconciler": RoleNeed("reconciler", prefer=("opus",)),
            },
            on_unresolved=("claude.fuzzy_prefill",),
            summary="Claude 多候选 reconciliation",
            build=lambda resolved, **kw: ClaudeReconciliationMethod(
                candidate_pool=int(kw.get("candidate_pool", 3)),
                selection_count=int(kw.get("selection_count", 3)),
                reconciler_model=resolved.role_ids["reconciler"],
            ),
        ),
        "gemini.fuzzy_prefill": MethodSpec(
            name="gemini.fuzzy_prefill",
            family="gemini",
            roles={
                "source": RoleNeed("source", prefer=("gemini_pro", "gemini_36")),
                "decoder": RoleNeed("decoder", prefer=("gemini_flash", "gemini_lite")),
            },
            on_unresolved=("gemini.reconciliation",),
            summary="Gemini thought prefill",
            build=lambda **_: GeminiFuzzyExtractionMethod(),
        ),
        "gemini.reconciliation": MethodSpec(
            name="gemini.reconciliation",
            family="gemini",
            roles={
                "source": RoleNeed("source", prefer=("gemini_pro", "gemini_36")),
                "decoder": RoleNeed("decoder", prefer=("gemini_flash", "gemini_lite")),
                "reconciler": RoleNeed("reconciler", prefer=("gemini_flash",)),
            },
            on_unresolved=("gemini.fuzzy_prefill",),
            summary="Gemini 多候选 reconciliation",
            build=lambda resolved, **kw: GeminiReconciliationMethod(
                candidate_pool=int(kw.get("candidate_pool", 3)),
                selection_count=int(kw.get("selection_count", 3)),
                reconciler_model=resolved.role_ids["reconciler"],
            ),
        ),
    }


# 各家族默认方法优先级（未指定 --method 时）
FAMILY_DEFAULT_METHODS: dict[str, tuple[str, ...]] = {
    "gpt": (
        "gpt.single_replay",
        "gpt.repeated_injection",
        "gpt.chunk_continuation",
        "gpt.single_best_of_3",
        "gpt.luna_then_terra",
        "gpt.reconcile_with_terra",
    ),
    "claude": ("claude.fuzzy_prefill", "claude.reconciliation"),
    "gemini": ("gemini.fuzzy_prefill", "gemini.reconciliation"),
}
