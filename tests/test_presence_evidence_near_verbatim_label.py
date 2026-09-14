"""在场证据按「近似逐字」匹配发现模型的称谓，而不是要求原文逐字写出标签。

2026-09-14 我欲封天第 1 集：标签「虎头虎脑少年」，原文写「这少年虎头虎脑」「那虎头虎脑的家伙」
「虎头虎脑的少年」，逐字搜 0 命中 → 判成路人、不建卡、每镜长相漂移；这个孩子第 19 集被点名叫小虎。
"""
from __future__ import annotations

from app.portraits.presence_evidence import _label_hit, collect_presence_evidence, functional_card_worthy

CHAPTER = (
    "“飞！”说出这个字的不是王有材，而是他旁边探出身子的一个八九岁少年，这少年虎头虎脑，大声开口。\n"
    "当他的目光落在王有材身上时，看到了他身边的两个少年，一个是那虎头虎脑的家伙，另一个则是白白净净身子较胖。\n"
    "“你，还有你，跟我走。”话语间，此人指了指王有材和其旁虎头虎脑的少年。\n"
    "孟浩看着远处的青山，沉默不语。\n"
)


def test_near_verbatim_hit_returns_the_source_run() -> None:
    assert _label_hit("虎头虎脑少年", "这少年虎头虎脑，大声开口。") == "虎头虎脑"
    assert _label_hit("八九岁少年", "一个八九岁少年探出身子") == "八九岁少年"
    # 只剩通用尾词「少年」（2 字，不足 3 字也不足 60%）不算命中
    assert _label_hit("虎头虎脑少年", "看到了他身边的两个少年") == ""
    assert _label_hit("白白净净胖少年", "另一个则是白白净净身子较胖") == ""


def test_recurring_described_boy_becomes_card_worthy() -> None:
    evidence = collect_presence_evidence("虎头虎脑少年", {1: CHAPTER})
    assert evidence["recurrence"]["paragraph_count"] == 3
    assert functional_card_worthy(evidence) is True  # ≥2 段在场即达标，不要求动作/对白邻接


def test_unrelated_label_still_has_no_evidence() -> None:
    evidence = collect_presence_evidence("独眼老者", {1: CHAPTER})
    assert evidence["recurrence"]["paragraph_count"] == 0 and functional_card_worthy(evidence) is False
