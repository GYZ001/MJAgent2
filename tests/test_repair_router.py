"""Repair Router：局部策略回归（无 redo_suffix / replan_outline）。"""
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


def test_preferred_levels():
    assert preferred_level_for_code("SCHEMA_INVALID") == "L1"
    assert preferred_level_for_code("STATE_CHAIN_INVALID") == "L2"
    assert preferred_level_for_code("SPINE_MISSING") == "L3"
    assert preferred_level_for_code("PLAN_EXHAUSTED_NOT_FINAL") == "L3"


def test_route_spoken_capacity_never_replans_outline():
    """容量超限走拆镜/插镜；升到 L4 后仍禁止整集重规划。"""
    issues = [_issue("SPOKEN_CAPACITY_EXCEEDED", "第 9 镜必保留台词超过 10 秒容量，请拆镜")]
    first = route_issues(issues, validated_prefix_end=8, next_shot_no=9)
    assert first.level == "L1"
    assert first.strategy == "split_adjacent_shot"
    assert first.invalidation_frontier <= 9

    split = route_issues(
        issues,
        validated_prefix_end=8,
        next_shot_no=9,
        current_level="L4",
    )
    assert split.level == "L4"
    assert split.strategy == "split_shot"
    assert split.strategy != "replan_outline"


def test_route_action_capacity_to_adjacent_split():
    issues = [_issue(
        "ACTION_CAPACITY_EXCEEDED",
        "shot_no=8 含约 4 个顺序动作节拍，超过 5s 镜头容量上限 2",
        8,
    )]

    plan = route_issues(issues, validated_prefix_end=7, next_shot_no=8)

    assert plan.level == "L1"
    assert plan.strategy == "split_adjacent_shot"
    assert plan.invalidation_frontier == 8


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


def test_provider_pauses_external():
    plan = route_issues([
        _issue("PROVIDER_UNAVAILABLE", "provider timeout", 3),
    ], validated_prefix_end=2)
    assert plan.pause_state == "PAUSED_EXTERNAL"
    assert plan.strategy == "waiting_retry"


def test_spine_missing_inserts_not_redo_suffix():
    plan = route_issues([
        _issue("SPINE_MISSING", "must_keep spine 未覆盖 第 3 镜", 3),
    ], validated_prefix_end=2, next_shot_no=3)
    assert plan.strategy == "insert_shot"
    assert plan.strategy not in {"redo_suffix", "replan_outline"}
