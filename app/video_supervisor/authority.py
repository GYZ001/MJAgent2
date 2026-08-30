"""付费边界前的授权/视频计划校验（VideoCompletionGrant 与 episode_video_plan 绑定）。"""
from __future__ import annotations

import asyncio

from typing import Any

from app.completion_grant import (
    GrantValidationError,
    VideoCompletionGrant,
    bind_video_grant_generation_plan,
    validate_video_grant,
)
from app.db import get_conn
from app.evidence import repository as evidence_repository

from .models import VideoSupervisorCheckpoint



def _verify_supervisor_paid_authority(
    cp: VideoSupervisorCheckpoint,
    *,
    stage: str,
) -> VideoCompletionGrant | None:
    """One fail-closed verifier shared by every Supervisor paid boundary."""
    del stage  # retained in the signature for boundary-specific audit callers
    conn = get_conn()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (cp.episode_id,),
    ).fetchone()
    if episode is None:
        raise GrantValidationError("GRANT_SCOPE_MISSING", "视频补齐分集已不存在")
    current_storyboard_id = str(episode["storyboard_artifact_id"] or "")
    if not cp.grant_id:
        # Explicit compatibility boundary for historical plan-null episodes.
        # Any durable screenplay authority makes an unauthorised paid run a
        # hard failure even if the mutable projection was stripped.
        from app.production.screenplay_authority import (
            episode_requires_immutable_screenplay_authority,
            resolve_downstream_screenplay,
        )

        immutable_required = episode_requires_immutable_screenplay_authority(
            episode,
            conn=conn,
        )
        try:
            context = resolve_downstream_screenplay(cp.episode_id, conn=conn)
        except ValueError as exc:
            if immutable_required:
                raise GrantValidationError(
                    "RELEASE_QUALIFICATION_INVALID",
                    f"当前剧本发布权威无法解析：{exc}",
                ) from exc
            context = None
        if immutable_required or (
            context is not None and context.immutable_authority_required
        ):
            raise GrantValidationError(
                "VIDEO_COMPLETION_GRANT_REQUIRED",
                "带耐久发布权威的分集在付费阶段前必须有补齐授权",
            )
        return None
    grant = validate_video_grant(
        cp.grant_id,
        episode_id=cp.episode_id,
        storyboard_artifact_id=current_storyboard_id,
    )
    plan_binding = dict(grant.release_qualification.get("generation_plan") or {})
    if plan_binding.get("applicable"):
        expected = {
            "episode_video_plan_id": grant.episode_video_plan_id,
            "episode_video_plan_revision": grant.episode_video_plan_revision,
            "video_plan_release_hash": grant.video_plan_release_hash,
            "capability_snapshot_id": grant.capability_snapshot_id,
        }
        actual = {
            "episode_video_plan_id": cp.episode_video_plan_id,
            "episode_video_plan_revision": cp.episode_video_plan_revision,
            "video_plan_release_hash": cp.video_plan_release_hash,
            "capability_snapshot_id": cp.capability_snapshot_id,
        }
        if actual != expected:
            raise GrantValidationError(
                "CHECKPOINT_PLAN_BINDING_CHANGED",
                "Supervisor checkpoint 与授权的当前视频计划不一致",
            )
    return grant


def _supervisor_checks_can_use_worker_thread() -> bool:
    """A private in-memory SQLite connection must stay on its owner thread."""
    try:
        database_rows = get_conn().execute("PRAGMA database_list").fetchall()
        return any(str(row[2] or "").strip() for row in database_rows)
    except Exception:
        return False


async def _verify_supervisor_paid_authority_async(
    cp: VideoSupervisorCheckpoint,
    *,
    stage: str,
) -> VideoCompletionGrant | None:
    """Keep modern grant verification off-loop; legacy plan-null stays local."""
    if not cp.grant_id:
        return _verify_supervisor_paid_authority(cp, stage=stage)
    if not _supervisor_checks_can_use_worker_thread():
        return _verify_supervisor_paid_authority(cp, stage=stage)
    return await asyncio.to_thread(
        _verify_supervisor_paid_authority,
        cp,
        stage=stage,
    )


