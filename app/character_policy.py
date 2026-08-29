"""Storyboard character classes that do not belong in the persistent character bible."""
from __future__ import annotations

import re


_GENERATED_FUNCTIONAL_ID_RE = re.compile(r"^functional:[^\s:][^\s]*$")
_GENERATED_COLLECTIVE_ID_RE = re.compile(r"^collective:[^\s:][^\s]*$")


def is_functional_extra(name: str) -> bool:
    """Recognize only an already-minted synthetic functional identity ID.

    Whether a source character is functional is decided upstream by the typed
    identity contract. Names, occupations, ages and honorifics are never used
    here as a business classifier.
    """
    value = (name or "").strip()
    return bool(value and _GENERATED_FUNCTIONAL_ID_RE.fullmatch(value))


def resolution_declares_functional_identity(value: object) -> bool:
    """Accept current and historical typed resolution records.

    These are persisted enum values, not role/name classifiers. The source
    label itself never decides whether an identity is functional.
    """
    resolution = (
        str(value.get("resolution") or "").strip()
        if isinstance(value, dict)
        else str(value or "").strip()
    )
    return (
        resolution == "functional_identity"
        or resolution == "functional_extra"
    )


def typed_functional_identity_names(screenplay: object | None) -> set[str]:
    """Return identities explicitly typed as functional by published data."""
    if screenplay is None:
        return set()
    return {
        str(getattr(voice, "speaker_id", "") or "").strip()
        for voice in (getattr(screenplay, "voice_bible", None) or [])
        if (
            str(getattr(voice, "role_type", "") or "").strip()
            == "functional_character"
            and str(getattr(voice, "speaker_id", "") or "").strip()
        )
    }


def is_collective_role(name: str) -> bool:
    """只识别上游已签发的 ``collective:`` 稳定身份 ID。"""
    value = (name or "").strip()
    return bool(value and _GENERATED_COLLECTIVE_ID_RE.fullmatch(value))


def is_allowed_storyboard_character(
    name: str,
    bible_names: set[str] | frozenset[str],
    *,
    allow_without_bible: bool = True,
    declared_functional_names: set[str] | frozenset[str] = frozenset(),
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
        or value in declared_functional_names
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


def functional_extra_anchor(
    name: str,
    *,
    declared_functional_names: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Build a stable visual instruction without minting a character-bible identity."""
    if (
        not is_functional_extra(name)
        and name not in declared_functional_names
    ):
        raise ValueError(f"not a functional extra: {name}")
    return (
        f"功能性路人「{name}」，穿着符合当前时代与职业身份的普通服饰，外貌自然克制、"
        "不使用任何主角标志性特征；同一镜头内脸型、发型和服装保持一致，不抢主角视觉中心"
    )
