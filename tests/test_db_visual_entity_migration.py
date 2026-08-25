"""红→绿测试：``character_portraits.visual_entity_id`` 迁移 + 回填 +
``visual_entity_merges`` 审计表（docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md
§4.2、§6 P0 第5项）。

覆盖设计文档 §8 可机械判定判据里与本项直接相关的部分：
- 迁移幂等：重复执行 ``init_db()`` 不炸、结果不变。
- 既有行回填确定性：``visual_entity_id = 'bible:' || character_name``。
- 回填只补 NULL，不覆盖已经显式设置过的值（含合并后改写的情形）。
- ``visual_entity_merges`` 建表成功 + 写入路径 ``record_visual_entity_merge``
  可用、可回溯、随项目级联删除。
"""
from __future__ import annotations

import sqlite3
import threading

from app import db


def _bootstrap_legacy_db(database) -> None:
    """构造迁移前状态：只跑 SCHEMA（无 visual_entity_id 列、无审计表）。"""
    conn = sqlite3.connect(database)
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p1','项目','ready',1)"
    )
    conn.commit()
    conn.close()


def _patch_db(monkeypatch, tmp_path, database) -> None:
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())


def test_migration_adds_visual_entity_id_column_and_index(tmp_path, monkeypatch) -> None:
    database = tmp_path / "visual-entity-column.db"
    _bootstrap_legacy_db(database)
    _patch_db(monkeypatch, tmp_path, database)

    # 迁移前：列不存在（证明这确实是从"旧结构"出发的测试，不是空断言）。
    pre = sqlite3.connect(database)
    pre_cols = {row[1] for row in pre.execute("PRAGMA table_info(character_portraits)")}
    assert "visual_entity_id" not in pre_cols
    pre.close()

    db.init_db()

    conn = db.get_conn()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(character_portraits)")}
    assert "visual_entity_id" in cols
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(character_portraits)")}
    assert "idx_character_portraits_visual_entity" in indexes


