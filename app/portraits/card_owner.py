"""角色卡的唯一身份归属解析器。

判断"这个称呼字符串是否已经属于人物谱里的某个角色"是一个全仓库反复出现的判断，
过去每一处调用点各自实现了一遍，且全部只比对 ``character.name`` 精确相等、无视
``character.aliases[].text``——人物谱里登记「李富贵（别名：小胖子）」时，分集发现
解析出 canonical_name「小胖子」会查无此人、于是建出第二张卡。本模块是这个判断
唯一的落地点：新的调用点只应从这里取判据，不要各写一套。

底层匹配原语只有一条：``_character_claims_label``——单个角色是否通过【精确相等，
不做子串】认领了某个称呼字符串。``resolve_card_owner`` 在其上组合 name+alias 两个
字段，回答"这是不是人物谱里已有的角色"；``app/production/prep_pack/alias_resolution.py``
的 ``_prep_pack_bible_alias_owner`` / ``_prep_pack_bible_alias_conflicting_owner``
目前仍各自内联同款精确匹配逻辑，只匹配 alias、不匹配 name——它们服务的是姓名改绑
/跨集别名冲突检测语义，与本模块的建卡去重语义是两种不同分工，其 docstring 已明确
"两者分工跟历史的读侧函数/冲突检查函数两分是同构的，不合并成一个函数"，本次改动
不触碰它们。将来这两个函数收敛到本模块时，只应该复用 ``_character_claims_label``
这一条原语（``include_name=False``），不应该改变各自的字段范围；本模块的匹配判据
（精确相等、无子串）必须与它们当前行为保持一致，不要出现第三种漂移的写法。

conflict 判据与 ``app/identity_authority.py``（见该文件 320-341 行）算出的
``colliding_alias_texts`` 是同一个算法形状："按精确文本分组、命中 ≥2 个不同角色
即视为非排他/歧义"。区别在于那里额外做了 ``is_exclusive`` 预过滤——只把角色自己
标记为"排他"的别名计入碰撞集合，用来决定能不能折进身份决议的 ``source_labels``；
本模块回答的是一个更基础的问题："这个称呼能不能安全地当作已有角色、从而跳过建卡"，
任何一次精确命中（无论 ``is_exclusive``）都构成实际的去重风险，因此这里不做该预
过滤。实测 ``proj_195be7df1fd6`` 里"大汉"同时是"曹阳"和"虎爷"的别名（且两条都
标记 ``is_exclusive=False``——"大汉"只是非排他的描述性称谓，两人确实是不同的人），
``resolve_card_owner`` 对"大汉"必须返回 conflict 并 fail closed，不能因为两条别名
各自"非排他"就悄悄放行成 none——那会让建卡逻辑把这个真实存在的歧义当成"查无此
人"，制造第三张卡。

禁止子串匹配：合并方向的子串匹配会把"许"并进"许清"。``app/stages/roster_recurring.py``
的 ``_bible_covers_name`` 用的是双向子串，那是"开谱后补录检查"的口径（宁可多补），
方向与本模块相反，不是建卡去重口径，本模块不参照它、也不应该被改动。
"""

from __future__ import annotations

from app.schemas import Bible, Character


def _character_claims_label(
    character: Character,
    label: str,
    *,
    include_name: bool,
    include_aliases: bool,
) -> bool:
    """单个角色是否通过精确相等（不做子串）认领了 ``label``——全仓库【建卡去重】
    口径唯一的底层匹配原语，语义边界见模块 docstring。"""
    if include_name and str(getattr(character, "name", "") or "").strip() == label:
        return True
    if include_aliases:
        for alias in getattr(character, "aliases", None) or []:
            if str(getattr(alias, "text", "") or "").strip() == label:
                return True
    return False


def resolve_card_owner(
    bible: Bible, label: str,
) -> tuple[str, str] | tuple[str, list[str]]:
    """``label``（人物谱之外的称呼字符串，如剧本/发现管线解析出的 canonical_name）
    是否已经属于人物谱里的某个角色。

    返回三态：
    - ``("owner", name)``：精确命中且只命中一个角色（通过 name 或 alias.text）。
    - ``("none", "")``：没有命中任何角色，可以安全建新卡。
    - ``("conflict", [names])``：精确命中 ≥2 个不同角色——真实存在的合法数据
      （见模块 docstring "大汉" 案例），fail closed：调用方不得在这里自行猜测
      归属，只能把它当"已有归属、不安全新建"处理，真名改绑是另一件事（不在本
      解析器职责内）。
    """
    label = str(label or "").strip()
    if not label:
        return ("none", "")
    owners = _exact_owners(bible, label)
    if not owners:
        # 「登记名＋关系称谓」是同一个人的派生写法（原文里「韩宗师兄」就是人物谱里的
        # 「韩宗」，2026-09-05 第 5 集实测：按独立群演建了第二张卡、分镜台显示无定妆照）。
        # 判据仍是精确相等：去掉一个闭集里的称谓后缀后，剩下的字符串必须逐字等于某个
        # 登记名/别名；不是子串匹配，也不猜姓氏（「上官师叔」剩「上官」查无此人就仍是 none）。
        stem = strip_relational_title(label)
        if stem:
            owners = _exact_owners(bible, stem)
    if not owners:
        return ("none", "")
    if len(owners) == 1:
        return ("owner", owners[0])
    return ("conflict", owners)


def _exact_owners(bible: Bible, label: str) -> list[str]:
    owners: list[str] = []
    for character in getattr(bible, "characters", None) or []:
        name = str(getattr(character, "name", "") or "").strip()
        if not name or name in owners:
            continue
        if _character_claims_label(
            character, label, include_name=True, include_aliases=True,
        ):
            owners.append(name)
    return owners


# 汉语里挂在人名后面的关系/尊称后缀：闭集、语法性质，不是任何人物的黑名单。长的在前，
# 保证「师兄」先于「兄」匹配。单字后缀只在剩余部分 ≥2 字时才考虑（「王兄」剩「王」不算）。
RELATIONAL_TITLE_SUFFIXES: tuple[str, ...] = (
    "师兄", "师姐", "师弟", "师妹", "师叔", "师伯", "师祖", "师父", "师傅", "师尊",
    "长老", "前辈", "道友", "道长", "公子", "姑娘", "小姐", "大人", "仙子", "真人",
    "上人", "老祖", "老爷", "掌门", "宗主", "先生", "夫人",
    "兄", "姐", "弟", "妹", "叔", "伯", "爷", "哥",
)


def strip_relational_title(label: str) -> str | None:
    """``label`` 以一个关系称谓后缀结尾且去掉后剩 ≥2 字时返回剩余部分，否则 None。"""
    text = str(label or "").strip()
    for suffix in RELATIONAL_TITLE_SUFFIXES:
        if text.endswith(suffix) and len(text) - len(suffix) >= 2:
            return text[: -len(suffix)]
    return None


def bible_known_labels(bible: Bible) -> set[str]:
    """``bible.characters`` 的 name + 全部 alias.text 全集。

    不按 ``is_exclusive`` 过滤——这里只回答"这个字符串是否已经在人物谱里出现
    过"（供 known_names 类成员检查、或喂给模型当作已知称呼线索），不是
    ``resolve_card_owner`` 那种要 fail closed 的身份归属硬判定，两者用途不同。
    """
    labels: set[str] = set()
    for character in getattr(bible, "characters", None) or []:
        name = str(getattr(character, "name", "") or "").strip()
        if name:
            labels.add(name)
        for alias in getattr(character, "aliases", None) or []:
            text = str(getattr(alias, "text", "") or "").strip()
            if text:
                labels.add(text)
    return labels
