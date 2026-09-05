"""app.production.storyboard_segment_ranges——分镜台 2.4.0 句单元切分与跨段
引用范围（真实故障，2026-09-03「橘座在上」EP1：节拍表把一个 761 字、无空行
的原文段拆成 6 段，但 6 段的 source_segment_indexes 全是同一个原文段号，只靠
节拍摘要区分该拍哪一块，猫跳上桌/黄总抓猫等情节被反复重拍）。

覆盖：句单元切分的确定性、6 段共享一个原文段时的合法/回退/留洞三种形态、
paratext 段的排除、kept 台词的单元绑定、阶段二原文载荷的范围裁剪与衔接
上下文。不覆盖真实供应商往返（本模块无模型调用）。
"""
from __future__ import annotations

from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_segment_ranges import (
    _PARATEXT_PLACEHOLDER_TEXT,
    kept_line_unit_binding_errors,
    quote_unit_index,
    render_source_units,
    segment_source_payload,
    segment_unit_range_errors,
    split_source_units,
)
from app.source_excerpt import SourceSegment


# ---------------------------------------------------------------------------
# split_source_units
# ---------------------------------------------------------------------------

def test_split_source_units_splits_on_terminal_punctuation():
    text = "他走了。她哭了！真的假的？"
    units = split_source_units(text)
    assert [text[s:e] for s, e in units] == ["他走了。", "她哭了！", "真的假的？"]


def test_split_source_units_splits_on_newlines_first():
    text = "第一句没有标点\n第二句。"
    units = split_source_units(text)
    texts = [text[start:end] for start, end in units]
    assert texts == ["第一句没有标点", "第二句。"]


def test_split_source_units_absorbs_trailing_closing_quote_into_previous_sentence():
    text = "他说：「我们走吧。」"
    units = split_source_units(text)
    texts = [text[start:end] for start, end in units]
    # 句末标点后紧跟的闭引号并入前一句，不单独成为下一句的开头。
    assert texts == ["他说：「我们走吧。」"]


def test_split_source_units_drops_whitespace_only_units():
    text = "第一句。\n\n第二句。"
    units = split_source_units(text)
    texts = [text[start:end] for start, end in units]
    assert texts == ["第一句。", "第二句。"]


def test_split_source_units_covers_every_non_whitespace_character():
    """单元覆盖全部非空白字符——把各单元原文拼起来（用空串连接）应该等于原文
    去掉全部空白字符后的结果。"""
    text = "老板走进办公室。\n他看到桌上有一只猫。\n猫忽然跳上了桌子。"
    units = split_source_units(text)
    covered = "".join(text[start:end] for start, end in units)
    expected = "".join(ch for ch in text if not ch.isspace())
    assert covered == expected


def test_split_source_units_trailing_content_without_terminal_punctuation_is_its_own_unit():
    text = "他犹豫了一下，没有说完"
    units = split_source_units(text)
    assert [text[s:e] for s, e in units] == ["他犹豫了一下，没有说完"]


# ---------------------------------------------------------------------------
# render_source_units
# ---------------------------------------------------------------------------

def test_render_source_units_numbers_units_from_one():
    text = "他走了。她哭了！"
    rendered = render_source_units(4, text, None)
    assert rendered == "[段4·S01] 他走了。\n[段4·S02] 她哭了！"


def test_render_source_units_paratext_placeholder_is_a_single_unnumbered_line():
    rendered = render_source_units(7, "求票求收藏，谢谢诸位道友", _PARATEXT_PLACEHOLDER_TEXT)
    assert rendered == f"[段7] {_PARATEXT_PLACEHOLDER_TEXT}"
    assert "·S" not in rendered


# ---------------------------------------------------------------------------
# 真实故障场景：一个原文段（761 字剧本格式的简化重现）被 6 段引用
# ---------------------------------------------------------------------------

_SCENE_4_TEXT = (
    "老板走进办公室。\n"
    "他看到桌上有一只猫。\n"
    "猫忽然跳上了桌子。\n"
    "所有人都愣住了。\n"
    "黄总伸手去抓猫。\n"
    "猫轻巧地躲开了。\n"
    "黄总又扑了一次。\n"
    "这次总算抓住了。\n"
    "他松了一口气。\n"
    "心想项目总算保住了。\n"
    "他喃喃自语。\n"
    "「它居然自己选了这个项目。」"
)


def _seg(text: str, no: int = 1) -> SourceSegment:
    return SourceSegment(segment_id=f"seg{no}", text=text, start_offset=0, end_offset=len(text))


