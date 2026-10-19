"""存量道具复核表升级：保留记录、恢复抢占/结果写回、遵守调用方事务。"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from app import db
from app.props import card_audit, card_audit_store


_LEGACY_SCHEMA = """CREATE TABLE prop_card_rule_audits (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    prop_name TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    old_appearance TEXT,
    new_appearance TEXT,
    removed_clauses_json TEXT,
    removed_aliases_json TEXT,
    reimaged INTEGER NOT NULL DEFAULT 0,
    feature_shortfall INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(project_id, prop_name)
)"""


@pytest.fixture
def legacy_conn():
    conn = db.get_conn()
    conn.execute(_LEGACY_SCHEMA)
    conn.execute(
        """INSERT INTO prop_card_rule_audits(
            id, project_id, prop_name, rules_version, status, attempts,
            old_appearance, new_appearance, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("legacy", "project", "白瓷杯", "v1", "ready", 2, "白色，带柄", "白色，带柄", 1.0, 2.0),
    )
    conn.commit()
    return conn


@pytest.mark.parametrize("existing_columns", [(), ("claim_token",), ("doubts_json",), ("claim_token", "doubts_json")])
def test_migrate_legacy_and_partial_schemas_preserves_data_and_is_idempotent(legacy_conn, existing_columns):
    for column in existing_columns:
        legacy_conn.execute(f"ALTER TABLE prop_card_rule_audits ADD COLUMN {column} TEXT")
        legacy_conn.execute(f"UPDATE prop_card_rule_audits SET {column}=?", (f"existing-{column}",))
    legacy_conn.commit()
    original = dict(legacy_conn.execute("SELECT * FROM prop_card_rule_audits").fetchone())

    card_audit_store.ensure_tables_on_connection(legacy_conn)
    card_audit_store.ensure_tables_on_connection(legacy_conn)
    legacy_conn.commit()

    with closing(sqlite3.connect(db.DB_PATH)) as observer:
        observer.row_factory = sqlite3.Row
        migrated = dict(observer.execute("SELECT * FROM prop_card_rule_audits").fetchone())
        assert {key: migrated[key] for key in original} == original
        assert {"claim_token", "doubts_json"} <= migrated.keys()
        for column in {"claim_token", "doubts_json"} - set(existing_columns):
            assert migrated[column] is None
        assert observer.execute("SELECT count(*) FROM prop_card_rule_audits").fetchone()[0] == 1
        assert observer.execute("SELECT count(*) FROM prop_card_audit_doubt_decisions").fetchone()[0] == 0


async def test_legacy_schema_supports_claim_completion_and_fences_stale_results(legacy_conn):
    first, should_run = await card_audit.claim_audit(project_id="project", prop_name="玉格", rules_version="v2")
    assert should_run is True
    second, should_run = await card_audit.claim_audit(project_id="project", prop_name="玉格", rules_version="v3")
    assert should_run is True
    assert first["claim_token"] != second["claim_token"]
    result = {
        "prop_name": "玉格", "old_appearance": "空的架子", "new_appearance": "空的架子",
        "removed_clauses": [], "removed_aliases": [], "reimaged": False, "feature_shortfall": False,
        "doubts": [{"kind": "alias", "alias": "架子", "reason_a": "待确认"}],
    }
    await card_audit._mark_audit_ready(row_id=first["id"], claim_token=first["claim_token"], result=result)
    row = card_audit_store.get_audit(legacy_conn, project_id="project", prop_name="玉格")
    assert row["status"] == "running" and row["rules_version"] == "v3"
    await card_audit._mark_audit_ready(row_id=second["id"], claim_token=second["claim_token"], result=result)

    with closing(sqlite3.connect(db.DB_PATH)) as observer:
        observer.row_factory = sqlite3.Row
        row = observer.execute("SELECT * FROM prop_card_rule_audits WHERE prop_name='玉格'").fetchone()
        assert row["status"] == "ready" and row["claim_token"] == second["claim_token"]
        assert "待确认" in row["doubts_json"]


def test_migration_rolls_back_with_caller_and_retries_after_rollback(legacy_conn):
    legacy_conn.execute("BEGIN IMMEDIATE")
    legacy_conn.execute("UPDATE prop_card_rule_audits SET new_appearance='未提交改动'")
    card_audit_store.ensure_schema()
    assert legacy_conn.in_transaction
    assert {"claim_token", "doubts_json"} <= {
        row[1] for row in legacy_conn.execute("PRAGMA table_info(prop_card_rule_audits)")
    }

    with closing(sqlite3.connect(db.DB_PATH)) as observer:
        assert observer.execute("SELECT new_appearance FROM prop_card_rule_audits").fetchone()[0] == "白色，带柄"
        assert "claim_token" not in {row[1] for row in observer.execute("PRAGMA table_info(prop_card_rule_audits)")}

    legacy_conn.rollback()
    assert "claim_token" not in {row[1] for row in legacy_conn.execute("PRAGMA table_info(prop_card_rule_audits)")}
    card_audit_store.ensure_schema()
    with closing(sqlite3.connect(db.DB_PATH)) as observer:
        assert {"claim_token", "doubts_json"} <= {
            row[1] for row in observer.execute("PRAGMA table_info(prop_card_rule_audits)")
        }
