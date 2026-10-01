"""同 label 的多条道具提及合并 plot_significant（2026-09-28 code review 实测
发现）。

``_prep_pack_build_prop_manifest`` 用 ``props.setdefault`` 合并同一
label 的多条提及：``segment_indexes`` 跨提及正确取并集，但
``plot_significant``/``plot_significant_quote`` 此前只在 ``setdefault``
第一次插入该 label 时写入——同 label 后续提及即使 ``plot_significant=True``
且带真实可核验证据，也会被静默丢弃（该 label 已存在于字典里，
``setdefault`` 不会用新字典覆盖已有 entry）。

这正是 Part C（关键剧情道具建卡）要接住的形状：黄铜旧星盘、童年合影这类
贴身反复出现的伏笔道具，早期提及往往平淡（模型判
``plot_significant=False``），只有交接/特写/伏笔揭示那一刻的提及才会被
模型正确标记为 ``True``。``chunk_extraction`` 按分块顺序逐块独立调用模型，
真正命中判据的那条提及完全可能出现在更晚的分块里；修复前，只要它不是
该 label 第一次出现时的那条提及，就会被丢弃，且没有任何日志或错误信号
（CLAUDE.md「缺失要有可见信号」明确禁止的静默丢弃）。本文件只钉住
``_prep_pack_build_prop_manifest`` 这一纯函数的合并语义，不重复
``app.props.judge.is_key_prop_mention`` 消费侧的逐字核验（那部分已有
tests/test_props_library.py 覆盖）。
"""
from __future__ import annotations

from app.production.prep_pack.prop_manifest import _prep_pack_build_prop_manifest
from app.source_excerpt import index_source_segments

SOURCE_TEXT = (
    "甲一贴身收着黄铜旧星盘，随手把玩。"
    "\n\n林動郑重地把黄铜旧星盘交到甲一手中。"
)
REVEAL_QUOTE = "林動郑重地把黄铜旧星盘交到甲一手中"


def _mentions(*, reveal_first: bool) -> list[dict]:
    plain = {
        "label": "黄铜旧星盘", "description": "一枚黄铜制的老旧星盘",
        "segment_indexes": [1], "plot_significant": False,
        "plot_significant_quote": "",
    }
    reveal = {
        "label": "黄铜旧星盘", "description": "一枚黄铜制的老旧星盘",
        "segment_indexes": [2], "plot_significant": True,
        "plot_significant_quote": REVEAL_QUOTE,
    }
    return [reveal, plain] if reveal_first else [plain, reveal]


def test_plot_significant_survives_when_later_mention_reveals_it() -> None:
    """第一条提及平淡，第二条提及才是交接时刻——合并结果必须采纳第二条
    提及的判定与证据，不能因为 setdefault 只认第一条而被丢弃。"""
    segments = index_source_segments(SOURCE_TEXT)
    props = _prep_pack_build_prop_manifest(_mentions(reveal_first=False), segments)

    assert len(props) == 1
    assert props[0]["segment_indexes"] == [1, 2]
    assert props[0]["plot_significant"] is True, (
        "第二条提及的剧情重要判定不能被第一条平淡提及的 setdefault 首次插入丢弃"
    )
    assert props[0]["plot_significant_quote"] == REVEAL_QUOTE


def test_plot_significant_merge_is_order_independent() -> None:
    """同一对提及颠倒顺序（先到的是交接时刻，后到的是平淡提及），合并结果
    必须与顺序无关——语义是'这个道具在本集里是否存在任一次剧情重要提及'，
    不是'谁先被处理谁定终身'。"""
    segments = index_source_segments(SOURCE_TEXT)
    props = _prep_pack_build_prop_manifest(_mentions(reveal_first=True), segments)

    assert len(props) == 1
    assert props[0]["segment_indexes"] == [1, 2]
    assert props[0]["plot_significant"] is True
    assert props[0]["plot_significant_quote"] == REVEAL_QUOTE
