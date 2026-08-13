from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.source_excerpt import index_source_segments
from app.source_facts import (
    SourceFactQuotationError,
    source_facts,
    source_segment_facts,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "blueprint_latest_three_failures.json"
)


def test_real_src0003_unit016_keeps_key_and_closing_quote() -> None:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][0]
    action_text = case["source_facts"][0]["text"]
    quoted_text = case["source_facts"][1]["text"]
    prior_units = "".join(
        f"前情动作{index}。“前情对白{index}。”"
        for index in range(1, 8)
    )
    source = (
        f"{prior_units}{action_text}\n{quoted_text}"
        "孟浩顺着藤条问道。"
    )
    question_end = source.index("？”") + 1

    segments = index_source_segments(source, max_chars=question_end)
    facts = source_segment_facts("SRC0003", segments[0].text)

    assert segments[0].text.endswith("？”")
    assert facts[-1].source_unit_key == "SRC0003:unit:016"
    assert facts[-1].projection == "quoted"
    assert facts[-1].surface_form == "quoted_span"
    assert facts[-1].text == quoted_text


def test_source_split_extends_only_to_structural_quote_close() -> None:
    source = "前情。" * 10 + "“跨越候选切点的问题？”闭引号后动作。"
    question_end = source.index("？”") + 1

    segments = index_source_segments(source, max_chars=question_end)

    assert segments[0].text.endswith("？”")
    assert segments[1].text == "闭引号后动作。"
    assert [
        fact.projection
        for fact in source_segment_facts("SRC0001", segments[0].text)
    ] == ["action", "quoted"]


def test_unclosed_quote_is_reported_instead_of_coerced_to_action() -> None:
    source = "前置动作。“确有开引号但来源已经结束"

    with pytest.raises(
        SourceFactQuotationError,
        match=r"\[SOURCE_FACT_QUOTE_UNCLOSED\].*SRC0001.*offset=",
    ):
        source_facts(source)


def test_closing_quote_does_not_open_a_new_quoted_span() -> None:
    facts = source_segment_facts("SRC0004", "”闭引号后的叙述继续。")

    assert len(facts) == 1
    assert facts[0].projection == "action"
