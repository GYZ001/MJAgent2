"""按段选参考图：wardrobe_matches_default 三个取值 × face_closeup 是否存在，
覆盖定妆照双视角改造（2026-10-01）设计文档第 5/10 节约定的全部组合。"""
from __future__ import annotations

from app.video_modes.character_look_selection import pick_character_reference_view


def _closeup_view(image_path: str = "/tmp/closeup.jpg", view_id: str = "view_closeup") -> dict:
    return {"id": view_id, "view_role": "face_closeup", "image_path": image_path, "input_fingerprint": "fp1"}


def test_yes_sends_front_full_and_locks_costume():
    result = pick_character_reference_view(
        wardrobe_matches_default="yes", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", ready_views=[_closeup_view()],
    )
    assert result == {
        "id": "portrait_1", "view_role": "front_full", "image_path": "/tmp/front.jpg",
        "input_fingerprint": "portrait_1", "costume_mode": None,
    }


def test_no_sends_closeup_when_available():
    result = pick_character_reference_view(
        wardrobe_matches_default="no", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", ready_views=[_closeup_view()],
    )
    assert result == {
        "id": "view_closeup", "view_role": "face_closeup", "image_path": "/tmp/closeup.jpg",
        "input_fingerprint": "fp1", "costume_mode": "neutral",
    }


def test_unsure_sends_closeup_when_available():
    """缺省/拿不准一律按"非默认"保守处理——与显式 no 同一分支。"""
    result = pick_character_reference_view(
        wardrobe_matches_default="unsure", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", ready_views=[_closeup_view()],
    )
    assert result["view_role"] == "face_closeup"
    assert result["costume_mode"] == "neutral"


def test_missing_field_defaults_to_conservative_closeup():
    """旧数据/模型漏填（空字符串）同样落在保守分支，不是"yes"。"""
    result = pick_character_reference_view(
        wardrobe_matches_default="", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", ready_views=[_closeup_view()],
    )
    assert result["view_role"] == "face_closeup"


def test_no_without_closeup_falls_back_to_front_full_with_neutral_text():
    """存量角色还没补出 face_closeup：退回全身照，但文案仍切成"只锁长相"
    ——好过维持现状的"硬锁服装"。"""
    result = pick_character_reference_view(
        wardrobe_matches_default="no", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg", ready_views=[],
    )
    assert result == {
        "id": "portrait_1", "view_role": "front_full", "image_path": "/tmp/front.jpg",
        "input_fingerprint": "portrait_1", "costume_mode": "neutral",
    }


def test_no_front_full_image_returns_none():
    """角色没有可用全身照：没有图可用，交回调用方按既有口径处理，不发明兜底。"""
    assert pick_character_reference_view(
        wardrobe_matches_default="yes", portrait_id=None,
        front_full_image_path="", ready_views=[_closeup_view()],
    ) is None


def test_ready_views_without_image_path_are_ignored():
    """face_closeup 行存在但没有落盘路径（例如状态行但文件缺失的防御性输入）
    不得被当成可用——仍退回全身照。"""
    result = pick_character_reference_view(
        wardrobe_matches_default="no", portrait_id="portrait_1",
        front_full_image_path="/tmp/front.jpg",
        ready_views=[{"id": "v2", "view_role": "face_closeup", "image_path": ""}],
    )
    assert result["view_role"] == "front_full"
    assert result["costume_mode"] == "neutral"
