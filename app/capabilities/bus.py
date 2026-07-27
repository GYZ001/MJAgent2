"""Command Bus：预检 / 批准 / 幂等 / 执行入口（PRD §6）。

M0 提供可测试合同；领域 handler 未接入前，执行会返回明确的 not_implemented，
禁止伪造业务成功。页面仍走现有 REST，待 M1+ 逐步改为调用本 Bus。
"""
from __future__ import annotations

import contextvars
import uuid
from typing import Any

from pydantic import BaseModel, ValidationError

from app.capabilities import idempotency as idem_store
from app.capabilities import policy
from app.capabilities.registry import CapabilityRegistry, CommandSpec, get_registry
from app.capabilities.schemas import (
    CommandResult,
    CommandStatus,
    IdempotencyPolicy,
    PreflightResult,
    RiskLevel,
    StandardCommandInput,
)

# 由 HTTP 中间件注入：页面二次确认时通过请求头携带，无需改每个 route 签名。
_request_approval_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_approval_token", default=None
)


def set_request_approval_token(token: str | None) -> None:
    _request_approval_token.set(token)


def get_request_approval_token() -> str | None:
    return _request_approval_token.get()


class CommandBus:
    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self.registry = registry or get_registry()

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
        return self._execute_sync(name, raw_args, session_id=session_id)

    async def execute_async(
        self, name: str, raw_args: dict[str, Any] | BaseModel, *, session_id: str | None = None
    ) -> CommandResult:
        return await self._execute(name, raw_args, session_id=session_id, allow_await=True)

    def _execute_sync(
        self, name: str, raw_args: dict[str, Any] | BaseModel, *, session_id: str | None = None
    ) -> CommandResult:
        # 同步路径禁止 await handler；与旧 API 兼容。
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._execute(name, raw_args, session_id=session_id, allow_await=False))
        # 已在事件循环中：走同步 handler 分支（async handler 会报错）
        return self._run_pipeline(name, raw_args, session_id=session_id, outcome_resolver=self._resolve_sync)

    async def _execute(
        self,
        name: str,
        raw_args: dict[str, Any] | BaseModel,
        *,
        session_id: str | None,
        allow_await: bool,
    ) -> CommandResult:
        async def resolve(spec: CommandSpec, args: StandardCommandInput, preflight: PreflightResult) -> CommandResult:
            return await self._run_handler_async(spec, args, preflight, allow_await=allow_await)

        return await self._run_pipeline_async(name, raw_args, session_id=session_id, outcome_resolver=resolve)

    def _run_pipeline(self, name, raw_args, *, session_id, outcome_resolver) -> CommandResult:
        spec = self.registry.get_command(name)
        args = self._inject_approval(self._parse_input(spec, raw_args))
        payload = args.model_dump(mode="json")
        idem_key = self._resolve_idempotency_key(spec, args)
        if idem_key:
            cached = idem_store.lookup(idem_key)
            if cached is not None:
                return cached

        preflight = self.preflight(name, args)
        gated = self._gate(name, args, payload, preflight, session_id=session_id)
        if gated is not None:
            return gated

        claimed = False
        if idem_key:
            raced = idem_store.claim(idem_key, command=name)
            if raced is not None:
                return raced
            claimed = True

        try:
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
                result = outcome_resolver(spec, args, preflight)

            if idem_key:
                if result.status in {CommandStatus.ACCEPTED, CommandStatus.SUCCEEDED}:
                    idem_store.store(idem_key, command=name, result=result)
                elif claimed:
                    idem_store.release_if_running(idem_key)
            return result
        except Exception:
            if claimed and idem_key:
                idem_store.release_if_running(idem_key)
            raise

    async def _run_pipeline_async(self, name, raw_args, *, session_id, outcome_resolver) -> CommandResult:
        spec = self.registry.get_command(name)
        args = self._inject_approval(self._parse_input(spec, raw_args))
        payload = args.model_dump(mode="json")
        idem_key = self._resolve_idempotency_key(spec, args)
        if idem_key:
            cached = idem_store.lookup(idem_key)
            if cached is not None:
                return cached

        preflight = await self.preflight_async(name, args)
        gated = self._gate(name, args, payload, preflight, session_id=session_id)
        if gated is not None:
            return gated

        claimed = False
        if idem_key:
            raced = idem_store.claim(idem_key, command=name)
            if raced is not None:
                return raced
            claimed = True

        try:
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
                result = await outcome_resolver(spec, args, preflight)

            if idem_key:
                if result.status in {CommandStatus.ACCEPTED, CommandStatus.SUCCEEDED}:
                    idem_store.store(idem_key, command=name, result=result)
                elif claimed:
                    idem_store.release_if_running(idem_key)
            return result
        except Exception:
            if claimed and idem_key:
                idem_store.release_if_running(idem_key)
            raise

    def _gate(
        self,
        name: str,
        args: StandardCommandInput,
        payload: dict[str, Any],
        preflight: PreflightResult,
        *,
        session_id: str | None,
    ) -> CommandResult | None:
        if not preflight.allowed:
            denial_data: dict[str, Any] = {}
            if preflight.denial_code:
                denial_data = {
                    "code": preflight.denial_code,
                    "message": preflight.denial_message or "命令被策略拒绝",
                    **(preflight.affected.extra or {}),
                }
                if preflight.denial_code == "version_conflict":
                    # 兼容旧客户端对 detail 做“版本冲突”包含判断，
                    # 新客户端则消费 code/current_version/diff 结构。
                    denial_data["版本冲突"] = True
            return CommandResult(
                status=CommandStatus.REJECTED,
                summary=preflight.denial_message or "命令被策略拒绝",
                command=name,
                preflight=preflight,
                error_code=preflight.denial_code or "policy_denied",
                data=denial_data,
            )
        if args.dry_run:
            return CommandResult(
                status=CommandStatus.SUCCEEDED,
                summary=preflight.summary,
                command=name,
                preflight=preflight,
                data={"dry_run": True},
            )
        if not preflight.requires_confirmation:
            return None
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
        return None

    def _resolve_sync(self, spec: CommandSpec, args: StandardCommandInput, preflight: PreflightResult) -> CommandResult:
        from app.capabilities.direct import enter_handler

        with enter_handler():
            outcome = spec.handler(args)  # type: ignore[misc]
            if hasattr(outcome, "__await__"):
                raise RuntimeError(f"{spec.name}: async handler must be awaited via execute_async")
        assert isinstance(outcome, CommandResult)
        return self._finalize(spec.name, outcome, preflight)

    async def _run_handler_async(
        self, spec: CommandSpec, args: StandardCommandInput, preflight: PreflightResult, *, allow_await: bool
    ) -> CommandResult:
        from app.capabilities.direct import enter_handler

        with enter_handler():
            outcome = spec.handler(args)  # type: ignore[misc]
            if hasattr(outcome, "__await__"):
                if not allow_await:
                    raise RuntimeError(f"{spec.name}: async handler must be awaited via execute_async")
                outcome = await outcome  # type: ignore[misc]
        assert isinstance(outcome, CommandResult)
        return self._finalize(spec.name, outcome, preflight)

    def _finalize(self, name: str, result: CommandResult, preflight: PreflightResult) -> CommandResult:
        result.command = result.command or name
        result.preflight = result.preflight or preflight
        if not result.command_id:
            result.command_id = f"cmd_{uuid.uuid4().hex[:12]}"
        return result

    def reset_idempotency_for_tests(self) -> None:
        idem_store.clear_for_tests()

    def _inject_approval(self, args: StandardCommandInput) -> StandardCommandInput:
        if args.approval_token:
            return args
        header_token = get_request_approval_token()
        if not header_token:
            return args
        return args.model_copy(update={"approval_token": header_token})

    def _parse_input(self, spec: CommandSpec, raw_args: dict[str, Any] | BaseModel) -> StandardCommandInput:
        try:
            if isinstance(raw_args, spec.input_model):
                return raw_args  # type: ignore[return-value]
            if isinstance(raw_args, BaseModel):
                return spec.input_model.model_validate(raw_args.model_dump())
            return spec.input_model.model_validate(raw_args)
        except ValidationError as exc:
            raise ValueError(f"invalid input for {spec.name}: {exc}") from exc

    def _resolve_idempotency_key(self, spec: CommandSpec, args: StandardCommandInput) -> str | None:
        """仅在显式提供 key 时启用幂等。

        禁止按参数自动派生永久键：否则 resume/重跑会误命中陈旧成功结果。
        REQUIRED 但未传 key 时仍允许执行（UI 单击），只是不缓存。
        """
        if not args.idempotency_key:
            return None
        if spec.idempotency == IdempotencyPolicy.NONE:
            return None
        return idem_store.make_key(spec.name, args.idempotency_key)


_BUS = CommandBus()


def get_command_bus() -> CommandBus:
    return _BUS


def reset_command_bus_for_tests() -> CommandBus:
    global _BUS
    policy.reset_approvals_for_tests()
    _BUS = CommandBus(get_registry())
    _BUS.reset_idempotency_for_tests()
    return _BUS
