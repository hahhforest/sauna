"""项目本地配置：模型骨架 + 方法依赖解析。

配置只声明「有哪些模型」；方法在 catalog 里声明角色依赖。
解析规则：
  - 方法需要 family 下具备某 capability 的模型
  - prefer 链按序找已配置逻辑名；找不到则在同 family 里找任意具备该 capability 的模型
  - 必需角色全部失败 → METHOD_UNRESOLVED，再走 on_unresolved 方法链
  - 没配任何 gpt 模型 → 不能跑 gpt.*（FAMILY_NOT_CONFIGURED）

不读取 ~/.minimax。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .catalog import (
    FAMILY_DEFAULT_METHODS,
    MethodSpec,
    RoleNeed,
    default_catalog,
)
from .errors import ProbeError
from .models import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = _REPO_ROOT / "config.example.yaml"


@dataclass(frozen=True)
class UpstreamConfig:
    """上游 HTTP 连接配置。"""

    base_url: str
    api_key: str
    auth: str = "bearer"
    auth_header: str | None = None
    auth_prefix: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelEntry:
    """一个已配置的逻辑模型。"""

    name: str
    family: str
    model_id: str
    protocol: str
    roles: frozenset[str]
    effort: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    model_config: dict[str, Any] = field(default_factory=dict)
    auth: str | None = None
    auth_header: str | None = None
    auth_prefix: str | None = None


@dataclass(frozen=True)
class AppConfig:
    """完整应用配置快照。"""

    upstream: UpstreamConfig
    runtime: dict[str, Any] = field(default_factory=dict)
    protocols: dict[str, Any] = field(default_factory=dict)
    models: dict[str, ModelEntry] = field(default_factory=dict)
    method_overrides: dict[str, Any] = field(default_factory=dict)
    # 按目标模型（逻辑名）定制：方法链 + decoder 偏好。
    # 由矩阵实验生成推荐默认，用户可改。
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None

    def families(self) -> set[str]:
        """已配置模型覆盖的家族集合。"""
        return {m.family for m in self.models.values()}

    def models_for(self, family: str, capability: str | None = None) -> list[ModelEntry]:
        """列出某家族（可选某角色能力）的模型。"""
        out = [m for m in self.models.values() if m.family == family]
        if capability:
            out = [m for m in out if capability in m.roles]
        return out


@dataclass(frozen=True)
class ResolvedMethodRun:
    """一次可执行的方法解析结果。"""

    method: str
    family: str
    # 角色名 → 逻辑模型名
    role_names: dict[str, str]
    # 角色名 → 上游 model id
    role_ids: dict[str, str]
    settings: Settings
    strategy: Any
    # 解析轨迹（调试 / 落盘）
    resolution_log: tuple[str, ...] = ()


def resolve_config_path(explicit: str | Path | None = None) -> Path | None:
    """解析配置文件路径；不存在则返回 None。"""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    env = os.getenv("SAUNA_CONFIG")
    if env:
        path = Path(env).expanduser()
        return path if path.is_file() else None
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def load_app_config(path: str | Path | None = None, *, require: bool = False) -> AppConfig:
    """加载 YAML：upstream + models 骨架 + runtime。"""
    config_path = resolve_config_path(path)
    raw: dict[str, Any] = {}
    if config_path is not None:
        raw = _read_yaml(config_path)

    upstream_raw = dict(raw.get("upstream") or {})
    base_url = _as_str(upstream_raw.get("base_url")) or os.getenv("UPSTREAM_BASE_URL") or ""
    api_key = _as_str(upstream_raw.get("api_key")) or os.getenv("UPSTREAM_API_KEY") or ""
    auth = (_as_str(upstream_raw.get("auth")) or "bearer").lower()
    auth_header = _as_str(upstream_raw.get("auth_header"))
    auth_prefix = upstream_raw.get("auth_prefix")
    if auth_prefix is not None and not isinstance(auth_prefix, str):
        auth_prefix = str(auth_prefix)

    # 兼容旧 defaults 段
    runtime = dict(raw.get("runtime") or raw.get("defaults") or {})

    models = _parse_models(raw.get("models") or {})
    targets_raw = raw.get("targets") or {}
    targets = (
        {str(k): v for k, v in targets_raw.items() if isinstance(v, dict)}
        if isinstance(targets_raw, dict)
        else {}
    )
    app = AppConfig(
        upstream=UpstreamConfig(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            auth=auth,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            headers=_as_str_dict(upstream_raw.get("headers")),
        ),
        runtime=runtime,
        protocols=dict(raw.get("protocols") or {}),
        models=models,
        method_overrides=dict(raw.get("methods") or {}),
        targets=targets,
        path=config_path,
    )
    if require:
        missing = []
        if not app.upstream.base_url:
            missing.append("upstream.base_url / UPSTREAM_BASE_URL")
        if not app.upstream.api_key and app.upstream.auth != "none":
            missing.append("upstream.api_key / UPSTREAM_API_KEY")
        if not app.models:
            missing.append("models（至少一个逻辑模型）")
        if missing:
            raise ProbeError(
                "CONFIG_MISSING",
                "缺少配置：" + "、".join(missing)
                + f"。请复制 {EXAMPLE_CONFIG_PATH.name} 为 config.yaml 并填写模型骨架。",
                details={"config_path": str(config_path) if config_path else None},
            )
    return app


def resolve_method_run(
    app: AppConfig,
    method: str | None = None,
    *,
    family: str | None = None,
    target: str | None = None,
    decoder: str | None = None,
    fallback_methods: tuple[str, ...] | list[str] = (),
    effort: str | None = None,
    max_output_tokens: int | None = None,
    timeout: float | None = None,
    extra_headers: dict[str, str] | None = None,
    model_config: dict[str, Any] | None = None,
    candidate_pool: int | None = None,
    selection_count: int | None = None,
    catalog: dict[str, MethodSpec] | None = None,
) -> ResolvedMethodRun:
    """按方法依赖解析模型角色，失败则沿 fallback / on_unresolved 链继续。

    Args:
        target: 目标模型逻辑名。给定后 source 角色固定为该模型，
            未显式给 method 时优先使用 targets.<name>.methods 方法链。
        decoder: 显式覆盖 decoder 逻辑名（矩阵实验用）。
    """
    cat = catalog or default_catalog()
    log: list[str] = []

    chain = _build_method_chain(app, method, family, fallback_methods, cat, log, target)
    errors: list[dict[str, Any]] = []

    for name in chain:
        spec = cat.get(name)
        if spec is None:
            log.append(f"{name}: unknown method")
            errors.append({"method": name, "code": "METHOD_UNKNOWN"})
            continue
        if not app.models_for(spec.family):
            log.append(f"{name}: family {spec.family} not configured")
            errors.append(
                {
                    "method": name,
                    "code": "FAMILY_NOT_CONFIGURED",
                    "family": spec.family,
                    "hint": f"在 config.yaml 的 models 下配置 family={spec.family} 的模型",
                }
            )
            continue

        try:
            role_names, role_ids, role_entries = _resolve_roles(app, spec, log, target, decoder)
        except ProbeError as exc:
            log.append(f"{name}: unresolved — {exc.message}")
            errors.append({"method": name, "code": exc.code, "details": exc.details})
            # 方法自身 on_unresolved 插入后续链（去重）
            for nxt in spec.on_unresolved:
                if nxt not in chain:
                    chain.append(nxt)
            continue

        settings = _settings_for_roles(
            app,
            family=spec.family,
            source=role_entries["source"],
            decoder=role_entries["decoder"],
            effort=effort,
            max_output_tokens=max_output_tokens,
            timeout=timeout,
            extra_headers=extra_headers,
            model_config=model_config,
        )
        resolved = ResolvedMethodRun(
            method=name,
            family=spec.family,
            role_names=role_names,
            role_ids=role_ids,
            settings=settings,
            strategy=None,
            resolution_log=tuple(log + [f"{name}: ok source={role_names['source']} decoder={role_names['decoder']}"]),
        )
        options = {
            "candidate_pool": (
                candidate_pool
                if candidate_pool is not None
                else int(spec.options.get("candidate_pool", 3))
            ),
            "selection_count": (
                selection_count
                if selection_count is not None
                else int(spec.options.get("selection_count", 3))
            ),
        }
        if spec.build is None:
            raise ProbeError("METHOD_NO_BUILDER", f"方法无 builder: {name}")
        try:
            strategy = spec.build(resolved=resolved, **options)
        except TypeError:
            strategy = spec.build(**options)
        return replace(resolved, strategy=strategy)

    raise ProbeError(
        "METHOD_UNRESOLVED",
        "没有可执行的方法：所需模型未配置，或 prefer 链全部落空。"
        "请在 config.yaml 的 models 中补齐对应 family/roles，或换方法。",
        details={"tried": errors, "log": log, "configured_models": sorted(app.models.keys())},
    )


def list_runnable_methods(app: AppConfig, catalog: dict[str, MethodSpec] | None = None) -> list[str]:
    """返回当前配置下能完整解析的方法名。"""
    cat = catalog or default_catalog()
    runnable: list[str] = []
    for name, spec in cat.items():
        if not app.models_for(spec.family):
            continue
        try:
            _resolve_roles(app, spec, [])
        except ProbeError:
            continue
        runnable.append(name)
    return runnable


def client_from_settings(settings: Settings):
    """按 Settings 构造 HTTP 客户端。"""
    from .protocol import UrllibJsonClient

    return UrllibJsonClient(
        settings.base_url,
        settings.api_key,
        headers=settings.extra_headers,
        auth=settings.auth,
        auth_header=settings.auth_header,
        auth_prefix=settings.auth_prefix,
    )


# ---- 内部：模型骨架解析 ----


def _parse_models(raw: dict[str, Any]) -> dict[str, ModelEntry]:
    """解析 models 段为 ModelEntry。"""
    out: dict[str, ModelEntry] = {}
    for name, block in raw.items():
        if not isinstance(block, dict):
            continue
        # 跳过纯注释占位
        model_id = _as_str(block.get("id") or block.get("model"))
        family = _as_str(block.get("family"))
        if not model_id or not family:
            continue
        roles_raw = block.get("roles") or []
        if isinstance(roles_raw, str):
            roles = frozenset({roles_raw})
        else:
            roles = frozenset(str(r) for r in roles_raw)
        protocol = _as_str(block.get("protocol")) or _default_protocol(family)
        out[str(name)] = ModelEntry(
            name=str(name),
            family=family.lower(),
            model_id=model_id,
            protocol=protocol,
            roles=roles,
            effort=_as_str(block.get("effort")),
            headers=_as_str_dict(block.get("headers")),
            model_config=dict(block.get("model_config") or {}),
            auth=_as_str(block.get("auth")),
            auth_header=_as_str(block.get("auth_header")),
            auth_prefix=block.get("auth_prefix") if isinstance(block.get("auth_prefix"), str) else None,
        )
    return out


def _default_protocol(family: str) -> str:
    return {
        "gpt": "responses",
        "claude": "anthropic_messages",
        "gemini": "gemini",
    }.get(family.lower(), "responses")


def _build_method_chain(
    app: AppConfig,
    method: str | None,
    family: str | None,
    fallback_methods: tuple[str, ...] | list[str],
    catalog: dict[str, MethodSpec],
    log: list[str],
    target: str | None = None,
) -> list[str]:
    """构造待尝试的方法有序列表。

    优先级：显式 method > targets.<target>.methods > 家族默认链。
    """
    chain: list[str] = []
    if method:
        chain.append(method)
    else:
        target_block = app.targets.get(target) if target else None
        target_methods = (
            [str(m) for m in target_block["methods"]] if target_block and target_block.get("methods") else []
        )
        if target_methods:
            chain.extend(target_methods)
            log.append(f"target={target} methods={target_methods}")
        else:
            fam = (family or _as_str(app.runtime.get("default_family")) or "").lower()
            if not fam:
                # 有哪个家族就用哪个
                configured = app.families()
                for candidate in ("gpt", "claude", "gemini"):
                    if candidate in configured:
                        fam = candidate
                        break
            if not fam:
                raise ProbeError(
                    "FAMILY_NOT_CONFIGURED",
                    "未配置任何模型家族。请在 config.yaml 的 models 下添加至少一条模型。",
                )
            defaults = FAMILY_DEFAULT_METHODS.get(fam, ())
            chain.extend(defaults)
            log.append(f"default family={fam} methods={list(defaults)}")
    for item in fallback_methods:
        if item and item not in chain:
            chain.append(item)
    # 过滤未知稍后处理
    return chain


def _resolve_roles(
    app: AppConfig,
    spec: MethodSpec,
    log: list[str],
    target: str | None = None,
    decoder: str | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, ModelEntry]]:
    """为方法解析全部必需角色。target/decoder 给定则对应角色固定。"""
    roles_spec = _apply_method_overrides(app, spec, target)
    names: dict[str, str] = {}
    ids: dict[str, str] = {}
    entries: dict[str, ModelEntry] = {}
    for role_name, need in roles_spec.items():
        entry: ModelEntry | None = None
        if target and role_name == "source":
            entry = app.models.get(target)
            if entry is not None and (
                entry.family != spec.family or need.capability not in entry.roles
            ):
                entry = None
            log.append(f"  {role_name}: pinned target {target}")
        elif decoder and role_name == "decoder":
            entry = app.models.get(decoder)
            if entry is not None and (
                entry.family != spec.family or need.capability not in entry.roles
            ):
                entry = None
            log.append(f"  {role_name}: pinned decoder {decoder}")
        else:
            entry = _pick_model(app, spec.family, need, log, role_name)
        if entry is None:
            if need.required:
                raise ProbeError(
                    "ROLE_UNRESOLVED",
                    f"方法 {spec.name} 无法解析角色 {role_name}（需要 capability={need.capability}，"
                    f"prefer={list(need.prefer) or '任意'}）。"
                    f"请在 models 中配置 family={spec.family} 且 roles 含 {need.capability} 的模型。",
                    details={
                        "method": spec.name,
                        "role": role_name,
                        "capability": need.capability,
                        "prefer": list(need.prefer),
                        "available": [
                            m.name for m in app.models_for(spec.family, need.capability)
                        ],
                    },
                )
            continue
        names[role_name] = entry.name
        ids[role_name] = entry.model_id
        entries[role_name] = entry
    if "source" not in entries or "decoder" not in entries:
        raise ProbeError(
            "ROLE_UNRESOLVED",
            f"方法 {spec.name} 缺少 source 或 decoder",
            details={"resolved": names},
        )
    return names, ids, entries


def _apply_method_overrides(
    app: AppConfig, spec: MethodSpec, target: str | None = None
) -> dict[str, RoleNeed]:
    """合并 config.methods.<name>.prefer 与 targets.<target>.decoder 覆盖。"""
    override = app.method_overrides.get(spec.name) or {}
    prefer_map = override.get("prefer") or {}
    roles = dict(spec.roles)
    if isinstance(prefer_map, dict):
        for role_name, prefer_list in prefer_map.items():
            if role_name not in roles:
                continue
            if isinstance(prefer_list, str):
                prefer_t = (prefer_list,)
            else:
                prefer_t = tuple(str(x) for x in prefer_list)
            old = roles[role_name]
            roles[role_name] = RoleNeed(old.capability, prefer=prefer_t, required=old.required)
    # targets.<name>.decoder：该目标模型下的 decoder 偏好链（矩阵实验推荐值）
    if target and "decoder" in roles:
        target_block = app.targets.get(target) or {}
        decoder_prefer = target_block.get("decoder")
        if isinstance(decoder_prefer, str):
            decoder_prefer = [decoder_prefer]
        if isinstance(decoder_prefer, (list, tuple)) and decoder_prefer:
            old = roles["decoder"]
            roles["decoder"] = RoleNeed(
                old.capability, prefer=tuple(str(x) for x in decoder_prefer), required=old.required
            )
    return roles


def _pick_model(
    app: AppConfig,
    family: str,
    need: RoleNeed,
    log: list[str],
    role_name: str,
) -> ModelEntry | None:
    """按 prefer 链挑选模型。

    - prefer 非空：只在 prefer 列表里找；全部未配置 → None（方法 unresolved，走 fallback）
    - prefer 为空：任意具备 capability 的同家族模型
    """
    if need.prefer:
        for name in need.prefer:
            entry = app.models.get(name)
            if entry is None:
                log.append(f"  {role_name}: prefer {name} — not configured")
                continue
            if entry.family != family:
                log.append(f"  {role_name}: prefer {name} — family mismatch")
                continue
            if need.capability not in entry.roles:
                log.append(f"  {role_name}: prefer {name} — missing role {need.capability}")
                continue
            log.append(f"  {role_name}: picked {name} ({entry.model_id}) via prefer")
            return entry
        # prefer 写了但全落空：不静默换别的模型，让上层报 ROLE_UNRESOLVED
        log.append(f"  {role_name}: all prefer failed {list(need.prefer)}")
        return None

    candidates = app.models_for(family, need.capability)
    if candidates:
        entry = candidates[0]
        log.append(f"  {role_name}: picked {entry.name} ({entry.model_id}) via any-{need.capability}")
        return entry
    return None


def _settings_for_roles(
    app: AppConfig,
    *,
    family: str,
    source: ModelEntry,
    decoder: ModelEntry,
    effort: str | None,
    max_output_tokens: int | None,
    timeout: float | None,
    extra_headers: dict[str, str] | None,
    model_config: dict[str, Any] | None,
) -> Settings:
    """由 source/decoder 模型条目合成 Settings。"""
    protocol = source.protocol
    protocol_block = dict(app.protocols.get(protocol) or {})

    headers: dict[str, str] = {}
    headers.update(app.upstream.headers)
    headers.update(_as_str_dict(protocol_block.get("headers")))
    headers.update(source.headers)
    headers.update(decoder.headers)
    if extra_headers:
        headers.update(extra_headers)

    auth = (
        decoder.auth
        or source.auth
        or _as_str(protocol_block.get("auth"))
        or app.upstream.auth
        or "bearer"
    ).lower()
    auth_header = (
        decoder.auth_header
        or source.auth_header
        or _as_str(protocol_block.get("auth_header"))
        or app.upstream.auth_header
    )
    auth_prefix = (
        decoder.auth_prefix
        if decoder.auth_prefix is not None
        else source.auth_prefix
        if source.auth_prefix is not None
        else protocol_block.get("auth_prefix", app.upstream.auth_prefix)
    )
    if auth_prefix is not None and not isinstance(auth_prefix, str):
        auth_prefix = str(auth_prefix)

    merged_mc: dict[str, Any] = {}
    merged_mc.update(dict(protocol_block.get("model_config") or {}))
    merged_mc.update(source.model_config)
    merged_mc.update(decoder.model_config)
    if model_config:
        merged_mc.update(model_config)

    resolved_effort = (
        effort
        or source.effort
        or _as_str(app.runtime.get("effort"))
        or "high"
    )
    resolved_max = (
        max_output_tokens
        if max_output_tokens is not None
        else _as_int(app.runtime.get("max_output_tokens"), 4096)
    )
    resolved_timeout = (
        timeout if timeout is not None else _as_float(app.runtime.get("timeout"), 120.0)
    )

    if not app.upstream.base_url:
        raise ProbeError("CONFIG_MISSING_BASE_URL", "缺少 upstream.base_url")
    if not app.upstream.api_key and auth != "none":
        raise ProbeError("CONFIG_MISSING_API_KEY", "缺少 upstream.api_key")

    return Settings(
        base_url=app.upstream.base_url,
        api_key=app.upstream.api_key,
        source_model=source.model_id,
        decoder_model=decoder.model_id,
        protocol=protocol,
        effort=resolved_effort,
        max_output_tokens=resolved_max,
        timeout=resolved_timeout,
        model_config=merged_mc,
        extra_headers=headers,
        auth=auth,
        auth_header=auth_header,
        auth_prefix=auth_prefix,
    )


# ---- YAML 工具 ----


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ProbeError(
            "CONFIG_YAML_UNAVAILABLE",
            "读取 YAML 需要 PyYAML：pip install pyyaml",
        ) from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProbeError("CONFIG_READ_ERROR", f"无法读取配置: {path}", details={"error": str(exc)}) from exc
    except yaml.YAMLError as exc:
        raise ProbeError("CONFIG_INVALID_YAML", f"YAML 解析失败: {path}", details={"error": str(exc)}) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProbeError("CONFIG_INVALID_SHAPE", "配置文件根节点必须是 mapping")
    return data


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


# 兼容旧 import 名（逐步淘汰）
def build_settings(*args: Any, **kwargs: Any) -> Settings:
    """已废弃：请用 resolve_method_run。保留仅为过渡。"""
    raise ProbeError(
        "CONFIG_API_CHANGED",
        "build_settings 已移除。请使用 resolve_method_run(app, method=...) 按模型骨架解析。",
    )
