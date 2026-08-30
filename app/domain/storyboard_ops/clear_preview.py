"""分镜清空前的预览与不在途断言。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 clear_apply 与 resume_state。
"""
from __future__ import annotations

from app import task_registry
from app.db import get_conn
from app.domain.common import (
    _episode_or_404,
    router,
)
from fastapi import HTTPException

from .clear_apply import clear_storyboard_projection
from .resume_state import _storyboard_has_persisted_work


def _assert_storyboard_clear_not_running(episode_id: str, ep: dict) -> None:
    """Clearing is a stopped-state action; never use it as an implicit pause."""
    from app.storyboard_supervisor import load_latest_checkpoint

    active_run_id = ep.get("active_storyboard_run_id")
    active_run = (
        get_conn().execute(
            "SELECT status FROM workflow_runs WHERE id=?", (active_run_id,),
        ).fetchone()
        if active_run_id else None
    )
    checkpoint = load_latest_checkpoint(episode_id)
    stopped_phases = {
        "PAUSED_EXTERNAL", "PAUSED_BUDGET", "WAITING_HUMAN",
        "WAITING_AUTHORIZATION", "CANCELLED", "SUCCEEDED",
    }
    task_is_live = task_registry.active("storyboard", episode_id)
    run_is_live = bool(active_run and active_run["status"] in {"CREATED", "RUNNING"})
    episode_is_live = bool(
        ep.get("status") == "scripting"
        and (checkpoint is None or checkpoint.phase not in stopped_phases)
    )
    if task_is_live or run_is_live or episode_is_live:
        raise HTTPException(409, "分镜任务仍在运行，请先暂停任务，再清空分镜")

@router.post("/episodes/{episode_id}/storyboard/clear-preview")
def preview_storyboard_clear(episode_id: str):
    """Return the complete impact of resetting an episode's storyboard workspace."""
    from app.storyboard_workspace import create_preview, episode_fingerprint

    ep = _episode_or_404(episode_id)
    _assert_storyboard_clear_not_running(episode_id, dict(ep))
    # A task can stop before its first shot with either a zero-prefix checkpoint
    # or only a preflight failure projection. Both must remain clearable;
    # otherwise Resume and Clear reject the same empty workspace.
    stopped_preflight_failure = bool(
        ep["status"] == "script_failed" and str(ep["script_error"] or "").strip()
    )
    if (
        not _storyboard_has_persisted_work(episode_id, dict(ep))
        and not stopped_preflight_failure
    ):
        raise HTTPException(409, "当前没有可清空的分镜数据")
    conn = get_conn()
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
    ).fetchone()["c"])
    video_version_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
        (episode_id,),
    ).fetchone()["c"])
    reference_asset_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM reference_assets a
           JOIN reference_sets r ON r.id=a.reference_set_id
           JOIN shots s ON s.id=r.shot_id
           WHERE s.episode_id=? AND a.deleted=0""",
        (episode_id,),
    ).fetchone()["c"])
    workflow_run_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM workflow_runs
           WHERE workflow_type IN ('storyboard','video_completion')
             AND scope_type='episode' AND scope_id=?""",
        (episode_id,),
    ).fetchone()["c"])
    delivery_package_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM delivery_packages WHERE episode_id=?",
        (episode_id,),
    ).fetchone()["c"])
    payload = {
        "shot_count": shot_count,
        "video_version_count": video_version_count,
        "reference_asset_count": reference_asset_count,
        "workflow_run_count": workflow_run_count,
        "delivery_package_count": delivery_package_count,
        "active_task_will_stop": bool(
            ep["active_storyboard_run_id"] or ep["active_video_run_id"]
        ),
        "screenplay_preserved": True,
        "irreversible": True,
    }
    return create_preview(
        "storyboard_clear",
        episode_id,
        payload,
        baseline_fingerprint=episode_fingerprint(episode_id),
    )

@router.post("/episodes/{episode_id}/storyboard/clear")
async def apply_storyboard_clear(episode_id: str, body: dict):
    """Clear all storyboard/downstream state after an explicit current preview."""
    from app.storyboard_workspace import require_preview

    ep = _episode_or_404(episode_id)
    _assert_storyboard_clear_not_running(episode_id, dict(ep))
    require_preview(body.get("preview_token"), "storyboard_clear", episode_id)
    return await clear_storyboard_projection(episode_id)
