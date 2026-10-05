"""存量分镜道具/衣物状态复核 API：只读复核（GET，供前端预览与
``scripts/storyboard_prop_continuity_dry_run.py`` 共用同一判据）+ 复核后
最小修改重写（POST，走「最小修改重写」语义保存，旧视频保留至新版本生成成功，
见 ``app.domain.storyboard_ops.prop_continuity_minimal_patch`` 模块
docstring「为什么不能用整段重生成修存量分镜」）。

业务逻辑见 ``app.domain.storyboard_ops.prop_continuity_review`` 模块
docstring；本文件只负责装配 ``episode``/``bible`` 两份输入、落盘/读取预览
快照，以及把结果序列化成 JSON。

## 耗时提示

GET 预览对本集每一段都发一次复核模型调用，段数越多耗时越长（实测约 35 段
需要 30 分钟量级）；POST 重写只对 ``confirmed_segment_nos`` 里真正要改的
段各发 1-2 次模型调用（提案 + 保存后复核），通常远快于 GET。两者都在 HTTP
请求内同步完成——调用方（含前端）应按分钟级展示进度提示，不要把等待误判
成卡死。

## POST /rewrite 认快照、不重新复核（2026-10-05 快照化修正）

POST 不再重新调用复核模型：必须先调一次 GET 拿到 ``snapshot_id``，把其中
希望重写的段号挑出来连同 ``snapshot_id`` 一起传给 POST。``confirmed_
segment_nos`` 必须是这份快照里「有已核验违规」段号集合的**子集**（不要求
完全相等：用户可以只重写一部分段）；不是子集（例如包含了快照里没有违规的
段）返回 409，错误信息里直接给出快照里的真实待重写段号。快照有效期见
``prop_continuity_snapshot_store.SNAPSHOT_TTL_S``，过期后 POST 返回 409
并提示重新预览；``snapshot_id`` 不存在或不属于该集返回 404。每个确认段落
还会核对「当前 prompt_text 哈希」与「快照里预览那一刻的哈希」是否一致，
不一致（分镜在预览后被别的操作改过）会被跳过并给出可见原因，不会拿一份
过期的违规清单去瞎改当前正文——完整设计见 ``prop_continuity_review`` 模块
docstring。没有字段默认值——漏传等同于没有确认，直接走标准请求校验
（422）。``kinds`` 可选，非空时只处理这些类别的违规，取值必须是 11 类判据
之一，否则 422（``VALID_PROSE_REVIEW_KINDS``，与 ``storyboard_prose_
review`` 的 kind 合法性判据同一份数据）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.db import get_conn
from app.domain.common import _project_bible_or_placeholder

from .prop_continuity_review import (
    VALID_PROSE_REVIEW_KINDS, build_review_snapshot_segments, flagged_segment_nos, outcomes_from_snapshot,
    review_existing_episode_segments, rewrite_flagged_segments,
)
from .prop_continuity_snapshot_store import is_expired, load_snapshot, save_snapshot

router = APIRouter(prefix="/api")


class PropContinuityRewriteBody(BaseModel):
    #: GET 预览落盘的快照 id；POST 只认这份快照的违规清单，不重新复核，见
    #: 模块 docstring。没有默认值——漏传直接 422，不会被当成"用服务端此刻
    #: 状态"的隐式含义。
    snapshot_id: str
    #: 调用方选择重写的段号子集（不是任选全集）；必须是快照里待重写段号
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
    snapshot = await save_snapshot(episode_id=episode_id, segments=build_review_snapshot_segments(conn, episode_id, outcomes))
    return {
        "episode_id": episode_id, "snapshot_id": snapshot["id"], "snapshot_expires_at": snapshot["expires_at"],
        "segments": [o.to_dict() for o in outcomes],
    }


@router.post("/episodes/{episode_id}/prop-continuity-review/rewrite")
async def prop_continuity_rewrite(episode_id: str, body: PropContinuityRewriteBody):
    conn = get_conn()
    try:
        episode = _episode_or_raise(conn, episode_id)
        snapshot = load_snapshot(conn, snapshot_id=body.snapshot_id, episode_id=episode_id)
        if snapshot is None:
            raise HTTPException(404, "预览快照不存在或不属于该集，请重新调用 GET 预览")
        if is_expired(snapshot):
            raise HTTPException(409, "预览快照已过期，请重新调用 GET 预览后再确认")
        outcomes = outcomes_from_snapshot(snapshot["segments"])
        flagged = flagged_segment_nos(outcomes)
        confirmed = set(body.confirmed_segment_nos)
        if not confirmed.issubset(flagged):
            raise HTTPException(409, f"确认重写的段号必须是本次快照里待重写段号的子集（快照里应重写：{flagged}），请重新预览后再确认")
        project = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(project)
        outcomes = await rewrite_flagged_segments(
            conn, episode=episode, bible=bible, outcomes=outcomes,
            confirmed_segment_nos=confirmed, kinds=set(body.kinds) if body.kinds else None,
        )
    except ValueError as exc:
        # 与 GET 预览同一取舍：_episode_or_raise/rewrite_flagged_segments 内部的
        # _load_stored_segments 都可能因为该集在确认重写前被改成旧式分镜而抛
        # ValueError，必须转成 409（CLAUDE.md「错误要转成合适的状态码」），不能
        # 让它作为未捕获异常变成裸 500。
        raise HTTPException(409, str(exc)) from exc
    return {"episode_id": episode_id, "segments": [o.to_dict() for o in outcomes]}