def _scene_4_segments() -> list[SourceSegment]:
    return [
        SourceSegment(segment_id="s1", text="第一章标题", start_offset=0, end_offset=0),
        SourceSegment(segment_id="s2", text="场1-1。", start_offset=0, end_offset=0),
        SourceSegment(segment_id="s3", text="场1-2。", start_offset=0, end_offset=0),
        SourceSegment(segment_id="s4", text=_SCENE_4_TEXT, start_offset=0, end_offset=0),
    ]


def _plan(no: int, from_unit: int, to_unit: int, *, index: int = 4):
    from app.production.storyboard_beat_sheet import _AiSegmentPlan

    return _AiSegmentPlan(
        segment_no=no, synopsis=f"段{no}", source_segment_indexes=[index],
        source_unit_ranges=[{"source_segment_index": index, "from_unit": from_unit, "to_unit": to_unit}],
    )


def test_six_segments_sharing_one_source_segment_contiguous_ranges_pass():
    """还原真实故障的正确解法：6 段各占 761 字段落的一块，首尾相接、不回退、
    并集覆盖全部 12 个句单元（本 fixture 恰好切出 12 个单元）。"""
    assert len(split_source_units(_SCENE_4_TEXT)) == 12
    plans = [_plan(no, frm, to) for no, (frm, to) in enumerate(
        [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 12)], start=1,
    )]
    assert segment_unit_range_errors(plans, _scene_4_segments(), set()) == []


def test_backward_range_between_two_segments_sharing_a_source_segment_is_rejected():
    """段 2 从 S02 开始，倒退回段 1 已经占用过的单元——猫跳上桌被拍两次的
    根本原因。"""
    plans = [_plan(1, 1, 3), _plan(2, 2, 5)]
    errors = segment_unit_range_errors(plans, _scene_4_segments(), set())
    assert any("回退或重叠超过一个单元" in e for e in errors)


def test_gap_between_two_segments_sharing_a_source_segment_is_rejected():
    """段 1 到 S03，段 2 从 S06 开始——S04/S05 没有任何段负责，等于被静默删掉。"""
    plans = [_plan(1, 1, 3), _plan(2, 6, 8)]
    errors = segment_unit_range_errors(plans, _scene_4_segments(), set())
    assert any("S04" in e and "S05" in e and "没有被任何段" in e for e in errors)


def test_shared_boundary_unit_between_adjacent_segments_is_legal():
    """允许恰好共享一个边界单元：段 1 到 S03，段 2 从 S03 开始（不是 S04）。
    只断言不触发"回退/重叠"——覆盖率检查另有专门用例，这里只有 2 段、天然
    覆盖不了全部 12 个单元，不是本用例要测的维度。"""
    plans = [_plan(1, 1, 3), _plan(2, 3, 6)]
    errors = segment_unit_range_errors(plans, _scene_4_segments(), set())
    assert not any("回退或重叠" in e for e in errors)


def test_missing_range_for_a_referenced_non_paratext_segment_is_rejected():
    from app.production.storyboard_beat_sheet import _AiSegmentPlan

    plan = _AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[4], source_unit_ranges=[])
    errors = segment_unit_range_errors([plan], _scene_4_segments(), set())
    assert any("声明了 0 条范围" in e for e in errors)


def test_range_declared_for_an_unreferenced_segment_index_is_rejected():
    from app.production.storyboard_beat_sheet import _AiSegmentPlan

    plan = _AiSegmentPlan(
        segment_no=1, synopsis="x", source_segment_indexes=[2],
        source_unit_ranges=[{"source_segment_index": 4, "from_unit": 1, "to_unit": 1}],
    )
    errors = segment_unit_range_errors([plan], _scene_4_segments(), set())
    assert any("不在本段" in e for e in errors)


def test_range_declared_for_a_paratext_segment_is_rejected():
    from app.production.storyboard_beat_sheet import _AiSegmentPlan

    plan = _AiSegmentPlan(
        segment_no=1, synopsis="x", source_segment_indexes=[1, 4],
        source_unit_ranges=[
            {"source_segment_index": 1, "from_unit": 1, "to_unit": 1},
            {"source_segment_index": 4, "from_unit": 1, "to_unit": 12},
        ],
    )
    # 段号 1 是 paratext：不该为它声明范围。
    errors = segment_unit_range_errors([plan], _scene_4_segments(), {1})
    assert any("不在本段" in e for e in errors)


