"""接线守卫 + 端到端：场景状态与场景卡不一致时省略场景参考图（2026-10-01，
《顾念长安》第 1 集出租屋被淹场景根因调查落地）。

覆盖 yes/no/unsure/字段缺失（旧数据）四种形状的装配结果与可见信号；并钉住
生产硬门禁（``manifest_production_blockers``/``scan_episode_reference_asset_
gaps``）不会把"按设计省略"误判成"缺图"，2.x 主通路的图库回退
（``app.video_modes.reference_assemble``）不会把被省略的场景图绕回来。
"""
from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from app import db
from app import multiview as mv
from app.multiview import _storyboard_pack_asset_dependencies, manifest_production_blockers, scan_episode_reference_asset_gaps
from app.production.storyboard_pack import _segment_content_advisories
from app.production.storyboard_segment_resources import _AiResourceScene, _AiSegmentResources
from app.schemas import Bible, Scene, Shot, World
from app.video_modes.reference_assemble import _build_library_reference_assets
from tests.test_storyboard_pack import _draft

_TAG = "[STORYBOARD_SCENE_REF_OMITTED_STATE_CHANGED][未拦截]"


# ---------- 源码级接线守卫（防止字段加了却没接进真正消费它的通路） ----------

def test_resolve_scene_entry_calls_resolve_scene_reference_entry():
    source = inspect.getsource(mv._storyboard_pack_asset_dependencies)
    assert "resolve_scene_reference_entry(" in source
    assert 'scene_entry.get("scene_state_matches_card")' in source
    assert 'entry["scene_state_omitted_reason"]' in source


def test_resolve_scene_reference_entry_calls_omission_reason():
    from app.video_modes import scene_state_selection

    source = inspect.getsource(scene_state_selection.resolve_scene_reference_entry)
    assert "scene_reference_omission_reason(" in source
    assert '"scene_state_omitted_reason": omitted_reason' in source


def test_manifest_production_blockers_skips_state_omitted_scene():
    source = inspect.getsource(mv.manifest_production_blockers)
    assert 'scene.get("scene_state_omitted_reason")' in source


def test_scan_episode_reference_asset_gaps_skips_state_omitted_scene():
    source = inspect.getsource(mv.scan_episode_reference_asset_gaps)
    assert 'scene.get("scene_state_omitted_reason")' in source


def test_reference_assemble_scene_fallback_respects_state_omission():
    from app.video_modes import reference_assemble

    source = inspect.getsource(reference_assemble._build_library_reference_assets)
    assert "scene_state_omitted" in source
    assert "scene_reference_assets(" in source


# ---------- 端到端：真实 sqlite，四种 scene_state_matches_card 形状 ----------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-state-wiring.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _seed_scene(conn, tmp_path) -> None:
    image = tmp_path / "出租屋.png"
    image.write_bytes(b"dry-room")
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        ("proj-1", "出租屋 fixture", "{}", db.now()),
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, image_path, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_a", "proj-1", "温念的出租屋", 1, None, str(image), db.now()),
    )
    conn.commit()


def _manifest_for(conn, scene_resource: dict):
    segment = {
        "segment_no": 18, "prompt_text": "温念推开门，屋内积水没过脚踝。",
        "resources": {"characters": [], "scenes": [scene_resource]},
    }
    bible = Bible(characters=[], world=World(visual_style_canonical="写实"),
                  scenes=[Scene(name="温念的出租屋", scene_canonical="温念的出租屋，干燥整洁")])
    return _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id="shot-18", segment=segment, conn=conn, bible=bible,
    )


def _scene_resource(**extra) -> dict:
    return {"scene_id": "scene:温念的出租屋", "scene_reference_id": None, **extra}


def test_yes_sends_scene_reference(tmp_path, caplog):
    conn = db.get_conn()
    _seed_scene(conn, tmp_path)
    with caplog.at_level(logging.INFO):
        manifest = _manifest_for(conn, _scene_resource(scene_state_matches_card="yes"))
    assert [v["view_role"] for v in manifest["scene"]["selected_views"]] == ["establishing"]
    assert manifest["scene"]["scene_state_omitted_reason"] is None
    assert _TAG not in caplog.text


