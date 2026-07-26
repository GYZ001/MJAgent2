"""分镜集级 Repair Router：把结构化 Issue 映射为最小修复范围与策略。

PRD《剧本分镜一次生成与Agent局部自愈交付方案》：
删除 redo_suffix / replan_outline；L3/L4 改为 insert_shot / split_shot。
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.evaluations.issues import issue_code, issue_fingerprint
from app.harness.types import Issue, IssueSeverity

RepairLevel = Literal["L0", "L1", "L2", "L3", "L4", "L5"]
RepairStrategy = Literal[
    "normalize",
    "repair_current",
    "repair_window",
    "insert_shot",
    "split_shot",
    "split_adjacent_shot",
    "delete_shot",
    "move_shot",
    "waiting_human",
    "waiting_retry",
    "waiting_authorization",
]

LEVEL_ORDER: list[RepairLevel] = ["L0", "L1", "L2", "L3", "L4", "L5"]

# Issue code → 首选层级（不再映射到整版重做）
_PREFERRED_LEVEL: dict[str, RepairLevel] = {
    "SCHEMA_INVALID": "L1",
    "JSON_INVALID": "L1",
    "SPOKEN_CAPACITY_EXCEEDED": "L1",
    "SPOKEN_CONTRACT_CONFLICT": "L0",
    "SHOT_OUTLINE_COVERAGE": "L1",
    "STATE_CHAIN_INVALID": "L2",
    "KEY_LINE_MISSING": "L2",
    "SPINE_MISSING": "L3",  # 插入明确镜头，不删后缀
    "KEY_CONTENT_MISSING": "L2",
    "DROP_LIST_REINTRODUCED": "L1",
    "PLAN_EXHAUSTED_NOT_FINAL": "L3",  # 追加 final / 修最后窗口，不重规划大纲
    "PROVIDER_UNAVAILABLE": "L5",
    "UPSTREAM_VERSION_CHANGED": "L5",
}

_SHOT_NO_RE = re.compile(r"(?:shot_no\s*=\s*|第\s*)(\d+)\s*镜?", re.I)

# 旧策略别名 → 新策略（兼容 checkpoint / 前端文案）
_LEGACY_STRATEGY_MAP = {
    "redo_suffix": "repair_window",
    "replan_outline": "insert_shot",
}


class RepairPlan(BaseModel):
    level: RepairLevel
    strategy: RepairStrategy
    invalidation_frontier: int
    issue_codes: list[str] = Field(default_factory=list)
    fingerprint: str = ""
    reason: str = ""
    pause_state: str | None = None  # WAITING_HUMAN / PAUSED_EXTERNAL / WAITING_AUTHORIZATION
    touched_shot_nos: list[int] = Field(default_factory=list)


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
        "L3": "insert_shot",
        "L4": "split_shot",
        "L5": "waiting_human",
    }[level]


def normalize_strategy(strategy: str) -> RepairStrategy:
    """把遗留 redo_suffix / replan_outline 映射为局部策略。"""
    mapped = _LEGACY_STRATEGY_MAP.get(strategy, strategy)
    allowed = {
        "normalize", "repair_current", "repair_window", "insert_shot",
        "split_shot", "split_adjacent_shot", "delete_shot", "move_shot",
        "waiting_human", "waiting_retry", "waiting_authorization",
    }
    if mapped not in allowed:
        return "repair_window"
    return mapped  # type: ignore[return-value]


def _capacity_needs_adjacent_split(issues: list[Issue]) -> bool:
    """容量超限且文案暗示不可单镜满足 → 相邻插镜/拆镜，绝不整集重规划。"""
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
    """最早失效边界：只触及 frontier 附近窗口，禁止整后缀失效。"""
    shot_nos = _extract_shot_nos(issues)
    candidates = [n for n in shot_nos if n > 0]
    if next_shot_no and next_shot_no > 0:
        candidates.append(next_shot_no)
    if not candidates:
        frontier = max(1, validated_prefix_end) if validated_prefix_end else 1
    else:
        frontier = min(candidates)

    if level in {"L0", "L1"}:
        return frontier
    if level == "L2":
        return max(1, frontier - 1)
    # L3/L4：插入/拆分也不清空后缀；frontier 仅作定位
    if level in {"L3", "L4"}:
        return frontier
    return frontier


def route_issues(
    issues: list[Issue] | list[str],
    *,
    validated_prefix_end: int = 0,
    next_shot_no: int | None = None,
    issue_fingerprint_counts: dict[str, int] | None = None,
    current_level: RepairLevel | None = None,
) -> RepairPlan:
    """将 Issue 列表映射为局部 RepairPlan；相同 fingerprint 连续出现则升级层级，但不整版重做。"""
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
    preferred = "L0"
    for code in codes:
        lvl = preferred_level_for_code(code)
        if LEVEL_ORDER.index(lvl) > LEVEL_ORDER.index(preferred):
            preferred = lvl

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
    touched = sorted({n for n in _extract_shot_nos(normalized) if n > 0} or {frontier})

    # 容量不可满足：始终走拆镜/插镜，永不 replan_outline
    if capacity_split:
        strategy: RepairStrategy = "split_adjacent_shot" if level in {"L0", "L1", "L2"} else "split_shot"
        return RepairPlan(
            level=level,
            strategy=strategy,
            invalidation_frontier=frontier,
            issue_codes=codes,
            fingerprint=fp,
            reason=f"route:{strategy}:capacity",
            touched_shot_nos=touched,
        )

    strategy = strategy_for_level(level)
    # spine 缺失 → insert_shot；结局未收束 → insert_shot（追加 final）
    if "SPINE_MISSING" in codes or "PLAN_EXHAUSTED_NOT_FINAL" in codes:
        strategy = "insert_shot"
    return RepairPlan(
        level=level,
        strategy=strategy,
        invalidation_frontier=frontier,
        issue_codes=codes,
        fingerprint=fp,
        reason=f"route:{level}:{strategy}",
        touched_shot_nos=touched,
    )


def bump_fingerprint_count(
    counts: dict[str, int], fingerprint: str
) -> dict[str, int]:
    updated = dict(counts)
    if fingerprint:
        updated[fingerprint] = updated.get(fingerprint, 0) + 1
    return updated
