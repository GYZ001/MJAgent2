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
        "duration_s": 10,
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


def test_selector_forced_reference_mode_from_config(monkeypatch) -> None:
    """监制房把 video_generation_default_mode 强制为参考图模式时，select() 直接返回该模式（不调 LLM），
    并给出默认参考图计划。"""
    _fake_settings(monkeypatch,
                   video_generation_enable_reference_image_mode="true",
                   video_generation_default_mode=REFERENCE_IMAGE_MODE)

    async def fail_chat(*a, **k):
        raise AssertionError("强制模式不应调用 LLM 选择")

    monkeypatch.setattr(hiagent, "chat", fail_chat)
    decision = asyncio.run(ShotVideoModeSelector().select(_shot(), _bible()))

    assert decision.mode == REFERENCE_IMAGE_MODE
    assert decision.defaulted is True and decision.llmUsed is False
    assert decision.referenceImagePlan.totalCount > 0


def test_selector_disabled_still_returns_reference(monkeypatch) -> None:
    """视频生成已固定参考图模式，即使旧配置关闭参考图也不回退首尾帧。"""
    _fake_settings(monkeypatch, video_generation_enable_reference_image_mode="false")
    decision = asyncio.run(ShotVideoModeSelector().select(_shot(), _bible()))

    assert decision.mode == REFERENCE_IMAGE_MODE
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
    assert plan.totalCount == 4 and plan.generateNewCount == 4
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


def test_build_reference_assets_collects_rejected_for_discard_gallery(monkeypatch) -> None:
    """质检未通过、未被选用的参考图收集进 rejected_out（带图片），供废弃照片画廊展示；
    选用列表里仍只有过审/兜底图，且 rejected_out 里的图 selectedForSeedance=False。"""
    bible = _bible()
    shot = _shot(shot_no=4, narration="对白场景", dialogues=[{"speaker": "A", "line": "嗯", "emotion": "平静"}])

    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 1)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 2)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index, content_override=None, seed_inputs=None):
        score = 0.5 + 0.1 * (index % 3)  # 100→0.6, 101→0.7, 102→0.5：均低于阈值 0.75
        asset = ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                    path=f"/tmp/g{index}.jpg", qualityScore=score, qa={"overall": score, "issues": []})
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

    assert len(assets) == 1 and assets[0].qualityScore == 0.7  # 兜底保留最佳一版
    assert assets[0].selectedForSeedance is True
    assert len(rejected) == 2  # 另两次尝试进废弃画廊
    assert all(a.selectedForSeedance is False and a.path for a in rejected)


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


def _shot_row(**kwargs) -> dict:
    row = {
        "shot_no": 1,
        "duration_s": 10,
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
        return [ReferenceImageAsset(
            id="r1", url="data:image/jpeg;base64,abc", type="character",
            source="seedream_generated", selectedForSeedance=True,
        )]

    # 运行期一旦调用 LLM 选择即视为回归（应已被移除）
    monkeypatch.setattr(ShotVideoModeSelector, "select", fail_select)
    monkeypatch.setattr(worker, "_first_keyframe_for_video", lambda conn, shot, after: (None, "head_keyframe", None))
    monkeypatch.setattr(worker, "_approved_keyframe", lambda conn, shot, kind: None)
    monkeypatch.setattr(worker, "_set_version", lambda *a, **k: None)
    monkeypatch.setattr(video_modes, "build_reference_assets", fake_build_reference_assets)

    reference_decision = decision_to_dict(ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE, reason="对白镜，保持角色与场景一致", confidence=0.9,
        needGenerateNewReferences=True,
        referenceImagePlan=ReferenceImagePlan(totalCount=2, reusePreviousSceneCount=0, generateNewCount=2),
    ))
    conn = _FakeConn({"bible_json": _bible().model_dump_json()})
    job = {"project_id": "p1", "episode_id": "e1", "shot_id": "s1"}
    version = {"id": "v1"}
    shot = _shot_row()
    ep = {"episode_no": 1}
    meta = {"mode": REFERENCE_IMAGE_MODE, "mode_decision": reference_decision, "after_shot_id": None}

    out_meta, _ = asyncio.run(
        worker._prepare_reference_mode_inputs(conn, job, version, shot, ep, meta, "PROMPT"))

    assert out_meta["mode"] == REFERENCE_IMAGE_MODE
    assert out_meta.get("reference_images")
    assert not out_meta.get("fallback_reason")


