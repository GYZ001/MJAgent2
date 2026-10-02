"""按段选参考图：wardrobe_matches_default 三个取值 × 本段造型照是否就绪，
覆盖人物造型照改造（2026-10-02）设计约定的全部组合。

2026-10-02 起 face_closeup（头像九宫格）不再作为视频参考图候选——Seedance 对
它判定真人隐私疑似拒收，详见 app.video_modes.character_look_selection 模块
文档；旧版覆盖 face_closeup 分支的用例已随之改写为覆盖 look_view 分支。
"""
from __future__ import annotations

from app.video_modes.character_look_selection import pick_character_reference_view


def _look_view(image_path: str = "/tmp/look.jpg", view_id: str = "view_look") -> dict:
    return {"id": view_id, "image_path": image_path, "input_fingerprint": "fp1"}


def test_yes_sends_front_full_and_locks_costume():
    result = pick_character_reference_view(
        wardrobe_matches_default="yes", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", look_view=_look_view(),
    )
    assert result == {
        "id": "portrait_1", "view_role": "front_full", "image_path": "/tmp/front.jpg",
        "input_fingerprint": "portrait_1", "costume_mode": None,
    }


def test_no_sends_look_view_when_ready():
    result = pick_character_reference_view(
        wardrobe_matches_default="no", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", look_view=_look_view(),
    )
    assert result == {
        "id": "view_look", "view_role": "look", "image_path": "/tmp/look.jpg",
        "input_fingerprint": "fp1", "costume_mode": None,
    }


def test_unsure_sends_look_view_when_ready():
    """缺省/拿不准一律按"非默认"保守处理——与显式 no 同一分支。"""
    result = pick_character_reference_view(
        wardrobe_matches_default="unsure", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", look_view=_look_view(),
    )
    assert result["view_role"] == "look"
    assert result["costume_mode"] is None


def test_missing_field_defaults_to_conservative_look_view():
    """旧数据/模型漏填（空字符串）同样落在保守分支，不是"yes"。"""
    result = pick_character_reference_view(
        wardrobe_matches_default="", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", look_view=_look_view(),
    )
    assert result["view_role"] == "look"


def test_no_without_look_view_falls_back_to_front_full_with_neutral_text():
    """造型照还没生成好：退回全身照，但文案仍切成"只锁长相"——好过维持现状的
    "硬锁服装"，同时是调用方判断要不要留可见提示（look_fallback_notice）的信号。"""
    result = pick_character_reference_view(
        wardrobe_matches_default="no", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", look_view=None,
    )
    assert result == {
        "id": "portrait_1", "view_role": "front_full", "image_path": "/tmp/front.jpg",
        "input_fingerprint": "portrait_1", "costume_mode": "neutral",
    }


def test_no_front_full_image_returns_none():
    """角色没有可用全身照：没有图可用，交回调用方按既有口径处理，不发明兜底。"""
    assert pick_character_reference_view(
        wardrobe_matches_default="yes", portrait_id=None,
        front_full_image_path="", look_view=_look_view(),
    ) is None


def test_look_view_without_image_path_is_ignored():
    """look_view 存在但没有落盘路径（防御性输入）不得被当成可用——仍退回全身照。"""
    result = pick_character_reference_view(
        wardrobe_matches_default="no", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg",
        look_view={"id": "v2", "image_path": ""},
    )
    assert result["view_role"] == "front_full"
    assert result["costume_mode"] == "neutral"
