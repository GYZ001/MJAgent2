"""整集分镜契约：Storyboard 逐镜列表、大纲阶段的 StoryboardOutline 与场景上下文。

StoryboardOutlineShot.narrative_boundary_from_previous 与
StoryboardOutline.scene_contexts/cognitive_bridge_plans 三处沿用原文件的
前向引用字符串（分别指向 .narrative_boundary 的 NarrativeBoundaryContract/
CognitiveBridgePlan，以及本文件后定义的 StoryboardSceneContext），由包
``__init__.py`` 统一 ``model_rebuild()``，理由同 .shot 模块docstring。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from .common import _normalize_information_ids
from .narrative_action import ActionParticipantDelivery
from .narrative_capacity import (
    AudienceStatePathRef,
    ReadabilityWindow,
    ShotCapacityBudget,
    ShotContribution,
)
from .shot import Shot

if TYPE_CHECKING:
    from .narrative_boundary import CognitiveBridgePlan, NarrativeBoundaryContract

class Storyboard(BaseModel):
    episode_no: int
    shots: list[Shot]


class StoryboardOutlineShot(BaseModel):
    """分镜大纲里的一条镜头节拍：按状态变化一次规划原子镜头。
    逐镜填充阶段据此把整集剧情均匀铺满，避免多镜停留在同一情绪/同一句原文。"""

    shot_no: int
    scene_time: str = ""      # 独立时间标签，可为具体时刻
    scene_name: str = ""      # 场景图素材库的规范名
    scene_setting: str = ""   # 旧数据/显示兼容字段
    beat: str = ""            # 本镜推进的剧情（一句话：谁做了什么 / 局势如何变化 / 与上一镜的区别）
    covers: str = ""          # 本镜落实的必保留关键台词/剧情点（可空）
    # 原子分镜规划字段（PRD §7）：大纲阶段即锁定状态链与连续性模式
    story_event_id: str = ""
    # PRD VAL-422 §4.2：大纲阶段就把关键台词/主线节拍按稳定 ID 分配到镜头，
    # 容量预检据此判断「这一镜的必保留口播是否念得完」。
    spine_beat_ids: list[str] = Field(default_factory=list)
    key_line_ids: list[str] = Field(default_factory=list)
    information_ids: list[str] = Field(default_factory=list)
    new_information_ids: list[str] = Field(default_factory=list)
    state_in: str = ""
    primary_action: str = ""
    emotion_beat: str = ""
    state_out: str = ""
    continuity_mode: str = ""
    duration_s: int | None = None
    characters_visible: list[str] = Field(default_factory=list)
    audio_cast: list[str] = Field(default_factory=list)
    purpose: str = ""
    context_requirement_ids: list[str] = Field(default_factory=list)
    resulting_change: str = ""
    readability_focus: str = ""
    camera_size: str = ""
    camera_angle: str = ""
    camera_movement: str = ""
    camera_motivation: str = ""
    repeat_of_shot_id: str | None = None
    repeat_gain: str = ""
    shot_id: str = ""
    scene_id: str = ""
    event_ids: list[str] = Field(default_factory=list)
    primary_action_id: str | None = None
    supporting_action_ids: list[str] = Field(default_factory=list)
    action_phase_ids: list[str] = Field(default_factory=list)
    visible_entity_ids: list[str] = Field(default_factory=list)
    offscreen_action_actor_ids: list[str] = Field(default_factory=list)
    offscreen_action_target_ids: list[str] = Field(default_factory=list)
    action_participant_deliveries: list[ActionParticipantDelivery] = Field(
        default_factory=list
    )
    capacity_budget: ShotCapacityBudget | None = None
    shot_contribution: ShotContribution | None = None
    audience_state_paths: list[AudienceStatePathRef] = Field(default_factory=list)
    planned_state_in_fact_ids: list[str] = Field(default_factory=list)
    planned_delta_add_fact_ids: list[str] = Field(default_factory=list)
    planned_delta_remove_fact_ids: list[str] = Field(default_factory=list)
    planned_state_out_fact_ids: list[str] = Field(default_factory=list)
    completed_before_action_ids: list[str] = Field(default_factory=list)
    completed_before_action_phase_ids: list[str] = Field(default_factory=list)
    reserved_future_event_ids: list[str] = Field(default_factory=list)
    readability_window_ids: list[str] = Field(default_factory=list)
    narrative_boundary_from_previous: "NarrativeBoundaryContract | None" = None

    @model_validator(mode="after")
    def _sync_information_ids(self) -> "StoryboardOutlineShot":
        return _normalize_information_ids(self)


class StoryboardOutline(BaseModel):
    """整集分镜大纲：一次性把剧本铺成有序的 N 条镜头节拍，先定全局节奏再逐镜填充。"""

    episode_no: int
    shots: list[StoryboardOutlineShot] = Field(default_factory=list)
    scene_contexts: list["StoryboardSceneContext"] = Field(default_factory=list)
    readability_windows: list[ReadabilityWindow] = Field(default_factory=list)
    cognitive_bridge_plans: list["CognitiveBridgePlan"] = Field(default_factory=list)


class StoryboardContextRequirement(BaseModel):
    requirement_id: str
    description: str
    required_before_shot_no: int | None = None


class StoryboardSceneContext(BaseModel):
    scene_id: str
    scene_no: int
    scene_name: str = ""
    scene_time: str = ""
    entry_state: str
    exit_state: str
    transition_from_previous: str = ""
    spatial_axis: str = ""
    context_requirements: list[StoryboardContextRequirement] = Field(default_factory=list)