def test_build_reference_assets_fallback_keyframe_yields_to_clean_portrait(monkeypatch) -> None:
    """Change 2：当本镜唯一的生成关键帧只是「兜底」（QA 低于阈值、地板以上）时，含人物名额优先留给
    干净定妆照（QA 满分），兜底关键帧被抑制进废弃画廊——脏兜底图压过满分定妆照得不偿失。
    生成仍会发生（逐图异步写提示词），只是兜底关键帧最终不喂模型。"""
    bible = _bible()
    shot = _shot(shot_no=3, narration="次日清晨，新闻和昨晚补的细节吻合",
                 dialogues=[{"speaker": "A", "line": "这不可能", "emotion": "惊恐"}])

    # 只有一张定妆照可用；无可复用历史帧。
    monkeypatch.setattr(video_modes, "character_reference_assets",
                        lambda b, names, *, limit, project_id=None, episode_no=None: ([ReferenceImageAsset(
                            id="c1", url="u", type="character", source="asset_library",
                            path="/tmp/a.jpg", relatedCharacterIds=["A"], qualityScore=1.0)] if limit > 0 else []))
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 1)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 2)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: True)

    prompt_calls = {"n": 0}

    async def fake_write_prompt(shot, bible, ref_type, *, intent=None):
        prompt_calls["n"] += 1
        return f"detailed english prompt for {ref_type} #{prompt_calls['n']}"

    monkeypatch.setattr(video_modes, "write_reference_prompt", fake_write_prompt)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index, content_override=None, seed_inputs=None):
        assert content_override, "每张图必须带逐图异步生成的提示词"
        score = 0.5 + 0.1 * (index % 3)  # 0.5/0.6/0.7：均低于阈值 0.75，但高于地板 0.4
        asset = ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                    path=f"/tmp/g{index}.jpg", qualityScore=score, qa={"overall": score, "issues": []})
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
    assert [a.source for a in fed] == ["asset_library"], "兜底关键帧应让位给干净定妆照"
    assert all(a.qualityScore == 1.0 for a in fed)
    assert prompt_calls["n"] == 1, "生成仍发生（逐图异步写提示词，本例 1 张）"
    # 兜底关键帧被抑制进废弃画廊、不喂模型
    suppressed = [a for a in rejected if a.source == "seedream_generated"]
    assert suppressed and all(not a.selectedForSeedance for a in suppressed)
    assert any(a.rejectReason == "duplicate_character_suppressed" for a in suppressed)


def test_build_reference_assets_subfloor_fallback_not_fed(monkeypatch) -> None:
    """Change 1：生成图全不达标且最佳一版仍低于质量地板（quality_floor=0.4）时，一张都不喂模型，
    只靠定妆照/场景锚点撑住；所有尝试进废弃画廊（脏图当参考反而拖累成片）。"""
    bible = _bible()
    shot = _shot(shot_no=5, narration="夜里独白",
                 dialogues=[{"speaker": "A", "line": "为什么", "emotion": "悲伤"}])

    monkeypatch.setattr(video_modes, "character_reference_assets",
                        lambda b, names, *, limit, project_id=None, episode_no=None: ([ReferenceImageAsset(
                            id="c1", url="u", type="character", source="asset_library",
                            path="/tmp/a.jpg", relatedCharacterIds=["A"], qualityScore=1.0)] if limit > 0 else []))
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 1)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 2)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index, content_override=None, seed_inputs=None):
        score = 0.2 + 0.05 * (index % 3)  # 0.20/0.25/0.30：均低于地板 0.4
        asset = ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                    path=f"/tmp/g{index}.jpg", qualityScore=score, qa={"overall": score, "issues": []})
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
    assert [a.source for a in fed] == ["asset_library"], "低于地板的兜底图不喂，只留定妆照锚点"
    assert not any(a.source == "seedream_generated" for a in fed), "低于地板的生成图一律不喂"
    gen_rejected = [a for a in rejected if a.source == "seedream_generated"]
    assert len(gen_rejected) == 3 and all(not a.selectedForSeedance for a in gen_rejected), "全部尝试进废弃画廊"


