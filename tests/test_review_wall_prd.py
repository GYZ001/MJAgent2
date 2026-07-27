from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import HTTPException

from app import api, db, worker


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,status,screenplay_status,
               screenplay_artifact_id,storyboard_artifact_id,
               published_screenplay_artifact_id,published_storyboard_artifact_id,created_at
           ) VALUES('e','p',1,'E','confirmed','ready','screenplay-1','board-1','screenplay-1','board-1',0)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,action_desc,characters,dialogues,
               storyboard_artifact_id
           ) VALUES('s1','e',1,5,'action','[]','[]','board-1')"""
    )
    conn.commit()
    return conn


def test_review_context_and_items_are_stable_and_audited(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    context = api.review_wall_context("e")
    assert context["upstream"]["eligible_for_production"] is True
    assert context["shots"][0]["shot_id"] == "s1"
    assert context["shots"][0]["review_status"] == "pending"
    content_version = context["shots"][0]["content_version"]

    item = api.create_shot_review_item("s1", {
        "issue_type": "continuity",
        "severity": "blocker",
        "comment": "末帧状态与下镜首帧不一致",
        "assignee": "director",
        "anchor": {"field": "state_out"},
        "content_version": content_version,
    })
    assert item["shot_id"] == "s1"
    assert item["revision"] == 1

    with pytest.raises(HTTPException) as blocked:
        api.set_shot_review_state("s1", {
            "review_status": "completed",
            "expected_revision": 1,
        })
    assert blocked.value.status_code == 409

    api.update_shot_review_item(item["id"], {
        "expected_revision": 1,
        "status": "resolved",
    })
    state = api.set_shot_review_state("s1", {
        "review_status": "completed",
        "expected_revision": 1,
    })
    assert state["review_status"] == "completed"
    assert conn.execute("SELECT COUNT(*) FROM review_action_audit").fetchone()[0] >= 3


def test_review_writes_are_idempotent(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    version = api.review_wall_context("e")["shots"][0]["content_version"]
    create_body = {
        "issue_type": "visual",
        "severity": "medium",
        "comment": "构图需要收紧",
        "content_version": version,
        "idempotency_key": "create-once",
    }
    first = api.create_shot_review_item("s1", create_body)
    repeated = api.create_shot_review_item("s1", create_body)
    assert repeated["id"] == first["id"]
    assert conn.execute("SELECT COUNT(*) FROM shot_review_items").fetchone()[0] == 1

    update_body = {
        "expected_revision": 1,
        "status": "in_progress",
        "idempotency_key": "update-once",
    }
    updated = api.update_shot_review_item(first["id"], update_body)
    repeated_update = api.update_shot_review_item(first["id"], update_body)
    assert repeated_update["revision"] == updated["revision"] == 2

    state_body = {
        "review_status": "in_review",
        "expected_revision": 1,
        "idempotency_key": "state-once",
    }
    state = api.set_shot_review_state("s1", state_body)
    repeated_state = api.set_shot_review_state("s1", state_body)
    assert repeated_state["revision"] == state["revision"] == 2


def test_review_item_rejects_stale_content_anchor(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    with pytest.raises(HTTPException) as conflict:
        api.create_shot_review_item("s1", {
            "issue_type": "visual",
            "severity": "medium",
            "comment": "需要修正",
            "content_version": "old-content",
        })
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "REVIEW_CONTENT_CHANGED"


def test_positive_actions_fail_closed_for_hard_failed_asset(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('v1','s1',1,'p','k','succeeded',?,0)""",
        (json.dumps({
            "reference_images": [{
                "id": "ref-1",
                "selectedForSeedance": True,
                "qa": {"status": "failed", "hard_failures": ["character_duplicate"]},
            }]
        }),),
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    snapshot = api._review_upstream_snapshot("e")
    assert snapshot["eligible_for_production"] is True
    assert snapshot["assets"]["blockers"] == []
    assert any(
        item["ref_id"] == "ref-1"
        and item["warning"] == "qa_hard_failure:character_duplicate"
        for item in snapshot["assets"]["soft_warnings"]
    )
    assert api._review_assert_positive_action("e")["eligible_for_production"] is True


@pytest.mark.parametrize("value", [-1, 0, float("nan"), float("inf"), 100001])
def test_authorization_numbers_reject_invalid_values(value) -> None:
    with pytest.raises(HTTPException) as rejected:
        api._review_validate_authorization_number(
            value, field="budget_cap_cny", minimum=1, maximum=100000,
        )
    assert rejected.value.status_code == 422


