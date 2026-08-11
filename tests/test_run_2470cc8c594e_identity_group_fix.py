from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import portraits
from app.identity_authority import (
    IdentityAuthorityConflictError,
    identity_authority_registry,
)
from app.schemas import Bible


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "run_2470cc8c594e_identity_group_conflict.json"
)


def _production_conflict_rows() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _resolution_conn(rows: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes("
        "id TEXT PRIMARY KEY, "
        "screenplay_character_resolutions TEXT NOT NULL DEFAULT '[]'"
        ")"
    )
    conn.execute(
        "INSERT INTO episodes(id, screenplay_character_resolutions) VALUES(?, ?)",
        ("ep_711b29204aa9", json.dumps(rows, ensure_ascii=False)),
    )
    return conn


def test_run_2470cc8c594e_legacy_aliases_reuse_each_group_authority() -> None:
    conn = _resolution_conn(_production_conflict_rows())

    loaded = portraits.load_screenplay_character_resolutions(
        conn, "ep_711b29204aa9"
    )
    by_group = {
        group: [
            item for item in loaded if item.get("identity_group") == group
        ]
        for group in ("current-1:F1", "current-1:F2")
    }

    assert {
        item["canonical_name"] for item in by_group["current-1:F1"]
    } == {"虎头虎脑的少年"}
    assert {
        item["authority_id"] for item in by_group["current-1:F1"]
    } == {"functional:03f2ad2f69f130c5"}
    assert {
        item["canonical_name"] for item in by_group["current-1:F2"]
    } == {"白白净净身子较胖"}
    assert {
        item["authority_id"] for item in by_group["current-1:F2"]
    } == {"functional:accacf4a96da93d0"}
    assert by_group["current-1:F1"][0]["authority_id"] != (
        by_group["current-1:F2"][0]["authority_id"]
    )
    assert len(identity_authority_registry(Bible(), loaded)) == 2


def test_run_2470cc8c594e_normal_persist_rewrites_legacy_conflict() -> None:
    conn = _resolution_conn(_production_conflict_rows())

    migrated = portraits.persist_screenplay_character_resolutions(
        conn, "ep_711b29204aa9", []
    )
    durable = json.loads(conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id=?",
        ("ep_711b29204aa9",),
    ).fetchone()["screenplay_character_resolutions"])

    assert durable == migrated
    assert {
        (item["identity_group"], item["authority_id"])
        for item in durable
    } == {
        ("current-1:F1", "functional:03f2ad2f69f130c5"),
        ("current-1:F2", "functional:accacf4a96da93d0"),
    }


def test_same_group_distinct_named_identities_fail_instead_of_guessing() -> None:
    with pytest.raises(
        IdentityAuthorityConflictError,
        match="identity_group=episode:visitor.*多个真名",
    ):
        portraits.merge_screenplay_character_resolutions([], [
            {
                "source_label": "青衣人",
                "canonical_name": "丁力",
                "resolution": "future_identity",
                "identity_group": "episode:visitor",
            },
            {
                "source_label": "守门人",
                "canonical_name": "王平",
                "resolution": "future_identity",
                "identity_group": "episode:visitor",
            },
        ])
