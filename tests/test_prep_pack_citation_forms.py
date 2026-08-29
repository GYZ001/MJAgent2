"""引文收尾符号的锚点定位：模型抄到语义完整处停笔并替换句末标点，不该拦停整集。

真实故障 ERR-20260828-f819b0（《我欲封天》EP1「靠山宗山腰青石坪」）。
"""
from app.production.prep_pack import (
    _prep_pack_citation_forms,
    _prep_pack_locate_phrase,
)


class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


SOURCE = [
    _Seg("“仙人？”他坚持了数十息的时间，便难以承受，眼前一黑昏了过去。"),
    _Seg(
        "当他睁开眼睛时，已经在了一处半山腰的青石空地上，四周山峦起伏，"
        "云雾缭绕绝非凡尘，能看到一些精美的阁楼环绕山峦八方，满眼陌生。"
    ),
]


def test_terminal_period_substituted_for_source_comma_still_locates():
    """模型把原文的「，」写成「。」收尾——差一个标点，正文逐字全对。"""
    quote = "当他睁开眼睛时，已经在了一处半山腰的青石空地上，四周山峦起伏，云雾缭绕绝非凡尘。"
    segments, phrase = _prep_pack_locate_phrase(SOURCE, quote)
    assert segments == [2]
    # 落库的必须是真正出现在原文里的那个串，不是模型原样上报的串
    assert phrase == quote.rstrip("。")
    assert phrase in SOURCE[1].text


def test_quote_marks_and_terminal_mark_combined():
    """两端引号 + 句末标点同时存在时也要能剥到命中。"""
    segments, phrase = _prep_pack_locate_phrase(
        SOURCE, "“当他睁开眼睛时，已经在了一处半山腰的青石空地上。”"
    )
    assert segments == [2]
    assert phrase == "当他睁开眼睛时，已经在了一处半山腰的青石空地上"


def test_exact_quote_returned_unchanged():
    """本来就逐字命中的引文不得被改动。"""
    quote = "四周山峦起伏，云雾缭绕绝非凡尘，能看到一些精美的阁楼环绕山峦八方，满眼陌生。"
    segments, phrase = _prep_pack_locate_phrase(SOURCE, quote)
    assert segments == [2]
    assert phrase == quote


def test_fabricated_and_paraphrased_quotes_still_fail_closed():
    """剥的是符号不是文字：编造/改写/漏字的引文必须仍然定位不到。"""
    for bad in (
        "当他睁开双眼时，已经在了一处半山腰的青石空地上。",      # 改写用词
        "他站在半山腰的青石空地上，四周云雾缭绕。",                # 重新组句
        "当他睁开眼睛时，已经在了一处山腰的青石空地上，四周山峦起伏。",  # 漏一个「半」字
        "当他睁开眼睛时，已经在了一处半山腰的青石空地上，仙鹤盘旋。",    # 后半段编造
    ):
        assert _prep_pack_locate_phrase(SOURCE, bad) == ([], ""), bad


def test_citation_forms_do_not_strip_interior_punctuation():
    """只剥收尾符号，句子内部的标点一个都不能动。"""
    forms = _prep_pack_citation_forms("甲说，乙答。")
    assert "甲说，乙答" in forms
    assert all("，" in f for f in forms), forms


def test_empty_and_punctuation_only_phrases_are_not_anchors():
    assert _prep_pack_locate_phrase(SOURCE, "") == ([], "")
    assert _prep_pack_locate_phrase(SOURCE, "。。。") == ([], "")
