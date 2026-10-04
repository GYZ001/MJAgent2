"""定妆照肤色局部色块核验结果缓存（``portrait_skin_blush_audits``）的表结构与
读写原语。懒建表手法照抄 ``app.video_modes.prop_composite_face_store``（``app.db``
扇入 254、不许再加职责，该表不进 ``app/db.py`` 的核心 schema）。

缓存键是「图片内容 sha256 + 规则版本」的组合，不是只有内容哈希：规则文案或
判定提示词改了（``app.portraits.portrait_skin_blush.PORTRAIT_SKIN_BLUSH_RULE_VERSION``
递增）之后，旧版本判过的结果不能被新规则误当作"已核验通过"，必须重新判一次；
同一张图在同一规则版本下结论不会变，没有"过期"语义。只缓存"判定成功"
（``checked=True``）的结果：判定失败（模型调用异常/返回不可解析）不写入缓存，
下一次重新判定，不把一次性的网络抖动固化成永久结论——这件事是调用方
``app.domain.bible_ops.portrait_skin_blush_audit`` 的职责，本模块只管存取。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from app import db, db_schema

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS portrait_skin_blush_audits (
        content_hash TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        project_id TEXT NOT NULL,
        character_name TEXT NOT NULL,
        portrait_id TEXT NOT NULL DEFAULT '',
        image_path TEXT NOT NULL,
        has_local_color INTEGER NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        checked_at REAL NOT NULL,
        PRIMARY KEY (content_hash, rule_version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_portrait_skin_blush_audits_project "
    "ON portrait_skin_blush_audits(project_id, character_name)",
)

_ensured_paths: set[str] = set()


def content_sha256(path: str) -> str:
    """缓存键的另一半。存量核验（``portrait_skin_blush_audit``）与生成流程
    自带判定回填（``portrait_skin_blush_check.generate_front_full_with_skin_check``）
    必须用同一份哈希逻辑，否则同一张图在两条路径算出两个哈希、缓存永远不命中。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


def get_cached_audit(content_hash: str, rule_version: str) -> dict | None:
    """命中返回 ``{"has_local_color", "reason", "checked_at"}``；未命中返回
    ``None``——调用方对 ``None`` 一律重新真实判定一次，不是本函数的职责。"""
    ensure_schema()
    row = db.get_conn().execute(
        "SELECT has_local_color, reason, checked_at FROM portrait_skin_blush_audits "
        "WHERE content_hash=? AND rule_version=?",
        (content_hash, rule_version),
    ).fetchone()
    if row is None:
        return None
    return {
        "has_local_color": bool(row["has_local_color"]),
        "reason": row["reason"] or "",
        "checked_at": row["checked_at"],
    }


def set_cached_audit(
    *, content_hash: str, rule_version: str, project_id: str, character_name: str,
    portrait_id: str, image_path: str, has_local_color: bool, reason: str,
) -> None:
    """独立连接自提交——纯缓存写入，不是业务状态转移的一部分（CLAUDE.md
    「诊断写入不得在调用方连接上隐式提交」的另一面：这个 commit 本身就是这次
    缓存写入的全部，没有借道调用方事务，失败也不影响判定结果本身）。"""
    ensure_schema()

    def operation(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO portrait_skin_blush_audits(content_hash, rule_version, project_id, "
            "character_name, portrait_id, image_path, has_local_color, reason, checked_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(content_hash, rule_version) DO UPDATE SET "
            "project_id=excluded.project_id, character_name=excluded.character_name, "
            "portrait_id=excluded.portrait_id, image_path=excluded.image_path, "
            "has_local_color=excluded.has_local_color, reason=excluded.reason, "
            "checked_at=excluded.checked_at",
            (content_hash, rule_version, project_id, character_name, portrait_id,
             image_path, 1 if has_local_color else 0, reason, db.now()),
        )

    try:
        db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 缓存写入失败不阻断判定本身，下次重新判定
        return
