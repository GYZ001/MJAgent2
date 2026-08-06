import asyncio
import base64
import json
from pathlib import Path

import pytest

from app import hiagent, video_modes, worker
from app.schemas import Bible, Character, Shot, World
from app.video_modes import (
    REFERENCE_IMAGE_MODE,
    ReferenceImageAsset,
    ReferenceImagePlan,
    ShotVideoModeDecision,
    ShotVideoModeSelector,
    build_seedance_image_inputs,
    decision_to_dict,
    dict_to_decision,
)


def _fake_settings(monkeypatch, **overrides):
    """让 video_modes.get_setting 读自一个内存字典，避免依赖真实 DB 设置。"""
    monkeypatch.setattr(video_modes, "get_setting", lambda k, *a, **kw: overrides.get(k))


def _bible() -> Bible:
    return Bible(
        characters=[Character(name="A", role="lead", appearance_canonical="black hair, blue robe")],
        world=World(visual_style_canonical="anime drama style"),
    )


def _shot(**kwargs) -> Shot:
    data = {
        "shot_no": 1,
        "duration_s": 5,
        "shot_size": "中景",
        "camera_move": "固定",
        "scene_setting": "室内",
        "characters": ["A"],
        "action_desc": "A坐在桌前轻声说话。",
        "first_frame_desc": "A坐在桌前。",
        "last_frame_desc": "A看向窗外。",
        "source_excerpt": "A坐在桌前轻声说话。",
        "dialogues": [],
        "transition": "硬切",
        "continuity_from_prev": False,
    }
    data.update(kwargs)
    return Shot(**data)


def _patch_multiview_production_ready(monkeypatch) -> None:
    """测试中绕过真实 DB 多视角硬门禁，只验证参考图生成/QA/装箱路径。"""
    import app.multiview as mv

    async def _ready(*_a, **_k):
        return {"status": "ready"}

    monkeypatch.setattr(mv, "complete_legacy_character_pack", _ready)
    monkeypatch.setattr(mv, "complete_legacy_scene_pack", _ready)
    monkeypatch.setattr(mv, "resolve_shot_asset_dependencies", lambda **_k: {
        "episode_no": 1, "shot_id": "s", "characters": [], "scene": None,
        "keyframe_slot": "narrative_keyframe", "input_fingerprint": "fp",
    })
    monkeypatch.setattr(mv, "assert_manifest_allows_production", lambda _m: None)
    monkeypatch.setattr(mv, "manifest_revisions_match", lambda _a, _b: True)
    monkeypatch.setattr(mv, "library_anchor_assets_from_manifest", lambda _m: [])
    monkeypatch.setattr(mv, "keyframe_seed_paths", lambda _m: [])
    monkeypatch.setattr(mv, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(mv, "scene_multiview_enabled", lambda: True)


def _patch_reference_build_unit(monkeypatch) -> None:
    """Keep reference-slot recovery tests local and free of DB/provider dependencies."""
    _patch_multiview_production_ready(monkeypatch)
    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "_portrait_seed_inputs", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 1)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 0)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "batch_prompt_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)
    # These tests exercise the legacy/master slot's best-of-three lifecycle in
    # isolation.  The production default now expands a shot to the free slots
    # in Seedance's nine-image budget, which is a separate contract.
    monkeypatch.setattr(video_modes, "max_reference_images", lambda: 1)


def _passing_reference_qa() -> dict:
    return {
        "status": "scored", "overall": 0.95, "absolute_quality": 0.95,
        "action_match": 0.95, "body_proportion": 0.95,
        "face_identity": 0.95, "outfit_match": 0.95, "hair_match": 0.95,
        "scene_match": 0.95, "hard_failures": [], "issues": [],
    }


def test_selector_always_uses_reference_mode(monkeypatch) -> None:
    """The removed mode switch cannot reactivate an obsolete video path."""
    _fake_settings(monkeypatch)

    async def fail_chat(*a, **k):
        raise AssertionError("强制模式不应调用 LLM 选择")

    monkeypatch.setattr(hiagent, "chat", fail_chat)
    decision = asyncio.run(ShotVideoModeSelector().select(_shot(), _bible()))

    assert decision.mode == REFERENCE_IMAGE_MODE
    assert decision.defaulted is True and decision.llmUsed is False
    assert decision.referenceImagePlan.totalCount > 0


def test_selector_does_not_call_llm_for_strong_action(monkeypatch) -> None:
    """强运动镜头也固定参考图模式，不再调用 LLM 选择首尾帧。"""
    async def fail_chat(*args, **kwargs):
        raise AssertionError("固定参考图模式不应调用 LLM 选择")

    monkeypatch.setattr(hiagent, "chat", fail_chat)
    _fake_settings(monkeypatch)  # AUTO + 默认启用
    shot = _shot(action_desc="A快速转身释放法术，与敌人打斗，必须保证结尾落点。")
    decision = asyncio.run(ShotVideoModeSelector().select(shot, _bible()))

    assert decision.mode == REFERENCE_IMAGE_MODE
    assert decision.llmUsed is False
    assert decision.defaulted is True


@pytest.mark.parametrize(
    "meta",
    [
        {
            "mode": REFERENCE_IMAGE_MODE,
            "first_frame_path": "/tmp/first.jpg",
            "reference_images": [{"url": "data:image/jpeg;base64,abc", "selectedForSeedance": True}],
        },
        {
            "mode": "FIRST_LAST_FRAME_MODE",
            "reference_images": [{"url": "data:image/jpeg;base64,abc", "selectedForSeedance": True}],
        },
    ],
)
def test_seedance_inputs_are_mutually_exclusive(meta: dict) -> None:
    with pytest.raises(Exception):
        build_seedance_image_inputs(meta)


def test_selector_returns_default_reference_plan_without_llm(monkeypatch) -> None:
    """模式选择已剔除：select() 返回固定参考图计划，不解析 LLM 的逐图计划。"""
    async def fail_chat(*args, **kwargs):
        raise AssertionError("固定参考图模式不应调用 LLM 选择")

    monkeypatch.setattr(hiagent, "chat", fail_chat)
    monkeypatch.setattr(video_modes, "get_setting", lambda *a, **k: None)

    shot = _shot(action_desc="A站在室内与同伴对话。", dialogues=[{"speaker": "A", "line": "你好", "emotion": "平静"}])
    decision = asyncio.run(ShotVideoModeSelector().select(shot, _bible()))

    assert decision.mode == REFERENCE_IMAGE_MODE
    assert decision.llmUsed is False
    plan = decision.referenceImagePlan
    assert plan.totalCount == 2 and plan.generateNewCount == 2
    assert plan.types == ["plot_key_frame"] * 2
    assert plan.prompts == []
    # 决策可往返序列化（入队持久化 → 生成期复用）
    assert dict_to_decision(decision_to_dict(decision)).referenceImagePlan.prompts == plan.prompts


def test_reference_mode_builds_reference_image_roles() -> None:
    inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [
            {"url": "data:image/jpeg;base64,abc", "selectedForSeedance": True, "type": "character"},
            {"url": "data:image/jpeg;base64,def", "selectedForSeedance": False, "type": "scene"},
        ],
    })

    assert inputs == [("data:image/jpeg;base64,abc", "reference_image")]


def test_reference_mode_excludes_deleted_reference_images() -> None:
    """用户在素材画廊里废弃（deleted）的参考图即便仍标 selectedForSeedance，也不喂给模型。"""
    inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [
            {"url": "data:image/jpeg;base64,keep", "selectedForSeedance": True, "type": "character"},
            {"url": "data:image/jpeg;base64,gone", "selectedForSeedance": True, "deleted": True, "type": "scene"},
        ],
    })

    assert inputs == [("data:image/jpeg;base64,keep", "reference_image")]


def test_reference_mode_excludes_rejected_candidate_with_stale_video_purpose() -> None:
    """QA 淘汰图会保留用途元数据，但 selected=false 必须阻止它进入供应商请求。"""
    inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [
            {
                "url": "data:image/jpeg;base64,keep",
                "selectedForSeedance": True,
                "type": "plot_key_frame",
                "purposes": ["video_input", "qa_anchor"],
            },
            {
                "url": "data:image/jpeg;base64,rejected",
                "selectedForSeedance": False,
                "type": "plot_key_frame",
                "purposes": ["video_input", "qa_anchor"],
                "rejectReason": "quality_below_threshold",
            },
        ],
    })

    assert inputs == [("data:image/jpeg;base64,keep", "reference_image")]


def test_reference_mode_defensively_packs_only_highest_selected_keyframe() -> None:
    """即使被旧数据污染成三张 selected，视频请求也只能收到最高分关键帧。"""
    inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [
            {
                "url": "data:image/jpeg;base64,low",
                "type": "plot_key_frame",
                "selectedForSeedance": True,
                "qualityScore": 0.41,
            },
            {
                "url": "data:image/jpeg;base64,best",
                "type": "plot_key_frame",
                "selectedForSeedance": True,
                "qualityScore": 0.93,
            },
            {
                "url": "data:image/jpeg;base64,mid",
                "type": "plot_key_frame",
                "selectedForSeedance": True,
                "qualityScore": 0.67,
            },
        ],
    })

    assert inputs == [("data:image/jpeg;base64,best", "reference_image")]


def test_short_shot_provider_boundary_never_passes_a_second_keyframe() -> None:
    inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "keyframe_sequence": {
            "keyframe_plan": {"duration_s": 7, "count": 1},
            "beats": [{"slot_key": "narrative_keyframe"}],
        },
        "reference_images": [
            {
                "url": "data:image/jpeg;base64,auxiliary",
                "type": "plot_key_frame",
                "slot_key": "narrative_keyframe_01",
                "keyframe_index": 1,
                "keyframe_time_ratio": 0.0,
                "selectedForSeedance": True,
                "qualityScore": 0.99,
            },
            {
                "url": "data:image/jpeg;base64,master",
                "type": "plot_key_frame",
                "slot_key": "narrative_keyframe",
                "keyframe_index": 2,
                "keyframe_time_ratio": 0.64,
                "selectedForSeedance": True,
                "qualityScore": 0.8,
            },
        ],
    })

    assert inputs == [("data:image/jpeg;base64,master", "reference_image")]


def test_narrative_keyframe_beats_are_chronological_and_have_distinct_targets(monkeypatch) -> None:
    monkeypatch.setattr(video_modes, "max_reference_images", lambda: 9)
    shot = _shot(
        duration_s=10,
        state_in="A站在门口，手里还没有信件。",
        first_frame_desc="A在门口停下，双手空着。",
        primary_action="A走到桌前，拿起封好的信件并拆开。",
        action_desc="A从门口走向桌边，拿起信件拆开，读到内容后神色一凛。",
        emotion_beat="A看到信中的名字，震惊转为坚定。",
        state_out="A已收起信件，决定立刻离开。",
        last_frame_desc="A将信件收进衣内，转身面向门外。",
    )

    beats = video_modes.narrative_keyframe_beats(shot, 2)

    assert [beat["beat_index"] for beat in beats] == [1, 2]
    assert [beat["time_ratio"] for beat in beats] == sorted(beat["time_ratio"] for beat in beats)
    assert beats[0]["time_ratio"] == 0.0
    assert beats[-1]["time_ratio"] == 1.0
    assert len({beat["slot_key"] for beat in beats}) == 2
    assert sum(beat["slot_key"] == "narrative_keyframe" for beat in beats) == 1
    assert len({beat["target_desc"] for beat in beats}) == 2
    assert all(beat["target_desc"] in beat["prompt_intent"] for beat in beats)


def test_timeline_keyframe_plan_enforces_duration_cap_and_complexity_judgment() -> None:
    short_complex = _shot(
        duration_s=7,
        state_in="A空手站在门口。",
        primary_action="A拿起信件并拆开。",
        emotion_beat="A读完后震惊。",
        state_out="A收起信件转身离开。",
    )
    long_static = _shot(duration_s=10)
    long_complex = short_complex.model_copy(update={"duration_s": 10})

    assert video_modes.timeline_keyframe_plan(short_complex)["count"] == 1
    assert video_modes.timeline_keyframe_plan(long_static)["count"] == 1
    assert video_modes.timeline_keyframe_plan(long_complex)["count"] == 2


def test_reference_mode_keeps_one_winner_per_timeline_slot() -> None:
    refs = [
        {
            "id": "beat-1-low",
            "url": "data:image/jpeg;base64,beat1low",
            "type": "plot_key_frame",
            "slot_key": "narrative_keyframe_01",
            "keyframe_index": 1,
            "keyframe_time_ratio": 0.0,
            "candidate_no": 1,
            "selectedForSeedance": True,
            "qualityScore": 0.41,
        },
        {
            "id": "beat-3",
            "url": "data:image/jpeg;base64,beat3",
            "type": "plot_key_frame",
            "slot_key": "narrative_keyframe_03",
            "keyframe_index": 3,
            "keyframe_time_ratio": 1.0,
            "candidate_no": 1,
            "selectedForSeedance": True,
            "qualityScore": 0.99,
        },
        {
            "id": "beat-1-best",
            "url": "data:image/jpeg;base64,beat1best",
            "type": "plot_key_frame",
            "slot_key": "narrative_keyframe_01",
            "keyframe_index": 1,
            "keyframe_time_ratio": 0.0,
            "candidate_no": 2,
            "selectedForSeedance": True,
            "qualityScore": 0.92,
        },
        {
            "id": "beat-2-master",
            "url": "data:image/jpeg;base64,beat2",
            "type": "plot_key_frame",
            "slot_key": "narrative_keyframe",
            "keyframe_index": 2,
            "keyframe_time_ratio": 0.64,
            "candidate_no": 1,
            "selectedForSeedance": True,
            "qualityScore": 0.55,
        },
    ]

    packed = video_modes.pack_reference_images_for_seedance(refs, max_images=9)

    assert [ref["id"] for ref in packed] == [
        "beat-1-best",
        "beat-2-master",
    ]


