"""拼接引文的锚点定位：模型把原文里不相连的两句接成一条 quote，不该拦停整集。

真实故障 ERR-20260902-507cb0（《三国演义》第一回「涿县榜文告示处」）：quote 为
「榜文行到涿县，引出涿县中一个英雄。当日见了榜文，慨然长叹。」，两句在原文中相隔数十字，
整条逐字不命中；三路候选全部落空，来源证明自校验拦停，两轮重试模型给出一字不差的引文。
放行的边界：每一句都逐字命中、次序与原文一致；任一句编造或次序颠倒仍失败关闭。
"""
from app.production.prep_pack import _prep_pack_locate_phrase


class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


SOURCE = [
    _Seg("刘焉然其说，随即出榜招募义兵。"),
    _Seg("榜文行到涿县，引出涿县中一个英雄。那人不甚好读书；性宽和，寡言语。"),
    _Seg("玄德当日见了榜文，慨然长叹。随后一人厉声言曰：大丈夫不与国家出力，何故长叹？"),
]


def test_stitched_quote_of_verbatim_sentences_anchors_on_the_longest():
    quote = "榜文行到涿县，引出涿县中一个英雄。当日见了榜文，慨然长叹。"
    segments, phrase = _prep_pack_locate_phrase(SOURCE, quote)
    assert segments == [2]
    assert phrase == "榜文行到涿县，引出涿县中一个英雄。"
    assert phrase in SOURCE[1].text  # 落库的锚点必须真在原文里，自校验才能复核


def test_whole_quote_verbatim_takes_precedence_over_sentences():
    quote = "玄德当日见了榜文，慨然长叹。随后一人厉声言曰：大丈夫不与国家出力，何故长叹？"
    segments, phrase = _prep_pack_locate_phrase(SOURCE, quote)
    assert segments == [3]
    assert phrase == quote


def test_partially_invented_stitched_quote_is_rejected():
    """拼接里只要有一句编造，整条拒绝——不能借一句真句给编造内容背书。"""
    assert _prep_pack_locate_phrase(SOURCE, "榜文行到涿县，引出涿县中一个英雄。翼德在旁怒喝。") == ([], "")


def test_reversed_order_stitching_is_rejected():
    """两句都在原文里但次序颠倒：这是改写，不是引用格式。"""
    assert _prep_pack_locate_phrase(SOURCE, "当日见了榜文，慨然长叹。榜文行到涿县，引出涿县中一个英雄。") == ([], "")


def test_all_short_sentences_are_not_accepted_as_anchors():
    """句句都真但最长的不足 6 字（「寡言语」）没有区分度，不作锚点。"""
    assert _prep_pack_locate_phrase(SOURCE, "性宽和。寡言语。") == ([], "")


def test_fabricated_quote_still_fails_closed():
    assert _prep_pack_locate_phrase(SOURCE, "玄德看榜叹息。翼德在旁怒喝。") == ([], "")
