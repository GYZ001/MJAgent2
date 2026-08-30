"""视频/镜头/参考图产物的清空与视频完成态重置。

从 app/domain/video_ops.py 按原样搬移；依赖 completion_core。
"""
from __future__ import annotations

from app import (
    worker,
)
from app.db import get_conn
from app.domain.common import (
    _episode_or_404,
    router,
)
from app.domain.review_wall import (
    _review_upstream_snapshot,
    _review_write_audit,
)
from fastapi import HTTPException

from .completion_core import _ensure_video_episode_columns


def _require_provider_clearance(
    conn,
    *,
    episode_id: str | None = None,
    shot_ids: list[str] | tuple[str, ...] = (),
    version_ids: list[str] | tuple[str, ...] = (),
) -> None:
    from app.completion_grant import (
        ProviderTasksNotTerminalError,
        assert_provider_tasks_clearable,
    )

    try:
        assert_provider_tasks_clearable(
            episode_id=episode_id,
            shot_ids=shot_ids,
            version_ids=version_ids,
            conn=conn,
        )
    except ProviderTasksNotTerminalError as exc:
        raise HTTPException(409, exc.detail) from exc

@router.post("/episodes/{episode_id}/provider-tasks/reconcile")
async def reconcile_episode_provider_tasks(episode_id: str):
    """给「清空视频提示词」等操作撞上 409 PROVIDER_TASKS_NOT_TERMINAL 的用户一个
    真实可用的恢复入口：核对本集每一个未终态供应商任务的真实状态，而不是让用户
    对着一句「请核对供应商创建结果」却无处可核对。

    做且只做两件诚实的事，都不新建供应商任务、不下载或采用任何结果、不放宽
    任何闸门：

    1. 对每个仍有付费任务嫌疑的阻塞项，实际去查一次供应商——只有供应商自己
       回答「已成功」或「已失败」才结算对应的费用责任并把任务落定；供应商仍
       在跑或查不到，原样保留为阻塞项。
    2. 对本地证据已经证明「从未提交给供应商、因而不可能产生费用」且所属镜头
       已经采用了别的成功版本的孤儿任务，按既有的「过时任务」收口惯例
       （``app/video_plan.py::reconcile_adopted_revision``）关闭，不产生任何
       新费用。

    返回核对后的最新阻塞快照；调用方（前端）据此判断清空操作现在能不能重试。
    """
    from app.completion_grant import (
        close_superseded_unclaimed_video_jobs,
        provider_task_clearance_snapshot,
        reconcile_provider_tasks_for_clear,
    )

    _episode_or_404(episode_id)
    conn = get_conn()
    before = provider_task_clearance_snapshot(episode_id=episode_id, conn=conn)
    provider_reconciliation = await reconcile_provider_tasks_for_clear(
        episode_id=episode_id,
        conn=conn,
        evidence_source="episode_provider_tasks_reconcile",
    )
    superseded_closed = close_superseded_unclaimed_video_jobs(episode_id, conn=conn)
    clearance = provider_task_clearance_snapshot(episode_id=episode_id, conn=conn)
    result = {
        "episode_id": episode_id,
        "blockers_before": len(before["blockers"]),
        "provider_confirmed_terminal_job_ids": provider_reconciliation["reconciled_job_ids"],
        "superseded_jobs_closed_job_ids": superseded_closed,
        "clearance": clearance,
    }
    _review_write_audit(
        "video.provider_tasks_reconcile", "episode", episode_id, new_state=result,
    )
    return result

