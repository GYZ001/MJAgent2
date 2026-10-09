"""标题与下一个标题之间没有正文时，不丢弃这一行——并入下一章开头。

病灶：原始上传文件不落盘，``app.ingest._split_chapters_with_removed`` 旧实现
里 ``if remainder:`` 为假（两个标题紧挨、中间零正文）时把前一个标题整行连
标题文字本身都直接丢弃，丢的是原文，丢了永久找不回来（真实发生过一次：
切章器把紧挨着误判标题的真标题 `--- 第481章 少女情怀 ---` 当成"无正文标
题"整行丢弃）。修复见 ``app.ingest._merge_heading_only_matches``：这一行
并入下一章开头（下一章 content 从这个无正文标题的 start 算起，标题仍取
下一章自己的），连续多个无正文标题依次累积；若落在全文末尾（后面没有下
一个标题），并入上一章末尾。本文件只用手写小夹具，全部通过
``ingest_novel`` 的公开结果断言。
"""
from __future__ import annotations

import re

from app.ingest import ingest_novel, _split_chapters_with_removed

# ---------------------------------------------------------------------------
# 1. 两个标题紧挨：前一行文字出现在后一章 content 开头，章数不变
# ---------------------------------------------------------------------------

_ADJACENT_HEADINGS_TEXT = (
    "第一章 开端\n"
    "故事的开端平静而寻常，谁也没想到接下来会发生那么多出乎意料的事情，"
    "一切看起来都和往常没有什么不同，阳光照常升起，街道照常车水马龙。\n"
    "第二章 占位\n"
    "第三章 真正开始\n"
    "真正的故事从这里才算正式展开，此前的一切都只是铺垫，没有人能想到"
    "接下来会发生这样的转折，所有的线索都在这一刻渐渐汇聚到了一起。\n"
)


def test_adjacent_heading_only_line_merges_into_next_chapter() -> None:
    chapters = ingest_novel(_ADJACENT_HEADINGS_TEXT.encode("utf-8"))["chapters"]

    assert [c["title"] for c in chapters] == ["第一章 开端", "第三章 真正开始"]
    assert chapters[1]["content"].startswith("第二章 占位\n第三章 真正开始")
    assert "占位" in chapters[1]["content"]
    assert "真正的故事从这里才算正式展开" in chapters[1]["content"]


# ---------------------------------------------------------------------------
# 2. 末尾孤立标题（后面没有下一个标题）并入上一章末尾
# ---------------------------------------------------------------------------

_TRAILING_HEADING_TEXT = (
    "第一章 开端\n"
    "故事的开端平静而寻常，谁也没想到接下来会发生那么多出乎意料的事情，"
    "一切看起来都和往常没有什么不同，阳光照常升起，街道照常车水马龙。\n"
    "第二章 结尾"
)


def test_trailing_heading_only_line_merges_into_previous_chapter() -> None:
    chapters = ingest_novel(_TRAILING_HEADING_TEXT.encode("utf-8"))["chapters"]

    assert [c["title"] for c in chapters] == ["第一章 开端"]
    assert chapters[0]["content"].endswith("第二章 结尾")


# ---------------------------------------------------------------------------
# 3. 正文守恒：连续多个无正文标题依次累积 + 末尾孤立标题，逐字不丢不增
# ---------------------------------------------------------------------------

_CONSERVATION_TEXT = (
    "第一章 开端\n"
    "故事从这里开始，少年背著行囊走出小镇，前路漫长，但他从未想过回头。\n"
    "第二章 占位甲\n"
    "第三章 占位乙\n"
    "第四章 真正展开\n"
    "真正的故事从这里才算正式展开，此前的一切都只是铺垫，没有人能想到"
    "接下来会发生这样的转折，所有的线索都在这一刻渐渐汇聚到了一起。\n"
    "第五章 收尾"
)


def test_conservation_holds_with_chained_and_trailing_heading_only_lines() -> None:
    """「第二章 占位甲」「第三章 占位乙」连续两个无正文标题依次累积进
    「第四章 真正展开」开头，末尾孤立的「第五章 收尾」并入「第四章」末尾——
    去空白后原文与重建正文逐字相等，不丢字也不多字。
    """
    chapters = ingest_novel(_CONSERVATION_TEXT.encode("utf-8"))["chapters"]

    assert [c["title"] for c in chapters] == ["第一章 开端", "第四章 真正展开"]
    assert chapters[1]["content"].startswith("第二章 占位甲\n第三章 占位乙\n第四章 真正展开")
    assert chapters[1]["content"].endswith("第五章 收尾")

    original_stripped = re.sub(r"\s+", "", _CONSERVATION_TEXT)
    rebuilt_stripped = re.sub(r"\s+", "", "".join(c["content"] for c in chapters))
    assert original_stripped == rebuilt_stripped


