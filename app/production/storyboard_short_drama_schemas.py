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

2026-09-24 新增 ``verify_dropped_source_spans``（区间的 beat 归属核验，见其
docstring）：本该是 ``storyboard_short_drama.py`` 的逻辑，但那个文件当时已
无行数余量，且这条核验只需要 span/beat 的字段值、不需要 ``storyboard_short_
drama`` 的任何符号，放在本模块（``_AiDroppedSourceSpan`` 的真源）比新开一个
文件更贴近「这个字段的合法值谁说了算」——``storyboard_short_drama.
reconcile_dropped_units`` 单向 import 本函数，不构成新的循环。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.production.storyboard_beat_sheet_schemas import _AiBeat, _AiBeatSheetDraft
from app.production.storyboard_dialogue_ledger import _AiDroppedLine


class _AiDroppedSourceSpan(BaseModel):
    """短剧档模型声明的一段「整块删掉的非关键原文」（阶段一草稿字段）。

    这是模型的原始声明，不是最终落库记录——冲突（已被某段覆盖/含保留台词/
    必拍内容/paratext/beat 归属不合法）由 ``app.production.storyboard_short_
    drama.reconcile_dropped_units``/``finalize_dropped_units`` 确定性裁掉，
    落库前还会按修补后的状态重算一遍，见该模块 docstring。

    ``beat_id``（2026-09-24 新增必填字段）：这条区间所属的节拍——核验见
    ``verify_dropped_source_spans``，三条任一不满足（beat 不存在/不是
    optional/segment_indexes 不覆盖这个 source_segment_index）整条区间不认。
    B 机沙箱第三轮真实验证实测：没有这道核验时，模型把关键铺垫台词（修炼
    口诀的目标铺垫）整块划进删减区间绕开了逐句台词的 beat_id 核验。
    """

    source_segment_index: int
    from_unit: int = Field(ge=1)
    to_unit: int = Field(ge=1)
    reason: str = Field(min_length=1)
    beat_id: str = Field(min_length=1)


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


def verify_dropped_source_spans(
    spans: list[_AiDroppedSourceSpan], beats_by_id: dict[str, Any],
) -> tuple[list[_AiDroppedSourceSpan], list[str]]:
    """区间的 beat 归属核验（模型提名、代码核验）：``beat_id`` 必须真实存在、
    所属节拍 ``importance="optional"``、且这个节拍的 ``segment_indexes``
    覆盖区间的 ``source_segment_index``——三条任一不满足，整条区间**不认**
    （不产出到返回的合法列表里），当成模型没声明过这段删减；调用方
    （``storyboard_short_drama.reconcile_dropped_units``）据此让这段原文退回
    既有的洞检测/补段安全网，其中的台词也不会被强制弃置。与逐句台词的
    ``storyboard_short_drama_beat_guard._beat_id_is_valid`` 同一套三条判据、
    同一个顺序，但这里作用于整条区间（不是单句台词），也不做"放回 kept_
    lines"这类修补动作——区间不认之后，它覆盖的原文单元本就没被声明为删减，
    不需要额外动作。返回 ``(合法区间列表, 人话核验日志)``，调用方负责写
    ``_LOGGER.info``（本函数不直接依赖 logging，保持纯函数、方便单测）。
    """
    valid: list[_AiDroppedSourceSpan] = []
    notes: list[str] = []
    for span in spans:
        where = f"原文段 {span.source_segment_index} S{span.from_unit:02d}-S{span.to_unit:02d}"
        beat = beats_by_id.get(span.beat_id)
        if beat is None:
            notes.append(f"{where} 声明的删减区间 beat_id={span.beat_id!r} 不存在，整条区间按未声明处理")
        elif getattr(beat, "importance", None) != "optional":
            notes.append(f"{where} 声明的删减区间所属节拍 {span.beat_id} 不是 optional，整条区间按未声明处理")
        elif span.source_segment_index not in beat.segment_indexes:
            notes.append(f"{where} 声明的删减区间所属节拍 {span.beat_id} 的 segment_indexes 未覆盖该原文段，整条区间按未声明处理")
        else:
            valid.append(span)
    return valid, notes
