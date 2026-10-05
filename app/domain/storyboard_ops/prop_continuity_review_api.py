"""存量分镜道具/衣物状态复核 API：只读复核（GET，供前端预览与
``scripts/storyboard_prop_continuity_dry_run.py`` 共用同一判据）+ 复核后
重写（POST，走「修订本段」语义保存，旧视频保留至新版本生成成功）。

业务逻辑见 ``app.domain.storyboard_ops.prop_continuity_review`` 模块
docstring；本文件只负责装配 ``episode``/``payload``/``bible`` 三份输入
（复用 ``identity_workspace.load_identity_workspace`` 的 payload 场景重绑定，
不重新发明第二套），以及把结果序列化成 JSON。

## POST /rewrite 要求显式确认段号（2026-10-04 复核修正）

调用方必须先调一次 GET 拿到当前复核结果，把其中带违规的段号原样回传到
``confirmed_segment_nos``——服务端重新跑一遍复核，只有「调用方刚看到的
待重写段号」与「服务端此刻算出来的待重写段号」完全一致才会真的重写；
中途有任何变化（比如另一个请求先改了分镜，或调用方拿的是一份过期预览）
都会被拒绝（409），不会把调用方没见过的段也悄悄重写掉，也不会在集合
变化时假装什么都没发生——这是「拦住用户时必须给出路」里「不能把人晾在
原地」的反面：错误信息里直接给出服务端此刻的真实待重写段号，调用方可以
据此决定要不要重新拉取并再次确认。没有字段默认值——漏传等同于没有确认，
直接走标准请求校验（422），不会被当成「确认重写全部」。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_conn
from app.domain.common import _project_bible_or_placeholder

from .identity_workspace import load_identity_workspace
from .prop_continuity_review import review_existing_episode_segments, rewrite_flagged_segments

router = APIRouter(prefix="/api")


class PropContinuityRewriteBody(BaseModel):
    #: 调用方刚看到的 GET 预览里，带已核验违规的段号清单（原样回传，不是
    #: 任选）；与服务端重新复核算出的结果不一致就拒绝，见模块 docstring。
    confirmed_segment_nos: list[int]


def _episode_payload_and_bible(conn, episode_id: str) -> tuple[dict, dict]:
    """取该集任意一个镜头，借 ``load_identity_workspace`` 拿到已重绑定场景引用
    的 ``episode``/``payload``（与「修订本段」同一份权威数据，不重新发明第二套
    场景绑定逻辑）；该集没有任何镜头时抛错，提示先生成分镜。"""
    first_shot = conn.execute("SELECT id FROM shots WHERE episode_id=? ORDER BY shot_no LIMIT 1", (episode_id,)).fetchone()
    if first_shot is None:
        raise ValueError("本集尚未生成任何分镜，无法复核")
    _row, episode, payload, _segment = load_identity_workspace(conn, first_shot["id"])
    return episode, payload


@router.get("/episodes/{episode_id}/prop-continuity-review")
async def prop_continuity_review(episode_id: str):
    conn = get_conn()
    try:
        episode, _payload = _episode_payload_and_bible(conn, episode_id)
        project = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
        outcomes = await review_existing_episode_segments(conn, episode=episode, bible=_project_bible_or_placeholder(project))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"episode_id": episode_id, "segments": [o.to_dict() for o in outcomes]}


@router.post("/episodes/{episode_id}/prop-continuity-review/rewrite")
async def prop_continuity_rewrite(episode_id: str, body: PropContinuityRewriteBody):
    conn = get_conn()
    try:
        episode, payload = _episode_payload_and_bible(conn, episode_id)
        project = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(project)
        outcomes = await review_existing_episode_segments(conn, episode=episode, bible=bible)
        flagged = sorted(o.segment_no for o in outcomes if o.violations)
        if sorted(body.confirmed_segment_nos) != flagged:
            raise HTTPException(409, f"待重写段号与服务端此刻的复核结果不一致（当前应重写：{flagged}），请重新拉取 GET 预览结果后再确认")
        outcomes = await rewrite_flagged_segments(conn, episode=episode, payload=payload, bible=bible, outcomes=outcomes)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"episode_id": episode_id, "segments": [o.to_dict() for o in outcomes]}
