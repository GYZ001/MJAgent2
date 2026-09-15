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

_SENTENCE_SPLIT_RE = re.compile(r"[。；;\n]+")
_CLAUSE_SPLIT_RE = re.compile(r"[，,、]+")
_FUNCTION_CHARS = set("的地得着了一张件把其中为呈缀根只对双条撮缕")  # 量词、系词、「缀/呈」不是特征
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
# 非人角色的通用形态词元只参与【外观子句】落地，不进 _GENERIC_RE：卡名/别名的具体性判据
# （card_aliases.alias_is_specific / card_name_is_specific）也复用 _GENERIC_RE，物种词进去后
# 「龙猫」「小虎」这类由物种词构成的真名会被判成「指谁都行的通称」而拒绝建卡（2026-09-15 实测：
# 龙猫四轮判定全过，卡名一步静默拒绝）。
_CREATURE_TOKENS: tuple[str, ...] = tuple(sorted({
    # 非人角色的通用形态（作为角色出镜的动物 / 拟人化生物 / 灵兽 / 数字生物）：物种、毛色毛长、
    # 体型、部位形态、眼睛颜色——对它们而言等价于人的「性别年龄感 / 发型发色 / 服装」，原文没写
    # 可按画风设定。2026-09-15《龙猫出爪》：主角是猫，「明黄色短毛猫」「橘白相间的家猫」整句被当
    # 标志性特征删空，20 字下限过不去，主角建不了卡。项圈、铃铛、伤疤、法器仍是标志性特征。
    "猫", "猫咪", "家猫", "小猫", "狗", "小狗", "犬", "狐", "狐狸", "兔", "鼠", "鸟", "雀", "鹰", "龙", "蛇", "虎", "狼",
    "熊", "鹿", "马", "牛", "羊", "猪", "猴", "鱼", "龟", "蛙", "兽", "灵兽", "妖兽", "生物", "幼崽", "猫龄", "外观", "整体",
    "毛", "毛发", "皮毛", "绒毛", "短毛", "长毛", "毛色", "毛茸茸", "蓬松", "顺滑", "光泽", "斑纹", "条纹", "虎斑", "花纹",
    "相间", "三花", "橘白", "橘", "橙", "奶白", "明黄", "纯色", "双色",
    "尾", "尾巴", "尾尖", "长尾", "短尾", "耳", "耳朵", "尖耳", "垂耳", "爪", "爪子", "肉垫", "须", "胡须", "长须",
    "鼻", "嘴", "眼", "眼睛", "双眼", "圆眼", "瞳", "琥珀", "翠绿", "碧绿", "湛蓝", "金黄",
    "圆胖", "圆滚滚", "小巧", "灵动", "掌心大", "巴掌大", "肥胖", "修长", "匀称", "壮",
    "普通", "常见", "家养", "宠物", "脸型", "圆脸", "脸", "圆钝", "头部", "头", "身躯", "躯干", "四肢", "前肢", "后肢",
    "腿", "背", "腹部", "腹", "胸", "颈", "脖子", "眼瞳", "瞳孔", "竖瞳", "杏眼", "杏", "鼻头", "嘴巴", "牙", "翅膀", "翅",
    "羽毛", "羽", "鳞片", "鳞", "犄角", "角", "软乎乎", "软", "蓬", "圆润", "细长", "偏胖", "体型", "娇小", "瘦小",
    "黑白", "斑点", "花斑", "纹路", "柔软", "爪垫", "粉润", "软萌", "萌", "物种", "偏小", "偏大", "圆头", "竖耳", "粗细", "匀整",
}, key=len, reverse=True))
_GENERIC_RE = re.compile("|".join(re.escape(t) for t in _GENERIC_TOKENS))
_GROUNDING_GENERIC_RE = re.compile("|".join(
    re.escape(t) for t in sorted({*_GENERIC_TOKENS, *_CREATURE_TOKENS}, key=len, reverse=True)
))
# 叙事时序词元（闭集语法成分）：带这些词的子句写的是剧情经过，不是可跨镜稳定复现的静态外观
# （第 11 轮：赵武刚「本集短暂变为兽化形态，后恢复人形，最终尸体…」整段进了外观锚点）。
_NARRATIVE_RE = re.compile(r"本集|随后|后来|最终|最后|短暂|一度|曾经|曾|变为|变成|化作|恢复|尸体|死后|此刻|当时|之后|之前")
# 部位形态子句（结构判据，2026-09-15）：写的是某个身体部位的形状/颜色/质地（「琥珀色圆形眼眸」
# 「尾巴修长柔顺」「毛发光泽感强」）就是通用形态，不要求原文逐字依据——模型描述动物部位的措辞
# 是开放集合，逐词补表追不上（龙猫提名连续三轮各换一套词，全被删到 6～18 字）。但子句里一旦
# 出现饰物 / 材质 / 伤痕 / 器物标记（项圈、铃铛、佩戴、疤、甲、袍、剑…），它就是标志性特征，
# 仍走原文依据核验。两张表都是闭集语法成分，不是任何角色的名单。
_BODY_PART_RE = re.compile(
    r"眼|眸|瞳|耳|尾|须|胡|爪|掌|肉垫|毛|头|脸|面部|鼻|嘴|牙|齿|舌|颈|脖|身形|身躯|身材|体型|体态|背|腹|肢|腿|足|蹄|翅|翼|羽|鳞|犄角|鬃|喙"
)
_ADORNMENT_RE = re.compile(
    r"项圈|铃|环|链|坠|佩|戴|挂|缠|披|系|疤|伤|纹身|刺青|甲|铠|袍|衣|裙|衫|装|器|剑|刀|枪|杖|珠|玉|宝|符|印|鞍|缰|兽皮|皮甲|皮革|绸|锦|铁|铜|金饰"
)
# 「可切换掌心大小」「能变大」是能力不是静态形态，不享受部位形态放行，照旧走原文依据核验。
_NON_STATIC_RE = re.compile(r"可切换|切换|可变|能变|会变|变大|变小|缩小|放大")


def _body_part_form(clause: str) -> bool:
    """部位形态子句：含身体部位词，且不含饰物/材质/伤痕/器物标记与能力描述。"""
    return bool(_BODY_PART_RE.search(clause)) and not _ADORNMENT_RE.search(clause) and not _NON_STATIC_RE.search(clause)


def _residue(clause: str) -> str:
    """去掉通用形态词元与功能字后剩下的字符（保持原顺序，非通用片段之间用空格分开）。"""
    text = _GROUNDING_GENERIC_RE.sub(" ", clause)
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
    for sentence in (x.strip() for x in _SENTENCE_SPLIT_RE.split(text)):
        clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(sentence) if c.strip()]
        if _NARRATIVE_RE.search(sentence):
            dropped.extend(clauses)  # 整句在讲剧情经过（变为/恢复/最终…），不是外观
            continue
        for clause in clauses:
            residue = _residue(clause)
            if not residue or _body_part_form(clause) or _grounded(residue, source):
                kept.append(clause)
            else:
                dropped.append(clause)
    if not dropped:
        return text, []
    return "，".join(kept), dropped
