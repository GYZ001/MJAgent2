import asyncio
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
from tests.conftest import patch_portraits_everywhere, patch_worker_everywhere
from tests.conftest import patch_video_modes_everywhere


def _fake_settings(monkeypatch, **overrides):
    """让 video_modes.get_setting 读自一个内存字典，避免依赖真实 DB 设置。"""
    patch_video_modes_everywhere(monkeypatch, "get_setting", lambda k, *a, **kw: overrides.get(k))


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
    patch_video_modes_everywhere(monkeypatch, "character_reference_assets", lambda *a, **k: [])
    patch_video_modes_everywhere(monkeypatch, "scene_reference_assets", lambda *a, **k: [])
    patch_video_modes_everywhere(monkeypatch, "_portrait_seed_inputs", lambda *a, **k: [])
    patch_video_modes_everywhere(monkeypatch, "min_generated_references", lambda: 1)
    patch_video_modes_everywhere(monkeypatch, "reference_prompt_async", lambda: False)
    patch_video_modes_everywhere(monkeypatch, "batch_prompt_enabled", lambda: False)
    patch_video_modes_everywhere(monkeypatch, "consistency_check_enabled", lambda: False)
    # These tests exercise the legacy/master slot's best-of-three lifecycle in
    # isolation.  The production default now expands a shot to the free slots
    # in Seedance's nine-image budget, which is a separate contract.
    patch_video_modes_everywhere(monkeypatch, "max_reference_images", lambda: 1)


def _passing_reference_qa() -> dict:
    return {
        "status": "scored", "overall": 0.95, "absolute_quality": 0.95,
        "action_match": 0.95, "body_proportion": 0.95,
        "face_identity": 0.95, "outfit_match": 0.95, "hair_match": 0.95,
        "scene_match": 0.95, "identity_contract_passed": True,
        "hard_failures": [], "issues": [],
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
    assert decision.referenceImagePlan.totalCount == 0


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
    patch_video_modes_everywhere(monkeypatch, "get_setting", lambda *a, **k: None)

    shot = _shot(action_desc="A站在室内与同伴对话。", dialogues=[{"speaker": "A", "line": "你好", "emotion": "平静"}])
    decision = asyncio.run(ShotVideoModeSelector().select(shot, _bible()))

    assert decision.mode == REFERENCE_IMAGE_MODE
    assert decision.llmUsed is False
    plan = decision.referenceImagePlan
    assert plan.totalCount == 0 and plan.generateNewCount == 0
    assert plan.types == []
    assert plan.prompts == []
    # 决策可往返序列化（入队持久化 → 生成期复用）
    assert dict_to_decision(decision_to_dict(decision)).referenceImagePlan.prompts == plan.prompts


def test_reference_mode_builds_reference_image_roles() -> None:
    inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "reference_images": [
            {"url": "data:image/jpeg;base64,abc", "selectedForSeedance": True, "type": "character", "source": "asset_library"},
            {"url": "data:image/jpeg;base64,def", "selectedForSeedance": False, "type": "scene", "source": "asset_library"},
        ],
    })

    assert inputs == [("data:image/jpeg;base64,abc", "reference_image")]


def test_reference_mode_excludes_deleted_reference_images() -> None:
    """用户在素材画廊里废弃（deleted）的参考图即便仍标 selectedForSeedance，也不喂给模型。"""
    inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "reference_images": [
            {"url": "data:image/jpeg;base64,keep", "selectedForSeedance": True, "type": "character", "source": "asset_library"},
            {"url": "data:image/jpeg;base64,gone", "selectedForSeedance": True, "deleted": True, "type": "scene", "source": "asset_library"},
        ],
    })

    assert inputs == [("data:image/jpeg;base64,keep", "reference_image")]


def test_reference_mode_rejects_generated_keyframe_even_with_video_purpose() -> None:
    """历史生成关键帧即使仍带视频用途，也不得进入新的供应商请求。"""
    with pytest.raises(hiagent.ProviderError, match="人物谱与场景库"):
        build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "reference_images": [
            {
                "url": "data:image/jpeg;base64,keep",
                "selectedForSeedance": True,
                "type": "plot_key_frame",
                "source": "seedream_generated",
                "purposes": ["video_input", "qa_anchor"],
            },
            {
                "url": "data:image/jpeg;base64,rejected",
                "selectedForSeedance": False,
                "type": "plot_key_frame",
                "source": "seedream_generated",
                "purposes": ["video_input", "qa_anchor"],
                "rejectReason": "quality_below_threshold",
            },
        ],
        })


