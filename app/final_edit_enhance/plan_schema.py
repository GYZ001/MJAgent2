"""成片增强编排计划的结构化输出 Schema（``app.harness.model_gateway.chat_structured``
的 ``model_type``）。

三项设计简化（都在本模块 docstring 里交代，不在别处重复）：

1. **配乐不问模型"入点"**：模型只按段（``shot_no``，2.x 契约下一行 shot 就是
   一个 15 秒叙事段）提名 ``track_id``；相邻段若提名同一首，代码侧
   （``app.final_edit_enhance.music_mix``）自动接续播放同一条曲目的时间轴
   （不为每段重新从头播），不重新问模型"从哪一秒开始播"——那是机械的拼接
   决策，不是"哪首曲子适合这段情绪"这个模型判断本身该管的事。
2. **独白只问模型选哪个候选静默窗口，不问模型报数字**：候选窗口
   （``window_index``）由 ``app.final_edit_enhance.silence`` 从本集字幕
   对齐结果现算、随 payload 一起发给模型；模型自由报的任意 start/end 秒数
   既难以验证又容易与真实台词区间重叠，改成"从代码给定的候选里选一个"后，
   越界校验退化成"index 是否在列表范围内"，不可能产生模型编的时间戳。
3. **独白角色不写死名字**：``character_name`` 由模型按原文与人物谱判断，
   代码只核验它是否是本集真实出现的角色（``app.final_edit_enhance.
   plan_validate``），不是从枚举里选、也不是代码自己猜"主角是谁"。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MusicCueDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_no: int
    track_id: str


class TeaserClipDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_no: int
    start_s: float
    end_s: float
    reason: str = ""


class MonologueLineDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_index: int
    character_name: str
    text: str


class EnhancementPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    music_cues: list[MusicCueDraft] = Field(default_factory=list)
    teaser_clips: list[TeaserClipDraft] = Field(default_factory=list)
    monologue_lines: list[MonologueLineDraft] = Field(default_factory=list)
