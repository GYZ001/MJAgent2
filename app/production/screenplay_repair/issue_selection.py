"""Choosing which open issue to repair next and testing whether a prior target
issue's signature is still open or whether a candidate patch introduced new
issues.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

import re
from app.harness.types import Issue
from collections import Counter
from typing import Any


def _choose_issue(issues: list[Issue]) -> Issue | None:
    if not issues:
        return None
    repairable = [i for i in issues if i.repairable]
    pool = repairable or issues

    severity_order = {"blocker": 0, "error": 1, "warning": 2, "info": 3}

    def issue_priority(item: tuple[int, Issue]) -> tuple[float, float, float, int]:
        index, issue = item
        evidence = issue.evidence or {}
        severity_value = getattr(issue.severity, "value", issue.severity)
        severity = severity_order.get(str(severity_value), 4)
        # Producers may expose graph depth/affected scope, but missing values
        # remain neutral.  These are relation properties, never issue-code or
        # story-word mappings.
        try:
            dependency_depth = float(evidence.get("dependency_depth", 0))
        except (TypeError, ValueError):
            dependency_depth = 0.0
        try:
            affected_scope = float(evidence.get("affected_scope_size", 1))
        except (TypeError, ValueError):
            affected_scope = 1.0
        # Validator order is the dependency-neutral final tiebreaker. Sorting by
        # fingerprint here used to turn a missing graph annotation into an
        # accidental alphabetical repair policy.
        return severity, dependency_depth, -affected_scope, index

    return min(enumerate(pool), key=issue_priority)[1]


def _identity_contract_repair_policy() -> dict[str, Any]:
    """Return the typed, content-agnostic identity rules used by graph repair."""
    return {
        "authority": (
            "identity_contracts 是所有非角色圣经身份的唯一权威声明；"
            "修复不得引入未声明的实体、场次人物或任何说话人（包括旁白）"
        ),
        "contract_fields": {
            "identity_id": "稳定图引用 ID",
            "display_name": "剧本与对白使用的精确显示名",
            "kind": "基于当前来源和戏剧职责的开放语义",
            "visual_policy": "canonical | contextual | collective | offscreen_only",
            "visual_canonical": "非 offscreen_only 必填的中性视觉锚点",
            "asset_requirement": "required | optional | forbidden",
            "voice_ids": "精确回指 voice_bible.speaker_id",
            "evidence": {
                "source_evidence_ids": [],
                "proposition_ids": [],
                "adaptation_decision_ids": [],
                "rationale": "身份决策的可追溯理由",
            },
        },
        "typed_invariants": [
            "canonical 必须 asset_requirement=required",
            "offscreen_only 必须 asset_requirement=forbidden",
            "除 offscreen_only 外 visual_canonical 必填",
            "仅当当前文档已有结构化 voice_bible.role_type=narrator 且有来源证据时才允许旁白；不得从 prose/summary/环境介绍推导或创建旁白",
            "narrator 或 offscreen_only 可作为声源，但不得写入 scene_blocks[*].characters 伪装成可见角色",
            "environment:<episode-scope> 是 compiler 独占的非人物状态主体；修复不得创建、删除或改写该 ID，也不得把它加入 identity_contracts、voice_bible、scene characters、动作参与者或 POV",
        ],
        "semantic_decision": (
            "具名新角色、一次性功能身份、群体或纯画外身份均按当前语义意图决策；"
            "禁止使用姓名、称谓、身份类型或题材白名单"
        ),
    }


def _issue_acceptance_test(issue: Issue) -> str:
    return (
        "把候选隔离应用到当前完整文档后，必须让 issue.message 从同一组确定性"
        "校验结果中消失，且不得新增任何校验错误。目标节点和字段只能由文档内"
        "稳定 ID、直接字段所有权与现行 schema 推导；来源内容必须可追溯到"
        "authorized_source_excerpt，禁止按问题码、题材、角色名或文本关键词套用模板"
    )


def _target_issue_signature_still_open(
    target: Issue,
    candidate_issues: list[Issue],
) -> bool:
    """Fail closed when a repair merely swaps one field error for another.

    Validator prose is allowed to become more specific after a candidate is
    applied, so comparing only the original message is insufficient.  For a
    structured target, the code/severity/subject/path/rule identity must be
    absent after repair; a different message in that same slot is still the
    same unresolved deterministic invariant.
    """
    evidence = target.evidence or {}
    signature = (
        target.code,
        target.severity,
        target.subject,
        str(evidence.get("path") or evidence.get("span") or ""),
        str(evidence.get("rule_id") or ""),
    )
    if not signature[3] and not signature[4]:
        return any(item.fingerprint == target.fingerprint for item in candidate_issues)
    return any(
        (
            item.code,
            item.severity,
            item.subject,
            str((item.evidence or {}).get("path") or (item.evidence or {}).get("span") or ""),
            str((item.evidence or {}).get("rule_id") or ""),
        ) == signature
        for item in candidate_issues
    )


def _introduced_issue_messages(
    baseline_issues: list[Issue],
    candidate_issues: list[Issue],
) -> list[str]:
    """Detect new validation slots while allowing one aggregate slot to shrink."""
    def slot(issue: Issue) -> tuple[str, str, str, str, str, str]:
        evidence = issue.evidence or {}
        path = str(evidence.get("path") or evidence.get("span") or "")
        collection_path = re.sub(r"\[\d+\]", "[]", path)
        return (
            issue.code,
            issue.subject,
            str(evidence.get("rule_id") or ""),
            collection_path,
            str(evidence.get("stage") or ""),
            issue.severity.value,
        )

    baseline_counts = Counter(slot(issue) for issue in baseline_issues)
    candidate_counts: Counter[tuple[str, str, str, str, str, str]] = Counter()
    introduced: list[str] = []
    for issue in candidate_issues:
        key = slot(issue)
        candidate_counts[key] += 1
        if candidate_counts[key] > baseline_counts[key]:
            introduced.append(issue.message)
    return introduced


