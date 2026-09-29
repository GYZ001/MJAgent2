"""成片增强编排计划的结构化输出 Schema（``app.harness.model_gateway.chat_structured``
的 ``model_type``）。

四项设计简化（都在本模块 docstring 里交代，不在别处重复）：

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
4. **预告片段模型只选起点，不选时长**：``teaser_clips`` 每条只给
   ``start_s``（该段自身时间轴上的起始秒数），片段时长固定为
   ``TEASER_CLIP_LENGTH_S`` 秒，由代码（``app.final_edit_enhance.
   plan_validate``/``teaser``）据此算出 ``end_s``——不问模型"选多长"：
   "片长必须落在 1.5-4 秒区间" 是一个 JSON Schema 数值类型表达不了的区间
   约束（``end_s - start_s``），只能写进提示词文字里指望模型自己算对。
   2026-09-29 之前的版本确实让模型自己给 ``start_s``/``end_s`` 两个数，
   生产实测同一集两次作答（含一次重试）都给出整整 10 秒的片段，与目标区间
   相差数倍，校验只能逐条丢弃，导致预告片经常被判定为空。改成"只选起点、
   时长固定"后，这类片长直接从模型的自由度里消失，不再需要校验它。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# 预告片段的固定时长：3-4 段 × 3 秒 = 9-12 秒，落在既有预告总长目标区间
# 8-12 秒内（``app.final_edit_enhance.plan_validate.TEASER_TOTAL_MIN_S``/
# ``MAX_S``）——取代原先要求模型自己给 1.5-4 秒可变片长的做法（见上方
# docstring 第 4 条）。唯一权威定义处：其余模块（``plan_validate``/
# ``teaser``/``apply``）一律从这里导入，不各自重复这个数字。
TEASER_CLIP_LENGTH_S = 3.0


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

    shot_no: int = Field(
        description="预告片段取材的段号（shot_no），必须是给定 segments 列表中的段。",
    )
    start_s: float = Field(
        description=(
            f"该段自身时间轴上的起始秒数（从 0 开始，不是全集绝对时间）。片段"
            f"时长系统固定为 {TEASER_CLIP_LENGTH_S:.0f} 秒，从这个秒数起自动"
            f"截取到 start_s+{TEASER_CLIP_LENGTH_S:.0f} 秒——不要给出结束时间，"
            f"只需要选「从哪一秒开始最有悬念/信息量」；start_s 必须使"
            f"start_s+{TEASER_CLIP_LENGTH_S:.0f} 秒不超出该段总时长。"
        ),
    )
    reason: str = Field(
        default="", description="选中这一段作为预告的原因，仅供人工复核，不参与时长判定。",
    )


class MonologueLineDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_index: int = Field(
        description="从给定候选静默窗口列表中选择的下标（不要自己编造秒数）。",
    )
    character_name: str = Field(
        description="给定人物谱中的真实角色姓名，逐字取用。",
    )
    text: str = Field(
        description=(
            "本集原文里描述该角色内心感受/想法的一句叙述文字，取引号之外的"
            "叙述句（不是角色已经用引号说出口的台词），逐字原样摘录；不能与"
            "本集 segments 的 dialogue 中已经出现过的台词/旁白重复或互相"
            "包含——独白是观众听不到的心里话，不是把已经说过的话再念一遍。"
        ),
    )


class EnhancementPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    music_cues: list[MusicCueDraft] = Field(default_factory=list)
    teaser_clips: list[TeaserClipDraft] = Field(default_factory=list)
    monologue_lines: list[MonologueLineDraft] = Field(default_factory=list)
