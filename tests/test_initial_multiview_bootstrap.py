from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path

import pytest

from app import api, config, db, hiagent, multiview, portraits, refs, scenes, stages
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

    with pytest.raises(hiagent.ProviderError):
        asyncio.run(refs.generate_refs("proj_bootstrap"))

    persisted = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_bootstrap'"
    ).fetchone()["bible_json"])
    by_name = {character["name"]: character for character in persisted["characters"]}
    assert by_name["Hero"]["ref_image_path"]
    assert by_name["Villain"]["ref_image_path"] is None


def test_failed_initial_pack_is_not_exposed_as_front_only_portrait(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_bible_project(conn)
    _patch_successful_character_generation(monkeypatch)

    async def failed_pack(**_kwargs):
        return {"status": "failed", "failed_view": "profile"}

    monkeypatch.setattr(multiview, "ensure_character_multiview_pack", failed_pack)

    with pytest.raises(hiagent.ProviderError):
        asyncio.run(refs.generate_refs("proj_bootstrap"))

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM character_portraits WHERE project_id='proj_bootstrap'"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM character_portrait_views"
    ).fetchone()["c"] == 0
    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_bootstrap'"
    ).fetchone()["bible_json"])
    assert bible["characters"][0]["ref_image_path"] is None


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


def test_scene_exists_requires_complete_pack_when_multiview_is_enabled(
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
    assert scenes.scene_ref_exists(conn, "proj_bootstrap", "Courtyard") is False

    conn.execute("UPDATE scene_references SET pack_status='ready' WHERE id=?", (scene_id,))
    for role in ("establishing", "reverse_angle"):
        conn.execute(
            "INSERT INTO scene_reference_views(" 
            "id, scene_reference_id, view_role, image_path, status, created_at) VALUES(?,?,?,?,?,?)",
            (f"scene_view_{role}", scene_id, role, str(image), "ready", 1),
        )
    conn.commit()
    assert scenes.scene_ref_exists(conn, "proj_bootstrap", "Courtyard") is True


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