def test_out_of_bounds_range_is_rejected():
    plan = _plan(1, 1, 99)
    errors = segment_unit_range_errors([plan], _scene_4_segments(), set())
    assert any("不合法" in e and "1 ≤ from_unit ≤ to_unit ≤" in e for e in errors)


# ---------------------------------------------------------------------------
# 小说体：一段一句话被两段引用，共享边界单元
# ---------------------------------------------------------------------------

def test_prose_single_sentence_segment_referenced_by_two_plans_sharing_the_only_unit():
    """小说体一段一句话只有 1 个句单元，两段各自声明 from=to=1 引用同一个
    单元——共享边界的极端情形（整段就是边界），合法。"""
    from app.production.storyboard_beat_sheet import _AiSegmentPlan

    segments = [SourceSegment(segment_id="s1", text="他推开了院门。", start_offset=0, end_offset=0)]
    plans = [
        _AiSegmentPlan(
            segment_no=1, synopsis="a", source_segment_indexes=[1],
            source_unit_ranges=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 1}],
        ),
        _AiSegmentPlan(
            segment_no=2, synopsis="b", source_segment_indexes=[1],
            source_unit_ranges=[{"source_segment_index": 1, "from_unit": 1, "to_unit": 1}],
        ),
    ]
    assert segment_unit_range_errors(plans, segments, set()) == []


# ---------------------------------------------------------------------------
# quote_unit_index
# ---------------------------------------------------------------------------

def test_quote_unit_index_uses_start_offset_when_present():
    text = "他走了。她哭了！"
    # "她哭了！" 起始于 offset 4。
    quote = DialogueQuote(
        quote_id="Q01", source_segment_index=1, text="她哭了", content_chars=3, start_offset=4, end_offset=7,
    )
    assert quote_unit_index(quote, text) == 2


def test_quote_unit_index_falls_back_to_text_search_when_offset_missing():
    text = "他走了。她哭了！"
    quote = DialogueQuote(quote_id="Q01", source_segment_index=1, text="她哭了", content_chars=3)
    assert quote_unit_index(quote, text) == 2


def test_quote_unit_index_returns_negative_one_when_unresolvable():
    text = "他走了。她哭了！"
    quote = DialogueQuote(quote_id="Q01", source_segment_index=1, text="根本不存在的句子", content_chars=8)
    assert quote_unit_index(quote, text) == -1


# ---------------------------------------------------------------------------
# kept_line_unit_binding_errors
# ---------------------------------------------------------------------------

def _kept(quote_id: str, segment_no: int):
    from app.production.storyboard_dialogue_ledger import _AiKeptLine

    return _AiKeptLine(quote_id=quote_id, segment_no=segment_no)


def test_kept_line_within_its_segments_declared_range_passes():
    plans = [_plan(1, 1, 3)]
    quotes = [DialogueQuote(
        quote_id="Q01", source_segment_index=4, text="猫忽然跳上了桌子", content_chars=8,
    )]  # 第 3 句 -> S03，在段 1 的 [1,3] 范围内
    errors = kept_line_unit_binding_errors([_kept("Q01", 1)], quotes, plans, _scene_4_segments())
    assert errors == []


def test_kept_line_outside_its_segments_range_but_covered_by_another_segment_is_rejected():
    plans = [_plan(1, 1, 2), _plan(2, 3, 4)]
    quotes = [DialogueQuote(
        quote_id="Q01", source_segment_index=4, text="猫忽然跳上了桌子", content_chars=8,
    )]  # 第 3 句 -> S03，落在段 2 的范围，但被分给了段 1
    errors = kept_line_unit_binding_errors([_kept("Q01", 1)], quotes, plans, _scene_4_segments())
    assert any("覆盖单元 S03 的是第 2 段" in e for e in errors)


def test_kept_line_with_unresolvable_quote_reports_cannot_locate():
    plans = [_plan(1, 1, 12)]
    quotes = [DialogueQuote(
        quote_id="Q01", source_segment_index=4, text="原文里完全找不到的句子", content_chars=10,
    )]
    errors = kept_line_unit_binding_errors([_kept("Q01", 1)], quotes, plans, _scene_4_segments())
    assert any("定位不到句单元" in e for e in errors)


# ---------------------------------------------------------------------------
# segment_source_payload
# ---------------------------------------------------------------------------