def test_reference_mode_rejects_gallery_contaminated_by_generated_keyframes() -> None:
    """旧画廊只要选中了生成关键帧，就必须整体失效并重新解析图库。"""
    with pytest.raises(hiagent.ProviderError, match="人物谱与场景库"):
        build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "reference_images": [
            {
                "url": "data:image/jpeg;base64,low",
                "type": "plot_key_frame",
                "source": "seedream_generated",
                "selectedForSeedance": True,
                "qualityScore": 0.41,
            },
            {
                "url": "data:image/jpeg;base64,best",
                "type": "plot_key_frame",
                "source": "seedream_generated",
                "selectedForSeedance": True,
                "qualityScore": 0.93,
            },
            {
                "url": "data:image/jpeg;base64,mid",
                "type": "plot_key_frame",
                "source": "seedream_generated",
                "selectedForSeedance": True,
                "qualityScore": 0.67,
            },
        ],
        })


def test_reference_mode_rejects_historical_timeline_keyframes() -> None:
    with pytest.raises(hiagent.ProviderError, match="人物谱与场景库"):
        build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "keyframe_sequence": {
            "keyframe_plan": {"duration_s": 7, "count": 1},
            "beats": [{"slot_key": "narrative_keyframe"}],
        },
        "reference_images": [
            {
                "url": "data:image/jpeg;base64,auxiliary",
                "type": "plot_key_frame",
                "source": "seedream_generated",
                "slot_key": "narrative_keyframe_01",
                "keyframe_index": 1,
                "keyframe_time_ratio": 0.0,
                "selectedForSeedance": True,
                "qualityScore": 0.99,
            },
            {
                "url": "data:image/jpeg;base64,master",
                "type": "plot_key_frame",
                "source": "seedream_generated",
                "slot_key": "narrative_keyframe",
                "keyframe_index": 2,
                "keyframe_time_ratio": 0.64,
                "selectedForSeedance": True,
                "qualityScore": 0.8,
            },
        ],
        })


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


def test_reference_pack_prioritizes_timeline_before_scene_character_then_props(monkeypatch) -> None:
    patch_video_modes_everywhere(monkeypatch, "max_character_reference_images", lambda: 1)
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
        "beat-1", "beat-5", "scene-main", "character-a", "prop-overflow",
    ]
    assert {ref["id"] for ref in packed if ref["type"] == "plot_key_frame"} == {
        "beat-1", "beat-5",
    }


def test_reference_prompt_numbering_uses_exact_packed_order(monkeypatch) -> None:
    patch_video_modes_everywhere(monkeypatch, "max_reference_images", lambda: 9)
    patch_video_modes_everywhere(monkeypatch, "max_character_reference_images", lambda: 1)
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
    assert "图片1：A的关键帧参考" in note and "目标画面：opening target" in note
    assert "图片2：A的关键帧参考" in note and "目标画面：closing target" in note
    assert "图片3：场景参考，只用来锁定环境外观" in note
    assert "图片4：角色A的人物参考，只用来锁定长相与服装" in note
    assert "进度约0%" in note and "第1/2拍" in note
    assert "进度约100%" in note and "第2/2拍" in note
    assert "每个具名角色在画面里只出现一次" in note
    assert "[SUBJECT DEFINITIONS | HIGHEST PRIORITY]" not in note


def test_reference_prompt_notes_preserve_trailing_technical_suffix() -> None:
    prompt = "[FORMAT]\n电影化单镜头。 --ratio 9:16 --dur 5"
    refs = [{
        "id": "character",
        "url": "data:image/jpeg;base64,YQ==",
        "type": "character",
        "source": "asset_library",
        "selectedForSeedance": True,
        "entity_name": "A",
        "relatedCharacterIds": ["A"],
    }]

    result = video_modes.append_reference_prompt_notes_from_dicts(prompt, refs)

    assert result.endswith("--ratio 9:16 --dur 5")
    assert result.index("图片1") < result.rindex("--ratio 9:16 --dur 5")
    assert result.count("--ratio 9:16") == 1
    assert result.count("--dur 5") == 1


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
        "scene_setting": "日, 甲家广场",
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


def _runtime_library_asset(asset_id: str = "character-a") -> ReferenceImageAsset:
    return ReferenceImageAsset(
        id=asset_id,
        url="data:image/jpeg;base64,bGlicmFyeQ==",
        type="character",
        source="asset_library",
        entity_type="character",
        entity_name="A",
        selectedForSeedance=True,
        purposes=["video_input", "qa_anchor"],
        required=True,
    )


