"""Capability Registry / Command Bus — 领域能力唯一来源（PRD AGENT_MCP_CAPABILITY）。

MCP、内嵌 Agent、页面 REST 都应复用本包注册的 CommandSpec，而不是各自复制业务规则。
M0 只建立合同与覆盖门禁；领域 handler 在后续里程碑从现有 route 中抽取。
"""
from __future__ import annotations

from app.capabilities.bus import CommandBus, get_command_bus
from app.capabilities.registry import (
    CapabilityKind,
    CapabilityRegistry,
    CommandSpec,
    HumanOnlySpec,
    ResourceSpec,
    UiIntentSpec,
    get_registry,
)
from app.capabilities.schemas import (
    ApprovalDecision,
    CommandResult,
    CommandStatus,
    ConfirmationPolicy,
    IdempotencyPolicy,
    PreflightResult,
    RiskLevel,
    StandardCommandInput,
    UiIntent,
)

__all__ = [
    "ApprovalDecision",
    "CapabilityKind",
    "CapabilityRegistry",
    "CommandBus",
    "CommandResult",
    "CommandSpec",
    "CommandStatus",
    "ConfirmationPolicy",
    "HumanOnlySpec",
    "IdempotencyPolicy",
    "PreflightResult",
    "ResourceSpec",
    "RiskLevel",
    "StandardCommandInput",
    "UiIntent",
    "UiIntentSpec",
    "get_command_bus",
    "get_registry",
]
