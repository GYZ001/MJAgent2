"""只改转场这类成片阶段字段的单镜保存：不清付费视频、不触发人物称谓解析（2026-09-15 实测 7 段视频被清）。"""
from __future__ import annotations

from app.domain.storyboard_ops.mutation_primitives import RENDER_TIME_ONLY_EDIT_FIELDS, render_time_only_edit


def test_transition_only_edit_is_render_time_only() -> None:
    assert "transition" in RENDER_TIME_ONLY_EDIT_FIELDS
    assert render_time_only_edit({"transition"}) is True
    assert render_time_only_edit({"transition", "action_desc"}) is False
    assert render_time_only_edit(set()) is False
    assert render_time_only_edit(["dialogues"]) is False


def test_only_identity_bearing_fields_trigger_appellation_check() -> None:
    from app.domain.storyboard_ops.mutation_primitives import edit_touches_identities

    assert not edit_touches_identities({"scene_time"})
    assert not edit_touches_identities({"transition", "scene_name", "shot_size", "camera_move", "duration_s"})
    assert edit_touches_identities({"scene_time", "dialogues"})
    assert edit_touches_identities({"characters"}) and edit_touches_identities({"action_desc"})