def test_migration_creates_visual_entity_merges_table(tmp_path, monkeypatch) -> None:
    database = tmp_path / "visual-entity-merges-table.db"
    _bootstrap_legacy_db(database)
    _patch_db(monkeypatch, tmp_path, database)

    pre = sqlite3.connect(database)
    pre_tables = {
        row[0]
        for row in pre.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "visual_entity_merges" not in pre_tables
    pre.close()

    db.init_db()

    conn = db.get_conn()
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "visual_entity_merges" in tables
    merge_cols = {row[1] for row in conn.execute("PRAGMA table_info(visual_entity_merges)")}
    assert merge_cols == {
        "id", "project_id", "from_visual_entity_id", "to_visual_entity_id",
        "canonical_name", "merge_rule", "selected_portrait_id",
        "evidence_episode_no", "created_at",
    }


def test_migration_backfills_existing_rows_deterministically(tmp_path, monkeypatch) -> None:
    database = tmp_path / "visual-entity-backfill.db"
    _bootstrap_legacy_db(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO character_portraits(id,project_id,character_name,ep_start,created_at) "
        "VALUES('portrait_xuqing','p1','许清',1,1)"
    )
    conn.execute(
        "INSERT INTO character_portraits(id,project_id,character_name,ep_start,created_at) "
        "VALUES('portrait_lifugui','p1','李富贵',1,1)"
    )
    conn.commit()
    conn.close()
    _patch_db(monkeypatch, tmp_path, database)

    db.init_db()

    migrated = db.get_conn()
    rows = {
        row["id"]: row["visual_entity_id"]
        for row in migrated.execute(
            "SELECT id, visual_entity_id FROM character_portraits"
        ).fetchall()
    }
    assert rows == {
        "portrait_xuqing": "bible:许清",
        "portrait_lifugui": "bible:李富贵",
    }


def test_migration_is_idempotent_across_repeated_init_db(tmp_path, monkeypatch) -> None:
    database = tmp_path / "visual-entity-idempotent.db"
    _bootstrap_legacy_db(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO character_portraits(id,project_id,character_name,ep_start,created_at) "
        "VALUES('portrait_a','p1','甲',1,1)"
    )
    conn.commit()
    conn.close()
    _patch_db(monkeypatch, tmp_path, database)

    # 三次调用不炸、结果稳定——"迁移必须幂等（重复执行不炸）"。
    db.init_db()
    db.init_db()
    db.init_db()

    migrated = db.get_conn()
    value = migrated.execute(
        "SELECT visual_entity_id FROM character_portraits WHERE id='portrait_a'"
    ).fetchone()[0]
    assert value == "bible:甲"
    cols = [row[1] for row in migrated.execute("PRAGMA table_info(character_portraits)")]
    assert cols.count("visual_entity_id") == 1


def test_backfill_does_not_clobber_explicitly_set_value(tmp_path, monkeypatch) -> None:
    """回填只补 NULL；已被合并逻辑显式改写过的行，后续 init_db() 不得覆盖。"""
    database = tmp_path / "visual-entity-no-clobber.db"
    _bootstrap_legacy_db(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO character_portraits(id,project_id,character_name,ep_start,created_at) "
        "VALUES('portrait_b','p1','乙',1,1)"
    )
    conn.commit()
    conn.close()
    _patch_db(monkeypatch, tmp_path, database)

    db.init_db()
    migrated = db.get_conn()
    assert migrated.execute(
        "SELECT visual_entity_id FROM character_portraits WHERE id='portrait_b'"
    ).fetchone()[0] == "bible:乙"

    # 模拟合并后把该行改指到别的规范实体（例如 §4.2 的实体合并把历史图挂到
    # 新权威之下）。
    migrated.execute(
        "UPDATE character_portraits SET visual_entity_id='bible:乙(合并后规范名)' "
        "WHERE id='portrait_b'"
    )
    migrated.commit()

    db.init_db()
    migrated = db.get_conn()
    assert migrated.execute(
        "SELECT visual_entity_id FROM character_portraits WHERE id='portrait_b'"
    ).fetchone()[0] == "bible:乙(合并后规范名)"


def test_record_visual_entity_merge_writes_recoverable_audit_row(tmp_path, monkeypatch) -> None:
    database = tmp_path / "visual-entity-merge-write.db"
    _bootstrap_legacy_db(database)
    _patch_db(monkeypatch, tmp_path, database)
    db.init_db()

    conn = db.get_conn()
    merge_id = db.record_visual_entity_merge(
        conn,
        project_id="p1",
        from_visual_entity_id="entity:abc123",
        to_visual_entity_id="bible:李富贵",
        canonical_name="李富贵",
        merge_rule="same_batch_k_absorption",
        evidence_episode_no=10,
        selected_portrait_id="portrait_9e2209df3692",
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM visual_entity_merges WHERE id=?", (merge_id,)
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "id": merge_id,
        "project_id": "p1",
        "from_visual_entity_id": "entity:abc123",
        "to_visual_entity_id": "bible:李富贵",
        "canonical_name": "李富贵",
        "merge_rule": "same_batch_k_absorption",
        "selected_portrait_id": "portrait_9e2209df3692",
        "evidence_episode_no": 10,
        "created_at": row["created_at"],
    }
    assert isinstance(row["created_at"], float)


def test_visual_entity_merges_row_is_append_only_on_repeated_calls(tmp_path, monkeypatch) -> None:
    database = tmp_path / "visual-entity-merge-append.db"
    _bootstrap_legacy_db(database)
    _patch_db(monkeypatch, tmp_path, database)
    db.init_db()

    conn = db.get_conn()
    for _ in range(2):
        db.record_visual_entity_merge(
            conn,
            project_id="p1",
            from_visual_entity_id="entity:same",
            to_visual_entity_id="bible:许清",
            canonical_name="许清",
            merge_rule="same_batch_k_absorption",
            evidence_episode_no=6,
        )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM visual_entity_merges WHERE from_visual_entity_id='entity:same'"
    ).fetchone()[0]
    assert count == 2


def test_visual_entity_merges_cascades_on_project_delete(tmp_path, monkeypatch) -> None:
    database = tmp_path / "visual-entity-merge-cascade.db"
    _bootstrap_legacy_db(database)
    _patch_db(monkeypatch, tmp_path, database)
    db.init_db()

    conn = db.get_conn()
    db.record_visual_entity_merge(
        conn,
        project_id="p1",
        from_visual_entity_id="entity:cascade",
        to_visual_entity_id="bible:待删除",
        canonical_name="待删除",
        merge_rule="same_batch_k_absorption",
        evidence_episode_no=1,
    )
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM visual_entity_merges WHERE project_id='p1'"
    ).fetchone()[0] == 1

    conn.execute("DELETE FROM projects WHERE id='p1'")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM visual_entity_merges WHERE project_id='p1'"
    ).fetchone()[0] == 0
