"""MCP 对外接入层（PRD AGENT_MCP_CAPABILITY §9 / M4）。

只做协议适配：把 Capability Registry / Command Bus 映射为 MCP Tools/Resources/Prompts，
不在这里重复业务规则、风险判定或批准逻辑——那些永远只在 `app.capabilities` 里判定一次。
"""
from __future__ import annotations

from app.mcp.server import router

__all__ = ["router"]
