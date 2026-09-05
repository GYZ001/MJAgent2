"""WS2-2d：分镜段落台词与后段必保台词冲突的确定性修补
（app.production.storyboard_dialogue_repeat_repair）。

真实故障形态（见 storyboard_dialogue_repeat.py docstring）：EP1 重跑实测，
段 4 写「跟我去公司，别出声。」，是段 5 必保台词「算了，跟我去公司当社畜
吧……绝对不能出声，知道吗？」的压缩版（覆盖率 0.71）——原来直接阻断整段
重试；这里验证改成先从本段删除冲突台词、原校验（repeated_delivery_errors）
不再报错，且合法输入/无法修补的重复保持原有行为不变。
"""
from __future__ import annotations

from app.production.storyboard_dialogue_repeat import repeated_delivery_errors
from app.production.storyboard_dialogue_repeat_repair import (
    repair_preempted_dialogue,
    repaired_repeated_delivery_errors,
)
from app.production.storyboard_pack import _AiDialogueLine, _AiStoryboardSegmentDraft

_RESERVED_LINE = (
    5,
    "算了，跟我去公司当社畜吧，记住了，到了公司你就是个“没有感情的摆件”，绝对不能出声，知道吗？",
)


def _draft(lines: list[tuple[str, str]]) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(
        prompt_text="占位提示词",
        shot_count=2,
        dialogue=[
            _AiDialogueLine(speaker_identity_id=speaker, line=line, source_segment_index=1)
            for speaker, line in lines
        ],
    )


def test_real_failure_shape_is_repaired_and_original_check_passes():
    """真实故障形态：段 4 抢说了段 5 必保台词的压缩版——修补后原校验通过，
    该行从本段 dialogue 中被删除。"""
    draft = _draft([("bible:李麦麦", "跟我去公司，别出声。")])
    errors = repaired_repeated_delivery_errors(
        draft, [], current_segment_no=4, reserved=[_RESERVED_LINE],
    )
    assert errors == []
    assert draft.dialogue == []


def test_repair_keeps_unrelated_lines_and_only_drops_the_conflicting_one():
    draft = _draft([
        ("bible:李麦麦", "跟我去公司，别出声。"),
        ("bible:李麦麦", "今天天气不错。"),
    ])
    notes = repair_preempted_dialogue(draft, [_RESERVED_LINE], current_segment_no=4)
    assert len(notes) == 1
    assert "第 5 段必保台词" in notes[0]
    assert [line.line for line in draft.dialogue] == ["今天天气不错。"]


def test_legal_dialogue_with_no_reserved_conflict_is_untouched():
    """合法输入：本段台词与后段必保台词无关——修补是空操作。"""
    draft = _draft([("bible:李麦麦", "明天九点的会我不能迟到。")])
    original = list(draft.dialogue)
    notes = repair_preempted_dialogue(draft, [_RESERVED_LINE], current_segment_no=4)
    assert notes == []
    assert draft.dialogue == original


def test_unrelated_repeat_across_earlier_segments_is_not_repaired_and_still_errors():
    """修不了的形态：与更早段落完全重复交付（不是"抢说后段必保台词"），
    这属于另一类失败（already_delivered），本模块不处理，仍由原校验拦截。"""
    draft = _draft([("id_a", "它居然自己选了这个项目")])
    delivered = [(2, "id_a", "它居然自己选了这个项目")]
    errors = repaired_repeated_delivery_errors(
        draft, delivered, current_segment_no=6, reserved=[],
    )
    assert len(errors) == 1
    assert "第 2 段" in errors[0]
    assert len(draft.dialogue) == 1


def test_repair_result_matches_calling_original_checker_directly():
    """确认 wrapper 与原 repeated_delivery_errors 在无冲突时行为一致
    （只是多了一步先删冲突台词）。"""
    draft = _draft([("id_a", "完全不相关的一句话")])
    wrapped = repaired_repeated_delivery_errors(
        draft, [], current_segment_no=1, reserved=[_RESERVED_LINE],
    )
    direct = repeated_delivery_errors(
        [], [("id_a", "完全不相关的一句话")], current_segment_no=1, reserved=[_RESERVED_LINE],
    )
    assert wrapped == direct == []
