"""研究 harness 的稳定错误类型。"""

from __future__ import annotations

from typing import Any


class ProbeError(Exception):
    """带机器可读 code 与完整 details 的预期失败。"""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        """初始化错误。

        Args:
            code: 稳定错误码，如 SOURCE_NO_REASONING_ENVELOPE。
            message: 人类可读说明。
            details: 任意结构化细节（研究用途完整保留）。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        """序列化为字典，便于 JSON 落盘。"""
        return {"code": self.code, "message": self.message, "details": self.details}
