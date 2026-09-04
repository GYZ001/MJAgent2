"""app.production.storyboard_pack —— 2.4.0 句单元范围契约的测试子集。

拆出原因：``tests/test_storyboard_pack.py`` 在 ``app/FILE_CONVENTIONS.toml`` 的
``test_line_count`` 棘轮基线上（3160 行），2.4.0（每段各占一块原文的句单元范围
声明 + 容量归一化同步拆分单元范围，见 app.production.storyboard_segment_ranges
模块 docstring 的真实故障）新增/改写的用例把它推到 3205 行、超出基线。本文件
承接这部分用例（纯搬移，断言逻辑不变）：``_validate_beat_sheet_draft`` 新签名
（``source_segments=`` 取代 ``total_segments=``）相关的结构校验、``_source_block_
for_prompt`` 的单元编号渲染、以及 ``normalize_beat_sheet_capacity``/
``normalize_and_assert_capacity`` 的 ``source_segments=`` 新参数与拆分行为。
不覆盖真实供应商往返；跨段台词去重（``delivered_lines``/``current_segment_no``）
仍留在 ``tests/test_storyboard_pack.py``——那是另一项独立改造（阶段二台词去重
闸门），不属于"单元范围"这个契约面，专门测试见 ``tests/test_storyboard_dialogue_
repeat.py``；句单元切分/渲染/校验函数本身的详尽单测见 ``tests/test_storyboard_
segment_ranges.py``，这里只保留原本就挂在 ``tests/test_storyboard_pack.py`` 里、
依赖该文件其它 fixture 风格的这部分用例。
"""
from __future__ import annotations

import pytest

from app import config
from app.production.storyboard_capacity_normalize import (
    StoryboardCapacityNormalizationError,
    normalize_and_assert_capacity,
    normalize_beat_sheet_capacity,
)
from app.production.storyboard_dialogue_ledger import DialogueQuote, _AiKeptLine
from app.production.storyboard_pack import (
    STORYBOARD_PACK_CONTRACT_MARKER,
    STORYBOARD_PACK_VERSION,
    _AiBeat,
    _AiBeatSheetDraft,
    _AiSegmentPlan,
    _source_block_for_prompt,
    _validate_beat_sheet_draft,
)
from app.source_excerpt import SourceSegment


# ---------------------------------------------------------------------------
# 假原文段 fixture：只关心段号存在性/单元范围合法性，不关心真实原文内容
# ---------------------------------------------------------------------------

def _fake_source_segments(n: int) -> list[SourceSegment]:
    """``n`` 个各含恰好一个句单元的假原文段，供只关心段号存在性（不关心真实
    原文内容）的 ``_validate_beat_sheet_draft``/容量归一化测试构造
    ``source_segments=``。每段文本 "正文一句N。" 是单独一句、单独一个句单元
    （``split_source_units`` 按句末标点切），所以 ``from_unit=to_unit=1`` 恒
    合法，测试只需要声明 ``source_unit_ranges=[{"source_segment_index": i,
    "from_unit": 1, "to_unit": 1}]`` 即可通过 2.4.0 的单元范围校验。
    """
    return [
        SourceSegment(segment_id=f"s{i}", text=f"正文一句{i}。", start_offset=0, end_offset=0)
        for i in range(1, n + 1)
    ]


def _full_unit_range(index: int) -> dict:
    """配 ``_fake_source_segments`` 用：覆盖该原文段唯一句单元的完整范围。"""
    return {"source_segment_index": index, "from_unit": 1, "to_unit": 1}


def test_source_block_for_prompt_omits_paratext_text_but_keeps_numbering():
    segments = [
        SourceSegment(segment_id="s1", text="【第八章】\n第八章", start_offset=0, end_offset=1),
        SourceSegment(segment_id="s2", text="孟浩推开院门。", start_offset=1, end_offset=2),
        SourceSegment(
            segment_id="s3", text="又是大章，求推荐票，谢谢诸位道友！",
            start_offset=2, end_offset=3,
        ),
    ]
    block = _source_block_for_prompt(segments, {1, 3})

    # 段1/3 是 paratext：不切单元，整段一行占位。段2（正文）2.4.0 起改用
    # render_source_units 的单元编号形式，不再是整段一行。
    assert "[段1]" in block and "[段2·S01]" in block and "[段3]" in block
    assert "孟浩推开院门。" in block
    # 作者的话的原文一个字都不能出现在喂给模型的文本里。
    assert "求推荐票" not in block
    assert "诸位道友" not in block
    assert "【第八章】" not in block
    # 段号不重新编号：段2（唯一的正文段）紧跟在 [段2·S01] 后面，不是 [段1]。
    assert "[段2·S01] 孟浩推开院门。" in block


