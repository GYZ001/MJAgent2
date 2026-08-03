"""Storyboard character classes that do not belong in the persistent character bible."""
from __future__ import annotations

import re


# Only deterministic, non-proper role labels are allowed through this path. A named or
# recurring character must still enter the character bible and receive normal assets.
FUNCTIONAL_EXTRA_ROLES = frozenset({
    "测验员", "裁判", "主持人", "司仪", "店员", "服务员", "侍者", "守卫", "门卫",
    "护卫", "保安", "司机", "医生", "护士", "记者", "警员", "官差", "伙计", "小二",
    "传令兵", "随从", "仆人", "侍女", "宫女", "太监", "管家", "管事", "长老",
})
FUNCTIONAL_EXTRA_BASES = frozenset({
    "路人", "围观者", "群众", "族人", "弟子", "学生", "顾客", "客人", "村民", "士兵",
    "护卫", "守卫", "侍女", "仆人", "记者", "警员",
})
FUNCTIONAL_EXTRA_BARE_LABELS = frozenset({"路人", "围观者"})
FUNCTIONAL_EXTRA_MODIFIERS = frozenset({
    "年轻", "中年", "年长", "老年", "老", "男", "女", "男性", "女性",
})
_EXTRA_INDEX_RE = re.compile(r"(?:甲|乙|丙|丁|A|B|C|D|[1-9]\d*)$", re.I)
_ROLE_ORDINAL_RE = re.compile(r"(?:[一二三四五六七八九十]|[1-9]\d*)$")
_COLLECTIVE_ROLE_EXACT = frozenset({
    "人群", "众人", "群众", "大家", "人们", "百姓", "观众", "家族子弟", "族人", "子弟",
})
_COLLECTIVE_ROLE_MARKERS = (
    "人群", "众人", "群众", "一群", "一众", "众弟子", "众族人", "围观人群", "围观众人",
)
_COLLECTIVE_ROLE_SUFFIXES = (
    "们", "子弟", "人群", "群众", "百姓", "观众", "护卫队", "守卫队", "士兵队伍",
)
_COLLECTIVE_QUANTIFIER_RE = re.compile(
    r"^(?:几|数|多|两|[2-9]|二|三|四|五|六|七|八|九|十)(?:名|位|个)?"
    r"(?:族人|弟子|子弟|学生|顾客|村民|士兵|护卫|守卫|围观者|观众)$"
)
_SINGULAR_QUANTIFIER_RE = re.compile(r"^(?:一|1)(?:名|位|个)")


def is_functional_extra(name: str) -> bool:
    """Return whether ``name`` is an unnamed, non-persistent background role."""
    value = (name or "").strip()
    if not value or len(value) > 8:
        return False
    if (
        value in FUNCTIONAL_EXTRA_ROLES
        or value in FUNCTIONAL_EXTRA_BARE_LABELS
    ):
        return True
    if any(
        value == modifier + role
        for modifier in FUNCTIONAL_EXTRA_MODIFIERS
        for role in FUNCTIONAL_EXTRA_ROLES
    ):
        return True
    if any(
        value.endswith(role)
        and _ROLE_ORDINAL_RE.fullmatch(value[:-len(role)]) is not None
        for role in FUNCTIONAL_EXTRA_ROLES
    ):
        return True
    return any(
        value.startswith(base) and _EXTRA_INDEX_RE.fullmatch(value[len(base):]) is not None
        for base in FUNCTIONAL_EXTRA_BASES | FUNCTIONAL_EXTRA_ROLES
    )


def is_collective_role(name: str) -> bool:
    """返回这个可见名单项是否表示群体，而不是一个需锁定身份的人。

    「萧家子弟」这类 legacy 标签会出现在 characters_visible 中。如果按一个角色
    处理，图像模型会把「众人议论」压成单人，还会错误寻找它的定妆照。
    """
    value = (name or "").strip()
    if not value:
        return False
    if _SINGULAR_QUANTIFIER_RE.match(value):
        return False
    if value in _COLLECTIVE_ROLE_EXACT or _COLLECTIVE_QUANTIFIER_RE.fullmatch(value):
        return True
    if any(marker in value for marker in _COLLECTIVE_ROLE_MARKERS):
        return True
    return any(value.endswith(suffix) for suffix in _COLLECTIVE_ROLE_SUFFIXES)


def is_allowed_storyboard_character(
    name: str,
    bible_names: set[str] | frozenset[str],
    *,
    allow_without_bible: bool = True,
) -> bool:
    """判定一个镜头角色标签是否能进入渲染合同。

    可见名单、声轨和 Prompt 编译必须共用这一口径，否则就会出现
    ``characters`` 已修复、``characters_visible`` 却仍携带旧名的“幽灵角色”。
    没有真实角色圣经时不做强制删除，保持占位圣经的历史兼容行为。
    """
    value = (name or "").strip()
    if not value:
        return False
    return (
        (not bible_names and allow_without_bible)
        or value in bible_names
        or is_functional_extra(value)
        or is_collective_role(value)
    )


def collective_role_anchor(name: str) -> str:
    """群体名单项的画面锚点：锁阵营/服饰语义，不伪造单人身份。"""
    if not is_collective_role(name):
        raise ValueError(f"not a collective role: {name}")
    return (
        f"叙事群体「{name}」，人数、前后景和动作严格按本镜目标描述；"
        "这是由多个不同个体组成的群体，不得缩成一个固定身份，不复制主角长相"
    )


def functional_extra_anchor(name: str) -> str:
    """Build a stable visual instruction without minting a character-bible identity."""
    if not is_functional_extra(name):
        raise ValueError(f"not a functional extra: {name}")
    return (
        f"功能性路人「{name}」，穿着符合当前时代与职业身份的普通服饰，外貌自然克制、"
        "不使用任何主角标志性特征；同一镜头内脸型、发型和服装保持一致，不抢主角视觉中心"
    )


def functional_extra_policy_text() -> str:
    """Shared prompt wording for the deterministic storyboard contract."""
    return (
        "允许无姓名、无需跨集定妆的功能性路人进入 characters 并开口，例如测验员、裁判、"
        "店员、守卫、路人甲/乙/丙、族人甲、弟子乙；这类角色必须使用通用身份标签。"
        "原文只写绿袍男子/青衣女子/大汉/陌生人等过渡称谓时，不得直接放行："
        "必须先向后解析真名，"
        "无真名则改用不冲突的路人甲/乙/丙/丁；"
        "并在 action_desc 或首尾帧中明确可见。任何具体姓名、重要配角或跨镜持续角色仍必须来自角色圣经"
    )
