"""镜头内嵌状态契约：对白/音轨时间线与可比较的连续性状态快照。

ContinuityState 把 state_in/state_out 的自然语言描述之外，另存一份可程序
比较的场景/人物/道具结构化快照，供连续性校验直接比对而不必重新理解自然语言。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

class Dialogue(BaseModel):
    speaker: str
    line: str
    emotion: str = "平静"
    # 画外角色发声：保留 speaker 身份，但不要求画面口型
    delivery: str = "spoken_dialogue"  # spoken_dialogue | offscreen_voice | narration


class AudioTimelineItem(BaseModel):
    start_s: float = 0.0
    end_s: float = 0.0
    type: str = "ambient_sound"
    speaker_id: str | None = None
    text: str = ""
    lip_sync: bool = False
    emotion: str = "平静"
    voice_canonical: str = ""


class SceneContinuityState(BaseModel):
    scene_revision_id: str = ""
    time_of_day: str = ""
    lighting_state: str = ""
    axis_id: str = ""
    landmarks: dict[str, str] = Field(default_factory=dict)


class CharacterContinuityState(BaseModel):
    look_revision_id: str = ""
    outfit_revision_id: str = ""
    visibility: str = "visible"
    visible_in_frame: bool | None = None
    screen_side: str = ""
    pose: str = ""
    facing: str = ""
    gaze_target: str = ""
    left_hand: str = ""
    right_hand: str = ""


class PropContinuityState(BaseModel):
    canonical_name: str = ""
    revision_id: str = ""
    owner: str = ""
    location: str = ""
    form: str = ""
    visibility: str = "optional"
    text_state: str = "none"
    required: bool = False


class ContinuityState(BaseModel):
    """可比较的镜头状态快照；自然语言 state_in/state_out 仍作为生成提示。"""

    scene: SceneContinuityState = Field(default_factory=SceneContinuityState)
    characters: dict[str, CharacterContinuityState] = Field(default_factory=dict)
    props: dict[str, PropContinuityState] = Field(default_factory=dict)


class RequiredOnScreenText(BaseModel):
    surface: str = ""
    exact_text: str = ""
    strategy: str = "deterministic_insert"
    delivery_owner_shot_no: int | None = None
    appear_start_s: float = 0.0
    stable_until_s: float | None = None
    style: str = ""
    allow_other_text: bool = False
    max_other_text: int = 0
    font_role: str = "classical_serif"
    reading_priority: str = "plot_critical"