def test_source_block_for_prompt_full_text_when_no_paratext():
    segments = [SourceSegment(segment_id="s1", text="正文。", start_offset=0, end_offset=1)]
    block = _source_block_for_prompt(segments, set())
    assert block == "[段1·S01] 正文。"


# ---------------------------------------------------------------------------
# 阶段一：节拍表 + 分段的结构校验
# ---------------------------------------------------------------------------

def test_validate_beat_sheet_draft_accepts_well_formed_draft():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="他扔掉了理想", segment_indexes=[1, 2])],
        segments=[
            _AiSegmentPlan(
                segment_no=1, synopsis="他扔掉了理想",
                source_segment_indexes=[1, 2], beat_ids=["B1"],
                source_unit_ranges=[_full_unit_range(1), _full_unit_range(2)],
            )
        ],
    )
    assert _validate_beat_sheet_draft(draft, source_segments=_fake_source_segments(3), dialogue_quotes=[]) == []


def test_validate_beat_sheet_draft_rejects_out_of_range_segment_index():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[99])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1])],
    )
    errors = _validate_beat_sheet_draft(draft, source_segments=_fake_source_segments(3), dialogue_quotes=[])
    assert any("不存在的原文段号" in e for e in errors)


def test_validate_beat_sheet_draft_rejects_non_contiguous_segment_no():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=2, synopsis="x", source_segment_indexes=[1])],
    )
    errors = _validate_beat_sheet_draft(draft, source_segments=_fake_source_segments(3), dialogue_quotes=[])
    assert any("连续递增" in e for e in errors)


def test_validate_beat_sheet_draft_rejects_unknown_beat_id_reference():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(
                segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B-GHOST"],
            )
        ],
    )
    errors = _validate_beat_sheet_draft(draft, source_segments=_fake_source_segments(3), dialogue_quotes=[])
    assert any("不存在的 beat_id" in e for e in errors)


def test_validate_beat_sheet_draft_surfaces_dialogue_ledger_errors():
    """_validate_beat_sheet_draft 把 dialogue_ledger_errors 接进自己的 blocking 校验。"""
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"])],
        kept_lines=[_AiKeptLine(quote_id="Q-GHOST", segment_no=1)],
    )
    errors = _validate_beat_sheet_draft(draft, source_segments=_fake_source_segments(3), dialogue_quotes=[])
    assert any("不存在的 quote_id" in e for e in errors)


# ---------------------------------------------------------------------------
# 2.1.2 容量归一化：normalize_beat_sheet_capacity / normalize_and_assert_capacity
# ---------------------------------------------------------------------------

def _plan(segment_no: int, synopsis: str = "x", source=(1,), beat_ids=("B1",)) -> _AiSegmentPlan:
    return _AiSegmentPlan(
        segment_no=segment_no, synopsis=synopsis,
        source_segment_indexes=list(source), beat_ids=list(beat_ids),
    )


def test_normalize_beat_sheet_capacity_bins_135_chars_into_three_segments():
    """真实 EP1 归因数字：Q01(4)+Q02(39)+Q03(38)+Q04(54)=135 字应拆成 3 箱。"""
    quotes = [
        DialogueQuote(quote_id="Q01", source_segment_index=2, text="a", content_chars=4),
        DialogueQuote(quote_id="Q02", source_segment_index=2, text="b", content_chars=39),
        DialogueQuote(quote_id="Q03", source_segment_index=2, text="c", content_chars=38),
        DialogueQuote(quote_id="Q04", source_segment_index=2, text="d", content_chars=54),
    ]
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[2])],
        segments=[_plan(1, synopsis="原段", source=(2,))],
        kept_lines=[_AiKeptLine(quote_id=q.quote_id, segment_no=1) for q in quotes],
    )
    telemetry = normalize_beat_sheet_capacity(draft, quotes, source_segments=_fake_source_segments(5))

    assert len(draft.segments) == 3
    assert [s.segment_no for s in draft.segments] == [1, 2, 3]
    assert telemetry == [{"original_segment_no": 1, "bin_count": 3, "new_segment_nos": [1, 2, 3]}]
    for segment_no, quote_ids in [(1, {"Q01", "Q02"}), (2, {"Q03"}), (3, {"Q04"})]:
        kept_here = {k.quote_id for k in draft.kept_lines if k.segment_no == segment_no}
        assert kept_here == quote_ids