def test_segment_source_payload_only_includes_units_within_own_range():
    plan = _plan(1, 5, 6)
    payload = segment_source_payload(plan, _scene_4_segments(), set())
    assert "[段4·S05]" in payload["source_text_by_segment"]
    assert "[段4·S06]" in payload["source_text_by_segment"]
    assert "[段4·S04]" not in payload["source_text_by_segment"]
    assert "[段4·S07]" not in payload["source_text_by_segment"]


def test_segment_source_payload_context_windows_are_up_to_two_units_each_side():
    plan = _plan(1, 5, 6)
    payload = segment_source_payload(plan, _scene_4_segments(), set())
    before_labels = [line.split("]")[0] for line in payload["context_before"]]
    after_labels = [line.split("]")[0] for line in payload["context_after"]]
    assert before_labels == ["[段4·S03", "[段4·S04"]
    assert after_labels == ["[段4·S07", "[段4·S08"]
    assert payload["context_note"]


def test_segment_source_payload_renders_paratext_segment_as_placeholder():
    plan_index = _plan(1, 1, 1, index=1)
    payload = segment_source_payload(plan_index, _scene_4_segments(), {1})
    assert _PARATEXT_PLACEHOLDER_TEXT in payload["source_text_by_segment"]
    assert "第一章标题" not in payload["source_text_by_segment"]


def test_reassign_kept_line_to_the_segment_whose_range_covers_its_unit():
    """EP1 试验跑实测：Q08 在 S21，模型分给声明 S15-S20 的第 8 段，覆盖 S21 的是第 9 段。"""
    from types import SimpleNamespace

    from app.production.storyboard_dialogue_ledger import DialogueQuote, _AiKeptLine
    from app.production.storyboard_segment_ranges import (
        _AiSourceUnitRange,
        reassign_kept_lines_to_covering_segments,
    )
    from app.source_excerpt import SourceSegment

    text = "\n".join(f"第{i}句。" for i in range(1, 25))
    segments = [SourceSegment(segment_id="SRC0001", text=text, start_offset=0, end_offset=len(text))]
    quotes = [DialogueQuote(quote_id="Q08", source_segment_index=1, text="第21句。", content_chars=4)]
    plans = [
        SimpleNamespace(segment_no=8, source_unit_ranges=[_AiSourceUnitRange(source_segment_index=1, from_unit=15, to_unit=20)]),
        SimpleNamespace(segment_no=9, source_unit_ranges=[_AiSourceUnitRange(source_segment_index=1, from_unit=21, to_unit=24)]),
    ]
    kept = [_AiKeptLine(quote_id="Q08", segment_no=8)]
    moves = reassign_kept_lines_to_covering_segments(kept, quotes, plans, segments)
    assert moves == [{"quote_id": "Q08", "from_segment_no": 8, "to_segment_no": 9, "unit": 21}]
    assert kept[0].segment_no == 9
    # 已经在自己范围内的不动；没有任何段覆盖的也不动（留给越界/洞检查）
    kept_ok = [_AiKeptLine(quote_id="Q08", segment_no=9)]
    assert reassign_kept_lines_to_covering_segments(kept_ok, quotes, plans, segments) == []
    plans_hole = [plans[0]]
    kept_hole = [_AiKeptLine(quote_id="Q08", segment_no=8)]
    assert reassign_kept_lines_to_covering_segments(kept_hole, quotes, plans_hole, segments) == []
    assert kept_hole[0].segment_no == 8


def test_segment_declaring_two_ranges_on_one_source_segment_does_not_crash_the_sort():
    """真实故障（2026-09-04 我欲封天第 12、13 集）：一段对同一原文段声明两个范围时，
    覆盖查找里 sorted((段号, 范围)) 碰到相同段号就去比较范围对象 → TypeError 把整集分镜打死。
    同时：台词落在该段第二个范围里也必须算合法。"""
    from app.production.storyboard_beat_sheet import _AiSegmentPlan

    two_ranges = _AiSegmentPlan(
        segment_no=1, synopsis="段1", source_segment_indexes=[4],
        source_unit_ranges=[
            {"source_segment_index": 4, "from_unit": 1, "to_unit": 2},
            {"source_segment_index": 4, "from_unit": 5, "to_unit": 6},
        ],
    )
    plans = [two_ranges, _plan(2, 3, 4)]
    quotes = [DialogueQuote(
        quote_id="Q01", source_segment_index=4, text="猫忽然跳上了桌子", content_chars=8,
    )]  # S03，只在段 2 的范围里
    errors = kept_line_unit_binding_errors([_kept("Q01", 1)], quotes, plans, _scene_4_segments())
    assert len(errors) == 1 and "第 2 段" in errors[0]
    # 落在段 1 第二个范围（S05/S06）里的台词不得被误报
    start, end = split_source_units(_SCENE_4_TEXT)[4]  # 单元是 (起, 止) 偏移
    text5 = _SCENE_4_TEXT[start:end].strip()
    quotes5 = [DialogueQuote(quote_id="Q05", source_segment_index=4, text=text5, content_chars=len(text5))]
    assert kept_line_unit_binding_errors([_kept("Q05", 1)], quotes5, plans, _scene_4_segments()) == []


