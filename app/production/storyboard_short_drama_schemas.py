"""短剧节奏档节拍表 schema：忠实档 ``_AiBeat``/``_AiBeatSheetDraft`` 的显式子类
（2026-09-23 用户拍板新增「改编强度档位」：``faithful``/``short_drama``）。

不修改忠实档任何一个字段——子类只新增字段，父类原样不动，保证忠实档发给
模型的 ``output_schema``/``model_json_schema()`` 逐字节不变（见
``tests/test_storyboard_short_drama_schemas.py`` 的指纹冻结测试：忠实档不
接触本模块任何一行）。放在独立叶子模块、不进 ``storyboard_beat_sheet.py``
是为了避免循环导入：本模块 import ``storyboard_beat_sheet_schemas``（叶子），
``storyboard_beat_sheet.py`` 反过来要 import 本模块选 ``chat_structured`` 的
``model_type``——若把这两个子类直接定义在 ``storyboard_beat_sheet.py`` 里，
本模块与它就会互相 import 成环。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.production.storyboard_beat_sheet_schemas import _AiBeat, _AiBeatSheetDraft
from app.production.storyboard_dialogue_ledger import _AiDroppedLine


class _AiDroppedSourceSpan(BaseModel):
    """短剧档模型声明的一段「整块删掉的非关键原文」（阶段一草稿字段）。

    这是模型的原始声明，不是最终落库记录——冲突（已被某段覆盖/含保留台词/
    必拍内容/paratext）由 ``app.production.storyboard_short_drama.
    reconcile_dropped_units``/``finalize_dropped_units`` 确定性裁掉，落库前
    还会按修补后的状态重算一遍，见该模块 docstring。
    """

    source_segment_index: int
    from_unit: int = Field(ge=1)
    to_unit: int = Field(ge=1)
    reason: str = Field(min_length=1)


class _AiShortDramaBeat(_AiBeat):
    #: key=推动主线/人物关系/关键设定/章末钩子，必须被某段的 beat_ids 引用；
    #: optional=可删的闲笔、重复、过场。不给默认值：每个节拍都必须显式分类，
    #: 不允许模型漏填后被悄悄当成某一类处理。
    importance: Literal["key", "optional"]


class _AiShortDramaDroppedLine(_AiDroppedLine):
    """忠实档 ``_AiDroppedLine`` 的短剧档子类：新增必填 ``beat_id``——这条个别
    （区间外）弃置的台词属于哪个节拍。核验见 ``app.production.
    storyboard_short_drama_beat_guard.restore_dropped_lines_with_invalid_beat``：
    beat_id 必须真实存在、所属节拍 ``importance`` 必须是 ``optional``、且这句
    台词的原文段号必须落在该节拍 ``segment_indexes`` 覆盖范围内，三者任一不
    满足就被机械放回 ``kept_lines``（偏向保留），不打回模型——2026-09-24 真实
    三集验证实测：没有这道核验时，模型会用这条豁免删掉修炼口诀、目标铺垫这类
    关键节拍的台词，理由写得通顺但经不起核对。随原文区间强制弃置的台词（
    ``storyboard_short_drama._SPAN_DROP_REASON_PREFIX`` 前缀）由代码直接构造
    基类 ``_AiDroppedLine`` 实例，不经过这个子类，天然不受影响；语气词/屏上
    文字弃置同样不受这条新规则的语义核验约束（见该函数 docstring）。
    """

    beat_id: str = Field(min_length=1)


class _AiShortDramaBeatSheetDraft(_AiBeatSheetDraft):
    beat_sheet: list[_AiShortDramaBeat] = Field(min_length=1)
    #: 覆盖父类字段类型（忠实档 dropped_lines 不要求 beat_id，见上面
    #: _AiShortDramaDroppedLine 与 _AiDroppedLine 两个类各自的字段集合）。
    dropped_lines: list[_AiShortDramaDroppedLine] = Field(default_factory=list)
    #: 模型声明要整块删掉的非关键原文区间；忠实档没有这个字段（getattr 判断
    #: 「是不是短剧档草稿」时以此为准，见 storyboard_short_drama 模块）。
    dropped_source_spans: list[_AiDroppedSourceSpan] = Field(default_factory=list)
