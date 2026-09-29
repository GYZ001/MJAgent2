"""预告片头/主角内心独白对既有字幕报告（``app.subtitles.episode.report_section``
的产物）的两处必要改动：

1. 片头预告插进正片前面后，正片字幕在"最终成片"这条时间轴上的绝对位置要整体
   后移预告时长——只改 ``cues_timeline``（sidecar/连播台章节偏移唯一消费的
   字段）与随之重算的 srt/ass，不改 ``lines``/``missing``/``extra_speech``
   （这些是按镜号呈现的诊断信息，语义上不随"整片在最终产物里挪到第几秒"变化）。
2. 主角内心独白的文本与时间追加进同一条 cue 时间轴，一并出字幕/出 srt。

AI 标识的独立 ``Dialogue`` 行（``extra_events``）不在本函数重建范围内——它由
``app.subtitles.ass.ai_label_event`` 生成的是原始字符串而不是结构化 ``Cue``，
本函数拿不到它来源的 ``play_res``/``duration_s`` 之外的参数去重新推导时移；
这是已知的窄边界局限（AI 标识 + 片头预告同开时，重建出的 ``episode.ass``
边车文件会丢失标识那一行，只影响下载的字幕边车文件本身，不影响已经烧进
像素的标识——见 ``app.final_edit_enhance.apply`` 模块文档）。
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.final_edit_enhance.monologue_audio import MonologueAudioItem
from app.subtitles.ass import SubtitleStyle, render_ass, render_srt
from app.subtitles.cues import Cue


def _cue_from_entry(entry: dict[str, Any], offset_s: float) -> Cue:
    return Cue(
        shot_no=int(entry["shot_no"]), utterance_id=str(entry["utterance_id"]), text=str(entry["text"]),
        start_s=float(entry["start_s"]) + offset_s, end_s=float(entry["end_s"]) + offset_s,
        estimated=bool(entry.get("estimated", False)),
    )


def _monologue_cues(items: list[MonologueAudioItem], offset_s: float) -> tuple[Cue, ...]:
    return tuple(
        Cue(
            shot_no=-1, utterance_id=f"MONO{i:02d}", text=item.text,
            start_s=item.start_s + offset_s, end_s=item.start_s + item.duration_s + offset_s,
            speaker="内心独白",
        )
        for i, item in enumerate(items)
    )


def shift_and_augment_subtitles(
    subtitles: dict[str, Any], *, offset_s: float, monologue_items: list[MonologueAudioItem],
    style: SubtitleStyle, play_res: tuple[int, int],
) -> dict[str, Any]:
    if offset_s <= 1e-9 and not monologue_items:
        return subtitles
    if not subtitles.get("enabled"):
        # 字幕总开关关闭时没有 cues_timeline 可移；独白字幕单独走
        # ``app.final_edit_enhance.monologue_burn``（不依赖这里的重建），
        # 这里原样返回，不伪造一份"启用"的字幕报告。
        return subtitles
    dialogue_cues = tuple(_cue_from_entry(e, offset_s) for e in subtitles.get("cues_timeline") or [])
    all_cues = tuple(sorted((*dialogue_cues, *_monologue_cues(monologue_items, offset_s)), key=lambda c: c.start_s))
    ass_text = render_ass(all_cues, style, play_res)
    srt_text = render_srt(all_cues)
    updated = dict(subtitles)
    updated["cues_timeline"] = [
        {"shot_no": c.shot_no, "utterance_id": c.utterance_id, "text": c.text, "start_s": c.start_s, "end_s": c.end_s, "estimated": c.estimated}
        for c in all_cues
    ]
    updated["cues"] = len(all_cues)
    updated["ass_text"] = ass_text
    updated["ass_sha256"] = hashlib.sha256(ass_text.encode("utf-8")).hexdigest()
    updated["srt_sha256"] = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    return updated
