from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path

import pytest

from app import api, config, db, hiagent, multiview, portraits, refs, scenes, stages
from app.domain import bible_ops
from app.schemas import Bible, Character, Scene, World


@pytest.fixture
def asset_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "assets.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn(), tmp_path
    db.get_conn().close()


def _seed_bible_project(conn, *, with_scene: bool = False) -> Bible:
    bible = Bible(
        world=World(visual_style_canonical="cinematic animation"),
        characters=[
            Character(
                name="Hero",
                role="lead",
                appearance_canonical="young hero, short black hair, blue coat, tall build",
            )
        ],
        scenes=(
            [Scene(name="Courtyard", scene_canonical="stone courtyard at dawn with a red gate")]
            if with_scene else []
        ),
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES('proj_bootstrap', 'Bootstrap', 'bible_ready', ?, 1, 1)",
        (bible.model_dump_json(),),
    )
    conn.commit()
    return bible


def _patch_successful_character_generation(monkeypatch) -> None:
    encoded = base64.b64encode(b"test-image").decode("ascii")

    async def fake_image(*_args, **_kwargs):
        return {"b64_json": encoded}

    async def fake_qa(*_args, **_kwargs):
        return {"overall": 0.95, "status": "ready", "issues": []}

    async def duplicate_view_qa(*_args, **_kwargs):
        raise AssertionError("初始主图与侧视角应由已存主图 QA + 一次整包 QA 覆盖")

    monkeypatch.setattr(refs.hiagent, "generate_image", fake_image)
    monkeypatch.setattr(multiview, "_generate_image", fake_image)
    monkeypatch.setattr(stages, "review_portrait_image", fake_qa)
    monkeypatch.setattr(multiview, "review_character_view", duplicate_view_qa)
    monkeypatch.setattr(multiview, "review_character_pack_consistency", fake_qa)
    monkeypatch.setattr(multiview, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(
        refs,
        "record_reference_asset",
        lambda **_kwargs: {"id": "artifact_portrait", "status": "approved"},
    )


def test_initial_character_generation_publishes_complete_three_view_pack(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_bible_project(conn)
    _patch_successful_character_generation(monkeypatch)

    asyncio.run(refs.generate_refs("proj_bootstrap"))

    portrait = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id='proj_bootstrap' AND character_name='Hero'"
    ).fetchone()
    assert portrait is not None
    assert portrait["pack_status"] == "ready"
    view_rows = conn.execute(
        "SELECT view_role, status, image_path FROM character_portrait_views "
        "WHERE portrait_id=? ORDER BY view_role",
        (portrait["id"],),
    ).fetchall()
    assert {row["view_role"] for row in view_rows} == {
        "front_full", "three_quarter", "profile",
    }
    assert all(row["status"] == "ready" and Path(row["image_path"]).is_file() for row in view_rows)
    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_bootstrap'"
    ).fetchone()["bible_json"])
    assert bible["characters"][0]["ref_image_path"] == portrait["image_path"]
    detail = api.project_detail("proj_bootstrap", view="bible")
    visible = detail["bible"]["characters"][0]["portraits"][0]
    assert visible["pack_status"] == "ready"
    assert {view["view_role"] for view in visible["views"]} == {
        "front_full", "three_quarter", "profile",
    }
    assert all(view["image_url"] for view in visible["views"])


