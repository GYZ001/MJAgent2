"""delivery.* Command Handlers（成片拼接、交付候选、审批与客户反馈）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import accepted, call_guarded, failed, succeeded
from app.capabilities.schemas import CommandResult


async def concatenate(args: I.EpisodeScopedInput) -> CommandResult:
    from app import worker
    from app.capabilities.bus import canonical_command_request_fingerprint

    request_fingerprint = canonical_command_request_fingerprint(
        "delivery.concatenate",
        args.model_dump(mode="json"),
    )
    try:
        claim_token, replay = worker.claim_concat_operation(
            idempotency_key=args.idempotency_key or "",
            request_fingerprint=request_fingerprint,
            episode_id=args.episode_id,
        )
    except worker.ConcatOperationConflict as exc:
        return failed(str(exc), error_code="idempotency_request_mismatch")
    except worker.ConcatOperationInProgress as exc:
        return accepted(
            str(exc),
            data={"idempotency_in_progress": True},
            resource_uris=[f"manju://episodes/{args.episode_id}/delivery"],
        )
    if replay is not None:
        return succeeded(
            "本集已按镜号顺序拼接成片",
            data=replay,
            resource_uris=[f"manju://episodes/{args.episode_id}/delivery"],
        )
    assert claim_token is not None

    outcome = await call_guarded(
        worker.concatenate_episode,
        args.episode_id,
        operation_idempotency_key=args.idempotency_key,
        operation_request_fingerprint=request_fingerprint,
        operation_claim_token=claim_token,
    )
    if isinstance(outcome, CommandResult):
        worker.release_concat_operation(
            idempotency_key=args.idempotency_key or "",
            request_fingerprint=request_fingerprint,
            claim_token=claim_token,
        )
        return outcome
    return succeeded("本集已按镜号顺序拼接成片", data=outcome, resource_uris=[f"manju://episodes/{args.episode_id}/delivery"])


async def check(args: I.EpisodeScopedInput) -> CommandResult:
    from app.delivery import delivery_readiness

    outcome = await call_guarded(delivery_readiness, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    ready = outcome.get("ready")
    blockers = outcome.get("blockers") or []
    summary = "交付就绪检查通过" if ready else f"交付就绪检查未通过，{len(blockers)} 项阻塞"
    return succeeded(summary, data=outcome, resource_uris=[f"manju://episodes/{args.episode_id}/delivery"])


async def create_package(args: I.EpisodeScopedInput) -> CommandResult:
    from app.orchestration import api as orch_api

    outcome = await call_guarded(
        orch_api.create_delivery_package,
        args.episode_id,
        body={
            "idempotency_key": args.idempotency_key,
            "request_id": args.request_id,
        },
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        f"交付候选 {outcome.get('package_id')} 已生成，等待人工审核",
        data=outcome,
        run_id=outcome.get("run_id"),
        resource_uris=[f"manju://episodes/{args.episode_id}/delivery"],
    )


_DECISION_LABEL = {"approve": "批准", "approve_with_risk": "带风险批准", "reject": "拒绝"}


async def review(args: I.DeliveryReviewInput) -> CommandResult:
    from app.orchestration import api as orch_api

    if not (args.reason or "").strip():
        return failed("批准/拒绝交付必须填写审核意见", error_code="invalid_input")
    body = {
        "package_id": args.package_id,
        "decision": args.decision,
        "reason": args.reason,
        "accepted_risk": args.accepted_risk,
        "decided_by": "agent",
        "idempotency_key": args.idempotency_key,
        "request_id": args.request_id,
    }
    outcome = await call_guarded(orch_api.decide_delivery, args.episode_id, body=body)
    if isinstance(outcome, CommandResult):
        return outcome
    label = _DECISION_LABEL.get(args.decision, args.decision)
    return succeeded(
        f"交付包已{label}",
        data=outcome,
        run_id=outcome.get("run_id"),
        resource_uris=[f"manju://episodes/{args.episode_id}/delivery"],
    )


async def submit_feedback(args: I.DeliveryFeedbackInput) -> CommandResult:
    from app.orchestration import api as orch_api

    if not (args.feedback or "").strip():
        return failed("反馈内容不能为空", error_code="invalid_input")
    body = {
        "message": args.feedback,
        "created_by": "agent",
        "request_revision": args.request_revision,
    }
    outcome = await call_guarded(orch_api.create_customer_feedback, args.episode_id, body=body)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "客户反馈已记录" + ("，已创建修订 Run" if outcome.get("revision_run_id") else ""),
        data=outcome,
        run_id=outcome.get("revision_run_id"),
        resource_uris=[f"manju://episodes/{args.episode_id}/delivery"],
    )
