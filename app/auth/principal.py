"""请求身份（Principal）：账号即项目空间模型下的鉴权基础类型。

账号级隔离落地后角色模型收敛为两档：系统管理员（``is_system_admin=1``，隐式
跨账号可见，见模块内注释）与普通用户（只能触达自己拥有的项目，``projects.
owner_user_id`` 是唯一判据）。历史上这里有 ``workspace_admin``/``production``/
``review``/``readonly`` 四档团队角色（``ROLE_SCOPES``）——1 账号 1 独立空间之后，
一个账号对自己名下的项目天然拥有全部操作权，角色差异化不再有意义，随团队/
工作空间模型一并退场。

``ALL_SCOPES`` 保留：它不只是团队角色的产物，还是 MCP Bearer Token
（``app/mcp/auth.py::TokenClaims.scopes``）的授权契约——服务账号可以只领到
``{"manju:read"}`` 这样的子集，与 ``Principal`` 无关，不能一并删除。
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass

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


@dataclass(frozen=True)
class Principal:
    """一次已鉴权请求背后的身份；由 ``app.auth.sessions.resolve_session`` 构建。"""

    user_id: str
    username: str
    is_system_admin: bool

    def owns(self, owner_user_id: str | None) -> bool:
        """能否触达归属 ``owner_user_id`` 的项目：本人，或系统管理员（跨账号可见）。"""
        if self.is_system_admin:
            return True
        return bool(owner_user_id) and owner_user_id == self.user_id

    @property
    def all_scopes(self) -> frozenset[str]:
        """账号内没有角色差异化：能登录就对自己名下的项目拥有全部操作 scope。

        「碰得到哪个项目」由 ``owns()``/HTTP 边界的归属校验单独判定，与这里的
        「能做哪类操作」正交——本属性只回答后者，见 ``app/capabilities/bus.py::
        _authorize`` 的同一条分工说明。
        """
        return ALL_SCOPES


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