def test_reference_pack_prioritizes_timeline_and_props_before_scene_and_character(monkeypatch) -> None:
    monkeypatch.setattr(video_modes, "max_character_reference_images", lambda: 1)
    timeline = [
        {
            "id": f"beat-{index}",
            "url": f"data:image/jpeg;base64,beat{index}",
            "type": "plot_key_frame",
            "slot_key": "narrative_keyframe" if index == 5 else f"narrative_keyframe_{index:02d}",
            "keyframe_index": index,
            "keyframe_total": 7,
            "keyframe_time_ratio": (index - 1) / 6,
            "selectedForSeedance": True,
            # Deliberately reverse score order: time, not QA score, controls the
            # order of winners from different narrative slots.
            "qualityScore": 1.0 - index / 20,
            "purposes": ["video_input", "qa_anchor"],
        }
        for index in range(1, 8)
    ]
    refs = [
        timeline[5],
        {
            "id": "scene-main",
            "url": "data:image/jpeg;base64,scene",
            "type": "scene",
            "source": "asset_library",
            "selectedForSeedance": True,
            "qualityScore": 0.4,
            "purposes": ["video_input", "qa_anchor"],
        },
        timeline[1],
        {
            "id": "character-a",
            "url": "data:image/jpeg;base64,character",
            "type": "character",
            "source": "asset_library",
            "selectedForSeedance": True,
            "qualityScore": 0.3,
            "purposes": ["video_input", "qa_anchor"],
        },
        timeline[6],
        timeline[3],
        {
            "id": "prop-overflow",
            "url": "data:image/jpeg;base64,prop",
            "type": "prop",
            "selectedForSeedance": True,
            "qualityScore": 1.0,
            "purposes": ["video_input"],
        },
        timeline[0],
        timeline[4],
        timeline[2],
    ]

    packed = video_modes.pack_reference_images_for_seedance(refs, max_images=9)

    assert len([ref for ref in packed if ref["type"] == "plot_key_frame"]) == 2
    assert [ref["id"] for ref in packed] == [
        "beat-1", "beat-5", "prop-overflow", "scene-main", "character-a",
    ]
    assert {ref["id"] for ref in packed if ref["type"] == "plot_key_frame"} == {
        "beat-1", "beat-5",
    }


def test_default_reference_build_respects_one_or_two_keyframe_policy(monkeypatch, tmp_path) -> None:
    """短镜头严格一帧；长且有两个剧情阶段时也最多两帧。"""
    import app.multiview as mv

    _patch_multiview_production_ready(monkeypatch)
    monkeypatch.setattr(video_modes, "max_reference_images", lambda: 9)
    monkeypatch.setattr(video_modes, "max_character_reference_images", lambda: 1)
    monkeypatch.setattr(video_modes, "keyframe_candidate_count", lambda: 3)
    monkeypatch.setattr(video_modes, "supporting_keyframe_candidate_count", lambda: 3)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "batch_prompt_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *_a, **_k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *_a, **_k: [])
    monkeypatch.setattr(video_modes, "_portrait_seed_inputs", lambda *_a, **_k: [])

    character_path = tmp_path / "character.jpg"
    scene_path = tmp_path / "scene.jpg"
    character_path.write_bytes(b"character")
    scene_path.write_bytes(b"scene")
    anchors = [
        {
            "entity_type": "character", "entity_name": "A", "type": "character",
            "image_path": str(character_path), "source": "asset_library", "view_role": "front_full",
            "library_revision_id": "look-1", "library_view_id": "char-front",
            "purposes": ["keyframe_seed", "qa_anchor"],
        },
        {
            "entity_type": "scene", "entity_name": "室内", "type": "scene",
            "image_path": str(scene_path), "source": "asset_library", "view_role": "establishing",
            "library_revision_id": "scene-1", "library_view_id": "scene-est",
            "purposes": ["keyframe_seed", "qa_anchor"],
        },
    ]
    monkeypatch.setattr(mv, "library_anchor_assets_from_manifest", lambda _manifest: anchors)

    generation_calls: list[tuple[int, str]] = []

    async def generate(*, ref_type, index, shot, **_kwargs):
        generation_calls.append((index, shot.action_desc))
        path = tmp_path / f"generated-{index}.jpg"
        path.write_bytes(f"generated-{index}".encode())
        return ReferenceImageAsset(
            id=f"generated-{index}", url="u", path=str(path), type=ref_type,
            source="seedream_generated",
        )

    async def review(_payload, **_kwargs):
        return _passing_reference_qa()

    async def review_consistency(*, candidates, **_kwargs):
        return {
            "candidates": [
                {"asset_id": asset.id, "consistency": 1.0, "drift": [], "issues": []}
                for asset in candidates
            ],
            "overall": 1.0,
            "failed": False,
        }

    monkeypatch.setattr(video_modes, "_generate_one_reference", generate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", review)
    monkeypatch.setattr(video_modes, "review_reference_consistency", review_consistency)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda _qa: True)
    meta: dict = {}

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(
            state_in="A低头坐在桌前。", primary_action="A拿起桌上的信封并拆开。",
            emotion_beat="A读完后露出震惊。", state_out="A收起信封起身。",
        ),
        bible=_bible(), decision=video_modes.default_reference_decision(), existing_meta=meta,
    ))

    assert len(generation_calls) == 3
    assert len({target for _index, target in generation_calls}) == 1
    assert meta["keyframe_sequence"]["beat_count"] == 1
    expected_slots = {beat["slot_key"] for beat in meta["keyframe_sequence"]["beats"]}
    assert expected_slots == set(meta["reference_slots"])
    assert meta["reference_slots"]["narrative_keyframe"]["candidate_target"] == 3
    assert all(slot["candidate_target"] == 3 for slot in meta["reference_slots"].values())
    assert all(
        [candidate["status"] for candidate in slot["candidates"]].count("selected") == 1
        and [candidate["status"] for candidate in slot["candidates"]].count("discarded_deleted") == 2
        for slot in meta["reference_slots"].values()
    )
    assert len(list(tmp_path.glob("generated-*.jpg"))) == 1

    packed = video_modes.pack_reference_images_for_seedance([asset.public_dict() for asset in assets])
    assert len(packed) == 3
    assert [ref["type"] for ref in packed] == ["plot_key_frame", "scene", "character"]
    assert packed[0]["keyframe_index"] == 1

    generation_calls.clear()
    long_meta: dict = {}
    long_assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="long",
        shot=_shot(
            duration_s=10,
            state_in="A低头坐在桌前，双手空着。",
            primary_action="A拿起桌上的信封并拆开。",
            emotion_beat="A读完后露出震惊。",
            state_out="A收起信封，随后起身离开。",
        ),
        bible=_bible(), decision=video_modes.default_reference_decision(), existing_meta=long_meta,
    ))

    assert len(generation_calls) == 6
    assert long_meta["keyframe_sequence"]["beat_count"] == 2
    assert len(long_meta["reference_slots"]) == 2
    assert all(slot["candidate_target"] == 3 for slot in long_meta["reference_slots"].values())
    long_packed = video_modes.pack_reference_images_for_seedance([
        asset.public_dict() for asset in long_assets
    ])
    assert len(long_packed) == 4
    assert [ref["type"] for ref in long_packed] == [
        "plot_key_frame", "plot_key_frame", "scene", "character",
    ]
    assert [ref["keyframe_index"] for ref in long_packed[:2]] == [1, 2]

    async def review_consistency_with_drift(*, candidates, **_kwargs):
        return {
            "candidates": [
                {
                    "asset_id": asset.id,
                    "consistency": 1.0 if asset.slot_key == "narrative_keyframe" else 0.8,
                    "drift": [] if asset.slot_key == "narrative_keyframe" else ["costume"],
                    "issues": [],
                }
                for asset in candidates
            ],
            "overall": 0.8,
            "failed": False,
        }

    monkeypatch.setattr(video_modes, "review_reference_consistency", review_consistency_with_drift)
    fallback_meta: dict = {}
    fallback_assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="long-drift",
        shot=_shot(
            duration_s=10,
            state_in="A低头坐在桌前，双手空着。",
            primary_action="A拿起桌上的信封并拆开。",
            emotion_beat="A读完后露出震惊。",
            state_out="A收起信封，随后起身离开。",
        ),
        bible=_bible(), decision=video_modes.default_reference_decision(), existing_meta=fallback_meta,
    ))
    fallback_packed = video_modes.pack_reference_images_for_seedance([
        asset.public_dict() for asset in fallback_assets
    ])
    assert fallback_meta["keyframe_sequence"]["beat_count"] == 1
    assert fallback_meta["keyframe_sequence"]["keyframe_plan"]["reason"] == (
        "cross_frame_identity_invariance_fallback"
    )
    assert len([ref for ref in fallback_packed if ref["type"] == "plot_key_frame"]) == 1
    assert video_modes.reference_gallery_matches_keyframe_contract({
        **fallback_meta,
        "reference_images": [asset.public_dict() for asset in fallback_assets],
    })


def test_cross_keyframe_identity_drift_keeps_paid_auxiliary_as_warning(monkeypatch, tmp_path) -> None:
    master_path = tmp_path / "master.jpg"
    auxiliary_path = tmp_path / "auxiliary.jpg"
    master_path.write_bytes(b"master")
    auxiliary_path.write_bytes(b"auxiliary")
    master = ReferenceImageAsset(
        id="master", url="u", path=str(master_path), type="plot_key_frame",
        source="seedream_generated", slot_key="narrative_keyframe",
        keyframe_index=2, keyframe_time_ratio=1.0, selectedForSeedance=True,
        purposes=["video_input", "qa_anchor"], qualityScore=0.9,
    )
    auxiliary = ReferenceImageAsset(
        id="auxiliary", url="u", path=str(auxiliary_path), type="plot_key_frame",
        source="seedream_generated", slot_key="narrative_keyframe_01",
        keyframe_index=1, keyframe_time_ratio=0.0, selectedForSeedance=True,
        purposes=["video_input", "qa_anchor"], qualityScore=0.95,
    )

    async def drift_report(**_kwargs):
        return {
            "candidates": [
                {"asset_id": "auxiliary", "consistency": 0.9, "drift": ["height_ratio"], "issues": []},
                {"asset_id": "master", "consistency": 1.0, "drift": [], "issues": []},
            ],
            "overall": 0.9,
            "failed": False,
        }

    monkeypatch.setattr(video_modes, "review_reference_consistency", drift_report)
    rejected = []
    kept, dropped = asyncio.run(video_modes._enforce_timeline_keyframe_invariance(
        selected=[auxiliary, master], shot=_shot(duration_s=10), bible=_bible(),
        rejected_out=rejected,
    ))

    assert [asset.id for asset in kept] == ["master"]
    assert dropped == {"narrative_keyframe_01"}
    assert master_path.is_file()
    assert auxiliary_path.is_file()
    assert rejected == [auxiliary]
    assert auxiliary.deleted is False
    assert auxiliary.selectedForSeedance is False


def test_reference_prompt_numbering_uses_exact_packed_order(monkeypatch) -> None:
    monkeypatch.setattr(video_modes, "max_reference_images", lambda: 9)
    monkeypatch.setattr(video_modes, "max_character_reference_images", lambda: 1)
    assets = [
        ReferenceImageAsset(
            id="late", url="data:image/jpeg;base64,bGF0ZQ==", type="plot_key_frame",
            source="seedream_generated", selectedForSeedance=True, slot_key="narrative_keyframe",
            relatedCharacterIds=["A"],
            keyframe_index=2, keyframe_total=2, keyframe_time_ratio=1.0,
            keyframe_target_desc="closing target",
        ),
        ReferenceImageAsset(
            id="scene", url="data:image/jpeg;base64,c2NlbmU=", type="scene",
            source="asset_library", selectedForSeedance=True,
        ),
        ReferenceImageAsset(
            id="early", url="data:image/jpeg;base64,ZWFybHk=", type="plot_key_frame",
            source="seedream_generated", selectedForSeedance=True, slot_key="narrative_keyframe_01",
            relatedCharacterIds=["A"],
            keyframe_index=1, keyframe_total=2, keyframe_time_ratio=0.0,
            keyframe_target_desc="opening target",
        ),
        ReferenceImageAsset(
            id="character", url="data:image/jpeg;base64,Y2hhcg==", type="character",
            source="asset_library", selectedForSeedance=True,
            entity_name="A", relatedCharacterIds=["A"],
        ),
    ]

    note = video_modes.append_reference_prompt_notes("PROMPT", assets)
    packed = video_modes.pack_reference_images_for_seedance([asset.public_dict() for asset in assets])

    assert [ref["id"] for ref in packed] == ["early", "late", "scene", "character"]
    assert "Reference image 1: use as plot key frame" in note and "freeze only: opening target" in note
    assert "Reference image 2: use as plot key frame" in note and "freeze only: closing target" in note
    assert "Reference image 3: use as scene" in note
    assert "Reference image 4: use as character" in note
    assert "chronological waypoints of ONE continuous shot" in note
    assert "[SUBJECT DEFINITIONS | HIGHEST PRIORITY]" in note
    assert "定义为「A」" in note
    assert "是同一个且仅一个人物" in note
    assert "禁止中途变性、换脸、换人" in note


