from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import portraits
from app.identity_authority import (
    IdentityAuthorityConflictError,
    authority_id_for_resolution,
    identity_authority_registry,
)
from app.orchestration.state_machine import StateConflict
from app.schemas import Bible, World


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
        "screenplay_character_resolutions TEXT NOT NULL DEFAULT '[]', "
        "active_screenplay_run_id TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO episodes(id, screenplay_character_resolutions) VALUES(?, ?)",
        ("ep_711b29204aa9", json.dumps(rows, ensure_ascii=False)),
    )
    return conn


def _empty_bible() -> Bible:
    return Bible(
        characters=[],
        world=World(visual_style_canonical="国风"),
    )


def _fresh_source_anchors(scope: str = "source-sha-episode-1") -> list[dict]:
    return [
        {
            "source_label": "虎头虎脑的少年",
            "canonical_name": "虎头虎脑的少年",
            "resolution": "functional_identity",
            "identity_group": "current-1:F1",
            "identity_scope_fingerprint": scope,
        },
        {
            "source_label": "白白净净身子较胖",
            "canonical_name": "白白净净身子较胖",
            "resolution": "functional_identity",
            "identity_group": "current-1:F2",
            "identity_scope_fingerprint": scope,
        },
    ]


def test_run_2470cc8c594e_legacy_conflict_is_not_order_migrated() -> None:
    conn = _resolution_conn(_production_conflict_rows())

    loaded = portraits.load_screenplay_character_resolutions(
        conn, "ep_711b29204aa9"
    )
    reversed_loaded = portraits.load_screenplay_character_resolutions(
        _resolution_conn(list(reversed(_production_conflict_rows()))),
        "ep_711b29204aa9",
    )

    with pytest.raises(IdentityAuthorityConflictError):
        identity_authority_registry(_empty_bible(), loaded)
    with pytest.raises(IdentityAuthorityConflictError):
        identity_authority_registry(_empty_bible(), reversed_loaded)
    assert {
        (item["canonical_name"], item["authority_id"])
        for item in loaded
    } == {
        (item["canonical_name"], item["authority_id"])
        for item in reversed_loaded
    }
    assert all("source_instance_key" not in item for item in loaded)


def test_run_2470cc8c594e_fresh_owned_source_rebuilds_legacy_conflict() -> None:
    conn = _resolution_conn(_production_conflict_rows())

    migrated = portraits.persist_screenplay_character_resolutions(
        conn, "ep_711b29204aa9", _fresh_source_anchors()
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
        (
            "current-1:F1",
            authority_id_for_resolution(_fresh_source_anchors()[0]),
        ),
        (
            "current-1:F2",
            authority_id_for_resolution(_fresh_source_anchors()[1]),
        ),
    }
    assert len(identity_authority_registry(_empty_bible(), migrated)) == 2


