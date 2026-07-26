import asyncio
import json

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
    assert plan.totalCount == 1 and plan.generateNewCount == 1
    assert plan.types == ["plot_key_frame"]
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


def test_build_reference_assets_collects_rejected_for_discard_gallery(monkeypatch, tmp_path) -> None:
    """质检未通过的关键帧进入 rejected_out；人物库图只作 QA 依据，不默认 selectedForSeedance。"""
    bible = _bible()
    shot = _shot(shot_no=4, narration="对白场景", dialogues=[{"speaker": "A", "line": "嗯", "emotion": "平静"}])

    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 1)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 2)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "quality_threshold", lambda: 0.8)
    monkeypatch.setattr(video_modes, "batch_qa_enabled", lambda: False)
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
    assert fed == []
    assert rejected and all(a.selectedForSeedance is False and a.path for a in rejected)


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


def test_reference_assets_publish_each_completed_image_without_waiting_for_slowest(monkeypatch, tmp_path) -> None:
    """快图完成后应先进入画廊，不能被同批慢图的生成/QA 阻塞。"""
    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "_portrait_seed_inputs", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "batch_prompt_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "batch_qa_enabled", lambda: False)
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
            totalCount=2,
            generateNewCount=2,
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

    # 多视角改造后画廊可含证据锚点；本测试只断言生成关键帧会渐进出现
    generated_snapshots = [ids for ids in snapshots if any(i.startswith("g") for i in ids)]
    assert generated_snapshots, "应至少发布一次进度快照"
    assert any("g2" in ids for ids in generated_snapshots)
    assert {a.id for a in result if a.id.startswith("g")} == {"g1", "g2"}


def test_build_reference_assets_fallback_keyframe_yields_to_clean_portrait(monkeypatch) -> None:
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
    monkeypatch.setattr(video_modes, "batch_qa_enabled", lambda: False)
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
        asset = ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                    path=f"/tmp/g{index}.jpg", qualityScore=score,
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

    # PRD：人物库图默认只作 keyframe_seed/qa_anchor，不直接喂 Seedance
    assert not any(a.source == "asset_library" and a.selectedForSeedance for a in assets)
    assert all(a.source != "seedream_generated" or not a.selectedForSeedance for a in assets)
    assert prompt_calls["n"] >= 1, "仍应尝试生成必需关键帧"
    suppressed = [a for a in rejected if a.source == "seedream_generated"]
    assert suppressed and all(not a.selectedForSeedance for a in suppressed)


def test_build_reference_assets_subfloor_fallback_not_fed(monkeypatch) -> None:
    """Change 1：生成图全不达标且最佳一版仍低于质量地板（quality_floor=0.4）时，一张都不喂模型，
    只靠定妆照/场景锚点撑住；所有尝试进废弃画廊（脏图当参考反而拖累成片）。"""
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
        return {"status": "scored", "overall": 0.5, "action_match": 0.5, "body_proportion": 0.5,
                "face_identity": 0.5, "outfit_match": 0.5, "hair_match": 0.5, "scene_match": 0.5,
                "hard_failures": [], "issues": [], "absolute_quality": 0.5}
    monkeypatch.setattr(mv, "review_keyframe_with_evidence", _fake_kf_review)
    monkeypatch.setattr(mv, "keyframe_gate_passed", lambda qa: False)
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "scene_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 1)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 2)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    monkeypatch.setattr(video_modes, "batch_qa_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index, content_override=None, seed_inputs=None,
                           **kwargs):
        score = 0.2 + 0.05 * (index % 3)  # 0.20/0.25/0.30：均低于地板 0.4
        asset = ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                    path=f"/tmp/g{index}.jpg", qualityScore=score,
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

    fed = [a for a in assets if a.selectedForSeedance]
    assert not any(a.source == "seedream_generated" for a in fed), "低于地板的生成图一律不喂"
    # 定妆照进入画廊作 QA 依据，但不作为 video_input
    assert any(a.source == "asset_library" for a in assets)
    assert not any(a.source == "asset_library" and a.selectedForSeedance for a in assets)
    gen_rejected = [a for a in rejected if a.source == "seedream_generated"]
    assert gen_rejected and all(not a.selectedForSeedance for a in gen_rejected), "失败关键帧进废弃画廊"


