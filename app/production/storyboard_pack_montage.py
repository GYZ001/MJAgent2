"""WS11：蒙太奇拍点（``montage_beats``）落库辅助，从 storyboard_pack.py 拆出
（该文件登记在 ``app/FILE_CONVENTIONS.toml`` 的 2072 行零余量基线，新逻辑不
能加在那边，见该文件条目旁的说明）。

背景（WS9 之后发现的陈年 bug，从未真正落地过）：``StoryboardPackSegment``
曾经只有一个 ``beats`` 字段——生成期被赋值成模型自报的 ``MontageBeat`` 列表
（``_AiStoryboardSegmentDraft.beats``），持久化期又被
``persist_storyboard_pack`` 无条件改写成叙事节拍摘要（``beat_id``/
``summary``/``segment_indexes``，来自 ``pack.beat_sheet``，供
``StoryboardPackSegment`` 自包含展示用）——同一个键先后被两种完全不同形状
的数据占用，后写的覆盖先写的。``app.continuity.apply_shot_contract`` 重建
``Shot.beats`` 时读到的因此永远是摘要形状，``MontageBeat.model_validate``
拿不到任何已知字段，产出全是默认空串的空对象——蒙太奇镜头形态实际上从未
真正落库过。``montage_beats`` 是独立键，只承载真正的拍点，不会被节拍摘要
覆盖；叙事节拍摘要继续用原来的 ``beats`` 键，字段名/测试零变化。
"""
from __future__ import annotations

from typing import Any


def fill_montage_beat_time_anchors(
    montage_beats: list[dict[str, Any]], segment_timeline_anchors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """模型没填某一拍的 ``time_anchor`` 时，用本段（WS9）确定性时间线锚点回填。

    只在锚点原文 ``value`` 逐字出现在这一拍自己的 ``source_span`` 里才算「与
    这一拍对应」，不按位置顺序瞎配——避免把不相关的锚点安在错误的拍上
    （CLAUDE.md「不得兜底填充」）。模型已自报 ``time_anchor`` 时不覆盖，尊重
    模型自己更精确的原文摘录。"""
    if not montage_beats or not segment_timeline_anchors:
        return montage_beats
    filled: list[dict[str, Any]] = []
    for beat in montage_beats:
        beat = dict(beat)
        span = str(beat.get("source_span") or "")
        if not beat.get("time_anchor") and span:
            match = next(
                (
                    a for a in segment_timeline_anchors
                    if str(a.get("value") or "") and str(a.get("value")) in span
                ),
                None,
            )
            if match:
                beat["time_anchor"] = str(match.get("value") or "")
        filled.append(beat)
    return filled


__all__ = ["fill_montage_beat_time_anchors"]