def test_same_group_distinct_named_identities_fail_instead_of_guessing() -> None:
    with pytest.raises(
        IdentityAuthorityConflictError,
        match="identity_group=episode:visitor.*缺少唯一可验证权威",
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


def test_structural_alias_reuses_scoped_authority_and_remains_projectable() -> None:
    scope = "source-sha-episode-1"
    existing = portraits.merge_screenplay_character_resolutions(
        [], _fresh_source_anchors(scope)
    )
    merged = portraits.merge_screenplay_character_resolutions(existing, [{
        "source_label": "大青山被困少年1",
        "canonical_name": "大青山被困少年1",
        "resolution": "functional_identity",
        "identity_group": "current-1:F1",
        "identity_scope_fingerprint": scope,
    }])

    f1 = [item for item in merged if item["identity_group"] == "current-1:F1"]
    assert {item["canonical_name"] for item in f1} == {"虎头虎脑的少年"}
    assert len({item["authority_id"] for item in f1}) == 1
    assert all("source_instance_key" not in item for item in f1)
    screenplay = SimpleNamespace(
        full_script_text="大青山被困少年1走来。",
        scene_outline=[],
    )
    changes = portraits.apply_screenplay_character_resolutions(screenplay, f1)
    assert screenplay.full_script_text == "虎头虎脑的少年走来。"
    assert changes == [{
        "source_label": "大青山被困少年1",
        "canonical_name": "虎头虎脑的少年",
        "resolution": "functional_identity",
    }]


def test_same_group_token_from_distinct_discovery_epochs_does_not_merge() -> None:
    scope_a = _fresh_source_anchors("source-a")[:1]
    scope_b = [{
        **_fresh_source_anchors("source-b")[0],
        "source_label": "新章节门卫",
        "canonical_name": "新章节门卫",
    }]

    registry = identity_authority_registry(
        _empty_bible(), [*scope_a, *scope_b]
    )
    assert len(registry) == 2
    assert registry[0]["authority_id"] != registry[1]["authority_id"]
    assert portraits.merge_screenplay_character_resolutions(
        scope_a, scope_b
    ) == portraits.merge_screenplay_character_resolutions([], scope_b)


@pytest.mark.parametrize(
    ("old_resolution", "new_resolution", "new_name"),
    [
        ("future_identity", "future_identity", "李四"),
        ("future_identity", "functional_identity", "新章节门卫"),
    ],
)
def test_fresh_discovery_replaces_old_epoch_and_absent_groups(
    old_resolution: str,
    new_resolution: str,
    new_name: str,
) -> None:
    old = [
        {
            "source_label": "同一称谓",
            "canonical_name": "张三",
            "resolution": old_resolution,
            "identity_group": "current-1:F1",
            "identity_scope_fingerprint": "source-a",
            "decision_provenance": (
                portraits.AUTOMATIC_IDENTITY_DECISION_PROVENANCE
            ),
        },
        {
            "source_label": "旧集门卫",
            "canonical_name": "旧集门卫",
            "resolution": "functional_identity",
            "identity_group": "current-1:F3",
            "identity_scope_fingerprint": "source-a",
            "decision_provenance": (
                portraits.AUTOMATIC_IDENTITY_DECISION_PROVENANCE
            ),
        },
    ]
    conn = _resolution_conn(old)
    incoming = [{
        "source_label": "同一称谓",
        "canonical_name": new_name,
        "resolution": new_resolution,
        "identity_group": "current-1:F1",
        "identity_scope_fingerprint": "source-b",
        "decision_provenance": (
            portraits.AUTOMATIC_IDENTITY_DECISION_PROVENANCE
        ),
    }]

    replaced = portraits.persist_screenplay_character_resolutions(
        conn,
        "ep_711b29204aa9",
        incoming,
        replace_identity_scope="source-b",
    )

    assert len(replaced) == 1
    assert replaced[0]["canonical_name"] == new_name
    assert replaced[0]["identity_scope_fingerprint"] == "source-b"
    assert all(item["identity_group"] != "current-1:F3" for item in replaced)


def test_functional_group_upgrade_joins_existing_bible_authority() -> None:
    scope = "source-sha-episode-1"
    functional = _fresh_source_anchors(scope)[:1]
    upgraded = portraits.merge_screenplay_character_resolutions(
        functional,
        [{
            "source_label": "虎头虎脑的少年",
            "canonical_name": "李富贵",
            "resolution": "future_identity",
            "identity_group": "current-1:F1",
            "identity_scope_fingerprint": scope,
        }],
    )
    bible = Bible(
        characters=[{
            "name": "李富贵",
            "role": "配角",
            "appearance_canonical": "圆脸少年，粗麻长衫",
        }],
        world=World(visual_style_canonical="国风"),
    )

    registry = identity_authority_registry(bible, upgraded)
    rich = next(item for item in registry if item["authority_id"] == "bible:李富贵")
    assert rich["identity_group"] == "bible:李富贵"
    assert "虎头虎脑的少年" in rich["source_labels"]


def test_future_and_functional_claims_in_same_raw_group_still_conflict() -> None:
    scope = "source-sha-episode-1"
    claims = [
        {
            "source_label": "虎头虎脑的少年",
            "canonical_name": "李富贵",
            "resolution": "future_identity",
            "identity_group": "current-1:F1",
            "identity_scope_fingerprint": scope,
        },
        {
            "source_label": "少年",
            "canonical_name": "未知少年",
            "resolution": "functional_identity",
            "identity_group": "current-1:F1",
            "identity_scope_fingerprint": scope,
        },
    ]

    with pytest.raises(IdentityAuthorityConflictError):
        identity_authority_registry(_empty_bible(), claims)


def test_multiple_future_aliases_same_raw_group_join_one_bible_identity() -> None:
    scope = "source-sha-episode-1"
    claims = [
        {
            "source_label": label,
            "canonical_name": "李富贵",
            "resolution": "future_identity",
            "identity_group": "current-1:F1",
            "identity_scope_fingerprint": scope,
        }
        for label in ("虎头虎脑的少年", "小胖子")
    ]

    registry = identity_authority_registry(_empty_bible(), claims)
    assert len(registry) == 1
    assert set(registry[0]["source_labels"]) == {"虎头虎脑的少年", "小胖子"}


def test_resolution_persist_rejects_stale_owner_without_clobbering_new_run() -> None:
    conn = _resolution_conn([])
    conn.execute(
        "UPDATE episodes SET active_screenplay_run_id='run-new', "
        "screenplay_character_resolutions='[{\"source_label\":\"B\","
        "\"canonical_name\":\"B\"}]' WHERE id='ep_711b29204aa9'"
    )
    conn.commit()

    with pytest.raises(StateConflict, match="screenplay_resolution_owner"):
        portraits.persist_screenplay_character_resolutions(
            conn,
            "ep_711b29204aa9",
            _fresh_source_anchors(),
            expected_active_run_id="run-old",
        )

    durable = conn.execute(
        "SELECT active_screenplay_run_id, screenplay_character_resolutions "
        "FROM episodes WHERE id='ep_711b29204aa9'"
    ).fetchone()
    assert durable["active_screenplay_run_id"] == "run-new"
    assert json.loads(durable["screenplay_character_resolutions"])[0][
        "canonical_name"
    ] == "B"
    conn.execute(
        "UPDATE episodes SET screenplay_character_resolutions='[]' "
        "WHERE id='ep_711b29204aa9'"
    )
    conn.commit()


def test_resolution_persist_old_value_cas_rolls_back_and_releases_connection() -> None:
    conn = _resolution_conn([])
    conn.execute(
        "UPDATE episodes SET active_screenplay_run_id='run-current' "
        "WHERE id='ep_711b29204aa9'"
    )
    conn.commit()

    class RacingConnection:
        def __init__(self, inner: sqlite3.Connection):
            self.inner = inner
            self.raced = False

        def execute(self, sql, parameters=()):
            if (
                not self.raced
                and sql.startswith(
                    "UPDATE episodes SET screenplay_character_resolutions=? WHERE"
                )
            ):
                self.raced = True
                self.inner.execute(
                    "UPDATE episodes SET screenplay_character_resolutions=? "
                    "WHERE id='ep_711b29204aa9'",
                    (json.dumps([{
                        "source_label": "B",
                        "canonical_name": "B",
                    }]),),
                )
                self.inner.commit()
            return self.inner.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    racing = RacingConnection(conn)
    with pytest.raises(StateConflict, match="screenplay_resolution_cas"):
        portraits.persist_screenplay_character_resolutions(
            racing,
            "ep_711b29204aa9",
            _fresh_source_anchors(),
            expected_active_run_id="run-current",
        )

    assert json.loads(conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes "
        "WHERE id='ep_711b29204aa9'"
    ).fetchone()["screenplay_character_resolutions"])[0]["canonical_name"] == "B"
    conn.execute(
        "UPDATE episodes SET screenplay_character_resolutions='[]' "
        "WHERE id='ep_711b29204aa9'"
    )
    conn.commit()


