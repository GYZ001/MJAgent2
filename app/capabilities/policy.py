"""风险、确认与批准策略（PRD §8）。MCP annotations 不能覆盖本模块结论。"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import (
    ApprovalDecision,
    ApprovalTokenPayload,
    ConfirmationPolicy,
    PreflightResult,
    RiskLevel,
)

# 破坏性操作批准默认短时效（PRD §8.3）
DEFAULT_APPROVAL_TTL_S = 15 * 60
DESTRUCTIVE_APPROVAL_TTL_S = 5 * 60

_TOKEN_SECRET = secrets.token_bytes(32)
_APPROVALS: dict[str, ApprovalTokenPayload] = {}


def approval_ttl_for(risk: RiskLevel, command_name: str) -> int:
    if risk == RiskLevel.R3_DESTRUCTIVE or command_name in {
        "project.delete",
        "video.clear_episode",
        "video.clear_shot",
        "delivery.review",
    }:
        return DESTRUCTIVE_APPROVAL_TTL_S
    return DEFAULT_APPROVAL_TTL_S


def requires_confirmation(spec: CommandSpec, preflight: PreflightResult) -> bool:
    if not preflight.allowed:
        return False
    policy = spec.confirmation
    if policy == ConfirmationPolicy.NEVER:
        return False
    if policy == ConfirmationPolicy.ALWAYS:
        return True
    if policy == ConfirmationPolicy.WHEN_IMPACT:
        affected = preflight.affected
        return bool(
            affected.invalidated_artifacts
            or affected.shot_count > 1
            or affected.versions
            or affected.packages
            or preflight.estimated_cost_cny
            or preflight.warnings
        )
    # OPTIONAL：调用方可选择自动，但服务端仍可在高风险时升级
    return preflight.risk in {RiskLevel.R2_MATERIAL, RiskLevel.R3_DESTRUCTIVE}


def normalize_args(payload: dict[str, Any]) -> dict[str, Any]:
    """去掉标准追踪/批准字段后再做参数指纹，避免 request_id / reason 破坏幂等与批准校验。

    ``reason`` 常在批准卡由用户事后填写，不能绑进 Approval Token 的 args_hash。
    """
    skip = {"request_id", "idempotency_key", "dry_run", "approval_token", "reason"}
    return {key: payload[key] for key in sorted(payload) if key not in skip}


def args_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(normalize_args(payload), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def state_fingerprint(parts: dict[str, Any]) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def issue_approval(
    *,
    command: str,
    args: dict[str, Any],
    preflight: PreflightResult,
    session_id: str | None = None,
    reason: str | None = None,
) -> tuple[str, ApprovalTokenPayload]:
    approval_id = f"appr_{secrets.token_hex(8)}"
    ttl = approval_ttl_for(preflight.risk, command)
    payload = ApprovalTokenPayload(
        approval_id=approval_id,
        command=command,
        args_hash=args_hash(args),
        state_fingerprint=preflight.state_fingerprint,
        session_id=session_id,
        expires_at=time.time() + ttl,
        reason=reason,
        impact_snapshot=preflight.model_dump(mode="json"),
    )
    _APPROVALS[approval_id] = payload
    token = _sign_token(approval_id)
    return token, payload


def consume_approval(
    token: str,
    *,
    command: str,
    args: dict[str, Any],
    state_fingerprint_now: str,
    session_id: str | None = None,
) -> ApprovalTokenPayload:
    approval_id = _verify_token(token)
    payload = _APPROVALS.get(approval_id)
    if payload is None:
        raise PermissionError("approval_token unknown or revoked")
    if payload.used_at is not None:
        raise PermissionError("approval_token already used")
    if time.time() > payload.expires_at:
        raise PermissionError("approval_token expired")
    if payload.command != command:
        raise PermissionError("approval_token command mismatch")
    if payload.args_hash != args_hash(args):
        raise PermissionError("approval_token args mismatch")
    if payload.state_fingerprint != state_fingerprint_now:
        raise PermissionError("approval_token state fingerprint mismatch")
    if payload.session_id is not None and payload.session_id != session_id:
        raise PermissionError("approval_token session mismatch")
    payload.used_at = time.time()
    payload.decision = ApprovalDecision.APPROVE
    _APPROVALS[approval_id] = payload
    return payload


def decide_approval(approval_id: str, decision: ApprovalDecision, reason: str | None = None) -> ApprovalTokenPayload:
    payload = _APPROVALS.get(approval_id)
    if payload is None:
        raise KeyError(f"unknown approval: {approval_id}")
    if payload.used_at is not None:
        raise PermissionError("approval already finalized")
    payload.decision = decision
    payload.reason = reason
    if decision == ApprovalDecision.REJECT:
        payload.used_at = time.time()
    _APPROVALS[approval_id] = payload
    return payload


def reset_approvals_for_tests() -> None:
    _APPROVALS.clear()


def _sign_token(approval_id: str) -> str:
    sig = hmac.new(_TOKEN_SECRET, approval_id.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"{approval_id}.{sig}"


def _verify_token(token: str) -> str:
    if "." not in token:
        raise PermissionError("malformed approval_token")
    approval_id, sig = token.rsplit(".", 1)
    expected = hmac.new(_TOKEN_SECRET, approval_id.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, expected):
        raise PermissionError("invalid approval_token signature")
    return approval_id
