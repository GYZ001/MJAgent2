"""领域命令 JSON Schema 生成（供 MCP Tools 与对话 Agent 原生 tool calling 复用）。

单一来源：所有 Tool 的入参 schema 都来自 `spec.input_model.model_json_schema()`。
MCP 客户端拿到完整 schema；对话 Agent 则剔除 `StandardCommandInput` 的内部协议字段
（`approval_token` / `idempotency_key` 等由执行层管理，不能交给模型自由填写）。
"""
from __future__ import annotations

from typing import Any

from app.capabilities.registry import CommandSpec

# StandardCommandInput 的内部协议字段：由 Command Bus / 编排器管理，绝不暴露给模型选择。
# - approval_token：一次性批准令牌，模型自行填写即可绕过用户批准（P0）。
# - idempotency_key：编排器按 turn/seq 生成，模型乱填会破坏幂等去重。
# - request_id / expected_version / dry_run / reason：执行层职责，非领域参数。
INTERNAL_INPUT_FIELDS = (
    "request_id",
    "idempotency_key",
    "expected_version",
    "dry_run",
    "approval_token",
    "reason",
)


def command_input_schema(spec: CommandSpec, *, strip_internal: bool = False) -> dict[str, Any]:
    """返回命令入参 JSON Schema。

    strip_internal=True 时移除 StandardCommandInput 的内部字段，用于对话 Agent 的
    原生 function calling `parameters`。
    """
    schema = spec.input_model.model_json_schema()
    if strip_internal:
        _strip_internal_fields(schema)
    return schema


def _strip_internal_fields(schema: dict[str, Any]) -> None:
    props = schema.get("properties")
    if isinstance(props, dict):
        for field in INTERNAL_INPUT_FIELDS:
            props.pop(field, None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [name for name in required if name not in INTERNAL_INPUT_FIELDS]
