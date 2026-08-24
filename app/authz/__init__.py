"""RBAC 第四阶段：HTTP 边界的工作空间隔离（路径参数 -> 归属项目 -> workspace）。"""
from __future__ import annotations

from app.authz.resolve import require_workspace_access, resolve_request_scope

__all__ = [
    "require_workspace_access",
    "resolve_request_scope",
]
