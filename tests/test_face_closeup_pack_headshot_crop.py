"""face_closeup 的整包生成流程（app.multiview.ensure_character_multiview_pack
→ app.refs.generate_refs）绝不调用生图模型，改走 app.portraits.
character_side_view.crop_headshot_from_portrait（2026-10-02）。

与 tests/test_character_view_redo_headshot_crop.py 分工：那边测单视角重做
（app.multiview.regenerate_character_view）直接调用的
app.multiview.crop_headshot_from_portrait 绑定；这里测整包生成走的是
app.portraits.character_side_view 自己持有的绑定——两条路径各自独立 import，
monkeypatch 必须分别打在各自真正被调用的那个绑定上（CLAUDE.md「拆包会静默
废掉 monkeypatch」）。独立成文件只是为了不挤占 test_initial_multiview_
bootstrap.py 已经顶格的 line_count 棘轮基线，不是拆分测试关注点。
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path

import pytest

from app import config, db, multiview, refs
from app.portraits import character_side_view
from app.schemas import Bible, Character, World


@pytest.fixture
def asset_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "face-closeup-pack.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn()
    db.get_conn().close()


def _seed_bible_project(conn) -> None:
    bible = Bible(
        world=World(visual_style_canonical="cinematic animation"),
        characters=[Character(
            name="Hero", role="lead",
            appearance_canonical="young hero, short black hair, blue coat, tall build",
        )],
        scenes=[],
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES('proj_bootstrap', 'Bootstrap', 'bible_ready', ?, 1, 1)", (bible.model_dump_json(),),
    )
    conn.commit()


def test_face_closeup_never_calls_generate_image(asset_db, monkeypatch) -> None:
    conn = asset_db
    _seed_bible_project(conn)
    encoded = base64.b64encode(b"test-image").decode("ascii")
    crop_calls = []

    async def generate_image_rejects_face_closeup(*_args, **kwargs):
        assert kwargs.get("call_meta", {}).get("view_role") != "face_closeup"
        return {"b64_json": encoded}

    async def fake_crop(source_path, *, dest_path, call_meta):
        crop_calls.append((source_path, call_meta.get("character_name")))
        Path(dest_path).write_bytes(b"cropped")
        return {"provenance_preserved": False, "head_box": [0.3, 0.1, 0.7, 0.4],
                "clothing_top_y": 0.42, "crop_box_px": [0, 0, 10, 10],
                "output_size": [768, 921], "source_path": source_path}

    monkeypatch.setattr(refs.hiagent, "generate_image", generate_image_rejects_face_closeup)
    monkeypatch.setattr(multiview, "_generate_image", generate_image_rejects_face_closeup)
    monkeypatch.setattr(character_side_view, "crop_headshot_from_portrait", fake_crop)
    monkeypatch.setattr(multiview, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(
        refs, "record_reference_asset",
        lambda **_kwargs: {"id": "artifact_portrait", "status": "approved"},
    )

    asyncio.run(refs.generate_refs("proj_bootstrap"))

    assert len(crop_calls) == 1
    assert crop_calls[0][1] == "Hero"
    portrait = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id='proj_bootstrap' AND character_name='Hero'"
    ).fetchone()
    assert portrait["pack_status"] == "ready"
    qa_row = conn.execute(
        "SELECT qa_json FROM character_portrait_views WHERE portrait_id=? AND view_role='face_closeup'",
        (portrait["id"],),
    ).fetchone()
    qa = json.loads(qa_row["qa_json"])
    assert qa["provenance_preserved"] is False
    assert qa["head_box"] == [0.3, 0.1, 0.7, 0.4]
