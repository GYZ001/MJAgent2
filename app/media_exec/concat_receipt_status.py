"""合片操作的持久化终态查询——供 mix-status 投影，不读进程内存。

2026-10-01 对抗式复查发现：``concat_state``（``task_registry`` 在途标记 + 进程内
``_last_error`` 字典）在后端重启/崩溃后清零，而 ``localStorage`` 里持久化的合成
幂等键不会。若成片台只看 ``concat_state`` 判断"本轮合成是否完成"，重启瞬间就会
把"从未真正完成、旧任务已经死亡"误判成"已完成"，弹出虚假的成功提示——界面依然
展示旧成片，却告诉用户"合成完成"。这里改读事务化落盘的
``concat_operation_receipts.status``：它由 ``claim_concat_operation``/
``release_concat_operation``/晋级流程显式写入终态（``succeeded``/``failed``），
不随进程重启丢失；停在 ``running`` 就诚实地表示"还没有定论"，不冒充成功。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def latest_receipt_outcome(conn, episode_id: str) -> dict[str, Any] | None:
    """返回该集最近一次合片 receipt 的持久化状态，没有任何 receipt 时返回 ``None``。

    ``conn`` 必须由调用方传入（Ownership：不在这里隐式 get_conn），避免和调用方
    的事务/连接身份脱节。表可能还不存在（这一集从没发起过合成），用
    ``sqlite3.OperationalError`` 兜底而不是提前建表——这里只做只读投影。
    """
    try:
        row = conn.execute(
            "SELECT status, result_json FROM concat_operation_receipts "
            "WHERE episode_id=? ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    status = str(row["status"])
    error: str | None = None
    if status == "failed":
        try:
            parsed = json.loads(str(row["result_json"] or "{}"))
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            raw = parsed.get("error")
            error = str(raw) if raw else None
    return {"status": status, "error": error}


__all__ = ["latest_receipt_outcome"]