def test_generated_references_get_i2i_seeds(monkeypatch) -> None:
    """根因修复：新生成的参考图必须带 i2i 种子。定妆照（锁身份/服饰）只喂给含人物的图
    （character / plot_key_frame），纯场景图（scene）不注入人物定妆照，避免把角色塞进环境图；
    姿态/动作仍由文字提示词决定（见 _SEED_USAGE_NOTE）。"""
    bible = _bible()
    shot = _shot(shot_no=2)

    monkeypatch.setattr(video_modes, "character_reference_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "reusable_previous_assets", lambda *a, **k: [])
    monkeypatch.setattr(video_modes, "min_generated_references", lambda: 0)
    monkeypatch.setattr(video_modes, "reference_gen_retries", lambda: 0)
    monkeypatch.setattr(video_modes, "reference_prompt_async", lambda: False)
    # 隔离定妆照取数（不碰 DB/磁盘）：直接给一个已知的种子 data URL。
    monkeypatch.setattr(video_modes, "_portrait_seed_inputs", lambda *a, **k: ["PORTRAIT_A"])

    seen: dict[str, list] = {}

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index,
                           content_override=None, seed_inputs=None):
        seen[ref_type] = list(seed_inputs or [])
        return ReferenceImageAsset(id=f"g{index}", url="u", type=ref_type, source="seedream_generated",
                                   path=f"/tmp/g{index}.jpg", qualityScore=0.9, qa={"overall": 0.9, "issues": []})

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
    """Phase 2：相对一致性检查点名漂移的生成图，从锚点 i2i 重生；重生达标后替换原图、原图进废弃画廊。"""
    bible, shot = _bible(), _shot(shot_no=2)
    _consistency_settings(monkeypatch, retries=1)

    anchor = ReferenceImageAsset(id="p1", url="PORTRAIT", type="character", source="asset_library",
                                 path="/tmp/p1.jpg", qualityScore=1.0)
    good = ReferenceImageAsset(id="g_good", url="u", type="plot_key_frame", source="seedream_generated",
                               path="/tmp/good.jpg", qualityScore=0.9, selectedForSeedance=True)
    bad = ReferenceImageAsset(id="g_bad", url="u", type="plot_key_frame", source="seedream_generated",
                              path="/tmp/bad.jpg", qualityScore=0.9, selectedForSeedance=True)

    async def fake_review(*, candidates, anchors, shot, bible):
        # 任何 id 含 "bad" 判漂移；重生版 id 不含 "bad" → 达标。
        return {"candidates": [{"asset_id": c.id, "consistency": 0.4 if "bad" in c.id else 0.95,
                                "drift": ["costume", "hair"] if "bad" in c.id else [], "issues": []}
                               for c in candidates], "overall": 0.7}

    monkeypatch.setattr(video_modes, "review_reference_consistency", fake_review)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index,
                           content_override=None, seed_inputs=None, extra_instruction=None):
        assert seed_inputs == ["PORTRAIT"], "重生必须以锚点 data URL 做 i2i 种子"
        assert extra_instruction and "costume" in extra_instruction, "重生提示词须带漂移修复说明"
        return ReferenceImageAsset(id="g_fixed", url="u2", type=ref_type, source="seedream_generated",
                                   path="/tmp/fixed.jpg", qualityScore=0.9)

    monkeypatch.setattr(video_modes, "_generate_one_reference", fake_gen_one)

    rejected: list = []
    rej_details: list = []
    result = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[anchor, good, bad], shot=shot, bible=bible, project_id="p", episode_no=1,
        rejection_details=rej_details, rejected_out=rejected))

    ids = [a.id for a in result]
    assert "g_bad" not in ids and "g_fixed" in ids, "漂移图应被重生版替换"
    assert "g_good" in ids and "p1" in ids, "达标图与锚点应保留"
    assert any(a.id == "g_bad" for a in rejected), "原漂移图进废弃画廊"
    assert any(d.get("reason") == "consistency_drift" for d in rej_details)


def test_consistency_agent_drops_unfixable_reference(monkeypatch) -> None:
    """Phase 2：重生后仍漂移的生成图从喂给 Seedance 的集合里剔除（进废弃画廊），锚点与达标图保留。"""
    bible, shot = _bible(), _shot(shot_no=2)
    _consistency_settings(monkeypatch, retries=1)

    anchor = ReferenceImageAsset(id="p1", url="PORTRAIT", type="character", source="asset_library",
                                 path="/tmp/p1.jpg", qualityScore=1.0)
    good = ReferenceImageAsset(id="g_good", url="u", type="plot_key_frame", source="seedream_generated",
                               path="/tmp/good.jpg", qualityScore=0.9, selectedForSeedance=True)
    bad = ReferenceImageAsset(id="g_bad", url="u", type="plot_key_frame", source="seedream_generated",
                              path="/tmp/bad.jpg", qualityScore=0.9, selectedForSeedance=True)

    async def fake_review(*, candidates, anchors, shot, bible):
        return {"candidates": [{"asset_id": c.id, "consistency": 0.3 if "bad" in c.id else 0.95,
                                "drift": ["style"] if "bad" in c.id else [], "issues": []}
                               for c in candidates], "overall": 0.5}

    monkeypatch.setattr(video_modes, "review_reference_consistency", fake_review)

    async def fake_gen_one(*, project_id, episode_no, shot, bible, ref_type, index,
                           content_override=None, seed_inputs=None, extra_instruction=None):
        # 重生版 id 仍含 "bad" → 一致性检查仍判漂移。
        return ReferenceImageAsset(id="g_bad_fixed", url="u2", type=ref_type, source="seedream_generated",
                                   path="/tmp/fixed.jpg", qualityScore=0.9)

    monkeypatch.setattr(video_modes, "_generate_one_reference", fake_gen_one)

    rejected: list = []
    result = asyncio.run(video_modes._enforce_reference_consistency(
        selected=[anchor, good, bad], shot=shot, bible=bible, project_id="p", episode_no=1,
        rejection_details=[], rejected_out=rejected))

    ids = [a.id for a in result]
    assert "g_bad" not in ids and "g_bad_fixed" not in ids, "不可修复的漂移图应被剔除"
    assert "g_good" in ids and "p1" in ids, "达标图与锚点保留"
    assert all(not a.selectedForSeedance for a in rejected), "废弃图不喂 Seedance"


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
