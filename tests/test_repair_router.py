"""Repair Router：语义候选选择回归（无 code→strategy 白名单）。"""
from __future__ import annotations

from app.harness.types import Issue, IssueSeverity
from app.repair_router import (
    bump_fingerprint_count,
    compute_invalidation_frontier,
    preferred_level_for_code,
    route_issues,
    upgrade_level,
)


def _issue(code: str, message: str, shot_no: int = 9) -> Issue:
    return Issue(
        code=code,
        severity=IssueSeverity.BLOCKER,
        subject=f"shot:{shot_no}",
        message=message,
        evidence={"shot_no": shot_no, "path": f"shot_no={shot_no}", "rule_id": code},
        repairable=True,
    )


def test_issue_code_does_not_prescribe_a_repair_level():
    levels = {
        preferred_level_for_code(code)
        for code in (
            "SCHEMA_INVALID",
            "STATE_CHAIN_INVALID",
            "SPINE_MISSING",
            "PLAN_EXHAUSTED_NOT_FINAL",
            "SEMANTIC_GAP_OTHER",
        )
    }

    assert levels == {"L1"}


def test_route_spoken_capacity_never_replans_outline():
    """同一容量问题保留多种候选，由语义诊断选择，绝不整集重规划。"""
    issues = [_issue("SPOKEN_CAPACITY_EXCEEDED", "第 9 镜必保留台词超过 10 秒容量，请拆镜")]
    first = route_issues(issues, validated_prefix_end=8, next_shot_no=9)
    assert first.level == "L2"
    assert first.strategy == "repair_window"
    assert first.needs_semantic_selection is True
    assert {candidate.strategy for candidate in first.candidates} >= {
        "repair_current",
        "repair_window",
        "insert_shot",
        "split_adjacent_shot",
    }
    assert first.invalidation_frontier <= 9

    split = route_issues(
        issues,
        validated_prefix_end=8,
        next_shot_no=9,
        current_level="L4",
        semantic_diagnosis={
            "scope": "adjacent_window",
            "selected_strategy": "split_adjacent_shot",
            "reason": "单镜无法同时提供台词和独立处理时间",
        },
    )
    assert split.level == "L4"
    assert split.strategy == "split_adjacent_shot"
    assert split.needs_semantic_selection is False
    assert all(
        candidate.strategy not in {"redo_suffix", "replan_outline"}
        for candidate in split.candidates
    )


def test_route_action_capacity_to_adjacent_split():
    issues = [_issue(
        "ACTION_CAPACITY_EXCEEDED",
        "shot_no=8 含约 4 个顺序动作节拍，超过 5s 镜头容量上限 2",
        8,
    )]

    plan = route_issues(
        issues,
        validated_prefix_end=7,
        next_shot_no=8,
        semantic_diagnosis={
            "scope": "adjacent_window",
            "selected_strategy": "split_adjacent_shot",
        },
    )

    assert plan.level == "L2"
    assert plan.strategy == "split_adjacent_shot"
    assert plan.invalidation_frontier == 7


def test_route_uses_structured_scene_shot_window() -> None:
    issue = Issue(
        code="STORYBOARD_DIRECTION_CONTRACT_INVALID",
        severity=IssueSeverity.BLOCKER,
        subject="storyboard_scene:SC09",
        message="场景 SC09 缺少空间可读镜头",
        evidence={
            "scene_id": "SC09",
            "shot_nos": [37, 38, 39],
            "rule_id": "storyboard_direction_contract",
        },
        repairable=True,
    )

    plan = route_issues(
        [issue],
        validated_prefix_end=76,
        semantic_diagnosis={
            "scope": "current_shot",
            "selected_strategy": "repair_current",
            "execution_verified": True,
        },
    )

    assert plan.invalidation_frontier == 37
    assert plan.touched_shot_nos == [37, 38, 39]
    assert 75 not in plan.touched_shot_nos


def test_route_compiled_prompt_overflow_to_reported_shot() -> None:
    message = (
        "[ACTION_CAPACITY_EXCEEDED] Prompt 编译失败："
        "镜头 1 必填提示词段落总长 2006 超过上限 1500；"
        "说明镜头任务过载，请回到分镜阶段拆分"
    )

    plan = route_issues([message], validated_prefix_end=17)

    assert plan.issue_codes == ["ACTION_CAPACITY_EXCEEDED"]
    assert plan.strategy == "repair_window"
    assert plan.needs_semantic_selection is True
    assert "split_adjacent_shot" in {
        candidate.strategy for candidate in plan.candidates
    }
    assert plan.invalidation_frontier == 1


def test_route_state_chain_window():
    plan = route_issues([
        _issue("STATE_CHAIN_INVALID", "shot_no=8 与 shot_no=9 状态链不承接", 8),
    ], validated_prefix_end=9)
    assert plan.level == "L2"
    assert plan.strategy == "repair_window"
    assert plan.invalidation_frontier <= 8


