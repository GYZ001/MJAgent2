"""``monitor_audit`` 的本地可靠投递缓冲——独立连接抢不到写锁时不再直接丢行。

``app.db.insert_monitor_audit`` 用独立连接 + ``BEGIN IMMEDIATE`` + ``timeout=0``
写审计行（不阻塞调用方等锁，也不在调用方连接上 commit，理由见该函数 docstring）。
但"抢不到锁就放弃"意味着安全审计可能丢行。这里补一条不占用 SQLite 写锁的本地
缓冲通道：

- ``append()``：写失败时把这一行原样追加进一个 JSONL 文件（append-only，只碰
  本地磁盘，不碰 ``manju.db``，所以永远不会跟任何 SQLite 写事务抢锁）。
- ``flush()``：由后台循环（``app.recovery.monitor_audit_flush_loop``）定期调用，
  把攒下的行一次性 ``INSERT OR IGNORE`` 进 ``monitor_audit``。行的 ``id`` 在
  ``append()`` 时就已经生成好并固定下来，重放同一行只会撞主键被 IGNORE，不会
  产生重复审计行——即使进程在"DB 已提交但文件尚未截断"之间崩溃，下一轮重试
  也只是一次无害的空操作。

``append()``/``flush()`` 用同一把 ``fcntl``/``msvcrt`` 文件锁互斥：flush 读取
到截断是一个临界区，append 的追加是另一个临界区，二者永远不会交叉，所以 flush
读取期间新追加的行不会被截断误删；flush 只在 DB 事务确认提交后才截断文件，
中途失败（例如写锁仍被别的事务占着）原样保留，下一轮循环重试。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def _buffer_path() -> Path:
    from app.config import DATA_DIR

    return DATA_DIR / "monitor_audit_pending.jsonl"


def _lock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    else:
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def _unlock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def append(
    id: str, ts: float, action: str, object_type: str, object_id: str,
    outcome: str, detail_json: str,
) -> None:
    """把一条写失败的审计行落进本地缓冲；参数顺序与 monitor_audit 表列顺序一致。

    不抛出——这是失败路径的兜底，它自己再失败也不能让调用方（业务请求）跟着炸。
    """
    row = {
        "id": id, "ts": ts, "action": action, "object_type": object_type,
        "object_id": object_id, "outcome": outcome, "detail_json": detail_json,
    }
    try:
        path = _buffer_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            _lock(handle)
            try:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                _unlock(handle)
    except Exception:  # noqa: BLE001 兜底路径自身不能再让调用方（业务请求）跟着炸
        pass  # 连本地磁盘都写不进去：没有更兜底的地方了，吞掉。


def _insert_rows(rows: list[dict[str, Any]]) -> None:
    from app.config import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT OR IGNORE INTO monitor_audit"
            "(id,ts,action,object_type,object_id,outcome,detail_json)"
            " VALUES(:id,:ts,:action,:object_type,:object_id,:outcome,:detail_json)",
            rows,
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def flush() -> int:
    """把缓冲文件里攒下的行一次性补写进 ``monitor_audit``；返回补写行数。

    只在 DB 事务确认提交后才截断文件——中途失败（例如这一刻写锁仍被别的事务
    占着）原样保留，下一轮循环重试；不丢也不重复（重放靠 id 主键
    ``INSERT OR IGNORE`` 天然幂等，见模块 docstring）。
    """
    path = _buffer_path()
    if not path.exists():
        return 0
    try:
        with path.open("r+", encoding="utf-8") as handle:
            _lock(handle)
            try:
                rows = _parse_rows(handle.read())
                if not rows:
                    return 0
                _insert_rows(rows)
                handle.seek(0)
                handle.truncate()
                return len(rows)
            finally:
                _unlock(handle)
    except (OSError, sqlite3.Error):
        return 0  # 下一轮循环重试；文件未截断，行还在。


def _parse_rows(content: str) -> list[dict[str, Any]]:
    """逐行解析；单行损坏（极端崩溃场景下的截断写入）跳过，不卡住其余行。"""
    rows = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
