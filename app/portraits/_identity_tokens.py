"""身份标签的纯文本/token 级工具：分隔符判定、token 切分、消歧后缀、
visual_entity_id 的安全解析。零内部依赖，被证据投影与文本替换两侧模块共用。
"""

from __future__ import annotations

import re

from typing import Any


_IDENTITY_LIST_SEPARATOR_PATTERN = re.compile(
    r"([、，,／/；;｜|＆&＋+\s]+)"
)


def _strip_identity_list_separators(value: str) -> str:
    """去掉身份列表分隔符与空白（「周、尹二人」→「周尹二人」），供 functional source_label
    的确定性归一化用；与 ``_identity_source_label_has_list_separator`` 用同一张字符表。"""
    return _IDENTITY_LIST_SEPARATOR_PATTERN.sub("", str(value or ""))


def _identity_source_label_has_list_separator(value: str) -> bool:
    """True if ``value`` contains a char the identity-list grammar splits on.

    这是 source_label / 未来揭示真名的真正业务约束（见
    ``IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH`` 旁的说明）：长度只是不精确
    的代理，混入 ``_IDENTITY_LIST_SEPARATOR_PATTERN`` 命中的分隔符或空白才会
    让下游身份列表被错误切分。
    """
    return _IDENTITY_LIST_SEPARATOR_PATTERN.search(str(value or "")) is not None


def _project_identity_token(
    token: str,
    source_label: str,
    canonical_name: str,
) -> str:
    """Project one complete identity token through durable authority.

    ``plot_spine.who`` is a structured identity carrier, not prose.  Alias
    decisions therefore apply only to a complete token.  The expansion branch
    is a compatibility migration for artifacts produced by the former
    substring replacement; its shape is derived from this exact authority
    mapping rather than from any vocabulary list.
    """
    value = str(token or "").strip()
    if not value or source_label == canonical_name:
        return value
    if value == source_label or value == canonical_name:
        return canonical_name

    prefix, separator, suffix = canonical_name.partition(source_label)
    if not separator:
        return value
    if prefix and suffix:
        repeated = re.fullmatch(
            rf"(?:{re.escape(prefix)}){{2,}}"
            rf"{re.escape(source_label)}"
            rf"(?:{re.escape(suffix)}){{2,}}",
            value,
        )
    elif prefix:
        repeated = re.fullmatch(
            rf"(?:{re.escape(prefix)}){{2,}}{re.escape(source_label)}",
            value,
        )
    elif suffix:
        repeated = re.fullmatch(
            rf"{re.escape(source_label)}(?:{re.escape(suffix)}){{2,}}",
            value,
        )
    else:
        repeated = None
    return canonical_name if repeated is not None else value


def _identity_list_tokens(value: str) -> list[str]:
    """Return complete identities from the structured ``who`` grammar."""
    return [
        part.strip()
        for part in _IDENTITY_LIST_SEPARATOR_PATTERN.split(str(value or ""))
        if part.strip()
        and _IDENTITY_LIST_SEPARATOR_PATTERN.fullmatch(part) is None
    ]



_IDENTITY_DISAMBIGUATING_ORDINALS = "甲乙丙丁戊己庚辛壬癸"


def _identity_disambiguating_suffix(collision_index: int) -> str:
    """真实第20轮 EP4 回归 ERR-20260824-407c9b：两个不同的 identity_group
    退回同一个裸功能性标签（"外宗弟子"）当 route_name 时的确定性区分后缀。
    1-based collision_index -> 甲/乙/丙...；超出十天干（第11次及以后撞车，
    极端情况）退化为阿拉伯数字，保证任意数量的碰撞都有确定性且互不相同的
    后缀，不会自己再撞车。"""
    if 1 <= collision_index <= len(_IDENTITY_DISAMBIGUATING_ORDINALS):
        return _IDENTITY_DISAMBIGUATING_ORDINALS[collision_index - 1]
    return str(collision_index)



def _visual_entity_id_for_resolution_safe(value: dict[str, Any]) -> str | None:
    """延迟导入 ``app.identity_authority.visual_entity_id_for_resolution``。

    该函数按 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.2 冻结的签名由另一
    条并行改动落地在 app.identity_authority——本文件不实现、只消费，调用点
    延迟到函数体内部（而非模块顶层 import），避免在依赖尚未落地的窗口期让
    整个 app.portraits 模块导入失败。依赖缺失或调用异常时返回 None：命名侧
    折叠（K 决议本身）不依赖这个返回值，只是暂时跳过 visual 侧记账，等依赖
    落地后自动补齐，不需要再改这里。"""
    try:
        from app.identity_authority import visual_entity_id_for_resolution
    except ImportError:
        return None
    try:
        result = visual_entity_id_for_resolution(value)
    except Exception:  # noqa: BLE001 - 防御性：绝不让记账失败拖垮身份预检
        return None
    return str(result).strip() or None

