"""WS2-2d：分镜段落台词与后段必保台词冲突的确定性修补
（app.production.storyboard_dialogue_repeat_repair）。

真实故障形态（见 storyboard_dialogue_repeat.py docstring）：EP1 重跑实测，
段 4 写「跟我去公司，别出声。」，是段 5 必保台词「算了，跟我去公司当社畜
吧……绝对不能出声，知道吗？」的压缩版（覆盖率 0.71）——原来直接阻断整段
重试；这里验证改成先从本段删除冲突台词、原校验（repeated_delivery_errors）
不再报错，且合法输入/无法修补的重复保持原有行为不变。
"""
from __future__ import annotations

import pytest

from app.production.storyboard_dialogue_repeat import (
    _normalize,
    _preempts,
    repeated_delivery_errors,
)
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
        draft, [], current_segment_no=4, reserved=[_RESERVED_LINE], required_texts=[],
    )
    assert errors == []
    assert draft.dialogue == []


def test_repair_keeps_unrelated_lines_and_only_drops_the_conflicting_one():
    draft = _draft([
        ("bible:李麦麦", "跟我去公司，别出声。"),
        ("bible:李麦麦", "今天天气不错。"),
    ])
    notes = repair_preempted_dialogue(draft, [_RESERVED_LINE], current_segment_no=4, required_texts=[])
    assert len(notes) == 1
    assert "第 5 段必保台词" in notes[0]
    assert [line.line for line in draft.dialogue] == ["今天天气不错。"]


def test_legal_dialogue_with_no_reserved_conflict_is_untouched():
    """合法输入：本段台词与后段必保台词无关——修补是空操作。"""
    draft = _draft([("bible:李麦麦", "明天九点的会我不能迟到。")])
    original = list(draft.dialogue)
    notes = repair_preempted_dialogue(draft, [_RESERVED_LINE], current_segment_no=4, required_texts=[])
    assert notes == []
    assert draft.dialogue == original


def test_unrelated_repeat_across_earlier_segments_is_not_repaired_and_still_errors():
    """修不了的形态：与更早段落完全重复交付（不是"抢说后段必保台词"），
    这属于另一类失败（already_delivered），本模块不处理，仍由原校验拦截。"""
    draft = _draft([("id_a", "它居然自己选了这个项目")])
    delivered = [(2, "id_a", "它居然自己选了这个项目")]
    errors = repaired_repeated_delivery_errors(
        draft, delivered, current_segment_no=6, reserved=[], required_texts=[],
    )
    assert len(errors) == 1
    assert "第 2 段" in errors[0]
    assert len(draft.dialogue) == 1


def test_repair_result_matches_calling_original_checker_directly():
    """确认 wrapper 与原 repeated_delivery_errors 在无冲突时行为一致
    （只是多了一步先删冲突台词）。"""
    draft = _draft([("id_a", "完全不相关的一句话")])
    wrapped = repaired_repeated_delivery_errors(
        draft, [], current_segment_no=1, reserved=[_RESERVED_LINE], required_texts=[],
    )
    direct = repeated_delivery_errors(
        [], [("id_a", "完全不相关的一句话")], current_segment_no=1, reserved=[_RESERVED_LINE],
        required_texts=[],
    )
    assert wrapped == direct == []


# 2026-09-16 龙猫出爪连播第 3、4 集整集失败的真实数据：本段自己的必保原话与
# 后面某段的必保台词措辞相近，被判成「抢说」删掉，紧接着 quote_provenance_errors
# 报「必保引用 QNN 须保留完整原话」——删的正是同一轮校验要求必须在的那一句。
_REAL_DEADLOCKS = [
    ("第4集Q09", "我写了三个月了。", (6, "我写了三个月，你看了三秒。")),
    ("第3集Q23", "还有这个。每个月一笔，往外走，没名目。",
     (10, "还有一笔。每个月往外走，没名目。要写进去吗？")),
]


@pytest.mark.parametrize("tag,own_quote,reserved", _REAL_DEADLOCKS)
def test_own_required_quote_survives_even_when_it_looks_like_preemption(tag, own_quote, reserved):
    """本段自己的必保原话一律不删，哪怕它确实命中了抢说判据。

    判据本身仍然命中（下面的 _preempts 断言是独立观察点，证明这里不是因为
    「判据没命中」才没删）——保护来自归属：这句话已由台账分配给本段、带着本段
    的 quote_id，后段出现措辞相近的句子是后段的事。
    """
    assert _preempts(_normalize(own_quote), _normalize(reserved[1])), (
        f"{tag}：前提失效——抢说判据没有命中，这条用例就不再覆盖真实死锁了"
    )
    draft = _draft([("bible:阿凯", own_quote)])
    # 断言落在真判据上：这一段整体能不能过。只断言「修补没删」会漏掉死锁换位置——
    # 保留下来的那一行会被紧随其后的 _preemption_errors 判红，模型照样过不了。
    errors = repaired_repeated_delivery_errors(
        draft, [], current_segment_no=2, reserved=[reserved], required_texts=[own_quote],
    )
    assert errors == []
    assert [line.line for line in draft.dialogue] == [own_quote]


def test_preemption_of_someone_elses_reserved_line_is_still_repaired():
    """保护只覆盖本段必保原话：不在 required_texts 里的抢说行照删不误。"""
    draft = _draft([
        ("bible:阿凯", "我写了三个月了。"),
        ("bible:李麦麦", "跟我去公司，别出声。"),
    ])
    errors = repaired_repeated_delivery_errors(
        draft, [], current_segment_no=2,
        reserved=[(6, "我写了三个月，你看了三秒。"), _RESERVED_LINE],
        required_texts=["我写了三个月了。"],
    )
    assert errors == []
    assert [line.line for line in draft.dialogue] == ["我写了三个月了。"]