def test_normalize_beat_sheet_capacity_first_bin_keeps_original_synopsis_others_get_marker():
    quotes = [
        DialogueQuote(quote_id="Q01", source_segment_index=1, text="a", content_chars=54),
        DialogueQuote(quote_id="Q02", source_segment_index=1, text="b", content_chars=54),
    ]
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_plan(1, synopsis="他扔掉了理想", source=(1,))],
        kept_lines=[_AiKeptLine(quote_id="Q01", segment_no=1), _AiKeptLine(quote_id="Q02", segment_no=1)],
    )
    normalize_beat_sheet_capacity(draft, quotes, source_segments=_fake_source_segments(5))
    assert draft.segments[0].synopsis == "他扔掉了理想"
    assert draft.segments[1].synopsis == "他扔掉了理想（容量拆分·承接前段台词）"


def test_normalize_beat_sheet_capacity_new_segments_inherit_source_indexes_and_beat_ids():
    quotes = [
        DialogueQuote(quote_id="Q01", source_segment_index=3, text="a", content_chars=54),
        DialogueQuote(quote_id="Q02", source_segment_index=3, text="b", content_chars=54),
    ]
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B7", summary="x", segment_indexes=[3])],
        segments=[_plan(1, source=(3, 4), beat_ids=("B7",))],
        kept_lines=[_AiKeptLine(quote_id="Q01", segment_no=1), _AiKeptLine(quote_id="Q02", segment_no=1)],
    )
    draft.segments[0].palette = "冷调惨白"
    normalize_beat_sheet_capacity(draft, quotes, source_segments=_fake_source_segments(5))
    spawned = draft.segments[1]
    # 拆出的新段仍是同一场戏，色温方向必须原样继承（EP1 重跑实测：空 palette 让灯光又闪了两次）。
    assert spawned.palette == "冷调惨白"
    # source_unit_ranges 未声明（这条测试不关心单元范围）时，2.4.0 的拆分点
    # 定位不到（quote_unit_index 在假原文里搜不到占位 text="a"/"b"），按已知
    # 限制原样降级成旧行为：source_segment_indexes 整段继承，不裁剪。
    assert spawned.source_segment_indexes == [3, 4]
    assert spawned.beat_ids == ["B7"]
    # 继承的列表必须是独立拷贝，不与原段共享同一个对象。
    assert spawned.source_segment_indexes is not draft.segments[0].source_segment_indexes


def test_normalize_beat_sheet_capacity_noop_when_no_segment_overflows():
    quotes = [DialogueQuote(quote_id="Q01", source_segment_index=1, text="a", content_chars=10)]
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_plan(1)],
        kept_lines=[_AiKeptLine(quote_id="Q01", segment_no=1)],
    )
    telemetry = normalize_beat_sheet_capacity(draft, quotes, source_segments=_fake_source_segments(5))
    assert telemetry == []
    assert len(draft.segments) == 1
    assert draft.segments[0].segment_no == 1


def test_normalize_beat_sheet_capacity_renumbers_untouched_segments_after_an_earlier_split():
    """拆分发生在前面的段时，后面完全没超容的段也要跟着重排 segment_no。"""
    quotes = [
        DialogueQuote(quote_id="Q01", source_segment_index=1, text="a", content_chars=54),
        DialogueQuote(quote_id="Q02", source_segment_index=1, text="b", content_chars=54),
        DialogueQuote(quote_id="Q03", source_segment_index=2, text="c", content_chars=10),
    ]
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2])],
        segments=[_plan(1, source=(1,)), _plan(2, source=(2,))],
        kept_lines=[
            _AiKeptLine(quote_id="Q01", segment_no=1), _AiKeptLine(quote_id="Q02", segment_no=1),
            _AiKeptLine(quote_id="Q03", segment_no=2),
        ],
    )
    normalize_beat_sheet_capacity(draft, quotes, source_segments=_fake_source_segments(5))
    assert [s.segment_no for s in draft.segments] == [1, 2, 3]
    q03 = next(k for k in draft.kept_lines if k.quote_id == "Q03")
    assert q03.segment_no == 3, "原第 2 段被挤到第 3 段，kept_lines 必须跟着更新"


