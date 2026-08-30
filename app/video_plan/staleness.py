"""Revalidate whether a published episode video plan is still current, and
mark it (and its shot projections) stale when it is not.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale).
"""
from __future__ import annotations

from app.db import get_conn, now

from .capability_snapshot import capability_snapshot_by_id
from .models import EpisodeVideoGenerationPlan
from .primitives import VideoPlanValidationError
from .release_manifest import current_storyboard_release_manifest
from .validate import validate_episode_plan


def verify_episode_plan_is_current(
    plan: EpisodeVideoGenerationPlan,
    *,
    conn=None,
    mark_stale: bool = True,
) -> bool:
    """Revalidate the immutable release and every canonical shot fingerprint."""
    db = conn or get_conn()
    if plan.status != "valid":
        return False
    rows = db.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (plan.episode_id,),
    ).fetchall()
    snapshot = capability_snapshot_by_id(plan.capability_snapshot_id, conn=db)
    try:
        if snapshot is None:
            raise VideoPlanValidationError([{"code": "CAPABILITY_SNAPSHOT_MISSING"}])
        manifest = current_storyboard_release_manifest(plan.episode_id, conn=db)
        validate_episode_plan(
            plan,
            list(rows),
            snapshot,
            release_manifest=manifest,
        )
    except (ValueError, VideoPlanValidationError):
        if mark_stale:
            _mark_episode_video_plan_stale(plan, conn=db)
            if conn is None:
                db.commit()
        return False
    return True


def _mark_episode_video_plan_stale(
    plan: EpisodeVideoGenerationPlan,
    *,
    conn,
) -> None:
    """Persist one fail-closed state for every projection of an episode plan."""
    conn.execute(
        "UPDATE episode_video_generation_plans SET status='stale' WHERE id=?",
        (plan.episode_video_plan_id,),
    )
    conn.execute(
        "UPDATE shot_video_generation_plans SET status='stale',updated_at=? "
        "WHERE episode_video_plan_id=?",
        (now(), plan.episode_video_plan_id),
    )