def test_kept_line_in_segment_not_referencing_its_source_is_moved_by_source_index():
    """2026-09-05 第 23 集：Q09 原文段号 2，被分到只覆盖 [1] 的第 12 段；覆盖原文段 2 的是第 13、14 段。
    没有任何段的单元范围覆盖到这句时，按原文段号归到最早引用它的段，不再打回模型。"""
    from types import SimpleNamespace
    from app.production.storyboard_beat_sheet import _AiSegmentPlan
    from app.production.storyboard_segment_ranges import reassign_kept_lines_to_covering_segments

    plans = [
        _AiSegmentPlan(segment_no=12, synopsis="a", source_segment_indexes=[1], source_unit_ranges=[]),
        _AiSegmentPlan(segment_no=13, synopsis="b", source_segment_indexes=[4], source_unit_ranges=[]),
        _AiSegmentPlan(segment_no=14, synopsis="c", source_segment_indexes=[4], source_unit_ranges=[]),
    ]
    quotes = [DialogueQuote(quote_id="Q09", source_segment_index=4, text="猫忽然跳上了桌子", content_chars=8)]
    kept = [SimpleNamespace(quote_id="Q09", segment_no=12)]
    moves = reassign_kept_lines_to_covering_segments(kept, quotes, plans, _scene_4_segments())
    assert kept[0].segment_no == 13
    assert moves and moves[0]["to_segment_no"] == 13


def test_cross_source_fallback_prefers_the_segment_with_least_kept_chars():
    """归位到引用该原文段的段时按当前必保台词字数最少的选，不按最早——否则会把多句堆进一段撞容量。"""
    from types import SimpleNamespace
    from app.production.storyboard_beat_sheet import _AiSegmentPlan
    from app.production.storyboard_segment_ranges import reassign_kept_lines_to_covering_segments

    plans = [
        _AiSegmentPlan(segment_no=1, synopsis="a", source_segment_indexes=[1], source_unit_ranges=[]),
        _AiSegmentPlan(segment_no=2, synopsis="b", source_segment_indexes=[4], source_unit_ranges=[]),
        _AiSegmentPlan(segment_no=3, synopsis="c", source_segment_indexes=[4], source_unit_ranges=[]),
    ]
    quotes = [
        DialogueQuote(quote_id="Q01", source_segment_index=4, text="猫忽然跳上了桌子", content_chars=50),
        DialogueQuote(quote_id="Q02", source_segment_index=4, text="黄总一把抓住猫", content_chars=10),
    ]
    kept = [SimpleNamespace(quote_id="Q01", segment_no=2), SimpleNamespace(quote_id="Q02", segment_no=1)]
    reassign_kept_lines_to_covering_segments(kept, quotes, plans, _scene_4_segments(), unit_moves=False)
    assert kept[1].segment_no == 3, "第 2 段已有 50 字，Q02 应归到空的第 3 段"


def test_recheck_mode_never_moves_lines_whose_segment_references_their_source():
    """unit_moves=False（容量归一化复核）：所在段引用了原文段号就不动，哪怕单元范围没覆盖。"""
    from types import SimpleNamespace
    from app.production.storyboard_segment_ranges import reassign_kept_lines_to_covering_segments

    plans = [_plan(1, 1, 2), _plan(2, 3, 12)]
    quotes = [DialogueQuote(quote_id="Q01", source_segment_index=4, text="猫忽然跳上了桌子", content_chars=8)]  # S03
    kept = [SimpleNamespace(quote_id="Q01", segment_no=1)]
    assert reassign_kept_lines_to_covering_segments(kept, quotes, plans, _scene_4_segments(), unit_moves=False) == []
    assert kept[0].segment_no == 1
    reassign_kept_lines_to_covering_segments(kept, quotes, plans, _scene_4_segments())
    assert kept[0].segment_no == 2  # 校验路径仍按单元范围归位