def test_continuity_assembly_does_not_restore_discarded_candidates(monkeypatch, tmp_path) -> None:
    """等待连续性尾帧后重新装配时，人工废弃和 QA 淘汰图都不得被重新选中。"""
    keep_path = tmp_path / "keep.jpg"
    rejected_path = tmp_path / "rejected.jpg"
    deleted_path = tmp_path / "deleted.jpg"
    keep_path.write_bytes(b"keep")
    rejected_path.write_bytes(b"rejected")
    deleted_path.write_bytes(b"deleted")
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "quality_threshold", lambda: 0.8)

    rejected_out: list[ReferenceImageAsset] = []
    assets = asyncio.run(video_modes.assemble_continuity_tail(
        conn=None,
        project_id="p1",
        episode_no=1,
        episode_id="e1",
        shot_id="s1",
        shot=_shot(shot_no=1),
        bible=_bible(),
        meta={
            "reference_images": [
                {
                    "id": "keep",
                    "path": str(keep_path),
                    "type": "plot_key_frame",
                    "source": "seedream_generated",
                    "qualityScore": 0.95,
                    "qa": {"overall": 0.95, "absolute_quality": 0.95},
                    "selectedForSeedance": True,
                    "purposes": ["video_input", "qa_anchor"],
                },
                {
                    "id": "rejected",
                    "path": str(rejected_path),
                    "type": "plot_key_frame",
                    "source": "seedream_generated",
                    "qualityScore": 0.7,
                    "qa": {"overall": 0.7, "absolute_quality": 0.7},
                    "selectedForSeedance": False,
                    "purposes": ["video_input", "qa_anchor"],
                    "rejectReason": "quality_below_threshold",
                },
                {
                    "id": "deleted",
                    "path": str(deleted_path),
                    "type": "plot_key_frame",
                    "source": "seedream_generated",
                    "qualityScore": 0.99,
                    "qa": {"overall": 0.99, "absolute_quality": 0.99},
                    "selectedForSeedance": False,
                    "deleted": True,
                    "purposes": ["video_input", "qa_anchor"],
                },
            ],
        },
        prev_shot=None,
        rejected_out=rejected_out,
    ))

    assert [asset.id for asset in assets if asset.selectedForSeedance] == ["keep"]
    assert {asset.id for asset in rejected_out} == {"rejected", "deleted"}
    assert all(not asset.selectedForSeedance for asset in rejected_out)


def test_continuity_assembly_preserves_data_url_keyframe_when_tail_is_added(monkeypatch) -> None:
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "quality_threshold", lambda: 0.8)
    tail = ReferenceImageAsset(
        id="tail", url="data:image/jpeg;base64,tail", type="previous_shot_frame",
        source="previous_shot", selectedForSeedance=True, purposes=["video_input", "qa_anchor"],
    )
    monkeypatch.setattr(video_modes, "previous_tail_reference_asset", lambda *a, **k: tail)
    fingerprint = video_modes.keyframe_contract_fingerprint(_shot(shot_no=2), _bible())

    assets = asyncio.run(video_modes.assemble_continuity_tail(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(shot_no=2), bible=_bible(), meta={
            "reference_images": [{
                "id": "keyframe", "url": "data:image/jpeg;base64,keyframe",
                "type": "plot_key_frame", "source": "seedream_generated",
                "selectedForSeedance": True, "purposes": ["video_input", "qa_anchor"],
                "keyframe_contract_fingerprint": fingerprint,
            }],
        },
        prev_shot={"id": "prev"},
    ))

    assert {asset.id for asset in assets} == {"keyframe", "tail"}
    assert next(asset for asset in assets if asset.id == "keyframe").url.startswith("data:image/")


def test_build_reference_assets_collects_score_only_warning_without_discard(monkeypatch, tmp_path) -> None:
    """QA 低分只记录风险；技术有效关键帧仍作为视频输入。"""
    bible = _bible()
    shot = _shot(shot_no=4, narration="对白场景", dialogues=[{"speaker": "A", "line": "嗯", "emotion": "平静"}])

    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 1)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 2)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "quality_threshold", lambda: 0.8)
    monkeypatch.setattr(video_modes, "batch_prompt_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)

    import app.multiview as mv
    _patch_multiview_production_ready(monkeypatch)

    async def fake_keep_best(**kwargs):
        score = 0.55
        path = tmp_path / "g.jpg"
        path.write_bytes(b"img")
        asset = ReferenceImageAsset(
            id="g1", url="u", type="plot_key_frame", source="seedream_generated",
            path=str(path), qualityScore=score,
            qa={"overall": score, "absolute_quality": score, "issues": []},
        )
        return asset, [], []

    async def fake_review(b64, **kwargs):
        return {
            "status": "scored", "overall": 0.55, "action_match": 0.55, "body_proportion": 0.55,
            "face_identity": 0.55, "outfit_match": 0.55, "hair_match": 0.55, "scene_match": 0.55,
            "hard_failures": [], "issues": ["低分"], "absolute_quality": 0.55,
        }

    monkeypatch.setattr(video_modes, "_generate_reference_keep_best", fake_keep_best)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", fake_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda qa: False)

    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="对白", confidence=0.9,
        referenceImagePlan=ReferenceImagePlan(totalCount=1, reusePreviousSceneCount=0,
                                              generateNewCount=1, types=["plot_key_frame"], prompts=[]))
    rejected: list = []
    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=shot, bible=bible, decision=decision, prev_shot=None, rejected_out=rejected))

    fed = [a for a in assets if a.selectedForSeedance]
    assert [a.id for a in fed] == ["g1"]
    assert fed[0].rejectReason == "quality_below_threshold_score_only"
    assert rejected == []


def test_qa_pending_slot_resumes_existing_file_without_paid_regeneration(monkeypatch, tmp_path) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    image_path = tmp_path / "pending.jpg"
    image_path.write_bytes(b"pending-image")
    calls = {"generate": 0, "qa": 0}

    async def fail_generate(**_kwargs):
        calls["generate"] += 1
        raise AssertionError("qa_pending recovery must not regenerate an existing image")

    async def pass_review(_b64, **_kwargs):
        calls["qa"] += 1
        return _passing_reference_qa()

    monkeypatch.setattr(video_modes, "_generate_reference_keep_best", fail_generate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", pass_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda _qa: True)
    contract_fingerprint = video_modes.keyframe_contract_fingerprint(_shot(), _bible())
    existing_meta = {
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "keyframe_contract_fingerprint": contract_fingerprint,
        "reference_slots": {
            "narrative_keyframe": {
                "status": "qa_pending",
                "type": "plot_key_frame",
                "path": str(image_path),
                "prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
                "keyframe_contract_fingerprint": contract_fingerprint,
            },
        },
    }

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(), bible=_bible(), decision=video_modes.default_reference_decision(),
        existing_meta=existing_meta,
    ))

    assert calls == {"generate": 0, "qa": 1}
    assert existing_meta["reference_slots"]["narrative_keyframe"]["status"] == "passed"
    assert any(a.path == str(image_path) and a.selectedForSeedance for a in assets)


def test_keyframe_best_of_three_selects_highest_and_deletes_losers(monkeypatch, tmp_path) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    scores = [0.62, 0.94, 0.71]
    generated_paths: list[Path] = []
    score_by_payload: dict[str, float] = {}
    qa_calls: list[str] = []

    async def generate_candidate(*, ref_type, **_kwargs):
        candidate_no = len(generated_paths) + 1
        path = tmp_path / f"candidate-{candidate_no}.jpg"
        path.write_bytes(f"candidate-{candidate_no}".encode())
        generated_paths.append(path)
        score_by_payload[hiagent.encode_image_file(str(path))] = scores[candidate_no - 1]
        return ReferenceImageAsset(
            id=f"candidate-{candidate_no}",
            url=hiagent.data_url_from_file(str(path)),
            path=str(path),
            type=ref_type,
            source="seedream_generated",
        )

    async def review_candidate(payload, **_kwargs):
        qa_calls.append(payload)
        score = score_by_payload[payload]
        return {
            **_passing_reference_qa(),
            "overall": score,
            "absolute_quality": score,
        }

    monkeypatch.setattr(video_modes, "_generate_one_reference", generate_candidate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", review_candidate)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda _qa: True)
    existing_meta: dict = {}
    rejected: list[ReferenceImageAsset] = []
    progress_snapshots: list[tuple[list[str], list[str]]] = []

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(), bible=_bible(), decision=video_modes.default_reference_decision(),
        existing_meta=existing_meta, rejected_out=rejected,
        on_progress=lambda current, removed: progress_snapshots.append((
            [asset.path or "" for asset in current if asset.source == "seedream_generated"],
            [asset.path or "" for asset in removed if asset.source == "seedream_generated"],
        )),
    ))

    assert len(generated_paths) == 3
    assert len(qa_calls) == 3
    generated = [asset for asset in assets if asset.source == "seedream_generated"]
    assert len(generated) == 1
    winner = generated[0]
    assert winner.path == str(generated_paths[1])
    assert winner.selectedForSeedance is True
    assert generated_paths[1].is_file()
    assert not generated_paths[0].exists()
    assert not generated_paths[2].exists()
    assert rejected == []

    slot = existing_meta["reference_slots"]["narrative_keyframe"]
    assert slot["candidate_target"] == 3
    assert slot["candidate_count"] == 3
    assert slot["winner_candidate_no"] == 2
    assert slot["path"] == str(generated_paths[1])
    candidate_by_no = {item["candidate_no"]: item for item in slot["candidates"]}
    assert set(candidate_by_no) == {1, 2, 3}
    for candidate_no in (1, 3):
        assert not candidate_by_no[candidate_no].get("path")
        assert not candidate_by_no[candidate_no].get("url")
    serialized_slot = json.dumps(slot, ensure_ascii=False)
    assert str(generated_paths[0]) not in serialized_slot
    assert str(generated_paths[2]) not in serialized_slot
    assert progress_snapshots[-1] == ([str(generated_paths[1])], [])

    video_inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [asset.public_dict() for asset in assets],
    })
    assert video_inputs == [(hiagent.data_url_from_file(str(generated_paths[1])), "reference_image")]


def test_all_three_structurally_invalid_keyframes_keep_best_after_retry_exhaustion(monkeypatch, tmp_path) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    generated_paths: list[Path] = []
    character_path = tmp_path / "character.jpg"
    scene_path = tmp_path / "scene.jpg"
    character_path.write_bytes(b"character-anchor")
    scene_path.write_bytes(b"scene-anchor")
    monkeypatch.setattr(video_modes, "max_reference_images", lambda: 3)
    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [
        ReferenceImageAsset(
            id="character-anchor", url="", path=str(character_path), type="character",
            source="asset_library", entity_type="character", entity_name="A",
        )
    ])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [
        ReferenceImageAsset(
            id="scene-anchor", url="", path=str(scene_path), type="scene",
            source="asset_library", entity_type="scene", entity_name="room",
        )
    ])

    async def generate_candidate(*, ref_type, **_kwargs):
        path = tmp_path / f"bad-{len(generated_paths) + 1}.jpg"
        path.write_bytes(b"bad-scale")
        generated_paths.append(path)
        return ReferenceImageAsset(
            id=f"bad-{len(generated_paths)}", url="u", path=str(path), type=ref_type,
            source="seedream_generated",
        )

    async def reject_geometry(*_args, **_kwargs):
        return {
            **_passing_reference_qa(),
            "overall": 0.9,
            "relative_height_match": 0.1,
            "hard_failures": ["relative_scale_mismatch"],
        }

    monkeypatch.setattr(video_modes, "_generate_one_reference", generate_candidate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", reject_geometry)
    meta: dict = {}

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(), bible=_bible(), decision=video_modes.default_reference_decision(),
        existing_meta=meta,
    ))

    assert len(generated_paths) == 3
    assert sum(path.exists() for path in generated_paths) == 1
    assert meta["reference_slots"]["narrative_keyframe"]["status"] == "scored_warning"
    assert meta["reference_slots"]["narrative_keyframe"]["gate_retry_exhausted"] is True
    assert meta["narrative_keyframe_missing"] is False
    assert "keyframe_fallback_mode" not in meta
    assert {asset.type for asset in assets if asset.selectedForSeedance} == {
        "character", "scene", "plot_key_frame",
    }
    assert any(asset.type == "plot_key_frame" for asset in assets)

    packed = build_seedance_image_inputs({
        **meta,
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [asset.public_dict() for asset in assets],
    })
    assert len(packed) == 3
    assert video_modes.reference_gallery_matches_keyframe_contract({
        **meta,
        "reference_images": [asset.public_dict() for asset in assets],
    }, expected_fingerprint=meta["keyframe_contract_fingerprint"])


