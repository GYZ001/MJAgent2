"""转场先定后写（真实故障：2026-09-30《顾念长安》EP1 第19段——字段是叠化，镜头1正文却按
硬切写，因为旧流程里模型写正文时只被告知了纯文本判据的猜测，字段在模型写完之后才用
resources.scenes 差异升级）。见 app.production.storyboard_transition_plan 模块 docstring。"""
from __future__ import annotations

import logging

from app.production.screenplay_markers import SAME_SCENE_TRANSITION, SCENE_CHANGE_TRANSITION
from app.production.storyboard_transition_plan import (
    finalize_transition_after_generation,
    resolve_transition_before_generation,
)


def _structure(transition: str, scene_change: bool = False) -> dict:
    return {"scene_change": scene_change, "transition_from_previous": transition, "required_beats": []}


def test_resolve_upgrades_same_scene_when_planned_scenes_differ() -> None:
    """文本判据判定同场，但映射台为本段登记的计划场景与上一段已生成场景不同：
    生成前就把转场升级成换场默认值，并同步 scene_change，供 structure_rules 两个
    字段一起换场，不再是一个换一个不换。"""
    structure = _structure(SAME_SCENE_TRANSITION, scene_change=False)
    resolved, text_transition = resolve_transition_before_generation(structure, {"scene_a"}, {"scene_b"})
    assert resolved["transition_from_previous"] == SCENE_CHANGE_TRANSITION
    assert resolved["scene_change"] is True
    assert text_transition == SAME_SCENE_TRANSITION  # 纯文本判据基准原样返回，供生成后核对漂移


def test_resolve_keeps_same_scene_when_planned_scenes_match() -> None:
    structure = _structure(SAME_SCENE_TRANSITION, scene_change=False)
    resolved, text_transition = resolve_transition_before_generation(structure, {"scene_a"}, {"scene_a"})
    assert resolved["transition_from_previous"] == SAME_SCENE_TRANSITION
    assert resolved["scene_change"] is False
    assert resolved is structure  # 没有升级时不必造一份新字典
    assert text_transition == SAME_SCENE_TRANSITION


def test_resolve_does_not_downgrade_explicit_or_structural_scene_change() -> None:
    """文本判据已经是显式标记/结构化换场（非 SAME_SCENE_TRANSITION）时原样返回，
    不被资源判据覆盖——与 transition_with_resource_bypass 的既有语义一致。"""
    structure = _structure("遮挡转场", scene_change=True)
    resolved, text_transition = resolve_transition_before_generation(structure, {"scene_a"}, {"scene_b"})
    assert resolved["transition_from_previous"] == "遮挡转场"
    assert resolved["scene_change"] is True
    assert text_transition == "遮挡转场"


def test_resolve_keeps_same_scene_when_planned_scenes_are_superset_of_previous() -> None:
    """计划场景是上一段场景的真超集（本段开头仍在上一场景，段尾钩子把目的地场景也登记进了
    本段 asset_manifest——见 screenplay_markers.scene_changed_by_resource_scenes 的超集用例）：
    不应升级为换场，镜头1 仍延续上一段同场站位；钩子切场由 required_beats 单独告诉模型。"""
    structure = _structure(SAME_SCENE_TRANSITION, scene_change=False)
    resolved, text_transition = resolve_transition_before_generation(structure, {"scene_a"}, {"scene_a", "scene_b"})
    assert resolved["transition_from_previous"] == SAME_SCENE_TRANSITION
    assert resolved["scene_change"] is False
    assert resolved is structure
    assert text_transition == SAME_SCENE_TRANSITION


def test_resolve_missing_planned_scene_data_does_not_upgrade() -> None:
    """计划场景缺失（映射台没为本段登记任何场景）是数据缺口不是"没变化"，
    transition_with_resource_bypass 本身在任一侧为空时就不升级——这里只确认接线没有
    绕过这条既有判据。"""
    structure = _structure(SAME_SCENE_TRANSITION, scene_change=False)
    resolved, _ = resolve_transition_before_generation(structure, {"scene_a"}, set())
    assert resolved["transition_from_previous"] == SAME_SCENE_TRANSITION
    assert resolved["scene_change"] is False


def test_finalize_no_drift_when_actual_scenes_match_plan() -> None:
    """生成后实际场景与计划一致：重算结果等于告诉模型的值，不告警。"""
    resolved = finalize_transition_after_generation(
        SAME_SCENE_TRANSITION, {"scene_a"}, {"scene_b"},
        told_transition=SCENE_CHANGE_TRANSITION, segment_no=19,
    )
    assert resolved == SCENE_CHANGE_TRANSITION


def test_finalize_drift_when_actual_scenes_diverge_from_plan_and_warns(caplog) -> None:
    """生成前按计划场景升级成了叠化并告诉模型，但模型实际登记的 resources.scenes
    和上一段相同（计划落空）：重算应该退回硬切，以重算结果为准且记可见告警。"""
    with caplog.at_level(logging.WARNING, logger="app.production.storyboard_transition_plan"):
        resolved = finalize_transition_after_generation(
            SAME_SCENE_TRANSITION, {"scene_a"}, {"scene_a"},
            told_transition=SCENE_CHANGE_TRANSITION, segment_no=19,
        )
    assert resolved == SAME_SCENE_TRANSITION
    assert any("STORYBOARD_TRANSITION_DRIFT" in r.message for r in caplog.records)
    assert any("第19段" in r.message for r in caplog.records)


def test_finalize_matches_told_transition_silently_when_no_bypass_ever_triggers() -> None:
    """全程都不满足升级条件（文本判据本就是显式换场）：生成前生成后都原样返回，
    不会因为 resources.scenes 差异而误触发告警。"""
    resolved = finalize_transition_after_generation(
        "遮挡转场", {"scene_a"}, {"scene_b"}, told_transition="遮挡转场", segment_no=1,
    )
    assert resolved == "遮挡转场"
