"""客户反馈只读查询——供成片台展示，不承担写路径（写路径见
``app.delivery.add_customer_feedback``）。

拆成独立模块而不是塞进 ``app/orchestration/api.py`` 或 ``app/delivery.py``：
两者都在各自的 ``[baseline.line_count]`` 上限（分别是 1744、1460 行），新增
查询逻辑没有余量。``app.evaluations`` 已经是 L4，``app.orchestration.api``
（L5）依赖它不构成分层上行。
"""
from __future__ import annotations

from typing import Any

from app.db import get_conn


def recent_customer_feedback(episode_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """本集最近的客户反馈，按提交时间倒序：时间、提交人、内容、评分。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, created_at, created_by, message, rating, issue_code
             FROM customer_feedback WHERE episode_id=?
            ORDER BY created_at DESC LIMIT ?""",
        (episode_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]
