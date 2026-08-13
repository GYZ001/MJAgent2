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
from app.validators import prefer_default_shot_durations, validate_storyboard
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


def test_duration_normalization_retimes_coherent_spoken_timeline() -> None:
    shot = _shot_with_line("走吧。", duration_s=10)
    shot.audio_timeline[0].end_s = 8.0

    changes = prefer_default_shot_durations(Storyboard(episode_no=1, shots=[shot]))

    assert shot.duration_s == 5
    assert max(
        item.end_s for item in shot.audio_timeline if item.type == "spoken_dialogue"
    ) <= 5
    assert not any(
        issue.code == "SPOKEN_TIMELINE_OUT_OF_RANGE"
        for issue in validate_spoken_contract(shot)
    )
    assert any(
        item.get("reason") == "retimed_audio_after_duration_normalization"
        for item in changes
    )


def test_duration_normalization_preserves_human_reviewed_duration() -> None:
    from app.renderability import HUMAN_DURATION_REVIEW_TAG

    shot = _shot_with_line("走吧。", duration_s=10)
    shot.risk_tags = [HUMAN_DURATION_REVIEW_TAG]

    changes = prefer_default_shot_durations(Storyboard(episode_no=1, shots=[shot]))

    assert shot.duration_s == 10
    assert any(
        item.get("reason") == "human_duration_review_preserved"
        for item in changes
    )
    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]), _bible(), target_duration_s=50,
    )
    assert not any("duration_s=10 过长" in error for error in errors)


def test_duration_normalization_does_not_hide_spoken_contract_conflict() -> None:
    shot = _shot_with_line("甲。", duration_s=10)
    shot.audio_timeline[0].text = "乙。"
    original_timeline_text = shot.audio_timeline[0].text

    prefer_default_shot_durations(Storyboard(episode_no=1, shots=[shot]))

    assert shot.audio_timeline[0].text == original_timeline_text
    assert any(
        issue.code == "SPOKEN_CONTRACT_CONFLICT"
        for issue in validate_spoken_contract(shot)
    )


def test_declared_onscreen_speaker_missing_from_visible_is_blocked() -> None:
    """shot_no=83 复现：画内声明不得被静默改写成画外音。

    宗门绿袍修士2 不在 characters_visible，dialogues 与 audio_timeline 的原始
    delivery/type 都写着 spoken_dialogue，就是对画内开口和口型的明确
    声明。如果 visible 漏了说话人，只能补可见身份或由上游明确
    改为 offscreen_voice；不能由同步器为了自洽而猜测。
    """
    line = "许师姐已经到了凝气第七层，被掌教赐了风幡，没到筑基便可飞行，让人羡慕。"
    shot = _compact_shot(83)
    shot.duration_s = 9
    shot.characters = ["孟浩", "王有材"]
    shot.characters_visible = ["孟浩", "王有材"]
    shot.dialogues = [
        Dialogue(speaker="宗门绿袍修士2", line=line, emotion="平静带艳羡", delivery="spoken_dialogue")
    ]
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.0, end_s=8.6, type="spoken_dialogue",
            speaker_id="宗门绿袍修士2", text=line, lip_sync=True, emotion="平静带艳羡",
        )
    ]

    # 两侧文字一致，但结构化可见身份仍必须硬拦截。
    assert not any(
        i.code == "SPOKEN_CONTRACT_CONFLICT" for i in validate_spoken_contract(shot)
    )
    assert any(
        "SPOKEN_VISIBLE_SPEAKER_INVALID" in error
        for error in spoken_contract_coherence_errors(shot)
    )

    # 同步侧不改写业务语义，仍然返回 blocker。
    result = synchronize_spoken_contract(shot)
    assert result.status == "coherent"
    assert shot.spoken_contract_status == "coherent"
    assert not result.ok
    assert shot.audio_timeline[0].type == "spoken_dialogue"
    assert shot.audio_timeline[0].lip_sync is True
    assert shot.dialogues[0].delivery == "spoken_dialogue"
    assert shot.audio_timeline[0].text == line
    assert shot.dialogues[0].line == line


