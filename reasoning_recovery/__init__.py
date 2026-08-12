"""可组合的 Reasoning 恢复研究 harness。

提供协议适配、恢复策略、四维验证与内存引擎。
面向研究：完整保留恢复正文与候选，不在结果层做脱敏截断。
不启动 Web 服务，不持久化凭证。
"""

from .engine import RecoveryEngine
from .errors import ProbeError
from .models import (
    AttemptRecord,
    DimensionResult,
    Envelope,
    HarvestRecord,
    MethodContext,
    MethodResult,
    RecoveryResult,
    Settings,
)

__all__ = [
    "AttemptRecord",
    "DimensionResult",
    "Envelope",
    "HarvestRecord",
    "MethodContext",
    "MethodResult",
    "ProbeError",
    "RecoveryEngine",
    "RecoveryResult",
    "Settings",
]
