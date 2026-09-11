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

EP-01：``org_id``/``team_ids``/``permission_keys``/``role_governed`` 四个新
字段与 ``can()`` 方法是组织/团队/自定义角色模型的接入点。``role_governed``
是关键：只有 ``app.auth.sessions.resolve_session`` 构造的真实 HTTP 会话
Principal 才可能把它设为 ``True``，且只在该账号已经被管理员拉进至少一个
团队或直接授予至少一个项目时才会这样做（``app.orgs.store.
user_is_governed``；同时算好 ``permission_keys``）——一个刚开户、还没被分配
任何团队/角色的账号必须继续拥有和今天一样的全部非 admin_only 命令权限，这不
是历史遗留的宽松，是这一阶段唯一在用的开户路径产出的正常状态（EP-02/EP-03
的批量导入/SSO 自动开户落地前不会变，见 ``app/orgs/store.py`` 模块文档）。
MCP Bearer Token（``app/mcp/server.py::_principal_from_claims``）、进程级
共享会话回退（``app/local_session.py::_legacy_shared_principal``）与大量
早于 EP-01 编写、直接手写 ``Principal(user_id=..., username=...,
is_system_admin=...)`` 的测试同样保持 ``role_governed=False``（默认值）——
`can()` 对它们恒放行，只受 ``admin_only`` 把关，语义与迁移前完全一致。反过来，
一旦 ``role_governed=True`` 而 ``permission_keys`` 是空集合（团队里挂着一个
还没配权限点的角色），`can()` 必须拒绝一切非系统管理员命令（CLAUDE.md
「空集合不等于放行」）。
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field

# 不在模块顶层 `from app.authz import policy`：app/authz/__init__.py 会连带
# import app.authz.resolve，而 resolve.py 反过来 `from app.auth.principal
# import get_current_principal`——三者构成真实循环导入（已用
# `python -c "import app.auth.sessions"` 实测触发 ImportError: cannot import
# name 'get_current_principal' from partially initialized module），不是可以
# 忽略的风格问题。`can()` 方法内部延迟 import 把它推迟到运行时（届时两个
# 模块都已初始化完毕），是这里唯一可行的写法。

ALL_SCOPES: frozenset[str] = frozenset(
    {
        "manju:read",
        "manju:project-write",
        "manju:generation-text",
        "manju:generation-media",  # 2026-09-10 起已拆分为下面两个子 scope，
        "manju:media-generate",    # 保留旧名字供既有 MCP token 继续持有
        "manju:media-decide",      # （EP-01 §7：旧的 = 新的并集，只增不改）
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
    org_id: str | None = None
    team_ids: frozenset[str] = field(default_factory=frozenset)
    permission_keys: frozenset[str] = field(default_factory=frozenset)
    # 见模块 docstring：默认 False = 未接入组织角色模型，can() 只受 admin_only
    # 把关（迁移前语义）；仅 resolve_session() 构造的真实会话身份设为 True。
    role_governed: bool = False

    def owns(self, owner_user_id: str | None) -> bool:
        """能否触达归属 ``owner_user_id`` 的项目：本人，或系统管理员（跨账号可见）。"""
        if self.is_system_admin:
            return True
        return bool(owner_user_id) and owner_user_id == self.user_id

    def can(self, command_name: str) -> bool:
        """Command Bus 命令级判定：EP-01 §8 的"这个动作你能不能做"。

        与 ``owns()``（HTTP 边界"这条数据是不是你的"）正交，见
        ``app/capabilities/bus.py::_authorize`` 的既有分工说明。
        """
        if self.is_system_admin:
            return True
        if not self.role_governed:
            return True
        from app.authz import policy  # 见模块顶部延迟 import 的说明

        return policy.command_allowed(self.permission_keys, command_name)

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