def test_generated_references_get_i2i_seeds(monkeypatch) -> None:
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
    monkeypatch.setattr(video_modes, "batch_qa_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "consistency_check_enabled", lambda: False)
    monkeypatch.setattr(video_modes, "_portrait_seed_inputs", lambda *a, **k: ["PORTRAIT_A"])
    _patch_multiview_production_ready(monkeypatch)

    seen: dict[str, list] = {}

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index,
                           content_override=None, seed_inputs=None, **kwargs):
        seen[ref_type] = list(seed_inputs or [])
        return ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                   path=f"/tmp/g{index}.jpg", qualityScore=0.9,
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
    """Phase 2：低一致性触发 i2i 重生；原图若综合分跌破门禁则进废弃（理由为分数不足，非 consistency_drift）。"""
    bible, shot = _bible(), _shot(shot_no=2)
    _consistency_settings(monkeypatch, retries=1)
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
        assert seed_inputs == ["PORTRAIT"], "重生必须以锚点 data URL 做 i2i 种子"
        assert extra_instruction and "costume" in extra_instruction, "重生提示词须带漂移修复说明"
        return ReferenceImageAsset(id="g_fixed", url="u2", type=ref_type, source="seedream_generated",
                                   path="/tmp/fixed.jpg", qualityScore=0.9,
                                   qa={"overall": 0.9, "absolute_quality": 0.9})

    monkeypatch.setattr(video_modes, "_generate_one_reference", fake_gen_one)

    rejected: list = []
    rej_details: list = []
    result = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[anchor, good, bad], shot=shot, bible=bible, project_id="p", episode_no=1,
        rejection_details=rej_details, rejected_out=rejected))
    result = video_modes._finalize_reference_selection(
        result, rejected_out=rejected, rejection_details=rej_details)

    ids = [a.id for a in result]
    assert "g_fixed" in ids, "重生版应进入候选"
    assert "g_good" in ids and "p1" in ids, "达标图与锚点应保留"
    # abs=0.9, cons=0.4 → 综合分约 0.706 < 0.8 → 原漂移图因分数不足废弃
    assert any(a.id == "g_bad" for a in rejected), "原低一致性图因综合分不足进废弃"
    assert all(d.get("reason") == "quality_below_threshold" for d in rej_details if d.get("reason"))
    assert not any(d.get("reason") == "consistency_drift" for d in rej_details)


def test_consistency_agent_drops_unfixable_reference(monkeypatch) -> None:
    """Phase 2：重生后仍低一致性 → 综合分跌破门禁才废弃；不再使用 consistency_drift_unfixable。"""
    bible, shot = _bible(), _shot(shot_no=2)
    _consistency_settings(monkeypatch, retries=1)
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
        return ReferenceImageAsset(id="g_bad_fixed", url="u2", type=ref_type, source="seedream_generated",
                                   path="/tmp/fixed.jpg", qualityScore=0.9,
                                   qa={"overall": 0.9, "absolute_quality": 0.9})

    monkeypatch.setattr(video_modes, "_generate_one_reference", fake_gen_one)

    rejected: list = []
    result = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[anchor, good, bad], shot=shot, bible=bible, project_id="p", episode_no=1,
        rejection_details=[], rejected_out=rejected))
    result = video_modes._finalize_reference_selection(result, rejected_out=rejected)

    ids = [a.id for a in result]
    assert "g_bad" not in ids and "g_bad_fixed" not in ids, "综合分不足的漂移图应被门禁淘汰"
    assert "g_good" in ids and "p1" in ids, "达标图与锚点保留"
    assert all(not a.selectedForSeedance for a in rejected), "废弃图不喂 Seedance"
    assert all(a.rejectReason == "quality_below_threshold" for a in rejected)


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

    kept = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[cand, anchor], shot=shot, bible=bible, project_id="p", episode_no=1))
    assert {a.id for a in kept} == {"g1", "p1"}
    assert (cand.qa or {}).get("consistency_check_failed") is True


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


def test_multiple_high_score_character_refs_all_selected() -> None:
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
    # 默认人物偏好上限 1：装箱只带一张含人物图，但 selected 不变
    assert len(packed) == 1
    assert all(r["selectedForSeedance"] for r in refs)


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


def test_build_reference_assets_blocks_incomplete_pack(monkeypatch) -> None:
    """不完整多视角包必须阻止关键帧生成，不得静默继续。"""
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

    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="x", confidence=1.0,
        referenceImagePlan=ReferenceImagePlan(totalCount=1, generateNewCount=1, types=["plot_key_frame"]),
    )
    with pytest.raises(hiagent.ProviderError, match="禁止关键帧"):
        asyncio.run(video_modes.build_reference_assets(
            conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
            shot=shot, bible=bible, decision=decision, prev_shot=None))


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
    monkeypatch.setattr(video_modes, "batch_qa_enabled", lambda: False)
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
    monkeypatch.setattr(mv, "character_multiview_enabled", lambda: True)
    monkeypatch.setattr(mv, "scene_multiview_enabled", lambda: True)

    decision = ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="x", confidence=1.0,
        referenceImagePlan=ReferenceImagePlan(totalCount=0, generateNewCount=0, types=[]),
    )
    meta = {"reference_manifest": frozen, "reference_manifest_frozen": True}
    asyncio.run(video_modes.build_reference_assets(
        conn=None, project_id="p", episode_no=1, episode_id="e", shot_id="s",
        shot=shot, bible=bible, decision=decision, prev_shot=None, existing_meta=meta))

    assert resolve_calls["n"] == 1, "仅探测当前版本一次"
    assert meta["reference_manifest"]["input_fingerprint"] == "frozen-fp"
    assert meta["reference_manifest_frozen"] is True
