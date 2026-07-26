"""结构化 Issue 合同增强：稳定 path / node / must_fix / repairable。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.evaluations.issues import issue_code, issues_from_messages
from app.harness.types import Issue, IssueSeverity


_SCENE_RE = re.compile(r"(?:场|SC|scene[_\s-]?)(\d+)", re.I)
_SHOT_RE = re.compile(r"(?:shot_no\s*=\s*|第\s*|镜\s*)(\d+)", re.I)
_FIELD_RE = re.compile(r"^字段\s+([^：:]+)[：:]", re.I)
_CHAIN_RE = re.compile(r"(DC\d+(?:-T\d+)?|KL\d+|I\d+|E\d+|S\d+)", re.I)


def structured_issue(
    *,
    code: str,
    message: str,
    subject: str,
    path: str = "",
    rule_id: str = "",
    related_node_ids: list[str] | None = None,
    source_evidence: list[dict[str, Any]] | None = None,
    dependency_hints: list[str] | None = None,
    repair_hint: str | None = None,
    repairable: bool = True,
    must_fix: bool = True,
    severity: IssueSeverity = IssueSeverity.BLOCKER,
    artifact_id: str | None = None,
    artifact_hash: str | None = None,
    stage: str | None = None,
) -> Issue:
    evidence: dict[str, Any] = {
        "path": path,
        "rule_id": rule_id or code.lower(),
        "must_fix": must_fix,
        "related_node_ids": list(related_node_ids or []),
        "source_evidence": list(source_evidence or []),
        "dependency_hints": list(dependency_hints or []),
    }
    if artifact_id:
        evidence["artifact_id"] = artifact_id
    if artifact_hash:
        evidence["artifact_hash"] = artifact_hash
    if stage:
        evidence["stage"] = stage
    return Issue(
        code=code,
        severity=severity,
        subject=subject,
        message=message,
        evidence=evidence,
        repair_hint=repair_hint or f"定向修复：{message}",
        repairable=repairable,
    )


def enrich_issue(issue: Issue, *, stage: str | None = None, artifact_id: str | None = None) -> Issue:
    """把遗留 Issue 补齐 path / related_node_ids / must_fix。"""
    evidence = dict(issue.evidence or {})
    path = str(evidence.get("path") or "")
    if not path:
        field_match = _FIELD_RE.match(issue.message or "")
        if field_match:
            path = field_match.group(1).strip()
        else:
            path = _infer_path(issue)
        evidence["path"] = path
    if "rule_id" not in evidence:
        evidence["rule_id"] = issue.code.lower()
    if "must_fix" not in evidence:
        evidence["must_fix"] = issue.severity == IssueSeverity.BLOCKER
    related = list(evidence.get("related_node_ids") or [])
    if not related:
        related = _infer_related_nodes(issue, path)
        evidence["related_node_ids"] = related
    if stage:
        evidence["stage"] = stage
    if artifact_id:
        evidence["artifact_id"] = artifact_id
    return issue.model_copy(update={"evidence": evidence, "repairable": True if issue.repairable is False and issue.severity == IssueSeverity.BLOCKER else issue.repairable})


def enrich_issues(
    issues: list[Issue],
    *,
    stage: str | None = None,
    artifact_id: str | None = None,
) -> list[Issue]:
    return [enrich_issue(i, stage=stage, artifact_id=artifact_id) for i in issues]


def issues_from_validator_messages(
    messages: list[str],
    *,
    subject: str,
    stage: str,
    severity: IssueSeverity = IssueSeverity.BLOCKER,
) -> list[Issue]:
    base = issues_from_messages(messages, subject=subject, severity=severity)
    return enrich_issues(base, stage=stage)


def issue_set_hash(issues: list[Issue]) -> str:
    parts = sorted(i.fingerprint for i in issues)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def must_fix_count(issues: list[Issue]) -> int:
    return sum(
        1
        for i in issues
        if i.severity == IssueSeverity.BLOCKER or bool((i.evidence or {}).get("must_fix", True))
    )


def blocker_count(issues: list[Issue]) -> int:
    return sum(1 for i in issues if i.severity == IssueSeverity.BLOCKER)


def _infer_path(issue: Issue) -> str:
    msg = issue.message or ""
    code = issue.code or issue_code(msg)
    shot = _SHOT_RE.search(msg) or _SHOT_RE.search(issue.subject or "")
    scene = _SCENE_RE.search(msg)
    node = _CHAIN_RE.search(msg)
    if shot:
        return f"/shots/{shot.group(1)}"
    if scene:
        return f"/scene_blocks/SC{int(scene.group(1)):02d}"
    if node:
        return f"/nodes/{node.group(1).upper()}"
    # dramatic contract fields
    for field in ("stakes", "obstacle", "protagonist_goal", "dramatic_question"):
        if field in msg.lower() or field in msg:
            return f"/{field}"
    if "dialogue" in msg.lower() or "对白" in msg:
        return "/dialogue_chains"
    if "ledger" in msg.lower() or "台账" in msg:
        return "/information_ledger"
    if "spine" in msg.lower() or "主线" in msg:
        return "/plot_spine"
    return f"/{code.lower()}"


def _infer_related_nodes(issue: Issue, path: str) -> list[str]:
    nodes: list[str] = []
    for text in (issue.message, issue.subject, path):
        for match in _CHAIN_RE.finditer(text or ""):
            nodes.append(match.group(1).upper())
        for match in _SHOT_RE.finditer(text or ""):
            nodes.append(f"shot:{match.group(1)}")
        for match in _SCENE_RE.finditer(text or ""):
            nodes.append(f"SC{int(match.group(1)):02d}")
    # de-dupe preserve order
    return list(dict.fromkeys(nodes))
