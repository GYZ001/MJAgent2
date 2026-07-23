"""把 CommandSpec 映射为 MCP Tools（PRD §9.3）。

annotations 只是给客户端的提示，真正的风险/批准判定永远发生在
`app.capabilities.bus.CommandBus` 里；这里绝不重复或放宽那套策略。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.capabilities import ensure_catalog_loaded, get_command_bus, get_registry
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import CommandStatus, IdempotencyPolicy, RiskLevel
from app.mcp.errors import ForbiddenError, McpError

if TYPE_CHECKING:
    from app.mcp.auth import TokenClaims


def _tool_definition(spec: CommandSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "title": spec.title,
        "description": spec.description,
        "inputSchema": spec.input_model.model_json_schema(),
        "annotations": {
            "title": spec.title,
            "readOnlyHint": spec.risk == RiskLevel.R0_READ,
            "destructiveHint": spec.risk == RiskLevel.R3_DESTRUCTIVE,
            "idempotentHint": spec.idempotency != IdempotencyPolicy.NONE,
            "openWorldHint": False,
        },
        "_meta": {
            "version": spec.version,
            "risk": spec.risk.value,
            "scopes": sorted(spec.scopes),
            "side_effect": spec.side_effect,
            "supports_dry_run": spec.supports_dry_run,
            "supports_cancel": spec.supports_cancel,
            "confirmation": spec.confirmation.value,
            "idempotency": spec.idempotency.value,
        },
    }


def list_mcp_tools() -> list[dict[str, Any]]:
    ensure_catalog_loaded()
    registry = get_registry()
    return [_tool_definition(spec) for spec in registry.list_mcp_tools()]


async def call_tool(name: str, arguments: dict[str, Any], *, claims: "TokenClaims") -> dict[str, Any]:
    ensure_catalog_loaded()
    registry = get_registry()
    try:
        spec = registry.get_command(name)
    except KeyError as exc:
        raise McpError(-32602, f"unknown tool: {name}") from exc
    if not spec.mcp_exposed or spec.admin_only:
        raise McpError(-32602, f"tool not exposed via mcp: {name}")

    missing_scopes = spec.scopes - claims.scopes
    if missing_scopes:
        raise ForbiddenError(
            f"token 缺少调用 {name} 所需 scope：{sorted(missing_scopes)}"
        )

    bus = get_command_bus()
    try:
        result = await bus.execute_async(name, arguments, session_id=claims.token_id)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc

    payload = result.model_dump(mode="json")
    is_error = result.status in {
        CommandStatus.FAILED,
        CommandStatus.REJECTED,
        CommandStatus.CONFLICT,
    }
    lines = [f"[{result.status.value}] {result.summary}"]
    if result.status == CommandStatus.WAITING_APPROVAL:
        approval_id = (payload.get("data") or {}).get("approval_id")
        lines.append(f"需要用户批准后才能执行（approval_id={approval_id}），当前未执行任何业务变更。")
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": payload,
        "isError": is_error,
    }
