"""``scripts/repair_dangling_fk_refs.py``：按 schema 声明的语义修悬挂外键。

背景（2026-09-10 计算服务器 B 实测）：``PRAGMA foreign_keys`` 是每连接开关且默认关，
``scripts/`` 里直接删库的脚本走裸 ``sqlite3.connect`` 没开它，级联动作一次都没触发，
库里攒了 7332 条悬挂引用；而 ``scripts/backup_manju_db.py`` 拿 ``foreign_key_check``
当验证判据，于是**连续五晚的备份全部被挪进隔离区**，最后一份可用备份停在 09-05。
"""
from __future__ import annotations

import sqlite3

import pytest

from scripts.repair_dangling_fk_refs import apply_plans, build_plans

SCHEMA = """
CREATE TABLE jobs(id TEXT PRIMARY KEY);
CREATE TABLE shots(id TEXT PRIMARY KEY);
CREATE TABLE artifacts(id TEXT PRIMARY KEY);
-- SET NULL + 可空：置空，行保留（provider_video_budget_claims 的形状）
CREATE TABLE claims(
    op TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    shot_id TEXT REFERENCES shots(id) ON DELETE SET NULL
);
-- CASCADE：删行（budget_reservations 的形状）
CREATE TABLE reservations(
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE
);
-- NO ACTION + NOT NULL：父行已经不在、列又不可空，这一行不可表示 → 删行
-- （gate_decisions.artifact_id 的形状）
CREATE TABLE gates(
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id)
);
"""


@pytest.fixture()
def damaged(tmp_path):
    """造一份「父行被无 pragma 连接删掉」的库——正是线上那七千条的来路。"""
    path = tmp_path / "damaged.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO jobs VALUES(?)", [("j1",), ("j2",)])
    conn.execute("INSERT INTO shots VALUES('s1')")
    conn.executemany("INSERT INTO artifacts VALUES(?)", [("a1",), ("a2",)])
    conn.execute("INSERT INTO claims VALUES('o1','j1','s1')")
    conn.execute("INSERT INTO claims VALUES('o2','j2','s1')")
    conn.execute("INSERT INTO reservations VALUES('r1','j1')")
    conn.execute("INSERT INTO reservations VALUES('r2','j2')")
    conn.execute("INSERT INTO gates VALUES('g1','a1')")
    conn.execute("INSERT INTO gates VALUES('g2','a2')")
    conn.commit()
    # 关键：不开 PRAGMA foreign_keys，删父行不会触发任何级联
    conn.execute("DELETE FROM jobs WHERE id='j1'")
    conn.execute("DELETE FROM shots WHERE id='s1'")
    conn.execute("DELETE FROM artifacts WHERE id='a1'")
    conn.commit()
    yield conn
    conn.close()


def test_pragma_off_is_what_produces_dangling_refs(tmp_path):
    """根因的独立复现：同样的删除，开与不开 pragma 结果不同。"""
    results = {}
    for pragma in (True, False):
        conn = sqlite3.connect(tmp_path / f"probe-{pragma}.db")
        if pragma:
            conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO jobs VALUES('j1')")
        conn.execute("INSERT INTO claims VALUES('o1','j1',NULL)")
        conn.commit()
        conn.execute("DELETE FROM jobs WHERE id='j1'")
        conn.commit()
        results[pragma] = (
            conn.execute("SELECT job_id FROM claims").fetchone()[0],
            len(conn.execute("PRAGMA foreign_key_check").fetchall()),
        )
        conn.close()
    assert results[True] == (None, 0), "开着 pragma：SET NULL 触发，无违规"
    assert results[False] == ("j1", 1), "关着 pragma：引用原样留着，一条违规"


