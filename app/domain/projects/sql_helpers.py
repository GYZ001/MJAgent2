"""SQLite ``IN (...)`` 子句的分块通用原语。

只被 :mod:`app.domain.projects.evidence` 用来批量删除/查询 Harness 证据行——
SQLite 单条语句的绑定参数上限决定了大批量 ``IN`` 必须分块执行，这里与具体删的
是哪张表完全无关，是纯 SQL 拼接原语，因此单独成一个零业务依赖的文件。
"""
from __future__ import annotations

from collections.abc import Iterable

_SQLITE_IN_CHUNK_SIZE = 400


def _in_chunks(values: Iterable[object], size: int | None = None):
    size = size or _SQLITE_IN_CHUNK_SIZE
    items = list(values)
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _marks(values: list[object]) -> str:
    return ",".join("?" for _ in values)


def _ids_by_in(conn, sql_template: str, values: Iterable[object]) -> set[str]:
    ids: set[str] = set()
    for chunk in _in_chunks(values):
        ids.update(
            row["id"] for row in conn.execute(
                sql_template.format(marks=_marks(chunk)),
                chunk,
            ).fetchall()
        )
    return ids


def _execute_by_in(conn, sql_template: str, values: Iterable[object]) -> int:
    affected = 0
    for chunk in _in_chunks(values):
        cursor = conn.execute(sql_template.format(marks=_marks(chunk)), chunk)
        affected += max(0, cursor.rowcount)
    return affected


def _scope_ids(conn, table: str, *, scope_ids: Iterable[str],
               scope_prefix: str, id_column: str = "id") -> set[str]:
    ids = {
        row[id_column] for row in conn.execute(
            f"SELECT {id_column} FROM {table} WHERE scope_id LIKE ?",
            (f"{scope_prefix}:%",),
        ).fetchall()
    }
    ids.update(_ids_by_in(
        conn,
        f"SELECT {id_column} AS id FROM {table} WHERE scope_id IN ({{marks}})",
        scope_ids,
    ))
    return ids


def _delete_scope_rows(conn, table: str, *, scope_ids: Iterable[str],
                       scope_prefix: str) -> None:
    conn.execute(f"DELETE FROM {table} WHERE scope_id LIKE ?", (f"{scope_prefix}:%",))
    _execute_by_in(
        conn,
        f"DELETE FROM {table} WHERE scope_id IN ({{marks}})",
        scope_ids,
    )
