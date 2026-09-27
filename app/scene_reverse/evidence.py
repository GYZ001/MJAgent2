"""反打视角的判定证据与提示词点名约定（纯函数，无 IO，除开关读取外）。

点名约定：分镜模型在某个「镜头N」里写 ``@场景名·反打``，表示这一镜从与该场景
主视角相对的方向拍摄、使用该场景的反打视角图。场景名必须逐字取自本段资源；
合法名字集合永远来自调用方传入的本段数据，这里不维护任何名单。
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from app.db import get_setting

REVERSE_ANGLE_VIEW_ROLE = "reverse_angle"
REVERSE_MENTION_SUFFIX = "·反打"
ENABLED_SETTING_KEY = "scene_reverse_angle_reference_enabled"

# 点名文本的形状：@ + 不含空白与常见标点的名字 + ·反打。只用来找出「不对应任何
# 本段场景名」的残留点名；合法点名一律按传入的场景名逐字匹配，不靠这个正则。
_MENTION_RE = re.compile(r"@([^@\s，。；：、,.;:！？!?（）()「」“”\"']{1,40}?)·反打")


def reverse_angle_reference_enabled() -> bool:
    """总开关（默认开）。关闭时分镜模型看不到反打选项、装配时也不装反打图。"""
    value = (get_setting(ENABLED_SETTING_KEY) or "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _qa_dict(view: Mapping[str, Any]) -> dict[str, Any]:
    qa = view.get("qa_json", view.get("qa"))
    if isinstance(qa, str):
        try:
            qa = json.loads(qa)
        except (TypeError, ValueError):
            return {}
    return qa if isinstance(qa, dict) else {}


def reverse_check_passed(view: Mapping[str, Any]) -> bool:
    """这张反打图是否带「确实朝向相反方向」的判定通过证据。

    证据形状：``qa_json = {"reverse_check": {"checked": bool, "passed": bool | None,
    "reason": str, "error": str | None, ...}}``。没有证据（2026-09-27 之前生成的
    存量图）、判定未完成或判定不通过，一律 False——空证据不等于通过。
    """
    if str(view.get("view_role") or "") != REVERSE_ANGLE_VIEW_ROLE:
        return False
    check = _qa_dict(view).get("reverse_check")
    return isinstance(check, dict) and check.get("checked") is True and check.get("passed") is True


def reverse_mention(scene_name: str) -> str:
    return f"@{scene_name}{REVERSE_MENTION_SUFFIX}"


def mentioned_reverse_scenes(prompt_text: str, scene_names: Iterable[str]) -> set[str]:
    """正文里被 ``@场景名·反打`` 点到的场景名（只认传入的名字，逐字匹配）。"""
    text = prompt_text or ""
    return {name for name in scene_names if name and reverse_mention(name) in text}


def unmatched_reverse_mentions(prompt_text: str, scene_names: Iterable[str]) -> list[str]:
    """正文里形如 ``@X·反打``、但 X 不是任何传入场景名的点名（原样返回）。"""
    text = prompt_text or ""
    for name in sorted({n for n in scene_names if n}, key=len, reverse=True):
        text = text.replace(reverse_mention(name), "")
    return [m.group(0) for m in _MENTION_RE.finditer(text)]


def demote_reverse_mentions(prompt_text: str, scene_names: Iterable[str]) -> str:
    """把指定场景的 ``@场景名·反打`` 降级成不带 @ 的普通文字「场景名反打方向」。

    装配时反打图没有装进请求（开关关闭、证据缺失、文件丢失）用这个处理正文，
    避免一个没有对应参考图的 @ 点名原样发给视频供应商。
    """
    text = prompt_text or ""
    for name in sorted({n for n in scene_names if n}, key=len, reverse=True):
        text = text.replace(reverse_mention(name), f"{name}反打方向")
    return text
