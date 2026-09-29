"""成片增强编排计划的结构化输出 Schema（``app.harness.model_gateway.chat_structured``
的 ``model_type``）。

三项设计简化（都在本模块 docstring 里交代，不在别处重复）：

1. **配乐是"换曲点"而不是逐段配乐表**：``music_cues`` 里每一条是把全集
   按情绪切成几个大段之后的一个分段起点——模型只在真正换曲的 ``shot_no``
   （2.x 契约下一行 shot 就是一个 15 秒叙事段）给一条，代码侧
   （``app.final_edit_enhance.apply``/``music_runs``）按 shot_no 顺序把这份
   稀疏换曲点展开成逐段稠密映射，再折叠成连续播放的时间轴——一条 cue 从它
   的 shot_no 起持续播放到下一条 cue 的 shot_no 为止（没有下一条就播到全集
   结束），不是只覆盖它自己那一段；不重新问模型"从哪一秒开始播"——那是机械
   的拼接决策，不是"哪首曲子适合这段情绪"这个模型判断本身该管的事。2026-09-29
   之前的版本要求模型给出的 ``track_id`` 只在字面相邻的 shot_no 之间折叠、
   缺条目的段落一律静音，实测导致模型逐段换曲、又只覆盖前几段（见
   ``app.final_edit_enhance.apply`` 里 ``expand_sparse_cues`` 调用点的说明）。
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

    shot_no: int = Field(
        description=(
            "换曲点所在的段号（shot_no）：从这一段起改播 track_id 对应的曲子，"
            "一直连续播放到下一条 music_cues 的 shot_no 为止（没有下一条就播到"
            "全集结束）——不是只给这一段配乐，不要给每个 shot_no 都各提一条，"
            "只在真正换曲时给一条。"
        ),
    )
    track_id: str = Field(
        description=(
            "从给定曲库逐字选取的曲目 ID，要与这一段的情绪匹配（参考曲库的 "
            "mood_tags）；情绪没变就不要重复提名，只在真正换曲时新增一条。"
        ),
    )


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
