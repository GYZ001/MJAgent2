from __future__ import annotations

import hashlib
import re

from app.harness.types import Issue, IssueSeverity


_CODE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ACTION_CAPACITY_EXCEEDED", re.compile(
        r"Prompt 编译失败.+镜头\s*\d+.+总长.+超过上限|镜头任务过载",
        re.I,
    )),
    ("SCHEMA_INVALID", re.compile(r"字段|schema|JSON|解析|类型|必填", re.I)),
    ("SOURCE_FIDELITY", re.compile(r"原文|来源|source|依据|凭空", re.I)),
    # 口播相关诊断必须先匹配具体合同/时间轴错误，再匹配容量兜底。
    # 否则包含“口播”的分叉和时间轴错误都会被误路由为容量超限。
    ("SPOKEN_CONTRACT_CONFLICT", re.compile(r"dialogues.+audio_timeline|口播合同|spoken.?contract|分叉", re.I)),
    ("SPOKEN_TIMELINE_OVERLAP", re.compile(r"口播时间段.+重叠|SPOKEN_TIMELINE_OVERLAP", re.I)),
    ("SPOKEN_TIMELINE_OUT_OF_RANGE", re.compile(r"口播时间段.+超出|SPOKEN_TIMELINE_OUT_OF_RANGE", re.I)),
    ("CONTRACT_FIELD_INVALID", re.compile(r"episode_no|mode|5~10 秒|5-10 秒|时长", re.I)),
    # 动作容量必须先于通用“容量上限”口播兜底匹配；否则分镜动作过载会被
    # 错路由为口播问题，并在逐镜 Agent Loop 中作为 score-only 候选漏过。
    ("ACTION_CAPACITY_EXCEEDED", re.compile(r"顺序动作节拍|主动作过载|动作容量", re.I)),
    ("SPOKEN_CAPACITY_EXCEEDED", re.compile(r"口播|台词.{0,8}超|字数.{0,6}超|容量上限|超过.{0,12}字", re.I)),
    ("DIALOGUE_FRAMING_INVALID", re.compile(
        r"多个画内说话人|单人对白|只保留说话人|近景或特写|对白双人镜|按话轮拆|正反打|"
        r"剧情道具操作|走位/离场|大形体动作|shot_size\s*不得为特写|不能用单人大近景替代",
        re.I,
    )),
    ("LEGACY_COVERAGE_UNCERTAIN", re.compile(r"LEGACY_COVERAGE_UNCERTAIN", re.I)),
    ("STATE_CHAIN_INVALID", re.compile(r"状态链|state_in|state_out|承接", re.I)),
    ("KEY_LINE_MISSING", re.compile(
        r"主线台词|关键台词|主线对白|对白上下文|对白顺序|dialogue_chains|key_lines|key_line", re.I
    )),
    ("SPINE_MISSING", re.compile(r"must_keep|spine_beat|主线节拍|plot_spine", re.I)),
    ("KEY_CONTENT_MISSING", re.compile(r"key_plot_points|主线剧情|关键剧情", re.I)),
    ("SHOT_OUTLINE_COVERAGE", re.compile(r"covers|大纲.*落实|本镜.*漏", re.I)),
    ("DROP_LIST_REINTRODUCED", re.compile(r"drop_list|又拍回了", re.I)),
    ("PLAN_EXHAUSTED_NOT_FINAL", re.compile(r"计划.*跑完|is_final|收束|未收束", re.I)),
    ("PLOT_SPINE_INVALID", re.compile(r"must_keep_ending|主线骨架", re.I)),
    ("OVERDETAIL", re.compile(r"超纲细节|衣角|指节|泪珠|写细", re.I)),
    ("LEDGER_INVALID", re.compile(r"information_ledger|events\[|event_id", re.I)),
    ("SHOT_BUDGET", re.compile(r"软预算|硬上限|镜头数|合并反应镜", re.I)),
    ("CHARACTER_CONSISTENCY", re.compile(r"人物谱|角色圣经|角色名|说话人|characters", re.I)),
    ("SCENE_FIELD_INVALID", re.compile(
        r"scene_outline.*\.(?:scene_no|scene_heading|story_function|summary|conflict|turn|source_basis|characters)",
        re.I,
    )),
    ("DRAMATIC_CONTRACT_INCOMPLETE", re.compile(
        r"dramatic_question|protagonist_goal|obstacle|stakes|戏剧", re.I
    )),
    ("FORMAT_CONTRACT_INVALID", re.compile(r"场次|段落|镜头语言|禁用词|full_script_text", re.I)),
)

# These codes describe missing or contradictory delivery contracts.  They are
# not subjective quality scores and must be resolved before storyboard
# confirmation can unlock paid media.
STORYBOARD_CONFIRMATION_BLOCKER_CODES = frozenset({
    "SPOKEN_CONTRACT_CONFLICT",
    "SPOKEN_TIMELINE_OVERLAP",
    "SPOKEN_TIMELINE_OUT_OF_RANGE",
    "STATE_CHAIN_INVALID",
    "KEY_LINE_MISSING",
    "SPINE_MISSING",
    "KEY_CONTENT_MISSING",
    "SHOT_OUTLINE_COVERAGE",
    "DROP_LIST_REINTRODUCED",
    "PLAN_EXHAUSTED_NOT_FINAL",
})

_EXPLICIT_CODE_RE = re.compile(r"^\s*\[([A-Z][A-Z0-9_]{2,80})\]")


def issue_code(message: str) -> str:
    explicit = _EXPLICIT_CODE_RE.match(message or "")
    if explicit:
        return explicit.group(1)
    for code, pattern in _CODE_RULES:
        if pattern.search(message):
            return code
    return "BUSINESS_RULE_FAILED"


def is_storyboard_confirmation_blocker(message_or_issue: str | Issue) -> bool:
    code = (
        message_or_issue.code
        if isinstance(message_or_issue, Issue)
        else issue_code(str(message_or_issue))
    )
    return (
        code in STORYBOARD_CONFIRMATION_BLOCKER_CODES
        or bool(_EXPLICIT_CODE_RE.match(
            message_or_issue.message if isinstance(message_or_issue, Issue) else str(message_or_issue)
        ))
    )


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
