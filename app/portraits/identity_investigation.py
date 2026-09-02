"""当前身份判定的「有工具调查」阶段（Phase A）与闸门回喂循环。

在严格 json_schema 作答（Phase B，仍是 ``discovery_resample._identity_structured_
with_resample``）之前，先给模型三个只读工具（核对原文引文/查人物谱/查已登记决议），
让它带着核实过的事实再作答，而不是凭记忆猜测。调查为空（模型一次工具都没调、也没写
笔记）时 Phase B 的 prompt 与关闭本功能时字节完全相同，不影响现有 prompt 哈希/
operation_id 断言。

闸门回喂循环的触发点——重要的实测结论，实现前必须先读：``discovery_legacy.py``
里 ``run_phase_b`` 包装的 ``_identity_structured_with_resample`` 之后本该有一段
「``_project_current_identity_response`` 复核，发现语义违规就把 ``(candidates,
errors)`` 返回给这里重试」的逻辑，但那段在当前契约下是死代码：
``validate_current_response`` 与复核调用的是同一个纯函数、同样入参，而
``model_gateway.chat_structured`` 在 strict_identity_substage 下对 ``validate()``
的返回值语义是「非空立即 ``raise StructuredSemanticError``，不留给调用方任何
机会」（见 ``tests/test_character_discovery.py::
test_semantically_invalid_identity_answer_is_never_resampled`` 的实测）——只要
validate 判定有语义错误，``_identity_structured_with_resample`` 根本不会把
response 返回给调用方，复核永远只会看到「零语义错误」的响应；唯一会走到复核、
进而抛出格式错误的，只有「validate 判定通过但复核又发现纯 schema 越界」这一种
情况，而那种情况按设计必须保持立即失败、不重试（本模块也不改这一条）。

**没有把捕获点挪到 ``StructuredSemanticError`` 上去凑活。** 最初的实现试过在
``run_phase_b`` 里捕获这个异常、把语义错误交回这里重试，但那样做直接打穿了一条
独立、且同样有实测支撑的设计不变量：``discovery_resample.py`` 顶部原文——真正
的业务判断分歧（不是"未交付"）绝不能重采样，因为把模型自己的错误答案回显给它
换取"改对"就是在教它伪造合规。``tests/test_character_discovery.py`` 里至少 6 个
以 ``_fails_once``/``StructuredSemanticError`` 命名或断言的用例（如
``test_current_identity_cross_batch_same_label_new_group_fails_once``，断言
``model_gateway.chat`` 恰好被调用 2 次）直接实测这一条：语义违规必须一次性失败，
不得重试。捕获 ``StructuredSemanticError`` 会让这些用例的调用次数与异常类型全部
改变——这不是可以为了让新功能"看起来生效"而牺牲的细节，是先于本次改动就存在、
经过 RCA 验证的正确行为。因此这里保持 ``phase_b`` 对语义违规继续原样向上抛出
``StructuredSemanticError``（``run_phase_b`` 完全不捕获它），回喂循环对当前身份
契约事实上永远只跑 1 轮——``resolve_with_investigation_gate`` 的重试机制本身是
通用的（``phase_b`` 返回 ``(_, errors)`` 而不是 raise 时才会重试），为将来
若这条契约改变留了正确的挂载点，但今天不会被触发，也不应该被强行改造成触发。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app import hiagent
from app.db import get_setting
from app.errors import ContentGenerationError
from app.schemas import Bible

logger = logging.getLogger(__name__)

IDENTITY_INVESTIGATION_MAX_TOOL_ROUNDS = 6
IDENTITY_INVESTIGATION_MAX_TOOL_CALLS_PER_ROUND = 3
IDENTITY_GATE_ROUNDS = 2

_DISABLED_SETTING_VALUES = {"0", "off", "false", "no", "disabled"}
_SEARCH_CONTEXT_CHARS = 120
_SEARCH_MAX_HITS = 5
_LIST_DECISIONS_MAX = 20
_WHITESPACE_RE = re.compile(r"\s+")

_INVESTIGATION_SYSTEM_PROMPT = (
    "你是本集人物身份判定的调查助手。正式作答前，你可以调用只读工具核实原文引文、"
    "查询人物谱与已登记决议，避免凭记忆或猜测判断身份。工具结果由后端执行，真实"
    "可信；调用工具不消耗你的最终作答机会，最终结构化作答会在下一步单独请求。"
)


@dataclass
class InvestigationContext:
    """一个 current_batch 调查所需的只读上下文；由 discovery_legacy.py 组装。"""

    episode_no: int
    current_batch: int
    current_haystack: str
    future_text: str
    future_label: str
    bible: Bible
    current_authorities: list[dict] = field(default_factory=list)
    known_decision_projection: list[dict] = field(default_factory=list)
    prior_functional_projection: list[dict] = field(default_factory=list)
    existing_resolution_projection: list[dict] = field(default_factory=list)


def _identity_investigation_enabled() -> bool:
    value = str(get_setting("identity_investigation") or "").strip().lower()
    return value not in _DISABLED_SETTING_VALUES


def _normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


# ---------------------------------------------------------------------------
# 工具 1：search_source
# ---------------------------------------------------------------------------


def _search_in_text(haystack: str, quote: str, *, source: str, limit: int) -> list[dict]:
    normalized_haystack = _normalize_whitespace(haystack)
    normalized_quote = _normalize_whitespace(quote)
    if not normalized_quote or limit <= 0:
        return []
    hits: list[dict] = []
    start = 0
    while len(hits) < limit:
        idx = normalized_haystack.find(normalized_quote, start)
        if idx < 0:
            break
        left = max(0, idx - _SEARCH_CONTEXT_CHARS)
        right = min(len(normalized_haystack), idx + len(normalized_quote) + _SEARCH_CONTEXT_CHARS)
        hits.append({"source": source, "context": normalized_haystack[left:right]})
        start = idx + len(normalized_quote)
    return hits


def search_source(context: InvestigationContext, quote: str) -> dict:
    """空白归一后的逐字查找，不做模糊匹配兜底；没命中就如实说未找到。"""
    hits = _search_in_text(
        context.current_haystack, quote, source="current", limit=_SEARCH_MAX_HITS
    )
    remaining = _SEARCH_MAX_HITS - len(hits)
    if remaining > 0 and context.future_text:
        hits.extend(
            _search_in_text(context.future_text, quote, source="future", limit=remaining)
        )
    if not hits:
        return {"message": "未找到：这句引文在本集原文与后续章节里都不存在。"}
    return {
        "hits": hits,
        "note": "source=future 的命中仅用于消歧，不得据此把未出场人物带回本集。",
    }


# ---------------------------------------------------------------------------
# 工具 2：lookup_bible
# ---------------------------------------------------------------------------


def _character_matches(character: Any, name: str) -> bool:
    if name in character.name or character.name in name:
        return True
    return any(
        name in alias.text or alias.text in name
        for alias in character.aliases
        if alias.text
    )


def _bible_character_summary(character: Any) -> dict:
    return {
        "name": character.name,
        "aliases": [
            {"text": a.text, "name_kind": a.name_kind, "evidence_quote": a.evidence_quote}
            for a in character.aliases
        ],
        "appearance": (character.appearance_canonical or "")[:80],
    }


def _authority_matches(authority: dict, name: str) -> bool:
    canonical = str(authority.get("canonical_name") or "")
    labels = [str(v) for v in (authority.get("source_labels") or [])]
    if canonical and (name in canonical or canonical in name):
        return True
    return any(label and (name in label or label in name) for label in labels)


def lookup_bible(context: InvestigationContext, name: str) -> dict:
    """按 name / aliases[].text 精确或子串匹配人物谱，并核对已登记身份权威。"""
    name = str(name or "").strip()
    if not name:
        return {"message": "未收录：查询名为空。"}
    characters = [
        _bible_character_summary(c)
        for c in context.bible.characters
        if _character_matches(c, name)
    ]
    authorities = [
        {
            "canonical_name": a.get("canonical_name"),
            "source_labels": a.get("source_labels"),
            "identity_kind": a.get("identity_kind"),
            "authority_id": a.get("authority_id"),
        }
        for a in context.current_authorities
        if _authority_matches(a, name)
    ]
    if not characters and not authorities:
        return {"message": f"未收录：人物谱与已登记权威里都没有能匹配「{name}」的条目。"}
    return {"bible_characters": characters, "authorities": authorities}


# ---------------------------------------------------------------------------
# 工具 3：list_decisions
# ---------------------------------------------------------------------------


def list_decisions(context: InvestigationContext, label: str) -> dict:
    """按 label 子串过滤本批已登记决议；空串返回全部（截断到合理数量）。"""
    label = str(label or "").strip()
    known = [
        {
            "decision_id": item.get("decision_id"),
            "decision_type": item.get("decision_type"),
            "source_label": item.get("source_label"),
            "canonical_name": item.get("canonical_name"),
            "allowed_kinds": item.get("allowed_kinds"),
        }
        for item in context.known_decision_projection
        if not label or label in str(item.get("source_label") or "")
    ][:_LIST_DECISIONS_MAX]
    prior = [
        {
            "decision_id": item.get("decision_id"),
            "source_labels": item.get("source_labels"),
            "existing_route_name": item.get("existing_route_name"),
        }
        for item in context.prior_functional_projection
        if not label
        or any(label in str(v) for v in (item.get("source_labels") or []))
    ][:_LIST_DECISIONS_MAX]
    existing = [
        item
        for item in context.existing_resolution_projection
        if not label or label in str(item.get("source_label") or "")
    ][:_LIST_DECISIONS_MAX]
    if not known and not prior and not existing:
        return {"message": "未找到匹配的已登记决议。" if label else "本批暂无任何已登记决议。"}
    return {"known_decisions": known, "prior_functional_groups": prior, "existing_resolutions": existing}


# ---------------------------------------------------------------------------
# 工具 schema 与调度
# ---------------------------------------------------------------------------


def _function_tool(
    name: str, description: str, properties: dict[str, Any], required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _investigation_tool_specs() -> list[dict[str, Any]]:
    return [
        _function_tool(
            "search_source",
            "核实一句逐字引文是否真的出现在本集原文或后续章节，返回命中处的上下文。"
            "想核实某句话是否真在原文出现、或想看清某个称谓的具体语境时调用。",
            {"quote": {"type": "string", "description": "要核实的逐字引文或短语"}},
            ["quote"],
        ),
        _function_tool(
            "lookup_bible",
            "按姓名或称谓查询人物谱与已登记身份权威，返回别名、外观摘要与权威绑定。"
            "某个称谓拿不准对应人物谱哪一位真名时调用。",
            {"name": {"type": "string", "description": "要查询的姓名、称谓或别名"}},
            ["name"],
        ),
        _function_tool(
            "list_decisions",
            "按称谓子串过滤已登记的身份决议（本批 K 决议、前批 functional 分组、"
            "本集已有功能身份决议）。想确认某个称谓是否已有决议、避免重复申报时调用；"
            "留空返回全部（数量有截断）。",
            {"label": {"type": "string", "description": "称谓子串，留空返回全部"}},
            [],
        ),
    ]


def _record_hit(stats: dict, tool: str, hit: bool) -> None:
    bucket = stats.setdefault(tool, {"hit": 0, "miss": 0})
    bucket["hit" if hit else "miss"] += 1


def _dispatch_tool(context: InvestigationContext, name: str, arguments: dict, stats: dict) -> dict:
    stats["tool_calls"] = stats.get("tool_calls", 0) + 1
    if name == "search_source":
        result = search_source(context, str(arguments.get("quote") or ""))
        _record_hit(stats, name, "hits" in result)
        return result
    if name == "lookup_bible":
        result = lookup_bible(context, str(arguments.get("name") or ""))
        _record_hit(stats, name, "message" not in result)
        return result
    if name == "list_decisions":
        result = list_decisions(context, str(arguments.get("label") or ""))
        _record_hit(stats, name, "message" not in result)
        return result
    return {"error": f"未知工具：{name}"}


# ---------------------------------------------------------------------------
# Phase A：有界调查循环
# ---------------------------------------------------------------------------


def _assistant_message_with_tool_calls(turn: hiagent.AssistantTurn) -> dict[str, Any]:
    """与 app.agent.orchestrator._assistant_message_with_tool_calls 同构；
    app.agent 是 L5、本包是 L4，不能反向 import，纯函数在此重新实现一份。"""
    return {
        "role": "assistant",
        "content": turn.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.arguments_raw
                    if tc.arguments_raw is not None
                    else json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in turn.tool_calls
        ],
    }


def _build_initial_messages(base_prompt: str) -> list[dict]:
    instruction = (
        "先调查，再作答：本轮你只需要按需调用工具核实信息，调查完成后用一段中文文字"
        "写下调查笔记（哪些称谓核实了什么、依据是什么）；不要在这一步输出最终结构化"
        "结果，最终作答会在下一步单独请求。\n\n" + base_prompt
    )
    return [
        {"role": "system", "content": _INVESTIGATION_SYSTEM_PROMPT},
        {"role": "user", "content": instruction},
    ]


def _gate_feedback_message(errors: str) -> dict:
    return {
        "role": "user",
        "content": f"后端校验未通过：{errors}。请继续调查（可再调用工具），在笔记里写明你准备如何修正。",
    }


async def _chat_with_tools(messages: list[dict], tools: list[dict], **kwargs: Any) -> hiagent.AssistantTurn:
    """Phase A 唯一的网关出口。tests/conftest.py 的 autouse 桩替换的是这个名字，
    不是 ``hiagent.chat_with_tools`` 本体——后者有自己的契约测试
    （tests/test_chat_with_tools.py、test_reasoning_token_budget.py）测的就是真函数，
    全局替换会让它们静默测到桩子。"""
    return await hiagent.chat_with_tools(messages, tools, **kwargs)


async def _run_phase_a(
    messages: list[dict], context: InvestigationContext, gate_round: int, stats: dict,
) -> tuple[str, list[dict]]:
    """有界工具调查：每轮最多执行 IDENTITY_INVESTIGATION_MAX_TOOL_CALLS_PER_ROUND 个
    tool_calls，多余的忽略并在结果里说明；模型不再请求工具即结束，content 就是笔记。"""
    tool_records: list[dict] = []
    call_meta = {
        "stage": "discover_character_candidates",
        "stage_key": "screenplay_character_discovery",
        "substage": "current_identity_investigation",
        "episode_no": context.episode_no,
        "source_batch": context.current_batch,
        "gate_round": gate_round,
    }
    for _round in range(IDENTITY_INVESTIGATION_MAX_TOOL_ROUNDS):
        turn = await _chat_with_tools(
            messages, _investigation_tool_specs(), temperature=0.1, call_meta=call_meta,
        )
        if not turn.tool_calls:
            return turn.content or "", tool_records
        messages.append(_assistant_message_with_tool_calls(turn))
        budget = IDENTITY_INVESTIGATION_MAX_TOOL_CALLS_PER_ROUND
        for tc in turn.tool_calls[:budget]:
            result = _dispatch_tool(context, tc.name, tc.arguments, stats)
            tool_records.append({"tool": tc.name, "arguments": tc.arguments, "result": result})
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        for tc in turn.tool_calls[budget:]:
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(
                    {"skipped": "本轮工具调用数超过上限，未执行"}, ensure_ascii=False
                ),
            })
    return "", tool_records


def _phase_b_prompt(base_prompt: str, notes: str, tool_records: list[dict]) -> str:
    """调查为空（无工具调用且笔记为空）时原样返回，保证 prompt 字节不变。"""
    if not notes.strip() and not tool_records:
        return base_prompt
    record = {"investigation_notes": notes, "tool_results": tool_records}
    return (
        f"{base_prompt}\n\n调查记录（以下工具结果由后端执行、可信；调查笔记是你上一步写的）：\n"
        + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    )


def _log_investigation_stats(context: InvestigationContext, gate_round: int, stats: dict) -> None:
    detail = {k: v for k, v in stats.items() if k != "tool_calls"}
    logger.info(
        "identity_investigation episode=%s batch=%s gate_rounds=%s tool_calls=%s detail=%s",
        context.episode_no, context.current_batch, gate_round + 1,
        stats.get("tool_calls", 0), detail,
    )


# ---------------------------------------------------------------------------
# 主入口：Phase A + Phase B + 闸门回喂循环
# ---------------------------------------------------------------------------

PhaseB = Callable[[str, int], Awaitable[tuple[list[dict], list[str]]]]


async def resolve_with_investigation_gate(
    base_prompt: str, *, context: InvestigationContext, phase_b: PhaseB,
) -> list[dict]:
    """调用方 ``phase_b(prompt_text, gate_round)`` 负责真正的严格作答 + 业务校验：
    成功时返回 ``(candidates, [])``；业务语义校验失败时返回 ``(_, errors)``（不
    raise，交给这里决定是否重试）；纯 schema 越界必须由 ``phase_b`` 自行
    ``raise model_gateway.StructuredFormatError``，不进入这个循环（见模块 docstring）。
    功能关闭时跳过调查与回喂，只调用一次 ``phase_b``，行为与关闭前完全一致。
    """
    if not _identity_investigation_enabled():
        result, errors = await phase_b(base_prompt, 0)
        if errors:
            raise ContentGenerationError("；".join(errors))
        return result
    messages = _build_initial_messages(base_prompt)
    stats: dict[str, Any] = {"tool_calls": 0}
    round_errors: list[str] = []
    feedback = ""
    for gate_round in range(IDENTITY_GATE_ROUNDS + 1):
        if feedback:
            messages.append(_gate_feedback_message(feedback))
        notes, tool_records = await _run_phase_a(messages, context, gate_round, stats)
        prompt_text = _phase_b_prompt(base_prompt, notes, tool_records)
        result, errors = await phase_b(prompt_text, gate_round)
        if not errors:
            _log_investigation_stats(context, gate_round, stats)
            return result
        feedback = "；".join(errors)
        round_errors.append(f"第 {gate_round + 1} 轮：{feedback}")
    _log_investigation_stats(context, IDENTITY_GATE_ROUNDS, stats)
    raise ContentGenerationError("；".join(round_errors))