def test_fingerprint_stall_upgrades():
    issues = [_issue("SCHEMA_INVALID", "字段 shot.action_desc：类型错误", 4)]
    first = route_issues(issues, validated_prefix_end=3, next_shot_no=4)
    counts = bump_fingerprint_count({}, first.fingerprint)
    counts = bump_fingerprint_count(counts, first.fingerprint)
    stalled = route_issues(
        issues,
        validated_prefix_end=3,
        next_shot_no=4,
        issue_fingerprint_counts=counts,
        current_level=first.level,
    )
    assert stalled.level != "L1" or stalled.pause_state == "WAITING_HUMAN"
    assert upgrade_level("L1") == "L2"


def test_persisted_fingerprint_count_escalates_without_current_level() -> None:
    issues = [_issue("BUSINESS_RULE_FAILED", "第 10 镜 camera_move 不在合法枚举中", 10)]
    first = route_issues(issues, validated_prefix_end=9, next_shot_no=10)

    stalled = route_issues(
        issues,
        validated_prefix_end=9,
        next_shot_no=10,
        issue_fingerprint_counts={first.fingerprint: 8},
    )

    assert stalled.level == "L5"
    assert stalled.strategy == "waiting_human"
    assert stalled.pause_state == "WAITING_HUMAN"
    assert stalled.reason == "stalled_after_upgrade"


def test_frontier_from_message():
    issues = [_issue("KEY_LINE_MISSING", "分镜丢失了剧本标记的 1 条主线台词：薰儿相信", 5)]
    frontier = compute_invalidation_frontier(
        issues, level="L2", validated_prefix_end=10, next_shot_no=11,
    )
    assert frontier <= 5


def test_provider_code_alone_does_not_pause_external():
    plan = route_issues([
        _issue("PROVIDER_UNAVAILABLE", "provider timeout", 3),
    ], validated_prefix_end=2)
    assert plan.pause_state is None
    assert plan.strategy == "repair_window"
    assert plan.needs_semantic_selection is True


def test_structured_operational_state_pauses_without_story_code_mapping():
    issue = _issue("SEMANTIC_GAP_OTHER", "外部模型当前不可用", 3)
    issue.evidence["operational_kind"] = "provider_unavailable"

    plan = route_issues([issue], validated_prefix_end=2)

    assert plan.pause_state == "PAUSED_EXTERNAL"
    assert plan.strategy == "waiting_retry"


def test_spine_missing_requires_semantic_selection_before_insert():
    issues = [
        _issue("SPINE_MISSING", "must_keep spine 未覆盖 第 3 镜", 3),
    ]

    undecided = route_issues(issues, validated_prefix_end=2, next_shot_no=3)
    selected = route_issues(
        issues,
        validated_prefix_end=2,
        next_shot_no=3,
        semantic_diagnosis={
            "scope": "structure",
            "selected_strategy": "insert_shot",
            "reason": "删除测试证明现有窗口无法承载缺失事件",
        },
    )

    assert undecided.strategy == "repair_window"
    assert undecided.needs_semantic_selection is True
    assert "insert_shot" in {candidate.strategy for candidate in undecided.candidates}
    assert selected.level == "L3"
    assert selected.strategy == "insert_shot"
    assert selected.needs_semantic_selection is False
    assert all(
        candidate.strategy not in {"redo_suffix", "replan_outline"}
        for candidate in selected.candidates
    )


def test_same_unknown_issue_can_choose_different_repairs_from_semantic_evidence():
    issues = [_issue("SEMANTIC_GAP_OTHER", "开放语义缺口", 6)]

    local = route_issues(
        issues,
        validated_prefix_end=5,
        semantic_diagnosis={
            "scope": "current_shot",
            "selected_strategy": "repair_current",
            "candidate_scores": {"repair_current": 0.8, "insert_shot": 0.1},
        },
    )
    bridge = route_issues(
        issues,
        validated_prefix_end=5,
        semantic_diagnosis={
            "scope": "structure",
            "selected_strategy": "insert_shot",
            "candidate_scores": {"repair_current": 0.1, "insert_shot": 0.9},
        },
    )

    assert local.issue_codes == bridge.issue_codes == ["SEMANTIC_GAP_OTHER"]
    assert local.strategy == "repair_current"
    assert bridge.strategy == "insert_shot"
    assert {candidate.strategy for candidate in local.candidates} == {
        candidate.strategy for candidate in bridge.candidates
    }


def test_unknown_strategy_without_verified_operations_fails_closed() -> None:
    plan = route_issues(
        [_issue("SEMANTIC_GAP_OTHER", "开放关系需要新修复意图", 6)],
        validated_prefix_end=5,
        semantic_diagnosis={
            "scope": "structure",
            "selected_strategy": "repair_open_relation",
            "candidate_assessments": [{
                "strategy": "repair_open_relation",
                "expected_narrative_gain": 0.9,
                "outline_operations": [{
                    "op": "unavailable_runtime_capability",
                    "target": {"shot_id": "SH-6"},
                }],
            }],
            "execution_verified": False,
        },
    )

    assert plan.strategy == "waiting_human"
    assert plan.pause_state == "WAITING_HUMAN"
    assert plan.reason == "semantic_strategy_not_executable"
    assert plan.selected_candidate_id is None
    assert plan.needs_semantic_selection is True
