"""分镜/剧本校验共享的纯谓词、常量与最小归一化工具。

本文件是 app.validators 包里入度最高的一层：包内其余模块与包外多处调用方
（app.continuity、app.storyboard_supervisor、app.domain.common 等）只需要
default_scene_transition / storyboard_shot_count_range / normalize_action_desc
这类零依赖或近零依赖的小工具，拆出来后它们不必再为了一个常量而拖入整个
校验器包（拆分历史见 docs/coupling_review_2026-08-29.md C5）。
"""
from __future__ import annotations

import difflib
import re
import sys

from app.scene_contract import (
    same_scene,
    scene_time_of,
)
from app.schemas import (
    Shot,
    Storyboard,
    TRANSITIONS,
)

SOURCE_EXCERPT_MIN_CHARS = 8


def _named_character_is_explicitly_offscreen(name: str, text: str) -> bool:
    """允许动作描述交代听者在画外，但不能把其可见动作混进单人对白镜。"""
    escaped = re.escape(name)
    return bool(
        re.search(rf"(?:画外|镜外|不入画|留在画外)[^，。；]{{0,12}}{escaped}", text)
        or re.search(rf"{escaped}[^，。；]{{0,12}}(?:在画外|于画外|不入画|留在画外)", text)
    )
# 目标时长只提供初始节拍参考；剧情未完整覆盖时可继续补
# config.VIDEO_DURATION_MIN_S~config.VIDEO_DURATION_MAX_S 秒镜头。
SCENE_CUT_TRANSITIONS = TRANSITIONS - {"硬切"}
SAME_SCENE_CONTINUITY_MODES = {
    "same_scene_cut",
    "reaction_cut",
    "reverse_angle",
    "insert_detail",
}
def default_scene_transition(prev: Shot | None, shot: Shot) -> str:
    """只按场景字段关系选择稳定默认转场。"""
    if not prev:
        return "硬切"
    return (
        "硬切"
        if same_scene(prev, shot) and scene_time_of(prev) == scene_time_of(shot)
        else "淡出淡入"
    )


def storyboard_shot_count_range(target_duration_s: int) -> tuple[int, int]:
    """镜头数由剧情交付决定；上界仅为旧调用方需要的无界整数哨兵。"""
    _ = target_duration_s
    return 1, sys.maxsize


def _normalized_spoken_text(text: str | None) -> str:
    """Normalize punctuation/spacing so adjacent repeated delivery cannot hide behind typography."""
    return re.sub(r"[\W_]+", "", text or "", flags=re.UNICODE).casefold()


def adjacent_spoken_repeat_errors(board: Storyboard) -> list[str]:
    """Reject a line that is delivered again by the same speaker in the next shot.

    A longer line may legitimately span shots, so this only rejects a current line whose
    complete normalized text already appears in the immediately previous shot. Short
    interjections are ignored to avoid false positives such as repeated names or greetings.
    """
    errors: list[str] = []
    for index in range(1, len(board.shots)):
        previous = board.shots[index - 1]
        current = board.shots[index]
        previous_by_speaker: dict[str, str] = {}
        for dialogue in previous.dialogues:
            speaker = (dialogue.speaker or "").strip().casefold()
            previous_by_speaker[speaker] = (
                previous_by_speaker.get(speaker, "") + _normalized_spoken_text(dialogue.line)
            )
        for dialogue in current.dialogues:
            speaker = (dialogue.speaker or "").strip().casefold()
            normalized = _normalized_spoken_text(dialogue.line)
            if len(normalized) < 8 or not speaker:
                continue
            if normalized in previous_by_speaker.get(speaker, ""):
                errors.append(
                    f"shots[{index}](shot_no={current.shot_no}) 与上一镜相邻重复台词："
                    f"{dialogue.speaker} 的「{dialogue.line}」已在镜{previous.shot_no:02d}完整说过；"
                    "请删除重复台词并改为无台词反应镜，或改写为新的有效信息"
                )
    return errors


def _too_similar(a: str, b: str) -> bool:
    """首尾帧描述是否过于相似（几乎是同一句、看不出动作推进）。

    旧实现用【字符集合】Jaccard≥0.8：但首尾帧本就要求"同机位同构图、只让动作推进"，
    天然高词汇重叠，集合 Jaccard 会把"描写到位但动作确有变化"的合规首尾帧误判为雷同，
    反逼模型把首尾写成两个不同镜头/景别——正好制造它想避免的跳变。
    改用序列相似度（difflib，计入顺序与长度），只拦近乎逐字重复的真雷同。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _scene_time_key(scene_time: str) -> str:
    """Normalize only formatting; time semantics remain an open contract."""
    return _normalize_scene_label(scene_time or "").casefold()


def _scene_time_changed(prev_time: str, scene_time: str) -> bool:
    previous = _scene_time_key(prev_time)
    current = _scene_time_key(scene_time)
    return bool(previous and current and previous != current)


def normalize_transition_visuals(board: Storyboard) -> None:
    """保留旧入口；转场由最终编辑统一执行，不再污染原始镜头描述。"""
    _ = board


_LEADING_ACTION_SEQUENCE_RE = re.compile(r"^\s*(?:先|首先)\s*(?:[，,、。；;：:]|…+|\.{2,})\s*")


def normalize_action_desc(text: str | None) -> str:
    """去掉模型把顺序提示词误写进 action_desc 句首的孤立标记。"""
    normalized = (text or "").strip()
    while True:
        cleaned = _LEADING_ACTION_SEQUENCE_RE.sub("", normalized, count=1).lstrip()
        if cleaned == normalized:
            return normalized
        normalized = cleaned


def _shot_capacity_budget_total(shot: Shot) -> float:
    """Return the open-dimensional viewing work assigned to one ShotTask."""
    budget = getattr(shot, "capacity_budget", None)
    if budget is None:
        return 0.0
    return sum(
        float(value or 0.0)
        for field, value in budget.model_dump().items()
        if field != "other_reason" and isinstance(value, (int, float))
    )


# 从 scene_match 段落移到这里，打破本包内部的 import 环（_normalize_scene_label 原本紧邻的段落会反过来依赖它所在的段落）。
def _normalize_scene_label(s: str) -> str:
    """去掉标点/空白，得到稳定 token，用于场景标签的容错匹配。"""
    return re.sub(r"[\s，,。.：:；;/、|]+", "", (s or "").strip())
