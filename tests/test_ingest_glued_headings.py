"""行尾粘连标题：章节标题被粘在上一章末行末尾时的识别与切分。

病灶来自真实导入样本《魂穿刘关张，诸侯们被整麻了》（爱下电子书 ixdzs 的
TXT）：全书 690 个标题几乎都被粘在上一章最后一行的末尾、且末行常被截掉
1-3 个字，例如 `怎么斩华第2章我斩华雄？`——"怎么斩华"是上一章残片，
"第2章我斩华雄？"才是标题。旧实现要么把整行当标题（残片污染标题），要么
整条漏切（序号出现断点就停）。修复见 ``app.novel.structure._inline_heading_
candidates``/``_accept_inline_matches``/``_finalize_inline_matches`` 与
``app.ingest._recover_missing_unit_headings`` 的行首残片处理。

判据是序号连续性（结构证据），不是词表：候选只有在与前/后邻居序号相差
恰好 1、且前后邻居都不与它同序号时才采信，装饰前缀候选与粘连候选走同一
条判据（不再对"前缀不含字母"无条件采信，否则独占一行的引号对白会被误
切）；连续性只在同一单位（章/卷/回/…）之间比较。采信之后再按前缀形态决定
切法：前缀不含字母是纯装饰，整行当标题、前缀丢弃；前缀含汉字但在本次
采信结果里重复出现 >=2 次，说明是卷名一类属于标题自身的部分，整行当标题
且保留前缀；否则按一次性正文残片处理，只把核心到行尾当标题，前缀留给上
一章。本文件只用手写小夹具（不把生产样本整份搬进仓库），全部通过
``ingest_novel`` 的公开结果断言。
"""
from __future__ import annotations

from app.ingest import ingest_novel

# ---------------------------------------------------------------------------
# 1. 粘连标题链：残片归上一章末尾，标题本身不含残片
# ---------------------------------------------------------------------------

_GLUED_CHAIN_TEXT = (
    "第1章刘备摆摊\n"
    "刘备在摆摊卖草鞋，日子虽苦但心有大志。他低头擦拭草鞋，忽然听到身后脚步声走近，心头一震。\n"
    "他抬头一看，是关羽，两人相谈甚欢，渐生意气，决定结拜为兄弟，共谋大业，以安天下苍生怎么斩华第2章我斩华雄？\n"
    "华雄立于关前，横刀立马，诸侯军皆惧，无人敢应战，关羽主动请战，曹操亲自斟酒助威，三军肃然。\n"
    "片刻之后，鸾铃响动，马到中军，云长提华雄之首，掷于地上，其酒尚温，众人惊叹第3章袁绍绷不住了\n"
    "袁绍嗤之以鼻，不信此人能够如此轻易斩杀华雄，周围诸侯哄笑一片，气氛顿时变得微妙起来。\n"
)


def test_glued_heading_chain_splits_title_without_leading_fragment() -> None:
    chapters = ingest_novel(_GLUED_CHAIN_TEXT.encode("utf-8"))["chapters"]

    assert [c["title"] for c in chapters] == [
        "第1章刘备摆摊", "第2章我斩华雄？", "第3章袁绍绷不住了",
    ]
    # 残片留在上一章末尾，不丢也不跑进下一章标题。
    assert chapters[0]["content"].endswith("怎么斩华")
    assert "第2章" not in chapters[0]["content"]
    assert chapters[1]["content"].startswith("第2章我斩华雄？")
    assert "怎么斩华" not in chapters[1]["content"]
    assert chapters[1]["content"].endswith("众人惊叹")
    assert "第3章" not in chapters[1]["content"]
    assert chapters[2]["content"].startswith("第3章袁绍绷不住了")


# ---------------------------------------------------------------------------
# 2. 原文误编号段：序号局部连续但重复使用（如 3,4,5 后接 3',4'，内容不同）
#    仍按结构证据照样切——不要求全局单调，只要求局部相差恰好 1。
# ---------------------------------------------------------------------------

_MISNUMBERED_TEXT = (
    "这是一段开场白，介绍故事背景与主要人物，确保长度超过二百字的阈值不被误判为过短前情提要而被丢弃，"
    "同时刻意避免出现任何容易被误认成标题的序号片段，保持纯粹的背景介绍内容，便于测试环境稳定复现结果，"
    "多写几句垄满篇幅，不涉及任何剧情推进，只是单纯的铺垫文字前情提要第3章烽火初起\n"
    "诸侯初聚，各自心怀异志，暂且联手共讨国贼，营帐连绵，旌旗蔽日，气象颇为雄壮，三军肃穆战报送达第4章鼓角相闻\n"
    "两军对垲，鼓声震天，先锋交手，各有损伤，战况一度告急，诸侯营中人心浮动，议论纷纷援军抵达第5章决战之时\n"
    "决战之日终于到来，双方士气皆盛，鼓角齐鸣，战车滚滚，一场大战即将展开，无人退缩分毫战事突变第3章风云突变（续篇）\n"
    "局势突然逆转，原本胶着的战局因一支奇兵杀出而彻底改变，诸侯们始料未及，阵脚大乱，仓促应对突变持续第4章尘埃落定（续篇）\n"
    "最终尘埃落定，胜负已分，战场归于寂静，幸存者们开始清点损失，筹划下一步的去向与未来，故事暂告一段落。\n"
)