def test_multi_character_height_contract_keeps_best_when_all_keyframes_fail(monkeypatch, tmp_path) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    generated_paths: list[Path] = []

    async def generate_candidate(*, ref_type, **_kwargs):
        path = tmp_path / f"bad-height-{len(generated_paths) + 1}.jpg"
        path.write_bytes(b"bad-height")
        generated_paths.append(path)
        return ReferenceImageAsset(
            id=f"bad-height-{len(generated_paths)}", url="u", path=str(path),
            type=ref_type, source="seedream_generated",
        )

    async def reject_height(*_args, **_kwargs):
        return {
            **_passing_reference_qa(),
            "overall": 0.9,
            "relative_height_match": 0.1,
            "hard_failures": ["relative_scale_mismatch"],
        }

    monkeypatch.setattr(video_modes, "_generate_one_reference", generate_candidate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", reject_height)
    bible = Bible(
        characters=[
            Character(name="A", role="lead", appearance_canonical="black robe"),
            Character(name="B", role="lead", appearance_canonical="purple dress"),
        ],
        world=World(visual_style_canonical="anime drama style"),
    )
    shot = _shot(
        characters=["A", "B"],
        action_desc="A与B站在同一地面平视交谈。",
        first_frame_desc="A与B同身高并肩站立。",
        last_frame_desc="A与B同身高相对站立。",
        source_excerpt="A与B站在同一地面平视交谈。",
    )
    meta: dict = {}

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=shot, bible=bible, decision=video_modes.default_reference_decision(),
        existing_meta=meta,
    ))

    assert len(generated_paths) == 3
    assert sum(path.exists() for path in generated_paths) == 1
    assert meta["reference_slots"]["narrative_keyframe"]["status"] == "scored_warning"
    assert meta["reference_slots"]["narrative_keyframe"]["gate_retry_exhausted"] is True
    assert meta["narrative_keyframe_missing"] is False
    assert "keyframe_fallback_mode" not in meta
    assert "keyframe_structural_fallback_slots" not in meta
    assert any(asset.type == "plot_key_frame" for asset in assets)
    assert video_modes.reference_gallery_matches_keyframe_contract({
        **meta,
        "reference_images": [asset.public_dict() for asset in assets],
    }, expected_fingerprint=meta["keyframe_contract_fingerprint"])


def test_keyframe_best_of_three_all_unverified_keeps_first_deterministically(monkeypatch, tmp_path) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    generated_paths: list[Path] = []

    async def generate_candidate(*, ref_type, **_kwargs):
        candidate_no = len(generated_paths) + 1
        path = tmp_path / f"unverified-{candidate_no}.jpg"
        path.write_bytes(f"unverified-{candidate_no}".encode())
        generated_paths.append(path)
        # 让先规划的 1 号候选最后完成，证明兜底按 candidate_no 而不是完成顺序决定。
        if candidate_no == 1:
            await asyncio.sleep(0.02)
        return ReferenceImageAsset(
            id=f"unverified-{candidate_no}",
            url=hiagent.data_url_from_file(str(path)),
            path=str(path),
            type=ref_type,
            source="seedream_generated",
        )

    async def unverified_review(_payload, **_kwargs):
        return {
            "status": "unverified",
            "overall": None,
            "absolute_quality": None,
            "hard_failures": [],
            "issues": ["qa unavailable"],
        }

    monkeypatch.setattr(video_modes, "_generate_one_reference", generate_candidate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", unverified_review)
    existing_meta: dict = {}
    rejected: list[ReferenceImageAsset] = []

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(), bible=_bible(), decision=video_modes.default_reference_decision(),
        existing_meta=existing_meta, rejected_out=rejected,
    ))

    assert len(generated_paths) == 3
    generated = [asset for asset in assets if asset.source == "seedream_generated"]
    assert len(generated) == 1
    winner = generated[0]
    assert winner.path == str(generated_paths[0])
    assert winner.selectedForSeedance is True
    assert winner.qa and winner.qa["status"] == "unverified"
    assert winner.qualityScore is None
    assert generated_paths[0].is_file()
    assert not generated_paths[1].exists()
    assert not generated_paths[2].exists()
    assert rejected == []

    slot = existing_meta["reference_slots"]["narrative_keyframe"]
    assert slot["winner_candidate_no"] == 1
    assert slot["candidate_count"] == 3
    assert slot["status"] == "unverified"
    serialized_slot = json.dumps(slot, ensure_ascii=False)
    assert str(generated_paths[1]) not in serialized_slot
    assert str(generated_paths[2]) not in serialized_slot
    video_inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [asset.public_dict() for asset in assets],
    })
    assert video_inputs == [(hiagent.data_url_from_file(str(generated_paths[0])), "reference_image")]


def test_all_identity_bad_keyframes_are_deleted_and_fall_back_to_truth_anchors(
    monkeypatch,
    tmp_path,
) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    monkeypatch.setattr(video_modes, "max_reference_images", lambda: 9)
    character_path = tmp_path / "character.jpg"
    scene_path = tmp_path / "scene.jpg"
    character_path.write_bytes(b"character")
    scene_path.write_bytes(b"scene")
    manifest = {
        "episode_no": 1,
        "shot_id": "s",
        "characters": [{
            "name": "A",
            "role_kind": "canonical",
            "asset_required": True,
            "look_revision_id": "portrait-a",
            "selected_views": [{
                "id": "view-a",
                "view_role": "front_full",
                "image_path": str(character_path),
                "purposes": ["keyframe_seed", "qa_anchor"],
            }],
        }],
        "scene": {
            "name": "room",
            "scene_revision_id": "scene-room",
            "selected_views": [{
                "id": "scene-view",
                "view_role": "establishing",
                "image_path": str(scene_path),
                "purposes": ["keyframe_seed", "qa_anchor"],
            }],
        },
        "keyframe_slot": "narrative_keyframe",
        "input_fingerprint": "identity-gate",
    }
    monkeypatch.setattr(mv, "resolve_shot_asset_dependencies", lambda **_k: manifest)
    monkeypatch.setattr(mv, "library_anchor_assets_from_manifest", lambda _m: [
        {
            "entity_type": "character",
            "entity_name": "A",
            "library_revision_id": "portrait-a",
            "library_view_id": "view-a",
            "view_role": "front_full",
            "image_path": str(character_path),
            "purposes": ["keyframe_seed", "qa_anchor"],
            "type": "character",
            "source": "asset_library",
        },
        {
            "entity_type": "scene",
            "entity_name": "room",
            "library_revision_id": "scene-room",
            "library_view_id": "scene-view",
            "view_role": "establishing",
            "image_path": str(scene_path),
            "purposes": ["keyframe_seed", "qa_anchor"],
            "type": "scene",
            "source": "asset_library",
        },
    ])
    monkeypatch.setattr(
        mv,
        "keyframe_seed_paths",
        lambda _m: [str(character_path), str(scene_path)],
    )
    generated_paths: list[Path] = []

    async def generate_candidate(*, ref_type, **_kwargs):
        path = tmp_path / f"identity-bad-{len(generated_paths) + 1}.jpg"
        path.write_bytes(b"identity-bad")
        generated_paths.append(path)
        return ReferenceImageAsset(
            id=f"bad-{len(generated_paths)}",
            url="u",
            path=str(path),
            type=ref_type,
            source="seedream_generated",
        )

    async def reject_identity(*_args, **_kwargs):
        return {
            **_passing_reference_qa(),
            "overall": 0.9,
            "hard_failures": ["wrong_identity"],
            "issues": ["A was replaced by an unrelated person"],
        }

    monkeypatch.setattr(video_modes, "_generate_one_reference", generate_candidate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", reject_identity)
    meta: dict = {}

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None,
        project_id="p",
        episode_no=1,
        episode_id="e",
        shot_id="s",
        shot=_shot(characters=["A"], characters_visible=["A"]),
        bible=_bible(),
        decision=video_modes.default_reference_decision(),
        existing_meta=meta,
    ))

    assert all(not path.exists() for path in generated_paths)
    assert not any(
        asset.type == "plot_key_frame" and asset.selectedForSeedance
        for asset in assets
    )
    assert {
        asset.entity_type for asset in assets if asset.selectedForSeedance
    } == {"character", "scene"}
    assert meta["reference_slots"]["narrative_keyframe"]["status"] == "identity_gate_failed"
    assert meta["keyframe_fallback_mode"] == video_modes.KEYFRAME_STRUCTURAL_FALLBACK_MODE
    assert meta["keyframe_structural_fallback_slots"] == ["narrative_keyframe"]
    assert meta["narrative_keyframe_missing"] is False


def test_keyframe_three_qa_pending_candidates_resume_without_paid_regeneration(monkeypatch, tmp_path) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    scores = [0.68, 0.73, 0.91]
    candidate_paths: list[Path] = []
    score_by_payload: dict[str, float] = {}
    for candidate_no, score in enumerate(scores, start=1):
        path = tmp_path / f"pending-{candidate_no}.jpg"
        path.write_bytes(f"pending-{candidate_no}".encode())
        candidate_paths.append(path)
        score_by_payload[hiagent.encode_image_file(str(path))] = score

    async def must_not_generate(*_args, **_kwargs):
        raise AssertionError("qa_pending candidate recovery must not call the paid image provider")

    qa_calls: list[str] = []

    async def review_candidate(payload, **_kwargs):
        qa_calls.append(payload)
        score = score_by_payload[payload]
        return {
            **_passing_reference_qa(),
            "overall": score,
            "absolute_quality": score,
        }

    monkeypatch.setattr(video_modes, "_generate_one_reference", must_not_generate)
    monkeypatch.setattr(video_modes, "_generate_reference_keep_best", must_not_generate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", review_candidate)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda _qa: True)
    fingerprint = video_modes.keyframe_contract_fingerprint(_shot(), _bible())
    existing_meta = {
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "keyframe_contract_fingerprint": fingerprint,
        "reference_slots": {
            "narrative_keyframe": {
                "status": "qa_pending",
                "type": "plot_key_frame",
                "candidate_target": 3,
                "candidates": [
                    {
                        "candidate_no": candidate_no,
                        "id": f"pending-{candidate_no}",
                        "path": str(path),
                        "status": "qa_pending",
                        "qa": None,
                        "quality_score": None,
                    }
                    for candidate_no, path in enumerate(candidate_paths, start=1)
                ],
                "prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
                "keyframe_contract_fingerprint": fingerprint,
            },
        },
    }
    rejected: list[ReferenceImageAsset] = []

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(), bible=_bible(), decision=video_modes.default_reference_decision(),
        existing_meta=existing_meta, rejected_out=rejected,
    ))

    assert len(qa_calls) == 3
    generated = [asset for asset in assets if asset.source == "seedream_generated"]
    assert len(generated) == 1
    assert generated[0].path == str(candidate_paths[2])
    assert generated[0].selectedForSeedance is True
    assert candidate_paths[2].is_file()
    assert not candidate_paths[0].exists()
    assert not candidate_paths[1].exists()
    assert rejected == []

    slot = existing_meta["reference_slots"]["narrative_keyframe"]
    assert slot["winner_candidate_no"] == 3
    assert slot["candidate_count"] == 3
    assert slot["path"] == str(candidate_paths[2])
    for item in slot["candidates"]:
        if item["candidate_no"] != 3:
            assert not item.get("path")
            assert not item.get("url")
    serialized_slot = json.dumps(slot, ensure_ascii=False)
    assert str(candidate_paths[0]) not in serialized_slot
    assert str(candidate_paths[1]) not in serialized_slot
    video_inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [asset.public_dict() for asset in assets],
    })
    assert video_inputs == [(hiagent.data_url_from_file(str(candidate_paths[2])), "reference_image")]


def test_narrative_slot_forces_plot_keyframe_and_drops_wrong_type_brief(monkeypatch, tmp_path) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    captured: dict = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        image_path = tmp_path / "generated.jpg"
        image_path.write_bytes(b"generated-image")
        return ReferenceImageAsset(
            id="g", url="u", path=str(image_path), type=kwargs["ref_type"],
            source="seedream_generated",
        ), [], []

    async def pass_review(_b64, **_kwargs):
        return _passing_reference_qa()

    monkeypatch.setattr(video_modes, "_generate_reference_keep_best", fake_generate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", pass_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda _qa: True)
    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="legacy custom plan", confidence=1.0,
        referenceImagePlan=ReferenceImagePlan(
            totalCount=1, generateNewCount=1, types=["character"],
            prompts=[{"type": "character", "prompt": "front-facing portrait character sheet"}],
        ),
    )
    contract_fingerprint = video_modes.keyframe_contract_fingerprint(_shot(), _bible())
    existing_meta = {
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "keyframe_contract_fingerprint": contract_fingerprint,
        "reference_slots": {
            "narrative_keyframe": {
                "status": "passed", "type": "character", "path": "/stale/portrait.jpg",
                "quality_score": 1.0, "qa": {"overall": 1.0, "marker": "stale"},
                "prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
                "keyframe_contract_fingerprint": contract_fingerprint,
            },
        },
    }

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(), bible=_bible(), decision=decision, existing_meta=existing_meta,
    ))

    assert captured["ref_type"] == "plot_key_frame"
    assert captured["content_override"] is None
    assert any(a.type == "plot_key_frame" and a.slot_key == "narrative_keyframe" for a in assets)
    rebuilt_slot = existing_meta["reference_slots"]["narrative_keyframe"]
    assert rebuilt_slot["type"] == "plot_key_frame"
    assert rebuilt_slot["path"] != "/stale/portrait.jpg"
    assert rebuilt_slot["qa"].get("marker") != "stale"


