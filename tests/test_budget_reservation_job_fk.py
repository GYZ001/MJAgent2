"""``budget_reservations.job_id`` 可空 + ``ON DELETE SET NULL`` 的守卫。

用户 2026-09-10 拍板：审计台账独立于 job 存在，一行都不删。此前建表语句写的是
``NOT NULL ... ON DELETE CASCADE``（「预留跟着 job 一起死」），与
``app/media_exec/enqueue.py`` 的「budget_reservations 审计台账完整」直接打架；因为
删库脚本的裸连接没开 ``PRAGMA foreign_keys``，级联从没触发过，脚本那侧的意图悄悄
赢了，代价是 2118 条悬挂引用把每晚的备份全部打进隔离区。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from scripts.migrate_budget_reservation_job_fk import (
    NEW_TABLE_SQL,
    already_migrated,
    job_fk_shape,
    migrate,
)

OLD_TABLE_SQL = """CREATE TABLE budget_reservations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    amount_cny REAL NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    settled_at REAL,
    actual_cost_cny REAL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
)"""


def _legacy_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY)")
    conn.execute(OLD_TABLE_SQL)
    conn.execute("INSERT INTO jobs VALUES('j1')")
    conn.executemany(
        "INSERT INTO budget_reservations VALUES(?,?,?,?,?,?,?,NULL,NULL)",
        [("r1", "j1", "episode", "ep1", 12.0, "settled", 1.0),
         ("r2", "j_gone", "episode", "ep1", 12.0, "settled", 2.0)],
    )
    conn.commit()
    return conn


def test_schema_declaration_in_db_py_matches_the_migration_target() -> None:
    """两份声明必须逐字一致——分叉的那一种会长成「只在某些环境复现」的故障。"""
    schema = (Path(__file__).resolve().parent.parent / "app" / "db.py").read_text(encoding="utf-8")
    block = re.search(r"CREATE TABLE IF NOT EXISTS budget_reservations \((.*?)\n\);", schema, re.S)
    assert block, "app/db.py 里找不到 budget_reservations 的建表语句"
    declared = re.sub(r"\s*--.*", "", block.group(1))
    target = NEW_TABLE_SQL.split("(", 1)[1].rsplit(")", 1)[0]
    assert _normalise(declared) == _normalise(target)


def _normalise(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_legacy_shape_is_detected_and_migrated(tmp_path: Path) -> None:
    conn = _legacy_db(tmp_path)
    assert job_fk_shape(conn) == (True, "CASCADE")
    assert not already_migrated(conn)
    assert migrate(conn) == 2
    assert job_fk_shape(conn) == (False, "SET NULL")
    assert already_migrated(conn)
    conn.close()


def test_migration_moves_every_row_including_the_dangling_one(tmp_path: Path) -> None:
    """迁移只改结构，一行都不删——这正是用户拍板的那一侧。"""
    conn = _legacy_db(tmp_path)
    migrate(conn)
    assert conn.execute("SELECT id, job_id FROM budget_reservations ORDER BY id").fetchall() == [
        ("r1", "j1"), ("r2", "j_gone"),
    ]
    conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    conn = _legacy_db(tmp_path)
    migrate(conn)
    assert already_migrated(conn)
    # 第二次不该再被当成待迁移——调用方据此跳过
    assert job_fk_shape(conn) == (False, "SET NULL")
    conn.close()


def test_deleting_a_job_now_keeps_the_ledger_row(tmp_path: Path) -> None:
    """迁移之后的真实语义：job 删掉，台账行留下、引用置空。"""
    conn = _legacy_db(tmp_path)
    migrate(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM jobs WHERE id='j1'")
    conn.commit()
    assert conn.execute("SELECT job_id FROM budget_reservations WHERE id='r1'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 2
    conn.close()


def test_old_shape_would_have_deleted_the_ledger_row(tmp_path: Path) -> None:
    """红绿对照：不迁移、只把 pragma 打开，同样的删除会把台账行一起删掉——
    这正是「直接开 pragma」那条路不能走的原因。"""
    conn = _legacy_db(tmp_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM jobs WHERE id='j1'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 1
    conn.close()


def test_multiple_nulled_rows_coexist_under_the_unique_index(tmp_path: Path) -> None:
    """UNIQUE 列允许多个 NULL（SQLite 语义），所以大批台账行被置空不会互相冲突。"""
    conn = _legacy_db(tmp_path)
    migrate(conn)
    conn.execute("UPDATE budget_reservations SET job_id=NULL")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM budget_reservations WHERE job_id IS NULL").fetchone()[0] == 2
    conn.close()


class _DropsOneRow(sqlite3.Connection):
    """搬运时少搬一行，用来触发行数复核失败。``sqlite3.Connection.execute`` 是只读
    属性，patch 不上，所以走 ``connect(factory=...)`` 换一个子类。"""

    def execute(self, sql, *args):  # type: ignore[override]
        if sql.startswith("INSERT INTO budget_reservations_new"):
            sql = sql.replace("FROM budget_reservations", "FROM budget_reservations WHERE id='r1'")
        return super().execute(sql, *args)


def test_row_count_mismatch_rolls_back(tmp_path: Path) -> None:
    """复核不过就整体回滚——重建到一半留下半张表比不迁移危险得多。"""
    _legacy_db(tmp_path).close()
    conn = sqlite3.connect(tmp_path / "legacy.db", factory=_DropsOneRow)
    with pytest.raises(RuntimeError, match="行数不一致"):
        migrate(conn)
    conn.close()
    check = sqlite3.connect(tmp_path / "legacy.db")  # 换一条连接读盘，不读自己写的事务
    assert check.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 2
    assert job_fk_shape(check) == (True, "CASCADE"), "回滚后必须还是原来的表"
    check.close()