def test_interrupted_auto_discovered_portrait_resumes_same_candidate(
    asset_db, monkeypatch,
) -> None:
    conn, tmp_path = asset_db
    bible = _seed_bible_project(conn)
    primary = tmp_path / "hero-ep19.jpg"
    primary.write_bytes(b"paid-primary")
    conn.execute(
        """INSERT INTO character_portraits(
               id,project_id,character_name,ep_start,ep_end,appearance,prompt,image_path,
               base_portrait_id,bible_version,artifact_id,pack_status,created_at
           ) VALUES('portrait_interrupted','proj_bootstrap','Hero',19,19,?,?,?,?,1,NULL,'generating',1)""",
        (bible.characters[0].appearance_canonical, "portrait prompt", str(primary), None),
    )
    conn.execute(
        """INSERT INTO character_portrait_views(
               id,portrait_id,view_role,framing,image_path,prompt,qa_json,status,selected,created_at
           ) VALUES('front_interrupted','portrait_interrupted','front_full','full_body',?,?,?,'ready',1,1)""",
        (str(primary), "portrait prompt", json.dumps({"overall": 0.9, "hard_gate_passed": True})),
    )
    change = [{
        "id": "change_interrupted",
        "kind": "new_character",
        "status": "auto_applied_asset_pending",
        "character": "Hero",
        "ep_start": 19,
        "payload": {"character_card": bible.characters[0].model_dump(mode="json")},
    }]
    conn.execute(
        "UPDATE projects SET bible_auto_changes_json=? WHERE id='proj_bootstrap'",
        (json.dumps(change, ensure_ascii=False),),
    )
    conn.commit()
    pack_calls = []

    async def forbidden_primary(*_args, **_kwargs):
        raise AssertionError("服务重启后不得重复生成已经落盘的主图")

    async def resume_pack(**kwargs):
        pack_calls.append(kwargs)
        return {"status": "ready", "portrait_id": kwargs["portrait_id"]}

    monkeypatch.setattr(portraits, "_generate_fresh_portrait", forbidden_primary)
    monkeypatch.setattr(multiview, "ensure_character_multiview_pack", resume_pack)

    class _Scene:
        characters = ["Hero"]

    class _Screenplay:
        scene_outline = [_Scene()]

    result = asyncio.run(portraits.ensure_cards_for_screenplay(
        "proj_bootstrap", 19, _Screenplay(), bible,
    ))

    rows = conn.execute(
        "SELECT id,ep_start,ep_end,pack_status FROM character_portraits "
        "WHERE project_id='proj_bootstrap' AND character_name='Hero'",
    ).fetchall()
    assert [row["id"] for row in rows] == ["portrait_interrupted"]
    assert (rows[0]["ep_start"], rows[0]["ep_end"], rows[0]["pack_status"]) == (19, None, "ready")
    assert result["blocking_errors"] == []
    assert result["added"][0]["portrait_id"] == "portrait_interrupted"
    assert result["added"][0]["reused"] is True
    assert pack_calls[0]["portrait_id"] == "portrait_interrupted"
    stored_change = json.loads(conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id='proj_bootstrap'",
    ).fetchone()["bible_auto_changes_json"])[0]
    assert stored_change["status"] == "auto_applied"
    assert stored_change["payload"]["portrait_id"] == "portrait_interrupted"


def test_interrupted_auto_discovered_portrait_fails_without_duplicate(
    asset_db, monkeypatch,
) -> None:
    conn, tmp_path = asset_db
    bible = _seed_bible_project(conn)
    primary = tmp_path / "hero-ep20.jpg"
    primary.write_bytes(b"paid-primary")
    conn.execute(
        """INSERT INTO character_portraits(
               id,project_id,character_name,ep_start,ep_end,appearance,prompt,image_path,
               base_portrait_id,bible_version,artifact_id,pack_status,created_at
           ) VALUES('portrait_interrupted','proj_bootstrap','Hero',20,20,?,?,?,?,1,NULL,'generating',1)""",
        (bible.characters[0].appearance_canonical, "portrait prompt", str(primary), None),
    )
    conn.commit()

    async def forbidden_primary(*_args, **_kwargs):
        raise AssertionError("侧视角恢复失败也不得重复生成主图")

    async def failed_pack(**_kwargs):
        raise hiagent.ProviderError("provider unavailable after restart")

    monkeypatch.setattr(portraits, "_generate_fresh_portrait", forbidden_primary)
    monkeypatch.setattr(multiview, "ensure_character_multiview_pack", failed_pack)

    with pytest.raises(hiagent.ProviderError, match="provider unavailable"):
        asyncio.run(portraits._generate_discovered_character_portrait(
            "proj_bootstrap",
            "Hero",
            bible.world.visual_style_canonical,
            bible.characters[0].appearance_canonical,
            ep_start=20,
            bible_version=1,
        ))

    row = conn.execute(
        "SELECT id,ep_end,pack_status FROM character_portraits "
        "WHERE project_id='proj_bootstrap' AND character_name='Hero'",
    ).fetchone()
    assert (row["id"], row["ep_end"], row["pack_status"]) == (
        "portrait_interrupted", 20, "generating",
    )
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM character_portraits "
        "WHERE project_id='proj_bootstrap' AND character_name='Hero'",
    ).fetchone()["c"] == 1


