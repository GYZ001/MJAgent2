"""ASS/SRT 字幕渲染与 ffmpeg 滤镜参数生成（PRD §8）。纯函数，只依赖标准库 +
Pillow（仓库已有依赖，只用来读字体家族名，不做任何图像渲染）。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont

from app.subtitles.cues import Cue

_PLAY_RES_X = 1080
_PLAY_RES_Y = 1920

# ffmpeg 滤镜参数的两级转义特殊字符集合（ffmpeg-filters 文档「Notes on
# filtergraph escaping」）：第一级只转义 : \ '；第二级在第一级结果之上再转义
# \ ' [ ] , ;（含第一级产生的反斜杠本身要再转一次）。空格不在任一级特殊字符
# 集合内，原样保留。
_ESCAPE_LEVEL1 = frozenset(":\\'")
_ESCAPE_LEVEL2 = frozenset("\\'[],;")


@dataclass(frozen=True)
class SubtitleStyle:
    font_family: str
    font_size: int = 64
    margin_bottom: int = 400
    max_chars_per_line: int = 14
    show_speaker: bool = False


def font_family_from_file(font_path: Path) -> str:
    """从字体文件读家族名（如 wqy-zenhei.ttc -> WenQuanYi Zen Hei）。"""
    return ImageFont.truetype(str(font_path)).getname()[0]


def _escape_ass_text(text: str) -> str:
    """`\\`→`\\\\`、`{`→`\\{`、`}`→`\\}`；已有的 `\\N` 换行标记原样保留。"""
    result: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\\" and text[i : i + 2] == "\\N":
            result.append("\\N")
            i += 2
            continue
        ch = text[i]
        if ch == "\\":
            result.append("\\\\")
        elif ch == "{":
            result.append("\\{")
        elif ch == "}":
            result.append("\\}")
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def _cue_display_text(cue: Cue, style: SubtitleStyle) -> str:
    if style.show_speaker and cue.speaker:
        return f"{cue.speaker}：{cue.text}"
    return cue.text


def _ass_time(seconds: float) -> str:
    """ASS 时间格式 H:MM:SS.cc（centisecond，两位）。"""
    total_cs = round(max(0.0, seconds) * 100)
    cs, total_s = total_cs % 100, total_cs // 100
    s, total_m = total_s % 60, total_s // 60
    m, h = total_m % 60, total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_time(seconds: float) -> str:
    """SRT 时间格式 HH:MM:SS,mmm（millisecond，三位）。"""
    total_ms = round(max(0.0, seconds) * 1000)
    ms, total_s = total_ms % 1000, total_ms // 1000
    s, total_m = total_s % 60, total_s // 60
    m, h = total_m % 60, total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_header(style: SubtitleStyle) -> str:
    style_line = (
        f"Style: Default,{style.font_family},{style.font_size},&H00FFFFFF,"
        f"&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,1,2,60,60,"
        f"{style.margin_bottom},1\n"
    )
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {_PLAY_RES_X}\n"
        f"PlayResY: {_PLAY_RES_Y}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style_line}"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _ass_event_line(cue: Cue, style: SubtitleStyle) -> str:
    text = _escape_ass_text(_cue_display_text(cue, style))
    return f"Dialogue: 0,{_ass_time(cue.start_s)},{_ass_time(cue.end_s)},Default,,0,0,0,,{text}\n"


def render_ass(cues: Sequence[Cue], style: SubtitleStyle) -> str:
    header = _ass_header(style)
    events = "".join(_ass_event_line(c, style) for c in cues)
    return header + events


def render_srt(cues: Sequence[Cue]) -> str:
    blocks = []
    for idx, cue in enumerate(cues, start=1):
        text = cue.text.replace("\\N", "\n")
        blocks.append(f"{idx}\n{_srt_time(cue.start_s)} --> {_srt_time(cue.end_s)}\n{text}\n")
    return "\n".join(blocks) + ("\n" if blocks else "")


def _escape_level(text: str, special: frozenset[str]) -> str:
    return "".join(f"\\{ch}" if ch in special else ch for ch in text)


def _escape_ffmpeg_path(raw: str) -> str:
    return _escape_level(_escape_level(raw, _ESCAPE_LEVEL1), _ESCAPE_LEVEL2)


def ffmpeg_ass_filter(ass_path: Path, fonts_dir: Path) -> str:
    """返回 `ass=<转义路径>:fontsdir=<转义路径>`，路径按 ffmpeg 滤镜两级转义规则处理。"""
    return f"ass={_escape_ffmpeg_path(str(ass_path))}:fontsdir={_escape_ffmpeg_path(str(fonts_dir))}"