def _mark_runtime_library_policy(meta: dict) -> None:
    meta["reference_input_policy_version"] = video_modes.REFERENCE_INPUT_POLICY_VERSION


def test_runtime_reference_mode_uses_stored_decision(monkeypatch) -> None:
    """生成期复用入队时定好的参考图决策，不再跑一次运行期 LLM 选择（省调用、避免模式翻转）。
    既然存的是参考图决策且能拿到合格参考图，就直接以参考图模式生成，无需任何回退。"""

    def fail_select(*a, **k):
        raise AssertionError("生成期不应再调用 LLM 模式选择")

    async def fake_build_reference_assets(**kwargs):
        assets = [_runtime_library_asset("r1")]
        kwargs["on_progress"](assets, [])
        meta = kwargs.get("existing_meta")
        if isinstance(meta, dict):
            _mark_runtime_library_policy(meta)
        return assets

    # 运行期一旦调用 LLM 选择即视为回归（应已被移除）
    monkeypatch.setattr(ShotVideoModeSelector, "select", fail_select)
    writes: list[dict] = []
    patch_worker_everywhere(monkeypatch, "_set_version", lambda *a, **k: writes.append(k))
    patch_video_modes_everywhere(monkeypatch, "build_reference_assets", fake_build_reference_assets)

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


def test_runtime_auto_repairs_historical_generated_gallery(monkeypatch) -> None:
    """历史生成图污染画廊时，执行器必须重建为图库资产。"""
    build_calls: list[dict] = []

    async def fake_build_reference_assets(**kwargs):
        meta = kwargs["existing_meta"]
        build_calls.append(json.loads(json.dumps(meta)))
        if len(build_calls) == 1:
            meta.update({
                "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
            })
            return [
                ReferenceImageAsset(
                    id="stale-keyframe", url="data:image/jpeg;base64,c3RhbGU=",
                    type="plot_key_frame", source="seedream_generated",
                    selectedForSeedance=True, purposes=["video_input", "qa_anchor"],
                    slot_key="narrative_keyframe", required=True,
                ),
            ]
        _mark_runtime_library_policy(meta)
        return [_runtime_library_asset("repaired-library-anchor")]

    writes: list[dict] = []
    patch_worker_everywhere(monkeypatch, "_set_version", lambda *a, **k: writes.append(k))
    patch_video_modes_everywhere(monkeypatch, "build_reference_assets", fake_build_reference_assets)
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
    assert out_meta["reference_images"][0]["id"] == "repaired-library-anchor"
    assert out_meta["keyframe_file_repair_count"] == 1
    assert writes


