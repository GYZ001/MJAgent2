"""分镜阶段补卡（``card_pending_ensure``）的表结构与读写原语——懒建表，照
``app/video_modes/scene_state_views_store.py`` 的写法（同一类"按
(project_id, 判据对象) 做跨调用 CAS 抢占"需求）。

不进 ``app/db.py``：该文件扇入 >100，CLAUDE.md「扇入 >100 的模块不得再加职责」。
``ensure_schema()``/``ensure_tables_on_connection()`` 双入口分工、DDL 逐条
``conn.execute()``（禁 ``executescript``，它执行前的隐式 COMMIT 会把调用方尚未
提交的事务一起偷偷提交掉）照抄既有先例，不重复整套论证。

本模块只做持久化原语，不做业务判断（该不该补卡、判据怎么算）——那些留在同包
``card_pending_scan.py``/``card_pending_ensure.py``。``conn`` 全部由调用方传入、
本模块任何函数都不自行 ``commit()``（CLAUDE.md「不得在调用方的连接上隐式提交」）。

按 ``(project_id, label)`` 维度抢占，不按集（道具卡一旦建成是项目级共享资产，
与 ``app.props.store`` 的道具登记同一粒度；同一 label 在后续集里再出现，
``candidate_labels_without_card`` 会因为卡已存在而不再把它判成候选，天然
不会重复抢占）。``attempts`` 记建卡尝试次数，供 ``card_pending_ensure`` 的
「失败重试有限次」额度判据使用——超过上限后不再重试，调用方据此不再拿它
阻塞生成（退回"无卡"的既有降级行为，与本功能上线前完全一致）。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app import db, db_schema

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS prop_storyboard_card_pending (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        label TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        resolved_name TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(project_id, label)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_prop_storyboard_card_pending_proj "
    "ON prop_storyboard_card_pending(project_id, label, status)",
)

_ensured_paths: set[str] = set()


def ensure_tables_on_connection(conn: sqlite3.Connection) -> None:
    """轻量、同连接、无副作用的建表兜底，安全用于调用方已持有事务的场景。"""
    for statement in _CREATE_STATEMENTS:
        conn.execute(statement)


def ensure_schema() -> None:
    """幂等建表；按当前 ``db.DB_PATH`` 记忆已建，避免每次调用都重跑 DDL。"""
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def _run_independent() -> None:
        def operation(conn: sqlite3.Connection) -> None:
            ensure_tables_on_connection(conn)

        try:
            db._run_write_transaction_once(operation)
        except Exception:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞调用方
            return
        _ensured_paths.add(key)

    db_schema.ensure_schema_respecting_caller_transaction(
        db.get_conn(),
        on_caller_connection=ensure_tables_on_connection,
        run_independent=_run_independent,
    )


def get_pending(conn: Any, *, project_id: str, label: str) -> dict[str, Any] | None:
    ensure_schema()
    row = conn.execute(
        "SELECT * FROM prop_storyboard_card_pending WHERE project_id=? AND label=?",
        (project_id, label),
    ).fetchone()
    return dict(row) if row else None


def insert_running(
    conn: Any, *, row_id: str, project_id: str, label: str, stamp: float,
) -> None:
    """调用方必须先在同一个 ``conn`` 上调用过 ``ensure_tables_on_connection``。"""
    conn.execute(
        """INSERT INTO prop_storyboard_card_pending(
               id, project_id, label, status, resolved_name, attempts, error,
               created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (row_id, project_id, label, "running", None, 1, None, stamp, stamp),
    )


def update_running(conn: Any, *, row_id: str, attempts: int, stamp: float) -> None:
    """同 ``insert_running`` 的建表前提。"""
    conn.execute(
        "UPDATE prop_storyboard_card_pending SET status='running', attempts=?, "
        "error=NULL, updated_at=? WHERE id=?",
        (attempts, stamp, row_id),
    )


def update_ready(conn: Any, *, row_id: str, resolved_name: str, stamp: float) -> None:
    """同 ``insert_running`` 的建表前提。"""
    conn.execute(
        "UPDATE prop_storyboard_card_pending SET status='ready', resolved_name=?, "
        "error=NULL, updated_at=? WHERE id=?",
        (resolved_name, stamp, row_id),
    )


def update_failed(conn: Any, *, row_id: str, error: str, stamp: float) -> None:
    """同 ``insert_running`` 的建表前提。"""
    conn.execute(
        "UPDATE prop_storyboard_card_pending SET status='failed', error=?, "
        "updated_at=? WHERE id=?",
        (error[:2000], stamp, row_id),
    )