def test_prompt_checkpoint_is_persisted_before_provider_and_reused_after_restart(monkeypatch, tmp_path) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: True)
    events: list[str] = []
    writer_calls = {"n": 0}
    existing_meta: dict = {}

    async def write_once(*_args, **_kwargs):
        writer_calls["n"] += 1
        return "SAVED LLM KEYFRAME PROMPT"

    async def crash_provider(**_kwargs):
        assert any(event == "progress:prompt_ready" for event in events)
        events.append("provider:crash")
        raise RuntimeError("simulated worker crash after prompt checkpoint")

    def progress(_assets, _rejected):
        status = (existing_meta.get("reference_slots") or {}).get("narrative_keyframe", {}).get("status")
        events.append(f"progress:{status}")

    monkeypatch.setattr(video_modes, "write_reference_prompt", write_once)
    monkeypatch.setattr(video_modes, "_generate_reference_keep_best", crash_provider)

    with pytest.raises(RuntimeError, match="simulated worker crash"):
        asyncio.run(video_modes.build_reference_assets(
            conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
            shot=_shot(), bible=_bible(), decision=video_modes.default_reference_decision(),
            existing_meta=existing_meta, on_progress=progress,
        ))

    checkpoint = existing_meta["reference_slots"]["narrative_keyframe"]
    assert checkpoint["status"] == "prompt_ready"
    assert checkpoint["prompt"] == "SAVED LLM KEYFRAME PROMPT"
    assert checkpoint["prompt_source"] == "llm_override"

    async def writer_must_not_repeat(*_args, **_kwargs):
        raise AssertionError("restart must reuse the saved prompt")

    async def resume_provider(**kwargs):
        assert kwargs["content_override"] == "SAVED LLM KEYFRAME PROMPT"
        image_path = tmp_path / "resumed.jpg"
        image_path.write_bytes(b"resumed-image")
        return ReferenceImageAsset(
            id="resumed", url="u", path=str(image_path), type=kwargs["ref_type"],
            source="seedream_generated",
        ), [], []

    async def pass_review(_b64, **_kwargs):
        return _passing_reference_qa()

    monkeypatch.setattr(video_modes, "write_reference_prompt", writer_must_not_repeat)
    monkeypatch.setattr(video_modes, "_generate_reference_keep_best", resume_provider)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", pass_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda _qa: True)

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(), bible=_bible(), decision=video_modes.default_reference_decision(),
        existing_meta=existing_meta,
    ))

    assert writer_calls["n"] == 1
    assert existing_meta["reference_slots"]["narrative_keyframe"]["status"] == "passed"
    assert any(a.id == "resumed" and a.selectedForSeedance for a in assets)


def test_deterministic_prompt_checkpoint_with_none_skips_writer_on_restart(monkeypatch, tmp_path) -> None:
    _patch_reference_build_unit(monkeypatch)
    import app.multiview as mv

    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: True)
    fingerprint = video_modes.keyframe_contract_fingerprint(_shot(), _bible())
    existing_meta = {
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "keyframe_contract_fingerprint": fingerprint,
        "reference_slots": {
            "narrative_keyframe": {
                "status": "prompt_ready", "type": "plot_key_frame", "prompt": None,
                "prompt_source": "deterministic_template",
                "prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
                "keyframe_contract_fingerprint": fingerprint,
            },
        },
    }

    async def writer_must_not_run(*_args, **_kwargs):
        raise AssertionError("deterministic prompt checkpoint must skip the writer")

    async def fake_generate(**kwargs):
        assert kwargs["content_override"] is None
        image_path = tmp_path / "deterministic.jpg"
        image_path.write_bytes(b"image")
        return ReferenceImageAsset(
            id="deterministic", url="u", path=str(image_path), type="plot_key_frame",
            source="seedream_generated",
        ), [], []

    async def pass_review(_b64, **_kwargs):
        return _passing_reference_qa()

    monkeypatch.setattr(video_modes, "write_reference_prompt", writer_must_not_run)
    monkeypatch.setattr(video_modes, "_generate_reference_keep_best", fake_generate)
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", pass_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda _qa: True)

    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=_shot(), bible=_bible(), decision=video_modes.default_reference_decision(),
        existing_meta=existing_meta,
    ))

    assert any(a.id == "deterministic" and a.selectedForSeedance for a in assets)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, project_row):
        self._project_row = project_row

    def execute(self, sql, params=()):
        if "FROM projects" in sql:
            return _FakeCursor(self._project_row)
        return _FakeCursor(None)

    def commit(self):
        return None


def _shot_row(**kwargs) -> dict:
    row = {
        "shot_no": 1,
        "duration_s": 5,
        "shot_size": "特写",
        "camera_move": "推近",
        "scene_setting": "日, 萧家广场",
        "characters": json.dumps(["A"]),
        "action_desc": "A站在魔石碑前，碑面爆发出嘈杂声，A紧握双拳，淡出淡入。",
        "first_frame_desc": "A站在魔石碑前。",
        "last_frame_desc": "A仍站在碑前。",
        "source_excerpt": "A站在魔石碑前。",
        "narration": None,
        "dialogues": json.dumps([]),
        "transition": "淡出淡入",
        "continuity_from_prev": 0,
    }
    row.update(kwargs)
    return row


def test_runtime_reference_mode_uses_stored_decision(monkeypatch) -> None:
    """生成期复用入队时定好的参考图决策，不再跑一次运行期 LLM 选择（省调用、避免模式翻转）。
    既然存的是参考图决策且能拿到合格参考图，就直接以参考图模式生成，无需任何回退。"""

    def fail_select(*a, **k):
        raise AssertionError("生成期不应再调用 LLM 模式选择")

    async def fake_build_reference_assets(**kwargs):
        assets = [ReferenceImageAsset(
            id="r1", url="data:image/jpeg;base64,abc", type="plot_key_frame",
            source="seedream_generated", selectedForSeedance=True,
            purposes=["video_input", "qa_anchor"], slot_key="narrative_keyframe", required=True,
            qa={"overall": 0.9, "status": "scored", "absolute_quality": 0.9},
            qualityScore=0.9,
        )]
        kwargs["on_progress"](assets, [])
        meta = kwargs.get("existing_meta")
        if isinstance(meta, dict):
            meta["narrative_keyframe_missing"] = False
            meta["reference_group_gate_passed"] = True
        return assets

    # 运行期一旦调用 LLM 选择即视为回归（应已被移除）
    monkeypatch.setattr(ShotVideoModeSelector, "select", fail_select)
    writes: list[dict] = []
    monkeypatch.setattr(worker, "_set_version", lambda *a, **k: writes.append(k))
    monkeypatch.setattr(video_modes, "build_reference_assets", fake_build_reference_assets)

    reference_decision = decision_to_dict(ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="对白镜，保持角色与场景一致", confidence=0.9,
        needGenerateNewReferences=True,
        referenceImagePlan=ReferenceImagePlan(totalCount=2, reusePreviousSceneCount=0, generateNewCount=2),
    ))
    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    job = {"id": "j1", "project_id": "p1", "episode_id": "e1", "shot_id": "s1"}
    version = {"id": "v1"}
    shot = _shot_row()
    ep = {"episode_no": 1}
    meta = {"mode": REFERENCE_IMAGE_MODE, "mode_decision": reference_decision, "after_shot_id": None}

    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)

    out_meta, _ = asyncio.run(
        worker._prepare_reference_mode_inputs(conn, job, version, shot, ep, meta, "PROMPT"))

    assert out_meta["mode"] == REFERENCE_IMAGE_MODE
    assert out_meta.get("reference_images")
    streamed = [
        json.loads(write["image_inputs"])
        for write in writes
        if write.get("image_inputs")
    ]
    assert any(item.get("reference_generation_complete") is False for item in streamed)
    assert streamed[-1]["reference_generation_complete"] is True
    assert not out_meta.get("fallback_reason")


def test_runtime_auto_repairs_missing_selected_keyframe_file(monkeypatch) -> None:
    """Anchors must not hide a poisoned keyframe checkpoint left by a stale worker."""
    build_calls: list[dict] = []

    async def fake_build_reference_assets(**kwargs):
        meta = kwargs["existing_meta"]
        build_calls.append(json.loads(json.dumps(meta)))
        if len(build_calls) == 1:
            meta.update({
                "narrative_keyframe_missing": True,
                "reference_slots": {
                    "narrative_keyframe": {
                        "status": "passed",
                        "path": "/missing/stale-keyframe.jpg",
                    },
                },
            })
            return [
                ReferenceImageAsset(
                    id="character-anchor", url="data:image/jpeg;base64,character",
                    type="character", source="asset_library",
                    selectedForSeedance=True, purposes=["video_input", "qa_anchor"],
                ),
                ReferenceImageAsset(
                    id="stale-keyframe", url=None, path="/missing/stale-keyframe.jpg",
                    type="plot_key_frame", source="seedream_generated",
                    selectedForSeedance=True, purposes=["video_input", "qa_anchor"],
                    slot_key="narrative_keyframe", required=True,
                ),
            ]
        meta["narrative_keyframe_missing"] = False
        return [ReferenceImageAsset(
            id="repaired-keyframe", url="data:image/jpeg;base64,repaired",
            type="plot_key_frame", source="seedream_generated",
            selectedForSeedance=True, purposes=["video_input", "qa_anchor"],
            slot_key="narrative_keyframe", required=True,
        )]

    writes: list[dict] = []
    monkeypatch.setattr(worker, "_set_version", lambda *a, **k: writes.append(k))
    monkeypatch.setattr(video_modes, "build_reference_assets", fake_build_reference_assets)
    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "after_shot_id": None,
    }
    out_meta, _ = asyncio.run(worker._prepare_reference_mode_inputs(
        conn,
        {"id": "j1", "project_id": "p1", "episode_id": "e1", "shot_id": "s1"},
        {"id": "v1"},
        _shot_row(),
        {"episode_no": 1},
        meta,
        "PROMPT",
    ))

    assert len(build_calls) == 2
    assert build_calls[1]["reference_images"] == []
    assert build_calls[1]["reference_slots"] == {}
    assert build_calls[1]["stale_reference_reason"] == "final_keyframe_file_missing"
    assert out_meta["reference_group_gate_passed"] is True
    assert out_meta["reference_images"][0]["id"] == "repaired-keyframe"
    assert out_meta["keyframe_file_repair_count"] == 1
    assert writes


def test_runtime_requires_repair_when_required_keyframe_gate_is_exhausted(monkeypatch) -> None:
    """关键帧修复耗尽后保持参考图模式，不得用弱输入继续提交。"""

    async def fake_build_reference_assets(**kwargs):
        kwargs["existing_meta"].update({
            "narrative_keyframe_missing": True,
            "reference_group_gate_passed": False,
        })
        return [ReferenceImageAsset(
            id="character-anchor",
            url="data:image/jpeg;base64,character",
            type="character",
            source="asset_library",
            selectedForSeedance=True,
            required=True,
            purposes=["video_input", "qa_anchor"],
        )]

    monkeypatch.setattr(worker, "_set_version", lambda *a, **k: None)
    monkeypatch.setattr(video_modes, "build_reference_assets", fake_build_reference_assets)
    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "after_shot_id": None,
    }

    with pytest.raises(
        worker.VideoInputRepairRequired,
        match="参考图关键帧",
    ):
        asyncio.run(worker._prepare_reference_mode_inputs(
            conn,
            {"id": "j1", "project_id": "p1", "episode_id": "e1", "shot_id": "s1"},
            {"id": "v1"},
            _shot_row(),
            {"episode_no": 1},
            meta,
            "PROMPT",
        ))

    assert meta["reference_group_gate_passed"] is False
    assert meta["reference_gate_retry_exhausted"] is True
    assert "reference_fallback_mode" not in meta


def test_runtime_submits_anchor_only_fallback_after_all_keyframe_candidates_fail(monkeypatch) -> None:
    """3 张候选都有结构硬伤时，执行器不得再用“缺必需关键帧”阻断提交。"""

    async def fake_build_reference_assets(**kwargs):
        assets = [
            ReferenceImageAsset(
                id="character-anchor", url="data:image/jpeg;base64,character", type="character",
                source="asset_library", selectedForSeedance=True, required=True,
                purposes=["video_input", "qa_anchor"], entity_type="character", entity_name="A",
            ),
            ReferenceImageAsset(
                id="scene-anchor", url="data:image/jpeg;base64,scene", type="scene",
                source="asset_library", selectedForSeedance=True, required=True,
                purposes=["video_input", "qa_anchor"], entity_type="scene", entity_name="room",
            ),
        ]
        meta = kwargs["existing_meta"]
        fingerprint = video_modes.keyframe_contract_fingerprint(kwargs["shot"], kwargs["bible"])
        meta.update({
            "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
            "keyframe_contract_fingerprint": fingerprint,
            "keyframe_fallback_mode": video_modes.KEYFRAME_STRUCTURAL_FALLBACK_MODE,
            "keyframe_structural_fallback_slots": ["narrative_keyframe"],
            "narrative_keyframe_missing": False,
            "keyframe_sequence": {
                "beats": [{"slot_key": "narrative_keyframe"}],
                "keyframe_plan": {"count": 1, "duration_s": 5},
            },
        })
        kwargs["on_progress"](assets, [])
        return assets

    writes: list[dict] = []
    monkeypatch.setattr(worker, "_set_version", lambda *a, **k: writes.append(k))
    monkeypatch.setattr(video_modes, "build_reference_assets", fake_build_reference_assets)
    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "after_shot_id": None,
    }
    out_meta, _ = asyncio.run(worker._prepare_reference_mode_inputs(
        conn,
        {"id": "j1", "project_id": "p1", "episode_id": "e1", "shot_id": "s1"},
        {"id": "v1"},
        _shot_row(),
        {"episode_no": 1},
        meta,
        "PROMPT",
    ))

    assert out_meta["reference_group_gate_passed"] is True
    assert out_meta["video_input_manifest_frozen"] is True
    assert out_meta["narrative_keyframe_missing"] is False
    assert {ref["type"] for ref in out_meta["reference_images"]} == {"character", "scene"}
    assert writes