def test_plans_follow_the_declared_on_delete_semantics(damaged):
    plans, refusals = build_plans(damaged)
    assert refusals == []
    by_key = {(p.table, p.column): p for p in plans}
    assert by_key[("claims", "job_id")].action == "set_null"
    assert by_key[("claims", "shot_id")].action == "set_null"
    assert by_key[("reservations", "job_id")].action == "delete"  # CASCADE
    assert by_key[("gates", "artifact_id")].action == "delete"  # NOT NULL 不可表示
    assert sum(len(p.rowids) for p in plans) == 5


def test_apply_repairs_everything_and_leaves_healthy_rows_alone(damaged):
    plans, _ = build_plans(damaged)
    apply_plans(damaged, plans)
    assert damaged.execute("PRAGMA foreign_key_check").fetchall() == []
    # 置空的行还在，只是引用清了；引用仍有效的那一列一个字没动
    assert damaged.execute("SELECT * FROM claims ORDER BY op").fetchall() == [
        ("o1", None, None), ("o2", "j2", None),
    ]
    # 删除只落在悬挂的那一行
    assert damaged.execute("SELECT id FROM reservations").fetchall() == [("r2",)]
    assert damaged.execute("SELECT id FROM gates").fetchall() == [("g2",)]


def test_apply_is_idempotent(damaged):
    """修完再跑一次不应该有任何计划——否则就是「修完没复核」那种假修好。"""
    apply_plans(damaged, build_plans(damaged)[0])
    plans, refusals = build_plans(damaged)
    assert plans == [] and refusals == []


def test_delete_wins_over_set_null_on_the_same_row(tmp_path):
    """同一行同时踩到两个外键、一个要删一个要置空时，删优先——置空一个即将被删的行
    是无意义的写，且会让「改了多少行」的账对不上。"""
    conn = sqlite3.connect(tmp_path / "both.db")
    conn.executescript("""
        CREATE TABLE a(id TEXT PRIMARY KEY);
        CREATE TABLE b(id TEXT PRIMARY KEY);
        CREATE TABLE c(
            id TEXT PRIMARY KEY,
            a_id TEXT NOT NULL REFERENCES a(id),
            b_id TEXT REFERENCES b(id) ON DELETE SET NULL
        );
    """)
    conn.execute("INSERT INTO a VALUES('a1')")
    conn.execute("INSERT INTO b VALUES('b1')")
    conn.execute("INSERT INTO c VALUES('c1','a1','b1')")
    conn.commit()
    conn.execute("DELETE FROM a WHERE id='a1'")
    conn.execute("DELETE FROM b WHERE id='b1'")
    conn.commit()
    apply_plans(conn, build_plans(conn)[0])
    assert conn.execute("SELECT * FROM c").fetchall() == []
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_healthy_database_yields_no_plans(tmp_path):
    """空集合不等于「无需检查」的反面：干净库确实应该没有计划，且不报拒绝。"""
    conn = sqlite3.connect(tmp_path / "clean.db")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO jobs VALUES('j1')")
    conn.execute("INSERT INTO claims VALUES('o1','j1',NULL)")
    conn.commit()
    assert build_plans(conn) == ([], [])
    conn.close()


def test_without_rowid_table_is_refused_not_guessed(tmp_path):
    """``foreign_key_check`` 对 WITHOUT ROWID 表不给 rowid：不猜行标识，报拒绝。

    猜错删的是别人的行，比不修危险得多。
    """
    conn = sqlite3.connect(tmp_path / "worid.db")
    conn.executescript("""
        CREATE TABLE p(id TEXT PRIMARY KEY);
        CREATE TABLE q(k TEXT PRIMARY KEY, p_id TEXT REFERENCES p(id) ON DELETE SET NULL) WITHOUT ROWID;
    """)
    conn.execute("INSERT INTO p VALUES('p1')")
    conn.execute("INSERT INTO q VALUES('k1','p1')")
    conn.commit()
    conn.execute("DELETE FROM p WHERE id='p1'")
    conn.commit()
    plans, refusals = build_plans(conn)
    assert plans == []
    assert refusals and "WITHOUT ROWID" in refusals[0]
    conn.close()
