"""总线执行的审计包装——从 ``app.capabilities.bus`` 挪出来。

``app/capabilities/bus.py`` 是零行余量文件（``app/FILE_CONVENTIONS.toml`` 的
560 行棘轮基线）：装不下审计钩子就该拆成新文件，不是拿分号合并语句凑行数
（CLAUDE.md「装不下时先想怎么拆，不要先想加基线」）。``app.capabilities`` 前缀
已在 ``app/LAYERS.toml`` 声明 L5，本文件不需要单独声明层号。

``execute_as`` 延迟 import ``app.capabilities.bus.get_command_bus``：本模块的
``run_audited``/``run_audited_sync`` 被 ``bus.py`` 在模块级 import，若这里也在
模块级反向 import ``bus.py``，会在加载期形成 `bus -> bus_audit -> bus` 的循环——
延迟到函数体内执行时才 import，加载期两个模块互不依赖，循环自然打开。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.audit.recorder import record_bus_outcome, source_context
from app.capabilities.direct import in_handler
from app.capabilities.schemas import CommandResult


async def run_audited(
    registry: Any, name: str, raw_args: Any, awaitable: Awaitable[CommandResult],
) -> CommandResult:
    """``CommandBus.execute_async`` 的审计包装：异常路径也记一行
    outcome=error（error_code 为异常类名），再原样 re-raise；成功路径按
    CommandResult.status 映射 outcome。嵌套调用（``in_handler()`` 为真）由
    ``record_bus_outcome`` 内部据此丢弃，不重复记录。
    """
    try:
        result = await awaitable
    except BaseException as exc:
        record_bus_outcome(name, registry.commands.get(name), raw_args, None, exc, in_handler())
        raise
    record_bus_outcome(name, registry.commands.get(name), raw_args, result, None, in_handler())
    return result


def run_audited_sync(
    registry: Any, name: str, raw_args: Any, fn: Callable[[], CommandResult],
) -> CommandResult:
    """``CommandBus.execute``（同步路径）的审计包装，语义与 ``run_audited`` 一致。"""
    try:
        result = fn()
    except BaseException as exc:
        record_bus_outcome(name, registry.commands.get(name), raw_args, None, exc, in_handler())
        raise
    record_bus_outcome(name, registry.commands.get(name), raw_args, result, None, in_handler())
    return result


async def execute_as(
    source: str, name: str, args: dict[str, Any], *, session_id: str | None, actor_username: str | None = None,
) -> CommandResult:
    """agent/mcp 统一入口：临时打上来源标签再走总线，收尾自动还原来源标签。"""
    # 延迟 import 原因见模块 docstring：避免 bus <-> bus_audit 的加载期循环。
    from app.capabilities.bus import get_command_bus

    with source_context(source, actor_username=actor_username):
        return await get_command_bus().execute_async(name, args, session_id=session_id)
