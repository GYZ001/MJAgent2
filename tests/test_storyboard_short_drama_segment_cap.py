"""``SegmentCountSoftCap``：段数软上限按**容量归一化后的预计段数**判定
（2026-09-24），不再是模型自己声明的 ``len(draft.segments)``——从
``tests/test_storyboard_short_drama.py`` 拆出（同一批改造，那个文件自己的
500 行棘轮已无余量，见其模块 docstring）。``_draft``/``_range_plan``/
``_sources`` 复用同一份夹具，与 ``tests/test_storyboard_short_drama_
evidence.py`` 从 ``tests.test_storyboard_pack`` 借夹具同一种既有写法。
"""
from __future__ import annotations

from app.config import MAX_SPOKEN_CHARS_PER_SHOT
from app.production.storyboard_beat_sheet_schemas import _AiSegmentPlan
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_short_drama import MAX_SEGMENT_COUNT, SegmentCountSoftCap
from tests.test_storyboard_short_drama import _draft, _range_plan, _sources


def _n_segment_draft(n: int):
    segments = [_AiSegmentPlan(segment_no=i, synopsis="x", source_segment_indexes=[1]) for i in range(1, n + 1)]
    return _draft(segments)


# ---------------------------------------------------------------------------
# 无 kept 台词时预计段数 == 模型声明的段数（容量归一化对空 kept_lines 是空操作）
# ---------------------------------------------------------------------------

def test_soft_cap_blocks_first_attempts_then_warns_on_last():
    cap = SegmentCountSoftCap(adaptation_mode="short_drama", retry_limit=2, quotes=[], source_segments=[])
    over = _n_segment_draft(MAX_SEGMENT_COUNT + 2)
    assert cap.errors(over) != [], "第 1 次（attempt 0）应打回"
    assert cap.errors(over) != [], "第 2 次（attempt 1）应打回"
    assert cap.errors(over) == [], "第 3 次（attempt 2 == retry_limit，最后一次）应降级为警告"


def test_soft_cap_never_errors_when_within_target():
    cap = SegmentCountSoftCap(adaptation_mode="short_drama", retry_limit=2, quotes=[], source_segments=[])
    within = _n_segment_draft(MAX_SEGMENT_COUNT)
    assert cap.errors(within) == []
    assert cap.errors(within) == []
    assert cap.errors(within) == []


def test_soft_cap_is_noop_for_faithful_mode():
    cap = SegmentCountSoftCap(adaptation_mode="faithful", retry_limit=2, quotes=[], source_segments=[])
    huge = _n_segment_draft(999)
    assert cap.errors(huge) == []
    assert cap.errors(huge) == []
    assert cap.errors(huge) == []
    assert cap.last_projected_count is None, "忠实档永远不计算预计段数"


# ---------------------------------------------------------------------------
# 核心行为：预计段数看容量归一化之后，不看模型自己声明的段数
# ---------------------------------------------------------------------------

def test_soft_cap_uses_capacity_projected_count_not_declared_count():
    """2026-09-24 真实三集验证的根因（我欲封天 EP3/EP9）：模型规划 6 段——自己
    声明的段数达标（6 <= 8）——但每段 kept 台词 80 字，超过 54 字口播容量。
    按生产同源的容量归一化拆分后，每段至少拆成 2 段，预计段数 12 > 8，必须
    打回；模型不能再靠"自己声明的段数达标"蒙混过关。"""
    sources = _sources(*(["甲句一二三四五六七八九十。乙句一二三四五六七八九十。"] * 6))
    plans, quotes, kept = [], [], []
    counter = 0
    for i in range(1, 7):
        plans.append(_range_plan(i, i, 1, 2))
        for text in ("甲句一二三四五六七八九十。", "乙句一二三四五六七八九十。"):
            counter += 1
            quote = DialogueQuote(quote_id=f"Q{counter:02d}", source_segment_index=i, text=text, content_chars=40)
            quotes.append(quote)
            kept.append({"quote_id": quote.quote_id, "segment_no": i})
    draft = _draft(plans, kept_lines=kept)
    cap = SegmentCountSoftCap(adaptation_mode="short_drama", retry_limit=2, quotes=quotes, source_segments=sources)

    errors = cap.errors(draft)

    assert errors != [], "模型规划 6 段本身达标，但容量拆分后的预计段数必须驱动这条软上限"
    assert cap.last_projected_count == 12, "6 段各超容一倍，贪心装箱各拆成 2 段"
    assert str(MAX_SEGMENT_COUNT) in errors[0] and "12" in errors[0]
    assert "beat_id" in errors[0], "打回文案要给出可执行改法：再弃置台词并标 beat_id"
    assert len(draft.segments) == 6, "SegmentCountSoftCap 只读深拷贝判定，不改动原始草稿"


def test_soft_cap_last_projected_count_updates_on_every_call():
    """last_projected_count 记的是*最后一次*调用的值，供 adaptation_summary
    的 projected_segment_count 留档字段读取（见 storyboard_pack.
    generate_storyboard_pack）。"""
    cap = SegmentCountSoftCap(adaptation_mode="short_drama", retry_limit=5, quotes=[], source_segments=[])
    assert cap.last_projected_count is None, "还没调用过 errors() 之前是 None"
    cap.errors(_n_segment_draft(3))
    assert cap.last_projected_count == 3
    cap.errors(_n_segment_draft(5))
    assert cap.last_projected_count == 5


# ---------------------------------------------------------------------------
# 打回文案列出具体超容段（2026-09-24）：段号/保留字数/容量/拆成几段，按超出
# 量降序，最多 5 个——B 机沙箱第三轮真实验证：只给总量提示时模型不知道该
# 删哪一段（我欲封天 EP3 两次打回段数未变）。
# ---------------------------------------------------------------------------

def test_soft_cap_error_lists_up_to_five_oversized_segments_sorted_by_excess():
    seg_specs = [(1, 4, 40), (2, 3, 40), (3, 3, 35), (4, 2, 45), (5, 2, 40), (6, 2, 35), (7, 2, 30)]
    sources = _sources(*(["占位原文占位原文占位原文占位原文占位原文占位原文占位原文。"] * 7))
    plans, quotes, kept = [], [], []
    counter = 0
    for seg_no, n_quotes, chars in seg_specs:
        plans.append(_range_plan(seg_no, seg_no, 1, 1))
        for _ in range(n_quotes):
            counter += 1
            quote = DialogueQuote(quote_id=f"Q{counter:02d}", source_segment_index=seg_no, text="x" * chars, content_chars=chars)
            quotes.append(quote)
            kept.append({"quote_id": quote.quote_id, "segment_no": seg_no})
    draft = _draft(plans, kept_lines=kept)
    cap = SegmentCountSoftCap(adaptation_mode="short_drama", retry_limit=2, quotes=quotes, source_segments=sources)

    message = cap.errors(draft)[0]

    assert f"容量 {MAX_SPOKEN_CHARS_PER_SHOT} 字" in message
    assert "第 1 段保留台词 160 字" in message and "将被拆成 4 段" in message, "4 条 40 字台词贪心装箱拆成 4 段"
    assert "第 5 段保留台词 80 字" in message
    assert "第 6 段" not in message and "第 7 段" not in message, "最多只列 5 个，第 6/7 段（70/60 字，超出量最小）不应出现"
    order = [message.index(f"第 {i} 段") for i in range(1, 6)]
    assert order == sorted(order), "列出的 5 个段必须按保留字数（超出量）降序排列"
