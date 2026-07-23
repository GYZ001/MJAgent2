"""MCP JSON-RPC / HTTP 错误类型。协议层专用，不携带业务密钥或原始报文。"""
from __future__ import annotations

from typing import Any


class McpError(Exception):
    """映射为 JSON-RPC 2.0 error 字段（HTTP 200，jsonrpc 层面失败）。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


class ForbiddenError(Exception):
    """映射为 HTTP 403：scope 不足，绝不用 JSON-RPC 200 包装掉。"""

    def __init__(self, message: str, *, code: str = "insufficient_scope") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class NotFoundError(Exception):
    """通用资源/工具未找到。"""

    def __init__(self, message: str, *, code: str = "not_found") -> None:
        self.message = message
        self.code = code
        super().__init__(message)
