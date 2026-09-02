"""节拍表台账：台词的原文段没有任何段覆盖时，报错必须给出唯一可行的修法——新增段落。

ERR-20260902-b2db9f（《神墓》第一集分镜台）：模型只切了 1 段（覆盖原文段 1），却要安置
分布在 7 个原文段的 46 句台词。旧报错只说「必须分到覆盖它原文段号的那个段」——那个段
不存在；三轮修复模型都在虚构 segment_no=2/3，最终整集失败。
"""
from __future__ import annotations

from app.production.storyboard_dialogue_ledger import (
    DialogueQuote,
    _AiKeptLine,
    dialogue_ledger_errors,
)


def _quote(quote_id: str, segment_index: int) -> DialogueQuote:
    return DialogueQuote(quote_id=quote_id, source_segment_index=segment_index, text="台词", content_chars=2)


def _errors(kept: list[_AiKeptLine], quotes: list[DialogueQuote], segments: dict[int, list[int]]) -> list[str]:
    return dialogue_ledger_errors(
        quotes=quotes, kept_lines=kept, dropped_lines=[], segment_source_indexes=segments,
        max_chars_per_segment=54, include_capacity=False,
    )


def test_uncovered_source_segment_tells_model_to_add_a_segment() -> None:
    errors = _errors(
        kept=[_AiKeptLine(quote_id="Q01", segment_no=2), _AiKeptLine(quote_id="Q02", segment_no=1)],
        quotes=[_quote("Q01", 2), _quote("Q02", 2)],
        segments={1: [1]},
    )
    assert len(errors) == 1, errors
    message = errors[0]
    assert "原文段 2 的必保台词 ['Q01', 'Q02'] 没有任何段覆盖它" in message
    assert "新增一段、source_segment_indexes 含 2" in message
    assert "不受「保持其余已验证字段不变」的限制" in message


def test_covered_elsewhere_points_to_the_covering_segment() -> None:
    errors = _errors(
        kept=[_AiKeptLine(quote_id="Q01", segment_no=1), _AiKeptLine(quote_id="Q02", segment_no=99)],
        quotes=[_quote("Q01", 3), _quote("Q02", 3)],
        segments={1: [1], 2: [2, 3]},
    )
    assert len(errors) == 2
    drift, unknown = errors
    assert "跨段漂移" in drift and "覆盖它原文段号的是第 [2] 段" in drift
    assert "不存在的 segment_no=99" in unknown and "第 [2] 段" in unknown


def test_correct_binding_has_no_errors() -> None:
    assert _errors(
        kept=[_AiKeptLine(quote_id="Q01", segment_no=2)],
        quotes=[_quote("Q01", 3)],
        segments={1: [1], 2: [2, 3]},
    ) == []
