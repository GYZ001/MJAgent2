"""极小的 sqlite 探测辅助：某表/某列是否存在。零依赖，被本包多数模块用作基础库。
"""

from __future__ import annotations


def _has_column(conn, table: str, column: str) -> bool:
    """Support focused tests/old snapshots before app.db runs migrations."""
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _has_table(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None

