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
        "constraints": {"occurrence_ids": ["dlg_one"]},
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


def test_repeated_dialogue_text_has_distinct_occurrence_ids() -> None:
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','planned',1)")
    source = "【第一章】\n甲：「我会回来。」\n\n中间发生了很多事。\n\n乙：「我会回来。」"
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) VALUES('p1',1,'第一章',?)",
        (source,),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,created_at) "
        "VALUES('e1','p1',1,'E','[1]',1)"
    )
    conn.commit()

    detail = api.episode_detail("e1", view="script")
    matches = [
        item for item in detail["source_dialogue_occurrences"]
        if item["text"] == "我会回来。"
    ]
    assert len(matches) == 2
    assert matches[0]["id"] != matches[1]["id"]
    assert matches[0]["offset"] != matches[1]["offset"]


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


def test_nested_curly_quotes_do_not_truncate_dialogue_occurrence() -> None:
    source = "【第一章】\n“三段？嘿嘿，果然不出我所料，这个“天才”这一年又是在原地踏步！”"
    items = api._screenplay_occurrences(source, [1])
    assert len(items) == 1
    assert items[0]["text"] == "三段？嘿嘿，果然不出我所料，这个“天才”这一年又是在原地踏步！"
    assert "“天才”这一年又是在原地踏步" in items[0]["context"]
    assert items[0]["estimated_seconds"] > 7


def test_target_duration_can_be_changed_before_generation_and_versions_constraints(client) -> None:
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','planned',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,target_duration_s,"
        "screenplay_status,status,created_at) VALUES('e1','p1',1,'E','[1]',50,'pending','planned',1)"
    )
    conn.commit()

    changed = client.put("/api/episodes/e1/target-duration", json={"target_duration_s": 70})
    assert changed.status_code == 200
    assert changed.json()["previous_target_duration_s"] == 50
    assert changed.json()["target_duration_s"] == 70
    assert changed.json()["constraint_version"] == 1
    assert changed.json()["snapshot_version"] == 1
    row = conn.execute(
        "SELECT target_duration_s, screenplay_constraint_version, screenplay_snapshot_version "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert tuple(row) == (70, 1, 1)

    unchanged = client.put("/api/episodes/e1/target-duration", json={"target_duration_s": 70})
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
    assert invalid.json()["detail"]["allowed_choices"] == [40, 50, 60, 70, 80, 90]
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

    monkeypatch.setattr(portraits, "ensure_cards_for_text", unavailable)
    with enter_handler(), pytest.raises(HTTPException) as caught:
        asyncio.run(api.edit_screenplay("e1", {
            "screenplay": changed.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "screenplay_character_discovery_failed"
    assert "剧本阶段重试" in caught.value.detail["errors"][0]
    row = conn.execute(
        "SELECT screenplay_artifact_id,screenplay_json FROM episodes WHERE id='e1'"
    ).fetchone()
    assert row["screenplay_artifact_id"] == "art_sp_old"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id='e1'"
    ).fetchone()["c"] == 1


def test_qa_failed_manual_draft_publishes_with_score_only_warnings() -> None:
    _seed_episode(with_artifact=True)
    changed = _valid_script()
    changed.stakes = ""
    conn = db.get_conn()

    with enter_handler():
        result = asyncio.run(api.edit_screenplay("e1", {
            "screenplay": changed.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))
    assert result["saved"] is True
    assert result["gate_retry_exhausted"] is True
    assert result["qa_warnings"]
    published = conn.execute(
        "SELECT screenplay_artifact_id,screenplay_status FROM episodes WHERE id='e1'"
    ).fetchone()
    assert published["screenplay_artifact_id"] != "art_sp_old"
    assert published["screenplay_status"] == "ready"
    artifact = conn.execute(
        "SELECT type,status FROM artifacts WHERE id=?", (result["artifact_id"],)
    ).fetchone()
    assert tuple(artifact) == ("screenplay_document", "approved")


def test_manual_screenplay_edit_uses_model_identity_resolution_before_publish(monkeypatch) -> None:
    _seed_episode(with_artifact=True)
    changed = _valid_script()
    changed.scene_outline[0].characters.append("青衣人")
    changed.scene_outline[0].summary += "青衣人短暂送来一封信后离开。"
    changed.full_script_text = changed.full_script_text.replace(
        "雨水顺着玻璃滑下",
        "青衣人放下一封信后离开。雨水顺着玻璃滑下",
        1,
    )

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
            "screenplay": changed.model_dump(mode="json"),
            "expected_version": "art_sp_old",
        }))

    assert result["saved"] is True
    row = db.get_conn().execute(
        "SELECT screenplay_json,screenplay_character_resolutions FROM episodes WHERE id='e1'"
    ).fetchone()
    published = json.loads(row["screenplay_json"])
    resolutions = json.loads(row["screenplay_character_resolutions"])
    assert "青衣人" not in published["scene_outline"][0]["characters"]
    assert "路人甲" in published["scene_outline"][0]["characters"]
    assert resolutions[0]["source_label"] == "青衣人"
    assert resolutions[0]["canonical_name"] == "路人甲"


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
    assert result["unchanged"] is False
    published = json.loads(conn.execute(
        "SELECT screenplay_json FROM episodes WHERE id='e1'"
    ).fetchone()["screenplay_json"])
    assert "青衣人" not in published["scene_outline"][0]["characters"]
    assert "路人甲" in published["scene_outline"][0]["characters"]


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


def test_dialogue_grouping_only_suggests_clear_semantic_pairs() -> None:
    source = "\n".join([
        "甲：「你为什么来？」",
        "乙：「因为我要救他。」",
        "丙：「外面下雨了。」",
        "丁：「灯也亮了。」",
    ])
    items = api._screenplay_occurrences(source, [1])
    by_text = {item["text"]: item for item in items}
    assert by_text["你为什么来？"]["group_id"] == by_text["因为我要救他。"]["group_id"]
    assert by_text["你为什么来？"]["group_id"] is not None
    assert by_text["外面下雨了。"]["group_id"] is None
    assert by_text["灯也亮了。"]["group_id"] is None


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
    assert "本集目标时长" in source
    assert "/target-duration" in source
    assert "整集节奏预算" in source
