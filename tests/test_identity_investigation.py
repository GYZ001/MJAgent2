"""app.portraits.identity_investigation：三个只读工具、Phase A 有界调查循环、
Phase B prompt 拼接与闸门回喂循环、以及关闭开关的行为。

三个工具的纯函数测试直接调用 search_source/lookup_bible/list_decisions（不经
模型）；Phase A/闸门回喂测试用假的 ``hiagent.chat_with_tools`` 驱动
``_run_phase_a``/``resolve_with_investigation_gate``，不触碰真实网络。
"""
from __future__ import annotations

import asyncio

import pytest

from app import hiagent
from app.errors import ContentGenerationError
from app.portraits import identity_investigation as ii
from app.schemas.character import Character, CharacterAlias
from app.schemas.world import Bible, World


def _bible(characters: list[Character]) -> Bible:
    return Bible(characters=characters, world=World(visual_style_canonical="写实"))


def _character(name: str, *, aliases=None, appearance: str = "") -> Character:
    return Character(
        name=name, role="配角", appearance_canonical=appearance, aliases=aliases or [],
    )


def _context(**overrides) -> ii.InvestigationContext:
    defaults = dict(
        episode_no=1, current_batch=1, current_haystack="", future_text="",
        future_label="", bible=_bible([]),
    )
    defaults.update(overrides)
    return ii.InvestigationContext(**defaults)


# ---------------------------------------------------------------------------
# 工具 1：search_source
# ---------------------------------------------------------------------------


def test_normalize_whitespace_collapses_runs_and_strips():
    assert ii._normalize_whitespace("  老者\n\t抬头   说道 ") == "老者 抬头 说道"


def test_search_source_hits_with_context_and_source_tag():
    context = _context(current_haystack="前情提要。老者缓缓抬头，说道：你是何人？少年答道。")
    result = ii.search_source(context, "你是何人")
    assert "message" not in result
    assert result["hits"][0]["source"] == "current"
    assert "你是何人" in result["hits"][0]["context"]


def test_search_source_normalizes_whitespace_before_matching():
    context = _context(current_haystack="  老者\n\t说道：你是何人？   ")
    result = ii.search_source(context, "你是何人")
    assert result["hits"], "首尾空白归一后应能命中"


def test_search_source_miss_reports_not_found_without_fuzzy_fallback():
    context = _context(current_haystack="完全不相关的一段原文")
    result = ii.search_source(context, "从未出现过的引文")
    assert "hits" not in result
    assert "未找到" in result["message"]


def test_search_source_falls_back_to_future_text_when_absent_in_current():
    context = _context(
        current_haystack="本集原文，没有真名。",
        future_text="后续章节里揭晓：他本名叫许清。",
        future_label="第2集",
    )
    result = ii.search_source(context, "他本名叫许清")
    assert result["hits"][0]["source"] == "future"
    assert "future" in result["note"] or "未出场" in result["note"]


# ---------------------------------------------------------------------------
# 工具 2：lookup_bible
# ---------------------------------------------------------------------------


def test_lookup_bible_matches_by_alias_and_returns_authority():
    alias = CharacterAlias(
        text="许师姐", name_kind="honorific",
        evidence_chapter_index=1, evidence_quote="许师姐来了",
    )
    character = _character("许清", aliases=[alias], appearance="青衣少女，束发")
    context = _context(
        bible=_bible([character]),
        current_authorities=[{
            "canonical_name": "许清", "source_labels": ["许清", "许师姐"],
            "identity_kind": "named", "authority_id": "bible:许清",
        }],
    )
    result = ii.lookup_bible(context, "许师姐")
    assert result["bible_characters"][0]["name"] == "许清"
    assert result["bible_characters"][0]["aliases"][0]["text"] == "许师姐"
    assert result["authorities"][0]["authority_id"] == "bible:许清"


def test_lookup_bible_unregistered_name_reports_message():
    context = _context()
    result = ii.lookup_bible(context, "从未登记的人")
    assert "message" in result
    assert "未收录" in result["message"]


def test_lookup_bible_empty_name_is_rejected_without_scanning():
    context = _context(bible=_bible([_character("许清")]))
    result = ii.lookup_bible(context, "")
    assert "message" in result


# ---------------------------------------------------------------------------
# 工具 3：list_decisions
# ---------------------------------------------------------------------------


