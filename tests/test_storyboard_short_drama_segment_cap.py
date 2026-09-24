"""``SegmentCountSoftCap``：段数软上限按**容量归一化后的预计段数**判定
（2026-09-24），不再是模型自己声明的 ``len(draft.segments)``——从
``tests/test_storyboard_short_drama.py`` 拆出（同一批改造，那个文件自己的
500 行棘轮已无余量，见其模块 docstring）。``_draft``/``_range_plan``/
``_sources`` 复用同一份夹具，与 ``tests/test_storyboard_short_drama_
evidence.py`` 从 ``tests.test_storyboard_pack`` 借夹具同一种既有写法。
"""
from __future__ import annotations

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
