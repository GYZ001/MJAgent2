"""剧本生成任务的运行态判定、持久化运行取消、所有者断言、命令总线重试授权与目标时长/运行态失败投影。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 status_snapshot。

2026-08-30：``_assert_screenplay_run_owner`` 本体搬到同包 ``.run_owner``
（层号治理，见该文件 docstring）——本文件转发保持原名可从 ``.run_control``/
``app.domain.screenplay_ops``/``app.domain`` 原样导入，不影响既有调用点
（``guarded.py``/``task_body.py`` 等仍从本文件取这个符号）。
"""
from __future__ import annotations

from app import (
    screenplay_retry_authority as _retry_authority,
    config,
    task_registry,
)
from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _as_body_dict,
    _episode_or_404,
    router,
)
from app.evidence import repository as evidence_repository
from app.orchestration.engine import WorkflowRecorder
from app.orchestration.state_machine import StateConflict
from fastapi import (
    Body,
    HTTPException,
)
from typing import Any

from .run_owner import _assert_screenplay_run_owner as _assert_screenplay_run_owner
from .status_snapshot import (
    _clear_unpublished_screenplay_ir,
    _screenplay_production_state,
)


_SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL = (
    _retry_authority.SCREENPLAY_COMMAND_BUS_RETRY_APPROVAL
)

def _enter_screenplay_command_bus_retry_approval(evidence: dict[str, Any]):
    return _retry_authority.enter_screenplay_command_bus_retry_approval(
        evidence
    )

def _exit_screenplay_command_bus_retry_approval(token) -> None:
    _retry_authority.exit_screenplay_command_bus_retry_approval(token)

def _screenplay_task_active(episode_id: str) -> bool:
    if task_registry.active("screenplay", episode_id):
        return True
    conn = get_conn()
    episode = conn.execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    return bool(
        episode
        and evidence_repository.get_active_scoped_run(
            episode["active_screenplay_run_id"],
            workflow_type="screenplay",
            scope_type="episode",
            scope_id=episode_id,
            conn=conn,
        )
    )

def _cancel_persisted_screenplay_run(
    episode_id: str,
    run_id: str | None,
    *,
    message: str,
) -> bool:
    run = evidence_repository.get_active_scoped_run(
        run_id,
        workflow_type="screenplay",
        scope_type="episode",
        scope_id=episode_id,
    )
    if not run:
        return False
    try:
        WorkflowRecorder(str(run["id"])).cancel(message, conn=None)
        return True
    except StateConflict:
        latest = evidence_repository.get_run(str(run["id"]))
        if latest and latest.get("status") in {
            "CANCELLED", "FAILED", "SUCCEEDED", "PARTIAL",
        }:
            return False
        raise

