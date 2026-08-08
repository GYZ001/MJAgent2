from __future__ import annotations

import hashlib
import re

from app.harness.types import Issue, IssueSeverity


_EXPLICIT_CODE_RE = re.compile(r"^\s*\[([A-Z][A-Z0-9_]{2,80})\]")


def issue_code(message: str) -> str:
    """Read an explicit structured code; never infer one from story prose."""
    explicit = _EXPLICIT_CODE_RE.match(message or "")
    if explicit:
        return explicit.group(1)
    return "BUSINESS_RULE_FAILED"


_FIELD_ERROR_RE = re.compile(r"^字段\s+([^：:]+)[：:]\s*(.+)$", re.I)
_INDEXED_PATH_RE = re.compile(
    r"\b([a-z_][a-z0-9_]*(?:\[\d+\])"
    r"(?:\.[a-z_][a-z0-9_]*(?:\[\d+\])?)*)",
    re.I,
)
_SCENE_INDEX_RE = re.compile(r"\b(scene_outline)\s*第\s*(\d+)\s*场", re.I)
_ENTITY_REF_RE = re.compile(r"\b([A-Z][A-Z0-9_]*-\d+)\b")


def _canonical_issue_identity(code: str, message: str) -> tuple[str, str]:
    """Return a stable (path, rule_id) pair for loop progress detection.

    Human-readable messages are intentionally not used verbatim: validators
    often include counts or indexes that change between otherwise identical
    failures.  Schema errors get a precise field path and error rule, while
    legacy business rules use a normalized message-template digest.
    """
    field_error = _FIELD_ERROR_RE.match(message.strip())
    if field_error:
        path, detail = field_error.groups()
        normalized_detail = re.sub(r"\s+", " ", detail.strip().lower())
        known_rules = (
            ("valid string", "string_type"),
            ("valid list", "list_type"),
            ("valid integer", "int_type"),
            ("valid number", "number_type"),
            ("field required", "missing"),
        )
        rule_id = next(
            (rule for marker, rule in known_rules if marker in normalized_detail),
            "schema_" + hashlib.sha256(normalized_detail.encode("utf-8")).hexdigest()[:12],
        )
        return path.strip(), rule_id

    lowered = message.lower()
    if "json 解析失败" in lowered or "json解析失败" in lowered:
        return "$", "json_decode"
    if "找不到 json 对象" in lowered:
        return "$", "json_object_missing"
    if "json 根节点不是对象" in lowered:
        return "$", "json_root_type"

    indexed = _INDEXED_PATH_RE.search(message)
    scene_index = _SCENE_INDEX_RE.search(message)
    path = ""
    if indexed:
        path = indexed.group(1)
    elif scene_index:
        path = f"{scene_index.group(1)}[{max(0, int(scene_index.group(2)) - 1)}]"
    else:
        entity = _ENTITY_REF_RE.search(message)
        if entity:
            path = f"entity:{entity.group(1)}"

    # Keep the concrete path as issue identity, while normalizing volatile
    # counts in the rule text. Distinct nodes must never share repair history.
    normalized = re.sub(r"\s+", " ", lowered).strip()
    if path:
        normalized = normalized.replace(path.lower(), "$path")
    normalized = re.sub(r"\d+(?:\.\d+)?", "#", normalized)
    rule_id = "message_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return path, rule_id


def issues_from_messages(
    messages: list[str],
    *,
    subject: str,
    severity: IssueSeverity = IssueSeverity.BLOCKER,
) -> list[Issue]:
    """Compatibility layer while legacy validators still return human-readable strings."""
    issues: list[Issue] = []
    for message in messages:
        code = issue_code(message)
        path, rule_id = _canonical_issue_identity(code, message)
        issues.append(Issue(
            code=code,
            severity=severity,
            subject=subject,
            message=message,
            evidence={"span": subject, "path": path, "rule_id": rule_id},
            repair_hint=f"定向修复：{message}",
            repairable=True,
        ))
    return issues


def issue_fingerprint(issues: list[Issue]) -> str:
    return "|".join(sorted(issue.fingerprint for issue in issues))