def _coverage_cache_conn() -> sqlite3.Connection:
    conn = _resolution_conn([])
    conn.execute(
        "CREATE TABLE artifacts("
        "id TEXT PRIMARY KEY, scope_type TEXT, scope_id TEXT, type TEXT, "
        "status TEXT, content_json TEXT, created_at REAL)"
    )
    return conn


def _structural_cache_payload(
    source_text: str,
    structural_evidence: list[dict],
    *,
    contract_version: str,
    policy_version: str,
    candidates: list[dict],
) -> dict:
    source_hash = portraits.evidence_repository.content_hash(source_text)
    structural_hash = portraits.evidence_repository.content_hash({
        "policy_version": portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        "source_hash": source_hash,
        "structural_evidence": structural_evidence,
    })
    return {
        "mode": "structural_coverage",
        "contract_version": contract_version,
        "policy_version": policy_version,
        "source_hash": source_hash,
        "structural_evidence_hash": structural_hash,
        "candidates": candidates,
    }


def test_structural_coverage_reuses_only_current_contract_cache(
    monkeypatch,
) -> None:
    conn = _coverage_cache_conn()
    source_text = "虎头虎脑的少年站在山门前。"
    evidence = [{
        "identity_key": "大青山被困少年1",
        "source_segment_ids": ["SRC0001"],
        "usage": "visible",
        "node_key": "S001-N001",
    }]
    cached_candidates = [{
        "source_label": "虎头虎脑的少年",
        "identity_group": "current-1:F1",
    }]
    payload = _structural_cache_payload(
        source_text,
        evidence,
        contract_version=portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        policy_version=portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        candidates=cached_candidates,
    )
    conn.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
        (
            "art-current", "episode", "ep_711b29204aa9",
            "screenplay_identity_discovery", "validated",
            json.dumps(payload, ensure_ascii=False), 2.0,
        ),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_character_resolutions=? "
        "WHERE id='ep_711b29204aa9'",
        (json.dumps([{
            "source_label": "虎头虎脑的少年",
            "canonical_name": "虎头虎脑的少年",
            "resolution": "functional_identity",
            "identity_group": "current-1:F1",
            "identity_scope_fingerprint": (
                portraits.screenplay_identity_scope_fingerprint(1, source_text)
            ),
        }], ensure_ascii=False),),
    )
    conn.commit()
    monkeypatch.setattr(portraits, "get_conn", lambda: conn)

    async def forbidden_audit(*_args, **_kwargs):
        raise AssertionError("当前合同缓存应直接复用")

    monkeypatch.setattr(
        portraits,
        "audit_identity_coverage_from_structural_evidence",
        forbidden_audit,
    )
    result = asyncio.run(portraits.ensure_structural_identity_coverage(
        "proj", "ep_711b29204aa9", 1, source_text,
        _empty_bible(), evidence,
    ))

    assert result["reused"] is True
    assert result["candidates"] == cached_candidates


