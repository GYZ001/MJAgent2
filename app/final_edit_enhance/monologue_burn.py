"""主角内心独白的字幕烧录：只覆盖独白台词的 ASS 叠加层，单独一次 ffmpeg 编码
（对白/环境音字幕已经在 ``app.media_exec.concat`` 的既有合成路径里烧完，这里
不重复烧对白）。走现有 ``app.subtitles.ass`` 渲染管线，标注为"内心独白"复用
``Cue.speaker`` 字段（``show_speaker`` 关闭时该字段不显示，仍能通过
``app.final_edit_enhance.report`` 里的原句/出处让用户核对，满足"样式区分
可选"）。
"""
from __future__ import annotations

from pathlib import Path

from app.final_edit import _font_path
from app.final_edit_enhance.ffutil import run_ffmpeg
from app.final_edit_enhance.monologue_audio import MonologueAudioItem
from app.media_pipeline.delivery_encode import DELIVERY_VIDEO_ARGS, encode_timeout_s
from app.subtitles.ass import SubtitleStyle, ffmpeg_ass_filter, font_family_from_file, render_ass
from app.subtitles.cues import Cue

_MONOLOGUE_SPEAKER_TAG = "内心独白"


def default_style() -> SubtitleStyle:
    """字幕总开关关闭、没有 ``EpisodeSubtitlePlan`` 时的兜底样式，字体来源与
    普通对白字幕一致（``app.subtitles.episode.prepare_episode_subtitles``
    同一个 ``_font_path()``），只是不显示 speaker 前缀。"""
    return SubtitleStyle(font_family=font_family_from_file(_font_path()))


def _monologue_cues(items: list[MonologueAudioItem]) -> tuple[Cue, ...]:
    return tuple(
        Cue(
            shot_no=-1, utterance_id=f"MONO{i:02d}", text=item.text,
            start_s=item.start_s, end_s=item.start_s + item.duration_s,
            speaker=_MONOLOGUE_SPEAKER_TAG,
        )
        for i, item in enumerate(items)
    )


def burn_monologue_captions(
    video_path: Path, items: list[MonologueAudioItem], style: SubtitleStyle,
    play_res: tuple[int, int], work_dir: Path, total_duration_s: float,
) -> Path:
    if not items:
        return video_path
    cues = _monologue_cues(items)
    # 独白样式固定显示 speaker 前缀（不受项目字幕总开关的 show_speaker 设置
    # 影响）——这是独白与普通对白唯一的可见区分手段，关掉就退化成看不出来
    # 这句是心理描写还是说出口的话。
    style_with_speaker = SubtitleStyle(
        font_family=style.font_family, font_size=style.font_size, margin_bottom=style.margin_bottom,
        max_chars_per_line=style.max_chars_per_line, show_speaker=True,
    )
    ass_text = render_ass(cues, style_with_speaker, play_res)
    ass_path = work_dir / "monologue.ass"
    ass_path.write_text(ass_text, encoding="utf-8")
    out_path = work_dir / "monologue-burned.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
        "-vf", ffmpeg_ass_filter(ass_path, _font_path().parent),
        *DELIVERY_VIDEO_ARGS, "-c:a", "copy", "-movflags", "+faststart", str(out_path),
    ]
    run_ffmpeg(cmd, timeout=encode_timeout_s(total_duration_s), context="独白字幕烧录")
    return out_path
