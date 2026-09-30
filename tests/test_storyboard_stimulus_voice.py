"""P0-G 间接转述刺激必须出声：认领机制（按 stimulus_beat_id，独立于
storyboard_beat_causality 按 turn.beat_id 的认领线）、逐字子串判据、
StimulusVoiceSoftCheck 语义重试-降级、advisory 文案。写法与
tests/test_storyboard_beat_causality.py 同构。
"""
from __future__ import annotations

from app.production.storyboard_beat_sheet_schemas import _AiEmotionalTurn
from app.production.storyboard_stimulus_voice import (
    StimulusVoiceSoftCheck,
    segment_advisories,
    segment_rule_text,
    voice_claim_moments,
    voice_missing_errors,
)
from app.schemas.segment_identity import SegmentDialogue


def _turn(**overrides):
    defaults = dict(
        beat_id="B2", turn_kind="emotional_reaction", turn_evidence_quote="她一时语塞",
        stimulus_beat_id="B1", stimulus_evidence_quote="问她是不是又没睡好",
        stimulus_missing_reason="", stimulus_needs_voice=True,
    )
    defaults.update(overrides)
    return _AiEmotionalTurn(**defaults)


def _narration(text: str, *, source_segment_index: int = 1) -> SegmentDialogue:
    return SegmentDialogue(
        speaker_identity_id="旁白", line=text, source_segment_index=source_segment_index,
        delivery="offscreen_voice", delivery_kind="narration",
    )


def _spoken(text: str, *, source_segment_index: int = 1) -> SegmentDialogue:
    return SegmentDialogue(
        speaker_identity_id="id_a", line=text, source_segment_index=source_segment_index,
        delivery="spoken_dialogue", delivery_kind="spoken_dialogue",
    )


# ---------------------------------------------------------------------------
# voice_claim_moments：按 stimulus_beat_id 认领，只认领一次，与 turn.beat_id 无关
# ---------------------------------------------------------------------------

def test_claims_turn_whose_stimulus_is_in_this_segment():
    covered: set[str] = set()
    claimed = voice_claim_moments(["B1"], [_turn()], covered)
    assert claimed == [_turn()]
    assert covered == {"B2"}


def test_ignores_turn_not_marked_needs_voice():
    covered: set[str] = set()
    claimed = voice_claim_moments(["B1"], [_turn(stimulus_needs_voice=False)], covered)
    assert claimed == []
    assert covered == set()


def test_ignores_turn_whose_stimulus_is_in_a_different_segment():
    covered: set[str] = set()
    claimed = voice_claim_moments(["B_OTHER"], [_turn()], covered)
    assert claimed == []
    assert covered == set()


def test_claims_only_once_even_if_segment_repeats():
    """容量拆分后续段完整继承 beat_ids——同一份提名不能被反复认领。"""
    turns = [_turn()]
    covered: set[str] = set()
    first = voice_claim_moments(["B1"], turns, covered)
    assert first == turns
    second = voice_claim_moments(["B1"], turns, covered)
    assert second == []


def test_claim_line_is_independent_of_turn_beat_id_claim():
    """认领键是 turn.beat_id 而不是 stimulus_beat_id，即便刺激段与转折段不同，
    仍能被"刺激所在段"正确认领——这是与 storyboard_beat_causality.
    moments_for_segment（按 turn.beat_id 认领）刻意不同的地方。"""
    turn = _turn(beat_id="B_LATER", stimulus_beat_id="B_EARLIER")
    covered: set[str] = set()
    claimed = voice_claim_moments(["B_EARLIER"], [turn], covered)
    assert claimed == [turn]
    # 转折自己所在的段（B_LATER）不该再认领一次它的"出声"义务。
    assert voice_claim_moments(["B_LATER"], [turn], covered) == []


# ---------------------------------------------------------------------------
# segment_rule_text：正面陈述，带原文与容量提醒
# ---------------------------------------------------------------------------

def test_segment_rule_text_mentions_quote_narration_and_capacity():
    rules = segment_rule_text([_turn()])
    assert len(rules) == 1
    assert "问她是不是又没睡好" in rules[0]
    assert "delivery_kind=narration" in rules[0]
    assert "她一时语塞" in rules[0]
    assert "15 秒口播容量" in rules[0]


