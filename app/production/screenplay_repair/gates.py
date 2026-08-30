"""Repair-loop gate constants, gate exceptions and issue-severity gate helpers.

Split out of app/production/screenplay_repair.py (see app/production/
screenplay_repair/__init__.py for the full package rationale). Owns: repair
budget constants, ScreenplayIdentityGateError/ScreenplayNarrativeGateError,
non_waivable_screenplay_issues/screenplay_identity_gate_issues (which issues a
gate may not waive) and _gate_failure_message (how a gate reports itself).
"""
from __future__ import annotations

import json
import re
from app.harness.types import Issue
from app.orchestration.state_machine import StateConflict
from typing import Any


MAX_REPAIR_ACTIVATION_PATCHES = 12
MAX_REPAIR_ACTIVATION_PASSES = 32
MAX_STRATEGY_ATTEMPTS_PER_ISSUE = 5
NARRATIVE_PATCH_PLANNER_MAX_OUTPUT_TOKENS = 8192
SCREENPLAY_REPAIR_PLANNER_VERSION = "screenplay-repair-17"


def _persist_screenplay_duration_expansion(
    conn,
    *,
    episode_id: str,
    expected_target_s: int,
    expected_planning_s: int | None,
    expected_duration_authority: str,
    expected_active_run_id: str | None,
    required_target_s: int,
) -> None:
    """CAS-persist every duration field that belongs to screenplay authority."""
    if expected_duration_authority != "planning_estimate":
        raise StateConflict(
            "screenplay_duration_authority",
            episode_id,
            {"planning_estimate"},
            expected_duration_authority or None,
        )
    cursor = conn.execute(
        """UPDATE episodes
              SET target_duration_s=?,
                  planning_target_duration_s=?,
                  screenplay_snapshot_version=screenplay_snapshot_version+1
            WHERE id=?
              AND target_duration_s=?
              AND planning_target_duration_s IS ?
              AND target_duration_authority=?
              AND active_screenplay_run_id IS ?""",
        (
            required_target_s,
            required_target_s,
            episode_id,
            expected_target_s,
            expected_planning_s,
            expected_duration_authority,
            expected_active_run_id,
        ),
    )
    if cursor.rowcount != 1:
        current = conn.execute(
            """SELECT target_duration_s,planning_target_duration_s,
                      target_duration_authority,active_screenplay_run_id
                 FROM episodes WHERE id=?""",
            (episode_id,),
        ).fetchone()
        raise StateConflict(
            "screenplay_duration",
            episode_id,
            {
                json.dumps(
                    {
                        "target": expected_target_s,
                        "planning": expected_planning_s,
                        "authority": expected_duration_authority,
                        "owner": expected_active_run_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
            (
                json.dumps(
                    {
                        "target": current["target_duration_s"],
                        "planning": current["planning_target_duration_s"],
                        "authority": current["target_duration_authority"],
                        "owner": current["active_screenplay_run_id"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if current is not None else None
            ),
        )


class ScreenplayIdentityGateError(RuntimeError):
    """人物身份未解决时保留可操作的剧本阶段诊断。"""


class ScreenplayNarrativeGateError(RuntimeError):
    """修复耗尽仍未通过硬门禁；工作稿保留，但绝不发布。"""

_SCENE_NUMBER_RE = re.compile(r"scene_outline\s*第\s*(\d+)\s*场|/scene_blocks/SC(\d+)", re.I)
_DIALOGUE_SOURCE_MISMATCH_RE = re.compile(
    r"dialogue_chains\[(\d+)\]\.turns\[(\d+)\]\.source_text\s+"
    r"(?:未在本集原文中找到|与改编台词语义不匹配)"
)
_SOURCE_SPAN_EXACT_MISMATCH_RE = re.compile(
    r"\[SOURCE_SPAN_EXACT_MISMATCH\]\s+([^\s.。:：]+)"
)
_SOURCE_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_SOURCE_EVIDENCE_STOP_CHARS = set(
    "的一了是在有和与把被就都还又只这那个人我们你他她它说问答"
)


def _eval_id_from_create(evaluation_row: dict[str, Any] | str | None) -> str:
    if isinstance(evaluation_row, dict):
        return str(evaluation_row.get("id") or "")
    return str(evaluation_row or "")


def non_waivable_screenplay_issues(issues: list[Issue]) -> list[Issue]:
    """Select runtime gates from issue attributes, never from a code whitelist."""
    return [
        issue
        for issue in issues
        if (
            bool((issue.evidence or {}).get("must_fix", False))
            or bool((issue.evidence or {}).get("runtime_blocking", False))
        )
    ]


def screenplay_identity_gate_issues(issues: list[Issue]) -> list[Issue]:
    """Return identity-specific gates using their structured owner metadata."""
    return [
        issue
        for issue in non_waivable_screenplay_issues(issues)
        if (
            str((issue.evidence or {}).get("path") or "").startswith(
                "/character_identities/"
            )
            or str(
                (issue.evidence or {}).get("rule_id") or ""
            ) == "character_identity_must_resolve_before_publish"
        )
    ]


def _gate_failure_message(
    open_issues: list[Issue],
    *,
    failed_issue: Issue | None,
) -> str:
    """Put the issue that actually stopped repair ahead of the remaining backlog."""
    ordered: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for issue in ([failed_issue] if failed_issue is not None else []) + open_issues:
        identity = (issue.fingerprint, issue.message)
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(issue)

    prefix = "剧本工作稿已保留，但叙事/质量硬门禁仍未通过，禁止发布："
    if failed_issue is not None:
        prefix += f"自动修复停止于 {failed_issue.code}："
    return (prefix + "；".join(issue.message for issue in ordered[:5]))[:1200]