def test_failed_batch_refresh_preserves_previous_ready_pack(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_bible_project(conn)
    _patch_successful_character_generation(monkeypatch)
    asyncio.run(refs.generate_refs("proj_bootstrap"))
    previous = conn.execute(
        "SELECT id, image_path FROM character_portraits "
        "WHERE project_id='proj_bootstrap' AND character_name='Hero' AND ep_start=1"
    ).fetchone()

    async def fail_new_pack(**_kwargs):
        return {"status": "failed", "failed_view": "profile"}

    monkeypatch.setattr(multiview, "ensure_character_multiview_pack", fail_new_pack)
    with pytest.raises(Exception, match="多视角包结构不完整"):
        asyncio.run(refs.generate_refs("proj_bootstrap"))

    rows = conn.execute(
        "SELECT id, ep_start, image_path, pack_status FROM character_portraits "
        "WHERE project_id='proj_bootstrap' AND character_name='Hero' ORDER BY ep_start"
    ).fetchall()
    assert len(rows) == 1
    current = rows[0]
    assert current["id"] == previous["id"]
    assert current["image_path"] == previous["image_path"]
    assert current["pack_status"] == "ready"


def test_staged_refresh_pack_is_not_exposed_or_selected_before_qa(
    asset_db, monkeypatch,
) -> None:
    conn, tmp_path = asset_db
    bible = _seed_bible_project(conn)
    _patch_successful_character_generation(monkeypatch)
    asyncio.run(refs.generate_refs("proj_bootstrap"))
    previous = conn.execute(
        "SELECT id, image_path FROM character_portraits "
        "WHERE project_id='proj_bootstrap' AND character_name='Hero' AND ep_start=1"
    ).fetchone()
    candidate_path = tmp_path / "candidate.jpg"
    candidate_path.write_bytes(b"candidate")

    candidate_id = portraits.stage_initial_portrait(
        conn, "proj_bootstrap", "Hero", str(candidate_path),
        bible.characters[0].appearance_canonical, "new prompt", 1,
    )

    assert portraits.portrait_for_episode("proj_bootstrap", "Hero", 1) == previous["image_path"]
    visible = api.project_detail("proj_bootstrap", view="bible")["bible"]["characters"][0]["portraits"]
    assert [item["id"] for item in visible] == [previous["id"]]
    assert candidate_id not in {item["id"] for item in visible}


def test_manual_refresh_replaces_all_timeline_segments_from_episode_one(
    asset_db,
) -> None:
    conn, tmp_path = asset_db
    bible = _seed_bible_project(conn)
    initial_path = tmp_path / "initial.jpg"
    current_path = tmp_path / "current.jpg"
    candidate_path = tmp_path / "candidate.jpg"
    for path in (initial_path, current_path, candidate_path):
        path.write_bytes(b"image")

    initial_id = portraits.register_initial_portrait(
        conn, "proj_bootstrap", "Hero", str(initial_path),
        bible.characters[0].appearance_canonical, "initial prompt", 1,
    )
    conn.execute(
        "UPDATE character_portraits SET ep_end=10 WHERE id=?",
        (initial_id,),
    )
    current_id = "portrait_current_11"
    conn.execute(
        "INSERT INTO character_portraits("
        "id, project_id, character_name, ep_start, ep_end, appearance, prompt, "
        "image_path, bible_version, pack_status, created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            current_id, "proj_bootstrap", "Hero", 11, None,
            bible.characters[0].appearance_canonical, "episode 11 prompt",
            str(current_path), 1, "ready", 2,
        ),
    )
    conn.commit()

    candidate_id = portraits.stage_initial_portrait(
        conn, "proj_bootstrap", "Hero", str(candidate_path),
        bible.characters[0].appearance_canonical, "latest prompt", 1,
    )
    portraits.promote_staged_initial_portrait(
        conn, "proj_bootstrap", "Hero", candidate_id,
    )

    rows = conn.execute(
        "SELECT id, ep_start, ep_end FROM character_portraits "
        "WHERE project_id='proj_bootstrap' AND character_name='Hero' "
        "ORDER BY ep_start"
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    assert by_id[initial_id]["ep_start"] <= 0
    assert by_id[initial_id]["ep_end"] == 0
    assert by_id[current_id]["ep_start"] <= 0
    assert by_id[current_id]["ep_end"] == 0
    assert by_id[initial_id]["ep_start"] != by_id[current_id]["ep_start"]
    assert by_id[candidate_id]["ep_start"] == 1
    assert by_id[candidate_id]["ep_end"] is None


def test_recovered_fresh_batch_does_not_skip_pre_batch_ready_pack(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_bible_project(conn)
    _patch_successful_character_generation(monkeypatch)
    asyncio.run(refs.generate_refs("proj_bootstrap"))
    previous = conn.execute(
        "SELECT id, created_at FROM character_portraits "
        "WHERE project_id='proj_bootstrap' AND character_name='Hero' AND ep_start=1"
    ).fetchone()
    conn.execute("UPDATE character_portraits SET created_at=1 WHERE id=?", (previous["id"],))
    conn.commit()

    # 模拟“全量重生”在旧包之后启动，随后服务重启并以 resume=True 恢复。
    asyncio.run(refs.generate_refs(
        "proj_bootstrap", resume=True, fresh_after=2,
    ))

    current = conn.execute(
        "SELECT id, ep_start, pack_status FROM character_portraits "
        "WHERE project_id='proj_bootstrap' AND character_name='Hero' AND ep_start=1"
    ).fetchone()
    assert current["id"] != previous["id"]
    assert current["ep_start"] == 1
    assert current["pack_status"] == "ready"


def test_project_detail_uses_latest_ready_pack_when_bible_path_is_not_merged(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_bible_project(conn)
    _patch_successful_character_generation(monkeypatch)

    asyncio.run(refs.generate_refs("proj_bootstrap"))

    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_bootstrap'"
    ).fetchone()["bible_json"])
    bible["characters"][0]["ref_image_path"] = None
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='proj_bootstrap'",
        (json.dumps(bible),),
    )
    conn.commit()

    detail = api.project_detail("proj_bootstrap", view="bible")
    character = detail["bible"]["characters"][0]
    assert character["ref_image_url"] == character["portraits"][0]["image_url"]


