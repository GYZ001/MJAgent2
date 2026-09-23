"""``_AiSegmentPlan``/``DROPPABLE_MAX_CHARS`` 独立叶子模块。

从 ``app.production.storyboard_beat_sheet`` 逐字搬出（不改字段/默认值/行为），
理由是 ``app.production.storyboard_beat_sheet_repair`` 需要这两个符号来构造/
比较段落草稿，而 ``storyboard_beat_sheet`` 又在模块级 import
``storyboard_beat_sheet_repair`` 的修补函数——两边互相 import 就成环（架构
复测 2026-09-23 抓到）。本文件只依赖同包内更底层的
``storyboard_segment_ranges``（叶子，零依赖二者），两个兄弟模块都改从这里
导入，`storyboard_beat_sheet` 仍用 `as` 自别名re-export，全仓既有的
``from app.production.storyboard_beat_sheet import _AiSegmentPlan`` 用法不受
影响。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

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
