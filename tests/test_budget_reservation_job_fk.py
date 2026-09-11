"""台账外键可空 + ``ON DELETE SET NULL`` 的守卫（budget_reservations / gate_decisions）。

用户 2026-09-10 拍板：审计台账独立于流水线产物存在，一行都不删。此前两张表的建表
语句写的是 ``NOT NULL``（一张还带 ``ON DELETE CASCADE``），与
``app/media_exec/enqueue.py`` 的「budget_reservations 审计台账完整」、以及
``scripts/reset_project_episodes.py`` 刻意保留台账的清除清单直接打架；因为删库脚本的
裸连接没开 ``PRAGMA foreign_keys``，级联从没触发过，脚本那侧的意图悄悄赢了，代价是
7332 条悬挂引用把每晚的备份全部打进隔离区。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from scripts.migrate_budget_reservation_job_fk import (
    TABLES,
    LedgerTable,
    already_migrated,
    fk_shape,
    migrate,
)

BY_NAME = {t.name: t for t in TABLES}

LEGACY_SCHEMA = """
CREATE TABLE jobs(id TEXT PRIMARY KEY);
CREATE TABLE artifacts(id TEXT PRIMARY KEY);
CREATE TABLE workflow_runs(id TEXT PRIMARY KEY);
CREATE TABLE budget_reservations (
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
);
CREATE TABLE gate_decisions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    run_id TEXT,
    gate_key TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    accepted_risk TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id),
    FOREIGN KEY(run_id) REFERENCES workflow_runs(id)
);
CREATE UNIQUE INDEX idx_gate_decisions_storyboard_pack_release
    ON gate_decisions(artifact_id) WHERE gate_key='storyboard_pack_release';