def test_successful_character_is_checkpointed_before_later_character_fails(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    bible = _seed_bible_project(conn)
    bible.characters.append(Character(
        name="Villain",
        role="support",
        appearance_canonical="older villain, silver hair, red coat, lean build",
    ))
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='proj_bootstrap'",
        (bible.model_dump_json(),),
    )
    conn.commit()
    _patch_successful_character_generation(monkeypatch)

    encoded = base64.b64encode(b"test-image").decode("ascii")

    async def fail_second_character(prompt, *_args, **_kwargs):
        if "red coat" in prompt:
            raise hiagent.ProviderError("provider unavailable")
        return {"b64_json": encoded}

    monkeypatch.setattr(refs.hiagent, "generate_image", fail_second_character)

    with pytest.raises(Exception, match="provider unavailable"):
        asyncio.run(refs.generate_refs("proj_bootstrap"))

    persisted = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_bootstrap'"
    ).fetchone()["bible_json"])
    by_name = {character["name"]: character for character in persisted["characters"]}
    assert by_name["Hero"]["ref_image_path"]
    assert by_name["Villain"]["ref_image_path"] is None


def test_failed_initial_pack_is_not_published(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_bible_project(conn)
    _patch_successful_character_generation(monkeypatch)

    async def failed_pack(**_kwargs):
        return {"status": "failed", "failed_view": "profile"}

    monkeypatch.setattr(multiview, "ensure_character_multiview_pack", failed_pack)

    with pytest.raises(Exception, match="多视角包结构不完整"):
        asyncio.run(refs.generate_refs("proj_bootstrap"))

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM character_portraits WHERE project_id='proj_bootstrap'"
    ).fetchone()["c"] == 0
    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_bootstrap'"
    ).fetchone()["bible_json"])
    assert bible["characters"][0]["ref_image_path"] is None


