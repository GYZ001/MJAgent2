"""剧本删除（含孤儿收据清理与持久化运行取消）。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 activation 与 run_control。
"""
from __future__ import annotations

from app import (
    task_registry,
    worker,
)
from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _episode_or_404,
    router,
)
from app.orchestration.state_machine import StateConflict
from fastapi import HTTPException

from .activation import _abandon_orphaned_blueprint_receipts
from .run_control import _cancel_persisted_screenplay_run


@router.delete("/episodes/{episode_id}/screenplay")
async def delete_screenplay(episode_id: str):
    """Delete the current screenplay projection and invalidate every downstream pointer.

    Immutable artifacts/revisions remain as audit evidence.  The user-selected
    source dialogue requirements intentionally remain on the episode so the
    next Baseline can regenerate against the same explicit contract.
    """
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("screenplay.delete", {"episode_id": episode_id})
    if routed is not None:
        return routed
    episode = dict(_episode_or_404(episode_id))
    screenplay_run_id = episode.get("active_screenplay_run_id")

    cancelled = 0
    for kind in ("screenplay", "storyboard", "video_completion"):
        cancelled += int(await task_registry.cancel_and_wait(kind, episode_id))
    try:
        cancelled += int(_cancel_persisted_screenplay_run(
            episode_id,
            screenplay_run_id,
            message="用户删除剧本，终止持久化剧本运行",
        ))
    except StateConflict:
        raise HTTPException(409, "剧本运行状态已变化，请刷新后重试删除") from None

    conn = get_conn()
    expected_owner = str(screenplay_run_id or "")
    try:
        conn.execute("BEGIN IMMEDIATE")
        latest_owner = conn.execute(
            "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        actual_owner = str(
            latest_owner["active_screenplay_run_id"] or ""
        ) if latest_owner else "missing"
        if not latest_owner or actual_owner != expected_owner:
            raise StateConflict(
                "screenplay_owner",
                episode_id,
                {expected_owner},
                actual_owner,
            )
        shot_count = conn.execute(
            "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
        ).fetchone()["c"]
        worker.delete_episode_shots(episode_id, conn=conn, commit=False)
        stamp = now()

        # Revisions and grants are historical audit records; revoke/supersede
        # them instead of deleting them.
        conn.execute(
            "UPDATE production_revisions SET status='superseded', updated_at=? "
            "WHERE episode_id=? AND status='active'",
            (stamp, episode_id),
        )
        conn.execute(
            "UPDATE production_grants SET revoked_at=COALESCE(revoked_at, ?) WHERE episode_id=?",
            (stamp, episode_id),
        )
        conn.execute(
            "UPDATE completion_grants SET revoked_at=COALESCE(revoked_at, ?) WHERE episode_id=?",
            (stamp, episode_id),
        )
        conn.execute(
            "UPDATE delivery_packages SET status='superseded' "
            "WHERE episode_id=? AND status NOT IN ('rejected','superseded')",
            (episode_id,),
        )
        cursor = conn.execute(
            """UPDATE episodes SET
            screenplay_json=NULL,
            screenplay_character_resolutions='[]',
            screenplay_required_dialogues='[]',
            screenplay_required_dialogue_occurrences='[]',
            screenplay_status='pending',
            screenplay_error=NULL,
            screenplay_started_at=NULL,
            screenplay_updated_at=?,
            screenplay_artifact_id=NULL,
            active_screenplay_run_id=NULL,
            working_screenplay_artifact_id=NULL,
            published_screenplay_artifact_id=NULL,
            screenplay_production_revision_id=NULL,
            screenplay_completion_certificate_id=NULL,
            storyboard_outline_json=NULL,
            storyboard_artifact_id=NULL,
            storyboard_warning=NULL,
            active_storyboard_run_id=NULL,
            working_storyboard_artifact_id=NULL,
            published_storyboard_artifact_id=NULL,
            storyboard_production_revision_id=NULL,
            storyboard_completion_certificate_id=NULL,
            active_video_run_id=NULL,
            video_control_json=NULL,
            delivery_artifact_id=NULL,
            delivery_status='not_ready',
            status='planned',
            script_error=NULL
        WHERE id=? AND COALESCE(active_screenplay_run_id, '')=?""",
            (stamp, episode_id, expected_owner),
        )
        if cursor.rowcount != 1:
            raise StateConflict(
                "screenplay_owner",
                episode_id,
                {expected_owner},
                "changed_during_delete",
            )
        # The revision a blueprint retry grant must bind to is superseded
        # above, so an unknown provider outcome left over from the deleted
        # production would demand a grant that can no longer be issued and
        # every later Baseline would fail activation with
        # BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED.
        _abandon_orphaned_blueprint_receipts(episode_id, conn=conn)
        from app.storyboard_authority import (
            clear_storyboard_outline_authority,
        )

        clear_storyboard_outline_authority(
            episode_id,
            conn=conn,
        )
        conn.commit()
    except StateConflict:
        if conn.in_transaction:
            conn.rollback()
        raise HTTPException(409, "剧本已被新的运行接管，未执行删除") from None
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {
        "deleted": episode_id,
        "downstream_shots_cleared": int(shot_count or 0),
        "cancelled_tasks": cancelled,
    }