def test_list_decisions_filters_by_label_substring():
    context = _context(known_decision_projection=[
        {"decision_id": "K1", "decision_type": "prior_named", "source_label": "许师姐",
         "canonical_name": "许清", "allowed_kinds": ["onscreen"]},
        {"decision_id": "K2", "decision_type": "prior_named", "source_label": "老者",
         "canonical_name": "王伯", "allowed_kinds": ["mentioned"]},
    ])
    result = ii.list_decisions(context, "许")
    assert [d["decision_id"] for d in result["known_decisions"]] == ["K1"]


def test_list_decisions_empty_label_returns_all_but_capped():
    projection = [{"decision_id": f"K{i}", "source_label": f"称谓{i}"} for i in range(30)]
    context = _context(known_decision_projection=projection)
    result = ii.list_decisions(context, "")
    assert len(result["known_decisions"]) == ii._LIST_DECISIONS_MAX


def test_list_decisions_no_match_reports_message():
    context = _context(known_decision_projection=[
        {"decision_id": "K1", "source_label": "老者"},
    ])
    result = ii.list_decisions(context, "从不存在的称谓")
    assert "message" in result


# ---------------------------------------------------------------------------
# Phase A：有界调查循环
# ---------------------------------------------------------------------------


def test_run_phase_a_executes_tool_call_then_concludes(monkeypatch):
    responses = iter([
        hiagent.AssistantTurn(
            content="", tool_calls=[hiagent.ToolCall(
                id="call_1", name="search_source", arguments={"quote": "你是何人"},
            )],
        ),
        hiagent.AssistantTurn(content="调查笔记：核实了称谓来源。", tool_calls=[]),
    ])

    async def fake_chat_with_tools(_messages, _tools, *, temperature, call_meta):
        return next(responses)

    monkeypatch.setattr(ii, "_chat_with_tools", fake_chat_with_tools)
    context = _context(current_haystack="老者道：你是何人？")
    messages = ii._build_initial_messages("原始 prompt")
    stats: dict = {"tool_calls": 0}

    notes, tool_records = asyncio.run(ii._run_phase_a(messages, context, 0, stats))

    assert notes == "调查笔记：核实了称谓来源。"
    assert len(tool_records) == 1 and tool_records[0]["tool"] == "search_source"
    # 消息序列形状：assistant 带 tool_calls -> 紧跟一条 role=tool 结果。
    assistant_msg = messages[2]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "search_source"
    tool_msg = messages[3]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1"
    assert stats["tool_calls"] == 1


def test_run_phase_a_stops_at_max_rounds_when_model_keeps_calling_tools(monkeypatch):
    seen = {"n": 0}

    async def fake_chat_with_tools(_messages, _tools, *, temperature, call_meta):
        seen["n"] += 1
        return hiagent.AssistantTurn(content="", tool_calls=[
            hiagent.ToolCall(id=f"call_{seen['n']}", name="list_decisions", arguments={}),
        ])

    monkeypatch.setattr(ii, "_chat_with_tools", fake_chat_with_tools)
    context = _context()
    messages = ii._build_initial_messages("prompt")
    notes, tool_records = asyncio.run(ii._run_phase_a(messages, context, 0, {"tool_calls": 0}))

    assert seen["n"] == ii.IDENTITY_INVESTIGATION_MAX_TOOL_ROUNDS
    assert notes == ""
    assert len(tool_records) == ii.IDENTITY_INVESTIGATION_MAX_TOOL_ROUNDS


def test_run_phase_a_skips_tool_calls_beyond_per_round_budget(monkeypatch):
    responses = iter([
        hiagent.AssistantTurn(content="", tool_calls=[
            hiagent.ToolCall(id=letter, name="list_decisions", arguments={})
            for letter in ("a", "b", "c", "d")
        ]),
        hiagent.AssistantTurn(content="收工", tool_calls=[]),
    ])

    async def fake_chat_with_tools(_messages, _tools, *, temperature, call_meta):
        return next(responses)

    monkeypatch.setattr(ii, "_chat_with_tools", fake_chat_with_tools)
    context = _context()
    messages = ii._build_initial_messages("prompt")
    stats: dict = {"tool_calls": 0}
    notes, _tool_records = asyncio.run(ii._run_phase_a(messages, context, 0, stats))

    assert notes == "收工"
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    skipped = [m for m in tool_messages if "未执行" in m["content"]]
    assert len(skipped) == 1
    assert stats["tool_calls"] == ii.IDENTITY_INVESTIGATION_MAX_TOOL_CALLS_PER_ROUND


