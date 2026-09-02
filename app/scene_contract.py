"""分镜场景契约：时间与场景图身份分离。

``scene_time`` 只表达时间；``scene_name`` 是场景图素材库的规范名。
``scene_setting`` 仅保留为旧数据/显示兼容字段，不得再作为场景图外键。
"""
from __future__ import annotations

import re
from typing import Any


_SCENE_HEADING_PREFIX_RE = re.compile(r"^【场\s*\d+】\s*")
def split_legacy_scene_setting(value: str) -> tuple[str, str]:
    """按旧标题合同的显式分隔符拆出 ``(scene_time, scene_name)``。"""
    raw = _SCENE_HEADING_PREFIX_RE.sub("", (value or "").strip())
    # The screenplay heading contract uses an explicit slash/pipe boundary.
    # Its left side is an open-ended temporal or state-relative label, so the
    # delimiter itself is authoritative and does not need a vocabulary gate.
    structured_parts = re.split(r"\s*[/|]\s*", raw, maxsplit=1)
    if len(structured_parts) == 2:
        return structured_parts[0].strip(), structured_parts[1].strip()
    # Comma is the delimiter used by the older ``时间，地点`` contract.  Its
    # meaning comes from field position, not from a closed vocabulary of time
    # words; open-ended labels such as「状态变化后即刻」remain valid.
    parts = re.split(r"\s*[，,]\s*", raw, maxsplit=1)
    if len(parts) == 2:
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


# 场景命名契约（2026-09-02《神墓》proj_facfc3964f69）：场景圣经提示词曾以「夜晚密林」
# 举例，模型据此把同一地点拆成「白日神魔陵园」「夜晚神魔陵园」两个场景；映射台的
# 事件链只给裸地点「神魔陵园」，assess_new_scene 一次拒绝猜时段（整集失败）、一次
# 「默认对应白日」（侥幸通过）。两处提示词共用下面两条正面陈述，保证口径一致。
SCENE_ONE_LOCATION_RULE = (
    "一个地点只登记一个场景：name 只写地点本身（如「神魔陵园」「宗门广场」），不带"
    "白日/夜晚/清晨/雨雪等时段与天气限定；同一地点在不同时段、不同天气下的样子属于"
    "同一个场景，典型光线时段只写进 scene_canonical 的环境描述，每一镜的具体用光由分镜决定。"
)
SCENE_SAME_LOCATION_MATCH_RULE = (
    "已有场景若与待判定地点是同一地点、只是名字多了时段/天气限定（如「白日X」「夜晚X」"
    "对「X」），按同一地点处理：important=false，existing_scene_name 返回其中在已有列表里"
    "排在最前面的那个完整名称；时段差异由分镜用光表达，不构成新场景。"
)
