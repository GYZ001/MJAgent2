"""道具图人脸判定结果缓存（``prop_composite_face_checks``）的表结构与读写原语。

懒建表手法照抄 ``app.video_modes.scene_state_views_store``（``app.db`` 扇入 254、
不许再加职责，该表不进 ``app/db.py`` 的核心 schema）。

缓存键是图片内容的 sha256——跨项目、跨集全局唯一，与具体道具卡/集号无关：
同一段像素只要判过一次，结论永远成立，没有"过期"语义（输入是不可变的图片
字节本身，不是会随时间变化的业务状态）。只缓存"判定成功"的结果：判定失败
（模型调用异常/返回不可解析）不写入缓存，下一次重新判定，不把一次性的网络
抖动固化成永久的"有人脸"或"无人脸"结论——这件事是调用方
``prop_composite_face_check.prop_image_has_face`` 的职责，本模块只管存取。
"""
from __future__ import annotations

import sqlite3

from app import db, db_schema

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS prop_composite_face_checks (
        content_hash TEXT PRIMARY KEY,
        has_face INTEGER NOT NULL,
        checked_at REAL NOT NULL
    )""",
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


def get_cached_has_face(content_hash: str) -> bool | None:
    """命中返回判定结果；未命中返回 ``None``——调用方对 ``None`` 一律按
    fail-closed（视为有人脸）处理，不是本函数的职责。"""
    ensure_schema()
    row = db.get_conn().execute(
        "SELECT has_face FROM prop_composite_face_checks WHERE content_hash=?",
        (content_hash,),
    ).fetchone()
    return bool(row["has_face"]) if row is not None else None


def set_cached_has_face(content_hash: str, has_face: bool) -> None:
    """独立连接自提交——纯缓存写入，不是业务状态转移的一部分（CLAUDE.md
    「诊断写入不得在调用方连接上隐式提交」的另一面：这个 commit 本身就是
    这次缓存写入的全部，没有借道调用方事务，失败也不影响判定结果本身）。"""
    ensure_schema()

    def operation(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO prop_composite_face_checks(content_hash, has_face, checked_at) "
            "VALUES(?,?,?) ON CONFLICT(content_hash) DO UPDATE SET "
            "has_face=excluded.has_face, checked_at=excluded.checked_at",
            (content_hash, 1 if has_face else 0, db.now()),
        )

    try:
        db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 缓存写入失败不阻断判定本身，下次重新判定
        return
