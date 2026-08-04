"""Constraint-driven storyboard repair routing.

Issue codes identify violated invariants; they never prescribe a unique edit.
The router exposes an open candidate set and consumes an optional semantic
diagnosis (normally produced by :mod:`app.narrative_repair`).  Deterministic
code only limits the affected window, checks operational pauses and chooses a
safe reversible fallback when semantic diagnosis is unavailable.
"""
from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.evaluations.issues import issue_code, issue_fingerprint
from app.harness.types import Issue, IssueSeverity

RepairLevel = Literal["L0", "L1", "L2", "L3", "L4", "L5"]
# Strategy is an AI-authored semantic intent, not an executor enum.  Runtime
# safety is established by a typed ``outline_operations`` program and the
# complete narrative graph gate.  Keeping this surface open prevents a new
# narrative relation from being silently rewritten to a legacy repair mode.
RepairStrategy = str

LEVEL_ORDER: list[RepairLevel] = ["L0", "L1", "L2", "L3", "L4", "L5"]

_SHOT_NO_RE = re.compile(
    r"(?:shot_no\s*=\s*|第\s*|镜头\s*)(\d+)\s*镜?",
    re.I,
)

class RepairCandidate(BaseModel):
    candidate_id: str
    strategy: RepairStrategy
    touched_shot_nos: list[int] = Field(default_factory=list)
    expected_narrative_gain: float = 0.0
    destructive_cost: float = 0.0
    invariant_risks: list[str] = Field(default_factory=list)
    semantic_assumptions: list[str] = Field(default_factory=list)
    rationale: str = ""


