import asyncio
import json

import pytest

from app import video_modes, worker
from app.schemas import Bible, Character, Shot, World
from app.video_modes import (
    FIRST_LAST_FRAME_MODE,
    REFERENCE_IMAGE_MODE,
    ReferenceImageAsset,
    ShotVideoModeDecision,
    ShotVideoModeSelector,
    build_seedance_image_inputs,
    decision_to_dict,
)


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


def test_rules_choose_reference_for_ordinary_dialogue() -> None:
    shot = _shot(action_desc="A站在室内与同伴对话，只有轻微回头。")
    decision = ShotVideoModeSelector().select_by_rules(shot)

    assert decision.mode == REFERENCE_IMAGE_MODE
    assert decision.referenceImagePlan.totalCount == 4
    assert decision.needGenerateNewReferences is True


def test_rules_choose_first_last_for_strong_action() -> None:
    shot = _shot(action_desc="A快速转身释放法术，与敌人打斗，必须保证结尾落点。")
    decision = ShotVideoModeSelector().select_by_rules(shot)

    assert decision.mode == FIRST_LAST_FRAME_MODE
    assert decision.referenceImagePlan.totalCount == 0


def test_continuity_shot_uses_first_last_frame_chaining() -> None:
    """连贯镜头改走首尾帧模式：用上一镜尾图作为本镜 first_frame 做逐帧接续。
    参考图模式与 first_frame 互斥，会丢掉这个确切接续导致剪辑点跳变，故连贯镜不再走参考图。"""
    shot = _shot(shot_no=2, continuity_from_prev=True, scene_setting="室内")
    prev = {"id": "shot_prev", "scene_setting": "室内", "action_desc": "A站在窗边。"}
    decision = ShotVideoModeSelector().select_by_rules(shot, prev_shot=prev)

    assert decision.mode == FIRST_LAST_FRAME_MODE
    assert decision.referenceImagePlan.totalCount == 0


@pytest.mark.parametrize(
    "meta",
    [
        {
            "mode": REFERENCE_IMAGE_MODE,
            "first_frame_path": "/tmp/first.jpg",
            "reference_images": [{"url": "data:image/jpeg;base64,abc", "selectedForSeedance": True}],
        },
        {
            "mode": FIRST_LAST_FRAME_MODE,
            "reference_images": [{"url": "data:image/jpeg;base64,abc", "selectedForSeedance": True}],
        },
    ],
)
def test_seedance_inputs_are_mutually_exclusive(meta: dict) -> None:
    with pytest.raises(Exception):
        build_seedance_image_inputs(meta)


def test_reference_mode_builds_reference_image_roles() -> None:
    inputs = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [
            {"url": "data:image/jpeg;base64,abc", "selectedForSeedance": True, "type": "character"},
            {"url": "data:image/jpeg;base64,def", "selectedForSeedance": False, "type": "scene"},
        ],
    })

    assert inputs == [("data:image/jpeg;base64,abc", "reference_image")]


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

    reference_decision = decision_to_dict(ShotVideoModeSelector().select_by_rules(_shot(action_desc="A轻声说话。")))
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