def test_no_omits_scene_reference_and_is_not_a_production_blocker(tmp_path, caplog):
    conn = db.get_conn()
    _seed_scene(conn, tmp_path)
    with caplog.at_level(logging.INFO):
        manifest = _manifest_for(conn, _scene_resource(scene_state_matches_card="no"))
    assert manifest["scene"]["selected_views"] == []
    assert manifest["scene"]["scene_state_omitted_reason"] is not None
    assert _TAG in caplog.text
    # 卡片本身是存在的（scene_revision_id 非空），不该被判成"缺少本集场景版本"
    # 或"没有可用的场景图"——那两条都是真缺图的措辞，这里是按设计省略。
    assert manifest["scene"]["scene_revision_id"] == "scene_ref_a"
    assert manifest_production_blockers(manifest) == []


def test_unsure_is_treated_same_as_no(tmp_path):
    conn = db.get_conn()
    _seed_scene(conn, tmp_path)
    manifest = _manifest_for(conn, _scene_resource(scene_state_matches_card="unsure"))
    assert manifest["scene"]["selected_views"] == []
    assert manifest["scene"]["scene_state_omitted_reason"] is not None


def test_missing_field_legacy_row_preserves_pre_existing_send_behavior(tmp_path):
    """旧数据没有这个 key（字段上线前生成的分镜）：``.get()`` 返回 None，这是
    "从未被问过"，不是"问了答不出来"——按改动前的行为照常发送，不追溯套用新
    限制（否则会让全部存量分镜一次性停发场景图，远超本次要修的几段）。"""
    conn = db.get_conn()
    _seed_scene(conn, tmp_path)
    manifest = _manifest_for(conn, _scene_resource())
    assert [v["view_role"] for v in manifest["scene"]["selected_views"]] == ["establishing"]
    assert manifest["scene"]["scene_state_omitted_reason"] is None


def test_no_state_change_scene_not_counted_as_missing_in_episode_scan(tmp_path):
    conn = db.get_conn()
    _seed_scene(conn, tmp_path)
    shot = Shot(
        shot_no=18, duration_s=15, shot_size="中景", camera_move="固定", scene_setting="温念的出租屋",
        action_desc="温念推开门。", first_frame_desc="起幅。", last_frame_desc="落幅。",
        source_excerpt="温念推开门，屋内积水没过脚踝。",
    )
    segment = {
        "segment_no": 18, "prompt_text": "温念推开门，屋内积水没过脚踝。",
        "resources": {"characters": [], "scenes": [_scene_resource(scene_state_matches_card="no")]},
    }
    shot.storyboard_pack_segment = segment
    bible = Bible(characters=[], world=World(visual_style_canonical="写实"),
                  scenes=[Scene(name="温念的出租屋", scene_canonical="温念的出租屋，干燥整洁")])
    gaps = scan_episode_reference_asset_gaps(
        project_id="proj-1", episode_no=1, shots=[("shot-18", shot)], conn=conn, bible=bible,
    )
    assert gaps["scenes"] == []
    assert gaps["blockers"] == []


# ---------- 2.x 主通路：图库回退不得把省略决定绕过去 ----------

def _shot_for_fallback() -> Shot:
    return Shot(
        shot_no=1, duration_s=15, shot_size="中景", camera_move="固定",
        scene_setting="温念的出租屋", scene_name="温念的出租屋",
        action_desc="温念推开门。", first_frame_desc="起幅。", last_frame_desc="落幅。",
        source_excerpt="温念推开门，屋内积水没过脚踝。", characters=[], dialogues=[],
        transition="硬切", continuity_from_prev=False,
    )


def _library_manifest(*, scene_state_omitted_reason: str | None) -> dict:
    return {
        "episode_no": 1, "shot_id": "shot-1", "characters": [],
        "scene": {
            "name": "温念的出租屋", "asset_required": True, "scene_revision_id": "scene_ref_a",
            "pack_status": None, "asset_usable": False, "pack_usable": False, "primary_usable": False,
            "selected_view_ids": [], "selected_views": [], "available_view_roles": [],
            "missing_required": [], "scene_state_omitted_reason": scene_state_omitted_reason,
        },
        "additional_scenes": [], "keyframe_slot": "narrative_keyframe", "props": [],
        "input_fingerprint": "fp-test",
    }