class RepairPlan(BaseModel):
    level: RepairLevel
    strategy: RepairStrategy
    invalidation_frontier: int
    issue_codes: list[str] = Field(default_factory=list)
    issue_messages: list[str] = Field(default_factory=list)
    fingerprint: str = ""
    reason: str = ""
    pause_state: str | None = None  # WAITING_HUMAN / PAUSED_EXTERNAL / WAITING_AUTHORIZATION
    touched_shot_nos: list[int] = Field(default_factory=list)
    candidates: list[RepairCandidate] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    semantic_diagnosis: dict = Field(default_factory=dict)
    needs_semantic_selection: bool = False


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
    """Deprecated compatibility shim: content issue codes carry no repair level.

    Operational pauses are handled by explicit issue evidence in
    :func:`route_issues`; every content code deliberately returns the same
    neutral local scope.
    """
    _ = code
    return "L1"


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
    """Canonicalize public aliases without erasing an open semantic intent."""
    normalized = str(strategy or "").strip()
    if normalized == "split_shot":
        return "split_adjacent_shot"
    return normalized


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
    semantic_diagnosis: dict | None = None,
) -> RepairPlan:
    """Build candidates and select by semantic evidence, never by issue code.

    ``semantic_diagnosis`` is open JSON.  Its stable interoperability surface is
    ``scope`` and ``selected_strategy``; unknown dimensions remain preserved.
    """
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

    codes = [issue.code or "SEMANTIC_GAP_OTHER" for issue in normalized]
    messages = [issue.message for issue in normalized if issue.message]
    diagnosis = dict(semantic_diagnosis or {})

    # These are execution-state facts, not story semantics.  Prefer structured
    # evidence and retain legacy codes only as a transport compatibility layer.
    operational_kind = next((
        str(issue.evidence.get("operational_kind") or "")
        for issue in normalized
        if issue.evidence.get("operational_kind")
    ), "")
    if operational_kind == "provider_unavailable" or any(c == "PROVIDER_UNAVAILABLE" for c in codes):
        return RepairPlan(
            level="L5",
            strategy="waiting_retry",
            invalidation_frontier=max(1, validated_prefix_end or next_shot_no or 1),
            issue_codes=codes,
            issue_messages=messages,
            fingerprint=issue_fingerprint(normalized),
            reason="provider_unavailable",
            pause_state="PAUSED_EXTERNAL",
        )
    if operational_kind == "upstream_version_changed" or any(c == "UPSTREAM_VERSION_CHANGED" for c in codes):
        return RepairPlan(
            level="L5",
            strategy="waiting_authorization",
            invalidation_frontier=max(1, validated_prefix_end or 1),
            issue_codes=codes,
            issue_messages=messages,
            fingerprint=issue_fingerprint(normalized),
            reason="upstream_version_changed",
            pause_state="WAITING_AUTHORIZATION",
        )

    scope_to_level: dict[str, RepairLevel] = {
        "normalize": "L0",
        "current_shot": "L1",
        "adjacent_window": "L2",
        "structure": "L3",
        "multi_shot_structure": "L4",
        "human": "L5",
    }
    level: RepairLevel = scope_to_level.get(str(diagnosis.get("scope") or ""), "L2")
    if current_level and LEVEL_ORDER.index(current_level) > LEVEL_ORDER.index(level):
        level = current_level

    fp = issue_fingerprint(normalized)
    counts = dict(issue_fingerprint_counts or {})
    prior = counts.get(fp, 0)
    if prior >= 2:
        # Most callers persist fingerprint counts but do not persist/pass the
        # previous RepairLevel. Escalate from the count itself so a repeated
        # issue cannot remain at the same local-repair level forever.
        escalations = 1 if current_level else max(1, prior // 2)
        for _ in range(escalations):
            level = upgrade_level(level)
            if level == "L5":
                break
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
                issue_messages=messages,
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

    window = sorted(set([*touched, *[max(1, n - 1) for n in touched], *[n + 1 for n in touched]]))
    candidates = [
        RepairCandidate(
            candidate_id="candidate-repair-current",
            strategy="repair_current",
            touched_shot_nos=touched,
            destructive_cost=0.1,
            rationale="在原镜中重新组织证据、状态或节奏",
        ),
        RepairCandidate(
            candidate_id="candidate-repair-window",
            strategy="repair_window",
            touched_shot_nos=window,
            destructive_cost=0.2,
            rationale="在相邻窗口重分配贡献并保持整集顺序",
        ),
        RepairCandidate(
            candidate_id="candidate-insert",
            strategy="insert_shot",
            touched_shot_nos=touched,
            destructive_cost=0.35,
            rationale="仅在真实认知/状态缺口无法由现镜承载时增加支持镜",
        ),
        RepairCandidate(
            candidate_id="candidate-split",
            strategy="split_adjacent_shot",
            touched_shot_nos=window,
            destructive_cost=0.4,
            rationale="把超出单窗口处理能力的贡献拆到相邻镜",
        ),
        RepairCandidate(
            candidate_id="candidate-delete",
            strategy="delete_shot",
            touched_shot_nos=touched,
            destructive_cost=0.7,
            invariant_risks=["must_keep_event", "setup_payoff", "audience_handoff"],
            rationale="仅当删除测试证明镜头无边际叙事贡献时删除",
        ),
        RepairCandidate(
            candidate_id="candidate-move",
            strategy="move_shot",
            touched_shot_nos=window,
            destructive_cost=0.6,
            invariant_risks=["event_dag", "state_precondition", "audience_handoff"],
            rationale="仅当事件拓扑和状态前置条件允许时移动",
        ),
    ]
    by_strategy = {candidate.strategy: candidate for candidate in candidates}
    raw_selected_strategy = str(diagnosis.get("selected_strategy") or "").strip()
    selected_strategy = normalize_strategy(raw_selected_strategy)
    selected_assessment = next((
        item
        for item in list(diagnosis.get("candidate_assessments") or [])
        if normalize_strategy(str(item.get("strategy") or "")) == selected_strategy
    ), None)

    selected = by_strategy.get(selected_strategy)
    if selected is None and raw_selected_strategy:
        # An open strategy is executable only when it came through the semantic
        # planner's typed-operation + complete-graph validation boundary.  A
        # caller cannot smuggle an unknown strategy into a legacy branch merely
        # by naming it or by attaching unverified JSON.
        outline_operations = (
            list(selected_assessment.get("outline_operations") or [])
            if isinstance(selected_assessment, dict) else []
        )
        if diagnosis.get("execution_verified") is True and outline_operations:
            digest = hashlib.sha256(
                selected_strategy.encode("utf-8")
            ).hexdigest()[:12]
            selected = RepairCandidate(
                candidate_id=f"candidate-open-{digest}",
                strategy=selected_strategy,
                touched_shot_nos=touched,
                expected_narrative_gain=float(
                    selected_assessment.get("expected_narrative_gain") or 0.0
                ),
                destructive_cost=float(
                    selected_assessment.get("destructive_cost") or 0.0
                ),
                invariant_risks=list(
                    selected_assessment.get("invariant_risks") or []
                ),
                semantic_assumptions=list(
                    selected_assessment.get("semantic_assumptions") or []
                ),
                rationale=str(selected_assessment.get("rationale") or ""),
            )
            candidates.append(selected)
            by_strategy[selected_strategy] = selected
        else:
            return RepairPlan(
                level="L5",
                strategy="waiting_human",
                invalidation_frontier=frontier,
                issue_codes=codes,
                issue_messages=messages,
                fingerprint=fp,
                reason="semantic_strategy_not_executable",
                pause_state="WAITING_HUMAN",
                touched_shot_nos=touched,
                candidates=candidates,
                semantic_diagnosis=diagnosis,
                needs_semantic_selection=True,
            )
    if selected is None:
        selected = by_strategy["repair_window"]
    candidate_scores = diagnosis.get("candidate_scores") or {}
    normalized_scores = {
        normalize_strategy(str(candidate_strategy)): score
        for candidate_strategy, score in candidate_scores.items()
    }
    for candidate in candidates:
        score = normalized_scores.get(normalize_strategy(candidate.strategy))
        if isinstance(score, (int, float)):
            candidate.expected_narrative_gain = float(score)
    if not raw_selected_strategy:
        # No semantic evidence: choose the least destructive content-preserving
        # window and explicitly require semantic selection before destructive edits.
        selected = by_strategy["repair_window"]
    return RepairPlan(
        level=level,
        strategy=selected.strategy,
        invalidation_frontier=frontier,
        issue_codes=codes,
        issue_messages=messages,
        fingerprint=fp,
        reason=f"constraint_selection:{selected.candidate_id}",
        touched_shot_nos=selected.touched_shot_nos,
        candidates=candidates,
        selected_candidate_id=selected.candidate_id,
        semantic_diagnosis=diagnosis,
        needs_semantic_selection=not bool(raw_selected_strategy),
    )


def bump_fingerprint_count(
    counts: dict[str, int], fingerprint: str
) -> dict[str, int]:
    updated = dict(counts)
    if fingerprint:
        updated[fingerprint] = updated.get(fingerprint, 0) + 1
    return updated
