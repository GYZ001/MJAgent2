"""Capability Registry 命令声明——按领域拆分的兄弟模块集合。

``app/capabilities/catalog.py`` 原来的 ``_register_commands`` 是一条 1028
代码行的单函数：一长串 ``_cmd(...)`` 声明式注册调用，不是可按阶段切的业务逻辑。
按领域拆成本包下的兄弟模块（project / account / bible / scene / episode /
screenplay / storyboard / video / delivery / run / system），与
``app/capabilities/handlers/`` 已经按领域分文件的结构对齐——每个模块只导出自己
那批 ``CommandSpec``（``commands()`` 函数），``catalog.py`` 的
``_register_commands`` 只做汇总注册。

``build_command`` 是所有领域模块共用的 ``CommandSpec`` 构造 helper（原
``catalog.py`` 的私有 ``_cmd``）。放在包的 ``__init__`` 而不是 ``catalog.py``
本身，是为了避免循环导入：``catalog.py`` 要在模块顶层导入本包的各领域子模块，
若 helper 留在 ``catalog.py``，各子模块再从 ``catalog`` 导入它就会在 ``catalog``
模块尚未执行完初始化时触发循环。
"""
from __future__ import annotations

from app.capabilities.registry import CommandSpec, Handler
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def build_command(
    name: str,
    *,
    title: str,
    description: str,
    input_model: type,
    risk: RiskLevel,
    confirmation: ConfirmationPolicy,
    idempotency: IdempotencyPolicy,
    scopes: set[str],
    side_effect: str,
    handler: Handler | None = None,
    rest_routes: tuple[str, ...] = (),
    supports_dry_run: bool = True,
    supports_cancel: bool = False,
    mcp_exposed: bool = True,
    admin_only: bool = False,
    tags: tuple[str, ...] = (),
) -> CommandSpec:
    return CommandSpec(
        name=name,
        version="1.0.0",
        title=title,
        description=description,
        input_model=input_model,
        risk=risk,
        confirmation=confirmation,
        idempotency=idempotency,
        scopes=frozenset(scopes),
        side_effect=side_effect,
        supports_dry_run=supports_dry_run,
        supports_cancel=supports_cancel,
        mcp_exposed=mcp_exposed,
        admin_only=admin_only,
        handler=handler,
        rest_routes=rest_routes,
        tags=tags,
    )
