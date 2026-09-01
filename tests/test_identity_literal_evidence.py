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

    唯一命中时调用方会自动改绑、根本走不到这里；命中多条才会硬失败。
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
