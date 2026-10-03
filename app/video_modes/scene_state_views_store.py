"""场景状态图（``scene_state_views``）的表结构与读写原语（懒建表，照
``app/voice/store.py``/``app/video_modes/character_look_views_store.py``——同一
协作者此前为人物造型照写过——的写法）。

不进 ``app/db.py``：该文件扇入 >100，CLAUDE.md「扇入 >100 的模块不得再加职责」。
``ensure_schema()``/``ensure_tables_on_connection()`` 双入口分工、DDL 逐条
``conn.execute()``（禁 ``executescript``，它执行前的隐式 COMMIT 会把调用方尚未
提交的事务一起偷偷提交掉）照抄既有先例，不重复整套论证。

本模块只做持久化原语（建表、按行读写），不做任何业务判断（state_key 怎么算、
该不该重新生成）——那些留在同包 ``scene_state_views.py``/``scene_state_ensure.py``。
``conn`` 全部由调用方传入、本模块任何函数都不自行 ``commit()``（CLAUDE.md「不得
在调用方的连接上隐式提交」）——状态转移（queued/running/ready/failed）的事务
边界由调用方（``scene_state_ensure`` 的 ``claim_or_get``/``_mark_ready``/
``_mark_failed``，各自经 ``app.db.run_write_transaction`` 开独立事务）决定。

``insert_running``/``update_running``/``update_ready``/``update_failed`` 四个写
函数不自带 ``ensure_schema()`` 兜底——理由与 ``character_look_views_store`` 模块
文档完全相同（它们只会在 ``run_write_transaction`` 已持有的独立连接事务里被
调用，自己再调 ``ensure_schema()`` 会在同一数据库文件上重复 ``BEGIN IMMEDIATE``
而死锁）；``get_scene_state_view`` 保留 ``ensure_schema()`` 是因为它的调用方
（只读扫描/装配期查询）走普通 ``get_conn()``，不持有独立写事务。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app import db, db_schema

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS scene_state_views (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        episode_id TEXT NOT NULL,
        scene_reference_id TEXT NOT NULL,
        state_key TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        image_path TEXT NOT NULL DEFAULT '',
        prompt TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',
        error TEXT,
        input_fingerprint TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(scene_reference_id, episode_id, state_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_scene_state_views_scene_ep "
    "ON scene_state_views(scene_reference_id, episode_id, status)",
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


def get_scene_state_view(
    conn: Any, *, scene_reference_id: str, episode_id: str, state_key: str,
) -> dict[str, Any] | None:
    ensure_schema()
    row = conn.execute(
        "SELECT * FROM scene_state_views WHERE scene_reference_id=? AND episode_id=? AND state_key=?",
        (scene_reference_id, episode_id, state_key),
    ).fetchone()
    return dict(row) if row else None


def insert_running(
    conn: Any, *, row_id: str, project_id: str, episode_id: str, scene_reference_id: str,
    state_key: str, description: str, fingerprint: str, stamp: float,
) -> None:
    """调用方必须先在同一个 ``conn`` 上调用过 ``ensure_tables_on_connection``
    ——理由见模块文档。"""
    conn.execute(
        """INSERT INTO scene_state_views(
               id, project_id, episode_id, scene_reference_id, state_key, description,
               image_path, prompt, status, error, input_fingerprint, created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row_id, project_id, episode_id, scene_reference_id, state_key, description,
         "", "", "running", None, fingerprint, stamp, stamp),
    )


def update_running(conn: Any, *, row_id: str, description: str, fingerprint: str, stamp: float) -> None:
    """同 ``insert_running`` 的建表前提。"""
    conn.execute(
        "UPDATE scene_state_views SET status='running', description=?, error=NULL, "
        "input_fingerprint=?, updated_at=? WHERE id=?",
        (description, fingerprint, stamp, row_id),
    )


def update_ready(
    conn: Any, *, row_id: str, image_path: str, prompt: str, fingerprint: str, stamp: float,
) -> None:
    """同 ``insert_running`` 的建表前提。"""
    conn.execute(
        "UPDATE scene_state_views SET status='ready', image_path=?, prompt=?, error=NULL, "
        "input_fingerprint=?, updated_at=? WHERE id=?",
        (image_path, prompt, fingerprint, stamp, row_id),
    )


def update_failed(conn: Any, *, row_id: str, error: str, stamp: float) -> None:
    """同 ``insert_running`` 的建表前提。"""
    conn.execute(
        "UPDATE scene_state_views SET status='failed', error=?, updated_at=? WHERE id=?",
        (error[:2000], stamp, row_id),
    )