def test_segment_rule_text_empty_when_no_claims():
    assert segment_rule_text([]) == []


# ---------------------------------------------------------------------------
# voice_missing_errors / _dialogue_voices_quote：逐字连续子串判据
# ---------------------------------------------------------------------------

def test_missing_when_no_narration_line_at_all():
    errors = voice_missing_errors([_turn()], [_spoken("她一时语塞。")])
    assert len(errors) == 1
    assert "问她是不是又没睡好" in errors[0]
    assert "stimulus_needs_voice" in errors[0]


def test_passes_when_narration_line_equals_quote_exactly():
    errors = voice_missing_errors([_turn()], [_narration("问她是不是又没睡好")])
    assert errors == []


def test_passes_when_narration_line_is_a_continuous_excerpt():
    """「或其中连续一截」：旁白只取整句里连续的一段也算数。"""
    errors = voice_missing_errors([_turn()], [_narration("是不是又没睡好")])
    assert errors == []


def test_reports_when_narration_line_paraphrases_instead_of_quoting():
    """改写而非逐字取用——不满足"逐字来自原文"。"""
    errors = voice_missing_errors([_turn()], [_narration("他关心她昨晚睡得好不好")])
    assert len(errors) == 1


def test_ignores_non_narration_lines_even_if_text_matches():
    """spoken_dialogue/offscreen_dialogue 都不算——必须是 delivery_kind=narration。"""
    errors = voice_missing_errors([_turn()], [_spoken("问她是不是又没睡好")])
    assert len(errors) == 1


def test_no_problems_when_no_turns_claimed_here():
    assert voice_missing_errors([], [_spoken("无关台词")]) == []


# ---------------------------------------------------------------------------
# StimulusVoiceSoftCheck：前几次打回、最后一次放行
# ---------------------------------------------------------------------------

def test_soft_check_blocks_first_attempts_then_warns_on_last():
    check = StimulusVoiceSoftCheck(hard_attempts=2, segment_no=1)
    turns_here = [_turn()]
    unvoiced_dialogue = [_spoken("她一时语塞。")]
    assert check.filter(turns_here, unvoiced_dialogue) != [], "第 1 次（calls=1）应打回"
    assert check.filter(turns_here, unvoiced_dialogue) != [], "第 2 次（calls=2）应打回"
    assert check.filter(turns_here, unvoiced_dialogue) == [], "第 3 次（calls=3 > hard_attempts=2）应降级为放行"


def test_soft_check_never_errors_when_already_voiced():
    check = StimulusVoiceSoftCheck(hard_attempts=2, segment_no=1)
    voiced_dialogue = [_narration("问她是不是又没睡好")]
    assert check.filter([_turn()], voiced_dialogue) == []
    assert check.filter([_turn()], voiced_dialogue) == []


# ---------------------------------------------------------------------------
# segment_advisories：可见告警，独立于 SoftCheck 的内部尝试计数重算
# ---------------------------------------------------------------------------

def test_advisories_empty_when_voiced():
    assert segment_advisories([_turn()], [_narration("问她是不是又没睡好")]) == []


def test_advisories_flag_unvoiced_stimulus_with_visible_tag():
    advisories = segment_advisories([_turn()], [_spoken("她一时语塞。")])
    assert len(advisories) == 1
    assert "STORYBOARD_PACK_STIMULUS_VOICE_MISSING" in advisories[0]
    assert "未拦截" in advisories[0]
    assert "问她是不是又没睡好" in advisories[0]


def test_advisories_recompute_independent_of_soft_check_attempt_count():
    """即便 SoftCheck 已经用尽重试放行，advisory 仍要在最终产物上独立发现问题
    （不依赖 SoftCheck 的内部状态）——这是"放行分支必须是产品里的可见信号"的
    落点。"""
    check = StimulusVoiceSoftCheck(hard_attempts=1, segment_no=1)
    unvoiced_dialogue = [_spoken("她一时语塞。")]
    check.filter([_turn()], unvoiced_dialogue)  # calls=1，仍打回
    assert check.filter([_turn()], unvoiced_dialogue) == []  # calls=2，放行
    # 最终产物仍是这份未出声的 dialogue，advisory 独立重算，不参考 check 的状态。
    assert segment_advisories([_turn()], unvoiced_dialogue) != []