def test_explicit_offscreen_speaker_not_visible_remains_valid() -> None:
    line = "许师姐已经到了凝气第七层。"
    shot = _compact_shot(83)
    shot.duration_s = 6
    shot.characters = ["孟浩", "王有材"]
    shot.characters_visible = ["孟浩", "王有材"]
    shot.dialogues = [Dialogue(
        speaker="宗门绿袍修士2", line=line, delivery="offscreen_voice",
    )]
    shot.audio_timeline = [AudioTimelineItem(
        start_s=0.0, end_s=4.0, type="offscreen_voice",
        speaker_id="宗门绿袍修士2", text=line, lip_sync=False,
    )]

    assert spoken_contract_coherence_errors(shot) == []
    result = synchronize_spoken_contract(shot)
    assert result.ok
    assert shot.audio_timeline[0].type == "offscreen_voice"
    assert shot.dialogues[0].delivery == "offscreen_voice"


def test_shot83_onscreen_claim_cannot_pass_episode_gate_after_prompt_compile() -> None:
    """Prompt compilation is not authority; the full deterministic gate still wins."""
    from app.compiler import compile_prompt

    shot = _compact_shot(83)
    shot.duration_s = 6
    shot.characters = ["萧薰儿"]
    shot.characters_visible = ["萧薰儿"]
    shot.dialogues = [Dialogue(
        speaker="萧炎", line="我会追上来。", delivery="spoken_dialogue",
    )]
    shot.audio_timeline = [AudioTimelineItem(
        start_s=0.0, end_s=3.0, type="spoken_dialogue",
        speaker_id="萧炎", text="我会追上来。", lip_sync=True,
    )]

    # The visual prompt can be compiled from syntactically complete data.  That
    # must not be confused with identity/action/episode authority acceptance.
    assert compile_prompt(shot, _bible())
    sync = synchronize_spoken_contract(shot)
    assert not sync.ok
    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]),
        _bible(),
        6,
    )
    assert any("SPOKEN_VISIBLE_SPEAKER_INVALID" in error for error in errors)
    assert shot.dialogues[0].delivery == "spoken_dialogue"


def test_visible_speaker_stays_spoken_dialogue() -> None:
    """可见说话人不受影响：仍是 spoken_dialogue，两侧不冲突。"""
    line = "我一定会追上你。"
    shot = _compact_shot(84)
    shot.duration_s = 5
    shot.characters = ["孟浩"]
    shot.characters_visible = ["孟浩"]
    shot.dialogues = [Dialogue(speaker="孟浩", line=line, emotion="坚定", delivery="spoken_dialogue")]
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.3, end_s=3.0, type="spoken_dialogue",
            speaker_id="孟浩", text=line, lip_sync=True, emotion="坚定",
        )
    ]
    assert not any(
        i.code == "SPOKEN_CONTRACT_CONFLICT" for i in validate_spoken_contract(shot)
    )
    result = synchronize_spoken_contract(shot)
    assert result.status == "coherent"
    assert shot.audio_timeline[0].type == "spoken_dialogue"
    assert shot.audio_timeline[0].lip_sync is True
    assert shot.dialogues[0].delivery == "spoken_dialogue"


def test_genuine_text_divergence_still_conflicts() -> None:
    """收敛发声方式不能掩盖真实分歧：同一说话人、不同文本仍必须报冲突。"""
    shot = _compact_shot(85)
    shot.duration_s = 10
    shot.characters = ["孟浩"]
    shot.characters_visible = ["孟浩"]
    shot.dialogues = [Dialogue(speaker="孟浩", line="甲。", emotion="平静", delivery="spoken_dialogue")]
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.3, end_s=2.0, type="spoken_dialogue",
            speaker_id="孟浩", text="乙。", lip_sync=True,
        )
    ]
    assert any(
        i.code == "SPOKEN_CONTRACT_CONFLICT" for i in validate_spoken_contract(shot)
    )


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
