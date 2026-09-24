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


#: 弃置只对语气词/寒暄这类短句成立；有说话人、正文超过这个字数的整句台词不是那三类。
DROPPABLE_MAX_CHARS = 4


class _AiBeat(BaseModel):
    beat_id: str
    summary: str
    segment_indexes: list[int] = Field(min_length=1)


class _AiBeatSheetDraft(BaseModel):
    beat_sheet: list[_AiBeat] = Field(min_length=1)
    segments: list[_AiSegmentPlan] = Field(min_length=1)
    #: 2.1.0 对白台账：平铺列表，不用条件 schema；"逐一决定去留" 由
    #: _validate_beat_sheet_draft 的 dialogue_ledger_errors 检查兜底。
    kept_lines: list[_AiKeptLine] = Field(default_factory=list)
    dropped_lines: list[_AiDroppedLine] = Field(default_factory=list)


#: 固定 15 秒/段，不引入分档（用户 09-03 已拍板保留原设计）；短剧档段数目标由
#: SHORT_DRAMA_TARGET_DURATION_S ÷ 这个常量换算，真源仍是这一个数字。
SEGMENT_DURATION_S = 15
