"""古典小说人物自我介绍形态的确定性解析：「姓关，名羽，字长生，后改云长」→ 全名 关羽，字/改名 {长生, 云长}。

真实事故（2026-09-02《三国演义》旧项目）：第一回里「长生」先于「关羽」出现，卡合并判定问「长生是不是
候选之一」时候选集里还没有关羽，模型答「都不是」→ 建了「长生」卡；随后「关羽」出现，合并判定虽选中
「长生」，钉证要求引句逐字含「关羽」，而原文写的是「姓关，名羽」——两字不相连——被拒，于是又建了
「关羽」卡。同一形态还有 玄德/刘备、翼德/张飞、孟德/曹操。

这里解析的是**文本的显式声明**（「姓 X，名 Y，字 Z」是作者写下的身份链接句），不是按名字先验猜测：
每一条结果都带逐字引句，可回原文复核。识别不到就返回空，绝不编造。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 「姓X，名Y，字Z」——姓允许复姓（1–2 字），名 1–2 字，字可选；后接「后改/又名/号 W」也收进别名。
_INTRO_FULL = re.compile(
    r"姓(?P<surname>[一-龥]{1,2})\s*[，,、]?\s*名(?:叫|为|曰)?\s*(?P<given>[一-龥]{1,2})"
    r"(?:\s*[，,、]?\s*字\s*(?P<courtesy>[一-龥]{1,2}))?"
)
# 「刘备，字玄德」——全名紧接「，字 Z」。全名不能以 姓/名/叫/为/曰/氏 这类语法字开头（那是「姓X名Y」
# 形态或动词，不是全名的一部分；「此人叫徐庶，字元直」解析出的全名是「徐庶」）。
_INTRO_COURTESY_ONLY = re.compile(r"(?P<full>(?![姓名叫为曰氏])[一-龥]{2,3})\s*[，,]\s*字\s*(?P<courtesy>[一-龥]{1,2})")
# 紧随其后的「后改 W」「又名 W」「号 W」「小字 W」。
_ALT_NAMES = re.compile(r"[，,]\s*(?:后改|又名|号|小字|一名)\s*(?P<alt>[一-龥]{1,2})")


@dataclass(frozen=True)
class NameIntroduction:
    full_name: str
    alt_names: tuple[str, ...]
    quote: str
    aliases_kind: str = field(default="courtesy_name")


def _alt_names_after(text: str, end: int) -> list[str]:
    alts: list[str] = []
    pos = end
    while True:
        match = _ALT_NAMES.match(text, pos)
        if match is None:
            return alts
        alts.append(match.group("alt"))
        pos = match.end()


def find_name_introductions(text: str) -> list[NameIntroduction]:
    """返回文本里全部显式的姓名介绍；每条带原文逐字引句（含后续的「后改/又名」部分）。"""
    found: list[NameIntroduction] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for pattern in (_INTRO_FULL, _INTRO_COURTESY_ONLY):
        for match in pattern.finditer(text):
            if pattern is _INTRO_FULL:
                full = match.group("surname") + match.group("given")
            else:
                full = match.group("full")
            alts = [match.group("courtesy")] if match.group("courtesy") else []
            alts.extend(_alt_names_after(text, match.end()))
            alts = [alt for alt in dict.fromkeys(alts) if alt and alt != full]
            if not alts:
                continue
            key = (full, tuple(alts))
            if key in seen:
                continue
            seen.add(key)
            quote_end = match.end()
            for alt in alts:
                idx = text.find(alt, match.end())
                if idx >= 0:
                    quote_end = max(quote_end, idx + len(alt))
            found.append(NameIntroduction(full_name=full, alt_names=tuple(alts), quote=text[match.start():quote_end]))
    return found


def intro_owner_of(label: str, intros: list[NameIntroduction]) -> NameIntroduction | None:
    """``label`` 若是某条介绍里的字/改名，返回那条介绍（全名即归属者）；找不到或多条全名不一致返回 None。"""
    owners = {intro.full_name: intro for intro in intros if label in intro.alt_names}
    if len(owners) != 1:
        return None
    return next(iter(owners.values()))
