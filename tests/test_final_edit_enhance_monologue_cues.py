"""``app.final_edit_enhance.monologue_cues``：独白 cue 切分与「内心独白：」
前缀预算——修复 2026-09-29 生产实测缺陷（proj_ca86b15ab7d7 EP1 一句 42 字独白
整行烧进画面、右边缘被裁掉，见 monologue_burn.py/subtitle_shift.py 修复前的
``_monologue_cues`` 各自只出一条不做切分的 Cue）。同时守住
``monologue_burn``/``subtitle_shift`` 必须共用这一个实现，不得各自再造一份。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.final_edit_enhance import monologue_burn, subtitle_shift
from app.final_edit_enhance.monologue_audio import MonologueAudioItem
from app.final_edit_enhance.monologue_cues import MONOLOGUE_SPEAKER_TAG, monologue_cues

_LONG_TEXT = "他只当是从前磕碰留下的旧疤，没有多想，只是把她的手又拢进了被子里，掌心一直没有松开。"
_MAX_CHARS = 14


def _item(text: str, start_s: float = 3.0, duration_s: float = 8.4) -> MonologueAudioItem:
    return MonologueAudioItem(
        start_s=start_s, duration_s=duration_s, text=text, character_name="顾屿", audio_path=Path("/tmp/mono.wav"),
    )


def test_long_line_splits_and_every_piece_within_max_chars_including_prefix():
    cues = monologue_cues([_item(_LONG_TEXT)], offset_s=0.0, max_chars_per_line=_MAX_CHARS)
    assert len(cues) > 1
    for c in cues:
        rendered = f"{c.speaker}：{c.text}" if c.speaker else c.text
        assert len(rendered) <= _MAX_CHARS


def test_only_first_piece_carries_speaker_tag():
    cues = monologue_cues([_item(_LONG_TEXT)], offset_s=0.0, max_chars_per_line=_MAX_CHARS)
    assert cues[0].speaker == MONOLOGUE_SPEAKER_TAG
    assert all(c.speaker == "" for c in cues[1:])


def test_pieces_cover_original_text_in_order_nothing_lost():
    cues = monologue_cues([_item(_LONG_TEXT)], offset_s=0.0, max_chars_per_line=_MAX_CHARS)
    joined = "".join(c.text for c in cues)
    assert _LONG_TEXT.startswith(joined)
    assert len(_LONG_TEXT) - len(joined) <= 1  # 至多丢末尾一串标点（既有约定）


def test_pieces_times_contiguous_within_line_span():
    item = _item(_LONG_TEXT, start_s=3.0, duration_s=8.4)
    cues = monologue_cues([item], offset_s=0.0, max_chars_per_line=_MAX_CHARS)
    assert cues[0].start_s == item.start_s
    for a, b in zip(cues, cues[1:]):
        assert b.start_s == a.end_s
    assert cues[-1].end_s <= item.start_s + item.duration_s + 1e-9
    assert all(c.shot_no == -1 for c in cues)


def test_short_line_stays_single_cue_unchanged():
    """短句（含前缀预算仍装得下）行为与旧实现完全一致：一条 cue，
    utterance_id 沿用旧的 "MONO{i:02d}" 格式，文本/时间不受影响。"""
    item = _item("我不会认输", start_s=5.0, duration_s=2.0)
    cues = monologue_cues([item], offset_s=0.0, max_chars_per_line=_MAX_CHARS)
    assert len(cues) == 1
    c = cues[0]
    assert c.utterance_id == "MONO00"
    assert c.text == "我不会认输"
    assert c.start_s == 5.0 and c.end_s == 7.0
    assert c.speaker == MONOLOGUE_SPEAKER_TAG
    assert c.shot_no == -1


def test_offset_s_shifts_all_pieces():
    cues0 = monologue_cues([_item(_LONG_TEXT)], offset_s=0.0, max_chars_per_line=_MAX_CHARS)
    cues_shifted = monologue_cues([_item(_LONG_TEXT)], offset_s=10.0, max_chars_per_line=_MAX_CHARS)
    assert len(cues0) == len(cues_shifted)
    for c0, cs in zip(cues0, cues_shifted):
        assert cs.text == c0.text
        # 浮点：offset_s 在切分前加到 start_s/end_s 上再参与逐字比例插值，
        # 与「切分后整体平移」相比会有 1e-9 量级的浮点噪声，不是真实偏差。
        assert cs.start_s == pytest.approx(c0.start_s + 10.0, abs=1e-6)
        assert cs.end_s == pytest.approx(c0.end_s + 10.0, abs=1e-6)


def test_multiple_items_get_distinct_utterance_id_prefixes():
    items = [_item("我不会认输", start_s=1.0, duration_s=2.0), _item(_LONG_TEXT, start_s=20.0, duration_s=8.4)]
    cues = monologue_cues(items, offset_s=0.0, max_chars_per_line=_MAX_CHARS)
    assert cues[0].utterance_id == "MONO00"
    assert all(c.utterance_id.startswith("MONO01-") for c in cues[1:])


# ---------------------------------------------------------------------------
# 烧录路径（monologue_burn）与下载字幕轨路径（subtitle_shift）必须共用同一个
# 切分实现——不能各自再造一份，否则两处观众看到的独白文案会不一致。
# ---------------------------------------------------------------------------

def test_burn_and_shift_modules_import_the_same_function_object():
    assert monologue_burn.monologue_cues is monologue_cues
    assert subtitle_shift.monologue_cues is monologue_cues


def test_burn_and_shift_produce_identical_pieces_for_same_timeline():
    """两条路径在同一条时间轴（无片头预告偏移）上必须切出完全一致的片段。"""
    items = [_item(_LONG_TEXT)]
    burn_side = monologue_cues(items, offset_s=0.0, max_chars_per_line=_MAX_CHARS)
    shift_side = monologue_cues(items, offset_s=0.0, max_chars_per_line=_MAX_CHARS)
    assert burn_side == shift_side