async def _verify_episode_plan_current_async(plan) -> bool:
    from app.video_plan import verify_episode_plan_is_current

    if not _supervisor_checks_can_use_worker_thread():
        return verify_episode_plan_is_current(
            plan,
            conn=get_conn(),
            mark_stale=False,
        )
    return await asyncio.to_thread(
        verify_episode_plan_is_current,
        plan,
        mark_stale=False,
    )


async def _ensure_supervisor_video_plan(
    cp: VideoSupervisorCheckpoint,
) -> VideoCompletionGrant | None:
    """Generate/validate and bind the plan before any paid asset or video call."""
    checkpoint_binding = (
        cp.episode_video_plan_id,
        cp.episode_video_plan_revision,
        cp.video_plan_release_hash,
        cp.capability_snapshot_id,
    )
    if cp.grant_id and not any(value is not None for value in checkpoint_binding):
        # New and pre-migration checkpoints acquire their binding exactly once
        # from the content-addressed grant before paid work begins.
        if _supervisor_checks_can_use_worker_thread():
            grant = await asyncio.to_thread(
                validate_video_grant,
                cp.grant_id,
                episode_id=cp.episode_id,
                storyboard_artifact_id=cp.storyboard_artifact_id,
            )
        else:
            grant = validate_video_grant(
                cp.grant_id,
                episode_id=cp.episode_id,
                storyboard_artifact_id=cp.storyboard_artifact_id,
            )
    else:
        grant = await _verify_supervisor_paid_authority_async(
            cp,
            stage="video_plan_preflight",
        )
    if grant is None:
        return None
    from app.video_plan import (
        VideoPlanValidationError,
        generate_episode_plan,
        load_latest_plan,
        video_plan_provider_selection_is_current,
    )

    conn = get_conn()
    plan = load_latest_plan(cp.episode_id, conn=conn)
    if (
        plan is None
        or plan.status != "valid"
        or not await _verify_episode_plan_current_async(plan)
        or not video_plan_provider_selection_is_current(plan, conn=conn)
    ):
        # Planning may itself call an external model, so recheck the release
        # immediately before it.  A pending plan slot is the only permitted
        # mutation of the grant after this point.
        await _verify_supervisor_paid_authority_async(
            cp,
            stage="video_plan_generation",
        )
        try:
            plan = await generate_episode_plan(
                cp.episode_id,
                force=plan is not None,
                conn=conn,
            )
        except (ValueError, VideoPlanValidationError) as exc:
            raise GrantValidationError("VIDEO_PLAN_INVALID", str(exc)) from exc
    if (
        plan.status != "valid"
        or not await _verify_episode_plan_current_async(plan)
        or not video_plan_provider_selection_is_current(plan, conn=conn)
    ):
        raise GrantValidationError(
            "VIDEO_PLAN_INVALID",
            "Supervisor 启动前未取得当前有效的整集视频计划",
        )
    if not grant.episode_video_plan_id:
        grant = bind_video_grant_generation_plan(
            grant.grant_id,
            episode_id=cp.episode_id,
            storyboard_artifact_id=cp.storyboard_artifact_id,
        )
    if (
        grant.episode_video_plan_id != plan.episode_video_plan_id
        or grant.episode_video_plan_revision != int(plan.plan_revision)
        or grant.video_plan_release_hash != plan.release_qualification_hash
        or grant.capability_snapshot_id != plan.capability_snapshot_id
    ):
        raise GrantValidationError(
            "VIDEO_PLAN_BINDING_CHANGED",
            "当前有效视频计划与用户授权的计划不一致",
        )
    checkpoint_binding = (
        cp.episode_video_plan_id,
        cp.episode_video_plan_revision,
        cp.video_plan_release_hash,
        cp.capability_snapshot_id,
    )
    if any(value is not None for value in checkpoint_binding) and checkpoint_binding != (
        grant.episode_video_plan_id,
        grant.episode_video_plan_revision,
        grant.video_plan_release_hash,
        grant.capability_snapshot_id,
    ):
        raise GrantValidationError(
            "CHECKPOINT_PLAN_BINDING_CHANGED",
            "Supervisor checkpoint 已绑定另一个视频计划 revision",
        )
    cp.episode_video_plan_id = grant.episode_video_plan_id
    cp.episode_video_plan_revision = grant.episode_video_plan_revision
    cp.video_plan_release_hash = grant.video_plan_release_hash
    cp.capability_snapshot_id = grant.capability_snapshot_id
    return await _verify_supervisor_paid_authority_async(
        cp,
        stage="video_plan_bound",
    )


