"""人物定妆照与场景定场图批量出图：各自独立的最多 5 并发池。

不只是"能跑通"——通过让每次伪造的 generate_image 调用短暂 sleep，测出真实的
同时在飞并发峰值，断言峰值恰好命中池上限（不多不少）。角色/场景数量刻意取
9（> 5），确保上限真的生效，而不是"凑巧全部并发也不超过 5"。
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading

import pytest

from app import config, db, multiview, refs, scenes
from app.generation_concurrency import (
    CHARACTER_PORTRAIT_BATCH_CONCURRENCY,
    SCENE_REFERENCE_BATCH_CONCURRENCY,
)
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


def _concurrency_probe(delay: float = 0.05):
    """Fake provider call that records in-flight concurrency while it sleeps."""
    state = {"active": 0, "peak": 0}

    async def probe() -> None:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(delay)
        state["active"] -= 1

    return probe, state


def test_character_portrait_batch_caps_concurrency_at_pool_size(
    asset_db, monkeypatch,
) -> None:
    """9 个角色一次批量出图：同时在飞的定妆照生成不得超过 5，且必须真正用满 5。"""
    conn, _ = asset_db
    n_characters = 9
    assert n_characters > CHARACTER_PORTRAIT_BATCH_CONCURRENCY
    bible = Bible(
        world=World(visual_style_canonical="cinematic animation"),
        characters=[
            Character(
                name=f"Char{i}",
                role="lead",
                appearance_canonical=f"character number {i}, distinctive look, tall build, blue coat",
            )
            for i in range(n_characters)
        ],
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES('proj_conc_char', 'ConcChar', 'bible_ready', ?, 1, 1)",
        (bible.model_dump_json(),),
    )
    conn.commit()

    encoded = base64.b64encode(b"test-image").decode("ascii")
    probe, state = _concurrency_probe()

    async def fake_image(*_args, **_kwargs):
        await probe()
        return {"b64_json": encoded}

    monkeypatch.setattr(refs.hiagent, "generate_image", fake_image)
    # 多视角包内部还有正面→侧面的有序/并发生成，与本测试要验证的"跨角色并发池"
    # 是两回事；关掉它让每个角色恰好对应一次 generate_image 调用，峰值可干净归因。
    monkeypatch.setattr(multiview, "character_multiview_enabled", lambda: False)
    monkeypatch.setattr(
        refs,
        "record_reference_asset",
        lambda **_kwargs: {"id": "artifact_portrait", "status": "approved"},
    )

    result = asyncio.run(refs.generate_refs("proj_conc_char"))

    assert state["peak"] == CHARACTER_PORTRAIT_BATCH_CONCURRENCY
    assert result["generated"] == [c.name for c in bible.characters]

    rows = conn.execute(
        "SELECT character_name, image_path FROM character_portraits WHERE project_id='proj_conc_char'"
    ).fetchall()
    assert {row["character_name"] for row in rows} == {c.name for c in bible.characters}

    # 并发下多个角色同时把 ref_image_path 合并进 bible_json：bible_merge_lock 必须
    # 保证没有任何一次合并被后写者静默覆盖丢失。
    bible_after = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_conc_char'"
    ).fetchone()["bible_json"])
    paths = [c["ref_image_path"] for c in bible_after["characters"]]
    assert all(paths)
    assert len(set(paths)) == n_characters


def test_scene_reference_batch_caps_concurrency_at_pool_size(
    asset_db, monkeypatch,
) -> None:
    """9 个场景一次批量出图：同时在飞的定场图生成不得超过 5，且必须真正用满 5。"""
    conn, _ = asset_db
    n_scenes = 9
    assert n_scenes > SCENE_REFERENCE_BATCH_CONCURRENCY
    bible = Bible(
        world=World(visual_style_canonical="cinematic animation"),
        characters=[],
        scenes=[
            Scene(
                name=f"Scene{i}",
                scene_canonical=f"stone courtyard number {i} at dawn with a red gate and lanterns",
            )
            for i in range(n_scenes)
        ],
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES('proj_conc_scene', 'ConcScene', 'bible_ready', ?, 1, 1)",
        (bible.model_dump_json(),),
    )
    conn.commit()

    encoded = base64.b64encode(b"test-image").decode("ascii")
    probe, state = _concurrency_probe()

    async def fake_generate(*_args, **_kwargs):
        await probe()
        return {"b64_json": encoded}

    monkeypatch.setattr(scenes, "_generate_scene_image", fake_generate)
    # 场景多视角包同理关闭，让每个场景恰好对应一次 _generate_scene_image 调用。
    monkeypatch.setattr(multiview, "scene_multiview_enabled", lambda: False)
    monkeypatch.setattr(
        scenes,
        "record_reference_asset",
        lambda **_kwargs: {"id": "artifact_scene", "status": "approved"},
    )

    result = asyncio.run(scenes.generate_scene_refs("proj_conc_scene"))

    assert state["peak"] == SCENE_REFERENCE_BATCH_CONCURRENCY
    assert result["generated"] == [s.name for s in bible.scenes]

    rows = conn.execute(
        "SELECT scene_name, image_path FROM scene_references WHERE project_id='proj_conc_scene'"
    ).fetchall()
    assert {row["scene_name"] for row in rows} == {s.name for s in bible.scenes}

    bible_after = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_conc_scene'"
    ).fetchone()["bible_json"])
    paths = [s["ref_image_path"] for s in bible_after["scenes"]]
    assert all(paths)
    assert len(set(paths)) == n_scenes


def test_character_and_scene_batches_use_independent_pools(asset_db, monkeypatch) -> None:
    """人物池与场景池互相独立：同时跑两个批次，互相不挤占对方的并发槽位。

    人物池撑满 5 个并占住（不放行），同一时刻场景池仍应各自独立跑到自己的上限，
    证明两者不是共享同一个信号量。
    """
    conn, _ = asset_db
    n_characters = CHARACTER_PORTRAIT_BATCH_CONCURRENCY
    n_scenes = SCENE_REFERENCE_BATCH_CONCURRENCY
    bible = Bible(
        world=World(visual_style_canonical="cinematic animation"),
        characters=[
            Character(
                name=f"Char{i}", role="lead",
                appearance_canonical=f"character number {i}, distinctive look, tall build, blue coat",
            )
            for i in range(n_characters)
        ],
        scenes=[
            Scene(
                name=f"Scene{i}",
                scene_canonical=f"stone courtyard number {i} at dawn with a red gate and lanterns",
            )
            for i in range(n_scenes)
        ],
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES('proj_conc_both', 'ConcBoth', 'bible_ready', ?, 1, 1)",
        (bible.model_dump_json(),),
    )
    conn.commit()

    encoded = base64.b64encode(b"test-image").decode("ascii")
    char_probe, char_state = _concurrency_probe(delay=0.15)
    scene_probe, scene_state = _concurrency_probe(delay=0.05)

    async def fake_image(*_args, **_kwargs):
        await char_probe()
        return {"b64_json": encoded}

    async def fake_generate(*_args, **_kwargs):
        await scene_probe()
        return {"b64_json": encoded}

    monkeypatch.setattr(refs.hiagent, "generate_image", fake_image)
    monkeypatch.setattr(multiview, "character_multiview_enabled", lambda: False)
    monkeypatch.setattr(
        refs, "record_reference_asset",
        lambda **_kwargs: {"id": "artifact_portrait", "status": "approved"},
    )
    monkeypatch.setattr(scenes, "_generate_scene_image", fake_generate)
    monkeypatch.setattr(multiview, "scene_multiview_enabled", lambda: False)
    monkeypatch.setattr(
        scenes, "record_reference_asset",
        lambda **_kwargs: {"id": "artifact_scene", "status": "approved"},
    )

    async def _run_both():
        return await asyncio.gather(
            refs.generate_refs("proj_conc_both"),
            scenes.generate_scene_refs("proj_conc_both"),
        )

    char_result, scene_result = asyncio.run(_run_both())

    # 人物池撑到自己的上限（用更长的 delay 保证它在场景批次跑完前仍占着槽位）。
    assert char_state["peak"] == CHARACTER_PORTRAIT_BATCH_CONCURRENCY
    # 场景池同时也跑到了自己的上限——如果两者共享一个池子，人物已经占满 5 个
    # 槽位时场景池会被挤到 0，peak 就不可能达到 SCENE_REFERENCE_BATCH_CONCURRENCY。
    assert scene_state["peak"] == SCENE_REFERENCE_BATCH_CONCURRENCY
    assert char_result["generated"] == [c.name for c in bible.characters]
    assert scene_result["generated"] == [s.name for s in bible.scenes]
