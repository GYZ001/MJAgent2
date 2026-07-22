from __future__ import annotations

import re

from app.harness.types import Issue, IssueSeverity


_CODE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SCHEMA_INVALID", re.compile(r"字段|schema|JSON|解析|类型|必填", re.I)),
    ("SOURCE_FIDELITY", re.compile(r"原文|来源|source|依据|凭空|台词", re.I)),
    ("CONTRACT_FIELD_INVALID", re.compile(r"episode_no|mode|5~10 秒|5-10 秒|时长", re.I)),
    ("KEY_CONTENT_MISSING", re.compile(r"key_lines|key_plot_points|关键剧情|关键台词", re.I)),
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


def issues_from_messages(
    messages: list[str],
    *,
    subject: str,
    severity: IssueSeverity = IssueSeverity.BLOCKER,
) -> list[Issue]:
    """Compatibility layer while legacy validators still return human-readable strings."""
    return [
        Issue(
            code=issue_code(message),
            severity=severity,
            subject=subject,
            message=message,
            evidence={"span": subject},
            repair_hint=f"定向修复：{message}",
            repairable=True,
        )
        for message in messages
    ]


def issue_fingerprint(issues: list[Issue]) -> str:
    return "|".join(sorted(issue.fingerprint for issue in issues))
