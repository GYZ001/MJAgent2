"""``_AiSegmentPlan``/``_AiBeat``/``_AiBeatSheetDraft``/``DROPPABLE_MAX_CHARS``/
``SEGMENT_DURATION_S`` 独立叶子模块。

从 ``app.production.storyboard_beat_sheet`` 逐字搬出（不改字段/默认值/行为），
理由是 ``app.production.storyboard_beat_sheet_repair`` 需要这两个符号来构造/
比较段落草稿，而 ``storyboard_beat_sheet`` 又在模块级 import
``storyboard_beat_sheet_repair`` 的修补函数——两边互相 import 就成环（架构
复测 2026-09-23 抓到）。本文件只依赖同包内更底层的
``storyboard_segment_ranges``（叶子，零依赖二者）与
``storyboard_dialogue_ledger``（同样不反向依赖本文件），两个兄弟模块都改从
这里导入，`storyboard_beat_sheet` 仍用 `as` 自别名re-export，全仓既有的
``from app.production.storyboard_beat_sheet import _AiSegmentPlan`` 用法不受
影响。

2026-09-23 短剧节奏档改造再挪两样：``_AiBeat``/``_AiBeatSheetDraft`` 原本定义
在 ``storyboard_beat_sheet.py``，新增的 ``app.production.storyboard_short_drama_
schemas`` 需要子类化这两个类（加 ``importance``/``dropped_source_spans`` 字段），
但那两个类又要被 ``storyboard_beat_sheet`` 用作 ``chat_structured`` 的
``model_type``——两边互相依赖就成环，搬到这个叶子模块后双方都从真源导入，
不借道对方。``SEGMENT_DURATION_S`` 原本定义在 ``storyboard_pack.py``，短剧档
的目标段数（``target_duration_s / SEGMENT_DURATION_S``）要在 ``storyboard_
beat_sheet``/``storyboard_short_drama`` 里换算，而这两个模块都不能 import
``storyboard_pack``（同一条循环导入边界），一并搬到这里，``storyboard_pack.py``
改用 ``as`` 自别名再导出，数值与用法不变。三处搬移都是纯移动，不改字段/
默认值/行为——``_generate_beat_sheet`` 发给模型的 ``output_schema``/``rules``
逐字节不变（忠实档指纹冻结测试见 ``tests/test_storyboard_short_drama_schemas.py``）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .storyboard_dialogue_ledger import _AiDroppedLine, _AiKeptLine
from .storyboard_segment_ranges import _AiSourceUnitRange


class _AiSegmentPlan(BaseModel):
    segment_no: int
    synopsis: str
    source_segment_indexes: list[int] = Field(min_length=1)
    beat_ids: list[str] = Field(default_factory=list)
    #: 2.2.0 色温弧线：开放词汇，不设枚举；默认空串兼容模型截断导致的漏填。
    palette: str = ""
    #: 2.4.0：这一段对它引用的每个非 paratext 原文段号声明的句单元范围，
    #: 每个 source_segment_index 恰好一条；校验见 segment_unit_range_errors。
    source_unit_ranges: list[_AiSourceUnitRange] = Field(default_factory=list)
    #: 2026-10-01：这一段需要呈现的关键动作清单（模型提名，口径与阶段二
    #: storyboard_action_density.key_action_definition() 完全相同）；代码核验
    #: 条数是否超过本段承载量，超了必须拆成更多段——见 storyboard_beat_action_
    #: capacity.segment_action_capacity_errors。默认空列表兼容旧存量 beat_draft
    #: （storyboard_identity_regenerate._existing_plan 重建时只挑选固定字段，
    #: 不产出这个字段，同 physical_anchors 等既有字段先例）；不落库，同阶段二
    #: shot_action_beats（见 storyboard_beat_action_capacity 模块 docstring）。
    key_actions: list[str] = Field(default_factory=list)


#: 弃置只对语气词/寒暄这类短句成立；有说话人、正文超过这个字数的整句台词不是那三类。
DROPPABLE_MAX_CHARS = 4


class _AiBeat(BaseModel):
    beat_id: str
    summary: str
    segment_indexes: list[int] = Field(min_length=1)


class _AiEmotionalTurn(BaseModel):
    """人物情绪转折/重大决定节拍提名（P0-A，2026-09-27）。忠实档/短剧档共用
    （挂在基类 ``_AiBeatSheetDraft``）：核验见
    ``app.production.storyboard_beat_causality``。

    ``stimulus_beat_id``/``stimulus_evidence_quote``/``stimulus_missing_
    reason`` 三个字段构成一个平铺的"恰好二选一"约束，不用嵌套判别联合
    （代码层核验即可，见 ``storyboard_beat_causality._turn_problems``）：
    - 原文写清楚了促使这次决定/转折发生的具体刺激时：``stimulus_beat_id``
      填刺激所在的节拍（可以与转折是同一个节拍）、``stimulus_evidence_
      quote`` 逐字取自该节拍覆盖的原文，``stimulus_missing_reason`` 留空
      （``""``）；
    - 确实找不到原文写出的诱因时：``stimulus_beat_id``/``stimulus_
      evidence_quote`` 都留空（``""``），``stimulus_missing_reason`` 如实
      说明原文缺了什么——不为了凑因果链编造原文没写的刺激。
    两个默认值都是 ``""`` 而不是 ``None``：空串在两种情形下语义相同（"没有
    值"），且与本类其余字段、``_AiHookNomination`` 等既有 schema 的空值口径
    一致，不必再多判断一种 ``None``。
    """

    beat_id: str = Field(min_length=1)
    turn_kind: Literal["decisive_action", "emotional_reaction"]
    turn_evidence_quote: str = Field(min_length=1)  # 逐字取自 beat_id 覆盖原文
    stimulus_beat_id: str = ""
    stimulus_evidence_quote: str = ""
    stimulus_missing_reason: str = ""
    #: 2026-09-30（真实回归 proj_ca86b15ab7d7 EP1）：刺激确有其事
    #: （stimulus_beat_id 非空）时，原文是把这次刺激写成间接转述/叙述（别人
    #: 说了什么、但原文没有用引号把这句话本身写出来），还是引号台词或一个
    #: 可以直接看见的动作/画面——这是语义判断，只有读原文的模型分得清，代码
    #: 判不出（CLAUDE.md「判据从数据推导，模型提名、代码核验」）。true 时
    #: 阶段二必须在对应段落补一条旁白把这句话说出来，见
    #: app.production.storyboard_stimulus_voice 模块 docstring；默认 false
    #: 兼容旧存量 beat_draft（旧提名一律按"不需要额外出声"处理，回退行为与
    #: 改造前一致）。
    stimulus_needs_voice: bool = False


class _AiForeshadowingBeat(BaseModel):
    """伏笔/悬念/类型信号节拍提名（P0-C，2026-09-27）。忠实档/短剧档共用。
    核验见 ``app.production.storyboard_beat_foreshadowing``。"""

    beat_id: str = Field(min_length=1)
    signal_kind: Literal["foreshadowing", "genre_signal"]
    evidence_quote: str = Field(min_length=1)


class _AiWardrobeState(BaseModel):
    """全集服装表条目提名（P0-D，2026-09-29，真实回归见
    ``app.production.storyboard_wardrobe_plan`` 模块 docstring）。忠实档/
    短剧档共用。一个人物在全集里有几次换装就有几条记录，第一条是这个人物
    第一次出场时的着装。核验/按段拆解见
    ``app.production.storyboard_wardrobe_plan``。"""

    identity_id: str = Field(min_length=1)
    beat_id: str = Field(min_length=1)
    wardrobe: str = Field(min_length=1)
    change_reason: str = Field(min_length=1)


class _AiPropEntrance(BaseModel):
    """道具入场计划提名（P0-D，2026-09-29）。忠实档/短剧档共用。核验/按段
    拆解见 ``app.production.storyboard_prop_entrance``。"""

    label: str = Field(min_length=1)
    beat_id: str = Field(min_length=1)
    entrance_description: str = Field(min_length=1)


class _AiPhysicalAnchor(BaseModel):
    """体貌专用锚点申报（P0-E，2026-09-30，真实回归 proj_ca86b15ab7d7 EP1 逐帧核对，
    见 ``app.production.storyboard_physical_anchor`` 模块 docstring）。忠实档/短剧档
    共用。核验（是完整外观锚点的字符子序列）与覆盖见该模块。"""

    identity_id: str = Field(min_length=1)
    physical_description: str = Field(min_length=1)


class _AiPropAppearanceLock(BaseModel):
    """道具外观全集锁定提名（P0-F，2026-09-30，真实回归 proj_ca86b15ab7d7 EP1 逐帧
    核对，见 ``app.production.storyboard_prop_appearance_lock`` 模块 docstring）。
    忠实档/短剧档共用。核验（有卡逐字核对）/按 beat_id 跨段分发见该模块。"""

    label: str = Field(min_length=1)
    appearance: str = Field(min_length=1)
    #: 全集范围内这件道具会出现在画面中的每一个节拍，不只是入场那一个——
    #: 分发依据这份列表，不依赖任何原文段号交集过滤（见该模块 docstring）。
    beat_ids: list[str] = Field(min_length=1)


class _AiBeatSheetDraft(BaseModel):
    beat_sheet: list[_AiBeat] = Field(min_length=1)
    segments: list[_AiSegmentPlan] = Field(min_length=1)
    #: 2.1.0 对白台账：平铺列表，不用条件 schema；"逐一决定去留" 由
    #: _validate_beat_sheet_draft 的 dialogue_ledger_errors 检查兜底。
    kept_lines: list[_AiKeptLine] = Field(default_factory=list)
    dropped_lines: list[_AiDroppedLine] = Field(default_factory=list)
    #: 2026-09-27（P0-A/C）：数量因章而异，不强设最小值——原文里有几个就提名
    #: 几个，也可能确实一个都没有（见各自模块的三态 summary）。
    emotional_turns: list[_AiEmotionalTurn] = Field(default_factory=list)
    foreshadowing_beats: list[_AiForeshadowingBeat] = Field(default_factory=list)
    #: 2026-09-29（P0-D）：全集服装表/道具入场计划，默认空列表兼容旧存量
    #: beat_draft（``storyboard_identity_regenerate._existing_plan`` 重建时
    #: 不产出这两个字段，见该模块调用点）。
    wardrobe_plan: list[_AiWardrobeState] = Field(default_factory=list)
    prop_entrances: list[_AiPropEntrance] = Field(default_factory=list)
    #: 2026-09-30（P0-E）：体貌专用锚点申报，默认空列表兼容旧存量 beat_draft
    #: （storyboard_identity_regenerate._existing_plan 重建时不产出这个字段，见该
    #: 模块调用点）——回退行为与改造前完全一致（沿用完整外观锚点）。
    physical_anchors: list[_AiPhysicalAnchor] = Field(default_factory=list)
    #: 2026-09-30（P0-F）：道具外观全集锁定，默认空列表兼容旧存量 beat_draft。
    #: 2026-10-01 补丁前，``storyboard_identity_regenerate._existing_plan`` 重建
    #: 时不产出这个字段（持久化当时漏掉了）；补丁后从 ``storyboard_pack_
    #: adaptation`` 留档的 ``prop_appearance_locks_full`` 找回，留档是本次改动
    #: 之前生成的老格式时仍然拿不到，见该模块 ``_restored_plan_items`` 的
    #: ``prop_appearance_locks_stale`` 可见信号。
    prop_appearance_locks: list[_AiPropAppearanceLock] = Field(default_factory=list)


#: 固定 15 秒/段，不引入分档（用户 09-03 已拍板保留原设计）；短剧档段数目标由
#: SHORT_DRAMA_TARGET_DURATION_S ÷ 这个常量换算，真源仍是这一个数字。
SEGMENT_DURATION_S = 15
