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

_PREFERRED_LEVEL: dict[str, RepairLevel] = {
    "VIDEO_PROVIDER_TRANSIENT": "L0",
    "VIDEO_DOWNLOAD_FAILED": "L0",
    "VIDEO_PROBE_UNAVAILABLE": "L0",
    "VIDEO_PROVIDER_TIMEOUT": "L1",
    "VIDEO_FILE_INVALID": "L1",
    "VIDEO_DURATION_CONTRACT": "L1",
    "VIDEO_REFERENCE_UNAVAILABLE": "L3",
    "VIDEO_CHAIN_ANCHOR_BLOCKED": "L3",
    "VIDEO_PROVIDER_SAFETY": "L4",
    "VIDEO_PROVIDER_COPYRIGHT": "L4",
    "VIDEO_PREFLIGHT_BLOCKED": "L5",
    "VIDEO_HARNESS_DISABLED": "L6",
    "VIDEO_PROVIDER_UNAVAILABLE": "L6",
    "VIDEO_BUDGET_EXHAUSTED": "L6",
    "VIDEO_WALL_CLOCK_EXCEEDED": "L6",
    "VIDEO_STORYBOARD_CHANGED": "L6",
}


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


def preferred_level_for_code(code: str) -> RepairLevel:
    return _PREFERRED_LEVEL.get(code, "L1")


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
    codes = {i.code for i in issues}
    if level == "L0":
        return "requeue_no_charge"
    if level == "L1":
        return "retake_same_input"
    if level == "L2":
        return "retake_directed"
    if level == "L3":
        if "VIDEO_CHAIN_ANCHOR_BLOCKED" in codes:
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
    from app.continuity import retry_patch_for_failure

    negatives: list[str] = []
    critiques: list[str] = []
    for issue in issues:
        rule = str((issue.evidence or {}).get("rule_id") or "")
        if not rule:
            # map issue code back to classify failure type
            reverse = {
                "VIDEO_QA_CHARACTER_DUPLICATE": "character_duplicate",
                "VIDEO_QA_TEXT_ARTIFACT": "text_error",
                "VIDEO_QA_STATE_MISMATCH": "state_mismatch",
                "VIDEO_QA_STORY_REPEAT": "story_repeat",
                "VIDEO_QA_FUTURE_LEAK": "future_leak",
                "VIDEO_QA_WRONG_DIALOGUE": "wrong_dialogue",
                "VIDEO_QA_NEEDS_CROP": "needs_crop",
                "VIDEO_QA_WRONG_IDENTITY": "wrong_identity",
                "VIDEO_QA_WRONG_OUTFIT": "wrong_outfit",
                "VIDEO_QA_SUBJECT_OCCLUSION": "subject_occlusion",
                "VIDEO_QA_ACTION_MISSING": "action_missing",
                "VIDEO_QA_PROP_IDENTITY": "prop_identity_mismatch",
                "VIDEO_QA_PROP_STATE": "prop_state_mismatch",
                "VIDEO_QA_OBJECT_COUNT": "object_count_mismatch",
                "VIDEO_QA_CAMERA_AXIS": "wrong_camera_axis",
                "VIDEO_QA_GEOMETRY": "geometry_guard_unverified",
            }
            rule = reverse.get(issue.code, "")
        if not rule:
            continue
        patch = retry_patch_for_failure(rule)
        for n in patch.get("extra_negative") or []:
            if n not in negatives:
                negatives.append(n)
        hint = patch.get("hint")
        if hint and hint not in critiques:
            critiques.append(str(hint))
    return negatives[:8], critiques[:6]


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

    score_only = [
        issue for issue in normalized
        if str(issue.code).startswith("VIDEO_QA_")
    ]
    normalized = [
        issue for issue in normalized
        if not str(issue.code).startswith("VIDEO_QA_")
    ]
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

    preferred = max(
        (preferred_level_for_code(c) for c in codes),
        key=LEVEL_ORDER.index,
    )
    level = preferred
    if current_level and current_level in LEVEL_ORDER:
        # 修复成功后不降级：保留当前层级下限
        if LEVEL_ORDER.index(current_level) > LEVEL_ORDER.index(level):
            level = current_level  # type: ignore[assignment]

    # 特殊暂停码
    if "VIDEO_PROVIDER_UNAVAILABLE" in codes:
        return VideoRepairPlan(
            level="L6",
            strategy="handoff_human",
            issue_codes=codes,
            fingerprint=normalized[0].fingerprint,
            reason="Provider 长时间不可用",
            pause_state="PAUSED_EXTERNAL",
            is_paid=False,
        )
    if "VIDEO_BUDGET_EXHAUSTED" in codes or "VIDEO_WALL_CLOCK_EXCEEDED" in codes:
        return VideoRepairPlan(
            level="L6",
            strategy="handoff_human",
            issue_codes=codes,
            fingerprint=normalized[0].fingerprint,
            reason="预算或时长墙用尽",
            pause_state="WAITING_AUTHORIZATION",
            is_paid=False,
        )
    if "VIDEO_STORYBOARD_CHANGED" in codes:
        return VideoRepairPlan(
            level="L6",
            strategy="handoff_human",
            issue_codes=codes,
            fingerprint=normalized[0].fingerprint,
            reason="分镜 Artifact 已变更",
            pause_state="WAITING_AUTHORIZATION",
            is_paid=False,
        )
    if "VIDEO_HARNESS_DISABLED" in codes:
        return VideoRepairPlan(
            level="L6",
            strategy="handoff_human",
            issue_codes=codes,
            fingerprint=normalized[0].fingerprint,
            reason="Harness 灰度隔离",
            pause_state="WAITING_HUMAN",
            is_paid=False,
        )

    primary_fp = normalized[0].fingerprint
    prior = int(counts.get(primary_fp, 0))
    if prior >= STALL_ROUNDS:
        level = upgrade_level(level)

    # 致命失败换过参考图后仍出现 → L6
    from app.video_issues import is_fatal
    has_fatal = any(is_fatal(i) for i in normalized)
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
