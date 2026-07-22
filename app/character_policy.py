"""Storyboard character classes that do not belong in the persistent character bible."""
from __future__ import annotations

import re


# Only deterministic, non-proper role labels are allowed through this path. A named or
# recurring character must still enter the character bible and receive normal assets.
FUNCTIONAL_EXTRA_ROLES = frozenset({
    "测验员", "裁判", "主持人", "司仪", "店员", "服务员", "侍者", "守卫", "门卫",
    "护卫", "保安", "司机", "医生", "护士", "记者", "警员", "官差", "伙计", "小二",
    "传令兵", "随从", "仆人", "侍女", "宫女", "太监",
})
FUNCTIONAL_EXTRA_BASES = frozenset({
    "路人", "围观者", "群众", "族人", "弟子", "学生", "顾客", "客人", "村民", "士兵",
    "护卫", "守卫", "侍女", "仆人", "记者", "警员",
})
FUNCTIONAL_EXTRA_BARE_LABELS = frozenset({"路人", "围观者"})
FUNCTIONAL_EXTRA_MODIFIERS = frozenset({
    "年轻", "中年", "年长", "老年", "男", "女", "男性", "女性",
})
_EXTRA_INDEX_RE = re.compile(r"(?:甲|乙|丙|丁|A|B|C|D|[1-9]\d*)$", re.I)


def is_functional_extra(name: str) -> bool:
    """Return whether ``name`` is an unnamed, non-persistent background role."""
    value = (name or "").strip()
    if not value or len(value) > 8:
        return False
    if value in FUNCTIONAL_EXTRA_ROLES or value in FUNCTIONAL_EXTRA_BARE_LABELS:
        return True
    if any(
        value == modifier + role
        for modifier in FUNCTIONAL_EXTRA_MODIFIERS
        for role in FUNCTIONAL_EXTRA_ROLES
    ):
        return True
    return any(
        value.startswith(base) and _EXTRA_INDEX_RE.fullmatch(value[len(base):]) is not None
        for base in FUNCTIONAL_EXTRA_BASES | FUNCTIONAL_EXTRA_ROLES
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
        "店员、守卫、路人甲/乙/丙、族人甲、弟子乙；这类角色必须使用通用身份标签，"
        "并在 action_desc 或首尾帧中明确可见。任何具体姓名、重要配角或跨镜持续角色仍必须来自角色圣经"
    )
