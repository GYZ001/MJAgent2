"""剧本运行所有权断言：判定当前 trace 的 run_id 是否仍是该剧集的活跃剧本运行。

从 ``app/domain/screenplay_ops/run_control.py`` 按原样搬移（2026-08-30，层号
治理，消掉 ``app/LAYERS.toml`` 组 12 全仓最后一条上行边
``app.production.screenplay_repair.checkpoint_recovery -> app.domain.
screenplay_ops``）：``_assert_screenplay_run_owner`` 只依赖
``app.db.get_conn``（L2）+ ``app.orchestration.state_machine.StateConflict``
（L2）+ 延迟 ``app.observability.tracing.current_trace``（L1）。但
``run_control.py`` 模块级 import 了 ``app.domain.common.router``（真实
FastAPI 路由对象，默认 L5）且承载 ``update_episode_target_duration`` 路由，
整个文件不能降级——只把这一个纯断言函数搬到独立文件，供
``app.domain.screenplay_ops.character_discovery``（L4）直接引用。
``run_control.py`` 从本文件重新导入并保持原名可从
``app.domain.screenplay_ops``/``app.domain``/``.run_control`` 原样导入，不
影响既有调用点（``guarded.py`` 等仍从 ``.run_control`` 取这个符号）。
"""
from __future__ import annotations

from app.db import get_conn
from app.orchestration.state_machine import StateConflict


def _assert_screenplay_run_owner(
    episode_id: str,
    *,
    run_id: str | None = None,
) -> None:
    if run_id is None:
        from app.observability.tracing import current_trace

        run_id = current_trace().run_id
    if not run_id:
        return
    row = get_conn().execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    actual = str(row["active_screenplay_run_id"] or "") if row else "missing"
    if not row or actual != run_id:
        raise StateConflict(
            "screenplay_owner",
            episode_id,
            {run_id},
            actual,
        )
