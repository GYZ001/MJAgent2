"""区间外个别弃置台词的 ``beat_id`` 核验（2026-09-24）：
``restore_dropped_lines_with_invalid_beat``——beat_id 必须存在、所属节拍
``importance="optional"``、且台词的原文段号落在该节拍 ``segment_indexes``
内，任一不满足确定性放回 ``kept_lines``；随原文区间强制弃置
（``_SPAN_DROP_REASON_PREFIX``）与语气词/屏上文字两类不受这条新规则约束。

从 ``tests/test_storyboard_short_drama.py`` 拆出的同一批改造（那个文件自己
的 500 行棘轮已无余量，见其模块 docstring），``_draft``/``_range_plan``/
``_sources`` 复用同一份夹具——与 ``tests/test_storyboard_short_drama_
evidence.py`` 从 ``tests.test_storyboard_pack`` 借夹具同一种既有写法。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_beat_sheet import _validate_beat_sheet_draft
from app.production.storyboard_dialogue_ledger import DialogueQuote, _AiDroppedLine
from app.production.storyboard_short_drama import _SPAN_DROP_REASON_PREFIX
from app.production.storyboard_short_drama_beat_guard import restore_dropped_lines_with_invalid_beat
from app.production.storyboard_short_drama_schemas import _AiShortDramaBeat
from tests.test_storyboard_short_drama import _draft, _range_plan, _sources

_KEY_BEAT = _AiShortDramaBeat(beat_id="B1", summary="主线", segment_indexes=[1], importance="key")
_OPTIONAL_BEAT = _AiShortDramaBeat(beat_id="B2", summary="寒暄", segment_indexes=[1], importance="optional")
_OTHER_SEGMENT_OPTIONAL_BEAT = _AiShortDramaBeat(beat_id="B3", summary="寒暄2", segment_indexes=[2], importance="optional")


def _quote(**overrides) -> DialogueQuote:
    fields = {"quote_id": "Q1", "source_segment_index": 1, "text": "句一", "content_chars": 8, "speaker": "老王"}
    fields.update(overrides)
    return DialogueQuote(**fields)


def _draft_with(dropped_lines, *, beat_sheet):
    sources = _sources("句一。句二。句三。", "另一段。")
    plan = _range_plan(1, 1, 1, 3, beat_ids=[b.beat_id for b in beat_sheet])
    return sources, _draft([plan], beat_sheet=beat_sheet, dropped_lines=dropped_lines)


# ---------------------------------------------------------------------------
# 三条核验各自单独触发放回
# ---------------------------------------------------------------------------

def test_restores_when_beat_id_missing_or_points_to_unknown_beat():
    quote = _quote()
    sources, draft = _draft_with(
        [{"quote_id": "Q1", "reason": "不重要", "beat_id": "B_NOT_EXIST"}], beat_sheet=[_KEY_BEAT, _OPTIONAL_BEAT],
    )
    notes = restore_dropped_lines_with_invalid_beat(draft, [quote], sources, adaptation_mode="short_drama")
    assert notes and "beat_id 缺失或指向不存在的节拍" in notes[0]
    assert draft.dropped_lines == []
    assert [(k.quote_id, k.segment_no) for k in draft.kept_lines] == [("Q1", 1)]


def test_restores_when_beat_is_key_not_optional():
    """key 节拍的台词不许靠 dropped_lines 弃置——即使 beat_id 真实存在。"""
    quote = _quote()
    sources, draft = _draft_with(
        [{"quote_id": "Q1", "reason": "不重要", "beat_id": "B1"}], beat_sheet=[_KEY_BEAT, _OPTIONAL_BEAT],
    )
    notes = restore_dropped_lines_with_invalid_beat(draft, [quote], sources, adaptation_mode="short_drama")
    assert notes and "不是 optional" in notes[0]
    assert draft.dropped_lines == []
    assert [(k.quote_id, k.segment_no) for k in draft.kept_lines] == [("Q1", 1)]


def test_restores_when_beat_does_not_cover_the_quotes_source_segment():
    """beat_id 指向的节拍是 optional，但它的 segment_indexes 不含这句台词的
    原文段号——说明模型在瞎填一个存在的 beat_id 应付 schema 必填。"""
    quote = _quote(source_segment_index=1)
    sources, draft = _draft_with(
        [{"quote_id": "Q1", "reason": "不重要", "beat_id": "B3"}], beat_sheet=[_KEY_BEAT, _OTHER_SEGMENT_OPTIONAL_BEAT],
    )
    notes = restore_dropped_lines_with_invalid_beat(draft, [quote], sources, adaptation_mode="short_drama")
    assert notes and "没有覆盖" in notes[0]
    assert draft.dropped_lines == []
    assert [(k.quote_id, k.segment_no) for k in draft.kept_lines] == [("Q1", 1)]


def test_keeps_dropped_when_beat_is_optional_and_covers_segment():
    """三条核验全部满足：保留弃置，不打扰模型的正当决定。"""
    quote = _quote()
    sources, draft = _draft_with(
        [{"quote_id": "Q1", "reason": "与主线无关的寒暄，画面已能交代", "beat_id": "B2"}],
        beat_sheet=[_KEY_BEAT, _OPTIONAL_BEAT],
    )
    notes = restore_dropped_lines_with_invalid_beat(draft, [quote], sources, adaptation_mode="short_drama")
    assert notes == []
    assert [d.quote_id for d in draft.dropped_lines] == ["Q1"]
    assert draft.kept_lines == []


# ---------------------------------------------------------------------------
# 不受影响的两类：区间强制弃置、语气词/屏上文字
# ---------------------------------------------------------------------------

def test_span_dropped_lines_are_unaffected_even_without_beat_id():
    """随原文区间强制弃置的台词由代码直接构造基类 _AiDroppedLine（没有
    beat_id 属性）——reason 前缀就是判据，不受这条新规则影响，即使
    beat_sheet 里没有任何合法 optional 节拍。"""
    quote = _quote()
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 3, beat_ids=["B1"])
    draft = _draft([plan], beat_sheet=[_KEY_BEAT])
    draft.dropped_lines = [_AiDroppedLine(quote_id="Q1", reason=f"{_SPAN_DROP_REASON_PREFIX}闲笔")]
    notes = restore_dropped_lines_with_invalid_beat(draft, [quote], sources, adaptation_mode="short_drama")
    assert notes == []
    assert [d.quote_id for d in draft.dropped_lines] == ["Q1"]
    assert draft.kept_lines == []


def test_filler_word_is_unaffected_by_invalid_beat_id():
    """语气词（content_chars <= DROPPABLE_MAX_CHARS）一直都能无条件弃置，不
    受这条新规则约束——即使没有 beat_id 属性（SimpleNamespace 模拟旧路径
    直接构造的条目）。"""
    quote = _quote(text="啊", content_chars=1)
    sources = _sources("句一。句二。句三。")
    plan = _range_plan(1, 1, 1, 3, beat_ids=["B1"])
    draft = _draft([plan], beat_sheet=[_KEY_BEAT])
    draft.dropped_lines = [SimpleNamespace(quote_id="Q1", reason="语气词")]
    notes = restore_dropped_lines_with_invalid_beat(draft, [quote], sources, adaptation_mode="short_drama")
    assert notes == []
    assert len(draft.dropped_lines) == 1


def test_quote_not_found_is_left_alone():
    sources, draft = _draft_with(
        [{"quote_id": "Q_UNKNOWN", "reason": "不重要", "beat_id": "B_NOT_EXIST"}], beat_sheet=[_KEY_BEAT, _OPTIONAL_BEAT],
    )
    notes = restore_dropped_lines_with_invalid_beat(draft, [], sources, adaptation_mode="short_drama")
    assert notes == []
    assert [d.quote_id for d in draft.dropped_lines] == ["Q_UNKNOWN"]


# ---------------------------------------------------------------------------
# 忠实档无副作用
# ---------------------------------------------------------------------------

def test_noop_for_faithful_mode():
    quote = _quote()
    sources, draft = _draft_with(
        [{"quote_id": "Q1", "reason": "不重要", "beat_id": "B_NOT_EXIST"}], beat_sheet=[_KEY_BEAT, _OPTIONAL_BEAT],
    )
    notes = restore_dropped_lines_with_invalid_beat(draft, [quote], sources, adaptation_mode="faithful")
    assert notes == []
    assert [d.quote_id for d in draft.dropped_lines] == ["Q1"], "忠实档不触碰 dropped_lines"
    assert draft.kept_lines == []


# ---------------------------------------------------------------------------
# S3 遗留边角（2026-09-24）：调用时机挪到 append_segments_for_uncovered_sources
# 之后——模型整段原文漏排时，覆盖它的段要等补段之后才存在
# ---------------------------------------------------------------------------

def test_validate_beat_sheet_draft_restores_invalid_beat_line_after_appending_missing_segment():
    """原文段 2 被模型整段漏排（``draft.segments`` 只覆盖原文段 1），其中一条
    台词被弃置且 beat_id 指向不存在的节拍。补段之前 ``_find_covering_segment_
    no`` 找不到覆盖段，只有调用顺序挪到 ``append_segments_for_uncovered_
    sources`` 之后才能找到刚补出来的段，把台词放回 kept_lines（不再卡在
    dropped_lines 里出不来）。"""
    sources = _sources("句一。", "这句台词很重要。")
    plan = _range_plan(1, 1, 1, 1)
    beat_sheet = [_AiShortDramaBeat(beat_id="B1", summary="x", segment_indexes=[1], importance="optional")]
    quote = DialogueQuote(
        quote_id="Q1", source_segment_index=2, text="这句台词很重要。", content_chars=8, speaker="老王",
    )
    draft = _draft(
        [plan], beat_sheet=beat_sheet,
        dropped_lines=[{"quote_id": "Q1", "reason": "模型瞎填的理由", "beat_id": "B_NOT_EXIST"}],
    )
    errors = _validate_beat_sheet_draft(draft, source_segments=sources, dialogue_quotes=[quote], adaptation_mode="short_drama")
    assert errors == []
    assert [d.quote_id for d in draft.dropped_lines] == [], "无效 beat_id 的台词不应继续卡在 dropped_lines 里"
    assert [k.quote_id for k in draft.kept_lines] == ["Q1"], "补段之后必须能定位覆盖段，放回 kept_lines"
