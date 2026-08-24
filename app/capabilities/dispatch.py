"""统一执行入口（PRD M2+M3）。

所有 initiator（ui / agent / mcp）共用同一套 Policy：需要确认时返回
``WAITING_APPROVAL``，由调用方展示 Impact 后再带 ``approval_token`` 重试。
页面不可再「同请求内自动签发并消费」批准令牌——否则直接调 REST 与点击无法区分，
会绕过「展示 Impact → 用户批准」（PRD §3.2 / §8）。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.capabilities import ensure_catalog_loaded
from app.capabilities.bus import get_command_bus
from app.capabilities.schemas import CommandResult, CommandStatus

_ERROR_CODE_HTTP_STATUS: dict[str, int] = {
    "not_found": 404,
    "invalid_state": 409,
    "invalid_input": 422,
    "attachment_invalid": 400,
    "policy_denied": 403,
    "approval_invalid": 403,
    # RBAC：授权失败必须是 403，不能落到 REJECTED 的默认 409。前端要靠状态码
    # 区分「你没有权限」和「当前状态不允许」——两者的处置方式完全不同：前者该
    # 提示联系管理员，后者该提示刷新或先完成上游步骤。
    "forbidden_scope": 403,
    "forbidden_admin_only": 403,
    "handler_not_implemented": 501,
    "version_conflict": 409,
}

# 授权拒绝码：REJECTED 状态下需要覆盖默认 409 的那一小撮。
_AUTHZ_REJECTION_CODES = frozenset({"forbidden_scope", "forbidden_admin_only"})

_STATUS_HTTP: dict[CommandStatus, int] = {
    CommandStatus.REJECTED: 409,
    CommandStatus.CONFLICT: 409,
    CommandStatus.CANCELLED: 409,
}


async def dispatch(
    name: str,
    args: dict[str, Any],
    *,
    initiator: str = "ui",
    session_id: str | None = None,
) -> CommandResult:
    """执行一次领域命令。任何 initiator 都不会自动批准。"""
    del initiator  # 保留参数以兼容旧调用方；策略已统一。
    ensure_catalog_loaded()
    if session_id is None:
        from app.local_session import get_request_session_id
        session_id = get_request_session_id()
    bus = get_command_bus()
    return await bus.execute_async(name, args, session_id=session_id)


def waiting_approval_payload(result: CommandResult, *, session_id: str | None = None) -> dict[str, Any]:
    """页面二次确认所需的完整载荷。

    仅在存在有效本机会话时回传 ``approval_token``（Todolist T4）；
    无会话不得把可自批自执行的令牌暴露给匿名调用方。
    """
    data = dict(result.data or {})
    payload: dict[str, Any] = {
        "ok": False,
        "status": result.status.value,
        "summary": result.summary,
        "command": result.command,
        "approval_id": data.get("approval_id"),
        "expires_at": data.get("expires_at"),
        "preflight": result.preflight.model_dump(mode="json") if result.preflight else None,
    }
    if session_id:
        payload["approval_token"] = data.get("approval_token")
    return payload


def result_http_payload(result: CommandResult) -> dict[str, Any]:
    """把 CommandResult 转成可直接被 FastAPI 路由返回的 dict，尽量贴近既有 REST 响应形状。"""
    payload: dict[str, Any] = dict(result.data or {})
    payload.setdefault("ok", result.status in {CommandStatus.SUCCEEDED, CommandStatus.ACCEPTED})
    payload.setdefault("status", result.status.value)
    payload.setdefault("summary", result.summary)
    if result.run_id is not None:
        payload.setdefault("run_id", result.run_id)
    if result.command_id is not None:
        payload.setdefault("command_id", result.command_id)
    if result.ui_intent is not None:
        payload.setdefault("ui_intent", result.ui_intent.model_dump(mode="json"))
    return payload


def _http_status_for(result: CommandResult) -> int | None:
    if result.status == CommandStatus.FAILED:
        code = result.error_code or "domain_error"
        if code.startswith("http_"):
            try:
                return int(code.split("_", 1)[1])
            except ValueError:
                return 409
        return _ERROR_CODE_HTTP_STATUS.get(code, 409)
    # REJECTED 默认是 409，但授权类拒绝必须还原成 403。这里刻意只放行
    # _AUTHZ_REJECTION_CODES 这两个码，不整体改成「REJECTED 也查错误码表」——
    # 那会顺带把 approval_invalid（当前 409）变成 403，属于计划外的行为变更。
    if result.status == CommandStatus.REJECTED and result.error_code in _AUTHZ_REJECTION_CODES:
        return _ERROR_CODE_HTTP_STATUS[result.error_code]
    return _STATUS_HTTP.get(result.status)


def raise_if_failed(result: CommandResult) -> None:
    """非成功态时抛出 HTTPException；``WAITING_APPROVAL`` 由 ``ui_route`` 单独处理为 202。"""
    if result.status == CommandStatus.WAITING_APPROVAL:
        return
    status_code = _http_status_for(result)
    if status_code is not None:
        detail: Any = result.summary
        if isinstance(result.data, dict) and result.data.get("code"):
            detail = {
                **result.data,
                "message": result.data.get("message") or result.summary,
            }
        raise HTTPException(status_code, detail)


raise_for_command_result = raise_if_failed


def respond_ui(result: CommandResult, *, session_id: str | None = None) -> dict[str, Any] | JSONResponse:
    """dispatch 之后的统一 REST 收尾：待批准 → 202；失败 → HTTPException；成功 → payload。"""
    if session_id is None:
        from app.local_session import get_request_session_id
        session_id = get_request_session_id()
    if result.status == CommandStatus.WAITING_APPROVAL:
        return JSONResponse(
            status_code=202,
            content=waiting_approval_payload(result, session_id=session_id),
        )
    if result.status == CommandStatus.ACCEPTED:
        raise_if_failed(result)
        return JSONResponse(status_code=202, content=result_http_payload(result))
    raise_if_failed(result)
    return result_http_payload(result)


async def ui_route(name: str, args: dict[str, Any]) -> dict[str, Any] | JSONResponse | None:
    """REST 入口统一走 Command Bus；Handler 内再次进入同名函数时返回 ``None`` 以执行领域逻辑。

    需要用户批准时返回 HTTP 202 + Impact/token，前端展示后再带
    ``X-Manju-Approval-Token`` 重试。批准令牌绑定本机会话（Todolist T4）。
    """
    from app.capabilities.direct import in_handler
    from app.local_session import get_request_session_id

    if in_handler():
        return None
    session_id = get_request_session_id()
    result = await dispatch(name, args, initiator="ui", session_id=session_id)
    return respond_ui(result, session_id=session_id)
