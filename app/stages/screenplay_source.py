"""剧本生成/蓝图修复共用的原文渲染与角色身份预解析提示词块。"""
from __future__ import annotations

import json
import re


from app.identity_authority import (
    identity_resolution_is_authoritative,
    model_identity_authority_prompt_rule,
)


# 模型以为看到了全部，把后半章静默丢掉。改为命名常量 + 截断标记，让模型知道"后文还有，按依据补全"。
SCREENPLAY_SOURCE_BUDGET_CHARS = 120000


_SOURCE_QUOTED_DIALOGUE_RE = re.compile(r"[“「『][^”」』\n]{2,240}[”」』]")
_SOURCE_SPEAKER_DIALOGUE_RE = re.compile(
    r"(?m)^[^\n：:]{1,20}[：:][^\n]{2,300}$"
)


def _source_dialogue_evidence(text: str, limit: int) -> str:
    """Select exact dialogue evidence from a source section without inventing prose.

    Long chapters used to be truncated at the head, which made dialogue in the
    middle or climax literally unavailable to generation and repair.  Preserve
    dialogue-shaped source fragments in addition to stable head/tail context.
    """
    if limit <= 0 or not text:
        return ""
    fragments: list[str] = []
    seen: set[str] = set()
    for pattern in (_SOURCE_SPEAKER_DIALOGUE_RE, _SOURCE_QUOTED_DIALOGUE_RE):
        for match in pattern.finditer(text):
            fragment = match.group(0).strip()
            condensed = re.sub(r"\s+", "", fragment)
            if not condensed or condensed in seen:
                continue
            seen.add(condensed)
            fragments.append(fragment)
    selected: list[str] = []
    used = 0
    for fragment in fragments:
        cost = len(fragment) + (1 if selected else 0)
        if used + cost > limit:
            continue
        selected.append(fragment)
        used += cost
    return "\n".join(selected)


def _render_screenplay_source(source_text: str, budget: int = SCREENPLAY_SOURCE_BUDGET_CHARS) -> str:
    text = source_text or ""
    if len(text) <= budget:
        return text
    marker_a = "\n\n……（中段叙事已按上下文预算压缩；以下保留中段原文对白证据）……\n"
    marker_b = "\n\n……（继续保留本章结尾原文，结尾事件与台词不得遗漏）……\n"
    payload_budget = max(0, budget - len(marker_a) - len(marker_b))
    head_budget = int(payload_budget * 0.35)
    tail_budget = int(payload_budget * 0.35)
    dialogue_budget = payload_budget - head_budget - tail_budget
    middle_end = max(head_budget, len(text) - tail_budget)
    middle = text[head_budget:middle_end]
    dialogue_evidence = _source_dialogue_evidence(middle, dialogue_budget)
    # If the middle contains little dialogue, use the remaining allowance for
    # contiguous context immediately after the head instead of wasting budget.
    unused = max(0, dialogue_budget - len(dialogue_evidence))
    head = text[:head_budget + unused]
    tail = text[-tail_budget:] if tail_budget else ""
    return head + marker_a + dialogue_evidence + marker_b + tail


def _character_resolution_prompt_block(episode: dict) -> str:
    """把剧本预检的姓名消歧结果转成生产硬合同。"""
    from app.identity_authority import normalize_character_resolutions

    rows = [
        item
        for item in normalize_character_resolutions(
            episode.get("character_resolutions") or [],
        )
        if identity_resolution_is_authoritative(item)
    ]
    if not rows:
        return (
            "【角色身份预解析】本集没有额外称谓决议；人物谱角色的 "
            "authority_id 使用 bible:<人物谱准确姓名>。"
            + model_identity_authority_prompt_rule()
        )
    return (
        "【角色身份预解析·剧本发布硬门禁】\n"
        + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        + "\n人物谱准确姓名使用 bible:<姓名>。"
        + model_identity_authority_prompt_rule()
        + "除 source_text 等逐字原文证据外，所有展示姓名必须使用对应 canonical_name。"
        + "后续章节只用于确认身份，严禁在本集泄露其剧情。"
    )