def test_normalize_and_assert_capacity_returns_telemetry_when_normalization_succeeds():
    quotes = [
        DialogueQuote(quote_id="Q01", source_segment_index=1, text="a", content_chars=54),
        DialogueQuote(quote_id="Q02", source_segment_index=1, text="b", content_chars=54),
    ]
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_plan(1)],
        kept_lines=[_AiKeptLine(quote_id="Q01", segment_no=1), _AiKeptLine(quote_id="Q02", segment_no=1)],
    )
    telemetry = normalize_and_assert_capacity(
        draft, quotes, source_segments=_fake_source_segments(5), paratext_indexes=set(),
    )
    assert telemetry[0]["bin_count"] == 2
    # 复核断言本身必须真的通过：归一化后每段合计不超容。
    assert all(
        sum(q.content_chars for q in quotes if any(
            k.quote_id == q.quote_id and k.segment_no == s.segment_no for k in draft.kept_lines
        )) <= 54
        for s in draft.segments
    )


def test_normalize_and_assert_capacity_fail_closed_when_normalization_itself_is_buggy(monkeypatch):
    """fail-closed 路径：归一化后复核仍超容，说明归一化算法自己有 bug，必须
    抛出而不是静默放行——这是唯一允许把容量错误亮给人看的场景。

    用一个"什么都不做"的假归一化模拟 bug；再把容量上限收到 10，让原本
    54 字的合法 kept 台词在这个假实现下必然显得超容，触发断言。
    """
    import app.production.storyboard_capacity_normalize as capacity_normalize_module

    quotes = [DialogueQuote(quote_id="Q01", source_segment_index=1, text="a", content_chars=54)]
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_plan(1)],
        kept_lines=[_AiKeptLine(quote_id="Q01", segment_no=1)],
    )
    monkeypatch.setattr(
        capacity_normalize_module, "normalize_beat_sheet_capacity", lambda d, q, **_kw: [],
    )
    monkeypatch.setattr(config, "MAX_SPOKEN_CHARS_PER_SHOT", 10)
    with pytest.raises(StoryboardCapacityNormalizationError):
        capacity_normalize_module.normalize_and_assert_capacity(
            draft, quotes, source_segments=_fake_source_segments(5), paratext_indexes=set(),
        )


def test_contract_marker_bumps_to_2_4_0_so_stale_packs_without_unit_ranges_regenerate():
    """marker 跟落库形状走：2.4.0 新增 StoryboardPackSegment.source_unit_ranges
    字段（每段各占一块原文的句单元范围声明，真实回归"猫跳上桌拍了两次、黄总
    抓猫拍了三次"驱动的改造，见 app.production.storyboard_segment_ranges 模块
    docstring），旧行没有这个字段，marker 不动会让 resume 短路把它们误判为
    "已经用新契约生成过"。
    """
    assert STORYBOARD_PACK_CONTRACT_MARKER == "storyboard_pack/2.4.0"
    assert STORYBOARD_PACK_VERSION == "2.4.0"



def test_dropping_a_full_speaker_line_is_rejected_but_interjections_may_be_dropped():
    """EP1 第三次重跑实测：模型把 Q10（李麦麦的 21 字整句）塞进 dropped_lines 逃过校验。"""
    from app.production.storyboard_beat_sheet import undroppable_quote_errors
    from app.production.storyboard_dialogue_ledger import _AiDroppedLine

    quotes = [
        DialogueQuote(quote_id="Q10", source_segment_index=4, text="黄总，它真的只是一只普通的流浪猫，不能留在公司……", content_chars=21, speaker="李麦麦"),
        DialogueQuote(quote_id="Q06", source_segment_index=4, text="这……", content_chars=1, speaker="黄总"),
        DialogueQuote(quote_id="Q20", source_segment_index=5, text="牌匾上的四个字", content_chars=7),
    ]
    dropped = [_AiDroppedLine(quote_id="Q10", reason="未在当前剧情节拍中保留"), _AiDroppedLine(quote_id="Q06", reason="语气词"), _AiDroppedLine(quote_id="Q20", reason="屏上文字")]
    errors = undroppable_quote_errors(dropped, quotes)
    assert len(errors) == 1 and "Q10" in errors[0] and "李麦麦" in errors[0]
