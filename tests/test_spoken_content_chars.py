"""口播去标点 + 禁止旁白合同。"""
from __future__ import annotations

from app.continuity import (
    content_char_count,
    max_speech_chars,
    speech_capacity_errors,
    spoken_chars_from_shot,
)
from app.schemas import AudioTimelineItem, Dialogue, Shot, Storyboard
from app.validators import strip_all_narration, validate_storyboard
from tests.test_validators import _bible, _compact_shot


def test_content_char_count_skips_punctuation() -> None:
    assert content_char_count("下一个，萧媚！") == 5
    assert content_char_count("萧媚，斗之气，七段！级别：高级！") == 11
    assert content_char_count("下一个，萧媚！") + content_char_count("萧媚，斗之气，七段！级别：高级！") == 16


def test_shot4_style_dialogue_fits_five_seconds() -> None:
    shot = _compact_shot(4)
    shot.duration_s = 5
    shot.characters = ["测验员", "萧炎"]
    shot.dialogues = [
        Dialogue(speaker="测验员", line="下一个，萧媚！", emotion="平静"),
        Dialogue(speaker="测验员", line="萧媚，斗之气，七段！级别：高级！", emotion="平静"),
    ]
    shot.action_desc = (
        "测验员当众点名萧媚上前，萧炎立在队伍末尾听着宣告，碑面亮起七段光芒"
    )
    shot.narration = ""
    assert spoken_chars_from_shot(shot) == 16
    assert max_speech_chars(5) >= 18
    assert speech_capacity_errors(shot) == []
    errors = validate_storyboard(Storyboard(episode_no=1, shots=[shot]), _bible(), target_duration_s=50)
    assert not any("口播上限" in e for e in errors), errors
    assert not any("禁止旁白" in e or "narration 非空" in e for e in errors), errors


def test_narration_excluded_from_spoken_count_and_banned() -> None:
    shot = _compact_shot(1)
    shot.dialogues = [Dialogue(speaker="萧炎", line="三段。", emotion="平静")]
    shot.narration = "同族少年，天壤之别"
    assert spoken_chars_from_shot(shot) == content_char_count("三段")
    errors = validate_storyboard(Storyboard(episode_no=1, shots=[shot]), _bible(), target_duration_s=50)
    assert any("禁止旁白" in e or "narration 非空" in e for e in errors), errors


def test_timeline_narration_not_counted_and_stripped() -> None:
    shot = _compact_shot(1)
    shot.dialogues = [Dialogue(speaker="萧炎", line="下一个，萧媚！", emotion="平静")]
    shot.audio_timeline = [
        AudioTimelineItem(start_s=0.3, end_s=1.5, type="spoken_dialogue",
                          speaker_id="萧炎", text="下一个，萧媚！", lip_sync=True),
        AudioTimelineItem(start_s=1.5, end_s=3.0, type="narration",
                          speaker_id="旁白", text="同族少年，天壤之别", lip_sync=False),
    ]
    assert spoken_chars_from_shot(shot) == 5
    strip_all_narration(Storyboard(episode_no=1, shots=[shot]))
    assert all(item.type != "narration" for item in shot.audio_timeline)
    assert spoken_chars_from_shot(shot) == 5


def test_speech_capacity_error_mentions_dialogue_not_narration() -> None:
    shot = _compact_shot(1)
    shot.duration_s = 5
    shot.dialogues = [
        Dialogue(
            speaker="萧炎",
            line="这扇门背后藏着我们寻找多年的真相现在必须立刻进去确认到底发生了什么",
            emotion="坚定",
        )
    ]
    errs = speech_capacity_errors(shot)
    assert any("台词纯文字" in e and "不计标点" in e for e in errs), errs
    assert not any("旁白" in e for e in errs), errs