def test_runtime_requires_repair_when_library_has_no_usable_images(monkeypatch) -> None:
    """人物谱和场景库都没有可用图片时，不得纯文本提交视频。"""

    async def fake_build_reference_assets(**kwargs):
        _mark_runtime_library_policy(kwargs["existing_meta"])
        return []

    patch_worker_everywhere(monkeypatch, "_set_version", lambda *a, **k: None)
    patch_video_modes_everywhere(monkeypatch, "build_reference_assets", fake_build_reference_assets)
    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "after_shot_id": None,
    }

    with pytest.raises(
        worker.VideoInputRepairRequired,
        match="参考图模式 2 次",
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


def test_runtime_falls_back_to_text_only_when_candidate_pool_is_genuinely_empty(monkeypatch) -> None:
    """群演/一次性人物没有定妆照、镜头没有场景名——候选池本来就是空的，是
    设计使然，不是漏抽：不得卡人工，应退化为纯文本继续出片，metadata 如实
    标注未使用参考图（PRD：REFERENCE_IMAGE_REPAIR_REQUIRED 误伤空池场景）。
    """
    import app.multiview as mv

    monkeypatch.setattr(mv, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(mv, "scene_multiview_enabled", lambda: True)

    async def fake_build_reference_assets(**kwargs):
        meta = kwargs["existing_meta"]
        _mark_runtime_library_policy(meta)
        meta["reference_manifest"] = {
            "characters": [{
                "name": "entity:extra1", "asset_required": False,
                "look_revision_id": None, "pack_status": None,
                "selected_view_ids": [], "missing_required": [],
            }],
            "scene": None,
        }
        return []

    stage_calls: list[tuple] = []
    writes: list[dict] = []
    patch_worker_everywhere(monkeypatch, "_set_version", lambda *a, **k: writes.append(k))
    patch_video_modes_everywhere(monkeypatch, "build_reference_assets", fake_build_reference_assets)
    monkeypatch.setattr(
        "app.media_pipeline.stage_state.set_pipeline_stage",
        lambda *a, **k: stage_calls.append((a, k)),
    )

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "after_shot_id": None,
    }

    out_meta, out_prompt = asyncio.run(worker._prepare_reference_mode_inputs(
        conn,
        {"id": "j1", "project_id": "p1", "episode_id": "e1", "shot_id": "s1"},
        {"id": "v1"},
        _shot_row(),
        {"episode_no": 1},
        meta,
        "PROMPT --dur 5",
    ))

    assert out_meta["reference_images"] == []
    assert out_meta["reference_generation_complete"] is True
    assert out_meta["reference_mode_text_only_fallback"] is True
    assert out_meta["reference_mode_text_only_reason"] == "empty_candidate_pool"
    # 关键防回归：这个字段若被误置 True，会被 dispatch.py 的 static_waiting
    # 判据误读成「静态图已备、只等尾帧」，把刚设好的 STAGE_VIDEO_READY 打回等待态。
    assert out_meta["reference_static_ready"] is False
    assert out_prompt.startswith("PROMPT")
    from app.media_pipeline import stages as media_stages

    assert any(call_args[1] == media_stages.STAGE_VIDEO_READY for call_args, _kw in stage_calls)
    assert writes


def test_runtime_still_requires_repair_when_generated_images_were_all_rejected(monkeypatch) -> None:
    """空池放行只认「从未要求过资产」；一旦真的生成过候选、又被判不合格
    （rejection_details 非空），必须维持原有的人工修复拦截，不能被空池
    放行顺带一起放走。"""
    import app.multiview as mv

    monkeypatch.setattr(mv, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(mv, "scene_multiview_enabled", lambda: True)

    async def fake_build_reference_assets(**kwargs):
        meta = kwargs["existing_meta"]
        _mark_runtime_library_policy(meta)
        meta["reference_manifest"] = {"characters": [], "scene": None}
        kwargs["rejection_details"].append({
            "reason": "quality_reject", "asset": "candidate-1",
        })
        return []

    patch_worker_everywhere(monkeypatch, "_set_version", lambda *a, **k: None)
    patch_video_modes_everywhere(monkeypatch, "build_reference_assets", fake_build_reference_assets)
    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "after_shot_id": None,
    }

    with pytest.raises(worker.VideoInputRepairRequired, match="参考图模式 2 次"):
        asyncio.run(worker._prepare_reference_mode_inputs(
            conn,
            {"id": "j1", "project_id": "p1", "episode_id": "e1", "shot_id": "s1"},
            {"id": "v1"},
            _shot_row(),
            {"episode_no": 1},
            meta,
            "PROMPT",
        ))

    assert meta.get("reference_mode_text_only_fallback") is None
    assert meta["reference_group_gate_passed"] is False


def test_runtime_submits_existing_character_and_scene_library_assets(monkeypatch) -> None:
    """图库资产齐全时直接进入视频就绪，不再生成剧情关键帧。"""

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
        meta.update({
            "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
            "narrative_keyframe_missing": False,
            "keyframe_sequence": {"beats": [], "beat_count": 0},
        })
        kwargs["on_progress"](assets, [])
        return assets

    writes: list[dict] = []
    patch_worker_everywhere(monkeypatch, "_set_version", lambda *a, **k: writes.append(k))
    patch_video_modes_everywhere(monkeypatch, "build_reference_assets", fake_build_reference_assets)
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
        _mark_runtime_library_policy(kwargs["existing_meta"])
        return [_runtime_library_asset("fresh")]

    patch_worker_everywhere(monkeypatch, "_set_version", lambda *a, **k: None)
    patch_video_modes_everywhere(monkeypatch, "build_reference_assets", fake_build_reference_assets)
    patch_portraits_everywhere(monkeypatch, "bible_for_episode", lambda _p, bible, _ep: bible)
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
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "reference_generation_complete": True,
        "reference_gallery_edited": True,
        "reference_gallery_contract_override": True,
        "reference_images": [{
            "id": "old", "url": "data:image/jpeg;base64,old", "type": "character",
            "entity_type": "character", "source": "asset_library",
            "selectedForSeedance": True, "dependency_manifest": {"revision": "old"},
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


def test_complete_historical_generated_gallery_is_invalidated_by_library_policy(monkeypatch) -> None:
    captured: dict = {}

    async def fake_build_reference_assets(**kwargs):
        captured["meta_at_rebuild"] = json.loads(json.dumps(kwargs["existing_meta"]))
        _mark_runtime_library_policy(kwargs["existing_meta"])
        return [_runtime_library_asset("fresh")]

    patch_worker_everywhere(monkeypatch, "_set_version", lambda *a, **k: None)
    patch_video_modes_everywhere(monkeypatch, "build_reference_assets", fake_build_reference_assets)
    patch_portraits_everywhere(monkeypatch, "bible_for_episode", lambda _p, bible, _ep: bible)
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
    assert rebuild_meta["stale_reference_reason"] == "reference_input_policy_or_file_invalid"


def test_static_ready_checkpoint_missing_library_file_cannot_skip_rebuild(monkeypatch) -> None:
    captured: dict = {}

    async def fake_build_reference_assets(**kwargs):
        captured["meta_at_rebuild"] = json.loads(json.dumps(kwargs["existing_meta"]))
        _mark_runtime_library_policy(kwargs["existing_meta"])
        return [_runtime_library_asset("fresh")]

    patch_worker_everywhere(monkeypatch, "_set_version", lambda *a, **k: None)
    patch_video_modes_everywhere(monkeypatch, "build_reference_assets", fake_build_reference_assets)
    patch_portraits_everywhere(monkeypatch, "bible_for_episode", lambda _p, bible, _ep: bible)
    monkeypatch.setattr("app.media_pipeline.stage_state.set_pipeline_stage", lambda *a, **k: None)

    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "mode_decision": decision_to_dict(video_modes.default_reference_decision()),
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "reference_static_ready": True,
        "reference_generation_complete": False,
        "reference_images": [{
            "id": "missing-library", "path": "/missing/library.jpg",
            "type": "character", "entity_type": "character", "source": "asset_library",
            "selectedForSeedance": True,
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
    assert rebuild_meta["stale_reference_reason"] == "library_reference_checkpoint_invalid"


def test_previous_tail_path_and_dependency_are_version_specific(monkeypatch, tmp_path) -> None:
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")

    class TailConn:
        def execute(self, _sql, _params=()):
            return _FakeCursor({"video_path": str(video_path)})

    def fake_extract(_source, dest):
        dest.write_bytes(b"tail-frame")
        return True

    patch_video_modes_everywhere(monkeypatch, "_extract_last_frame", fake_extract)
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


def test_seedance_provider_rejects_character_truth_anchor_mixed_with_keyframe() -> None:
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
    for ref in refs[:2]:
        ref["source"] = "asset_library"
    refs[2]["source"] = "seedream_generated"
    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "reference_images": refs,
    }

    with pytest.raises(hiagent.ProviderError, match="人物谱与场景库"):
        video_modes.build_seedance_image_inputs(meta)


def test_seedance_provider_pack_dedupes_repeated_progress_records() -> None:
    repeated = {
        "id": "character-a",
        "url": "data:image/jpeg;base64,YQ==",
        "path": "/tmp/character-a.jpg",
        "type": "character",
        "entity_name": "A",
        "relatedCharacterIds": ["A"],
        "selectedForSeedance": True,
        "purposes": ["video_input"],
    }
    refs = [
        repeated,
        {**repeated, "id": "duplicate-1"},
        {**repeated, "id": "duplicate-2"},
    ]

    packed = video_modes.pack_reference_images_for_seedance(refs)

    assert [ref["id"] for ref in packed] == ["character-a"]


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
    patch_video_modes_everywhere(monkeypatch, "max_character_reference_images", lambda: 1)
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
    patch_video_modes_everywhere(monkeypatch, "max_character_reference_images", lambda: 1)
    patch_video_modes_everywhere(monkeypatch, "max_reference_images", lambda: 2)
    refs = [
        {"id": "a", "url": "data:image/jpeg;base64,aaa", "selectedForSeedance": True,
         "type": "character", "source": "asset_library", "qualityScore": 0.99},
        {"id": "b", "url": "data:image/jpeg;base64,bbb", "selectedForSeedance": False,
         "type": "plot_key_frame", "source": "seedream_generated", "qualityScore": 0.95},
        {"id": "s", "url": "data:image/jpeg;base64,sss", "selectedForSeedance": True,
         "type": "scene", "source": "asset_library", "qualityScore": 0.9},
    ]
    inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "reference_images": refs,
    })
    # 人物上限 1 + 场景 → 最多 2 张；分数最高人物 + 场景
    assert len(inputs) == 2
    assert [r["selectedForSeedance"] for r in refs] == [True, False, True]