def test_misnumbered_but_locally_sequential_block_still_splits() -> None:
    chapters = ingest_novel(_MISNUMBERED_TEXT.encode("utf-8"))["chapters"]
    numbered = [c for c in chapters if c["title"].startswith("第")]

    assert [c["title"] for c in numbered] == [
        "第3章烽火初起", "第4章鼓角相闻", "第5章决战之时",
        "第3章风云突变（续篇）", "第4章尘埃落定（续篇）",
    ]
    assert "诸侯初聚" in numbered[0]["content"]
    assert "局势突然逆转" in numbered[3]["content"]
    # 两个「第3章」内容完全不同，不会被当成重复标题吞掉任何一边。
    assert "烽火初起" not in numbered[3]["content"]
    assert "风云突变" not in numbered[0]["content"]


# ---------------------------------------------------------------------------
# 3. 正文援引「第N章」不被误切——包括与真标题同序号相邻的情形
# ---------------------------------------------------------------------------

_MENTION_TEXT = (
    "『状态:更新到:第691章 千里传喜报』\n"
    "这是一段开场白，介绍故事背景与主要人物，确保长度超过二百字的阈值不被误判为过短前情提要而被丢弃，"
    "同时多写几句垄满篇幅方便测试环境稳定复现，这段纯属背景铺垫不涉及任何具体剧情推进的内容，"
    "继续补写几句确保整段长度足够超过楔子保留阈值，不会在导入时被当成过短引子直接丢弃掉，"
    "再多垄几句篇幅，保证这段开场白无论如何计算都稳稳超过二百字的门槛，便于测试环境下稳定复现结果。\n"
    "第4章风雨欲来\n"
    "乱世将至，诸侯各自为战，各怀异志。他想起第5章的往事，百感交集，不知该如何面对接下来的变局，心绪难平。\n"
    "第5章风雨已来\n"
    "诸侯会盟，共讨国贼，一时豪杰云集，气象万千，各路诸侯摩拳擦掌，准备大干一场，营中士气高涨。\n"
)


def test_metadata_line_and_same_ordinal_mention_are_not_treated_as_headings() -> None:
    chapters = ingest_novel(_MENTION_TEXT.encode("utf-8"))["chapters"]
    titles = [c["title"] for c in chapters]

    # 元数据行「第691章」与下一个候选「第4章」不连续，不会被当标题。
    assert "第691章" not in titles
    assert any("千里传喜报" in c["content"] for c in chapters)
    assert titles[-2:] == ["第4章风雨欲来", "第5章风雨已来"]
    # 正文援引「第5章」就在真标题「第5章」紧邻处（同序号），继续留在第4章正文里，不被切走。
    fourth = next(c for c in chapters if c["title"] == "第4章风雨欲来")
    assert "他想起第5章的往事" in fourth["content"]
    fifth = next(c for c in chapters if c["title"] == "第5章风雨已来")
    assert fifth["content"].startswith("第5章风雨已来")
    assert "诸侯会盟" in fifth["content"]


# ---------------------------------------------------------------------------
# 4. 行首纯装饰/编号前缀（不含汉字或字母）：整行是标题行，不往上一章挪字
# ---------------------------------------------------------------------------

def test_decorative_dash_prefix_is_not_treated_as_previous_chapter_fragment() -> None:
    text = (
        "第425章 风起之时\n"
        "风起于青萍之末，局势渐渐变得扑朔迷离，各方势力都在暗中观察，蓄势待发。\n"
        "--- 第426章 一抹风情 ---\n"
        "她转身离去，留下一抹风情，让人久久难以忘怀，街角的灯影也随之摇曳不定。\n"
    )
    chapters = ingest_novel(text.encode("utf-8"))["chapters"]

    assert [c["title"] for c in chapters] == ["第425章 风起之时", "第426章 一抹风情"]
    # 装饰前缀不是残片，不会被挪进上一章末尾——第425章末尾仍是它自己的正文。
    assert chapters[0]["content"].endswith("蓄势待发。")
    assert "---" not in chapters[0]["content"]
    # 装饰前后缀都不留在清洗后的标题里，但整行完整落在第426章正文开头。
    assert "---" not in chapters[1]["title"]
    assert "她转身离去" in chapters[1]["content"]


