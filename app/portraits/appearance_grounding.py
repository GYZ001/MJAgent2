"""外观锚点串逐子句落地（2026-09-06 用户截图：许师姐被写成「外宗弟子灰布劲装，披着黄褐色虎皮」，原文一个字都没有）。

``assess_new_character`` 的契约本来就是：通用形态（性别年龄感 / 发型发色 / 服装款式与颜色 / 身形）
原文没写可按画风设定，**标志性特征只有原文对这个角色本人确有描写才写**。但契约只在提示词里，
没有闸门——模型编了虎皮，卡就带着虎皮出图。这里把契约做成确定性判据，逐子句核对：

* 子句完全由通用形态词元组成（年龄性别 / 发型发色 / 款式+颜色+材质的服装 / 身形）→ 保留；
* 否则子句必须在这个角色的原文片段里有依据：去掉通用词元后的剩余部分，其 ≥3 字的连续片段或
  ≥ 一半的二字词逐字出现在片段里 → 保留；
* 两者都不满足 → 删掉这一子句（不是整张卡），删掉的子句随判定返回，让缺失可见、可复核；
  卡因此变薄时 ``assess_new_character`` 会带着被删子句重问一次。

词元表是闭集语法形态（同 ``card_owner.RELATIONAL_TITLE_SUFFIXES`` 的性质），不是任何人物的
黑名单；判据「有没有原文依据」来自片段本身。
"""
from __future__ import annotations

import re

_CLAUSE_SPLIT_RE = re.compile(r"[，,；;。、\n]+")
_FUNCTION_CHARS = set("的地得着了一张件把其中")
_CJK_RE = re.compile(r"[一-鿿]")

# 通用形态词元（长的在前，保证「修真青年」先于「青年」、「灰白」先于「白」匹配）。
_GENERIC_TOKENS: tuple[str, ...] = tuple(sorted({
    # 年龄 / 性别
    "约莫", "看起来", "年约", "余岁", "许岁", "岁", "年轻", "少年", "少女", "青年", "中年偏老", "中年", "老年", "幼年", "孩童",
    "修真青年", "青年修士", "女修士", "男修士", "成年", "男子", "女子", "女性", "男性", "老者", "老人", "修士", "女修", "男修", "身形", "模样", "样貌",
    "十六七", "十七八", "十八九", "二十许", "二十余", "三十许", "四十许", "五十许",
    # 发型 / 发色
    "乌黑", "黑色", "黑发", "黑", "花白", "银白", "灰白", "雪白", "白发", "白", "棕", "褐", "长发", "短发", "头发", "发色", "发", "高束", "束起",
    "束发", "束成马尾", "双马尾", "马尾", "束冠", "简单束起", "束高髻", "披肩", "披散", "盘起", "扎起", "扎着", "垂肩", "及腰", "利落", "高髻", "束",
    # 服装：动词 / 颜色 / 材质 / 款式
    "身着", "身穿", "穿着", "穿", "着", "一身", "素白", "素", "灰", "青", "蓝", "绿", "红", "紫", "黄", "银", "金", "粉", "墨", "藏青", "深色", "浅色", "深", "浅", "色",
    "底", "镶边", "粗布", "棉布", "布", "麻", "长袍", "长衫", "劲装", "布衣", "短打", "短褐", "短衫", "道袍", "外袍", "衣袍", "长裙", "衣裙", "布衫", "袍", "衫", "裙",
    # 身形
    "体态", "身材", "体格", "壮实", "挺拔", "高大", "中等", "偏瘦", "微胖", "壮硕", "清瘦", "纤细", "矮小", "健壮", "瘦削", "如松",
    # 数字
    "一", "二", "两", "三", "四", "五", "六", "七", "八", "九", "十",
}, key=len, reverse=True))
_GENERIC_RE = re.compile("|".join(re.escape(t) for t in _GENERIC_TOKENS))


def _residue(clause: str) -> str:
    """去掉通用形态词元与功能字后剩下的字符（保持原顺序，非通用片段之间用空格分开）。"""
    text = _GENERIC_RE.sub(" ", clause)
    text = "".join(ch if (_CJK_RE.match(ch) and ch not in _FUNCTION_CHARS) else " " for ch in text)
    return " ".join(part for part in text.split() if part)


def _grounded(residue: str, fragments: str) -> bool:
    if not fragments:
        return False
    parts = residue.split()
    for part in parts:
        if len(part) >= 3 and any(part[i:i + 3] in fragments for i in range(len(part) - 2)):
            return True
    grams = [p[i:i + 2] for p in parts for i in range(len(p) - 1)]
    singles = [p for p in parts if len(p) == 1]
    if grams:
        return sum(1 for g in grams if g in fragments) * 2 >= len(grams)
    return bool(singles) and all(s in fragments for s in singles)


def ground_appearance(appearance: str, fragments: str) -> tuple[str, list[str]]:
    """返回 (保留下来的外观串, 被删掉的子句)。空串或全部子句可保留时原样返回。"""
    text = str(appearance or "").strip()
    if not text:
        return text, []
    source = str(fragments or "")
    kept: list[str] = []
    dropped: list[str] = []
    for clause in (c.strip() for c in _CLAUSE_SPLIT_RE.split(text)):
        if not clause:
            continue
        residue = _residue(clause)
        if not residue or _grounded(residue, source):
            kept.append(clause)
        else:
            dropped.append(clause)
    if not dropped:
        return text, []
    return "，".join(kept), dropped