def test_edited_gallery_with_changed_dependencies_is_invalidated_before_rebuild(monkeypatch) -> None:
    """Manual gallery edits cannot pin an obsolete portrait/scene revision."""
    captured: dict = {}

    async def fake_build_reference_assets(**kwargs):
        captured["meta_at_rebuild"] = json.loads(json.dumps(kwargs["existing_meta"]))
        return [ReferenceImageAsset(
            id="fresh", url="data:image/jpeg;base64,fresh", type="plot_key_frame",
            source="seedream_generated", selectedForSeedance=True,
            purposes=["video_input", "qa_anchor"], slot_key="narrative_keyframe", required=True,
        )]

    monkeypatch.setattr(worker, "_set_version", lambda *a, **k: None)
    monkeypatch.setattr(video_modes, "build_reference_assets", fake_build_reference_assets)
    monkeypatch.setattr("app.portraits.bible_for_episode", lambda _p, bible, _ep: bible)
    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)
    monkeypatch.setattr("app.multiview.resolve_shot_asset_dependencies", lambda **_k: {"revision": "new"})
    monkeypatch.setattr("app.multiview.manifest_revisions_match", lambda _old, _new: False)

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    contract_fingerprint = video_modes.keyframe_contract_fingerprint(
        worker._load_shot_model(_shot_row()), _bible(),
    )
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "keyframe_contract_fingerprint": contract_fingerprint,
        "reference_manifest": {"revision": "old"},
        "reference_manifest_frozen": True,
        "reference_generation_complete": True,
        "reference_gallery_edited": True,
        "reference_gallery_contract_override": True,
        "reference_images": [{
            "id": "old", "url": "data:image/jpeg;base64,old", "type": "plot_key_frame",
            "selectedForSeedance": True, "dependency_manifest": {"revision": "old"},
            "keyframe_contract_fingerprint": contract_fingerprint,
        }],
        "reference_slots": {
            "narrative_keyframe": {"status": "passed", "path": "/stale/keyframe.jpg", "qa": {"overall": 1}},
        },
    }

    out_meta, _ = asyncio.run(worker._prepare_reference_mode_inputs(
        conn,
        {"id": "j", "project_id": "p", "episode_id": "e", "shot_id": "s"},
        {"id": "v"}, _shot_row(), {"episode_no": 1}, meta, "PROMPT",
    ))

    rebuild_meta = captured["meta_at_rebuild"]
    assert rebuild_meta["reference_images"] == []
    assert rebuild_meta["reference_slots"] == {}
    assert rebuild_meta["stale_reference_reason"] == "reference_dependency_manifest_changed"
    assert out_meta["reference_images"][0]["id"] == "fresh"


def test_complete_gallery_is_invalidated_when_shot_keyframe_contract_changes(monkeypatch) -> None:
    captured: dict = {}

    async def fake_build_reference_assets(**kwargs):
        captured["meta_at_rebuild"] = json.loads(json.dumps(kwargs["existing_meta"]))
        return [ReferenceImageAsset(
            id="fresh", url="data:image/jpeg;base64,fresh", type="plot_key_frame",
            source="seedream_generated", selectedForSeedance=True,
            purposes=["video_input", "qa_anchor"], slot_key="narrative_keyframe", required=True,
        )]

    monkeypatch.setattr(worker, "_set_version", lambda *a, **k: None)
    monkeypatch.setattr(video_modes, "build_reference_assets", fake_build_reference_assets)
    monkeypatch.setattr("app.portraits.bible_for_episode", lambda _p, bible, _ep: bible)
    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "keyframe_contract_fingerprint": "fingerprint-for-old-neutral-action",
        "reference_generation_complete": True,
        "reference_images": [{
            "id": "old", "url": "data:image/jpeg;base64,old", "type": "plot_key_frame",
            "selectedForSeedance": True,
            "keyframe_contract_fingerprint": "fingerprint-for-old-neutral-action",
        }],
    }
    changed_shot = _shot_row(
        characters=json.dumps(["A", "B"]),
        action_desc="A已经抓住B的手。",
        first_frame_desc="A伸手靠近B。",
        last_frame_desc="A已经抓住B的手。",
    )

    asyncio.run(worker._prepare_reference_mode_inputs(
        conn,
        {"id": "j", "project_id": "p", "episode_id": "e", "shot_id": "s"},
        {"id": "v"}, changed_shot, {"episode_no": 1}, meta, "PROMPT",
    ))

    rebuild_meta = captured["meta_at_rebuild"]
    assert rebuild_meta["reference_images"] == []
    assert rebuild_meta["stale_reference_reason"] == "shot_keyframe_contract_changed"


def test_static_ready_checkpoint_missing_keyframe_cannot_skip_rebuild(monkeypatch) -> None:
    captured: dict = {}

    async def fake_build_reference_assets(**kwargs):
        captured["meta_at_rebuild"] = json.loads(json.dumps(kwargs["existing_meta"]))
        return [ReferenceImageAsset(
            id="fresh", url="data:image/jpeg;base64,fresh", type="plot_key_frame",
            source="seedream_generated", selectedForSeedance=True,
            purposes=["video_input", "qa_anchor"], slot_key="narrative_keyframe", required=True,
        )]

    monkeypatch.setattr(worker, "_set_version", lambda *a, **k: None)
    monkeypatch.setattr(video_modes, "build_reference_assets", fake_build_reference_assets)
    monkeypatch.setattr("app.portraits.bible_for_episode", lambda _p, bible, _ep: bible)
    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "reference_static_ready": True,
        "reference_generation_complete": False,
        "reference_images": [{
            "id": "evidence-only", "url": "data:image/jpeg;base64,evidence",
            "type": "character", "selectedForSeedance": False,
        }],
    }

    asyncio.run(worker._prepare_reference_mode_inputs(
        conn,
        {"id": "j", "project_id": "p", "episode_id": "e", "shot_id": "s"},
        {"id": "v"}, _shot_row(), {"episode_no": 1}, meta, "PROMPT",
    ))

    rebuild_meta = captured["meta_at_rebuild"]
    assert rebuild_meta["reference_static_ready"] is False
    assert rebuild_meta["reference_images"] == []
    assert rebuild_meta["stale_reference_reason"] == "static_keyframe_contract_or_file_invalid"


def test_previous_tail_path_and_dependency_are_version_specific(monkeypatch, tmp_path) -> None:
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")

    class TailConn:
        def execute(self, _sql, _params=()):
            return _FakeCursor({"video_path": str(video_path)})

    def fake_extract(_source, dest):
        dest.write_bytes(b"tail-frame")
        return True

    monkeypatch.setattr(video_modes, "_extract_last_frame", fake_extract)
    first = video_modes.previous_tail_reference_asset(
        TailConn(), {"id": "prev", "adopted_version_id": "ver_1"}, dest_dir=tmp_path / "refs",
    )
    second = video_modes.previous_tail_reference_asset(
        TailConn(), {"id": "prev", "adopted_version_id": "ver_2"}, dest_dir=tmp_path / "refs",
    )

    assert first is not None and second is not None
    assert first.path != second.path
    assert first.dependency_manifest["continuity_source"]["adopted_version_id"] == "ver_1"
    assert second.dependency_manifest["continuity_source"]["adopted_version_id"] == "ver_2"
    assert first.path and second.path and Path(first.path).read_bytes() == Path(second.path).read_bytes() == b"tail-frame"


def test_reference_candidates_stay_hidden_until_winner_is_selected(monkeypatch, tmp_path) -> None:
    """三张候选可并发完成，但 winner 决出前不得把任何一张暴露给视频画廊。"""
    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "_portrait_seed_inputs", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "batch_prompt_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 0)
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 0)
    monkeypatch.setattr(video_modes, "max_character_reference_images", lambda: 2)
    monkeypatch.setattr(video_modes, "quality_threshold", lambda: 0.8)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)

    import app.multiview as mv
    _patch_multiview_production_ready(monkeypatch)

    async def fake_review(b64, **kwargs):
        return {
            "status": "scored", "overall": 0.9, "action_match": 0.9, "body_proportion": 0.9,
            "face_identity": 0.9, "outfit_match": 0.9, "hair_match": 0.9, "scene_match": 0.9,
            "hard_failures": [], "issues": [], "absolute_quality": 0.9,
        }
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", fake_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda qa: True)

    async def fake_generate(*, index, **kwargs):
        await asyncio.sleep(0.03 if index == 1 else 0.001)
        path = tmp_path / f"g{index}.jpg"
        path.write_bytes(b"img")
        return (
            ReferenceImageAsset(
                id=f"g{index}", url=f"u{index}", type="plot_key_frame",
                source="seedream_generated", path=str(path), qualityScore=0.9,
                qa={"overall": 0.9, "absolute_quality": 0.9},
            ),
            [],
            [],
        )

    async def skip_consistency(*, selected, **kwargs):
        return selected

    monkeypatch.setattr(video_modes, "_generate_reference_keep_best", fake_generate)
    monkeypatch.setattr(video_modes, "_enforce_reference_consistency", skip_consistency)

    snapshots: list[list[str]] = []
    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE,
        reason="x",
        confidence=1.0,
        referenceImagePlan=ReferenceImagePlan(
            totalCount=1,
            generateNewCount=1,
            types=["plot_key_frame"],
        ),
    )

    result = asyncio.run(video_modes.build_reference_assets(
        conn=None,
        project_id="p",
        episode_no=1,
        episode_id="e",
        shot_id="s",
        shot=_shot(),
        bible=_bible(),
        decision=decision,
        on_progress=lambda current, _rejected: snapshots.append([a.id for a in current]),
    ))

    # 候选 2/3 虽先完成，但画廊始终只能出现最终 winner。
    generated_snapshots = [ids for ids in snapshots if any(i.startswith("g") for i in ids)]
    assert generated_snapshots, "应至少发布一次进度快照"
    assert all(ids == ["g1"] for ids in generated_snapshots)
    assert {a.id for a in result if a.id.startswith("g")} == {"g1"}
    assert not (tmp_path / "g2.jpg").exists()
    assert not (tmp_path / "g3.jpg").exists()


def test_build_reference_assets_fallback_keyframe_yields_to_clean_portrait(monkeypatch, tmp_path) -> None:
    """低于门禁的兜底生成关键帧进废弃；干净定妆照（满分）留下。不再用 duplicate_character_suppressed 硬剔。"""
    bible = _bible()
    shot = _shot(shot_no=3, narration="次日清晨，新闻和昨晚补的细节吻合",
                 dialogues=[{"speaker": "A", "line": "这不可能", "emotion": "惊恐"}])

    monkeypatch.setattr(video_modes, "character_reference_assets",
                        lambda b, names, *, limit, project_id=None, episode_no=None: ([ReferenceImageAsset(
                            id="c1", url="u", type="character", source="asset_library",
                            path="/tmp/a.jpg", relatedCharacterIds=["A"], qualityScore=1.0,
                            qa={"overall": 1.0, "absolute_quality": 1.0})] if limit > 0 else []))

    import app.multiview as mv
    _patch_multiview_production_ready(monkeypatch)
    async def _fake_kf_review(b64, **kwargs):
        return {"status": "scored", "overall": 0.5, "action_match": 0.5, "body_proportion": 0.5,
                "face_identity": 0.5, "outfit_match": 0.5, "hair_match": 0.5, "scene_match": 0.5,
                "hard_failures": [], "issues": [], "absolute_quality": 0.5}
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", _fake_kf_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda qa: False)
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 1)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 2)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: True)
    monkeypatch.setattr(video_modes, "batch_prompt_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "quality_threshold", lambda: 0.8)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)

    prompt_calls = {"n": 0}

    async def fake_write_prompt(shot, bible, ref_type, *, intent=None):
        prompt_calls["n"] += 1
        return f"detailed english prompt for {ref_type} #{prompt_calls['n']}"

    monkeypatch.setattr(video_modes, "write_reference_prompt", fake_write_prompt)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index, content_override=None, seed_inputs=None,
                           **kwargs):
        assert content_override, "每张图必须带逐图异步生成的提示词"
        score = 0.5 + 0.1 * (index % 3)  # 0.5/0.6/0.7：均低于阈值 0.8，但高于地板 0.4
        path = tmp_path / f"g{index}.jpg"
        path.write_bytes(b"img")
        asset = ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                    path=str(path), qualityScore=score,
                                    qa={"overall": score, "absolute_quality": score, "issues": []})
        asset.rejectReason = "quality_below_threshold"
        return asset

    monkeypatch.setattr(video_modes, "_generate_one_reference", fake_gen_one)

    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="对白", confidence=0.9,
        referenceImagePlan=ReferenceImagePlan(totalCount=1, reusePreviousSceneCount=0,
                                              generateNewCount=0, types=["plot_key_frame"], prompts=[]))
    rejected: list = []
    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=shot, bible=bible, decision=decision, prev_shot=None, rejected_out=rejected))

    # 新合同：人物/场景锚点优先进入 Seedance，剩余席位才放关键帧。
    assert any(a.source == "asset_library" and a.selectedForSeedance for a in assets)
    generated = [a for a in assets if a.source == "seedream_generated"]
    assert generated and all(a.selectedForSeedance for a in generated)
    assert all(a.rejectReason == "quality_below_threshold_score_only" for a in generated)
    assert prompt_calls["n"] >= 1, "仍应尝试生成必需关键帧"
    suppressed = [a for a in rejected if a.source == "seedream_generated"]
    assert suppressed == []