def test_structural_cache_surviving_resolution_reset_is_rematerialized(
    monkeypatch,
) -> None:
    conn = _coverage_cache_conn()
    source_text = "虎头虎脑的少年站在山门前。"
    evidence = [{
        "identity_key": "大青山被困少年1",
        "source_segment_ids": ["SRC0001"],
        "usage": "visible",
        "node_key": "S001-N001",
    }]
    cached_alias = [{
        "name": "大青山被困少年1",
        "source_label": "大青山被困少年1",
        "identity_kind": "functional",
        "identity_group": "current-1:F1",
    }]
    payload = _structural_cache_payload(
        source_text,
        evidence,
        contract_version=portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        policy_version=portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        candidates=cached_alias,
    )
    conn.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
        (
            "art-current", "episode", "ep_711b29204aa9",
            "screenplay_identity_discovery", "validated",
            json.dumps(payload, ensure_ascii=False), 2.0,
        ),
    )
    conn.commit()
    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    calls = 0

    async def fake_audit(candidates, **_kwargs):
        nonlocal calls
        calls += 1
        assert candidates == []
        return []

    monkeypatch.setattr(
        portraits,
        "audit_identity_coverage_from_structural_evidence",
        fake_audit,
    )
    monkeypatch.setattr(
        portraits.evidence_repository,
        "create_artifact",
        lambda *_args, **_kwargs: {"id": "art-new"},
    )

    result = asyncio.run(portraits.ensure_structural_identity_coverage(
        "proj", "ep_711b29204aa9", 1, source_text,
        _empty_bible(), evidence,
    ))

    assert calls == 1
    assert "reused" not in result


def test_stale_structural_cache_is_neither_reused_nor_used_as_base(
    monkeypatch,
) -> None:
    conn = _coverage_cache_conn()
    source_text = "虎头虎脑的少年站在山门前。"
    evidence = [{
        "identity_key": "大青山被困少年1",
        "source_segment_ids": ["SRC0001"],
        "usage": "visible",
        "node_key": "S001-N001",
    }]
    stale_alias = [{
        "source_label": "大青山被困少年1",
        "identity_group": "current-1:F1",
    }]
    stale = _structural_cache_payload(
        source_text,
        evidence,
        contract_version=portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        policy_version="screenplay-identity-structural-coverage.v3",
        candidates=stale_alias,
    )
    valid_base = [{
        "source_label": "虎头虎脑的少年",
        "identity_group": "current-1:F1",
    }]
    conn.executemany(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
        [
            (
                "art-stale", "episode", "ep_711b29204aa9",
                "screenplay_identity_discovery", "validated",
                json.dumps(stale, ensure_ascii=False), 2.0,
            ),
            (
                "art-base", "episode", "ep_711b29204aa9",
                "screenplay_identity_discovery", "validated",
                json.dumps({
                    "mode": "targeted",
                    "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
                    "source_hash": portraits.evidence_repository.content_hash(source_text),
                    "candidates": valid_base,
                }, ensure_ascii=False),
                1.0,
            ),
        ],
    )
    conn.commit()
    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    observed_base: list[dict] = []

    async def fake_audit(candidates, **_kwargs):
        observed_base.extend(candidates)
        return list(candidates)

    monkeypatch.setattr(
        portraits,
        "audit_identity_coverage_from_structural_evidence",
        fake_audit,
    )
    monkeypatch.setattr(
        portraits.evidence_repository,
        "create_artifact",
        lambda *_args, **_kwargs: {"id": "art-new"},
    )
    result = asyncio.run(portraits.ensure_structural_identity_coverage(
        "proj", "ep_711b29204aa9", 1, source_text,
        _empty_bible(), evidence,
    ))

    assert "reused" not in result
    assert observed_base == valid_base
    assert stale_alias[0] not in observed_base