# ---------------------------------------------------------------------------
# Phase B：prompt 拼接
# ---------------------------------------------------------------------------


def test_phase_b_prompt_byte_identical_when_investigation_empty():
    assert ii._phase_b_prompt("原始 prompt", "", []) == "原始 prompt"


def test_phase_b_prompt_appends_tool_results_when_notes_present():
    record = [{"tool": "search_source", "arguments": {"quote": "x"}, "result": {"message": "未找到"}}]
    result = ii._phase_b_prompt("原始 prompt", "调查笔记内容", record)
    assert result.startswith("原始 prompt")
    assert "调查记录" in result
    assert "调查笔记内容" in result
    assert "search_source" in result


# ---------------------------------------------------------------------------
# 闸门回喂循环
# ---------------------------------------------------------------------------


def _quiet_chat_with_tools(monkeypatch, *, capture: list | None = None):
    async def fake(messages, _tools, *, temperature, call_meta):
        if capture is not None:
            capture.append([dict(m) for m in messages])
        return hiagent.AssistantTurn(content="", tool_calls=[])

    monkeypatch.setattr(ii, "_chat_with_tools", fake)


def test_gate_loop_feeds_error_back_into_phase_a_and_succeeds_second_round(monkeypatch):
    seen_batches: list = []
    _quiet_chat_with_tools(monkeypatch, capture=seen_batches)
    attempts = {"n": 0}

    async def phase_b(_prompt_text, _gate_round):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return [], ["current 投影后同一 source_label 冲突：许师姐"]
        return [{"name": "许清"}], []

    context = _context()
    result = asyncio.run(
        ii.resolve_with_investigation_gate("原始 prompt", context=context, phase_b=phase_b)
    )

    assert result == [{"name": "许清"}]
    assert attempts["n"] == 2
    second_round_messages = seen_batches[1]
    assert any(
        "同一 source_label 冲突" in str(m.get("content", "")) for m in second_round_messages
    )


def test_gate_loop_exhausts_all_rounds_and_reports_each_round(monkeypatch):
    _quiet_chat_with_tools(monkeypatch)

    async def phase_b(_prompt_text, gate_round):
        return [], [f"错误详情{gate_round}"]

    context = _context()
    with pytest.raises(ContentGenerationError) as excinfo:
        asyncio.run(
            ii.resolve_with_investigation_gate("原始 prompt", context=context, phase_b=phase_b)
        )
    message = str(excinfo.value)
    assert "第 1 轮" in message
    assert f"第 {ii.IDENTITY_GATE_ROUNDS + 1} 轮" in message


# ---------------------------------------------------------------------------
# 开关：关闭时跳过 Phase A 与回喂循环
# ---------------------------------------------------------------------------


def test_disabled_setting_never_calls_chat_with_tools(monkeypatch):
    monkeypatch.setattr(ii, "get_setting", lambda _key: "off")
    calls = {"n": 0}

    async def fake_chat_with_tools(*_args, **_kwargs):
        calls["n"] += 1
        return hiagent.AssistantTurn(content="", tool_calls=[])

    monkeypatch.setattr(ii, "_chat_with_tools", fake_chat_with_tools)

    async def phase_b(prompt_text, gate_round):
        assert prompt_text == "原始 prompt"
        assert gate_round == 0
        return [{"name": "候选"}], []

    context = _context()
    result = asyncio.run(
        ii.resolve_with_investigation_gate("原始 prompt", context=context, phase_b=phase_b)
    )
    assert result == [{"name": "候选"}]
    assert calls["n"] == 0


def test_disabled_setting_raises_immediately_without_retry(monkeypatch):
    monkeypatch.setattr(ii, "get_setting", lambda _key: "off")
    attempts = {"n": 0}

    async def phase_b(_prompt_text, _gate_round):
        attempts["n"] += 1
        return [], ["某个校验错误"]

    context = _context()
    with pytest.raises(ContentGenerationError):
        asyncio.run(
            ii.resolve_with_investigation_gate("原始 prompt", context=context, phase_b=phase_b)
        )
    assert attempts["n"] == 1
