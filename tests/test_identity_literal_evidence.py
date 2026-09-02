"""app.portraits.identity_literal_evidence：named 称谓的逐字出处查找与报错文案。

真实事故 ERR-20260901-0fbce1 / ERR-20260901-ca388b（《金瓶梅词话》第一回）：模型
按民间说法把「武大」写成本名「武植」，整集硬失败，而错误只说「缺少逐字 owned
evidence：武植」——排查的人无从判断这是"绑错了证据"还是"名字压根是编的"，用户看到
的更是天书。治因在提示词（constants.IDENTITY_LITERAL_LABEL_RULE），这里守住的是
"拦住用户时必须说清拦的是什么"。
"""
from __future__ import annotations

from app.portraits.identity_literal_evidence import (
    literal_owned_matches,
    literal_rebind_target,
    named_literal_miss_verdict,
)

_EVIDENCE = {
    "E001": {"text": "這武松因酒醉，打了童樞密。"},
    "E002": {"text": "只見武大買了些肉菜、果餅歸來。"},
    "E003": {"text": "武大自依前上街賣炊餅。"},
}


def test_matches_are_literal_substrings_not_fuzzy() -> None:
    assert len(literal_owned_matches("武大", _EVIDENCE)) == 2
    assert literal_owned_matches("武植", _EVIDENCE) == []
    # 空 label 不该匹配到"每一条证据都包含空串"这种退化结果。
    assert literal_owned_matches("", _EVIDENCE) == []


def test_zero_match_is_dropped_and_logged_not_escalated(caplog) -> None:
    """整批 0 命中 = 可被证据证伪的编造：就地丢弃（返回 None），并留下可见 WARNING。"""
    with caplog.at_level("WARNING"):
        assert named_literal_miss_verdict("武植", _EVIDENCE) is None
    assert "丢弃无出处名字：武植" in caplog.text
    assert "3 条证据" in caplog.text


def test_multi_match_says_the_binding_is_ambiguous_not_fabricated() -> None:
    """名字确实在原文里、只是绑错了 E：这不是编名字，报错要说成另一回事。

    调用方先走 ``literal_rebind_target``（named 多条命中也改绑），所以这个分支只剩
    防御意义；但文案仍须说清是"绑错了"而不是"编造"。
    """
    message = named_literal_miss_verdict("武大", _EVIDENCE)
    assert message is not None
    # 既有调用方（tests/test_character_discovery.py）按这个前缀匹配报错。
    assert message.startswith("current named 缺少逐字 owned evidence：武大")
    assert "另有 2 条证据逐字含它" in message


def test_empty_evidence_catalog_drops_too(caplog) -> None:
    """一条证据都没有时同样无从证实，按编造丢弃，不得升级成整集失败。"""
    with caplog.at_level("WARNING"):
        assert named_literal_miss_verdict("武植", {}) is None
    assert "0 条证据" in caplog.text


# ERR-20260902-c587ac《三国演义》第一回：E003 写「姓张，名飞，字翼德」，逐字串「张飞」
# 只出现在 E004–E007。模型把张飞钉在 E003，旧规则多条命中即整集硬失败且重试必现。
_SANGUO = {
    "E001": {"text": "第一回  宴桃园豪杰三结义斩黄巾英雄首立功"},
    "E002": {"text": "话说天下大势，分久必合，合久必分。"},
    "E003": {"text": "其人曰：某姓张，名飞，字翼德。世居涿郡。"},
    "E004": {"text": "张飞曰：吾庄后有一桃园，花开正盛。"},
    "E005": {"text": "云长、张飞一齐出马，杀入贼阵。"},
    "E006": {"text": "张飞大怒，挺矛直取邓茂。"},
    "E007": {"text": "玄德引关、张飞救了董卓回寨。"},
}


def test_named_multi_match_rebinds_to_first_literal_evidence(caplog) -> None:
    """named 多条命中：确定性改绑到目录顺序上的首条逐字出处，并留下可见 WARNING。"""
    with caplog.at_level("WARNING"):
        target = literal_rebind_target("张飞", _SANGUO, "named")
    assert target is _SANGUO["E004"]
    assert "改绑到首条逐字出处：张飞" in caplog.text
    assert "4 条证据逐字含它" in caplog.text


def test_named_single_match_rebinds_quietly(caplog) -> None:
    with caplog.at_level("WARNING"):
        assert literal_rebind_target("邓茂", _SANGUO, "named") is _SANGUO["E006"]
    assert "改绑" not in caplog.text


def test_named_zero_match_is_not_rebound() -> None:
    """一条都不含时不改绑，交给 named_literal_miss_verdict 走"编造→丢弃"。"""
    assert literal_rebind_target("张翼德", _SANGUO, "named") is None


def test_functional_multi_match_is_still_ambiguous() -> None:
    """描述性称谓多段各指不同的人是真正的身份歧义：functional 只在唯一命中时改绑。"""
    assert literal_rebind_target("张飞", _SANGUO, "functional") is None
    assert literal_rebind_target("邓茂", _SANGUO, "functional") is _SANGUO["E006"]
