"""prep-pack 三阶段（事件链提取/资产映射/覆盖与发布）状态快照。

从 ``app/domain/screenplay_ops/lightweight_status.py`` 按原样搬移出来
（2026-08-30，层号治理，见 ``docs/layer_violations_plan_2026-08-30.md`` 组
11）：``_prep_pack_stage_snapshot``/``_PREP_PACK_STAGE_STEP_KEYS`` 只依赖
``app.db.get_conn``（L2）+ 延迟 ``app.orchestration.engine.step_presentation``
（L3），但 ``lightweight_status.py`` 模块级 import 了 ``app.domain.common.router``
（真实 FastAPI 路由对象，L5），整个文件不能降级——只把这两个纯读取符号搬到独立
文件，让 ``app.production.revision`` 不用为了它们越级 import 整个 domain 包。
``lightweight_status.py`` 从本文件重新导入并保持原名可从
``app.domain.screenplay_ops``/``app.domain`` 原样导入，不影响既有调用点。
"""
from __future__ import annotations

from typing import Any

from app.db import get_conn

_PREP_PACK_STAGE_STEP_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("event_chain_extraction", ("episode_prep_pack_event_chain_chunk",)),
    ("asset_mapping", ("episode_prep_pack_asset_mapping",)),
    ("coverage_and_publish", ("episode_prep_pack_publish",)),
)


def _prep_pack_stage_snapshot(episode_id: str) -> list[dict[str, Any]]:
    """Derive each stage's state from the latest screenplay-workflow run's
    persisted step_runs (durable audit trail, not an in-memory guess).

    Shape: {"key", "display_name", "state"}; state in
    {"pending", "active", "done", "blocked"}.
    """
    from app.orchestration.engine import step_presentation

    conn = get_conn()
    run_row = conn.execute(
        """SELECT id FROM workflow_runs
             WHERE scope_type='episode' AND scope_id=? AND workflow_type='screenplay'
             ORDER BY COALESCE(started_at, updated_at) DESC, id DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    statuses_by_step_key: dict[str, list[str]] = {}
    if run_row is not None:
        rows = conn.execute(
            "SELECT step_key, status FROM step_runs WHERE run_id=?",
            (run_row["id"],),
        ).fetchall()
        for row in rows:
            statuses_by_step_key.setdefault(row["step_key"], []).append(row["status"])
    stages: list[dict[str, Any]] = []
    for stage_key, step_keys in _PREP_PACK_STAGE_STEP_KEYS:
        statuses = [s for key in step_keys for s in statuses_by_step_key.get(key, [])]
        if not statuses:
            state = "pending"
        elif any(s == "FAILED" for s in statuses):
            state = "blocked"
        elif any(s not in {"SUCCEEDED", "FAILED"} for s in statuses):
            state = "active"
        else:
            state = "done"
        stages.append({
            "key": stage_key,
            "display_name": step_presentation(step_keys[0]).name,
            "state": state,
        })
    return stages
