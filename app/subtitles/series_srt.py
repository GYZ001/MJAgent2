"""连播成片整片字幕（PRD §9）：各集 cue 时间轴按 chapter 偏移拼接成一份 SRT。

纯函数，只依赖标准库；靠 ``app.subtitles`` = 1 前缀定层，不需要单独声明
（本文件不碰 store/engine/db，也不碰任何调用方）。
"""
from __future__ import annotations

from app.subtitles.ass import render_srt
from app.subtitles.cues import Cue


def build_series_srt(episode_sections: list[tuple[float, dict]]) -> str:
    """``episode_sections``：每项 ``(chapter.start_s, episode.edit-report.json["subtitles"])``。

    没有任何一集带 ``cues_timeline`` 时返回空串（连播 report 的 ``subtitle_srt``
    键相应写 ``None``，不生成空文件——挂产物信号，不挂状态字段）。
    """
    cues: list[Cue] = []
    for offset_s, subtitles in episode_sections:
        if not isinstance(subtitles, dict):
            continue
        for item in subtitles.get("cues_timeline") or []:
            cues.append(Cue(
                shot_no=int(item["shot_no"]), utterance_id=str(item["utterance_id"]), text=str(item["text"]),
                start_s=float(item["start_s"]) + offset_s, end_s=float(item["end_s"]) + offset_s,
            ))
    if not cues:
        return ""
    return render_srt(cues)