def test_decorative_dash_and_counter_prefix_is_not_treated_as_fragment() -> None:
    text = (
        "第257章 补贴风暴前夜\n"
        "补贴大战的消息不断传出，各方都在紧张筹备，市场气氛空前热烈，人心浮动。\n"
        "--- 259.第258章 李华百亿补贴强势上线 ---\n"
        "李华百亿补贴正式上线，瞬间引爆全网讨论，各路商家连夜调整自己的促销策略。\n"
    )
    chapters = ingest_novel(text.encode("utf-8"))["chapters"]

    assert [c["title"] for c in chapters] == ["第257章 补贴风暴前夜", "第258章 李华百亿补贴强势上线"]
    # 「259.」是装饰编号，不含汉字/字母，整行当标题行，不挪进上一章也不留在标题里。
    assert "259" not in chapters[0]["content"]
    assert "259" not in chapters[1]["title"]
    assert "李华百亿补贴正式上线" in chapters[1]["content"]


# ---------------------------------------------------------------------------
# 5. 装饰前缀候选不再无条件采信：引号开头的独立对白不能被当成标题
# ---------------------------------------------------------------------------

_QUOTED_DIALOGUE_TEXT = (
    "第一章 开场\n"
    "教练一声令下，全场顿时安静下来，所有人都紧盯着场上的局势发展，等待裁判鸣哨示意比赛正式开始。\n"
    "“第二回合开始！”\n"
    "第二章 对决\n"
    "休息室里气氛紧张，队友们互相递着水，没人说话，只有器械碰撞的声音偶尔打破寂静的气氛。\n"
    "“第三集我看过了。”\n"
    "第三章 落幕\n"
    "散场时球迷陆续退场，场馆灯光渐渐熄灭，留下一片安静，只有清洁工开始打扫看台的角落。\n"
    "“第一部分我来讲。”\n"
)


def test_quoted_dialogue_with_ordinal_is_not_split_as_decorated_heading() -> None:
    """独占一行的引号对白前缀只有「“」，不含任何字母——如果对「前缀不含
    字母」无条件采信为装饰标题，`“第二回合开始！”`/`“第三集我看过了。”`/
    `“第一部分我来讲。”` 这类对白会被整行当成新标题切出去。改为与粘连候选
    走同一条序号连续性判据后，这三行与前后真标题既不连续、单位也不同
    （回/集/部 vs 真标题的 章），全部落回正文，不产生多余章节。
    """
    chapters = ingest_novel(_QUOTED_DIALOGUE_TEXT.encode("utf-8"))["chapters"]

    assert [c["title"] for c in chapters] == ["第一章 开场", "第二章 对决", "第三章 落幕"]
    assert "“第二回合开始！”" in chapters[0]["content"]
    assert "“第三集我看过了。”" in chapters[1]["content"]
    assert "“第一部分我来讲。”" in chapters[2]["content"]


# ---------------------------------------------------------------------------
# 6. 连续性只在同一单位之间比较：卷后粘连的「章」不与卷号比连续
# ---------------------------------------------------------------------------

_VOLUME_UNIT_MISMATCH_TEXT = (
    "第一卷 风起\n"
    "这一卷的故事才刚刚开始，主角尚未展露锋芒，所有的变数都还深埋在暗处等待激活，"
    "这其实只是一句不相关的提示语句，接下来这一段纯粹用来垄满篇幅不涉及具体剧情推进"
    "第2章节奏加快\n"
    "后续情节在这一卷里继续推进，暗藏的变数渐渐浮出水面，主角逐步意识到事情并不像表面那样简单。\n"
)


def test_unit_mismatch_neighbor_does_not_count_as_continuous() -> None:
    """「第一卷」（单位=卷，序号1）与粘连候选「第2章」（单位=章，序号2）数值
    上相差恰好 1，但单位不同——如果连续性判据跨单位比较数字，会把两者错判
    成连续从而误切。要求紧邻候选单位相同才计入连续性/同序号比较后，这条
    粘连候选的前后邻居都不是「章」单位，判定为不连续，继续留在「第一卷」
    正文里，不产生「第2章」这一假章节。
    """
    chapters = ingest_novel(_VOLUME_UNIT_MISMATCH_TEXT.encode("utf-8"))["chapters"]

    assert len(chapters) == 1
    assert chapters[0]["title"] == "第一卷 风起"
    assert "第2章节奏加快" in chapters[0]["content"]


# ---------------------------------------------------------------------------
# 7. 原有独占行格式（标题单独占一行，无粘连）结果不变
# ---------------------------------------------------------------------------

def test_standalone_heading_format_is_unaffected() -> None:
    text = "第一章 开端\n" + "正文内容平铺直叙。" * 40 + "\n第二章 发展\n" + "正文内容继续推进。" * 40
    result = ingest_novel(text.encode("utf-8"))

    assert result["auto_split"] is False
    assert [c["title"] for c in result["chapters"]] == ["第一章 开端", "第二章 发展"]
    assert all(c["content"].startswith(c["title"]) for c in result["chapters"])
