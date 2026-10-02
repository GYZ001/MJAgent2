"""人物造型照（``character_look_views``）的表结构与读写原语（懒建表，照
``app/voice/store.py``/``app/audit/store.py`` 的写法）。

不进 ``app/db.py``：该文件扇入 254，CLAUDE.md「扇入 >100 的模块不得再加职责」，
且该文件 line_count 已在 ``app/FILE_CONVENTIONS.toml`` 的棘轮基线上零余量（见派单
复核意见）。``ensure_schema()``/``ensure_tables_on_connection()`` 的双入口分工、
DDL 逐条 ``conn.execute()``（禁 ``executescript``，它执行前的隐式 COMMIT 会把调用方
尚未提交的事务一起偷偷提交掉——``app/orgs/schema.py`` 等五个包同一类地雷）都照抄
``app/voice/store.py`` 的先例，不重复整套论证。

本模块只做持久化原语（建表、按行读写），不做任何业务判断（look_key 怎么算、
wardrobe 从哪取、该不该重新生成）——那些留在同包 ``character_look_views.py``/
``character_looks_ensure.py``，与 ``app.voice.store``/``app.voice.service`` 的
分工同构。``conn`` 全部由调用方传入、本模块任何函数都不自行 ``commit()``
（CLAUDE.md「不得在调用方的连接上隐式提交」）——状态转移（queued/running/ready/
failed）的事务边界由调用方（``character_looks_ensure`` 的 ``claim_or_get``/
``_mark_ready``/``_mark_failed``，各自经 ``app.db.run_write_transaction`` 开独立
事务）决定。

``insert_running``/``update_running``/``update_ready``/``update_failed`` 四个写
函数**不**像 ``get_look_view`` 那样自带 ``ensure_schema()`` 兜底：它们只会在
``app.db.run_write_transaction`` 已经持有的独立连接事务里被调用，调用方会先在
同一连接上调一次 ``ensure_tables_on_connection``；如果这四个函数自己再调
``ensure_schema()``，它在判定"当前是否已处于调用方事务里"时读的是
``app.db.get_conn()``——这是 asyncio 任务/线程局部连接，``run_write_transaction``
的操作体跑在 ``asyncio.to_thread`` 开的工作线程里，``get_conn()`` 在那里几乎总是
拿到一个全新的、不在事务中的连接，于是 ``ensure_schema()`` 会判定"不在事务里"
走独立连接分支，试图在同一调用栈里对同一个数据库文件再开一次
``BEGIN IMMEDIATE``——自己的外层事务已经持有写锁，这第二次尝试会和自己死锁。
``get_look_view`` 保留 ``ensure_schema()`` 是因为它的调用方（只读扫描）走的是
普通 ``get_conn()``，不持有独立写事务，没有这个风险。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app import db, db_schema

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS character_look_views (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        portrait_id TEXT NOT NULL,
        look_key TEXT NOT NULL,
        wardrobe_text TEXT NOT NULL DEFAULT '',
        image_path TEXT NOT NULL DEFAULT '',
        prompt TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',
        error TEXT,
        input_fingerprint TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(portrait_id, look_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_character_look_views_portrait "
    "ON character_look_views(portrait_id, status)",
)

_ensured_paths: set[str] = set()


def ensure_tables_on_connection(conn: sqlite3.Connection) -> None:
    """轻量、同连接、无副作用的建表兜底，安全用于调用方已持有事务的场景。"""
    for statement in _CREATE_STATEMENTS:
        conn.execute(statement)


def ensure_schema() -> None:
    """幂等建表；按当前 ``db.DB_PATH`` 记忆已建，避免每次调用都重跑 DDL。

    调用方选错入口不再有后果：``app.db.get_conn()`` 若已经处在调用方开的事务里，
    直接改走同连接的 ``ensure_tables_on_connection``，不开独立连接、不抢锁；
    否则保持独立连接行为（``db._run_write_transaction_once``）。
    """
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


def get_look_view(conn: Any, portrait_id: str, look_key: str) -> dict[str, Any] | None:
    ensure_schema()
    row = conn.execute(
        "SELECT * FROM character_look_views WHERE portrait_id=? AND look_key=?",
        (portrait_id, look_key),
    ).fetchone()
    return dict(row) if row else None


def insert_running(
    conn: Any, *, row_id: str, project_id: str, portrait_id: str, look_key: str,
    wardrobe_text: str, fingerprint: str, stamp: float,
) -> None:
    """调用方必须先在同一个 ``conn`` 上调用过 ``ensure_tables_on_connection``
    ——理由见模块文档。"""
    conn.execute(
        """INSERT INTO character_look_views(
               id, project_id, portrait_id, look_key, wardrobe_text, image_path, prompt,
               status, error, input_fingerprint, created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row_id, project_id, portrait_id, look_key, wardrobe_text, "", "",
         "running", None, fingerprint, stamp, stamp),
    )


def update_running(
    conn: Any, *, row_id: str, wardrobe_text: str, fingerprint: str, stamp: float,
) -> None:
    """同 ``insert_running`` 的建表前提。"""
    conn.execute(
        "UPDATE character_look_views SET status='running', wardrobe_text=?, error=NULL, "
        "input_fingerprint=?, updated_at=? WHERE id=?",
        (wardrobe_text, fingerprint, stamp, row_id),
    )


def update_ready(
    conn: Any, *, row_id: str, image_path: str, prompt: str, fingerprint: str, stamp: float,
) -> None:
    """同 ``insert_running`` 的建表前提。"""
    conn.execute(
        "UPDATE character_look_views SET status='ready', image_path=?, prompt=?, error=NULL, "
        "input_fingerprint=?, updated_at=? WHERE id=?",
        (image_path, prompt, fingerprint, stamp, row_id),
    )


def update_failed(conn: Any, *, row_id: str, error: str, stamp: float) -> None:
    """同 ``insert_running`` 的建表前提。"""
    conn.execute(
        "UPDATE character_look_views SET status='failed', error=?, updated_at=? WHERE id=?",
        (error[:2000], stamp, row_id),
    )