def test_fallback_does_not_reintroduce_state_omitted_scene(tmp_path, monkeypatch):
    """真实图库里这张场景图确实存在——不靠「图库本来就查不到」侥幸通过；省略
    判据必须主动拦住回退，否则这里会真的查到图、把它塞回参考图列表。"""
    conn = db.get_conn()
    _seed_scene(conn, tmp_path)
    manifest = _library_manifest(scene_state_omitted_reason="本段场景状态与场景卡不一致")
    monkeypatch.setattr(mv, "resolve_shot_asset_dependencies", lambda **_k: manifest)
    result = asyncio.run(_build_library_reference_assets(
        conn=conn, project_id="proj-1", episode_no=1, episode_id="ep-1",
        shot_id="shot-1", shot=_shot_for_fallback(),
        bible=Bible(characters=[], world=World(visual_style_canonical="写实"),
                    scenes=[Scene(name="温念的出租屋", scene_canonical="温念的出租屋，干燥整洁")]),
        existing_meta={},
    ))
    assert not any((a.entity_type or a.type) == "scene" for a in result)


def test_fallback_still_fires_when_manifest_has_no_scene_section(monkeypatch):
    """旧式 name-based manifest（没有 2.x scene 字段）仍要能走图库回退——省略
    判据只拦"明确声明过的省略"，不能误伤"这条路径压根没算过状态"的旧形状。"""
    manifest = {
        "episode_no": 1, "shot_id": "shot-1", "characters": [], "scene": None,
        "additional_scenes": [], "keyframe_slot": "narrative_keyframe", "props": [],
        "input_fingerprint": "fp-test",
    }
    monkeypatch.setattr(mv, "resolve_shot_asset_dependencies", lambda **_k: manifest)
    called = {}

    def fake_scene_reference_assets(*_a, **_k):
        called["hit"] = True
        return []

    monkeypatch.setattr(
        "app.video_modes.reference_assemble.scene_reference_assets", fake_scene_reference_assets,
    )
    asyncio.run(_build_library_reference_assets(
        conn=object(), project_id="proj-1", episode_no=1, episode_id="ep-1",
        shot_id="shot-1", shot=_shot_for_fallback(),
        bible=Bible(characters=[], world=World(visual_style_canonical="写实")),
        existing_meta={},
    ))
    assert called.get("hit") is True


# ---------- 评审 #0（2026-10-02）：省略决定要写进本段 degraded_capabilities，
# 不能只靠装配期才能看到的后端日志——用户做「每轮分镜台后必须人工核查」时
# 看不到任何提示。生成时就能算（只看模型自报的 scene_state_matches_card，
# 不需要装配期才知道的 image_path/行记录），接进既有的
# _segment_content_advisories 通道即可。 ----------

def test_segment_content_advisories_calls_resource_scene_state_advisories():
    source = inspect.getsource(_segment_content_advisories)
    assert "resource_scene_state_advisories(" in source


def test_segment_content_advisories_flags_explicit_no_state_change():
    draft = _draft(resources=_AiSegmentResources(
        scenes=[_AiResourceScene(scene_id="scene:温念的出租屋", scene_state_matches_card="no")],
    ))
    advisories = _segment_content_advisories(
        draft, source_segment_indexes=[1], manifest=None,
        emotional_turns_here=(), foreshadowing_here=(), prop_entrances_here=(), prop_locks_here=(),
    )
    assert any(_TAG in a and "温念的出租屋" in a for a in advisories)


def test_segment_content_advisories_silent_when_matches_card_field_unset():
    """模型这次 JSON 回包压根没带这个 key——pydantic 字面量默认值 "unsure"
    在没被 model_fields_set 认领时不能当证据用，否则任何一次模型漏填都会被
    误判成"场景状态变了"（红灯测试实测：不做这层判断会让
    test_segment_content_advisories_empty_for_well_formed_draft 变红）。"""
    draft = _draft(resources=_AiSegmentResources(
        scenes=[_AiResourceScene(scene_id="scene:温念的出租屋")],
    ))
    advisories = _segment_content_advisories(
        draft, source_segment_indexes=[1], manifest=None,
        emotional_turns_here=(), foreshadowing_here=(), prop_entrances_here=(), prop_locks_here=(),
    )
    assert not any(_TAG in a for a in advisories)


def test_segment_content_advisories_silent_when_matches_card_explicit_yes():
    draft = _draft(resources=_AiSegmentResources(
        scenes=[_AiResourceScene(scene_id="scene:温念的出租屋", scene_state_matches_card="yes")],
    ))
    advisories = _segment_content_advisories(
        draft, source_segment_indexes=[1], manifest=None,
        emotional_turns_here=(), foreshadowing_here=(), prop_entrances_here=(), prop_locks_here=(),
    )
    assert not any(_TAG in a for a in advisories)
