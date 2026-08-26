"""Capability Registry：声明能力元数据，不处理自然语言。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel

from app.capabilities.schemas import (
    CommandResult,
    ConfirmationPolicy,
    IdempotencyPolicy,
    PreflightResult,
    RiskLevel,
    StandardCommandInput,
)

InputT = TypeVar("InputT", bound=BaseModel)
Handler = Callable[..., Awaitable[CommandResult] | CommandResult]
PreflightHandler = Callable[..., Awaitable[PreflightResult] | PreflightResult]


class CapabilityKind(str, Enum):
    RESOURCE = "resource"
    DOMAIN_TOOL = "domain_tool"
    UI_TOOL = "ui_tool"
    HUMAN_ONLY = "human_only"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """领域命令唯一合同（PRD §3.3）。"""

    name: str
    version: str
    title: str
    description: str
    input_model: type[BaseModel]
    risk: RiskLevel
    confirmation: ConfirmationPolicy
    idempotency: IdempotencyPolicy
    scopes: frozenset[str]
    side_effect: str
    supports_dry_run: bool = True
    supports_cancel: bool = False
    mcp_exposed: bool = True
    admin_only: bool = False
    handler: Handler | None = None
    preflight: PreflightHandler | None = None
    rest_routes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def kind(self) -> CapabilityKind:
        return CapabilityKind.DOMAIN_TOOL


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """可寻址只读上下文（PRD §9.2）。"""

    name: str
    uri_template: str
    title: str
    description: str
    scopes: frozenset[str] = frozenset({"manju:read"})
    mcp_exposed: bool = True
    rest_routes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def kind(self) -> CapabilityKind:
        return CapabilityKind.RESOURCE

    @property
    def risk(self) -> RiskLevel:
        return RiskLevel.R0_READ


@dataclass(frozen=True, slots=True)
class UiIntentSpec:
    """仅改变浏览器视图的白名单意图（PRD §3.1 / §11.3）。不作为外部 MCP 核心 Tool。"""

    name: str
    title: str
    description: str
    intent_type: str
    mcp_exposed: bool = False
    tags: tuple[str, ...] = ()

    @property
    def kind(self) -> CapabilityKind:
        return CapabilityKind.UI_TOOL

    @property
    def risk(self) -> RiskLevel:
        return RiskLevel.R0_READ


@dataclass(frozen=True, slots=True)
class HumanOnlySpec:
    """Agent 可引导，但必须由用户亲自完成（密钥、目录授权、首次选文件）。"""

    name: str
    title: str
    description: str
    reason: str
    related_ui_intent: str | None = None
    rest_routes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def kind(self) -> CapabilityKind:
        return CapabilityKind.HUMAN_ONLY

    @property
    def risk(self) -> RiskLevel:
        return RiskLevel.R4_SECRET

    @property
    def mcp_exposed(self) -> bool:
        return False


@dataclass
class CapabilityRegistry:
    commands: dict[str, CommandSpec] = field(default_factory=dict)
    resources: dict[str, ResourceSpec] = field(default_factory=dict)
    ui_intents: dict[str, UiIntentSpec] = field(default_factory=dict)
    human_only: dict[str, HumanOnlySpec] = field(default_factory=dict)
    # REST method+path → capability name（用于覆盖扫描）
    rest_bindings: dict[str, str] = field(default_factory=dict)
    # 明确豁免：不进入 Agent/MCP，但必须登记原因
    rest_exemptions: dict[str, str] = field(default_factory=dict)

    def register_command(self, spec: CommandSpec) -> CommandSpec:
        if spec.name in self.commands:
            raise ValueError(f"duplicate command: {spec.name}")
        if not issubclass(spec.input_model, BaseModel):
            raise TypeError(f"{spec.name}: input_model must be a Pydantic BaseModel")
        if not issubclass(spec.input_model, StandardCommandInput):
            raise TypeError(f"{spec.name}: input_model must extend StandardCommandInput")
        self.commands[spec.name] = spec
        for route in spec.rest_routes:
            self._bind_rest(route, spec.name)
        return spec

    def register_resource(self, spec: ResourceSpec) -> ResourceSpec:
        if spec.name in self.resources:
            raise ValueError(f"duplicate resource: {spec.name}")
        self.resources[spec.name] = spec
        for route in spec.rest_routes:
            self._bind_rest(route, spec.name)
        return spec

    def register_ui(self, spec: UiIntentSpec) -> UiIntentSpec:
        if spec.name in self.ui_intents:
            raise ValueError(f"duplicate ui intent: {spec.name}")
        self.ui_intents[spec.name] = spec
        return spec

    def register_human_only(self, spec: HumanOnlySpec) -> HumanOnlySpec:
        if spec.name in self.human_only:
            raise ValueError(f"duplicate human-only: {spec.name}")
        self.human_only[spec.name] = spec
        for route in spec.rest_routes:
            self._bind_rest(route, spec.name)
        return spec

    def exempt_rest(self, route: str, reason: str) -> None:
        key = _normalize_route(route)
        if not reason.strip():
            raise ValueError(f"exemption for {key} requires a non-empty reason")
        self.rest_exemptions[key] = reason.strip()

    def _bind_rest(self, route: str, capability_name: str) -> None:
        key = _normalize_route(route)
        existing = self.rest_bindings.get(key)
        if existing and existing != capability_name:
            raise ValueError(f"REST {key} already bound to {existing}, cannot bind {capability_name}")
        self.rest_bindings[key] = capability_name

    def get_command(self, name: str) -> CommandSpec:
        try:
            return self.commands[name]
        except KeyError as exc:
            raise KeyError(f"unknown command: {name}") from exc

    def list_mcp_tools(self) -> list[CommandSpec]:
        return [spec for spec in self.commands.values() if spec.mcp_exposed and not spec.admin_only]

    def coverage_snapshot(self) -> dict[str, Any]:
        return {
            "commands": sorted(self.commands),
            "resources": sorted(self.resources),
            "ui_intents": sorted(self.ui_intents),
            "human_only": sorted(self.human_only),
            "rest_bindings": dict(sorted(self.rest_bindings.items())),
            "rest_exemptions": dict(sorted(self.rest_exemptions.items())),
            "counts": {
                "commands": len(self.commands),
                "resources": len(self.resources),
                "ui_intents": len(self.ui_intents),
                "human_only": len(self.human_only),
                "rest_bindings": len(self.rest_bindings),
                "rest_exemptions": len(self.rest_exemptions),
            },
        }


_REGISTRY = CapabilityRegistry()


def get_registry() -> CapabilityRegistry:
    return _REGISTRY


def _normalize_route(route: str) -> str:
    text = " ".join(route.strip().split())
    method, _, path = text.partition(" ")
    if not method or not path:
        raise ValueError(f"route must be 'METHOD /path', got: {route!r}")
    method = method.upper()
    if not path.startswith("/"):
        path = "/" + path
    # 统一去掉末尾斜杠（根路径除外）
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return f"{method} {path}"