def test_build_reference_assets_all_low_scores_still_keep_best_without_gate(monkeypatch, tmp_path) -> None:
    """不设分数门禁：三张都低分也要保留最高分候选，其余候选直接删除。"""
    bible = _bible()
    shot = _shot(shot_no=5, narration="夜里独白",
                 dialogues=[{"speaker": "A", "line": "为什么", "emotion": "悲伤"}])

    monkeypatch.setattr(video_modes, "character_reference_assets",
                        lambda b, names, *, limit, project_id=None, episode_no=None: ([ReferenceImageAsset(
                            id="c1", url="u", type="character", source="asset_library",
                            path="/tmp/a.jpg", relatedCharacterIds=["A"], qualityScore=1.0,
                            qa={"overall": 1.0, "absolute_quality": 1.0})] if limit > 0 else []))

    import app.multiview as mv
    _patch_multiview_production_ready(monkeypatch)
    async def _fake_kf_review(b64, **kwargs):
        score = {b"candidate-1": 0.2, b"candidate-2": 0.3, b"candidate-3": 0.25}[
            base64.b64decode(b64)
        ]
        return {"status": "scored", "overall": score, "action_match": score, "body_proportion": score,
                "face_identity": score, "outfit_match": score, "hair_match": score, "scene_match": score,
                "hard_failures": [], "issues": [], "absolute_quality": score}
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", _fake_kf_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda qa: False)
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 1)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 2)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "batch_prompt_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index, content_override=None, seed_inputs=None,
                           **kwargs):
        candidate_no = index // 100
        path = tmp_path / f"candidate-{candidate_no}.jpg"
        path.write_bytes(f"candidate-{candidate_no}".encode())
        asset = ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                    path=str(path))
        return asset

    monkeypatch.setattr(video_modes, "_generate_one_reference", fake_gen_one)

    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="对白", confidence=0.9,
        referenceImagePlan=ReferenceImagePlan(totalCount=1, reusePreviousSceneCount=0,
                                              generateNewCount=0, types=["plot_key_frame"], prompts=[]))
    rejected: list = []
    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=shot, bible=bible, decision=decision, prev_shot=None, rejected_out=rejected))

    fed = [a for a in assets if a.selectedForSeedance]
    generated = [a for a in fed if a.source == "seedream_generated"]
    assert len(generated) == 1
    assert generated[0].qualityScore == pytest.approx(0.3)
    assert generated[0].rejectReason == "quality_below_threshold_score_only"
    # 定妆照作为身份锚点与关键帧同时进入 video_input。
    assert any(a.source == "asset_library" for a in assets)
    assert any(a.source == "asset_library" and a.selectedForSeedance for a in assets)
    assert rejected == []
    assert not (tmp_path / "candidate-1.jpg").exists()
    assert (tmp_path / "candidate-2.jpg").is_file()
    assert not (tmp_path / "candidate-3.jpg").exists()


def test_generated_references_get_i2i_seeds(monkeypatch, tmp_path) -> None:
    """根因修复：新生成的参考图必须带 i2i 种子。定妆照（锁身份/服饰）只喂给含人物的图
    （character / plot_key_frame），纯场景图（scene）不注入人物定妆照，避免把角色塞进环境图；
    姿态/动作仍由文字提示词决定（见 _SEED_USAGE_NOTE）。"""
    bible = _bible()
    shot = _shot(shot_no=2)

    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 0)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 0)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "batch_prompt_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "_portrait_seed_inputs", lambda *a, **k: ["PORTRAIT_A"])
    _patch_multiview_production_ready(monkeypatch)
    import app.multiview as mv

    async def pass_review(*_args, **_kwargs):
        return _passing_reference_qa()

    monkeypatch.setattr(mv, "review_keyframe_with_evidence", pass_review)
    monkeypatch.setattr(video_modes, "review_reference_image", pass_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda _qa: True)

    seen: dict[str, list] = {}

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index,
                           content_override=None, seed_inputs=None, **kwargs):
        seen[ref_type] = list(seed_inputs or [])
        path = tmp_path / f"g{index}.jpg"
        path.write_bytes(b"image")
        return ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                   path=str(path), qualityScore=0.9,
                                   qa={"overall": 0.9, "absolute_quality": 0.9, "issues": []})

    monkeypatch.setattr(video_modes, "_generate_one_reference", fake_gen_one)

    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="x", confidence=1.0,
        referenceImagePlan=ReferenceImagePlan(totalCount=2, reusePreviousSceneCount=0,
                                              generateNewCount=2, types=["plot_key_frame", "scene"], prompts=[]))
    asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=shot, bible=bible, decision=decision, prev_shot=None))

    assert seen.get("plot_key_frame") == ["PORTRAIT_A"], "含人物的参考图必须以定妆照做 i2i 种子"
    assert seen.get("scene") == [], "纯场景图不应注入人物定妆照"


def _consistency_settings(monkeypatch, *, retries: int) -> None:
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: True)
    monkeypatch.setattr(video_modes, "consistency_threshold", lambda: 0.7)
    monkeypatch.setattr(video_modes, "consistency_retries", lambda: retries)


def test_consistency_agent_regenerates_drifted_reference(monkeypatch) -> None:
    """QA 只评分：低一致性只扣分标记，不触发 i2i 重生或废弃。"""
    bible, shot = _bible(), _shot(shot_no=2)
    _consistency_settings(monkeypatch, retries=0)
    monkeypatch.setattr(video_modes, "quality_threshold", lambda: 0.8)

    anchor = ReferenceImageAsset(id="p1", url="PORTRAIT", type="character", source="asset_library",
                                 path="/tmp/p1.jpg", qualityScore=1.0,
                                 qa={"overall": 1.0, "absolute_quality": 1.0})
    good = ReferenceImageAsset(id="g_good", url="u", type="plot_key_frame", source="seedream_generated",
                               path="/tmp/good.jpg", qualityScore=0.9, selectedForSeedance=True,
                               qa={"overall": 0.9, "absolute_quality": 0.9})
    bad = ReferenceImageAsset(id="g_bad", url="u", type="plot_key_frame", source="seedream_generated",
                              path="/tmp/bad.jpg", qualityScore=0.9, selectedForSeedance=True,
                              qa={"overall": 0.9, "absolute_quality": 0.9})

    async def fake_review(*, candidates, anchors, shot, bible):
        return {"candidates": [{"asset_id": c.id, "consistency": 0.4 if "bad" in c.id else 0.95,
                                "drift": ["costume", "hair"] if "bad" in c.id else [], "issues": []}
                               for c in candidates], "overall": 0.7}

    monkeypatch.setattr(video_modes, "review_reference_consistency", fake_review)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index,
                           content_override=None, seed_inputs=None, extra_instruction=None, skip_inline_qa=False):
        raise AssertionError("QA 只评分不应因一致性漂移重生参考图")

    monkeypatch.setattr(video_modes, "_generate_one_reference", fake_gen_one)

    rejected: list = []
    rej_details: list = []
    result = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[anchor, good, bad], shot=shot, bible=bible, project_id="p", episode_no=1,
        rejection_details=rej_details, rejected_out=rejected))
    result = video_modes._finalize_reference_selection(
        result, rejected_out=rejected, rejection_details=rej_details)

    ids = [a.id for a in result]
    assert "g_good" in ids and "g_bad" in ids and "p1" in ids, "漂移图只扣分标记，仍保留"
    bad_asset = next(a for a in result if a.id == "g_bad")
    assert bad_asset.qa["consistency"] == 0.4
    assert bad_asset.rejectReason == "quality_below_threshold_score_only"
    assert rejected == []
    assert rej_details == []


def test_consistency_agent_drops_unfixable_reference(monkeypatch) -> None:
    """QA 只评分：持续低一致性也只保留分数/漂移标记，不重生不丢弃。"""
    bible, shot = _bible(), _shot(shot_no=2)
    _consistency_settings(monkeypatch, retries=0)
    monkeypatch.setattr(video_modes, "quality_threshold", lambda: 0.8)

    anchor = ReferenceImageAsset(id="p1", url="PORTRAIT", type="character", source="asset_library",
                                 path="/tmp/p1.jpg", qualityScore=1.0,
                                 qa={"overall": 1.0, "absolute_quality": 1.0})
    good = ReferenceImageAsset(id="g_good", url="u", type="plot_key_frame", source="seedream_generated",
                               path="/tmp/good.jpg", qualityScore=0.9, selectedForSeedance=True,
                               qa={"overall": 0.9, "absolute_quality": 0.9})
    bad = ReferenceImageAsset(id="g_bad", url="u", type="plot_key_frame", source="seedream_generated",
                              path="/tmp/bad.jpg", qualityScore=0.9, selectedForSeedance=True,
                              qa={"overall": 0.9, "absolute_quality": 0.9})

    async def fake_review(*, candidates, anchors, shot, bible):
        return {"candidates": [{"asset_id": c.id, "consistency": 0.3 if "bad" in c.id else 0.95,
                                "drift": ["style"] if "bad" in c.id else [], "issues": []}
                               for c in candidates], "overall": 0.5}

    monkeypatch.setattr(video_modes, "review_reference_consistency", fake_review)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index,
                           content_override=None, seed_inputs=None, extra_instruction=None, skip_inline_qa=False):
        raise AssertionError("QA 只评分不应因一致性漂移重生参考图")

    monkeypatch.setattr(video_modes, "_generate_one_reference", fake_gen_one)

    rejected: list = []
    result = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[anchor, good, bad], shot=shot, bible=bible, project_id="p", episode_no=1,
        rejection_details=[], rejected_out=rejected))
    result = video_modes._finalize_reference_selection(result, rejected_out=rejected)

    ids = [a.id for a in result]
    assert "g_good" in ids and "g_bad" in ids and "p1" in ids, "低一致性图仍保留给 Seedance"
    bad_asset = next(a for a in result if a.id == "g_bad")
    assert bad_asset.qa["consistency"] == 0.3
    assert bad_asset.selectedForSeedance is True
    assert bad_asset.rejectReason == "quality_below_threshold_score_only"
    assert rejected == []


def test_consistency_agent_skips_without_anchor(monkeypatch) -> None:
    """无锚点（无定妆照/上镜尾帧）时跳过相对判定，避免误删——此时不调用 VLM。"""
    bible, shot = _bible(), _shot(shot_no=2)
    _consistency_settings(monkeypatch, retries=1)

    async def boom(*a, **k):
        raise AssertionError("无锚点不应调用一致性检查 Agent")

    monkeypatch.setattr(video_modes, "review_reference_consistency", boom)
    only_gen = ReferenceImageAsset(id="g1", url="u", type="plot_key_frame", source="seedream_generated",
                                   path="/tmp/g1.jpg", qualityScore=0.9, selectedForSeedance=True)
    result = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[only_gen], shot=shot, bible=bible, project_id="p", episode_no=1))
    assert [a.id for a in result] == ["g1"]


def test_consistency_check_failure_does_not_fake_perfect_score(monkeypatch, tmp_path) -> None:
    """VLM 失败时不得返回 consistency=1.0 伪造成「完美一致」。"""
    bible, shot = _bible(), _shot(shot_no=2)
    _consistency_settings(monkeypatch, retries=1)

    async def boom(*_a, **_k):
        raise RuntimeError("vlm down")

    img = tmp_path / "x.jpg"
    img.write_bytes(b"fake-image")
    monkeypatch.setattr(video_modes.hiagent, "vlm_check", boom)
    monkeypatch.setattr(video_modes.hiagent, "encode_image_file", lambda _p: "YmFzZTY0")
    cand = ReferenceImageAsset(
        id="g1", url="u", type="plot_key_frame", source="seedream_generated",
        path=str(img), qualityScore=0.9, selectedForSeedance=True,
    )
    anchor = ReferenceImageAsset(
        id="p1", url="u2", type="character_sheet", source="asset_library",
        path=str(img), qualityScore=0.95, selectedForSeedance=True,
    )
    report = asyncio.run(video_modes.review_reference_consistency(
        candidates=[cand], anchors=[anchor], shot=shot, bible=bible))
    assert report.get("failed") is True
    assert report["candidates"][0]["consistency"] is None
    assert report["candidates"][0].get("check_failed") is True

    cand2 = ReferenceImageAsset(
        id="g2", url="u3", type="plot_key_frame", source="seedream_generated",
        path=str(img), qualityScore=0.88, selectedForSeedance=True,
    )
    kept = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[cand, cand2, anchor], shot=shot, bible=bible, project_id="p", episode_no=1))
    assert {a.id for a in kept} == {"g1", "g2", "p1"}
    assert (cand.qa or {}).get("consistency_check_failed") is True


