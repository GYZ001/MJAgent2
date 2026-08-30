"""轻量剧本状态查询：prep-pack 阶段快照与对外的轻量状态路由。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 status_snapshot。
"""
from __future__ import annotations

from app.db import get_conn
from app.domain.common import (
    _episode_or_404,
    router,
)
from app.evidence import repository as evidence_repository

from .prep_pack_stage_snapshot import (
    _PREP_PACK_STAGE_STEP_KEYS as _PREP_PACK_STAGE_STEP_KEYS,
    _prep_pack_stage_snapshot,
)
from .status_snapshot import (
    _screenplay_authority_state,
    _screenplay_production_state,
)


@router.get("/episodes/{episode_id}/screenplay/status")
def screenplay_lightweight_status(episode_id: str):
    """运行期轻量状态：不返回正文、台词库、镜头或证据。"""
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"])
    # 只读端点：同一次请求内权威链会反复读到同一批不可变 Artifact，
    # 复读一次也得不到不同的答案（见 artifact_read_scope）。
    with evidence_repository.artifact_read_scope():
        production = _screenplay_production_state(episode_id)
        snapshot = _screenplay_authority_state(
            ep,
            shot_count=shot_count,
            production=production,
        )
    # ``production["stages"]`` is the heavy-pipeline's 10-step ProductionRevision
    # ledger (app.production.revision.screenplay_production_state) -- for the
    # lightweight prep_pack flow (screenplay contract 6.0.0+) no
    # ProductionRevision is ever created, so ``rev`` there is always None and
    # this field used to be the hardcoded all-"pending" 10-step list, never
    # reflecting real progress. A real mobile observation during this task's
    # live verification run showed the old 10-step list still rendering
    # mid-generation because the frontend fallback picked up this
    # always-non-empty legacy field. Every other backend caller of
    # screenplay_production_state()/_screenplay_production_state() reads the
    # function's return value directly in Python (grepped: none read this
    # "stages" key from this HTTP response), so dropping it only from this
    # response shape is safe -- the underlying function/table and its own
    # tests (test_screenplay_controls.py, test_screenplay_delete.py) are
    # untouched.
    #
    # 单一真源收口（另一轮真实回归：集详情投影首屏闪现同一套旧十步，见
    # app.production.revision.screenplay_production_state 的模块级 E 类
    # 教训 docstring）：``production`` 现在自带一份 ``production["prep_pack_
    # stages"]``（跟下面显式下发的顶层 ``prep_pack_stages`` 出自同一个
    # _prep_pack_stage_snapshot 调用，值必然一致，允许重复但不允许来源
    # 不同）——这里的过滤只挑掉 "stages" 一个 key，不影响它随 production_
    # for_response 原样透出，storyboard_ops.py 的集详情投影因此也拿到同一
    # 份数据，不需要在那边另写一次。
    production_for_response = {k: v for k, v in production.items() if k != "stages"}
    return {
        "id": episode_id,
        "screenplay_status": ep["screenplay_status"],
        "screenplay_error": ep["screenplay_error"],
        "screenplay_updated_at": ep["screenplay_updated_at"],
        "status": ep["status"],
        "script_error": ep["script_error"],
        "shot_count": shot_count,
        "active_storyboard_run_id": ep.get("active_storyboard_run_id"),
        "screenplay_production": production_for_response,
        "screenplay_state": snapshot,
        "prep_pack_stages": _prep_pack_stage_snapshot(episode_id),
        "active": bool(
            production.get("task_active")
            or snapshot["storyboard_running"]
            or snapshot["code"] in {"screenplay_cancelling", "save_stopping_downstream"}
        ),
    }