def test_qualification_version_detects_upstream_change(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    before = api._review_upstream_snapshot("e")
    conn.execute("UPDATE episodes SET storyboard_artifact_id='board-2', published_storyboard_artifact_id='board-2' WHERE id='e'")
    conn.commit()

    with pytest.raises(HTTPException) as conflict:
        api._review_assert_positive_action("e", before["qualification_version"])
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "REVIEW_QUALIFICATION_CHANGED"


def test_worker_fences_stale_run_before_candidate_write(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    snapshot = api._review_upstream_snapshot("e")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('v-run','s1',1,'p','run-key','running',?,0)""",
        (json.dumps({
            "review_dependency_snapshot": {
                "qualification_version": snapshot["qualification_version"],
            }
        }),),
    )
    conn.execute(
        "UPDATE episodes SET published_storyboard_artifact_id='board-2', storyboard_artifact_id='board-2' WHERE id='e'"
    )
    conn.commit()

    with pytest.raises(worker.ReviewDependencyFence) as fenced:
        worker._assert_review_dependency_fence(
            {"episode_id": "e"}, "v-run", "candidate",
        )
    assert "REVIEW_DEPENDENCY_STALE" in str(fenced.value)


def test_asset_gate_uses_adopted_gallery_and_missing_verdict_is_unverified(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-adopted','s1',1,'p','k1','succeeded',?,0)""",
        (json.dumps({"reference_images": [{
            "id": "legacy-ref", "selectedForSeedance": True,
            "entity_type": "character", "entity_name": "角色甲",
            "gate_status": "unverified",
        }]}),),
    )
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-new','s1',2,'p','k2','succeeded',?,1)""",
        (json.dumps({"reference_images": [{
            "id": "new-ref", "selectedForSeedance": True,
            "gate_status": "passed", "rule_version": "r2",
        }]}),),
    )
    conn.execute("UPDATE shots SET adopted_version_id='v-adopted' WHERE id='s1'")
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    snapshot = api._review_upstream_snapshot("e")
    assert snapshot["eligible_for_production"] is True
    assert snapshot["assets"]["blockers"] == []
    warnings = snapshot["assets"]["soft_warnings"]
    assert any(
        item["ref_id"] == "legacy-ref" and item["warning"] == "gate_status:unverified"
        for item in warnings
    )
    assert all(item["ref_id"] != "new-ref" for item in snapshot["assets"]["inputs"] + warnings)


def test_asset_rule_version_participates_in_qualification_token(monkeypatch) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v1','s1',1,'p','k','succeeded',?,0)""",
        (json.dumps({"reference_images": [{
            "id": "ref", "selectedForSeedance": True,
            "gate_status": "passed", "rule_version": "r1",
        }]}),),
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    before = api._review_upstream_snapshot("e")
    meta = {"reference_images": [{
        "id": "ref", "selectedForSeedance": True,
        "gate_status": "passed", "rule_version": "r2",
    }]}
    conn.execute("UPDATE shot_versions SET image_inputs=? WHERE id='v1'", (json.dumps(meta),))
    conn.commit()
    after = api._review_upstream_snapshot("e")
    assert before["qualification_version"] != after["qualification_version"]


def test_worker_does_not_self_fence_when_gallery_is_copied_to_new_version(monkeypatch) -> None:
    conn = _conn()
    reference = {
        "id": "ref", "selectedForSeedance": True,
        "gate_status": "passed", "rule_version": "r1",
        "library_revision_id": "asset-v1",
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v1','s1',1,'p','k1','succeeded',?,0)""",
        (json.dumps({"reference_images": [reference]}),),
    )
    conn.commit()
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    snapshot = api._review_upstream_snapshot("e")
    captured = {
        key: snapshot.get(key) for key in (
            "qualification_version", "published_screenplay_artifact_id",
            "confirmed_storyboard_artifact_id", "screenplay_revision",
            "storyboard_revision", "asset_inputs", "asset_soft_warnings",
        )
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v2','s1',2,'p','k2','running',?,1)""",
        (json.dumps({
            "reference_images": [reference],
            "review_dependency_snapshot": captured,
        }),),
    )
    conn.commit()

    worker._assert_review_dependency_fence({"episode_id": "e"}, "v2", "candidate")
