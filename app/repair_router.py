"""分镜集级 Repair Router：把结构化 Issue 映射为最小修复范围与策略。

不让 LLM 自由决定是否重做全片；按 PRD §9 从 L0→L5 逐级升级。
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.evaluations.issues import issue_code, issue_fingerprint
from app.harness.types import Issue, IssueSeverity

RepairLevel = Literal["L0", "L1", "L2", "L3", "L4", "L5"]
RepairStrategy = Literal[
    "normalize",
    "repair_current",
    "repair_window",
    "redo_suffix",
    "split_adjacent_shot",
    "replan_outline",
    "waiting_human",
    "waiting_retry",
    "waiting_authorization",
]

LEVEL_ORDER: list[RepairLevel] = ["L0", "L1", "L2", "L3", "L4", "L5"]

# Issue code → 首选层级
_PREFERRED_LEVEL: dict[str, RepairLevel] = {
    "SCHEMA_INVALID": "L1",
    "JSON_INVALID": "L1",
    "SPOKEN_CAPACITY_EXCEEDED": "L1",
    "SPOKEN_CONTRACT_CONFLICT": "L0",
    "SHOT_OUTLINE_COVERAGE": "L1",
    "STATE_CHAIN_INVALID": "L2",
    "KEY_LINE_MISSING": "L2",
    "SPINE_MISSING": "L3",
    "KEY_CONTENT_MISSING": "L2",
    "DROP_LIST_REINTRODUCED": "L1",
    "PLAN_EXHAUSTED_NOT_FINAL": "L4",
    "PROVIDER_UNAVAILABLE": "L5",
    "UPSTREAM_VERSION_CHANGED": "L5",
}

_SHOT_NO_RE = re.compile(r"(?:shot_no\s*=\s*|第\s*)(\d+)\s*镜?", re.I)


class RepairPlan(BaseModel):
    level: RepairLevel
    strategy: RepairStrategy
    invalidation_frontier: int
    issue_codes: list[str] = Field(default_factory=list)
    fingerprint: str = ""
    reason: str = ""
    pause_state: str | None = None  # WAITING_HUMAN / PAUSED_EXTERNAL / WAITING_AUTHORIZATION


def _extract_shot_nos(issues: list[Issue]) -> list[int]:
    found: list[int] = []
    for issue in issues:
        for text in (issue.message, issue.subject, str(issue.evidence.get("shot_no", ""))):
            for match in _SHOT_NO_RE.finditer(text or ""):
                found.append(int(match.group(1)))
            if text.isdigit():
                found.append(int(text))
    return found


def preferred_level_for_code(code: str) -> RepairLevel:
    return _PREFERRED_LEVEL.get(code, "L1")


def strategy_for_level(level: RepairLevel) -> RepairStrategy:
    return {
        "L0": "normalize",
        "L1": "repair_current",
        "L2": "repair_window",
        "L3": "redo_suffix",
        "L4": "replan_outline",
        "L5": "waiting_human",
    }[level]


def _capacity_needs_adjacent_split(issues: list[Issue]) -> bool:
    """容量超限且文案暗示不可单镜满足 → 优先相邻插镜，而不是立刻整集重规划。"""
    if not any(issue.code == "SPOKEN_CAPACITY_EXCEEDED" for issue in issues):
        return False
    return any(
        any(token in (issue.message or "") for token in ("拆", "必保留", "NEEDS_REPLAN", "不可满足", "容量"))
        for issue in issues
    )


def upgrade_level(level: RepairLevel) -> RepairLevel:
    idx = LEVEL_ORDER.index(level)
    return LEVEL_ORDER[min(idx + 1, len(LEVEL_ORDER) - 1)]


def compute_invalidation_frontier(
    issues: list[Issue],
    *,
    level: RepairLevel,
    validated_prefix_end: int,
    next_shot_no: int | None = None,
) -> int:
    """最早失效边界：只保留 frontier 之前仍通过的 validated prefix。"""
    shot_nos = _extract_shot_nos(issues)
    candidates = [n for n in shot_nos if n > 0]
    if next_shot_no and next_shot_no > 0:
        candidates.append(next_shot_no)
    if not candidates:
        frontier = max(1, validated_prefix_end) if validated_prefix_end else 1
    else:
        frontier = min(candidates)

    if level == "L0":
        return frontier
    if level == "L1":
        return frontier
    if level == "L2":
        return max(1, frontier - 1)
    if level in {"L3", "L4"}:
        return max(1, frontier)
    return 1


def route_issues(
    issues: list[Issue] | list[str],
    *,
    validated_prefix_end: int = 0,
    next_shot_no: int | None = None,
    issue_fingerprint_counts: dict[str, int] | None = None,
    current_level: RepairLevel | None = None,
) -> RepairPlan:
    """将 Issue 列表映射为 RepairPlan；相同 fingerprint 连续出现则升级层级。"""
    normalized: list[Issue] = []
    for item in issues:
        if isinstance(item, Issue):
            normalized.append(item)
        else:
            msg = str(item)
            normalized.append(Issue(
                code=issue_code(msg),
                severity=IssueSeverity.BLOCKER,
                subject="storyboard",
                message=msg,
                repairable=True,
            ))
    if not normalized:
        return RepairPlan(
            level="L0",
            strategy="normalize",
            invalidation_frontier=max(1, validated_prefix_end or 1),
            reason="empty_issues",
        )

    codes = [issue.code for issue in normalized]
    # 取最严重（最高）首选层级
    preferred = "L0"
    for code in codes:
        lvl = preferred_level_for_code(code)
        if LEVEL_ORDER.index(lvl) > LEVEL_ORDER.index(preferred):
            preferred = lvl

    # SPOKEN_CAPACITY：首轮优先相邻插镜（split_adjacent_shot）；反复 stalled 再升 L4 整集重规划。
    capacity_split = _capacity_needs_adjacent_split(normalized)

    if any(c == "PROVIDER_UNAVAILABLE" for c in codes):
        return RepairPlan(
            level="L5",
            strategy="waiting_retry",
            invalidation_frontier=max(1, validated_prefix_end or next_shot_no or 1),
            issue_codes=codes,
            fingerprint=issue_fingerprint(normalized),
            reason="provider_unavailable",
            pause_state="PAUSED_EXTERNAL",
        )
    if any(c == "UPSTREAM_VERSION_CHANGED" for c in codes):
        return RepairPlan(
            level="L5",
            strategy="waiting_authorization",
            invalidation_frontier=max(1, validated_prefix_end or 1),
            issue_codes=codes,
            fingerprint=issue_fingerprint(normalized),
            reason="upstream_version_changed",
            pause_state="WAITING_AUTHORIZATION",
        )

    level: RepairLevel = preferred
    if current_level and LEVEL_ORDER.index(current_level) > LEVEL_ORDER.index(level):
        level = current_level

    fp = issue_fingerprint(normalized)
    counts = dict(issue_fingerprint_counts or {})
    prior = counts.get(fp, 0)
    # 相同 fingerprint 连续 2 轮视为 stalled → 升级
    if prior >= 2:
        level = upgrade_level(level)
        if level == "L5":
            return RepairPlan(
                level="L5",
                strategy="waiting_human",
                invalidation_frontier=compute_invalidation_frontier(
                    normalized, level=level,
                    validated_prefix_end=validated_prefix_end,
                    next_shot_no=next_shot_no,
                ),
                issue_codes=codes,
                fingerprint=fp,
                reason="stalled_after_upgrade",
                pause_state="WAITING_HUMAN",
            )

    frontier = compute_invalidation_frontier(
        normalized,
        level=level,
        validated_prefix_end=validated_prefix_end,
        next_shot_no=next_shot_no,
    )
    # 容量不可满足：未升到 L4 前走相邻插镜；已升 L4 则整集重规划。
    if capacity_split and LEVEL_ORDER.index(level) < LEVEL_ORDER.index("L4"):
        return RepairPlan(
            level=level,
            strategy="split_adjacent_shot",
            invalidation_frontier=frontier,
            issue_codes=codes,
            fingerprint=fp,
            reason="route:split_adjacent_shot:capacity",
        )
    if capacity_split and level == "L4":
        return RepairPlan(
            level="L4",
            strategy="replan_outline",
            invalidation_frontier=frontier,
            issue_codes=codes,
            fingerprint=fp,
            reason="route:L4:replan_outline:capacity_exhausted",
        )
    return RepairPlan(
        level=level,
        strategy=strategy_for_level(level),
        invalidation_frontier=frontier,
        issue_codes=codes,
        fingerprint=fp,
        reason=f"route:{level}:{strategy_for_level(level)}",
    )


def bump_fingerprint_count(
    counts: dict[str, int], fingerprint: str
) -> dict[str, int]:
    updated = dict(counts)
    if fingerprint:
        updated[fingerprint] = updated.get(fingerprint, 0) + 1
    return updated
