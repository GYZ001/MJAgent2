"""作用域权威版本号：把「上游变没变」从 O(项目规模) 的重算改成 O(1) 的整数比较。

``app.domain.common.screenplay_ready_identity`` 是 fail-closed 围栏的缓存键，原实现为了保证
「任何一类输入变化都改变键」，每次都把本集工件、**项目级**工件、评估、完成凭证、生产修订
整套读出来重新哈希。判据是对的，代价放错了地方：成本 = 项目积累量 × 作业并发度，两个都只会
涨。2026-09-07 B 上 py-spy 实测——视频在途 128 时后端 47.8% 的 CPU 落在这条路径，连静态首页
都要 9 秒。而且缓存是反的：结论按这个指纹缓存，最贵的算键那步永远省不掉。

这里把「算」从读路径挪到写路径：四张贡献表各挂 INSERT/UPDATE/DELETE 触发器，写入时给对应
作用域的版本号加一；读取时只按主键取两行整数。触发器与写入在同一个事务里，任何代码路径都
绕不过去——比「记得调用 bump()」可靠，也是本仓库既有守卫（``guard_chapter_project`` 等）
用过的机制。

粒度刻意保持与原实现一致：**章节与集行/项目行不进版本号**，仍按原样逐字进键——
``tests/test_screenplay_ready_identity.py::test_unrelated_chapter_does_not_change_identity``
钉着「本集没读的章节不得改变结论」，而整本小说上千章，用一个粗版本号会让指纹随任何一次
章节写入而变化，下游那个 1.9 秒的重验证反而永远命中不了缓存。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

SCOPE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS authority_versions (
    scope_key TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0
);
"""

# 表名 → (作用域键表达式模板, 需要 JOIN 取作用域的来源表)
_DIRECT_SCOPE_TABLES = {
    "artifacts": "{row}.scope_type || ':' || {row}.scope_id",
    "completion_certificates": "'episode:' || {row}.scope_id",
    "production_revisions": "'episode:' || {row}.episode_id",
}
_BUMP = (
    "INSERT INTO authority_versions(scope_key, version) VALUES({key}, 1) "
    "ON CONFLICT(scope_key) DO UPDATE SET version=version+1;"
)


def _direct_triggers(table: str, expression: str) -> list[str]:
    statements = []
    for event, rows in (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",))):
        body = " ".join(_BUMP.format(key=expression.format(row=row)) for row in rows)
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS bump_authority_{table}_{event.lower()} "
            f"AFTER {event} ON {table} BEGIN {body} END;"
        )
    return statements


def _evaluation_triggers() -> list[str]:
    """评估行本身不带作用域，要顺着 artifact_id 取。"""
    statements = []
    for event, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS bump_authority_evaluations_{event.lower()} "
            f"AFTER {event} ON evaluations BEGIN "
            "INSERT INTO authority_versions(scope_key, version) "
            "SELECT artifact.scope_type || ':' || artifact.scope_id, 1 FROM artifacts AS artifact "
            f"WHERE artifact.id={row}.artifact_id "
            "ON CONFLICT(scope_key) DO UPDATE SET version=version+1; END;"
        )
    return statements


def _existing_tables(conn: Any, names: tuple[str, ...]) -> set[str]:
    marks = ",".join("?" * len(names))
    rows = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({marks})", names
    ).fetchall()
    return {str(row[0]) for row in rows}


def ensure_authority_version_triggers(conn: Any) -> None:
    """建版本表并给**当前已存在**的贡献表挂触发器；幂等，可在启动与建表处反复调用。

    完成凭证与生产修订是按需建表的，所以不能一次性写死——那两张表的 ensure 函数建完表后
    也要调本函数，否则历史库里表已存在却没有触发器，围栏会读到过期版本号而 fail open。
    """
    # 逐条 execute：``executescript`` 会隐式 COMMIT 调用方的事务，本仓库已因此三次毁掉真实数据
    # （见 CLAUDE.md「不得在调用方的连接上隐式提交」，tests/test_ensure_tables_no_implicit_commit.py 钉着）。
    conn.execute(SCOPE_TABLE_DDL.strip().rstrip(";"))
    present = _existing_tables(conn, ("artifacts", "evaluations", *_DIRECT_SCOPE_TABLES))
    statements: list[str] = []
    for table, expression in _DIRECT_SCOPE_TABLES.items():
        if table in present:
            statements.extend(_direct_triggers(table, expression))
    if "evaluations" in present and "artifacts" in present:
        statements.extend(_evaluation_triggers())
    for statement in statements:
        conn.execute(statement)


def scope_versions(conn: Any, *, episode_id: str, project_id: str) -> list[tuple[str, float]]:
    """本集与本项目当前的权威版本号；没有写入过的作用域按 0 计。

    版本表不存在时**不能**按 0 返回——那会让指纹恒定不变，围栏永远读到"上游没变"，
    是 fail open。这里改为返回一个每次都不同的哨兵：调用方的指纹随之每次变化，
    退化成"不缓存、每次重验"，慢但安全（与 ``_rows_or_empty`` 对按需建表的宽容不同：
    那些表缺失确实等价于"零条记录"，而版本表缺失等价于"不知道变没变"）。
    """
    keys = (f"episode:{episode_id}", f"project:{project_id}")
    try:
        found = dict(
            conn.execute(
                "SELECT scope_key, version FROM authority_versions WHERE scope_key IN (?,?)", keys
            ).fetchall()
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return [("authority_versions:missing", time.time())]
    return [(key, int(found.get(key) or 0)) for key in keys]


def ensure_table_with_triggers(conn: Any, table: str, ddl: str) -> None:
    """按需建表 + 立刻挂上它的版本号触发器，一个入口两件事——分成两处调用就会出现
    「表建了、触发器没挂」的窗口，而那个窗口里的写入不会被围栏看见（fail open）。
    逐条 ``execute``：``executescript`` 会隐式 COMMIT 调用方的事务（CLAUDE.md 红线，
    tests/test_ensure_tables_no_implicit_commit.py 钉着）。"""
    conn.execute(ddl)
    conn.execute(SCOPE_TABLE_DDL.strip().rstrip(";"))
    for statement in _direct_triggers(table, _DIRECT_SCOPE_TABLES[table]):
        conn.execute(statement)