"""


def _legacy_db(tmp_path: Path, name: str = "legacy.db") -> sqlite3.Connection:
    """父行各留一个、子行各两条（一条有效、一条悬挂）——线上那批数据的形状。"""
    conn = sqlite3.connect(tmp_path / name)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO jobs VALUES('j1')")
    conn.execute("INSERT INTO artifacts VALUES('a1')")
    conn.executemany(
        "INSERT INTO budget_reservations VALUES(?,?,?,?,?,?,?,NULL,NULL)",
        [("r1", "j1", "episode", "ep1", 12.0, "settled", 1.0),
         ("r2", "j_gone", "episode", "ep1", 12.0, "settled", 2.0)],
    )
    conn.executemany(
        "INSERT INTO gate_decisions VALUES(?,?,NULL,?,'approve','bot','ok',NULL,?)",
        [("g1", "a1", "storyboard_pack_release", 1.0),
         ("g2", "a_gone", "storyboard_pack_release", 2.0)],
    )
    conn.commit()
    return conn


def _normalise(sql: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s*--.*", "", sql)).strip()


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_schema_declaration_in_db_py_matches_the_migration_target(table: LedgerTable) -> None:
    """两份声明必须逐字一致——分叉的那一种会长成「只在某些环境复现」的故障。"""
    schema = (Path(__file__).resolve().parent.parent / "app" / "db.py").read_text(encoding="utf-8")
    block = re.search(rf"CREATE TABLE IF NOT EXISTS {table.name} \((.*?)\n\);", schema, re.S)
    assert block, f"app/db.py 里找不到 {table.name} 的建表语句"
    assert _normalise(block.group(1)) == _normalise(table.new_body)


@pytest.mark.parametrize("name,legacy", [("budget_reservations", "CASCADE"), ("gate_decisions", "NO ACTION")])
def test_legacy_shape_is_detected_and_migrated(tmp_path: Path, name: str, legacy: str) -> None:
    conn = _legacy_db(tmp_path)
    table = BY_NAME[name]
    assert fk_shape(conn, table) == (True, legacy)
    assert not already_migrated(conn, table)
    assert migrate(conn, table) == 2
    assert fk_shape(conn, table) == (False, "SET NULL")
    conn.close()


def test_migration_moves_every_row_including_the_dangling_one(tmp_path: Path) -> None:
    """迁移只改结构，一行都不删——这正是用户拍板的那一侧。"""
    conn = _legacy_db(tmp_path)
    for table in TABLES:
        migrate(conn, table)
    assert conn.execute("SELECT id, job_id FROM budget_reservations ORDER BY id").fetchall() == [
        ("r1", "j1"), ("r2", "j_gone"),
    ]
    assert conn.execute("SELECT id, artifact_id FROM gate_decisions ORDER BY id").fetchall() == [
        ("g1", "a1"), ("g2", "a_gone"),
    ]
    conn.close()


def test_partial_unique_index_survives_the_rebuild(tmp_path: Path) -> None:
    """重建会连索引一起删掉。``idx_gate_decisions_storyboard_pack_release``
    （一个产物只能有一条发布决议）必须原样回来，否则是拿真实约束换备份通过。"""
    conn = _legacy_db(tmp_path)
    migrate(conn, BY_NAME["gate_decisions"])
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='gate_decisions'")}
    assert "idx_gate_decisions_storyboard_pack_release" in names
    conn.execute("INSERT INTO artifacts VALUES('a2')")
    conn.execute("INSERT INTO gate_decisions VALUES('g3','a2',NULL,'storyboard_pack_release','approve','bot','ok',NULL,3.0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO gate_decisions VALUES('g4','a2',NULL,'storyboard_pack_release','approve','bot','ok',NULL,4.0)")
    conn.close()


def test_nulled_orphans_do_not_weaken_that_unique_index(tmp_path: Path) -> None:
    """置空的孤儿行可以有很多条：SQLite 里 NULL 不与任何值相等，因此它们既不互相
    冲突、也不影响任何**存活**产物的唯一性。这正是「删掉 270 行」那条理由不成立的地方。"""
    conn = _legacy_db(tmp_path)
    migrate(conn, BY_NAME["gate_decisions"])
    conn.execute("UPDATE gate_decisions SET artifact_id=NULL")
    conn.execute("INSERT INTO gate_decisions VALUES('g5',NULL,NULL,'storyboard_pack_release','approve','bot','ok',NULL,5.0)")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM gate_decisions WHERE artifact_id IS NULL").fetchone()[0] == 3
    conn.close()


def test_deleting_a_parent_now_keeps_the_ledger_row(tmp_path: Path) -> None:
    """迁移之后的真实语义：父行删掉，台账行留下、引用置空。"""
    conn = _legacy_db(tmp_path)
    for table in TABLES:
        migrate(conn, table)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM jobs WHERE id='j1'")
    conn.execute("DELETE FROM artifacts WHERE id='a1'")
    conn.commit()
    assert conn.execute("SELECT job_id FROM budget_reservations WHERE id='r1'").fetchone()[0] is None
    assert conn.execute("SELECT artifact_id FROM gate_decisions WHERE id='g1'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM gate_decisions").fetchone()[0] == 2
    conn.close()


def test_old_shape_destroys_or_blocks_instead(tmp_path: Path) -> None:
    """红绿对照：不迁移、只把 pragma 打开，同样的删除会把预留行一起删掉，删产物则
    被直接拒绝、脚本中途失败——这正是「只开 pragma」那条路不能走的原因。"""
    conn = _legacy_db(tmp_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM jobs WHERE id='j1'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM artifacts WHERE id='a1'")
    conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    conn = _legacy_db(tmp_path)
    for table in TABLES:
        migrate(conn, table)
        assert already_migrated(conn, table)
    conn.close()


class _DropsOneRow(sqlite3.Connection):
    """搬运时少搬一行，用来触发行数复核失败。``sqlite3.Connection.execute`` 是只读
    属性，patch 不上，所以走 ``connect(factory=...)`` 换一个子类。"""

    def execute(self, sql, *args):  # type: ignore[override]
        if sql.startswith("INSERT INTO budget_reservations_migrating"):
            sql = sql.replace("FROM budget_reservations", "FROM budget_reservations WHERE id='r1'")
        return super().execute(sql, *args)


def test_row_count_mismatch_rolls_back(tmp_path: Path) -> None:
    """复核不过就整体回滚——重建到一半留下半张表比不迁移危险得多。"""
    _legacy_db(tmp_path).close()
    conn = sqlite3.connect(tmp_path / "legacy.db", factory=_DropsOneRow)
    with pytest.raises(RuntimeError, match="行数不一致"):
        migrate(conn, BY_NAME["budget_reservations"])
    conn.close()
    check = sqlite3.connect(tmp_path / "legacy.db")  # 换一条连接读盘，不读自己写的事务
    assert check.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 2
    assert fk_shape(check, BY_NAME["budget_reservations"]) == (True, "CASCADE"), "回滚后必须还是原来的表"
    check.close()
