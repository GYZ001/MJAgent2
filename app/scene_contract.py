"""分镜场景契约：时间与场景图身份分离。

``scene_time`` 只表达时间；``scene_name`` 是场景图素材库的规范名。
``scene_setting`` 仅保留为旧数据/显示兼容字段，不得再作为场景图外键。
"""
from __future__ import annotations

import re
from typing import Any


_SCENE_HEADING_PREFIX_RE = re.compile(r"^【场\s*\d+】\s*")
_SCENE_TIME_LABELS = frozenset({
    "日", "白日", "白天", "日间", "清晨", "早晨", "上午", "中午", "晌午",
    "午后", "下午", "傍晚", "黄昏", "夜", "夜晚", "夜里", "深夜", "午夜",
    "凌晨", "黎明", "次日", "翌日", "当日", "当天", "早", "中", "晚",
})


def looks_like_scene_time(value: str) -> bool:
    """识别时间段或具体时刻，同时避免把普通地名当成时间。"""
    compact = re.sub(r"\s+", "", value or "")
    if compact in _SCENE_TIME_LABELS:
        return True
    if not compact or len(compact) > 16:
        return False
    if re.search(r"(?:\d{1,2}[:：]\d{2}|\d{1,2}(?:点|时)(?:\d{1,2}分?)?)", compact):
        return True
    if re.search(r"(?:凌晨|早上|上午|中午|下午|晚上|深夜)[一二两三四五六七八九十百零\d]+(?:点|时)", compact):
        return True
    return bool(
        compact.endswith((
            "清晨", "早晨", "上午", "中午", "晌午", "午后", "下午", "傍晚",
            "黄昏", "夜晚", "夜里", "深夜", "午夜", "凌晨", "黎明",
        ))
        or (
            "转" in compact
            and all(part in _SCENE_TIME_LABELS for part in compact.split("转") if part)
        )
    )


def split_legacy_scene_setting(value: str) -> tuple[str, str]:
    """把旧的「时间，地点」拆开；无明确时间前缀时整体视为场景候选名。"""
    raw = _SCENE_HEADING_PREFIX_RE.sub("", (value or "").strip())
    parts = re.split(r"\s*[/，,|]\s*", raw, maxsplit=1)
    if len(parts) == 2 and looks_like_scene_time(parts[0]):
        return parts[0].strip(), parts[1].strip()
    return "", raw


def compose_scene_setting(scene_time: str, scene_name: str, *, fallback: str = "") -> str:
    """生成仅供旧读取方/绘画 prompt 使用的兼容文案。"""
    time = (scene_time or "").strip()
    name = (scene_name or "").strip()
    if time and name:
        return f"{time}，{name}"
    return name or time or (fallback or "").strip()


def scene_name_of(value: Any) -> str:
    """取物理场景身份；仅在旧数据没有 scene_name 时解析兼容字段。"""
    name = str(getattr(value, "scene_name", "") or "").strip()
    if name:
        return name
    _, legacy_name = split_legacy_scene_setting(
        str(getattr(value, "scene_setting", "") or "")
    )
    return legacy_name


def scene_time_of(value: Any) -> str:
    time = str(getattr(value, "scene_time", "") or "").strip()
    if time:
        return time
    legacy_time, _ = split_legacy_scene_setting(
        str(getattr(value, "scene_setting", "") or "")
    )
    return legacy_time


def same_scene(left: Any, right: Any) -> bool:
    """只比较场景图身份，时间变化不会悄悄改成另一张场景图。"""
    left_name = scene_name_of(left)
    right_name = scene_name_of(right)
    return bool(left_name and right_name and left_name == right_name)
