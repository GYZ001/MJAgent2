"""Assert that a shot is currently authorized to submit a provider video
generation job, degrading mode or raising a fail-closed error otherwise.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale). This file
holds exactly one function -- ``assert_video_provider_submission_authority``
is a single ~185-line function in the pre-split source; splitting it further
would change its control flow, so it is moved whole.
"""
from __future__ import annotations

from typing import Any

from app.db import get_conn

from .capability_snapshot import (
    capability_allows,
    capability_snapshot_by_id,
    current_capability_snapshot,
)
from .mode_attempt import active_plan_is_current
from .models import ProviderVideoCapabilitySnapshot, ShotVideoGenerationPlan, VideoGenerationMode
from .primitives import VideoPlanValidationError
from .publish import load_latest_plan
from .staleness import _mark_episode_video_plan_stale, verify_episode_plan_is_current


def assert_video_provider_submission_authority(
    *,
    shot_id: str,
    shot_plan_id: str,
    actual_mode: VideoGenerationMode | str,
    expected_capability_snapshot_id: str | None = None,
    conn=None,
) -> tuple[ShotVideoGenerationPlan, ProviderVideoCapabilitySnapshot]:
    """Authorize the last reversible boundary before a paid video submission.

    The check is intentionally mode-agnostic.  It resolves the current episode
    plan and then validates its complete storyboard release manifest and every
    canonical shot contract fingerprint.  It also compares the selected shot
    against the newest capability observation for the provider/model that the
    video client would use *now*.  Provider/model switches, failed probes, and
    capability withdrawals therefore stale the whole plan instead of letting a
    worker submit from an old positive snapshot.
    """
    from app import hiagent

    db = conn or get_conn()
    shot = db.execute(
        """SELECT s.episode_id AS episode_id, e.target_video_model AS target_video_model
             FROM shots s JOIN episodes e ON e.id=s.episode_id
            WHERE s.id=?""",
        (shot_id,),
    ).fetchone()
    issues: list[dict[str, Any]] = []
    # Episode/generation binding is mode-agnostic and independent of the plan:
    # the operator selects a video model per episode on the storyboard page,
    # and every provider submission must be re-checked against it here, at the
    # last reversible boundary, because the globally active provider (Model
    # Center) can drift after enqueue while a job sits queued.  A mismatch is
    # a caller bug (stale binding, or someone flipped the global provider
    # underneath a bound episode) and must be rejected, never silently
    # rewritten — the two providers' prompt dialects are incompatible.
    active_provider = hiagent.active_provider("video")
    if shot is not None:
        from app import video_providers

        episode_bound_provider = str(shot["target_video_model"] or "").strip() or "hiagent"
        # 按适配器族比较，不按 provider key 原始字符串比：自建实例（custom:xxx）
        # 复用内置协议实现，字符串比较会把"同协议、不同连接"误判成绑定不一致
        # （本机部署的历史模型迁移已经把内嵌 Seedance/MiniMax H3 包装成了
        # custom:<id>，字符串比较在这台机器上会 100% 误判，见
        # video_providers.same_family）。下面的能力快照解析仍使用未归一化的
        # 原始 active_provider，保持与改动前完全一致的行为。
        if not video_providers.same_family(episode_bound_provider, active_provider):
            issues.append({
                "code": "VIDEO_SUBMISSION_EPISODE_MODEL_BINDING_MISMATCH",
                "shot_id": shot_id,
                "episode_bound_provider": episode_bound_provider,
                "active_provider": active_provider,
            })
    plan = load_latest_plan(str(shot["episode_id"]), conn=db) if shot else None
    if shot is None:
        issues.append({"code": "VIDEO_SUBMISSION_SHOT_MISSING", "shot_id": shot_id})
    elif plan is None:
        issues.append({
            "code": "VIDEO_SUBMISSION_PLAN_MISSING",
            "shot_id": shot_id,
            "shot_plan_id": shot_plan_id,
        })
    elif not verify_episode_plan_is_current(plan, conn=db):
        issues.append({
            "code": "VIDEO_SUBMISSION_PLAN_STALE",
            "episode_video_plan_id": plan.episode_video_plan_id,
            "shot_plan_id": shot_plan_id,
        })

    selected = (
        next((item for item in plan.shots if item.shot_id == shot_id), None)
        if plan is not None
        else None
    )
    if selected is None:
        issues.append({
            "code": "VIDEO_SUBMISSION_SHOT_PLAN_MISSING",
            "shot_id": shot_id,
            "shot_plan_id": shot_plan_id,
        })
    elif selected.shot_plan_id != shot_plan_id:
        if not active_plan_is_current(shot_plan_id, conn=db):
            issues.append({
                "code": "VIDEO_SUBMISSION_SHOT_PLAN_STALE",
                "shot_id": shot_id,
                "stored": shot_plan_id,
                "current": selected.shot_plan_id,
            })

    try:
        submitted_mode = VideoGenerationMode(actual_mode)
    except ValueError:
        submitted_mode = None
        issues.append({
            "code": "VIDEO_SUBMISSION_MODE_INVALID",
            "shot_id": shot_id,
            "actual_mode": str(actual_mode),
        })

    latest_snapshot: ProviderVideoCapabilitySnapshot | None = None
    if selected is not None:
        if (
            expected_capability_snapshot_id
            and expected_capability_snapshot_id != selected.capability_snapshot_id
        ):
            issues.append({
                "code": "VIDEO_SUBMISSION_CAPABILITY_BINDING_STALE",
                "shot_id": shot_id,
                "stored": expected_capability_snapshot_id,
                "current": selected.capability_snapshot_id,
            })
        if submitted_mode is not None and submitted_mode != selected.mode:
            issues.append({
                "code": "VIDEO_SUBMISSION_MODE_PLAN_MISMATCH",
                "shot_id": shot_id,
                "planned_mode": selected.mode.value,
                "actual_mode": submitted_mode.value,
            })

        bound_snapshot = capability_snapshot_by_id(
            selected.capability_snapshot_id,
            conn=db,
        )
        active_model = hiagent.active_model("video", active_provider)
        latest_snapshot = current_capability_snapshot(
            provider=active_provider,
            model=active_model,
            conn=db,
        )
        if bound_snapshot is None:
            issues.append({
                "code": "VIDEO_SUBMISSION_CAPABILITY_SNAPSHOT_MISSING",
                "shot_id": shot_id,
                "snapshot_id": selected.capability_snapshot_id,
            })
        elif (
            bound_snapshot.provider != latest_snapshot.provider
            or bound_snapshot.model != latest_snapshot.model
        ):
            issues.append({
                "code": "VIDEO_SUBMISSION_PROVIDER_SELECTION_STALE",
                "shot_id": shot_id,
                "planned_provider": bound_snapshot.provider,
                "planned_model": bound_snapshot.model,
                "active_provider": latest_snapshot.provider,
                "active_model": latest_snapshot.model,
            })
        if not latest_snapshot.technical_success:
            issues.append({
                "code": "VIDEO_SUBMISSION_LATEST_PROBE_FAILED",
                "shot_id": shot_id,
                "snapshot_id": latest_snapshot.id,
                "probe_result": latest_snapshot.probe_result,
            })
        if submitted_mode is not None and not capability_allows(
            latest_snapshot,
            submitted_mode,
            selected.video_input_intent,
        ):
            issues.append({
                "code": "VIDEO_SUBMISSION_CAPABILITY_WITHDRAWN",
                "shot_id": shot_id,
                "snapshot_id": latest_snapshot.id,
                "mode": submitted_mode.value,
                "intent": (
                    selected.video_input_intent.value
                    if selected.video_input_intent
                    else None
                ),
            })

    if issues:
        if plan is not None:
            _mark_episode_video_plan_stale(plan, conn=db)
            # This assertion is a terminal paid-work fence.  When it rejects,
            # there is no successful caller transaction to commit later.  A
            # worker-thread connection would otherwise retain the stale-plan
            # UPDATE and hold SQLite's single writer lock indefinitely.
            db.commit()
        raise VideoPlanValidationError(issues)
    assert selected is not None and latest_snapshot is not None
    return selected, latest_snapshot
