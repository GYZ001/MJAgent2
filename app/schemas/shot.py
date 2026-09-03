"""单镜头契约（Shot）：分镜台的最小生成单元，装配上述各领域模块的产出。

``narrative_boundary_from_previous`` 字段引用的 ``NarrativeBoundaryContract``
定义在 .narrative_boundary。这里沿用原 app/schemas.py 的前向引用字符串写法
（不在运行时导入该类型），由包 ``__init__.py`` 在全部子模块导入完成后统一
调用 ``model_rebuild()`` 解析——与拆包前该文件末尾的解析时机完全一致（原文件
里 Shot 定义在 NarrativeBoundaryContract 之前，字段同样要等到文件末尾显式
``model_rebuild()`` 才完成解析）。``TYPE_CHECKING`` 导入只供静态检查使用，
不改变这个运行时解析时机。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

from .common import _normalize_information_ids
from .narrative_action import ActionParticipantDelivery
from .narrative_capacity import AudienceStatePathRef, ShotCapacityBudget, ShotContribution
from .shot_montage import MontageBeat
from .shot_state import AudioTimelineItem, ContinuityState, Dialogue, RequiredOnScreenText

if TYPE_CHECKING:
    from .narrative_boundary import NarrativeBoundaryContract

class Shot(BaseModel):
    shot_no: int
    shot_uid: str = ""
    duration_s: int
    shot_size: str
    camera_move: str
    # 时间与场景图身份是两个独立维度。scene_time 允许早/中/晚/黄昏/具体时刻。
    scene_time: str = ""
    # 旧的「时间，地点」混合字段，只作兼容显示；新流程不再用它选场景图。
    scene_setting: str = ""
    # 库内规范场景名，与场景图一一对应。模糊输入命中后必须回填成该规范名。
    scene_name: str = ""
    characters: list[str] = Field(default_factory=list)
    action_desc: str
    # 生成起点与结束目标：同场景起点继承上一条采用视频真实尾帧；换场起点只依赖人物谱/场景库。
    # last_frame_desc 是视频结束状态目标，不代表另行生成一张静态尾帧参考图。
    first_frame_desc: str = ""
    last_frame_desc: str = ""
    source_excerpt: str = ""
    narration: str | None = None
    dialogues: list[Dialogue] = Field(default_factory=list)
    transition: str = "硬切"
    continuity_from_prev: bool = False
    # ---- PRD 连续性生产契约（缺省时由 first/last_frame / action_desc / characters 回填）----
    story_event_id: str = ""
    purpose: str = ""
    # PRD VAL-422 §4.4.1：E/S/I/KL 四类 ID 分属四个空间，禁止互相混写。
    # story_event_id 只放剧本事件 E*；主线节拍走 spine_beat_ids；关键台词走 key_line_ids。
    spine_beat_ids: list[str] = Field(default_factory=list)
    key_line_ids: list[str] = Field(default_factory=list)
    information_ids: list[str] = Field(default_factory=list)
    # 兼容旧字段一个版本周期；内部立即归一到 information_ids（见 _sync_information_ids）。
    new_information_ids: list[str] = Field(default_factory=list)
    reinforcement_info_ids: list[str] = Field(default_factory=list)
    spoken_contract_status: str = "legacy"  # coherent | conflict | legacy
    state_in: str = ""
    primary_action: str = ""
    emotion_beat: str = ""
    state_out: str = ""
    observed_state_out: str = ""
    continuity_mode: str = ""
    characters_visible: list[str] = Field(default_factory=list)
    audio_cast: list[str] = Field(default_factory=list)
    audio_timeline: list[AudioTimelineItem] = Field(default_factory=list)
    required_text: RequiredOnScreenText | None = None
    continuity_state_in: ContinuityState = Field(default_factory=ContinuityState)
    continuity_state_out: ContinuityState = Field(default_factory=ContinuityState)
    reference_roles: list[str] = Field(default_factory=list)
    do_not_repeat: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    prompt_contract_version: str = ""
    legacy_unvalidated: bool = False
    camera_angle: str = ""
    spatial_anchor: str = ""
    is_final: bool = False
    context_requirement_ids: list[str] = Field(default_factory=list)
    resulting_change: str = ""
    readability_focus: str = ""
    camera_motivation: str = ""
    repeat_of_shot_id: str | None = None
    repeat_gain: str = ""
    # Narrative task.  A reaction/establishing/processing shot may have no
    # primary action, but it must still own a non-empty evidence contribution.
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
    # 分镜台 2.0.0（docs/STORYBOARD_PROMPT_IR_DESIGN.md）：episode_prep_pack 输入
    # 走 app.production.storyboard_pack 的新生成路径，一行 = 一个 15 秒段，段内
    # 3-4 镜写进 prompt_text 文本、不拆成独立 Shot。这个字段非 None 是唯一权威
    # 标记：shot_size/camera_move/camera_angle/first_frame_desc/last_frame_desc
    # 等描述单个连续镜头的字段在这行上不再有意义，校验器与生成台据此字段显式
    # 跳过那些假设，而不是对空值静默判错或静默放行。字段内容即冻结契约的
    # segments[] 一条记录（prompt_text/resources/dialogue/degraded_capabilities/
    # source_segment_indexes/shot_count/beat_ids/beats/target_model/
    # storyboard_version）。``beat_ids`` 是历史裸 ID 列表（兼容旧消费方）；
    # ``beats`` 是本段命中的节拍全量记录（beat_id/summary/segment_indexes，
    # 字段名与冻结契约 beat_sheet[] 一致），供前端展示节拍摘要用，见
    # app/production/storyboard_pack.py persist_storyboard_pack。
    storyboard_pack_segment: dict[str, Any] | None = None
    # 镜头形态（WS7，2026-09-02，见 .shot_montage 模块 docstring 的完整背景）：
    # "scene"（默认）= 一镜一地一动作，其余描述字段照旧解释；"montage" = 这一行
    # 的原文是叙述者总结/回忆列举/跨年排比，narration 承载原文本身（逐字或忠实
    # 压缩），scene_name/scene_time 等单场景字段在这行上不再有意义，改由 beats
    # 承载段内最多 3 个独立拍点。判据是正面陈述，不是黑名单：原文段落本身构成
    # 「我八岁……我十三岁……我三十五岁」这类跨越多个时间点的排比或列举时才用
    # montage；单一场景内的连续对白/动作（即使台词全部 offscreen_voice，例如
    # 人物在原地的第一人称内心独白）必须保持 "scene"，不得因为台词是画外音就
    # 顺带改判——那是两件独立的事：offscreen_voice 描述「这句话是不是张嘴说
    # 的」，form 描述「这一行是不是横跨多个时空」。
    form: str = "scene"
    beats: list[MontageBeat] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sync_information_ids(self) -> "Shot":
        return _normalize_information_ids(self)
