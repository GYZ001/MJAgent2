"""``app.final_edit_enhance.subtitle_shift``：片头预告后移正片字幕时间轴 +
追加独白 cue；字幕总开关关闭、或没有实际改动（无预告、无独白）时原样透传。
"""
from __future__ import annotations

from pathlib import Path

from app.final_edit_enhance.monologue_audio import MonologueAudioItem
from app.final_edit_enhance.subtitle_shift import shift_and_augment_subtitles
from app.subtitles.ass import SubtitleStyle

STYLE = SubtitleStyle(font_family="WenQuanYi Zen Hei")
PLAY_RES = (1080, 1920)


def _subtitles(cues: list[dict]) -> dict:
    return {"enabled": True, "cues_timeline": cues, "cues": len(cues), "ass_text": "old-ass", "srt_sha256": "old", "ass_sha256": "old"}


def test_no_offset_and_no_monologue_returns_same_object() -> None:
    subtitles = _subtitles([])
    result = shift_and_augment_subtitles(subtitles, offset_s=0.0, monologue_items=[], style=STYLE, play_res=PLAY_RES)
    assert result is subtitles


def test_disabled_subtitles_returns_unchanged_even_with_offset() -> None:
    subtitles = {"enabled": False}
    result = shift_and_augment_subtitles(subtitles, offset_s=8.0, monologue_items=[], style=STYLE, play_res=PLAY_RES)
    assert result is subtitles


def test_offset_shifts_cue_timeline_start_and_end() -> None:
    cue = {"shot_no": 1, "utterance_id": "U01", "text": "你好", "start_s": 1.0, "end_s": 2.0, "estimated": False}
    result = shift_and_augment_subtitles(_subtitles([cue]), offset_s=8.0, monologue_items=[], style=STYLE, play_res=PLAY_RES)
    assert result["cues_timeline"] == [{"shot_no": 1, "utterance_id": "U01", "text": "你好", "start_s": 9.0, "end_s": 10.0, "estimated": False}]
    assert result["ass_text"] != "old-ass"
    assert result["ass_sha256"] != "old"
    assert result["srt_sha256"] != "old"


def test_monologue_items_appended_and_sorted_by_start_time() -> None:
    dialogue_cue = {"shot_no": 2, "utterance_id": "U02", "text": "对白", "start_s": 20.0, "end_s": 21.0, "estimated": False}
    item = MonologueAudioItem(start_s=5.0, duration_s=2.0, text="我不会认输", character_name="顾屿", audio_path=Path("/tmp/x.wav"))
    result = shift_and_augment_subtitles(_subtitles([dialogue_cue]), offset_s=0.0, monologue_items=[item], style=STYLE, play_res=PLAY_RES)
    texts_in_order = [c["text"] for c in result["cues_timeline"]]
    assert texts_in_order == ["我不会认输", "对白"]  # 独白 start_s=5 早于对白 start_s=20
    mono_entry = result["cues_timeline"][0]
    assert mono_entry["start_s"] == 5.0
    assert mono_entry["end_s"] == 7.0
    assert mono_entry["shot_no"] == -1


def test_monologue_start_time_also_shifted_by_teaser_offset() -> None:
    item = MonologueAudioItem(start_s=5.0, duration_s=2.0, text="我不会认输", character_name="顾屿", audio_path=Path("/tmp/x.wav"))
    result = shift_and_augment_subtitles(_subtitles([]), offset_s=10.0, monologue_items=[item], style=STYLE, play_res=PLAY_RES)
    assert result["cues_timeline"][0]["start_s"] == 15.0
    assert result["cues_timeline"][0]["end_s"] == 17.0
