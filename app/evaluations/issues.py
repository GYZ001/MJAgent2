from __future__ import annotations

import hashlib
import re

from app.harness.types import Issue, IssueSeverity


_CODE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SCHEMA_INVALID", re.compile(r"字段|schema|JSON|解析|类型|必填", re.I)),
    ("SOURCE_FIDELITY", re.compile(r"原文|来源|source|依据|凭空", re.I)),
    ("CONTRACT_FIELD_INVALID", re.compile(r"episode_no|mode|5~10 秒|5-10 秒|时长", re.I)),
    ("KEY_CONTENT_MISSING", re.compile(r"key_lines|key_plot_points|主线台词|主线剧情|关键剧情|关键台词", re.I)),
    ("PLOT_SPINE_INVALID", re.compile(r"plot_spine|spine_beats|must_keep_ending|drop_list|主线骨架", re.I)),
    ("OVERDETAIL", re.compile(r"超纲细节|微微|衣角|指节|泪珠|写细", re.I)),
    ("LEDGER_INVALID", re.compile(r"information_ledger|events\[|event_id", re.I)),
    ("SHOT_BUDGET", re.compile(r"软预算|硬上限|镜头数|合并反应镜", re.I)),
    ("CHARACTER_CONSISTENCY", re.compile(r"人物谱|角色圣经|角色名|说话人|characters", re.I)),
    ("DRAMATIC_CONTRACT_INCOMPLETE", re.compile(
        r"dramatic_question|protagonist_goal|obstacle|stakes|戏剧", re.I
    )),
    ("FORMAT_CONTRACT_INVALID", re.compile(r"场次|段落|镜头语言|禁用词|full_script_text", re.I)),
)


def issue_code(message: str) -> str:
    for code, pattern in _CODE_RULES:
        if pattern.search(message):
            return code
    return "BUSINESS_RULE_FAILED"


_FIELD_ERROR_RE = re.compile(r"^字段\s+([^：:]+)[：:]\s*(.+)$", re.I)


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

    # Remove volatile numeric literals while retaining the actual rule text.
    template = re.sub(r"\d+(?:\.\d+)?", "#", re.sub(r"\s+", " ", lowered)).strip()
    return "", "message_" + hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]


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
