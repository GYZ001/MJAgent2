"""跨领域共享的常量、正则与 ID 归一化辅助（原 app/schemas.py 顶部共享区）。

分镜/剧本/叙事领域模块共同依赖这里的镜头词表、连续性模式常量、四类 ID 正则
（S*/E*/KL*）与 information_ids 双向同步辅助，避免各领域模块互相依赖。
"""
from __future__ import annotations

import re

from typing import TypeVar

from pydantic import BaseModel

SHOT_SIZES = {"远景", "全景", "中景", "近景", "特写"}
CAMERA_MOVES = {"固定", "推近", "拉远", "横摇", "跟随"}
TRANSITIONS = {
    "硬切",
    "叠化",
    "淡出淡入",
    "黑场",
    "闪黑",
    "闪白",
    "甩镜",
    "遮挡转场",
    "匹配剪辑",
    "声音延续+叠化",
    "声音先行+淡入",
}
EMOTIONS = {"平静", "愤怒", "悲伤", "惊恐", "喜悦", "讥讽", "坚定"}

# 镜头连续性模式；除 scene_change 外均使用上一条采用视频的真实尾帧作首帧。
CONTINUITY_MODES = {
    "action_continuation",  # 同一人物同一动作跨镜延续
    "same_scene_cut",       # 同场景换景别/构图
    "reaction_cut",         # 切到另一人物或人群反应
    "reverse_angle",        # 正反打
    "insert_detail",        # 道具/手部/文字特写
    "scene_change",         # 时间或地点改变
}

DELIVERY_OWNERS = {
    "visual_action",
    "spoken_dialogue",
    "offscreen_voice",
    "narration",
    "on_screen_text",
    "ambient_sound",
}

AUDIO_TIMELINE_TYPES = {
    "spoken_dialogue",
    "offscreen_voice",
    "narration",
    "ambient_sound",
}

PROMPT_CONTRACT_VERSION = "video_cinematic_continuity_v6"
NARRATIVE_CONTRACT_VERSION = "narrative-continuity.v2"
SYSTEM_ENVIRONMENT_ENTITY_PREFIX = "environment:"

# 镜头形态（WS7）：见 app.schemas.shot_montage 模块 docstring。"scene" 是绝大多数
# 镜头的既有形态；"montage" 是叙述者总结/回忆列举/跨年排比段落的新形态。
SHOT_FORMS = {"scene", "montage"}

# 旁白是画外叙述声音，从来不是画面里的人——它不应该出现在 Shot.characters /
# characters_visible 里（those two fields answer "谁在画面里/谁跟这一镜有关"）。
# 与 SYSTEM_ENVIRONMENT_ENTITY_PREFIX 同一类保留标识手法：不是靠猜测穷举各种
# 旁白写法（黑名单），而是把它定成一个显式保留字面量，产出方与校验方共用同一
# 个常量，谁写错都能立刻定位。
NARRATOR_LABEL = "旁白"


def system_environment_entity_id(scope_id: object) -> str:
    """Return the reserved, non-character narrative subject for one scope."""
    return SYSTEM_ENVIRONMENT_ENTITY_PREFIX + str(scope_id or "").strip()


def is_system_environment_entity_id(
    value: object,
    *,
    scope_id: object | None = None,
) -> bool:
    """Recognize only compiler-owned environment subjects, never characters."""
    normalized = str(value or "").strip()
    if scope_id is None:
        return normalized.startswith(SYSTEM_ENVIRONMENT_ENTITY_PREFIX)
    return normalized == system_environment_entity_id(scope_id)


def is_narrator_label(value: object) -> bool:
    """Recognize the reserved narrator sentinel, never a real character name."""
    return str(value or "").strip() == NARRATOR_LABEL

# 主线节拍 ID（S*）与剧本事件 ID（E*）长得像但语义不同，历史数据把 S07 写进了 story_event_id。
# 这两个正则是四类 ID 分离（PRD VAL-422 §4.4.1）的判定底座。
SPINE_BEAT_ID_RE = re.compile(r"^S\d{1,3}$", re.I)
STORY_EVENT_ID_RE = re.compile(r"^E\d{1,3}(?:\.\d{1,3})?$", re.I)
KEY_LINE_ID_RE = re.compile(r"^KL\d{1,3}$", re.I)

_IdCarrier = TypeVar("_IdCarrier", bound=BaseModel)


def _normalize_information_ids(model: _IdCarrier) -> _IdCarrier:
    """把 `new_information_ids` 归一到 `information_ids`，两侧保持同一份内容。

    PRD §7.1 要求兼容旧字段一个版本周期，但内部立即以 `information_ids` 为准。
    双向同步而不是单向覆盖，才能让仍在读旧字段的调用方继续正确工作。
    """
    new_ids = [str(x).strip() for x in (model.new_information_ids or []) if str(x).strip()]
    info_ids = [str(x).strip() for x in (model.information_ids or []) if str(x).strip()]
    merged = list(dict.fromkeys([*info_ids, *new_ids]))
    if merged != info_ids:
        model.information_ids = merged
    if merged != new_ids:
        model.new_information_ids = merged
    return model
