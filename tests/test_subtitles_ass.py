"""app.subtitles.ass 的表驱动测试：ASS/SRT 逐字符断言、转义、时间格式、
ffmpeg 滤镜路径两级转义。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.subtitles.ass import (
    SubtitleStyle,
    ffmpeg_ass_filter,
    font_family_from_file,
    render_ass,
    render_srt,
)
from app.subtitles.cues import Cue

_WQY_FONT = Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")


@pytest.mark.skipif(not _WQY_FONT.is_file(), reason="本机没有 wqy-zenhei 字体文件")
def test_font_family_from_real_wqy_zenhei_file():
    assert font_family_from_file(_WQY_FONT) == "WenQuanYi Zen Hei"


# ---------------------------------------------------------------------------
# ASS 渲染：逐字符断言
# ---------------------------------------------------------------------------

def _style(**overrides) -> SubtitleStyle:
    base = dict(font_family="WenQuanYi Zen Hei")
    base.update(overrides)
    return SubtitleStyle(**base)


def test_ass_header_exact_fields():
    ass_text = render_ass([], _style(font_size=64, margin_bottom=400), (1080, 1920))
    lines = ass_text.splitlines()
    assert lines[0] == "[Script Info]"
    assert "ScriptType: v4.00+" in lines
    assert "PlayResX: 1080" in lines
    assert "PlayResY: 1920" in lines
    assert "WrapStyle: 2" in lines
    assert "ScaledBorderAndShadow: yes" in lines
    assert "[V4+ Styles]" in lines
    style_line = next(line for line in lines if line.startswith("Style: Default,"))
    assert style_line == (
        "Style: Default,WenQuanYi Zen Hei,64,&H00FFFFFF,&H00FFFFFF,&H00000000,"
        "&H00000000,-1,0,0,0,100,100,0,0,1,3,1,2,60,60,400,1"
    )
    assert "[Events]" in lines
    assert "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text" in lines


def test_ass_style_line_reflects_custom_size_and_margin():
    ass_text = render_ass([], _style(font_size=48, margin_bottom=280), (1080, 1920))
    style_line = next(line for line in ass_text.splitlines() if line.startswith("Style: Default,"))
    assert style_line.split(",")[2] == "48"  # Fontsize
    assert style_line.split(",")[-2] == "280"  # MarginV


def test_ass_event_line_exact_text_and_time():
    cue = Cue(shot_no=7, utterance_id="U01", text="是啊，", start_s=0.32, end_s=1.14, speaker="师弟")
    ass_text = render_ass([cue], _style(), (1080, 1920))
    event_line = next(line for line in ass_text.splitlines() if line.startswith("Dialogue:"))
    assert event_line == "Dialogue: 0,0:00:00.32,0:00:01.14,Default,,0,0,0,,是啊，"


def test_ass_show_speaker_prefixes_non_empty_speaker_only():
    with_speaker = Cue(shot_no=1, utterance_id="U01", text="走。", start_s=0.0, end_s=1.0, speaker="师弟")
    without_speaker = Cue(shot_no=1, utterance_id="U02", text="嗯。", start_s=1.0, end_s=2.0, speaker="")
    ass_text = render_ass([with_speaker, without_speaker], _style(show_speaker=True), (1080, 1920))
    events = [line for line in ass_text.splitlines() if line.startswith("Dialogue:")]
    assert events[0].endswith(",,师弟：走。")
    assert events[1].endswith(",,嗯。")


def test_ass_show_speaker_off_never_prefixes():
    cue = Cue(shot_no=1, utterance_id="U01", text="走。", start_s=0.0, end_s=1.0, speaker="师弟")
    ass_text = render_ass([cue], _style(show_speaker=False), (1080, 1920))
    event_line = next(line for line in ass_text.splitlines() if line.startswith("Dialogue:"))
    assert event_line.endswith(",,走。")


@pytest.mark.parametrize(
    "raw_text, expected_escaped",
    [
        ("含{花括号}", "含\\{花括号\\}"),
        ("反斜杠\\符号", "反斜杠\\\\符号"),
        ("折行\\N两行", "折行\\N两行"),  # \N 换行标记必须原样保留，不能被转义成 \\N
        ("混合\\N{}\\结尾", "混合\\N\\{\\}\\\\结尾"),
    ],
)
def test_ass_text_escaping_exact(raw_text, expected_escaped):
    cue = Cue(shot_no=1, utterance_id="U01", text=raw_text, start_s=0.0, end_s=1.0)
    ass_text = render_ass([cue], _style(), (1080, 1920))
    event_line = next(line for line in ass_text.splitlines() if line.startswith("Dialogue:"))
    assert event_line.endswith(f",,{expected_escaped}")


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0.0, "0:00:00.00"),
        (0.32, "0:00:00.32"),
        (61.5, "0:01:01.50"),
        (3661.25, "1:01:01.25"),
    ],
)
def test_ass_time_format(seconds, expected):
    cue = Cue(shot_no=1, utterance_id="U01", text="x", start_s=seconds, end_s=seconds + 1)
    ass_text = render_ass([cue], _style(), (1080, 1920))
    event_line = next(line for line in ass_text.splitlines() if line.startswith("Dialogue:"))
    assert event_line.split(",")[1] == expected


def test_ass_empty_cues_still_has_valid_header_no_events():
    ass_text = render_ass([], _style(), (1080, 1920))
    assert "[Events]" in ass_text
    assert "Dialogue:" not in ass_text


# ---------------------------------------------------------------------------
# SRT 渲染：逐字符断言，\N 转真实换行
# ---------------------------------------------------------------------------

def test_srt_exact_format_single_cue():
    cue = Cue(shot_no=1, utterance_id="U01", text="是啊，", start_s=0.32, end_s=1.14)
    srt_text = render_srt([cue])
    assert srt_text == "1\n00:00:00,320 --> 00:00:01,140\n是啊，\n\n"


def test_srt_converts_ass_linebreak_to_real_newline():
    cue = Cue(shot_no=1, utterance_id="U01", text="第一行\\N第二行", start_s=0.0, end_s=1.0)
    srt_text = render_srt([cue])
    assert "第一行\n第二行" in srt_text
    assert "\\N" not in srt_text


def test_srt_multiple_cues_sequential_index():
    cues = [
        Cue(shot_no=1, utterance_id="U01", text="甲", start_s=0.0, end_s=1.0),
        Cue(shot_no=1, utterance_id="U02", text="乙", start_s=1.0, end_s=2.0),
    ]
    srt_text = render_srt(cues)
    assert srt_text.startswith("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n2\n")


def test_srt_empty_cues_is_empty_string():
    assert render_srt([]) == ""


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0.0, "00:00:00,000"),
        (1.5, "00:00:01,500"),
        (3661.005, "01:01:01,005"),
    ],
)
def test_srt_time_format(seconds, expected):
    cue = Cue(shot_no=1, utterance_id="U01", text="x", start_s=seconds, end_s=seconds + 1)
    srt_text = render_srt([cue])
    assert srt_text.split(" --> ")[0].split("\n")[-1] == expected


# ---------------------------------------------------------------------------
# ffmpeg 滤镜路径两级转义
# ---------------------------------------------------------------------------

def test_ffmpeg_escape_space_colon_quote_path():
    """派单要求：用一个含空格、冒号、单引号的路径写断言。"""
    ass_path = Path("/tmp/my dir:test's/episode.ass")
    fonts_dir = Path("/tmp/fonts dir")
    result = ffmpeg_ass_filter(ass_path, fonts_dir)
    expected_ass = "/tmp/my dir" + ("\\" * 2) + ":test" + ("\\" * 3) + "'s/episode.ass"
    assert result == f"ass={expected_ass}:fontsdir=/tmp/fonts dir"


@pytest.mark.parametrize(
    "raw_char, expected_backslashes",
    [(":", 2), ("'", 3), ("\\", 4), (",", 1), ("[", 1), ("]", 1), (";", 1)],
)
def test_ffmpeg_escape_each_special_char(raw_char, expected_backslashes):
    from app.subtitles.ass import _escape_ffmpeg_path

    escaped = _escape_ffmpeg_path(raw_char)
    assert escaped.count("\\") == expected_backslashes
    assert escaped.endswith(raw_char)


def test_ffmpeg_escape_space_untouched():
    from app.subtitles.ass import _escape_ffmpeg_path

    assert _escape_ffmpeg_path(" ") == " "
