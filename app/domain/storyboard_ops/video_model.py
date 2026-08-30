"""本集绑定视频生成模型的人工切换入口（与生成台强绑定，不做静默转换）。

从 app/domain/storyboard_ops.py 按原样搬移；与 video_ops/review_wall 的真实双向依赖用函数内延迟导入解决，见函数体内注释。
"""
from __future__ import annotations

from app import worker
from app.db import get_conn
from app.domain.common import (
    _episode_or_404,
    router,
)
from fastapi import HTTPException


def _episode_target_video_model(ep) -> str:
    """归一化本集绑定的视频供应商 key；历史脏值/空值回落到 provider 默认。"""
    from app import video_providers

    raw = str(ep["target_video_model"] or "").strip()
    return raw if raw in video_providers.registered_providers() else "hiagent"

@router.post("/episodes/{episode_id}/video-model")
async def set_episode_video_model(episode_id: str, body: dict | None = None):
    """分镜台人工切换本集绑定的视频生成模型；与生成台强绑定，不做静默转换。

    两个供应商的提示词方言互不兼容（Seedance 自由中文散文 vs MiniMax H3 结构化
    英文字段+双语台词块），留着旧方言已生成的产物就是脏数据。本集已有视频生成
    产物时必须显式带 ``confirm_clear_prompts=true`` 二次确认才会执行，执行时
    连带清空这些产物（复用 ``videos/clear`` 同一套清空机制，保留参考图）；没有
    产物的普通切换不受此限。账号即项目空间之后，能触达这个端点就已经是本项目
    的所有者或系统管理员（HTTP 边界 ``require_project_owner_access`` 已经拦过一
    轮，见 app/authz/resolve.py），不再需要按团队角色二次收紧写权限。写法参照
    本文件的 ``storyboard/clear-preview``/``storyboard/clear``：分镜台本机人工
    入口，不向 Agent/MCP 开放。
    """
    from app import video_providers
    from app.domain.review_wall import _review_upstream_snapshot, _review_write_audit
    from app.domain.video_ops import (
        _require_provider_clearance,
        reset_video_completion_state,
    )

    body = body or {}
    target = str(body.get("target_video_model") or "").strip()
    options = video_providers.registered_providers()
    if target not in options:
        raise HTTPException(
            422,
            f"未知视频模型：{target or '(空)'}；可选：{'、'.join(sorted(options))}",
        )
    ep = _episode_or_404(episode_id)
    current = _episode_target_video_model(ep)
    if target == current:
        return {
            "episode_id": episode_id, "target_video_model": current,
            "changed": False, "cleared_videos": 0,
        }
    conn = get_conn()
    prompt_artifact_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
        (episode_id,),
    ).fetchone()["c"])
    if prompt_artifact_count and not bool(body.get("confirm_clear_prompts")):
        raise HTTPException(409, {
            "code": "VIDEO_MODEL_SWITCH_REQUIRES_CONFIRMATION",
            "message": (
                f"本集已有 {prompt_artifact_count} 条视频生成产物（提示词方言绑定于 "
                f"{current}），切换到 {target} 会清空这些产物；两套模型提示词语法不兼容，"
                "不能混用。请带 confirm_clear_prompts=true 二次确认后再切换。"
            ),
            "prompt_artifact_count": prompt_artifact_count,
            "current_target_video_model": current,
            "requested_target_video_model": target,
        })
    cleared_videos = 0
    if prompt_artifact_count:
        snapshot = _review_upstream_snapshot(episode_id)
        if snapshot["active_upstream_runs"]:
            raise HTTPException(409, {
                "code": "UPSTREAM_RUN_ACTIVE",
                "message": "编剧或分镜任务仍在写入，不能切换视频模型",
                "active_runs": snapshot["active_upstream_runs"],
            })
        _require_provider_clearance(conn, episode_id=episode_id)
        await reset_video_completion_state(episode_id, reason="VIDEO_MODEL_SWITCH")
        worker.pause_episode_video_tasks(episode_id)
        try:
            clear_result = worker.clear_episode_video_assets(episode_id)
        except ValueError as exc:
            raise HTTPException(409, getattr(exc, "detail", str(exc))) from exc
        cleared_videos = int(clear_result.get("videos") or 0)
    conn = get_conn()
    conn.execute(
        "UPDATE episodes SET target_video_model=? WHERE id=?",
        (target, episode_id),
    )
    conn.commit()
    _review_write_audit(
        "episode.video_model_switch", "episode", episode_id,
        old_state={"target_video_model": current},
        new_state={"target_video_model": target, "cleared_videos": cleared_videos},
    )
    return {
        "episode_id": episode_id, "target_video_model": target,
        "changed": True, "cleared_videos": cleared_videos,
    }
