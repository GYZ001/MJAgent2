"""RBAC：路由级权限依赖。

给**不经 Command Bus** 的高危端点用。绝大多数写操作都走 ``ui_route`` →
CommandBus，scope 与 ``admin_only`` 在总线里统一判定（见
``app/capabilities/bus.py::_authorize``）；但少数运维端点被刻意排除在能力目录
之外——恰恰因为它们是「签发凭据/运维干预」，不能让 Agent 或 MCP 客户端自助
调用（见 ``app/system_api.py`` 里 MCP token 路由上方的注释）。这些端点历史上
只有「能连到端口的浏览器会话」这一层保护，现在补上真正的角色校验。

用法要点：以 ``_admin: None = Depends(require_system_admin)`` 的形式挂在参数
末尾并给默认值——测试里有大量 ``system_api.retry_job("j-1")`` 这样的**直接函数
调用**，带默认值才不会因为多一个参数而全部报错；经 HTTP 进来时 FastAPI 会正常
解析依赖。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.auth.principal import Principal, get_current_principal


def require_system_admin() -> Principal:
    """要求当前请求身份是系统管理员，否则 401/403。"""
    principal = get_current_principal()
    if principal is None:
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    if not principal.is_system_admin:
        raise HTTPException(403, "该操作仅限系统管理员")
    return principal
