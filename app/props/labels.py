"""道具标签归一（2026-09-06 第 11 轮物件库核查：野鸡/两只野鸡、大旗/养丹坊分店的大旗、灵石/半块灵石/
凝灵丹与半块灵石 各建了一条）。

道具标签是模型从原文抽的短语，同一件东西会带着数量、领属、并列写出来。登记前按汉语语法拆成
「物件本体」：
* 并列（与/和/及/、）拆成多件：「凝灵丹与半块灵石」→ 凝灵丹、半块灵石；
* 数量前缀（数词 + 量词，「两只」「半块」「九根」「一枚」）剥掉：「两只野鸡」→ 野鸡；
* 领属前缀（「X的」）剥掉：「养丹坊分店的大旗」→ 大旗，剩余部分至少两字才剥。
剥的是语法成分不是词表：量词表是闭集语法单位，同 ``card_owner.RELATIONAL_TITLE_SUFFIXES``。
原标签作为别名登记到本体上，下游按别名仍能查到图。
"""
from __future__ import annotations

import re

_SPLIT_RE = re.compile(r"[与和及、，,]")
_QUANTIFIER_RE = re.compile(
    r"^(?:[一二两三四五六七八九十百千半几数多\d]+)(?:个|只|根|把|枚|块|张|件|条|支|柄|颗|粒|口|面|座|串|双|对|片|瓶|本|卷|盏|匹|头|名|位)?"
)
_POSSESSIVE_RE = re.compile(r"^(?P<owner>[一-鿿]{1,8})的(?P<rest>[一-鿿]{2,})$")


def normalize_prop_label(label: str) -> list[str]:
    """返回该标签指向的物件本体名列表（去重、保序）；无法归一时返回 [原标签]。"""
    text = str(label or "").strip()
    if not text:
        return []
    out: list[str] = []
    for part in (p.strip() for p in _SPLIT_RE.split(text)):
        if not part:
            continue
        base = _QUANTIFIER_RE.sub("", part, count=1) or part
        owner = _POSSESSIVE_RE.match(base)
        if owner:
            base = owner.group("rest")
        base = base.strip()
        if len(base) < 2:
            base = part  # 剥完只剩一个字（「半块石」→「石」不成词）：保留原样
        if base not in out:
            out.append(base)
    return out or [text]
