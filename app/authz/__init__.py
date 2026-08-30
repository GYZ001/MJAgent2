"""RBAC 第四阶段：HTTP 边界的账号级项目隔离（路径参数 -> 归属项目 -> owner_user_id）。"""
from __future__ import annotations

from app.authz.resolve import require_project_owner_access, resolve_request_scope

__all__ = [
    "require_project_owner_access",
    "resolve_request_scope",
]