@router.post("/episodes/{episode_id}/clear-artifacts")
async def clear_episode_artifacts(episode_id: str):
    """清空整集所有镜头的参考图、视频与模型分析，并回退到「已确认」。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    snapshot = _review_upstream_snapshot(episode_id)
    if snapshot["active_upstream_runs"]:
        raise HTTPException(409, {
            "code": "UPSTREAM_RUN_ACTIVE",
            "message": "编剧或分镜任务仍在写入，不能普通清空；请先停止上游任务",
            "active_runs": snapshot["active_upstream_runs"],
        })
    _require_provider_clearance(get_conn(), episode_id=episode_id)
    await reset_video_completion_state(episode_id, reason="CLEARED")
    worker.pause_episode_video_tasks(episode_id)
    try:
        result = worker.clear_episode_artifacts(episode_id)
    except ValueError as exc:
        raise HTTPException(409, getattr(exc, "detail", str(exc))) from exc
    _review_write_audit("artifacts.clear_episode", "episode", episode_id, new_state=result)
    return result

@router.post("/episodes/{episode_id}/videos/clear")
async def clear_episode_videos(episode_id: str):
    """Clear all shot videos in the episode while preserving reference images."""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_episode_videos", {"episode_id": episode_id})
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    snapshot = _review_upstream_snapshot(episode_id)
    if snapshot["active_upstream_runs"]:
        raise HTTPException(409, {
            "code": "UPSTREAM_RUN_ACTIVE",
            "message": "编剧或分镜任务仍在写入，不能清空视频",
            "active_runs": snapshot["active_upstream_runs"],
        })
    _require_provider_clearance(get_conn(), episode_id=episode_id)
    await reset_video_completion_state(episode_id, reason="VIDEOS_CLEARED")
    worker.pause_episode_video_tasks(episode_id)
    try:
        result = worker.clear_episode_video_assets(episode_id)
    except ValueError as exc:
        raise HTTPException(409, getattr(exc, "detail", str(exc))) from exc
    _review_write_audit("artifacts.clear_episode_videos", "episode", episode_id, new_state=result)
    return result

async def reset_video_completion_state(episode_id: str, *, reason: str = "CANCELLED") -> dict:
    """停止全片补齐 Supervisor，并把集级补齐状态复位，避免生成台死锁。"""
    from app import task_registry
    from app.completion_grant import revoke_grant
    from app.video_control import request_control
    from app.video_supervisor import load_latest_checkpoint, save_checkpoint

    _ensure_video_episode_columns()
    cancelled = await task_registry.cancel_and_wait("video_completion", episode_id)
    try:
        request_control(episode_id, "clear")
    except Exception:  # noqa: BLE001
        pass
    cp = load_latest_checkpoint(episode_id)
    if cp:
        if cp.grant_id:
            try:
                revoke_grant(cp.grant_id)
            except Exception:  # noqa: BLE001
                pass
        if cp.phase not in {"SUCCEEDED_COVERED", "CANCELLED"}:
            cp.phase = "CANCELLED"
            cp.outcome = reason
            save_checkpoint(cp, run_id=cp.run_id)
    conn = get_conn()
    conn.execute(
        """UPDATE episodes
           SET video_completion_mode='quick',
               active_video_run_id=NULL,
               video_control_json=NULL,
               status=CASE WHEN status='generating' THEN 'confirmed' ELSE status END
           WHERE id=?""",
        (episode_id,),
    )
    conn.commit()
    return {"episode_id": episode_id, "cancelled_task": bool(cancelled), "reason": reason}

@router.post("/episodes/{episode_id}/video-completion/reset")
async def reset_video_completion(episode_id: str):
    """强制结束补齐 Supervisor 并复位面板状态（不清空已有视频文件）。"""
    _episode_or_404(episode_id)
    return await reset_video_completion_state(episode_id, reason="RESET")

@router.post("/shots/{shot_id}/clear-artifacts")
async def clear_shot_artifacts(shot_id: str):
    """清空单个镜头的参考图、视频与模型分析。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_shot", {"shot_id": shot_id})
    if routed is not None:
        return routed
    conn = get_conn()
    shot = conn.execute("SELECT id, episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    snapshot = _review_upstream_snapshot(shot["episode_id"])
    if snapshot["active_upstream_runs"]:
        raise HTTPException(409, {
            "code": "UPSTREAM_RUN_ACTIVE",
            "message": "编剧或分镜任务仍在写入，不能普通清空",
            "active_runs": snapshot["active_upstream_runs"],
        })
    _require_provider_clearance(conn, shot_ids=[shot_id])
    worker.stop_shot_video_tasks(shot_id)
    try:
        result = worker.clear_shot_artifacts(shot_id)
    except ValueError as exc:
        raise HTTPException(409, getattr(exc, "detail", str(exc))) from exc
    _review_write_audit("artifacts.clear_shot", "shot", shot_id, new_state=result)
    return result

def _shot_clear_context(shot_id: str):
    conn = get_conn()
    shot = conn.execute("SELECT id, episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    snapshot = _review_upstream_snapshot(shot["episode_id"])
    if snapshot["active_upstream_runs"]:
        raise HTTPException(409, {
            "code": "UPSTREAM_RUN_ACTIVE",
            "message": "编剧或分镜任务仍在写入，不能清空资产",
            "active_runs": snapshot["active_upstream_runs"],
        })
    return conn, shot

@router.post("/shots/{shot_id}/references/clear")
async def clear_shot_references(shot_id: str):
    """Clear this shot's generated images without touching its videos."""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_shot_references", {"shot_id": shot_id})
    if routed is not None:
        return routed
    conn, shot = _shot_clear_context(shot_id)
    active = conn.execute(
        """SELECT COUNT(*) AS c FROM jobs WHERE shot_id=? AND kind='video'
           AND status IN ('queued','running','waiting_provider','waiting_retry','paused')""",
        (shot_id,),
    ).fetchone()["c"]
    if active:
        raise HTTPException(409, "本镜仍有生成任务，请先停止整集任务再清空参考图")
    try:
        result = worker.clear_shot_reference_assets(shot_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _review_write_audit("artifacts.clear_shot_references", "shot", shot_id, new_state=result)
    return result

@router.post("/shots/{shot_id}/videos/clear")
async def clear_shot_videos(shot_id: str):
    """Clear this shot's videos while preserving its reference images."""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_shot_videos", {"shot_id": shot_id})
    if routed is not None:
        return routed
    conn, shot = _shot_clear_context(shot_id)
    _require_provider_clearance(conn, shot_ids=[shot_id])
    worker.stop_shot_video_tasks(shot_id)
    try:
        result = worker.clear_shot_video_assets(shot_id)
    except ValueError as exc:
        raise HTTPException(409, getattr(exc, "detail", str(exc))) from exc
    _review_write_audit("artifacts.clear_shot_videos", "shot", shot_id, new_state=result)
    return result

@router.delete("/versions/{version_id}")
async def delete_version(version_id: str):
    """删除一个已生成的视频版本（含文件）。若是采用版则清空采用、使本集成品失效。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.delete_version", {"version_id": version_id})
    if routed is not None:
        return routed
    conn = get_conn()
    v = conn.execute(
        """SELECT v.id, v.shot_id, s.adopted_version_id,
                  EXISTS(SELECT 1 FROM jobs j WHERE j.version_id=v.id AND j.status IN ('queued','running','waiting_provider')) AS active_job
             FROM shot_versions v JOIN shots s ON s.id=v.shot_id WHERE v.id=?""",
        (version_id,),
    ).fetchone()
    if not v:
        raise HTTPException(404, "视频版本不存在")
    _require_provider_clearance(conn, version_ids=[version_id])
    if v["adopted_version_id"] == version_id:
        raise HTTPException(409, "当前采用版受保护，请先采用其他版本")
    if v["active_job"]:
        raise HTTPException(409, "该版本仍在生成且被任务依赖，请先停止任务")
    shot_id = worker.delete_video_version(version_id)
    _review_write_audit("video_version.delete", "version", version_id, old_state=dict(v))
    return {"deleted": version_id, "shot_id": shot_id}