def _record_grant_validation_failure(
    cp: VideoSupervisorCheckpoint,
    exc: GrantValidationError,
    *,
    run_id: str | None,
    stage: str,
) -> None:
    """Persist the full detail behind a ``GrantValidationError`` before it is
    reduced to a bare ``cp.outcome = exc.code``.

    Every Supervisor boundary that catches ``GrantValidationError`` used to
    throw away ``str(exc)`` entirely -- for ``VIDEO_PLAN_INVALID`` that string
    is ``VideoPlanValidationError.issues`` (the full per-shot rejection
    reasons from ``app.video_plan``), wrapped via ``raise
    GrantValidationError(...) from exc``. The result was exactly the EP8
    incident in docs/delivery_pipeline_rca_2026-08-29.md 问题二: one orphaned
    ``RUN_PARTIAL :: VIDEO_PLAN_INVALID`` event with an empty payload and
    nothing in ``error_logs`` -- a legitimate rejection with no way to RCA it
    after the fact. All catch sites route through this one function so none
    of them can regress back to silently dropping the detail.

    Ordering follows the project rule that a rollback must be an exception
    handler's first statement, ahead of any logging/recorder call: the nested
    call that raised ``exc`` shares the ambient task connection with this
    caller and may have left it mid-transaction, so that connection is rolled
    back before anything here can commit on it.

    ``error_logs`` goes through ``app.errors.log_error`` ->
    ``app.db.insert_error_log``, which opens its own independent connection
    and commits there (see that function's docstring) -- it never touches the
    ambient connection. The ``run_events`` append below intentionally *does*
    use the ambient connection via ``evidence_repository.append_event`` --
    the same pattern every other event append in this function already uses
    (e.g. ``VIDEO_SUPERVISOR_STARTED``), safe now that any stale state on it
    has been rolled back above. Both writes swallow their own failures: a
    diagnostics write must never mask or interrupt the original authorization
    failure it is trying to explain.
    """
    conn = get_conn()
    if conn.in_transaction:
        conn.rollback()

    issues: list[dict[str, Any]] | None = None
    cause = exc.__cause__
    if cause is not None and type(cause).__name__ == "VideoPlanValidationError":
        issues = getattr(cause, "issues", None) or None

    message = str(exc)
    rid = run_id or cp.run_id
    detail: dict[str, Any] = {"stage": stage, "code": exc.code, "message": message[:4000]}
    if issues:
        detail["issues"] = issues

    error_id: str | None = None
    try:
        from app.errors import log_error

        record = log_error(
            exc,
            action=f"video_supervisor.{stage}",
            context={
                "episode_id": cp.episode_id,
                "run_id": rid,
                "grant_id": cp.grant_id,
                "stage": stage,
            },
            message=message,
            meta={"issues": issues} if issues else None,
        )
        error_id = record.error_id
    except Exception:  # noqa: BLE001 - 诊断落库失败绝不能掩盖原始授权失败
        pass
    if error_id:
        detail["error_id"] = error_id

    if rid:
        try:
            evidence_repository.append_event(
                rid, "RUN_PARTIAL", "warning",
                f"{exc.code}: {message[:500]}",
                payload=detail,
            )
        except Exception:  # noqa: BLE001 - 同上，事件落库失败不得掩盖原始失败
            pass
