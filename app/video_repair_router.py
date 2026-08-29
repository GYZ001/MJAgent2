"""视频 Repair Router：Issue → L0–L6 修复计划（纯函数，无副作用）。"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.harness.types import Issue, IssueSeverity

RepairLevel = Literal["L0", "L1", "L2", "L3", "L4", "L5", "L6"]
RepairStrategy = Literal[
    "requeue_no_charge",
    "retake_same_input",
    "retake_directed",
    "rebuild_reference",
    "degrade_chain",
    "rewrite_prompt",
    "amend_storyboard",
    "handoff_human",
    "auto_crop",
]

LEVEL_ORDER: list[RepairLevel] = ["L0", "L1", "L2", "L3", "L4", "L5", "L6"]

STALL_ROUNDS = 2
MAX_CHAIN_CASCADE_DEPTH = 3

class VideoRepairPlan(BaseModel):
    level: RepairLevel
    strategy: RepairStrategy
    issue_codes: list[str] = Field(default_factory=list)
    fingerprint: str = ""
    reason: str = ""
    pause_state: str | None = None  # WAITING_HUMAN / PAUSED_EXTERNAL / WAITING_AUTHORIZATION
    is_paid: bool = True
    cascade_shot_nos: list[int] = Field(default_factory=list)
    extra_negative: list[str] = Field(default_factory=list)
    critique: list[str] = Field(default_factory=list)
    degrade_chain: bool = False
    rebuild_reference: bool = False
    prompt_aggressive: bool = False
    amend_fields: dict[str, Any] = Field(default_factory=dict)


def upgrade_level(level: RepairLevel) -> RepairLevel:
    idx = LEVEL_ORDER.index(level)
    return LEVEL_ORDER[min(idx + 1, len(LEVEL_ORDER) - 1)]


def strategy_for_level(
    level: RepairLevel,
    *,
    issues: list[Issue] | None = None,
    chain_position: int = 0,
) -> RepairStrategy:
    issues = issues or []
    if level == "L0":
        return "requeue_no_charge"
    if level == "L1":
        return "retake_same_input"
    if level == "L2":
        return "retake_directed"
    if level == "L3":
        if any(
            (issue.evidence or {}).get("repair_mode") == "degrade_chain"
            for issue in issues
        ):
            return "degrade_chain"
        return "rebuild_reference"
    if level == "L4":
        return "rewrite_prompt"
    if level == "L5":
        return "amend_storyboard"
    return "handoff_human"


def bump_fingerprint_count(counts: dict[str, int], fingerprint: str) -> dict[str, int]:
    out = dict(counts)
    if fingerprint:
        out[fingerprint] = int(out.get(fingerprint, 0)) + 1
    return out


def _directed_patch(issues: list[Issue]) -> tuple[list[str], list[str]]:
    critiques = list(dict.fromkeys(
        str(issue.repair_hint).strip()
        for issue in issues
        if str(issue.repair_hint or "").strip()
    ))
    return [], critiques[:6]


def route(
    issues: list[Issue] | list[str],
    *,
    entry: Any | None = None,
    budget: dict[str, Any] | None = None,
    fingerprint_counts: dict[str, int] | None = None,
    current_level: RepairLevel | str | None = None,
    allow_storyboard_edit: bool = False,
    qa_history: list[float] | None = None,
    rebuilt_reference: bool = False,
    fatal_repeat_count: int = 0,
) -> VideoRepairPlan:
    """把 Issue 列表映射为确定性修复计划。"""
    normalized: list[Issue] = []
    for item in issues or []:
        if isinstance(item, Issue):
            normalized.append(item)
        elif isinstance(item, str):
            normalized.append(Issue(
                code=item,
                severity=IssueSeverity.BLOCKER,
                subject=getattr(entry, "shot_id", "") if entry else "",
                message=item,
            ))
        elif isinstance(item, dict):
            normalized.append(Issue.model_validate(item))

    contract_failures = [
        issue for issue in normalized
        if issue.category == "quality"
        and issue.severity == IssueSeverity.BLOCKER
    ]
    score_only = [
        issue for issue in normalized
        if issue.category == "quality"
        and issue.severity != IssueSeverity.BLOCKER
    ]
    normalized = [
        issue for issue in normalized
        if issue.category != "quality"
    ] + contract_failures
    if score_only and not normalized:
        codes = [issue.code for issue in score_only]
        return VideoRepairPlan(
            level="L0",
            strategy="handoff_human",
            issue_codes=codes,
            fingerprint=score_only[0].fingerprint,
            reason="QA 只评分，不进入视频修复路由",
            is_paid=False,
        )

    counts = dict(fingerprint_counts or {})
    chain_position = int(getattr(entry, "chain_position", 0) or 0) if entry else 0
    codes = [i.code for i in normalized]
    if not normalized:
        level: RepairLevel = "L1"
        if current_level and current_level in LEVEL_ORDER:
            level = max(level, current_level, key=LEVEL_ORDER.index)  # type: ignore[arg-type]
        return VideoRepairPlan(
            level=level,
            strategy=strategy_for_level(level, chain_position=chain_position),
            issue_codes=[],
            reason="无结构化 Issue，默认同输入重抽",
            is_paid=level != "L0",
        )

    non_repairable = [
        issue for issue in normalized
        if not issue.repairable
        and (issue.evidence or {}).get("pause_state")
    ]
    if non_repairable:
        codes = [issue.code for issue in non_repairable]
        pause_state = next(
            (
                str((issue.evidence or {}).get("pause_state"))
                for issue in non_repairable
                if (issue.evidence or {}).get("pause_state")
            ),
            "WAITING_HUMAN",
        )
        return VideoRepairPlan(
            level="L6",
            strategy="handoff_human",
            issue_codes=codes,
            fingerprint=non_repairable[0].fingerprint,
            reason="外部终态或不可自动修复问题，禁止付费重试",
            pause_state=pause_state,
            is_paid=False,
        )

    requested_levels = [
        str((issue.evidence or {}).get("recommended_level") or "L1")
        for issue in normalized
    ]
    requested_levels = [
        level for level in requested_levels if level in LEVEL_ORDER
    ] or ["L1"]
    preferred = max(requested_levels, key=LEVEL_ORDER.index)
    level = preferred
    if current_level and current_level in LEVEL_ORDER:
        # 修复成功后不降级：保留当前层级下限
        if LEVEL_ORDER.index(current_level) > LEVEL_ORDER.index(level):
            level = current_level  # type: ignore[assignment]

    primary_fp = normalized[0].fingerprint
    prior = int(counts.get(primary_fp, 0))
    if prior >= STALL_ROUNDS:
        level = upgrade_level(level)

    # 致命失败换过参考图后仍出现 → L6
    has_fatal = any(
        issue.severity == IssueSeverity.BLOCKER
        and bool((issue.evidence or {}).get("runtime_blocking"))
        for issue in normalized
    )
    if has_fatal and rebuilt_reference:
        level = "L6"
    if fatal_repeat_count >= 3:
        level = "L6"

    # L5 未授权 → WAITING_AUTHORIZATION
    if level == "L5" and not allow_storyboard_edit:
        return VideoRepairPlan(
            level="L5",
            strategy="amend_storyboard",
            issue_codes=codes,
            fingerprint=primary_fp,
            reason="需要微调分镜但未授权 allow_storyboard_edit",
            pause_state="WAITING_AUTHORIZATION",
            is_paid=False,
        )

    if level == "L6":
        return VideoRepairPlan(
            level="L6",
            strategy="handoff_human",
            issue_codes=codes,
            fingerprint=primary_fp,
            reason="修复层级耗尽或致命失败反复，转人工",
            pause_state="WAITING_HUMAN",
            is_paid=False,
        )

    strategy = strategy_for_level(level, issues=normalized, chain_position=chain_position)
    negatives, critiques = _directed_patch(normalized) if strategy == "retake_directed" else ([], [])

    return VideoRepairPlan(
        level=level,
        strategy=strategy,
        issue_codes=codes,
        fingerprint=primary_fp,
        reason=f"{level}/{strategy} for {', '.join(codes[:4])}",
        is_paid=strategy not in {"requeue_no_charge", "handoff_human", "auto_crop"},
        extra_negative=negatives,
        critique=critiques,
        degrade_chain=strategy == "degrade_chain",
        rebuild_reference=strategy == "rebuild_reference",
        prompt_aggressive=strategy == "rewrite_prompt",
    )


# 兼容别名
route_issues = route


def state_drift_significant(
    previous_state_out: str | None,
    observed_state_out: str | None,
) -> bool:
    """尾状态是否显著漂移（简单规范化比较）。"""
    a = re.sub(r"\s+", "", (previous_state_out or "").strip().lower())
    b = re.sub(r"\s+", "", (observed_state_out or "").strip().lower())
    if not a or not b:
        return False
    if a == b:
        return False
    # 字符级粗略相似度
    common = sum(1 for ch in set(a) if ch in b)
    union = max(1, len(set(a) | set(b)))
    return (common / union) < 0.55


def should_cascade(
    n_entry: Any,
    downstream_entry: Any,
    *,
    state_drift: bool = False,
) -> bool:
    """连续性链级联重做判定。"""
    if getattr(downstream_entry, "human_adopted", False):
        return False
    if getattr(downstream_entry, "grade", "C") == "A" and not state_drift:
        return False
    n_pos = int(getattr(n_entry, "chain_position", 0) or 0)
    d_pos = int(getattr(downstream_entry, "chain_position", 0) or 0)
    if d_pos - n_pos > MAX_CHAIN_CASCADE_DEPTH:
        return False
    return True
