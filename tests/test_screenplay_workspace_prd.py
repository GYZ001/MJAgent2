"""剧本台整改 PRD 的安全合同回归。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import api, db, portraits
from app.capabilities.direct import enter_handler
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.main import app
from tests.conftest import SessionTestClient
from tests.test_screenplay_edit_save import _seed_episode, _valid_script


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-workspace.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    yield
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
    monkeypatch.setattr(db._local, "conn", None, raising=False)


@pytest.fixture
def client():
    with TestClient(app) as raw:
        yield SessionTestClient(raw)


def test_noop_publish_keeps_artifact_and_downstream() -> None:
    _seed_episode(with_artifact=True)
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e1',1,5)"
    )
    conn.commit()
    before = conn.execute(
        "SELECT screenplay_artifact_id, screenplay_json FROM episodes WHERE id='e1'"
    ).fetchone()

    with enter_handler():
        result = asyncio.run(api.edit_screenplay("e1", {
            "screenplay": _valid_script().model_dump(mode="json"),
            "expected_version": before["screenplay_artifact_id"],
        }))

    after = conn.execute(
        "SELECT screenplay_artifact_id, screenplay_json FROM episodes WHERE id='e1'"
    ).fetchone()
    assert result["unchanged"] is True
    assert dict(after) == dict(before)
    assert conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'").fetchone()["c"] == 1


def test_orphan_storyboard_worker_blocks_publish_without_clearing() -> None:
    _seed_episode(with_artifact=True)
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e1',1,5)"
    )
    conn.execute("UPDATE episodes SET status='scripting' WHERE id='e1'")
    conn.commit()
    changed = _valid_script()
    changed.logline += "（草稿改动）"

    with enter_handler(), pytest.raises(HTTPException) as caught:
        asyncio.run(api.edit_screenplay("e1", {
            "screenplay": changed.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))

    assert caught.value.status_code == 409
    row = conn.execute(
        "SELECT screenplay_artifact_id, screenplay_publish_fence FROM episodes WHERE id='e1'"
    ).fetchone()
    assert row["screenplay_artifact_id"] == "art_sp_old"
    assert row["screenplay_publish_fence"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'").fetchone()["c"] == 1


def test_stale_storyboard_run_write_is_rejected_after_new_screenplay() -> None:
    _seed_episode(with_artifact=True)
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_artifact_id='art_screenplay_v2' WHERE id='e1'"
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="分镜写入被拒绝"):
        api._assert_storyboard_write_authorized(conn, "e1", "art_screenplay_v1")


def test_server_draft_survives_version_conflict(client) -> None:
    _seed_episode(with_artifact=True)
    draft = _valid_script().model_dump(mode="json")
    saved = client.put("/api/episodes/e1/screenplay/draft", json={
        "content": draft,
        "baseline_artifact_id": "art_stale",
    })
    assert saved.status_code == 200

    response = client.put("/api/episodes/e1/screenplay", json={
        "screenplay": draft,
        "expected_version": "art_stale",
    })
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "version_conflict"
    assert detail["current_version"] == "art_sp_old"
    recovered = client.get("/api/episodes/e1/screenplay/draft").json()["draft"]
    assert recovered["content"]["title"] == draft["title"]


def test_removed_constraint_only_draft_is_rejected(client) -> None:
    _seed_episode(with_artifact=False)
    saved = client.put("/api/episodes/e1/screenplay/draft", json={
        "constraints": {"occurrence_ids": ["legacy"]},
        "baseline_artifact_id": None,
    })

    assert saved.status_code == 422


def test_screenplay_preflight_has_no_dialogue_selection_budget(client) -> None:
    _seed_episode(with_artifact=False)

    response = client.post("/api/episodes/e1/screenplay/preflight", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["input"]["source_chars"] >= 0
    assert "selected_count" not in payload
    assert "selected_seconds" not in payload
    assert "hard_exceeded" not in payload
    assert "target_duration_s" not in payload


def test_character_discovery_bootstraps_placeholder_bible(monkeypatch) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_status,created_at) "
        "VALUES('p1','P','planned','idle',1)"
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,created_at) "
        "VALUES('e1','p1',1,'E','[1]',1)"
    )
    conn.commit()
    observed = {}

    async def fake_ensure(project_id, episode_no, source_text, bible, **kwargs):
        observed.update({
            "project_id": project_id,
            "episode_no": episode_no,
            "source_text": source_text,
            "characters": list(bible.characters),
            "generate_portraits": kwargs["generate_portraits"],
        })
        return {
            "checked": 0, "candidates": [], "added": [], "skipped": [],
            "resolutions": [], "errors": [], "warnings": [],
        }

    monkeypatch.setattr(portraits, "ensure_cards_for_text", fake_ensure)

    result = asyncio.run(api._screenplay_character_discovery("e1", "孟浩走上山顶。"))

    project = conn.execute(
        "SELECT bible_json,bible_status FROM projects WHERE id='p1'"
    ).fetchone()
    assert json.loads(project["bible_json"])["world"]["visual_style_canonical"]
    assert project["bible_status"] == "idle"
    assert observed == {
        "project_id": "p1",
        "episode_no": 1,
        "source_text": "孟浩走上山顶。",
        "characters": [],
        "generate_portraits": False,
    }
    assert result["resolutions"] == []


def test_target_duration_can_be_changed_before_generation_and_versions_constraints(client) -> None:
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','planned',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,target_duration_s,"
        "screenplay_status,status,created_at) VALUES('e1','p1',1,'E','[1]',50,'pending','planned',1)"
    )
    conn.commit()

    changed = client.put("/api/episodes/e1/target-duration", json={"target_duration_s": 1000})
    assert changed.status_code == 200
    assert changed.json()["previous_target_duration_s"] == 50
    assert changed.json()["target_duration_s"] == 1000
    assert changed.json()["constraint_version"] == 1
    assert changed.json()["snapshot_version"] == 1
    row = conn.execute(
        "SELECT target_duration_s, screenplay_constraint_version, screenplay_snapshot_version "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert tuple(row) == (1000, 1, 1)

    unchanged = client.put("/api/episodes/e1/target-duration", json={"target_duration_s": 1000})
    assert unchanged.status_code == 200
    assert unchanged.json()["unchanged"] is True
    row = conn.execute(
        "SELECT screenplay_constraint_version, screenplay_snapshot_version FROM episodes WHERE id='e1'"
    ).fetchone()
    assert tuple(row) == (1, 1)


def test_target_duration_rejects_unknown_step_and_published_episode(client) -> None:
    _seed_episode(with_artifact=True)

    invalid = client.put("/api/episodes/e1/target-duration", json={"target_duration_s": 55})
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["minimum_s"] == 40
    assert invalid.json()["detail"]["step_s"] == 10
    fractional = client.put("/api/episodes/e1/target-duration", json={"target_duration_s": 70.9})
    assert fractional.status_code == 422

    locked = client.put("/api/episodes/e1/target-duration", json={"target_duration_s": 70})
    assert locked.status_code == 409
    assert locked.json()["detail"]["code"] == "episode_target_duration_locked"
    assert db.get_conn().execute(
        "SELECT target_duration_s FROM episodes WHERE id='e1'"
    ).fetchone()["target_duration_s"] == 50


def test_edit_impact_preview_is_read_only_and_detects_downstream(client) -> None:
    _seed_episode(with_artifact=True)
    changed = _valid_script().model_dump(mode="json")
    changed["logline"] += "（改动）"
    before = db.get_conn().execute(
        "SELECT screenplay_json, screenplay_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()

    clean = client.post("/api/episodes/e1/screenplay/impact-preview", json={
        "screenplay": changed,
        "expected_version": "art_sp_old",
    })
    assert clean.status_code == 200
    assert clean.json()["read_only"] is True
    assert clean.json()["requires_server_approval"] is False
    after = db.get_conn().execute(
        "SELECT screenplay_json, screenplay_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(after) == dict(before)

    conn = db.get_conn()
    conn.execute("INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e1',1,5)")
    conn.commit()
    downstream = client.post("/api/episodes/e1/screenplay/impact-preview", json={
        "screenplay": changed,
        "expected_version": "art_sp_old",
    })
    assert downstream.status_code == 200
    assert downstream.json()["requires_server_approval"] is True
    assert downstream.json()["downstream"]["shots"] == 1


def test_edit_impact_preview_defers_identity_judgement_to_publish_model(client) -> None:
    _seed_episode(with_artifact=True)
    changed = _valid_script().model_dump(mode="json")
    changed["scene_outline"][0]["characters"].append("青衣人")
    before = db.get_conn().execute(
        "SELECT screenplay_character_resolutions,screenplay_json FROM episodes WHERE id='e1'"
    ).fetchone()

    response = client.post("/api/episodes/e1/screenplay/impact-preview", json={
        "screenplay": changed,
        "expected_version": "art_sp_old",
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["qa"]["runtime_blocking"] is False
    assert payload["character_identity_preflight"] == {
        "required": True,
        "status": "pending_model_resolution",
        "lookahead_chapters": 10,
        "message": "发布时会先由模型结合未来 10 章解析人物真名；无可靠真名时自动映射为路人角色",
    }
    after = db.get_conn().execute(
        "SELECT screenplay_character_resolutions,screenplay_json FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(after) == dict(before)


def test_unchanged_legacy_identity_still_requires_screenplay_preflight(client) -> None:
    _seed_episode(with_artifact=True)
    legacy = _valid_script().model_dump(mode="json")
    legacy["scene_outline"][0]["characters"].append("青衣人")
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_json=? WHERE id='e1'",
        (json.dumps(legacy, ensure_ascii=False),),
    )
    conn.commit()

    response = client.post("/api/episodes/e1/screenplay/impact-preview", json={
        "screenplay": legacy,
        "expected_version": "art_sp_old",
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["unchanged"] is True
    assert payload["character_identity_preflight"]["required"] is True


def test_manual_publish_turns_identity_model_failure_into_retriable_screenplay_error(monkeypatch) -> None:
    _seed_episode(with_artifact=True)
    conn = db.get_conn()
    conn.execute("INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e1',1,5)")
    conn.commit()
    changed = _valid_script()
    changed.scene_outline[0].characters.append("青衣人")

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "app.identity_adjudication.adjudicate_screenplay_document_identities",
        unavailable,
    )
    with enter_handler(), pytest.raises(HTTPException) as caught:
        asyncio.run(api.edit_screenplay("e1", {
            "screenplay": changed.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "screenplay_identity_adjudication_failed"
    assert "provider unavailable" in caught.value.detail["errors"][0]
    row = conn.execute(
        "SELECT screenplay_artifact_id,screenplay_json FROM episodes WHERE id='e1'"
    ).fetchone()
    assert row["screenplay_artifact_id"] == "art_sp_old"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'"
    ).fetchone()["c"] == 1


def test_qa_failed_manual_draft_never_publishes() -> None:
    _seed_episode(with_artifact=True)
    changed = _valid_script()
    changed.stakes = ""
    conn = db.get_conn()

    with enter_handler(), pytest.raises(HTTPException) as caught:
        asyncio.run(api.edit_screenplay("e1", {
            "screenplay": changed.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "screenplay_qa_failed"
    published = conn.execute(
        "SELECT screenplay_artifact_id,screenplay_status FROM episodes WHERE id='e1'"
    ).fetchone()
    assert published["screenplay_artifact_id"] == "art_sp_old"
    assert published["screenplay_status"] == "ready"


def test_manual_publish_consume_failure_rolls_back_authority_before_fence_cleanup(
    monkeypatch,
) -> None:
    _seed_episode(with_artifact=True)
    first = _valid_script()
    first.logline += "（首个正式发布版）"
    first.full_script_text += "\n门外再次响起更重的敲门声。\n谷言把钥匙收进掌心。"
    with enter_handler():
        initial = asyncio.run(api.edit_screenplay("e1", {
            "screenplay": first.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))

    conn = db.get_conn()
    authority_columns = (
        "screenplay_json,screenplay_artifact_id,published_screenplay_artifact_id,"
        "screenplay_production_revision_id,screenplay_completion_certificate_id,"
        "screenplay_status"
    )
    before = conn.execute(
        f"SELECT {authority_columns} FROM episodes WHERE id='e1'"
    ).fetchone()
    old_revision = conn.execute(
        "SELECT status,working_artifact_id,published_artifact_id "
        "FROM production_revisions WHERE id=?",
        (before["screenplay_production_revision_id"],),
    ).fetchone()
    old_certificate = conn.execute(
        "SELECT artifact_id,production_revision_id,consumed_at,payload_json "
        "FROM completion_certificates WHERE id=?",
        (before["screenplay_completion_certificate_id"],),
    ).fetchone()
    old_artifact = conn.execute(
        "SELECT status,trust_level FROM artifacts WHERE id=?",
        (before["screenplay_artifact_id"],),
    ).fetchone()
    from app.evidence import repository

    descendant = repository.create_artifact(EvidenceArtifact(
        type="storyboard",
        scope_type="episode",
        scope_id="e1",
        status="approved",
        trust_level="T2",
        content={"old_downstream": True},
        parent_artifact_ids=[before["screenplay_artifact_id"]],
    ))
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) "
        "VALUES('rollback-shot','e1',1,5)"
    )
    conn.commit()
    cleanup_outbox_count = conn.execute(
        "SELECT COUNT(*) FROM media_cleanup_outbox"
    ).fetchone()[0]
    certificate_count = conn.execute(
        "SELECT COUNT(*) AS c FROM completion_certificates"
    ).fetchone()["c"]
    certificate_artifact_count = conn.execute(
        "SELECT COUNT(*) AS c FROM artifacts WHERE type='completion_certificate'"
    ).fetchone()["c"]

    def fail_consumption(*_args, **_kwargs):
        raise RuntimeError("injected certificate consumption failure")

    monkeypatch.setattr(
        "app.production.publish.consume_completion_certificate",
        fail_consumption,
    )
    second = _valid_script()
    second.logline += "（不应发布的新版本）"
    second.full_script_text += "\n门外再次响起更重的敲门声。\n谷言没有收下钥匙。"
    with enter_handler(), pytest.raises(
        RuntimeError,
        match="injected certificate consumption failure",
    ):
        asyncio.run(api.edit_screenplay("e1", {
            "screenplay": second.model_dump(mode="json"),
            "expected_version": initial["artifact_id"],
        }))

    after = conn.execute(
        f"SELECT {authority_columns},screenplay_publish_fence "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert tuple(after[:-1]) == tuple(before)
    assert after["screenplay_publish_fence"] == 0
    assert tuple(conn.execute(
        "SELECT status,working_artifact_id,published_artifact_id "
        "FROM production_revisions WHERE id=?",
        (before["screenplay_production_revision_id"],),
    ).fetchone()) == tuple(old_revision)
    assert tuple(conn.execute(
        "SELECT artifact_id,production_revision_id,consumed_at,payload_json "
        "FROM completion_certificates WHERE id=?",
        (before["screenplay_completion_certificate_id"],),
    ).fetchone()) == tuple(old_certificate)
    assert tuple(conn.execute(
        "SELECT status,trust_level FROM artifacts WHERE id=?",
        (before["screenplay_artifact_id"],),
    ).fetchone()) == tuple(old_artifact)
    assert conn.execute(
        "SELECT status FROM artifacts WHERE id=?",
        (descendant["id"],),
    ).fetchone()["status"] == "approved"
    assert conn.execute(
        "SELECT COUNT(*) FROM shots WHERE id='rollback-shot'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM media_cleanup_outbox"
    ).fetchone()[0] == cleanup_outbox_count
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM completion_certificates"
    ).fetchone()["c"] == certificate_count
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM artifacts WHERE type='completion_certificate'"
    ).fetchone()["c"] == certificate_artifact_count

    failed_candidate = conn.execute(
        """SELECT status,working_artifact_id,published_artifact_id
             FROM production_revisions
            WHERE episode_id='e1' AND id<>?
            ORDER BY created_at DESC LIMIT 1""",
        (before["screenplay_production_revision_id"],),
    ).fetchone()
    assert failed_candidate["status"] == "active"
    assert failed_candidate["working_artifact_id"]
    assert failed_candidate["published_artifact_id"] is None
    assert conn.execute(
        "SELECT status FROM artifacts WHERE id=?",
        (failed_candidate["working_artifact_id"],),
    ).fetchone()["status"] == "candidate"


def test_publish_persists_cleanup_outbox_and_recovers_deferred_file_delete(
    monkeypatch,
    tmp_path,
) -> None:
    from app import artifacts
    from app.evidence import repository

    _seed_episode(with_artifact=True)
    first = _valid_script()
    first.logline += "（第一版）"
    first.full_script_text += "\n门外再次响起更重的敲门声。\n谷言把钥匙收进口袋。"
    with enter_handler():
        initial = asyncio.run(api.edit_screenplay("e1", {
            "screenplay": first.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))

    descendant = repository.create_artifact(EvidenceArtifact(
        type="storyboard",
        scope_type="episode",
        scope_id="e1",
        status="approved",
        trust_level="T2",
        content={"old_downstream": True},
        parent_artifact_ids=[initial["artifact_id"]],
    ))
    video_file = tmp_path / "old-shot.mp4"
    video_file.write_bytes(b"old video")
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) "
        "VALUES('old-shot','e1',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES('old-version','old-shot',1,'prompt','old-idem','done',?,1)""",
        (str(video_file),),
    )
    conn.commit()

    real_flush = artifacts.flush_media_cleanup_outbox
    monkeypatch.setattr(artifacts, "flush_media_cleanup_outbox", lambda _id: False)
    second = _valid_script()
    second.logline += "（第二版）"
    second.full_script_text += "\n门外再次响起更重的敲门声。\n谷言没有收下钥匙。"
    with enter_handler():
        asyncio.run(api.edit_screenplay("e1", {
            "screenplay": second.model_dump(mode="json"),
            "expected_version": initial["artifact_id"],
        }))

    assert conn.execute(
        "SELECT COUNT(*) FROM shots WHERE episode_id='e1'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM artifacts WHERE id=?",
        (descendant["id"],),
    ).fetchone()["status"] == "stale"
    pending = conn.execute(
        "SELECT id,status FROM media_cleanup_outbox "
        "WHERE episode_id='e1' AND status='pending'"
    ).fetchone()
    assert pending["status"] == "pending"
    assert video_file.exists()

    assert real_flush(str(pending["id"])) is True
    assert not video_file.exists()
    assert conn.execute(
        "SELECT status FROM media_cleanup_outbox WHERE id=?",
        (pending["id"],),
    ).fetchone()["status"] == "completed"


