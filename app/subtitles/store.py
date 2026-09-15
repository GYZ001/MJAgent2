"""``subtitle_alignments`` 缓存表：镜头版本 id + 文件 sha256 → 台词对齐结果。

L2（只依赖 ``app.db``/``app.db_schema``/``app.monitor_audit_buffer``）。与
``app/audit/store.py``/``app/models_registry/schema.py`` 同一套懒建表手法，
逐行照抄，理由不重复（见两个模块文档）：``ensure_schema()`` 走独立连接
（``db._run_write_transaction_once``），按当前 ``db.DB_PATH`` 幂等记忆；
``ensure_tables_on_connection(conn)`` 供调用方已经持有写事务时同连接建表用，
逐条 ``conn.execute``，禁 ``executescript``（隐式 COMMIT 会偷偷提交调用方尚
未提交的事务，CLAUDE.md 记录的三次真实事故同一类地雷）；两者经
``app.db_schema.ensure_schema_respecting_caller_transaction`` 分派，不自行
判断调用方连接是否在事务中。

``get_alignment(conn, ...)`` 的 ``conn`` 必传、无默认值——所有权显式，调用方
决定用谁的连接；只读，不做任何写入。``put_alignment(...)`` 不接受调用方
``conn``，只走 ``db._run_write_transaction_once`` 独立连接，绝不在调用方连接
上 commit——「记一条对齐缓存」是诊断类副作用，不该跟着调用方尚未提交的合成
事务一起提交或回滚（CLAUDE.md「不得在调用方的连接上隐式提交」）。写失败时
不上抛（缓存写失败不该毁掉整个成片合成），但也不能悄悄吞掉——落一条
``logging.warning`` 带 ``shot_version_id``，返回 ``False`` 交给调用方决定要
不要继续（正常情况下继续即可：下次合成会重新算一遍，只是没有缓存加速）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from app import db, db_schema, monitor_audit_buffer

_LOGGER = logging.getLogger(__name__)

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS subtitle_alignments(
        shot_version_id TEXT PRIMARY KEY,
        media_sha256 TEXT NOT NULL,
        engine_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
)

_ensured_paths: set[str] = set()


def ensure_tables_on_connection(conn: sqlite3.Connection) -> None:
    """轻量、同连接、无副作用的建表兜底——不开新连接、不申请新锁，安全用于
    调用方已持有事务的场景。见模块文档。逐条 ``execute``，绝不能用
    ``executescript``（``tests/test_schema_guard.py`` 有 AST 守卫钉死这一点）。
    """
    for statement in _CREATE_STATEMENTS:
        conn.execute(statement)


def ensure_schema() -> None:
    """幂等建表；按当前 ``db.DB_PATH`` 记忆已建，避免每次调用都重跑 DDL。

    调用方选错入口不再有后果（见 ``app.db_schema.ensure_schema_respecting_
    caller_transaction`` 文档）：``app.db.get_conn()`` 若已经处在调用方开的
    事务里，直接改走同连接的 ``ensure_tables_on_connection``，不开独立连接、
    不抢锁；否则保持独立连接行为。
    """
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def _run_independent() -> None:
        def operation(conn: sqlite3.Connection) -> None:
            ensure_tables_on_connection(conn)

        try:
            db._run_write_transaction_once(operation)
        except Exception as exc:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞
            # 调用方；不能悄悄吞掉——落一条可观测记录，见 app/models_registry/
            # schema.py 同名 except 分支的注释，理由完全一致。
            monitor_audit_buffer.note_schema_ensure_failure(__name__, exc)
            return
        _ensured_paths.add(key)

    db_schema.ensure_schema_respecting_caller_transaction(
        db.get_conn(),
        on_caller_connection=ensure_tables_on_connection,
        run_independent=_run_independent,
    )


def get_alignment(
    conn: sqlite3.Connection, *, shot_version_id: str, media_sha256: str, model_id: str,
) -> dict[str, Any] | None:
    """只读；``shot_version_id``/``media_sha256``/``model_id`` 三者全等才命中
    （样式改动只重做 cue/渲染，不重跑 ASR；模型换了/文件变了都必须重算）。
    """
    ensure_schema()
    row = conn.execute(
        "SELECT result_json FROM subtitle_alignments"
        " WHERE shot_version_id=? AND media_sha256=? AND model_id=?",
        (shot_version_id, media_sha256, model_id),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["result_json"])


def put_alignment(
    *, shot_version_id: str, media_sha256: str, engine_id: str, model_id: str, result: dict[str, Any],
) -> bool:
    ensure_schema()

    def operation(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO subtitle_alignments"
            "(shot_version_id, media_sha256, engine_id, model_id, result_json, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (
                shot_version_id, media_sha256, engine_id, model_id,
                json.dumps(result, ensure_ascii=False), time.time(),
            ),
        )

    try:
        db._run_write_transaction_once(operation)
        return True
    except Exception:  # noqa: BLE001 缓存写失败不该毁掉成片合成，但必须可观测
        _LOGGER.warning("字幕对齐缓存写入失败：shot_version_id=%s", shot_version_id)
        return False
