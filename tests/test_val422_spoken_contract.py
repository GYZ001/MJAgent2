"""VAL-422 P0：统一口播合同、容量去重、人工同步规则。"""
from __future__ import annotations

from app.continuity import speech_capacity_errors, spoken_contract_coherence_errors, spoken_chars_from_shot
from app.schemas import AudioTimelineItem, Dialogue, Shot
from app.spoken_contract import (
    RULE_SPOKEN_CAPACITY,
    synchronize_spoken_contract,
    validate_spoken_contract,
    spoken_text_of,
)
from app.validators import validate_storyboard
from tests.test_validators import _bible, _compact_shot
from app.schemas import Storyboard


def _shot_with_line(line: str, *, duration_s: int = 10) -> Shot:
    shot = _compact_shot(1)
    shot.duration_s = duration_s
    shot.characters = ["薰儿", "萧炎"]
    shot.dialogues = [Dialogue(speaker="薰儿", line=line, emotion="坚定")]
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.3, end_s=min(duration_s - 0.2, 8.0),
            type="spoken_dialogue", speaker_id="薰儿", text=line, lip_sync=True,
        )
    ]
    shot.narration = ""
    return shot


def test_coherent_dialogues_and_timeline_pass() -> None:
    line = "薰儿相信，你会重新站起来"
    shot = _shot_with_line(line)
    issues = validate_spoken_contract(shot)
    assert not any(i.code == "SPOKEN_CONTRACT_CONFLICT" for i in issues)
    assert spoken_text_of(shot) == line


def test_timeline_has_key_line_dialogues_missing_is_conflict() -> None:
    shot = _compact_shot(9)
    shot.duration_s = 10
    shot.characters = ["薰儿"]
    shot.dialogues = [Dialogue(speaker="薰儿", line="走吧。", emotion="平静")]
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.3, end_s=8.0, type="spoken_dialogue",
            speaker_id="薰儿",
            text="薰儿相信，你会重新站起来，取回属于你的荣耀与尊严",
            lip_sync=True,
        )
    ]
    issues = validate_spoken_contract(shot)
    conflict = [i for i in issues if i.code == "SPOKEN_CONTRACT_CONFLICT"]
    assert conflict, issues
    assert conflict[0].rule_id == "SHOT.SPOKEN.COHERENCE"
    # 不得再出现「容量按 timeline、丢词按 dialogues」的互相矛盾结论：
    # 冲突本身就是唯一业务诊断入口。
    assert conflict[0].repair_options


def test_human_edit_dialogues_rebuilds_timeline() -> None:
    shot = _shot_with_line("旧台词。")
    shot.dialogues = [Dialogue(speaker="薰儿", line="新的短句。", emotion="平静")]
    result = synchronize_spoken_contract(shot, changed_fields={"dialogues"})
    assert result.status == "coherent"
    assert any(item.text == "新的短句。" for item in shot.audio_timeline if item.type == "spoken_dialogue")
    assert spoken_text_of(shot) == "新的短句。"


def test_human_submit_conflicting_fields_returns_conflict() -> None:
    shot = _shot_with_line("甲。")
    shot.dialogues = [Dialogue(speaker="薰儿", line="乙。", emotion="平静")]
    # 两侧都改且不一致 → conflict，不静默择一
    result = synchronize_spoken_contract(
        shot, changed_fields={"dialogues", "audio_timeline"},
    )
    assert result.status == "conflict"
    assert shot.spoken_contract_status == "conflict"
    assert any(i.code == "SPOKEN_CONTRACT_CONFLICT" for i in result.issues)


def test_37_chars_10s_reports_capacity_once() -> None:
    # 37 字纯文字（不计标点）超过 10s=36 字上限
    from app.spoken_contract import content_char_count
    line = "一" * 37
    assert content_char_count(line) == 37
    shot = _shot_with_line(line, duration_s=10)
    capacity = [i for i in validate_spoken_contract(shot) if i.rule_id == RULE_SPOKEN_CAPACITY]
    assert len(capacity) == 1
    # speech_capacity_errors 与 validate_storyboard 不得重复两条
    errs = speech_capacity_errors(shot)
    assert len([e for e in errs if "口播上限" in e or "台词纯文字" in e]) == 1
    board_errs = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]), _bible(), target_duration_s=50,
    )
    capacity_msgs = [e for e in board_errs if "口播上限" in e or "台词纯文字" in e]
    assert len(capacity_msgs) == 1, capacity_msgs


def test_36_chars_10s_passes_capacity() -> None:
    from app.spoken_contract import content_char_count, max_speech_chars
    line = "一" * 36
    assert content_char_count(line) == 36
    assert max_speech_chars(10) == 36
    shot = _shot_with_line(line, duration_s=10)
    assert speech_capacity_errors(shot) == []
    assert spoken_chars_from_shot(shot) == 36


def test_key_line_only_in_source_excerpt_not_delivered() -> None:
    key = "薰儿相信，你会重新站起来，取回属于你的荣耀与尊严"
    shot = _compact_shot(9)
    shot.characters = ["薰儿"]
    shot.dialogues = [Dialogue(speaker="薰儿", line="走吧。", emotion="平静")]
    shot.source_excerpt = key
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.3, end_s=2.0, type="spoken_dialogue",
            speaker_id="薰儿", text="走吧。", lip_sync=True,
        )
    ]
    assert key not in spoken_text_of(shot)
    assert "走吧" in spoken_text_of(shot)
    # 一致性通过（两侧同为「走吧」）；关键台词覆盖由上层校验负责
    assert spoken_contract_coherence_errors(shot) == []
