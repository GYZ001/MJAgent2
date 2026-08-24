"""RBAC 身份认证支撑模块（第一阶段：仅口令哈希，尚未接入任何鉴权流程）。"""
from __future__ import annotations

from app.auth.passwords import hash_password, verify_password

__all__ = [
    "hash_password",
    "verify_password",
]
