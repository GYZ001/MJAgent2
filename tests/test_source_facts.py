from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.source_excerpt import index_source_segments
from app.source_facts import (
    SOURCE_FACT_VERSION,
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


def test_unclosed_quote_in_source_is_closed_deterministically_not_fatal() -> None:
    # 原文作者未闭合的引号（内心独白/口语常见）是来源写法，不是系统故障：
    # 必须确定性收尾为一个 quoted span，绝不因此让整集剧本硬失败，也不丢字。
    source = "前置动作。“确有开引号但来源已经结束"

    facts = source_facts(source)

    assert facts, "未闭合引号不得导致 0 facts 或异常"
    quoted = [fact for fact in facts if fact.projection == "quoted"]
    assert quoted, "未闭合的引号内容应作为 quoted span 保留"
    assert "确有开引号但来源已经结束" in quoted[-1].text
    # 前置动作仍应作为独立 action 单元保留，不被吞并。
    assert any(
        fact.projection == "action" and "前置动作" in fact.text
        for fact in facts
    )


def test_quote_spanning_paragraph_break_is_merged_into_one_balanced_segment() -> None:
    # 一段跨自然段（\n\n）的引文：开引号在前段、闭引号在后段。分段必须把它们并成
    # 一个引号平衡的 segment，避免下游 source fact 抽取把半截引文判为未闭合而崩溃。
    source = (
        "他心中默念。\n\n"
        "“凝气一层可以成为外宗弟子，那抓我来的女人，她是凝气七层。\n\n"
        "这等工钱虽说不是银两，但若能拿出去，必可卖到百金！”孟浩怦然心动。\n\n"
        "马脸青年闭上了眼。"
    )

    segments = index_source_segments(source)

    monologue = next(
        segment for segment in segments
        if "凝气一层可以成为外宗弟子" in segment.text
    )
    assert monologue.text.count("“") == monologue.text.count("”") == 1
    # 合并后的 segment 能正常抽取 source facts，不抛异常。
    facts = source_segment_facts(monologue.segment_id, monologue.text)
    assert any(fact.projection == "quoted" for fact in facts)
    # 全量抽取也不抛异常，且覆盖所有自然段。
    assert source_facts(source)


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


@pytest.mark.parametrize("divider", ["－－－－－－－－", "--------\n"])
def test_structural_divider_without_body_or_paratext_has_no_fact(
    divider: str,
) -> None:
    assert source_segment_facts("SRC0005", divider) == []
