"""把 Capability Registry 暴露为 OpenAI 原生 function-calling `tools` 数组。

对话 Agent 每轮把这份 `tools` 随请求下发给模型，取代过去「把整份目录内联进 system
prompt + 手写 JSON 协议」的做法。工具执行仍统一委托 Command Bus（见 orchestrator）。

安全不变量：
- 只暴露 `mcp_exposed and not admin_only` 的命令（与 MCP 同一张白名单）。
- 入参 schema 剔除 StandardCommandInput 的内部字段（approval_token 等），模型无法自填
  批准令牌绕过用户批准。
"""
from __future__ import annotations

from typing import Any

from app.capabilities import get_registry
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.registry import CapabilityRegistry, CommandSpec
from app.capabilities.tool_schemas import command_input_schema

# 只读资源读取工具名（保持与 MCP resources 语义一致；非 Command Bus 命令，orchestrator 单独路由）。
RESOURCE_READ_TOOL_NAME = "resource.read"
RESOURCE_READ_ALIASES = frozenset({"resource.read", "resource_read", "read_resource"})

# 缓存：registry 对象不变时复用已构建的 tools 数组，避免每轮重复生成 model_json_schema。
_CACHE: tuple[int, list[dict[str, Any]]] | None = None


def _command_description(spec: CommandSpec) -> str:
    """命令描述：标题 + 说明 + 风险等级，帮助模型判断哪些操作会触发用户批准。"""
    detail = f"{spec.title} —— {spec.description}".strip(" —")
    return f"{detail}（risk={spec.risk.value}）"


def _resource_read_tool(registry: CapabilityRegistry) -> dict[str, Any]:
    """resource.read 工具：uri 模板列于描述中，替代旧 system prompt 里的资源目录。"""
    templates = [
        f"{spec.uri_template}：{spec.title}"
        for spec in registry.resources.values()
    ]
    hint = "\n".join(f"  - {line}" for line in templates)
    description = (
        "读取只读业务资源快照（项目 / 剧集 / 分镜 / Run / Artifact 等），业务事实以其返回为准。"
        "\nuri 取自以下 manju:// 模板之一：\n" + hint
    )
    return {
        "type": "function",
        "function": {
            "name": RESOURCE_READ_TOOL_NAME,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "manju:// 资源 URI，例如 manju://projects 或 manju://project/{project_id}",
                    }
                },
                "required": ["uri"],
                "additionalProperties": False,
            },
        },
    }


def _domain_tool(spec: CommandSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": _command_description(spec),
            "parameters": command_input_schema(spec, strip_internal=True),
        },
    }


def build_agent_tools() -> list[dict[str, Any]]:
    """构建下发给模型的 OpenAI tools 数组：resource.read + 所有对 Agent 开放的领域命令。"""
    global _CACHE
    ensure_catalog_loaded()
    registry = get_registry()
    if _CACHE is not None and _CACHE[0] == id(registry):
        return _CACHE[1]
    tools: list[dict[str, Any]] = [_resource_read_tool(registry)]
    tools.extend(_domain_tool(spec) for spec in registry.list_mcp_tools())
    _CACHE = (id(registry), tools)
    return tools


def is_resource_read(name: str) -> bool:
    return name in RESOURCE_READ_ALIASES


def reset_tools_cache_for_tests() -> None:
    global _CACHE
    _CACHE = None
