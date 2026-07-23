"""Command Bus：预检 / 批准 / 幂等 / 执行入口（PRD §6）。

M0 提供可测试合同；领域 handler 未接入前，执行会返回明确的 not_implemented，
禁止伪造业务成功。页面仍走现有 REST，待 M1+ 逐步改为调用本 Bus。
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from pydantic import BaseModel, ValidationError

from app.capabilities import policy
from app.capabilities.registry import CommandSpec, CapabilityRegistry, get_registry
from app.capabilities.schemas import (
    CommandResult,
    CommandStatus,
    IdempotencyPolicy,
    PreflightResult,
    RiskLevel,
    StandardCommandInput,
)


class CommandBus:
    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self.registry = registry or get_registry()
        self._idempotency: dict[str, CommandResult] = {}

    def preflight(self, name: str, raw_args: dict[str, Any] | BaseModel) -> PreflightResult:
        spec = self.registry.get_command(name)
        args = self._parse_input(spec, raw_args)
        payload = args.model_dump(mode="json")
        if spec.preflight is not None:
            result = spec.preflight(args)
            if hasattr(result, "__await__"):
                raise RuntimeError(f"{name}: async preflight must be awaited via preflight_async")
            assert isinstance(result, PreflightResult)
            result.requires_confirmation = policy.requires_confirmation(spec, result)
            return result
        # 默认预检：仅声明元数据，不做领域状态查询（M0）
        fingerprint = policy.state_fingerprint({"command": name, "args": policy.normalize_args(payload)})
        result = PreflightResult(
            command=name,
            allowed=True,
            risk=spec.risk,
            summary=f"预检 {spec.title}",
            state_fingerprint=fingerprint,
            requires_confirmation=False,
            confirmation_policy=spec.confirmation,
        )
        result.requires_confirmation = policy.requires_confirmation(spec, result)
        if result.requires_confirmation and spec.risk in {RiskLevel.R2_MATERIAL, RiskLevel.R3_DESTRUCTIVE}:
            # ALWAYS / 高风险默认要求确认；WHEN_IMPACT 在默认空影响下可能不触发
            if spec.confirmation.value == "always":
                result.requires_confirmation = True
        return result

    async def preflight_async(self, name: str, raw_args: dict[str, Any] | BaseModel) -> PreflightResult:
        spec = self.registry.get_command(name)
        args = self._parse_input(spec, raw_args)
        if spec.preflight is None:
            return self.preflight(name, args)
        result = spec.preflight(args)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        assert isinstance(result, PreflightResult)
        result.requires_confirmation = policy.requires_confirmation(spec, result)
        return result

    def execute(self, name: str, raw_args: dict[str, Any] | BaseModel, *, session_id: str | None = None) -> CommandResult:
        spec = self.registry.get_command(name)
        args = self._parse_input(spec, raw_args)
        payload = args.model_dump(mode="json")

        idem_key = self._resolve_idempotency_key(spec, args, payload)
        if idem_key and idem_key in self._idempotency:
            return self._idempotency[idem_key]

        preflight = self.preflight(name, args)
        if args.dry_run or (spec.supports_dry_run and payload.get("dry_run")):
            return CommandResult(
                status=CommandStatus.SUCCEEDED,
                summary=preflight.summary,
                command=name,
                preflight=preflight,
                data={"dry_run": True},
            )

        if not preflight.allowed:
            return CommandResult(
                status=CommandStatus.REJECTED,
                summary=preflight.denial_message or "命令被策略拒绝",
                command=name,
                preflight=preflight,
                error_code=preflight.denial_code or "policy_denied",
            )

        if preflight.requires_confirmation:
            if not args.approval_token:
                token, approval = policy.issue_approval(
                    command=name,
                    args=payload,
                    preflight=preflight,
                    session_id=session_id,
                    reason=args.reason,
                )
                return CommandResult(
                    status=CommandStatus.WAITING_APPROVAL,
                    summary="需要用户批准后才能执行",
                    command=name,
                    preflight=preflight,
                    data={
                        "approval_id": approval.approval_id,
                        "approval_token": token,
                        "expires_at": approval.expires_at,
                    },
                )
            try:
                policy.consume_approval(
                    args.approval_token,
                    command=name,
                    args=payload,
                    state_fingerprint_now=preflight.state_fingerprint,
                    session_id=session_id,
                )
            except PermissionError as exc:
                return CommandResult(
                    status=CommandStatus.REJECTED,
                    summary=str(exc),
                    command=name,
                    preflight=preflight,
                    error_code="approval_invalid",
                )

        if spec.handler is None:
            result = CommandResult(
                status=CommandStatus.FAILED,
                summary=f"命令 {name} 尚未接入领域 handler（M0 仅注册合同）",
                command=name,
                command_id=f"cmd_{uuid.uuid4().hex[:12]}",
                preflight=preflight,
                error_code="handler_not_implemented",
            )
        else:
            outcome = spec.handler(args)
            if hasattr(outcome, "__await__"):
                raise RuntimeError(f"{name}: async handler must be awaited via execute_async")
            assert isinstance(outcome, CommandResult)
            result = outcome
            result.command = result.command or name
            result.preflight = result.preflight or preflight
            if not result.command_id:
                result.command_id = f"cmd_{uuid.uuid4().hex[:12]}"

        if idem_key and result.status in {
            CommandStatus.ACCEPTED,
            CommandStatus.SUCCEEDED,
        }:
            self._idempotency[idem_key] = result
        return result

    async def execute_async(
        self, name: str, raw_args: dict[str, Any] | BaseModel, *, session_id: str | None = None
    ) -> CommandResult:
        spec = self.registry.get_command(name)
        args = self._parse_input(spec, raw_args)
        payload = args.model_dump(mode="json")
        idem_key = self._resolve_idempotency_key(spec, args, payload)
        if idem_key and idem_key in self._idempotency:
            return self._idempotency[idem_key]

        preflight = await self.preflight_async(name, args)
        if args.dry_run:
            return CommandResult(
                status=CommandStatus.SUCCEEDED,
                summary=preflight.summary,
                command=name,
                preflight=preflight,
                data={"dry_run": True},
            )
        if not preflight.allowed:
            return CommandResult(
                status=CommandStatus.REJECTED,
                summary=preflight.denial_message or "命令被策略拒绝",
                command=name,
                preflight=preflight,
                error_code=preflight.denial_code or "policy_denied",
            )
        if preflight.requires_confirmation:
            if not args.approval_token:
                token, approval = policy.issue_approval(
                    command=name, args=payload, preflight=preflight, session_id=session_id, reason=args.reason
                )
                return CommandResult(
                    status=CommandStatus.WAITING_APPROVAL,
                    summary="需要用户批准后才能执行",
                    command=name,
                    preflight=preflight,
                    data={
                        "approval_id": approval.approval_id,
                        "approval_token": token,
                        "expires_at": approval.expires_at,
                    },
                )
            try:
                policy.consume_approval(
                    args.approval_token,
                    command=name,
                    args=payload,
                    state_fingerprint_now=preflight.state_fingerprint,
                    session_id=session_id,
                )
            except PermissionError as exc:
                return CommandResult(
                    status=CommandStatus.REJECTED,
                    summary=str(exc),
                    command=name,
                    preflight=preflight,
                    error_code="approval_invalid",
                )

        if spec.handler is None:
            result = CommandResult(
                status=CommandStatus.FAILED,
                summary=f"命令 {name} 尚未接入领域 handler（M0 仅注册合同）",
                command=name,
                command_id=f"cmd_{uuid.uuid4().hex[:12]}",
                preflight=preflight,
                error_code="handler_not_implemented",
            )
        else:
            outcome = spec.handler(args)
            if hasattr(outcome, "__await__"):
                outcome = await outcome  # type: ignore[misc]
            assert isinstance(outcome, CommandResult)
            result = outcome
            result.command = result.command or name
            result.preflight = result.preflight or preflight
            if not result.command_id:
                result.command_id = f"cmd_{uuid.uuid4().hex[:12]}"

        if idem_key and result.status in {
            CommandStatus.ACCEPTED,
            CommandStatus.SUCCEEDED,
        }:
            self._idempotency[idem_key] = result
        return result

    def reset_idempotency_for_tests(self) -> None:
        self._idempotency.clear()

    def _parse_input(self, spec: CommandSpec, raw_args: dict[str, Any] | BaseModel) -> StandardCommandInput:
        try:
            if isinstance(raw_args, spec.input_model):
                return raw_args  # type: ignore[return-value]
            if isinstance(raw_args, BaseModel):
                return spec.input_model.model_validate(raw_args.model_dump())
            return spec.input_model.model_validate(raw_args)
        except ValidationError as exc:
            raise ValueError(f"invalid input for {spec.name}: {exc}") from exc

    def _resolve_idempotency_key(
        self, spec: CommandSpec, args: StandardCommandInput, payload: dict[str, Any]
    ) -> str | None:
        if args.idempotency_key:
            return f"{spec.name}:{args.idempotency_key}"
        if spec.idempotency == IdempotencyPolicy.REQUIRED:
            # 未提供时派生稳定键，避免完全裸奔；Agent 侧仍应显式传入
            digest = hashlib.sha256(
                json_dumps(policy.normalize_args(payload)).encode("utf-8")
            ).hexdigest()[:24]
            return f"{spec.name}:auto:{digest}"
        return None


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


_BUS = CommandBus()


def get_command_bus() -> CommandBus:
    return _BUS


def reset_command_bus_for_tests() -> CommandBus:
    global _BUS
    policy.reset_approvals_for_tests()
    _BUS = CommandBus(get_registry())
    _BUS.reset_idempotency_for_tests()
    return _BUS
