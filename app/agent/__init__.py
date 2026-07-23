"""对话 Agent 后端（PRD AGENT_MCP_CAPABILITY_PRD.md §7/§10，M1）。

对话 Agent 只编排：理解意图、选择 Resource/Command、经批准后调用统一的
Capability Command Bus；不直接写数据库、不伪造执行结果。
"""
from __future__ import annotations
