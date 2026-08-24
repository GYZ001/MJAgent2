"""RBAC 第二阶段：请求身份（Principal）与角色 -> scope 映射。

``workspace_members.role`` 只有 4 个 ASCII 枚举值；系统管理员不是某个空间的
成员行，而是 ``users.is_system_admin=1``，隐式拥有全部 scope 且被视为所有
空间的成员。scope 常量与 ``app/mcp/auth.py`` 的 ``ALL_SCOPES`` 完全一致
（有意在此处保留一份独立定义而不是互相 import：两个模块的加载时机不同，
``app.mcp`` 包顶层会拉起整条 MCP 服务链路，不希望作为鉴权基础模块的隐式依赖）。
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field

ALL_SCOPES: frozenset[str] = frozenset(
    {
        "manju:read",
        "manju:project-write",
        "manju:generation-text",
        "manju:generation-media",
        "manju:delivery",
        "manju:admin",
    }
)

# workspace_members.role -> 该角色在其所属空间内拥有的 scope 集合。
ROLE_SCOPES: dict[str, frozenset[str]] = {
    "workspace_admin": frozenset(
        {
            "manju:read",
            "manju:project-write",
            "manju:generation-text",
            "manju:generation-media",
            "manju:delivery",
        }
    ),
    "production": frozenset(
        {
            "manju:read",
            "manju:project-write",
            "manju:generation-text",
            "manju:generation-media",
        }
    ),
    "review": frozenset({"manju:read", "manju:delivery"}),
    "readonly": frozenset({"manju:read"}),
}


@dataclass(frozen=True)
class Principal:
    """一次已鉴权请求背后的身份；由 ``app.auth.sessions.resolve_session`` 构建。"""

    user_id: str
    username: str
    is_system_admin: bool
    workspace_roles: dict[str, str] = field(default_factory=dict)

    def role_in(self, workspace_id: str) -> str | None:
        role = self.workspace_roles.get(workspace_id)
        if role is not None:
            return role
        # 系统管理员隐式是每个空间的管理员，即便没有 workspace_members 行。
        return "workspace_admin" if self.is_system_admin else None

    def can_access(self, workspace_id: str) -> bool:
        return self.is_system_admin or workspace_id in self.workspace_roles

    def scopes_for(self, workspace_id: str) -> frozenset[str]:
        if self.is_system_admin:
            return ALL_SCOPES
        role = self.workspace_roles.get(workspace_id)
        if role is None:
            return frozenset()
        return ROLE_SCOPES.get(role, frozenset())

    @property
    def all_scopes(self) -> frozenset[str]:
        if self.is_system_admin:
            return ALL_SCOPES
        union: set[str] = set()
        for role in self.workspace_roles.values():
            union |= ROLE_SCOPES.get(role, frozenset())
        return frozenset(union)


# 由 require_local_session 在校验通过后注入，供 Command Bus /
# 后续阶段的 scope 校验读取；镜像 app/local_session.py 的
# _request_session_id 写法（同一请求生命周期内可读，请求结束需显式清空）。
_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "current_principal", default=None
)


def set_current_principal(principal: Principal | None) -> None:
    _current_principal.set(principal)


def get_current_principal() -> Principal | None:
    return _current_principal.get()


def current_actor_name(fallback: str = "user") -> str:
    """审计字段里的「谁」——一律取已认证身份，不接受调用方自报。

    这些字段（``decided_by`` / ``archived_by`` / ``created_by`` / ``issued_by``）
    历史上是从请求体里读的自由文本，默认字面量 ``"user"``。在没有用户身份的
    单机时代那只是个占位符；有了真实登录之后它就是个**可伪造的署名**——任何人
    都能把自己的操作记成别人干的，审计链就失去意义了。

    ``fallback`` 用于没有 Principal 的场景（内部直接函数调用、后台任务、
    兼容期共享会话），保持既有行为不变。
    """
    principal = get_current_principal()
    return principal.username if principal is not None else fallback
