from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.source_excerpt import index_source_segments
from app.source_facts import (
    SOURCE_FACT_VERSION,
    SourceFactQuotationError,
    source_facts,
    source_segment_facts,
)


RUN_8FC0D05C3C81_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "run_8fc0d05c3c81_source_units.json"
)


@pytest.mark.parametrize(
    "legacy_unit",
    json.loads(
        RUN_8FC0D05C3C81_FIXTURE.read_text(encoding="utf-8")
    )["legacy_units"],
    ids=lambda value: value["legacy_source_unit_key"],
)
def test_real_run_action_units_split_into_authoritative_clauses(
    legacy_unit: dict,
) -> None:
    facts = source_segment_facts("SRC0003", legacy_unit["text"])

    assert len(facts) == legacy_unit["expected_clause_count"]
    assert [fact.source_unit_key for fact in facts] == [
        f"SRC0003:unit:{index:03d}"
        for index in range(1, len(facts) + 1)
    ]
    assert all(fact.contract_version == SOURCE_FACT_VERSION for fact in facts)
    assert all(fact.projection == "action" for fact in facts)
    fact_texts = [fact.text for fact in facts]
    assert all(
        expected in fact_texts
        for expected in legacy_unit["expected_clauses"]
    )
    assert legacy_unit["text"] not in fact_texts


def test_source_split_extends_only_to_structural_quote_close() -> None:
    source = "前情。" * 10 + "“跨越候选切点的问题？”闭引号后动作。"
    question_end = source.index("？”") + 1

    segments = index_source_segments(source, max_chars=question_end)

    assert segments[0].text.endswith("？”")
    assert segments[1].text == "闭引号后动作。"
    projections = [
        fact.projection
        for fact in source_segment_facts("SRC0001", segments[0].text)
    ]
    assert projections == [*(["action"] * 10), "quoted"]


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


def test_structural_divider_with_only_paratext_has_no_empty_action_unit() -> None:
    facts = source_segment_facts(
        "SRC0005",
        "－－－－－－－－\n作者附记与更新通知。",
    )

    assert [fact.projection for fact in facts] == ["paratext"]
    assert [fact.text for fact in facts] == ["作者附记与更新通知。"]
