"""存量分镜「按现行规则复核」预览快照存储——懒建表，照 ``app/props/
card_pending_store.py`` 的写法连同其前提：``ensure_tables_on_connection(conn)``
跑在调用方已持有的连接/事务上，只用逐条 ``conn.execute()``（禁
``executescript``，它执行前的隐式 COMMIT 会把调用方尚未提交的事务一起偷偷
提交掉——CLAUDE.md 记录的三次真实事故同一类地雷）；``ensure_schema()`` 经
``app.db_schema.ensure_schema_respecting_caller_transaction`` 按「调用方此刻
是否已在事务里」分派到同连接或独立连接，不自行判断（``tests/
test_schema_guard.py`` 对这套手法的 AST 守卫同样覆盖本模块）。

## 为什么需要这张表（完整背景见 ``app.domain.storyboard_ops.
prop_continuity_review`` 模块 docstring）

GET 预览与 POST 重写曾经各自独立跑一遍整集复核——复核模型有随机性，POST
收到的「确认重写段号」是针对预览那一刻的结果，但 POST 自己又重新跑一遍复核
产生第二份结果，两份结果不保证相同，「确认集合必须是服务端此刻待重写集合
的子集」在真实使用中必然经常失败（2026-10-05《顾念长安》第 1 集实测：预览
判出 29 段违规，POST 复核时第 13 段没再被判出，整批 409，一段没改，还多花
一整集复核的耗时）。本表把 GET 预览的结果原样存一份快照（每段违规清单 +
预览那一刻的 ``prompt_text`` 哈希），POST 只认这份快照，不再调用复核模型；
每个确认段落仍会核对「当前哈希」与快照哈希是否一致，正文在预览后被别的
操作改过的段落会被跳过并给出可见原因，不会拿一份过期的违规清单去瞎改当前
正文。

## 独立连接的理由

保存快照不是任何业务事务的一部分（预览本身是一次只读长调用，不持有任何
写锁），``save_snapshot`` 经 ``app.db.run_write_transaction``（独立连接 +
``BEGIN IMMEDIATE`` + 提交后关闭）落盘，不占用调用方 ``get_conn()`` 的连接，
也不会在调用方的事务上隐式提交（CLAUDE.md「不得在调用方的连接上隐式提交」）。
读（``load_snapshot``）走调用方传入的连接——读不改变任何状态，不需要独立
连接；读之前调用 ``ensure_schema()`` 兜底建表，和 ``card_pending_store.
get_pending`` 同一先例。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app import db, db_schema
from app.db import new_id, now

#: 快照有效期：24 小时。理由——预览到确认重写通常在同一工作时段内完成
#: （CLAUDE.md「一集做完再下一集」：单集复核是一次性任务，不会跨天悬置）；
#: 超过这个窗口，分镜更可能已经被别的操作（单段「修订本段」/另一次预览）
#: 动过，继续信任一份陈旧快照的违规清单比重新预览风险更高。过期后 POST
#: 直接 409 并提示重新预览，不自动续期（CLAUDE.md「拦住用户时必须给出路」：
#: 错误信息里已经给出下一步该做什么）。
SNAPSHOT_TTL_S = 24 * 60 * 60.0

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS prop_continuity_review_snapshots (
        id TEXT PRIMARY KEY,
        episode_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        segments_json TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_prop_continuity_review_snapshots_episode "
    "ON prop_continuity_review_snapshots(episode_id, created_at DESC)",
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


async def save_snapshot(*, episode_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    """落一条预览快照；``segments`` 每项须含 ``segment_no``/``shot_id``/
    ``prompt_text_hash``/``violations``（后者是 ``ProseViolation.model_dump
    (mode="json")`` 的列表），由调用方（``prop_continuity_review.
    build_review_snapshot_segments``）组装，本函数不做结构校验。返回
    ``{"id", "created_at", "expires_at"}``。独立连接写入，见模块 docstring。"""
    snapshot_id = new_id("pcrsnap")
    stamp = now()
    expires_at = stamp + SNAPSHOT_TTL_S
    payload = json.dumps(segments, ensure_ascii=False)

    def operation(conn: sqlite3.Connection) -> None:
        ensure_tables_on_connection(conn)
        conn.execute(
            "INSERT INTO prop_continuity_review_snapshots(id,episode_id,created_at,expires_at,segments_json) "
            "VALUES(?,?,?,?,?)",
            (snapshot_id, episode_id, stamp, expires_at, payload),
        )

    await db.run_write_transaction(operation)
    return {"id": snapshot_id, "created_at": stamp, "expires_at": expires_at}


def load_snapshot(conn: Any, *, snapshot_id: str, episode_id: str) -> dict[str, Any] | None:
    """按 ``episode_id``+``snapshot_id`` 读取快照；不存在或不属于该集返回
    ``None``（调用方据此判 404，不泄露「该 id 属于别的集」这个事实）。读走
    调用方传入的连接，见模块 docstring「独立连接的理由」。"""
    ensure_schema()
    row = conn.execute(
        "SELECT * FROM prop_continuity_review_snapshots WHERE id=? AND episode_id=?",
        (snapshot_id, episode_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"], "episode_id": row["episode_id"],
        "created_at": row["created_at"], "expires_at": row["expires_at"],
        "segments": json.loads(row["segments_json"]),
    }


def is_expired(snapshot: dict[str, Any]) -> bool:
    return now() > float(snapshot["expires_at"])
