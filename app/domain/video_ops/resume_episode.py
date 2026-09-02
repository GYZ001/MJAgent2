"""整集视频生成的续跑与停止。

从 app/domain/video_ops.py 按原样搬移；依赖 clear 与 generate。
"""
from __future__ import annotations

from app import worker
from app.domain.common import (
    _episode_or_404,
    router,
)
from app.domain.review_wall import _review_write_audit
from fastapi import HTTPException

from .clear import reset_video_completion_state
from .generate import _generate_episode_core


@router.post("/episodes/{episode_id}/resume")
async def resume_episode(episode_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.resume_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    reset_result = None
    if (ep["video_completion_mode"] or "quick") == "complete":
        reset_result = await reset_video_completion_state(
            episode_id,
            reason="CONTINUED_AS_QUICK",
        )
    try:
        resumed = worker.resume_episode_video_tasks(episode_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if resumed.get("requires_provider_confirmation"):
        raise HTTPException(409, {
            "code": "PROVIDER_HANDLE_UNCONFIRMED",
            "message": "部分暂停任务可能已被供应商接单，系统未自动重复提交，以避免重复扣费",
            "recovery_action": resumed.get("recovery_action"),
            "unresolved_provider_jobs": resumed.get("unresolved_provider_jobs") or [],
            "episode_id": episode_id,
            "recoverable": True,
        })
    generated = await _generate_episode_core(episode_id, {"only_incomplete": True})
    if (
        int(resumed.get("resumed_jobs") or 0) == 0
        and int(generated.get("selected_shots") or 0) == 0
        and not generated.get("enqueued")
    ):
        if reset_result is not None:
            return {
                **resumed,
                "enqueued": [],
                "skipped_completed": int(generated.get("skipped_completed") or 0),
                "selected_shots": 0,
                "state_changed": True,
                "video_completion_mode": "quick",
                "supervisor_stopped": True,
                "cancelled_task": bool(reset_result.get("cancelled_task")),
                "message": "已停止全片补齐并切回快速模式；当前没有其他待继续任务",
            }
        raise HTTPException(409, {
            "code": "VIDEO_RESUME_EMPTY",
            "message": "当前没有可继续的视频任务",
            "recovery_action": "如需重新生成，请在生成台选择具体镜头或整集重新生成",
            "episode_id": episode_id,
            "recoverable": True,
            "state": {
                "resumed_jobs": 0,
                "selected_shots": 0,
                "skipped_completed": int(generated.get("skipped_completed") or 0),
            },
        })
    return {
        **resumed,
        "enqueued": generated["enqueued"],
        "skipped_completed": generated["skipped_completed"],
        "selected_shots": generated["selected_shots"],
        "state_changed": reset_result is not None,
        "video_completion_mode": "quick",
        "supervisor_stopped": reset_result is not None,
    }

@router.post("/episodes/{episode_id}/video/stop")
async def stop_episode_video(episode_id: str):
    """Pause the whole episode's video work; a later Continue can resume it."""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.stop_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    if (ep["video_completion_mode"] or "quick") == "complete":
        await reset_video_completion_state(episode_id, reason="STOPPED")
    try:
        result = worker.pause_episode_video_tasks(episode_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    _review_write_audit("video.pause_episode", "episode", episode_id, new_state=result)
    return result