def test_runtime_blocking_manual_draft_routes_to_repair_without_publish(
    monkeypatch,
) -> None:
    _seed_episode(with_artifact=True)
    changed = _valid_script()
    changed.logline += " changed"
    issue = Issue(
        code="AUDIENCE_TARGET_DELTA_STAGING_REQUIRED",
        severity=IssueSeverity.BLOCKER,
        subject="screenplay",
        message="staged audience state required",
        evidence={"must_fix": True},
        repairable=True,
    )
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="screenplay_production_qa",
        evaluator_version="screenplay-qa-gate-2",
        status="failed",
        hard_gate_passed=False,
        evaluation_role="runtime_gate",
        runtime_blocking=True,
        score=90,
        issues=[issue],
    )
    monkeypatch.setattr(
        "app.production.screenplay_repair.run_screenplay_qa",
        lambda *_args, **_kwargs: ([issue], evaluation),
    )

    with enter_handler(), pytest.raises(HTTPException) as caught:
        asyncio.run(api.edit_screenplay("e1", {
            "screenplay": changed.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "screenplay_qa_failed"
    assert db.get_conn().execute(
        "SELECT screenplay_artifact_id FROM episodes WHERE id='e1'"
    ).fetchone()["screenplay_artifact_id"] == "art_sp_old"


def test_manual_screenplay_edit_rejects_identity_without_owned_source_evidence(monkeypatch) -> None:
    _seed_episode(with_artifact=True)
    changed = _valid_script()
    changed.scene_outline[0].characters.append("青衣人")
    changed.scene_outline[0].summary += "青衣人短暂送来一封信后离开。"
    changed.full_script_text = changed.full_script_text.replace(
        "雨水顺着玻璃滑下",
        "青衣人放下一封信后离开。雨水顺着玻璃滑下",
        1,
    )
    changed.full_script_text += "\n门外再次响起更重的敲门声。"

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({"characters": [{
            "source_label": "青衣人",
            "canonical_name": "",
            "identity_kind": "functional",
            "kind": "onscreen",
            "evidence": "青衣人送信后离开",
            "future_evidence": "",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    with enter_handler(), pytest.raises(HTTPException) as caught:
        asyncio.run(api.edit_screenplay("e1", {
            "screenplay": changed.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "screenplay_character_identity_unresolved"
    row = db.get_conn().execute(
        "SELECT screenplay_json,screenplay_character_resolutions FROM episodes WHERE id='e1'"
    ).fetchone()
    assert "青衣人" not in json.loads(row["screenplay_json"])["scene_outline"][0]["characters"]
    assert row["screenplay_character_resolutions"] in {None, "[]"}


def test_unchanged_legacy_screenplay_is_canonicalized_before_noop_return(monkeypatch) -> None:
    _seed_episode(with_artifact=True)
    legacy = _valid_script()
    legacy.scene_outline[0].characters.append("青衣人")
    legacy.scene_outline[0].summary += "青衣人短暂送来一封信后离开。"
    legacy.full_script_text = legacy.full_script_text.replace(
        "雨水顺着玻璃滑下",
        "青衣人放下一封信后离开。雨水顺着玻璃滑下",
        1,
    )
    legacy.full_script_text += "\n门外再次响起更重的敲门声。"
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_json=? WHERE id='e1'",
        (legacy.model_dump_json(),),
    )
    conn.commit()

    async def fake_chat(_messages, **_kwargs):
        return json.dumps({"characters": [{
            "source_label": "青衣人",
            "canonical_name": "",
            "identity_kind": "functional",
            "kind": "onscreen",
            "evidence": "青衣人送信后离开",
            "future_evidence": "",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    with enter_handler():
        result = asyncio.run(api.edit_screenplay("e1", {
            "screenplay": legacy.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))

    assert result["saved"] is True
    assert result["unchanged"] is True
    published = json.loads(conn.execute(
        "SELECT screenplay_json FROM episodes WHERE id='e1'"
    ).fetchone()["screenplay_json"])
    assert "青衣人" in published["scene_outline"][0]["characters"]
    resolutions = json.loads(conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id='e1'"
    ).fetchone()["screenplay_character_resolutions"])
    assert resolutions == []


def test_successful_storyboard_is_not_reported_as_failed_checkpoint() -> None:
    _seed_episode(with_artifact=True)
    conn = db.get_conn()
    conn.execute("UPDATE episodes SET status='scripted', script_error=NULL WHERE id='e1'")
    conn.commit()
    ep = dict(conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone())
    ready = api._screenplay_status_snapshot(ep, shot_count=8, production={})
    assert ready["code"] == "ready_storyboard_review"
    assert ready["checkpoint_shot"] is None

    ep["status"] = "script_failed"
    failed = api._screenplay_status_snapshot(ep, shot_count=5, production={})
    assert failed["code"] == "ready_storyboard_failed"
    assert failed["checkpoint_shot"] == 5


def test_invalid_published_certificate_recommends_revalidation(monkeypatch) -> None:
    _seed_episode(with_artifact=True)
    conn = db.get_conn()
    ep = dict(conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone())
    ep["screenplay_status"] = "ready"
    monkeypatch.setattr(api, "_screenplay_ready", lambda _ep: False)

    state = api._screenplay_status_snapshot(ep, shot_count=8, production={})

    assert state["code"] == "qa_certificate_invalid"
    assert state["recommended_action"] == "resume_screenplay"
    assert "重新校验" in state["message"]


def test_1646_episode_picker_and_light_status_reduce_minute_payload_over_80_percent() -> None:
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','planned',1)")
    conn.executemany(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,target_duration_s,"
        "screenplay_status,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (f"e{number}", "p1", number, f"第{number}集", "[]", 50, "pending", "planned", 1)
            for number in range(1, 1647)
        ],
    )
    conn.commit()
    picker_bytes = len(json.dumps(
        api.project_detail("p1", view="picker"), ensure_ascii=False, default=str
    ).encode())
    light_bytes = len(json.dumps(
        api.screenplay_lightweight_status("e1"), ensure_ascii=False, default=str
    ).encode())
    one_minute_bytes = picker_bytes + light_bytes * 30
    assert one_minute_bytes < 3_600_000 * 0.2


def test_script_page_has_pure_navigation_and_no_pipe_parser() -> None:
    source = Path("frontend/src/pages/ScriptPage.tsx").read_text(encoding="utf-8")
    assert "split('|')" not in source
    assert "window.confirm(`确认恢复" not in source
    assert "查看分镜台 →" in source
    assert "go('board', projectId, ep.id)" in source
    assert "storyboardTaskNotice" not in source
    assert "EpisodeStatusStamp" not in source
    assert "分镜生成未完成" not in source
    assert "查看分镜错误详情" not in source
    assert "必保留原文台词" not in source
    assert "/target-duration" not in source
    assert "required_dialogue" not in source
    assert "继续首版场次生成" in source
    assert "继续完整剧本校验" in source
    assert "场次写作已完成 {ep.screenplay_production.shard_progress.validated}/" in source
    assert "shard_progress" in source
