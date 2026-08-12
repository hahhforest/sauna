"""项目本地配置加载。

优先级（后者覆盖前者）：
1. config.example.yaml 同结构的默认空值
2. 项目 config.yaml（或 SAUNA_CONFIG 指定路径）
3. 环境变量 UPSTREAM_BASE_URL / UPSTREAM_API_KEY
4. CLI 显式参数

不读取 ~/.minimax 或其他全局 agent 配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import ProbeError
from .models import Settings

# 仓库根目录（本文件在 reasoning_recovery/ 下）
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
class AppConfig:
    """完整应用配置快照。"""

    upstream: UpstreamConfig
    defaults: dict[str, Any] = field(default_factory=dict)
    protocols: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None


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
    """加载 YAML 配置并叠加环境变量。

    Args:
        path: 显式配置路径；默认找 config.yaml / SAUNA_CONFIG。
        require: True 时缺少 base_url/api_key 直接报错。
    """
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
    headers = _as_str_dict(upstream_raw.get("headers"))

    upstream = UpstreamConfig(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        auth=auth,
        auth_header=auth_header,
        auth_prefix=auth_prefix,
        headers=headers,
    )
    app = AppConfig(
        upstream=upstream,
        defaults=dict(raw.get("defaults") or {}),
        protocols=dict(raw.get("protocols") or {}),
        models=dict(raw.get("models") or {}),
        path=config_path,
    )
    if require:
        missing = []
        if not app.upstream.base_url:
            missing.append("upstream.base_url / UPSTREAM_BASE_URL")
        if not app.upstream.api_key and app.upstream.auth != "none":
            missing.append("upstream.api_key / UPSTREAM_API_KEY")
        if missing:
            raise ProbeError(
                "CONFIG_MISSING",
                "缺少上游配置：" + "、".join(missing)
                + f"。请复制 {EXAMPLE_CONFIG_PATH.name} 为 config.yaml 并填写，"
                "或设置环境变量。",
                details={"config_path": str(config_path) if config_path else None},
            )
    return app


def build_settings(
    app: AppConfig,
    *,
    source_model: str | None = None,
    decoder_model: str | None = None,
    protocol: str | None = None,
    effort: str | None = None,
    max_output_tokens: int | None = None,
    timeout: float | None = None,
    model_config: dict[str, Any] | None = None,
    source_profile: str | None = None,
    decoder_profile: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Settings:
    """从 AppConfig + CLI 覆盖构造 Settings。"""
    defaults = app.defaults
    source_meta = _profile(app, source_profile)
    decoder_meta = _profile(app, decoder_profile)

    resolved_protocol = (
        protocol
        or _as_str(source_meta.get("protocol"))
        or _as_str(defaults.get("protocol"))
        or "responses"
    )
    protocol_block = dict(app.protocols.get(resolved_protocol) or {})

    # header 合并：upstream < protocol < source profile < decoder profile < CLI
    headers: dict[str, str] = {}
    headers.update(app.upstream.headers)
    headers.update(_as_str_dict(protocol_block.get("headers")))
    headers.update(_as_str_dict(source_meta.get("headers")))
    headers.update(_as_str_dict(decoder_meta.get("headers")))
    if extra_headers:
        headers.update(extra_headers)

    # auth 可被 protocol 覆盖
    auth = (_as_str(protocol_block.get("auth")) or app.upstream.auth or "bearer").lower()
    auth_header = _as_str(protocol_block.get("auth_header")) or app.upstream.auth_header
    auth_prefix = protocol_block.get("auth_prefix", app.upstream.auth_prefix)
    if auth_prefix is not None and not isinstance(auth_prefix, str):
        auth_prefix = str(auth_prefix)

    merged_model_config: dict[str, Any] = {}
    merged_model_config.update(dict(protocol_block.get("model_config") or {}))
    merged_model_config.update(dict(source_meta.get("model_config") or {}))
    merged_model_config.update(dict(decoder_meta.get("model_config") or {}))
    if model_config:
        merged_model_config.update(model_config)

    source = (
        source_model
        or _as_str(source_meta.get("id"))
        or _as_str(defaults.get("source_model"))
        or "gpt-5.6-sol"
    )
    decoder = (
        decoder_model
        or _as_str(decoder_meta.get("id"))
        or _as_str(defaults.get("decoder_model"))
        or "gpt-5.6-luna"
    )
    resolved_effort = (
        effort
        or _as_str(source_meta.get("effort"))
        or _as_str(defaults.get("effort"))
        or "high"
    )
    resolved_max = (
        max_output_tokens
        if max_output_tokens is not None
        else _as_int(defaults.get("max_output_tokens"), 4096)
    )
    resolved_timeout = (
        timeout if timeout is not None else _as_float(defaults.get("timeout"), 120.0)
    )

    if not app.upstream.base_url:
        raise ProbeError(
            "CONFIG_MISSING_BASE_URL",
            "缺少 base_url：请在 config.yaml 的 upstream.base_url 填写，或设 UPSTREAM_BASE_URL",
        )
    if not app.upstream.api_key and auth != "none":
        raise ProbeError(
            "CONFIG_MISSING_API_KEY",
            "缺少 api_key：请在 config.yaml 的 upstream.api_key 填写，或设 UPSTREAM_API_KEY",
        )

    return Settings(
        base_url=app.upstream.base_url,
        api_key=app.upstream.api_key,
        source_model=source,
        decoder_model=decoder,
        protocol=resolved_protocol,
        effort=resolved_effort,
        max_output_tokens=resolved_max,
        timeout=resolved_timeout,
        model_config=merged_model_config,
        extra_headers=headers,
        auth=auth,
        auth_header=auth_header,
        auth_prefix=auth_prefix,
    )


def client_from_settings(settings: Settings):
    """按 Settings 构造 HTTP 客户端（延迟 import 避免环依赖）。"""
    from .protocol import UrllibJsonClient

    return UrllibJsonClient(
        settings.base_url,
        settings.api_key,
        headers=settings.extra_headers,
        auth=settings.auth,
        auth_header=settings.auth_header,
        auth_prefix=settings.auth_prefix,
    )


def with_decoder(settings: Settings, decoder_model: str) -> Settings:
    """仅替换 decoder_model。"""
    return replace(settings, decoder_model=decoder_model)


def _profile(app: AppConfig, name: str | None) -> dict[str, Any]:
    if not name:
        return {}
    block = app.models.get(name)
    if not isinstance(block, dict):
        raise ProbeError("CONFIG_UNKNOWN_PROFILE", f"未知模型档案: {name}")
    return dict(block)


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
    out: dict[str, str] = {}
    for key, item in value.items():
        if item is None:
            continue
        out[str(key)] = str(item)
    return out