def test_failed_single_image_qa_is_visible_as_non_adoptable_candidate(
    asset_db, monkeypatch,
) -> None:
    conn, tmp_path = asset_db
    _seed_bible_project(conn)
    candidate = tmp_path / "projects" / "proj_bootstrap" / "refs" / "failed-front.jpg"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"candidate")
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,content_json,file_path,
               content_hash,parent_artifact_ids_json,contract_version,model_snapshot_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "art_failed_front", "character_portrait", "reference_asset",
            "proj_bootstrap:Hero:1", 1, "candidate", "T1",
            json.dumps({"character_name": "Hero", "attempt": 2}), str(candidate),
            "hash", "[]", "reference-1.0.0", "{}", 2,
        ),
    )
    conn.execute(
        """INSERT INTO evaluations(
               id,artifact_id,evaluator_type,evaluator_name,evaluator_version,status,
               hard_gate_passed,score,dimension_scores_json,issues_json,evidence_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "eval_failed_front", "art_failed_front", "model",
            "character_portrait_consistency_qa", "1", "failed", 0, 30,
            "{}",
            json.dumps([{
                "code": "REFERENCE_QUALITY_THRESHOLD", "severity": "blocker",
                "message": "参考资产质量分未达到 0.60",
            }], ensure_ascii=False),
            json.dumps({"qa": {"overall": 0.3, "issues": ["视觉年龄不符"]}}, ensure_ascii=False),
            2,
        ),
    )
    conn.commit()
    monkeypatch.setattr(bible_ops, "get_conn", lambda: conn)

    result = asyncio.run(bible_ops.list_portrait_candidates("proj_bootstrap", "Hero"))

    item = result["items"][0]
    assert item["candidate_kind"] == "single_image"
    assert item["adoptable"] is False
    assert item["group_qa"]["overall"] == 0.3
    assert "参考资产质量分未达到 0.60" in item["group_qa"]["hard_failures"]
    assert item["image_url"]


def test_failed_single_image_qa_fails_batch_without_publishing(
    asset_db, monkeypatch,
) -> None:
    _seed_bible_project(asset_db[0])
    encoded = base64.b64encode(b"test-image").decode("ascii")

    async def fake_image(*_args, **_kwargs):
        return {"b64_json": encoded}

    async def failed_qa(*_args, **_kwargs):
        return {"overall": 0.3, "status": "failed", "issues": ["视觉年龄不符"]}

    monkeypatch.setattr(refs.hiagent, "generate_image", fake_image)
    monkeypatch.setattr(stages, "review_portrait_image", failed_qa)
    monkeypatch.setattr(
        refs,
        "record_reference_asset",
        lambda **_kwargs: {"id": "artifact_failed", "status": "candidate"},
    )

    with pytest.raises(Exception, match="技术校验未通过"):
        asyncio.run(refs.generate_refs("proj_bootstrap"))


def test_expression_warning_seed_continues_into_multiview_pack(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_bible_project(conn)
    encoded = base64.b64encode(b"\xff\xd8\xff\xe0valid-test-image").decode("ascii")
    pack_calls: list[str] = []

    async def fake_image(*_args, **_kwargs):
        return {"b64_json": encoded}

    async def expression_warning_qa(*_args, **_kwargs):
        return {
            "identity_match": 0.92,
            "presentation_match": 0.3,
            "clean_frame": 1.0,
            "overall": 0.92,
            "issues": ["眼神未体现炽热爱慕"],
            "soft_warnings": ["眼神未体现炽热爱慕"],
            "hard_failures": [],
            "hard_gate_passed": True,
            "status": "warning",
        }

    async def fake_pack(**kwargs):
        pack_calls.append(kwargs["portrait_id"])
        conn.execute(
            "UPDATE character_portraits SET pack_status='ready', group_qa_json=? WHERE id=?",
            (json.dumps({"status": "warning", "overall": 0.9, "issues": ["神态软警告"]}), kwargs["portrait_id"]),
        )
        conn.commit()
        return {"status": "ready", "portrait_id": kwargs["portrait_id"]}

    monkeypatch.setattr(refs.hiagent, "generate_image", fake_image)
    monkeypatch.setattr(stages, "review_portrait_image", expression_warning_qa)
    monkeypatch.setattr(multiview, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(multiview, "ensure_character_multiview_pack", fake_pack)

    asyncio.run(refs.generate_refs("proj_bootstrap"))

    assert len(pack_calls) == 1
    portrait = conn.execute(
        "SELECT ep_start, pack_status FROM character_portraits WHERE project_id='proj_bootstrap'"
    ).fetchone()
    assert portrait["ep_start"] == 1
    assert portrait["pack_status"] == "ready"


def test_cancelled_initial_pack_removes_staged_front_only_portrait(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_bible_project(conn)
    _patch_successful_character_generation(monkeypatch)

    async def cancelled_pack(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(multiview, "ensure_character_multiview_pack", cancelled_pack)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(refs.generate_refs("proj_bootstrap"))

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM character_portraits WHERE project_id='proj_bootstrap'"
    ).fetchone()["c"] == 0


def test_resume_completes_partial_character_pack_instead_of_skipping_it(
    asset_db, monkeypatch,
) -> None:
    conn, tmp_path = asset_db
    bible = _seed_bible_project(conn)
    front = tmp_path / "front.jpg"
    front.write_bytes(b"front")
    portraits.register_initial_portrait(
        conn,
        "proj_bootstrap",
        "Hero",
        str(front),
        bible.characters[0].appearance_canonical,
        "front prompt",
        1,
    )
    completed: list[str] = []

    async def complete_pack(project_id: str, character_name: str, episode_no: int, visual_style: str):
        completed.append(character_name)
        row = conn.execute(
            "SELECT id FROM character_portraits WHERE project_id=? AND character_name=?",
            (project_id, character_name),
        ).fetchone()
        conn.execute("UPDATE character_portraits SET pack_status='ready' WHERE id=?", (row["id"],))
        for role in ("front_full", "three_quarter", "profile"):
            conn.execute(
                "INSERT INTO character_portrait_views(" 
                "id, portrait_id, view_role, image_path, status, created_at) VALUES(?,?,?,?,?,?)",
                (f"view_{role}", row["id"], role, str(front), "ready", 1),
            )
        conn.commit()
        return {"status": "ready", "portrait_id": row["id"]}

    async def should_not_regenerate(*_args, **_kwargs):
        raise AssertionError("front image should not be regenerated during resume")

    monkeypatch.setattr(multiview, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(multiview, "complete_legacy_character_pack", complete_pack)
    monkeypatch.setattr(refs.hiagent, "generate_image", should_not_regenerate)

    asyncio.run(refs.generate_refs("proj_bootstrap", resume=True))

    assert completed == ["Hero"]
    persisted = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_bootstrap'"
    ).fetchone()["bible_json"])
    assert persisted["characters"][0]["ref_image_path"] == str(front)


def test_scene_exists_accepts_primary_fallback_when_multiview_is_incomplete(
    asset_db, monkeypatch,
) -> None:
    conn, tmp_path = asset_db
    _seed_bible_project(conn, with_scene=True)
    image = tmp_path / "scene.jpg"
    image.write_bytes(b"scene")
    scene_id = scenes.register_initial_scene_ref(
        conn,
        "proj_bootstrap",
        "Courtyard",
        str(image),
        "stone courtyard at dawn with a red gate",
        "prompt",
        {"overall": 0.95},
        1,
    )
    monkeypatch.setattr(multiview, "scene_multiview_enabled", lambda: True)
    assert scenes.scene_ref_exists(conn, "proj_bootstrap", "Courtyard") is True

    conn.execute("UPDATE scene_references SET pack_status='ready' WHERE id=?", (scene_id,))
    for role in ("establishing", "reverse_angle"):
        conn.execute(
            "INSERT INTO scene_reference_views(" 
            "id, scene_reference_id, view_role, image_path, status, created_at) VALUES(?,?,?,?,?,?)",
            (f"scene_view_{role}", scene_id, role, str(image), "ready", 1),
        )
    conn.commit()
    assert scenes.scene_ref_exists(conn, "proj_bootstrap", "Courtyard") is True


def test_failed_extra_view_pack_keeps_primary_scene_available_to_video(
    asset_db, monkeypatch,
) -> None:
    conn, tmp_path = asset_db
    _seed_bible_project(conn, with_scene=True)
    image = tmp_path / "usable-primary.jpg"
    image.write_bytes(b"scene")
    qa = {
        "overall": 0.9,
        "status": "warning",
        "hard_gate_passed": True,
        "hard_failures": [],
    }
    scene_id = scenes.register_initial_scene_ref(
        conn, "proj_bootstrap", "Courtyard", str(image),
        "stone courtyard at dawn with a red gate", "prompt", qa, 1,
    )
    conn.execute(
        "UPDATE scene_references SET pack_status='failed',group_qa_json=? WHERE id=?",
        (json.dumps({
            "status": "failed",
            "hard_failures": ["缺少必需视角：reverse_angle"],
        }, ensure_ascii=False), scene_id),
    )
    conn.execute(
        "INSERT INTO scene_reference_views("
        "id,scene_reference_id,view_role,image_path,qa_json,status,created_at) "
        "VALUES('primary',?,'establishing',?,?,'ready',1)",
        (scene_id, str(image), json.dumps(qa)),
    )
    conn.commit()
    monkeypatch.setattr(multiview, "scene_multiview_enabled", lambda: True)

    row = conn.execute("SELECT * FROM scene_references WHERE id=?", (scene_id,)).fetchone()
    views = multiview.list_scene_views(scene_id, conn=conn)
    assert multiview.scene_pack_is_usable(row, views) is False
    assert multiview.scene_primary_is_usable(row, views) is True
    assert scenes.scene_ref_for_episode("proj_bootstrap", "Courtyard", 1) == str(image)


def test_pending_reverse_view_is_reviewed_without_regeneration(asset_db, monkeypatch) -> None:
    conn, tmp_path = asset_db
    _seed_bible_project(conn, with_scene=True)
    primary = tmp_path / "primary.jpg"
    reverse = tmp_path / "reverse.jpg"
    primary.write_bytes(b"primary")
    reverse.write_bytes(b"reverse")
    qa = {
        "overall": 0.9,
        "status": "warning",
        "hard_gate_passed": True,
        "hard_failures": [],
        "policy_version": "scene-practical-quality-1.1.0",
    }
    scene_id = scenes.register_initial_scene_ref(
        conn, "proj_bootstrap", "Courtyard", str(primary),
        "stone courtyard at dawn with a red gate", "prompt", qa, 1,
    )
    for role, path, status, view_qa in (
        ("establishing", primary, "ready", qa),
        ("reverse_angle", reverse, "qa_pending", None),
    ):
        conn.execute(
            "INSERT INTO scene_reference_views("
            "id,scene_reference_id,view_role,image_path,qa_json,status,created_at) "
            "VALUES(?,?,?,?,?,?,1)",
            (f"view-{role}", scene_id, role, str(path),
             json.dumps(view_qa) if view_qa else None, status),
        )
    conn.commit()
    reviewed = []

    async def must_not_generate(*_args, **_kwargs):
        raise AssertionError("qa_pending 反打图已存在，不得重复付费生成")

    async def review_view(path, canonical, role):
        reviewed.append(role)
        return {**qa, "view_role": role}

    async def review_pack(views, canonical):
        return {
            "overall": 0.9,
            "status": "ready",
            "hard_gate_passed": True,
            "hard_failures": [],
            "policy_version": "scene-practical-quality-1.1.0",
            "views": [
                {**qa, "view_role": view["view_role"], "status": "ready"}
                for view in views
            ],
        }

    monkeypatch.setattr(multiview, "_generate_image", must_not_generate)
    monkeypatch.setattr(multiview, "review_scene_view", review_view)
    monkeypatch.setattr(multiview, "review_scene_pack_consistency", review_pack)

    result = asyncio.run(multiview.ensure_scene_multiview_pack(
        project_id="proj_bootstrap",
        scene_reference_id=scene_id,
        scene_name="Courtyard",
        scene_canonical="stone courtyard at dawn with a red gate",
        visual_style="cinematic animation",
        ep_start=1,
        primary_qa=qa,
    ))

    assert result["status"] == "ready"
    assert reviewed == ["reverse_angle"]
    assert conn.execute(
        "SELECT status FROM scene_reference_views WHERE id='view-reverse_angle'",
    ).fetchone()["status"] == "ready"


def test_pending_character_side_view_is_reused_without_regeneration(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    bible = _seed_bible_project(conn)
    _patch_successful_character_generation(monkeypatch)
    asyncio.run(refs.generate_refs("proj_bootstrap"))
    portrait = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id='proj_bootstrap' "
        "AND character_name='Hero' AND ep_start=1",
    ).fetchone()
    profile = conn.execute(
        "SELECT image_path FROM character_portrait_views "
        "WHERE portrait_id=? AND view_role='profile'",
        (portrait["id"],),
    ).fetchone()
    Path(profile["image_path"]).unlink(missing_ok=True)
    conn.execute(
        "DELETE FROM character_portrait_views WHERE portrait_id=? AND view_role='profile'",
        (portrait["id"],),
    )
    conn.execute(
        "UPDATE character_portrait_views SET status='qa_pending',qa_json=NULL "
        "WHERE portrait_id=? AND view_role='three_quarter'",
        (portrait["id"],),
    )
    conn.execute(
        "UPDATE character_portraits SET pack_status='generating' WHERE id=?",
        (portrait["id"],),
    )
    conn.commit()
    generated_roles = []
    encoded = base64.b64encode(b"replacement-profile").decode("ascii")

    async def generate_only_missing(*_args, **kwargs):
        generated_roles.append(kwargs["call_meta"]["view_role"])
        return {"b64_json": encoded}

    monkeypatch.setattr(multiview, "_generate_image", generate_only_missing)

    result = asyncio.run(multiview.ensure_character_multiview_pack(
        project_id="proj_bootstrap",
        portrait_id=portrait["id"],
        character_name="Hero",
        appearance=bible.characters[0].appearance_canonical,
        visual_style=bible.world.visual_style_canonical,
        portrait_prompt=portrait["prompt"],
        ep_start=1,
    ))

    assert result["status"] == "ready"
    assert generated_roles == ["profile"]
    statuses = {
        row["view_role"]: row["status"]
        for row in conn.execute(
            "SELECT view_role,status FROM character_portrait_views WHERE portrait_id=?",
            (portrait["id"],),
        ).fetchall()
    }
    assert statuses == {
        "front_full": "ready",
        "three_quarter": "ready",
        "profile": "ready",
    }


def test_stale_assets_preview_accepts_sqlite_episode_rows(asset_db, monkeypatch) -> None:
    conn, _ = asset_db
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('proj_wall', 'Wall', 'planned', 1)"
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, storyboard_artifact_id, created_at) "
        "VALUES('ep_wall', 'proj_wall', 1, 'story_new', 1)"
    )
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, storyboard_artifact_id) "
        "VALUES('shot_wall', 'ep_wall', 1, 5, 'story_old')"
    )
    conn.commit()

    from app.domain import storyboard_ops

    monkeypatch.setattr(storyboard_ops, "_shot_video_is_stale", lambda *_args: True)
    result = api.stale_assets_preview("ep_wall")
    assert result["stale_count"] == 1
    assert result["shots"][0]["reasons"] == ["storyboard_artifact"]