# ---------------------------------------------------------------------------
# 4. 幂等性：把 content 拼回全文再切一次，标题与内容逐章必须相等——否则
#    「存量项目重切」这类修正流程会越改越歪（B 沙箱演练实测：刚准备高考
#    有 6 章第二次切会变标题，根因是补救分支把纯装饰前缀错当残片挪走）。
# ---------------------------------------------------------------------------

def _assert_idempotent(text: str) -> None:
    first = ingest_novel(text.encode("utf-8"))["chapters"]
    second, _ = _split_chapters_with_removed("\n".join(c["content"] for c in first))
    assert [c["title"] for c in first] == [c["title"] for c in second]
    assert [c["content"] for c in first] == [c["content"] for c in second]


_GLUED_CHAIN_FOR_IDEMPOTENCY = (
    "第1章刘备摆摊\n"
    "刘备在摆摊卖草鞋，日子虽苦但心有大志。他低头擦拭草鞋，忽然听到身后脚步声走近，心头一震。\n"
    "他抬头一看，是关羽，两人相谈甚欢，渐生意气，决定结拜为兄弟，共谋大业，以安天下苍生怎么斩华第2章我斩华雄？\n"
    "华雄立于关前，横刀立马，诸侯军皆惧，无人敢应战，关羽主动请战，曹操亲自斟酒助威，三军肃然。\n"
)


def test_glued_heading_chain_is_idempotent() -> None:
    _assert_idempotent(_GLUED_CHAIN_FOR_IDEMPOTENCY)


_DECORATED_PREFIX_FOR_IDEMPOTENCY = (
    "第425章 风起之时\n"
    "风起于青萍之末，局势渐渐变得扑朔迷离，各方势力都在暗中观察，蓄势待发。\n"
    "--- 第426章 一抹风情 ---\n"
    "她转身离去，留下一抹风情，让人久久难以忘怀，街角的灯影也随之摇曳不定。\n"
)


def test_decorative_prefix_heading_is_idempotent() -> None:
    _assert_idempotent(_DECORATED_PREFIX_FOR_IDEMPOTENCY)


_DUPLICATE_RECOVERY_FOR_IDEMPOTENCY = (
    "第一章 开端\n"
    "故事的开端平静而寻常，谁也没想到接下来会发生那么多出乎意料的事情，"
    "一切看起来都和往常没有什么不同，阳光照常升起，街道照常车水马龙。"
    "接下来的几个月里什么都没有发生，大家都渐渐放松了警惕，日子一天天过去，"
    "谁也没想到后面的转折会来得这样突然，一切都在悄无声息中悄然酝酿著。\n"
    "--- 第2章 中段 ---\n"
    "剧情在这一段陡然加快，几个关键人物相继登场，线索也渐渐浮出水面，"
    "读者能感觉到一场大戏即将拉开序幕，气氛一点一点变得紧张起来。\n"
    "--- 第2章 中段 ---\n"
    "另一边的视角也在同时推进，两条线渐渐有了交汇的迹象，仔细看就能发现"
    "其中埋藏的呼应与伏笔，故事的轮廓开始变得清晰，令人愈发期待后续。\n"
    "第三章 结局\n"
    "最终一切谜底揭晓，所有悬而未决的线索都有了交代，故事画上了圆满的"
    "句号，读者合上书页，心里却还回响著那些曾经牵动人心的片段与画面。\n"
)


def test_duplicate_decorated_heading_recovery_branch_is_idempotent() -> None:
    """复现《刚准备高考》真实病灶：两条完全相同的 `--- 第2章 中段 ---`
    紧邻出现，被判定不连续而拒收、退回补救分支。补救分支若把纯装饰前缀
    错当上一章残片挪走，第二次切章会把正文里残留的 ` ---` 重新识别成
    标题的一部分，标题逐章对不上——必须幂等。
    """
    _assert_idempotent(_DUPLICATE_RECOVERY_FOR_IDEMPOTENCY)


def test_heading_only_lines_are_idempotent() -> None:
    _assert_idempotent(_CONSERVATION_TEXT)
