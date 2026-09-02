"""resume 模式越过 owner-pointer 冲突、接管旧运行的判据。

从 `completion_core.py` 拆出（该文件行数基线已零缓冲，装不下新逻辑）。只服务一个
调用方——`completion_core.py::_complete_episode_core`——判断带着 grant 的
`mode="resume"` 请求能不能越过 `episodes.active_video_run_id` 指向的旧运行继续，
而不是被当成"重复提交"拦下。

判据（`_assert_resume_may_take_over`）：旧运行必须卡在等待类状态
（`_RESUME_TAKEOVER_STATUSES`：PAUSED_EXTERNAL/WAITING_RETRY/WAITING_HUMAN/
WAITING_AUTHORIZATION；CREATED/RUNNING 不放行——那可能是刚起步还没写心跳的活运行，
接管会和它抢同一批产出）、对应的补齐协程已经不在跑、且这次带来的 grant 正是旧运行
当初持有的那份（`load_latest_checkpoint` 校验）。不满足直接抛 409
VIDEO_COMPLETION_ALREADY_ACTIVE（与改动前行为一致）；checkpoint 缺失或 grant 对不上
抛更精确的 409 VIDEO_COMPLETION_RESUME_GRANT_MISMATCH，不静默放行也不用不相关的
错误码糊弄前端。

只依赖 app.task_registry（判协程是否还活着）与 app.video_supervisor（读
checkpoint）——都是层号更低/同层的模块，不导入 completion_core，避免循环。
"""
from __future__ import annotations

from app import task_registry
from fastapi import HTTPException

# resume 接管旧运行可越过的等待类状态（CREATED/RUNNING 不算，可能是刚起步还没写心跳的活运行）。
_RESUME_TAKEOVER_STATUSES = {
    "PAUSED_EXTERNAL", "WAITING_RETRY", "WAITING_HUMAN", "WAITING_AUTHORIZATION",
}


def _assert_resume_may_take_over(
    *, mode: str, grant_id: str | None, existing, episode_id: str,
    previous_run_id: str | None, active_status: str | None,
) -> None:
    """判据不满足就抛 ALREADY_ACTIVE；满足但 checkpoint 对不上就抛更精确的 RESUME_GRANT_MISMATCH；都通过则放行接管。"""
    eligible = (
        mode == "resume" and grant_id and existing is not None and previous_run_id
        and active_status in _RESUME_TAKEOVER_STATUSES
        and not task_registry.active("video_completion", episode_id)
    )
    if not eligible:
        raise HTTPException(409, {
            "code": "VIDEO_COMPLETION_ALREADY_ACTIVE",
            "message": "全片补齐任务已在启动或运行，请勿重复提交",
            "active_run_id": previous_run_id,
            "action": "view_progress",
        })
    from app.video_supervisor import load_latest_checkpoint

    cp = load_latest_checkpoint(episode_id)
    if not cp or cp.grant_id != grant_id or cp.run_id != previous_run_id:
        raise HTTPException(409, {
            "code": "VIDEO_COMPLETION_RESUME_GRANT_MISMATCH",
            "message": "本次续跑授权与当前挂起的补齐运行不一致，请重新发起全片补齐",
            "action": "start_fresh",
        })


def resolve_resume_parent_run_id(
    resume_takeover: bool, previous_active_run_id: str | None, parent_run_id: str | None,
) -> str | None:
    """接管时旧运行未必是调用方传入的 parent_run_id（连播台自动唤醒不传），改用真正
    被接管的 previous_active_run_id，让 WorkflowRecorder.create -> repository.create_run
    照常给旧运行写 recovered_by_run_id，避免它被续跑两次。"""
    return previous_active_run_id if resume_takeover else parent_run_id
