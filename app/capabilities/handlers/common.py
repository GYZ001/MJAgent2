"""Handler 共用工具：把领域函数/既有 route 返回值统一包装为 ``CommandResult``。

约定：
- Handler 只调用现有 Python 函数（``app.api`` / ``app.planning`` / ``app.orchestration.*`` /
  ``app.delivery`` / ``app.worker`` / ``app.system_api``），禁止用 httpx 回调本机 REST；
- 领域函数抛出的 ``HTTPException``／``ValueError``／``KeyError`` 统一转成
  ``CommandResult(status=FAILED, error_code=...)``，不让异常穿透到 Command Bus；
- ``dry_run`` 已由 Command Bus 统一处理，handler 内不需要再检查。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import HTTPException

from app.errors import ArtifactNeedsRebuildError
from app.capabilities.schemas import CommandResult, CommandStatus, UiIntent

T = TypeVar("T")


def succeeded(
    summary: str,
    *,
    data: dict[str, Any] | None = None,
    run_id: str | None = None,
    resource_uris: list[str] | None = None,
    ui_intent: UiIntent | None = None,
) -> CommandResult:
    return CommandResult(
        status=CommandStatus.SUCCEEDED,
        summary=summary,
        data=data or {},
        run_id=run_id,
        resource_uris=resource_uris or [],
        ui_intent=ui_intent,
    )


def accepted(
    summary: str,
    *,
    data: dict[str, Any] | None = None,
    run_id: str | None = None,
    resource_uris: list[str] | None = None,
    ui_intent: UiIntent | None = None,
) -> CommandResult:
    return CommandResult(
        status=CommandStatus.ACCEPTED,
        summary=summary,
        data=data or {},
        run_id=run_id,
        resource_uris=resource_uris or [],
        ui_intent=ui_intent,
    )


def failed(summary: str, *, error_code: str = "domain_error", data: dict[str, Any] | None = None) -> CommandResult:
    return CommandResult(status=CommandStatus.FAILED, summary=summary, error_code=error_code, data=data or {})


def from_http_exception(exc: HTTPException) -> CommandResult:
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("code") or detail)
        return failed(message, error_code=f"http_{exc.status_code}", data=detail)
    return failed(str(detail), error_code=f"http_{exc.status_code}")


async def call_guarded(
    func: Callable[..., Awaitable[T] | T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T | CommandResult:
    """执行领域函数（同步或异步皆可）；把常见领域异常转成 ``CommandResult(FAILED)``。

    调用方按 ``isinstance(outcome, CommandResult)`` 判断是否已经是失败结果，
    否则把返回值（通常是原路由本来会返回的 dict）当作正常领域数据继续处理。
    """
    try:
        outcome = func(*args, **kwargs)
        if hasattr(outcome, "__await__"):
            outcome = await outcome  # type: ignore[assignment]
        return outcome
    except HTTPException as exc:
        return from_http_exception(exc)
    except ArtifactNeedsRebuildError as exc:
        return failed(
            str(exc),
            error_code="http_409",
            data=exc.http_detail(),
        )
    except ValueError as exc:
        return failed(str(exc), error_code="invalid_state")
    except KeyError as exc:
        message = str(exc) or "关联对象不存在"
        return failed(message, error_code="not_found")
