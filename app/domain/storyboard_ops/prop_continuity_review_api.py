"""存量分镜道具/衣物状态复核 API：只读复核（GET，供前端预览与
``scripts/storyboard_prop_continuity_dry_run.py`` 共用同一判据）+ 复核后
最小修改重写（POST，走「最小修改重写」语义保存，旧视频保留至新版本生成成功，
见 ``app.domain.storyboard_ops.prop_continuity_minimal_patch`` 模块
docstring「为什么不能用整段重生成修存量分镜」）。

业务逻辑见 ``app.domain.storyboard_ops.prop_continuity_review`` 模块
docstring；本文件只负责装配 ``episode``/``bible`` 两份输入，以及把结果序列化
成 JSON。

## POST /rewrite 要求显式确认段号子集（2026-10-05 复核修正第二轮）

调用方必须先调一次 GET 拿到当前复核结果，把其中希望重写的段号挑出来传到
``confirmed_segment_nos``——必须是服务端此刻待重写段号集合的**子集**（不再
要求完全相等：用户现在可以只重写一部分段，不必每次都处理全部）；不是子集
（例如包含了服务端此刻没有违规的段，或调用方拿的是一份过期预览）会被拒绝
（409），错误信息里直接给出服务端此刻的真实待重写段号，调用方可以据此决定
要不要重新拉取并再次确认——不会把调用方没见过的段也悄悄重写掉。没有字段
默认值——漏传等同于没有确认，直接走标准请求校验（422）。``kinds`` 可选，
非空时只处理这些类别的违规（例如只修 ``prop_state_regression``，不碰同一段
里的其它违规），取值必须是 11 类判据之一，否则 422（``VALID_PROSE_REVIEW_
KINDS``，与 ``storyboard_prose_review`` 的 kind 合法性判据同一份数据）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.db import get_conn
from app.domain.common import _project_bible_or_placeholder

from .prop_continuity_review import VALID_PROSE_REVIEW_KINDS, review_existing_episode_segments, rewrite_flagged_segments

router = APIRouter(prefix="/api")


class PropContinuityRewriteBody(BaseModel):
    #: 调用方选择重写的段号子集（不是任选全集）；必须是服务端此刻待重写段号
    #: 集合的子集，否则 409，见模块 docstring。
    confirmed_segment_nos: list[int]
    #: 只处理这些类别的违规；``None``（默认）表示不按类别过滤，见模块 docstring。
    kinds: list[str] | None = None

    @field_validator("kinds")
    @classmethod
    def _kinds_must_be_known(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        invalid = sorted(set(value) - VALID_PROSE_REVIEW_KINDS)
        if invalid:
            raise ValueError(f"不支持的违规类别：{invalid}；合法取值见 11 类判据（storyboard_prose_review._KIND_RULES）")
        return value


def _episode_or_raise(conn, episode_id: str) -> dict:
    """取该集任意一个镜头确认已生成过分镜；该集没有任何镜头时抛错，提示先
    生成分镜——不需要 payload（复核/重写都不依赖它），不借 ``load_identity_
    workspace`` 多拿一份没用上的场景重绑定结果。"""
    first_shot = conn.execute("SELECT id FROM shots WHERE episode_id=? ORDER BY shot_no LIMIT 1", (episode_id,)).fetchone()
    if first_shot is None:
        raise ValueError("本集尚未生成任何分镜，无法复核")
    return dict(conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone())


@router.get("/episodes/{episode_id}/prop-continuity-review")
async def prop_continuity_review(episode_id: str):
    conn = get_conn()
    try:
        episode = _episode_or_raise(conn, episode_id)
        project = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
        outcomes = await review_existing_episode_segments(conn, episode=episode, bible=_project_bible_or_placeholder(project))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"episode_id": episode_id, "segments": [o.to_dict() for o in outcomes]}


@router.post("/episodes/{episode_id}/prop-continuity-review/rewrite")
async def prop_continuity_rewrite(episode_id: str, body: PropContinuityRewriteBody):
    conn = get_conn()
    try:
        episode = _episode_or_raise(conn, episode_id)
        project = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(project)
        outcomes = await review_existing_episode_segments(conn, episode=episode, bible=bible)
        flagged = sorted(o.segment_no for o in outcomes if o.violations)
        confirmed = set(body.confirmed_segment_nos)
        if not confirmed.issubset(flagged):
            raise HTTPException(409, f"确认重写的段号必须是服务端此刻待重写段号的子集（当前应重写：{flagged}），请重新拉取 GET 预览结果后再确认")
        outcomes = await rewrite_flagged_segments(
            conn, episode=episode, bible=bible, outcomes=outcomes,
            confirmed_segment_nos=confirmed, kinds=set(body.kinds) if body.kinds else None,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"episode_id": episode_id, "segments": [o.to_dict() for o in outcomes]}