def test_single_generated_candidate_skips_redundant_group_consistency(monkeypatch, tmp_path) -> None:
    bible, shot = _bible(), _shot(shot_no=2)
    _consistency_settings(monkeypatch, retries=1)
    img = tmp_path / "single.jpg"
    img.write_bytes(b"fake-image")

    async def must_not_run(*_a, **_k):
        raise AssertionError("single candidate must reuse evidence QA instead of a second VLM call")

    monkeypatch.setattr(video_modes, "review_reference_consistency", must_not_run)
    cand = ReferenceImageAsset(
        id="g1", url="u", type="plot_key_frame", source="seedream_generated",
        path=str(img), qualityScore=0.9, selectedForSeedance=True,
    )
    anchor = ReferenceImageAsset(
        id="p1", url="u2", type="character_sheet", source="asset_library",
        path=str(img), qualityScore=0.95, selectedForSeedance=True,
    )

    kept = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[cand, anchor], shot=shot, bible=bible, project_id="p", episode_no=1))

    assert kept == [cand, anchor]


def test_compose_reference_score_weights() -> None:
    """综合分以绝对分为底，一致性只降不抬；硬伤压乘数；冗余软惩罚。"""
    base = video_modes.compose_reference_score(absolute_quality=0.7, consistency=1.0)
    assert base["overall"] == 0.7  # cons=1 不抬分
    mild = video_modes.compose_reference_score(absolute_quality=1.0, consistency=0.5)
    # 1.0 * (0.55 + 0.35*0.5) / 0.9 = 0.806...
    assert mild["overall"] >= 0.8
    harsh = video_modes.compose_reference_score(absolute_quality=0.9, consistency=0.3)
    assert harsh["overall"] < 0.8
    hard = video_modes.compose_reference_score(
        absolute_quality=1.0, consistency=1.0, hard_failures=["watermark"])
    assert hard["overall"] <= 0.3
    penalized = video_modes.compose_reference_score(
        absolute_quality=1.0, consistency=1.0, redundancy_penalty=0.15)
    assert abs(penalized["overall"] - 0.85) < 0.001


def test_high_absolute_mild_consistency_must_keep() -> None:
    """绝对分高 + 一致性略低，综合分仍 ≥0.8 → 必须 selected。"""
    asset = ReferenceImageAsset(
        id="g1", url="u", type="plot_key_frame", source="seedream_generated",
        path="/tmp/g1.jpg", qualityScore=1.0,
        qa={"overall": 1.0, "absolute_quality": 1.0})
    video_modes.recompose_asset_score(asset, consistency=0.5)
    assert (asset.qualityScore or 0) >= 0.8
    assert video_modes.apply_keep_gate(asset, threshold=0.8) is True
    assert asset.selectedForSeedance is True
    assert asset.rejectReason is None


def test_multiple_high_score_character_refs_keep_one_truth_anchor_with_keyframe() -> None:
    """多张含人物高分图全部 selected；装箱只取 Top-N，不改 selected。"""
    assets = [
        ReferenceImageAsset(
            id="c1", url="u1", type="character", source="asset_library",
            path="/tmp/c1.jpg", relatedCharacterIds=["A"], qualityScore=1.0,
            qa={"overall": 1.0, "absolute_quality": 1.0}),
        ReferenceImageAsset(
            id="g1", url="u2", type="plot_key_frame", source="seedream_generated",
            path="/tmp/g1.jpg", relatedCharacterIds=["A"], qualityScore=0.95,
            qa={"overall": 0.95, "absolute_quality": 0.95}),
        ReferenceImageAsset(
            id="g2", url="u3", type="character", source="seedream_generated",
            path="/tmp/g2.jpg", relatedCharacterIds=["A"], qualityScore=0.92,
            qa={"overall": 0.92, "absolute_quality": 0.92}),
    ]
    kept = video_modes._finalize_reference_selection(assets, rejected_out=[])
    assert {a.id for a in kept} == {"c1", "g1", "g2"}
    assert all(a.selectedForSeedance for a in kept)

    refs = [a.public_dict() for a in kept]
    for r in refs:
        r["selectedForSeedance"] = True
    packed = video_modes.pack_reference_images_for_seedance(refs, max_images=8)
    # 剧情关键帧与一张干净人物真值图共同绑定到同一身份。
    assert [ref["id"] for ref in packed] == ["g1", "c1"]
    assert all(r["selectedForSeedance"] for r in refs)


def test_seedance_provider_inputs_keep_character_truth_anchor_with_keyframe() -> None:
    refs = [
        {
            "id": "character-a", "url": "data:image/jpeg;base64,YQ==", "type": "character",
            "entity_name": "A", "relatedCharacterIds": ["A"], "selectedForSeedance": True,
            "rejectReason": "missing_quality_score",
            "purposes": ["keyframe_seed", "qa_anchor", "video_input"], "required": True,
        },
        {
            "id": "scene", "url": "data:image/jpeg;base64,cw==", "type": "scene",
            "selectedForSeedance": True, "purposes": ["qa_anchor", "video_input"],
        },
        {
            "id": "keyframe-a", "url": "data:image/jpeg;base64,aw==", "type": "plot_key_frame",
            "relatedCharacterIds": ["A"], "selectedForSeedance": True,
            "slot_key": "narrative_keyframe", "purposes": ["qa_anchor", "video_input"],
        },
    ]
    meta = {"mode": REFERENCE_IMAGE_MODE, "reference_images": refs}

    inputs = video_modes.build_seedance_image_inputs(meta)

    assert inputs == [
        ("data:image/jpeg;base64,aw==", "reference_image"),
        ("data:image/jpeg;base64,cw==", "reference_image"),
        ("data:image/jpeg;base64,YQ==", "reference_image"),
    ]
    assert refs[0]["selectedForSeedance"] is True
    assert "video_input" in refs[0]["purposes"]
    assert refs[1]["selectedForSeedance"] is True
    assert refs[2]["selectedForSeedance"] is True


def test_seedance_keeps_anchor_for_identity_missing_from_keyframe() -> None:
    refs = [
        {
            "id": "character-b", "url": "data:image/jpeg;base64,Yg==", "type": "character",
            "entity_name": "B", "relatedCharacterIds": ["B"], "selectedForSeedance": True,
            "purposes": ["qa_anchor", "video_input"],
        },
        {
            "id": "keyframe-a", "url": "data:image/jpeg;base64,YQ==", "type": "plot_key_frame",
            "relatedCharacterIds": ["A"], "selectedForSeedance": True,
            "slot_key": "narrative_keyframe", "purposes": ["qa_anchor", "video_input"],
        },
    ]

    packed = video_modes.pack_reference_images_for_seedance(refs, max_images=9)

    assert [ref["id"] for ref in packed] == ["keyframe-a", "character-b"]


def test_pack_keeps_one_anchor_for_each_distinct_character(monkeypatch) -> None:
    monkeypatch.setattr(video_modes, "max_character_reference_images", lambda: 1)
    refs = [
        {
            "id": "character-a", "url": "data:image/jpeg;base64,a", "type": "character",
            "entity_name": "A", "selectedForSeedance": True, "qualityScore": 0.95,
            "purposes": ["video_input", "qa_anchor"],
        },
        {
            "id": "character-b", "url": "data:image/jpeg;base64,b", "type": "character",
            "entity_name": "B", "selectedForSeedance": True, "qualityScore": 0.9,
            "purposes": ["video_input", "qa_anchor"],
        },
        {
            "id": "scene", "url": "data:image/jpeg;base64,s", "type": "scene",
            "selectedForSeedance": True, "qualityScore": 0.8,
            "purposes": ["video_input", "qa_anchor"],
        },
    ]

    packed = video_modes.pack_reference_images_for_seedance(refs, max_images=3)

    assert {ref["id"] for ref in packed} == {"character-a", "character-b", "scene"}


def test_pack_seedance_prefers_score_and_keeps_gallery_selection(monkeypatch) -> None:
    monkeypatch.setattr(video_modes, "max_character_reference_images", lambda: 1)
    monkeypatch.setattr(video_modes, "max_reference_images", lambda: 2)
    refs = [
        {"id": "a", "url": "data:image/jpeg;base64,aaa", "selectedForSeedance": True,
         "type": "character", "qualityScore": 0.99},
        {"id": "b", "url": "data:image/jpeg;base64,bbb", "selectedForSeedance": True,
         "type": "plot_key_frame", "qualityScore": 0.95},
        {"id": "s", "url": "data:image/jpeg;base64,sss", "selectedForSeedance": True,
         "type": "scene", "qualityScore": 0.9},
    ]
    inputs = build_seedance_image_inputs({"mode": REFERENCE_IMAGE_MODE, "reference_images": refs})
    # 人物上限 1 + 场景 → 最多 2 张；分数最高人物 + 场景
    assert len(inputs) == 2
    assert all(r["selectedForSeedance"] for r in refs)


def test_build_reference_assets_warns_and_continues_with_incomplete_pack(monkeypatch) -> None:
    bible = _bible()
    shot = _shot(shot_no=9, scene_name="广场")
    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 0)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)

    import app.multiview as mv

    async def failed_pack(*_a, **_k):
        return {"status": "failed", "failed_view": "profile"}

    monkeypatch.setattr(mv, "complete_legacy_character_pack", failed_pack)
    monkeypatch.setattr(mv, "complete_legacy_scene_pack", failed_pack)
    monkeypatch.setattr(mv, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(mv, "scene_multiview_enabled", lambda: True)
    monkeypatch.setattr(mv, "narrative_keyframe_required", lambda: False)
    monkeypatch.setattr(mv, "resolve_shot_asset_dependencies", lambda **_kwargs: {
        "characters": [], "scene": None,
    })

    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="x", confidence=1.0,
        referenceImagePlan=ReferenceImagePlan(totalCount=0, generateNewCount=0, types=[]),
    )
    meta = {}
    assets = asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=shot, bible=bible, decision=decision, prev_shot=None, existing_meta=meta))
    assert assets == []
    assert meta["asset_pack_gate_retry_exhausted"] is True
    assert len(meta["asset_pack_warnings"]) == 2


def test_build_reference_assets_reuses_frozen_manifest_when_revisions_match(monkeypatch) -> None:
    """worker 重启后依赖不变：冻结 manifest 版本一致时必须复用，不重新选资产。"""
    bible = _bible()
    shot = _shot(shot_no=10)
    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 0)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 0)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "batch_prompt_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)

    import app.multiview as mv
    frozen = {
        "episode_no": 1, "shot_id": "s",
        "characters": [{"name": "A", "look_revision_id": "p1", "pack_status": "ready",
                        "missing_required": [], "selected_view_ids": ["v1"],
                        "selected_views": []}],
        "scene": None, "keyframe_slot": "narrative_keyframe", "input_fingerprint": "frozen-fp",
    }
    resolve_calls = {"n": 0}

    def resolve(**_k):
        resolve_calls["n"] += 1
        return {
            "episode_no": 1, "shot_id": "s",
            "characters": [{"name": "A", "look_revision_id": "p1", "pack_status": "ready",
                            "missing_required": [], "selected_view_ids": ["v9"],
                            "selected_views": []}],
            "scene": None, "keyframe_slot": "narrative_keyframe", "input_fingerprint": "new-fp",
        }

    async def must_not_complete(*_a, **_k):
        raise AssertionError("冻结且版本未变时不应再 complete_legacy")

    monkeypatch.setattr(mv, "resolve_shot_asset_dependencies", resolve)
    monkeypatch.setattr(mv, "complete_legacy_character_pack", must_not_complete)
    monkeypatch.setattr(mv, "complete_legacy_scene_pack", must_not_complete)
    monkeypatch.setattr(mv, "assert_manifest_allows_production", lambda _m: None)
    monkeypatch.setattr(mv, "library_anchor_assets_from_manifest", lambda _m: [])
    monkeypatch.setattr(mv, "keyframe_seed_paths", lambda _m: [])
    # 本测试只验证冻结依赖 manifest 的复用，不进入叙事关键帧生产合同。
    monkeypatch.setattr(mv, "narrative_keyframe_required", lambda: False)
    monkeypatch.setattr(mv, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(mv, "scene_multiview_enabled", lambda: True)

    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="x", confidence=1.0,
        referenceImagePlan=ReferenceImagePlan(totalCount=0, generateNewCount=0, types=[]),
    )
    meta = {
        "reference_manifest": frozen,
        "reference_manifest_frozen": True,
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
    }
    asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=shot, bible=bible, decision=decision, prev_shot=None, existing_meta=meta))

    assert resolve_calls["n"] == 1, "仅探测当前版本一次"
    assert meta["reference_manifest"]["input_fingerprint"] == "frozen-fp"
    assert meta["reference_manifest_frozen"] is True
