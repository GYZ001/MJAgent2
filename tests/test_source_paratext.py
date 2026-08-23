"""作者的话不得被当成正文喂给「造人物」的两条路径。

生产缺陷 R9（用户报告：人物谱里出现作者「耳根」）：

* 网文章节正文里直接粘着作者的话（求票、感谢读者、活动公告）。
  「我欲封天」1616 章里 209 章（12.9%）如此。
* `_recurring_character_names` 按**原文逐字出现次数**产出「必收名单」，
  提示词明令「名单里的每个名字…不得改写、合并或省略」。
  作者笔名在统计窗口里出现 27 次、排第 4（真配角王有材才 17 次），
  于是**模型是被程序命令**建出那张人物卡的——不是模型幻觉。
* `identity_authority_registry` 再把每个人物谱条目无条件注册成
  **所有分集**的可引用身份，证据写「角色圣经已登记身份」，
  条目自己就是自己的证据，于是污染扩散到 149 个产物。

剧本链路本身没问题：叙事蓝图会把这些段判成 paratext（实测 1736 个 paratext
节点 vs 15748 story），来源覆盖记 audit_only，剧本正文零污染。
问题是造人物的两条路径跑在这套分类之前且不看它。

本文件测的是**程序那一半**：锚点定位与切割必须确定性、有界、可证。
判断哪段是旁文本由模型做，不在这里测。
"""
from __future__ import annotations

import pytest

from app.source_paratext import (
    MAX_REGION_FRACTION,
    MAX_REMOVED_FRACTION,
    MIN_ANCHOR_CHARS,
    ParatextAnchor,
    PARATEXT_RULE,
    remove_spans,
)

STORY = (
    "孟浩推开院门走进屋舍，桌上摊着一卷未读完的书。"
    "他坐下翻了两页，忽然听见院外传来脚步声。"
    "「谁在外面？」孟浩起身问道，手已按在桌角。"
)
NOTE = "新书急需收藏，推荐票不要少，诸位道友，耳根在此谢过大家！"


def _anchor(text: str, head: int = 12, tail: int = 12) -> ParatextAnchor:
    return ParatextAnchor(start=text[:head], end=text[-tail:])


def test_anchors_cut_exactly_the_note_and_keep_the_story() -> None:
    raw = STORY + NOTE
    out = remove_spans(raw, [_anchor(NOTE)])

    assert "耳根" not in out
    assert "推荐票" not in out
    assert "孟浩推开院门走进屋舍" in out
    assert "「谁在外面？」" in out


def test_note_in_the_middle_is_cut_without_touching_either_side() -> None:
    raw = STORY[:20] + NOTE + STORY[20:]
    out = remove_spans(raw, [_anchor(NOTE)])

    assert NOTE not in out
    assert out == STORY[:20] + STORY[20:]


def test_unfindable_anchor_removes_nothing() -> None:
    """模型抄错锚点时必须整段放弃，宁可漏删也不能乱删。"""
    raw = STORY + NOTE
    bogus = ParatextAnchor(start="这段文字并不存在于原文", end="同样也不存在于原文")

    assert remove_spans(raw, [bogus]) == raw


def test_end_anchor_before_start_anchor_removes_nothing() -> None:
    raw = STORY + NOTE
    reversed_anchor = ParatextAnchor(start=NOTE[-12:], end=NOTE[:12])

    assert remove_spans(raw, [reversed_anchor]) == raw


@pytest.mark.parametrize("length", [0, 1, MIN_ANCHOR_CHARS - 1])
def test_too_short_anchors_are_refused(length: int) -> None:
    """短锚点会在正文里撞上同名片段，必须拒绝。"""
    raw = STORY + NOTE
    short = ParatextAnchor(start=NOTE[:length], end=NOTE[-length:] if length else "")

    assert remove_spans(raw, [short]) == raw


def test_removal_is_capped_so_a_wrong_call_cannot_eat_the_story() -> None:
    """判错一次不得把正文删掉大半——超过上限整体放弃。"""
    raw = STORY + NOTE
    whole = ParatextAnchor(start=raw[:12], end=raw[-12:])

    assert remove_spans(raw, [whole]) == raw


def test_cap_is_a_fraction_not_a_constant() -> None:
    assert 0 < MAX_REMOVED_FRACTION < 1


def test_overlapping_regions_are_merged_not_double_cut() -> None:
    raw = STORY + NOTE
    a = _anchor(NOTE)
    b = ParatextAnchor(start=NOTE[:14], end=NOTE[-10:])

    assert remove_spans(raw, [a, b]) == remove_spans(raw, [a])


def test_multiple_notes_are_all_cut() -> None:
    """真实比例：章节 3600 字、每段作者的话百余字，占比远低于上限。"""
    second = "今天两更，月票榜掉得厉害，恳请诸位道友支援一张月票！"
    body = STORY * 6  # 放大正文，让两段旁文本的占比接近真实章节
    raw = body[:120] + NOTE + body[120:] + second
    out = remove_spans(raw, [_anchor(NOTE), _anchor(second)])

    assert NOTE not in out
    assert second not in out
    assert "孟浩推开院门走进屋舍" in out


def test_one_bad_anchor_does_not_discard_the_good_removals() -> None:
    """一个定歪的锚点只丢它自己，其余有效删除必须照常生效。"""
    body = STORY * 6
    raw = body + NOTE
    good = _anchor(NOTE)
    runaway = ParatextAnchor(start=raw[:12], end=raw[-12:])  # 圈住整篇

    out = remove_spans(raw, [runaway, good])

    assert NOTE not in out
    assert "孟浩推开院门走进屋舍" in out


def test_caps_are_fractions_and_region_cap_is_tighter() -> None:
    assert 0 < MAX_REGION_FRACTION <= MAX_REMOVED_FRACTION < 1


def test_no_spans_returns_the_text_unchanged() -> None:
    raw = STORY + NOTE
    assert remove_spans(raw, []) == raw


def test_single_sentence_note_where_anchors_overlap() -> None:
    """整段就是一句话时，首尾锚点会重叠，仍必须能删掉。"""
    note = "求推荐票，谢谢诸位道友！"
    raw = STORY + note
    out = remove_spans(raw, [ParatextAnchor(start=note, end=note)])

    assert note not in out
    assert "孟浩推开院门走进屋舍" in out


def test_rule_text_forbids_keyword_classification() -> None:
    """判据必须和叙事蓝图同源：只按「是否在讲故事」判，不按关键词判。

    蓝图那层明文禁止关键词/位置分类（会误伤），这里不能自己另立一套。
    """
    assert "不得按段落位置、长度或是否出现某个词来判断" in PARATEXT_RULE
    assert "故事叙述本身" in PARATEXT_RULE