@router.put("/episodes/{episode_id}/target-duration")
def update_episode_target_duration(
    episode_id: str, body: dict | None = Body(None)
):
    """修改首版剧本生成前的整集节奏预算。"""
    body = _as_body_dict(body)
    raw_target = body.get("target_duration_s")
    if isinstance(raw_target, bool):
        raw_target = None
    try:
        numeric_target = float(raw_target)
        target = int(numeric_target) if numeric_target.is_integer() else -1
    except (TypeError, ValueError, OverflowError):
        target = -1
    suggested = list(config.EPISODE_TARGET_CHOICES)
    if (
        target < config.EPISODE_TARGET_MIN_S
        or target % config.EPISODE_TARGET_STEP_S != 0
    ):
        raise HTTPException(422, {
            "code": "invalid_episode_target_duration",
            "message": (
                f"目标时长至少为 {config.EPISODE_TARGET_MIN_S} 秒，"
                f"按 {config.EPISODE_TARGET_STEP_S} 秒递增；不设上限"
            ),
            "minimum_s": config.EPISODE_TARGET_MIN_S,
            "step_s": config.EPISODE_TARGET_STEP_S,
            "suggested_choices": suggested,
        })

    ep = dict(_episode_or_404(episode_id))
    current = int(ep.get("target_duration_s") or config.EPISODE_TARGET_DEFAULT_S)
    if target == current:
        return {
            "saved": True,
            "unchanged": True,
            "episode_id": episode_id,
            "target_duration_s": current,
            "suggested_choices": suggested,
            "constraint_version": int(ep.get("screenplay_constraint_version") or 0),
        }

    production = _screenplay_production_state(episode_id)
    active_runs = [
        kind for kind in ("screenplay", "storyboard", "video_completion")
        if task_registry.active(kind, episode_id)
    ]
    if active_runs or production.get("task_active"):
        raise HTTPException(409, {
            "code": "episode_target_duration_locked",
            "message": "本集正在制作中，不能同时修改目标时长；请等待任务结束后重试",
            "active_runs": active_runs,
        })
    if (
        production.get("can_resume_repair")
        or production.get("can_resume_baseline")
    ):
        raise HTTPException(409, {
            "code": "episode_target_duration_locked",
            "message": "本集已有可恢复的剧本生产状态，目标时长已被该约束版本锁定",
        })

    conn = get_conn()
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"])
    has_screenplay = bool(ep.get("screenplay_json") or ep.get("screenplay_artifact_id"))
    downstream_status = ep.get("status") not in {"planned", "drafting"}
    locked_status = ep.get("screenplay_status") not in {"pending", "failed"}
    if has_screenplay or shot_count or downstream_status or locked_status or ep.get("screenplay_publish_fence"):
        raise HTTPException(409, {
            "code": "episode_target_duration_locked",
            "message": "本集已有剧本或下游产物；为避免版本不一致，不能直接修改目标时长",
            "screenplay_status": ep.get("screenplay_status"),
            "storyboard_status": ep.get("status"),
            "shot_count": shot_count,
        })

    cursor = conn.execute(
        "UPDATE episodes SET target_duration_s=?, "
        "planning_target_duration_s=?, "
        "planning_duration_source='episode_target_duration_editor', "
        "target_duration_authority='planning_estimate', "
        "screenplay_constraint_version=screenplay_constraint_version+1, "
        "screenplay_snapshot_version=screenplay_snapshot_version+1 "
        "WHERE id=? AND screenplay_publish_fence=0 "
        "AND screenplay_status IN ('pending','failed') "
        "AND status IN ('planned','drafting') "
        "AND COALESCE(screenplay_json,'')='' "
        "AND COALESCE(screenplay_artifact_id,'')='' "
        "AND NOT EXISTS(SELECT 1 FROM shots WHERE episode_id=?)",
        (target, target, episode_id, episode_id),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise HTTPException(409, {
            "code": "episode_target_duration_conflict",
            "message": "本集状态刚刚发生变化，目标时长未修改；请刷新后重试",
        })
    saved = dict(_episode_or_404(episode_id))
    return {
        "saved": True,
        "unchanged": False,
        "episode_id": episode_id,
        "previous_target_duration_s": current,
        "target_duration_s": target,
        "suggested_choices": suggested,
        "constraint_version": int(saved.get("screenplay_constraint_version") or 0),
        "snapshot_version": int(saved.get("screenplay_snapshot_version") or 0),
    }

def _project_screenplay_runtime_failure(
    episode_id: str,
    *,
    run_id: str | None,
    public_error: str,
) -> bool:
    """Preserve durable baseline evidence and project an actionable UI state."""
    conn = get_conn()
    production_state = _screenplay_production_state(episode_id)
    has_recovery_point = bool(
        production_state.get("has_working_baseline")
        or production_state.get("has_resumable_baseline")
    )
    if not has_recovery_point:
        _clear_unpublished_screenplay_ir(
            episode_id,
            run_id=run_id,
        )
    conn.execute(
        "UPDATE episodes SET screenplay_status=?, screenplay_error=?, "
        "screenplay_updated_at=? WHERE id=?",
        (
            "repairing" if has_recovery_point else "failed",
            (
                "剧本流程在后续阶段暂停；已验证产物和安全恢复点已保留，"
                f"可继续流程。{public_error}"
                if has_recovery_point else public_error
            ),
            now(),
            episode_id,
        ),
    )
    conn.commit()
    return has_recovery_point

def _screenplay_fallback_status(ep) -> str:
    data = dict(ep)
    if not ep["screenplay_json"]:
        return "repairing" if data.get("working_screenplay_artifact_id") else "pending"
    artifact_id = ep["screenplay_artifact_id"] if "screenplay_artifact_id" in ep.keys() else None
    if not artifact_id:
        return "ready"
    artifact = evidence_repository.get_artifact(artifact_id)
    if artifact and artifact["status"] == "approved":
        return "ready"
    return "repairing" if data.get("working_screenplay_artifact_id") else "failed"
