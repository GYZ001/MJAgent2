"""统一执行入口（PRD M2+M3）。

- ``initiator="ui"``：页面 REST 复用。为保持单次请求体验，若预检要求确认，
  在同一次调用内自动签发并消费 approval token 再执行，页面无需二次往返。
  这不弱化风控——审批仍由服务端 Policy 强制签发/校验，只是把「批准」这一步
  从「用户点两次」收敛为「页面点一次」，因为页面本身就是已确认的用户操作入口。
- ``initiator="agent" | "mcp"``：不自动批准，遇到需要确认的命令原样返回
  ``WAITING_APPROVAL``，交由对话 Agent / 外部 MCP Client 走标准批准流程。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.capabilities import ensure_catalog_loaded
from app.capabilities.bus import get_command_bus
from app.capabilities.schemas import CommandResult, CommandStatus

# 只有页面发起的调用被视为「已经过用户点击确认」，可代为完成一次性批准。
_AUTO_APPROVE_INITIATORS = {"ui"}

_ERROR_CODE_HTTP_STATUS: dict[str, int] = {
    "not_found": 404,
    "invalid_state": 409,
    "invalid_input": 422,
    "attachment_invalid": 400,
    "policy_denied": 403,
    "approval_invalid": 403,
    "handler_not_implemented": 501,
}

_STATUS_HTTP: dict[CommandStatus, int] = {
    CommandStatus.REJECTED: 409,
    CommandStatus.CONFLICT: 409,
    CommandStatus.CANCELLED: 409,
    CommandStatus.WAITING_APPROVAL: 202,
}


async def dispatch(
    name: str,
    args: dict[str, Any],
    *,
    initiator: str = "ui",
    session_id: str | None = None,
) -> CommandResult:
    """执行一次领域命令。UI initiator 在同一请求内完成「预检→自动批准→执行」。"""
    ensure_catalog_loaded()
    bus = get_command_bus()
    result = await bus.execute_async(name, args, session_id=session_id)
    if result.status != CommandStatus.WAITING_APPROVAL or initiator not in _AUTO_APPROVE_INITIATORS:
        return result
    approval_token = (result.data or {}).get("approval_token")
    if not approval_token:
        return result
    approved_args = {**args, "approval_token": approval_token}
    return await bus.execute_async(name, approved_args, session_id=session_id)


def result_http_payload(result: CommandResult) -> dict[str, Any]:
    """把 CommandResult 转成可直接被 FastAPI 路由返回的 dict，尽量贴近既有 REST 响应形状。

    ``result.data`` 是 handler 产出的领域字段（大多是原路由本来就会返回的 dict），
    在此基础上补齐 ``ok/summary/run_id/command_id/ui_intent`` 等通用元信息，
    不覆盖 handler 已经给出的同名业务字段。
    """
    payload: dict[str, Any] = dict(result.data or {})
    payload.setdefault("ok", result.status in {CommandStatus.SUCCEEDED, CommandStatus.ACCEPTED})
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
    return _STATUS_HTTP.get(result.status)


def raise_if_failed(result: CommandResult) -> None:
    """非成功态时抛出 HTTPException，保持既有 REST 错误语义；成功/已受理态放行。

    ``WAITING_APPROVAL`` 理论上不会出现在 ``initiator="ui"`` 路径（已自动批准），
    真出现时说明批准签发失败或预检拒绝——同样必须报错，不能被前端当作成功处理。
    """
    status_code = _http_status_for(result)
    if status_code is not None:
        raise HTTPException(status_code, result.summary)


# 兼容 PRD 示例中使用的命名。
raise_for_command_result = raise_if_failed
