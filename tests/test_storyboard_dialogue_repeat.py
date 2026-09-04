"""app.production.storyboard_dialogue_repeat——分镜台 2.4.0 跨段台词去重闸门
（真实故障，2026-09-03「橘座在上」EP1：全集最后一句内心独白在段 4、段 6 各
说一遍——阶段二逐段独立调用互相看不到彼此写过的台词）。

覆盖：归一化比较、长度阈值、跨说话人/跨内容不误伤、已交付载荷渲染、规则
文案的空/非空两种措辞。不覆盖真实供应商往返（本模块无模型调用）。
"""
from __future__ import annotations

from app.production.storyboard_dialogue_repeat import (
    already_delivered_dialogue_rule,
    already_delivered_payload,
    repeated_delivery_errors,
    reserved_dialogue_payload,
    reserved_lines_for,
)


# ---------------------------------------------------------------------------
# repeated_delivery_errors
# ---------------------------------------------------------------------------

def test_exact_repeat_by_same_speaker_is_rejected_and_names_the_prior_segment():
    delivered = [(4, "id_cat_owner", "它居然自己选了这个项目")]
    current = [("id_cat_owner", "它居然自己选了这个项目")]
    errors = repeated_delivery_errors(delivered, current, current_segment_no=6, reserved=[])
    assert len(errors) == 1
    assert "第 6 段" in errors[0] and "第 4 段" in errors[0]
    assert "它居然自己选了这个项目" in errors[0]
    assert "反应" in errors[0] or "动作" in errors[0] or "画面呼应" in errors[0]


def test_different_speaker_saying_the_same_line_is_not_flagged():
    delivered = [(4, "id_a", "我们走吧我们这就走")]
    current = [("id_b", "我们走吧我们这就走")]
    assert repeated_delivery_errors(delivered, current, current_segment_no=6, reserved=[]) == []


def test_different_content_by_same_speaker_is_not_flagged():
    delivered = [(4, "id_a", "我们走吧我们这就走")]
    current = [("id_a", "完全不同的一句话内容")]
    assert repeated_delivery_errors(delivered, current, current_segment_no=6, reserved=[]) == []


def test_short_lines_below_the_length_floor_are_never_flagged():
    """语气词/短应答（"嗯""好的"）天然会在多段重复出现，不是本闸门要拦的。"""
    delivered = [(4, "id_a", "嗯")]
    current = [("id_a", "嗯")]
    assert repeated_delivery_errors(delivered, current, current_segment_no=6, reserved=[]) == []


def test_repeat_hidden_behind_punctuation_and_spacing_is_still_caught():
    """归一化去标点空白、casefold，排版差异不能掩盖真实重复。"""
    delivered = [(4, "id_a", "我们走吧，这就走！")]
    current = [("id_a", "我们走吧这就走")]
    errors = repeated_delivery_errors(delivered, current, current_segment_no=6, reserved=[])
    assert len(errors) == 1


def test_multiple_delivered_segments_report_the_correct_earlier_segment_number():
    delivered = [
        (2, "id_a", "第二段说过的一句长台词"),
        (4, "id_a", "猫忽然跳上了桌子这一幕"),
    ]
    current = [("id_a", "猫忽然跳上了桌子这一幕")]
    errors = repeated_delivery_errors(delivered, current, current_segment_no=7, reserved=[])
    assert "第 4 段" in errors[0]
    assert "第 2 段" not in errors[0]


def test_empty_delivered_history_never_flags_the_first_segment():
    current = [("id_a", "这是本集第一段的第一句台词")]
    assert repeated_delivery_errors([], current, current_segment_no=1, reserved=[]) == []


def test_multiple_repeats_in_one_segment_each_produce_their_own_error():
    delivered = [
        (1, "id_a", "第一句已经说过的长台词"),
        (1, "id_b", "第二句已经说过的长台词"),
    ]
    current = [
        ("id_a", "第一句已经说过的长台词"),
        ("id_b", "第二句已经说过的长台词"),
        ("id_a", "这句是全新的没说过"),
    ]
    errors = repeated_delivery_errors(delivered, current, current_segment_no=2, reserved=[])
    assert len(errors) == 2


# ---------------------------------------------------------------------------
# already_delivered_payload / already_delivered_dialogue_rule
# ---------------------------------------------------------------------------

def test_already_delivered_payload_renders_segment_no_speaker_and_line():
    delivered = [(3, "id_a", "台词甲"), (5, "id_b", "台词乙")]
    payload = already_delivered_payload(delivered)
    assert payload == [
        {"segment_no": 3, "speaker_identity_id": "id_a", "line": "台词甲"},
        {"segment_no": 5, "speaker_identity_id": "id_b", "line": "台词乙"},
    ]


def test_already_delivered_payload_empty_when_no_history():
    assert already_delivered_payload([]) == []


def test_already_delivered_dialogue_rule_states_emptiness_honestly_for_first_segment():
    rule = already_delivered_dialogue_rule([], [])
    assert "为空" in rule
    assert "不需要比对" in rule


def test_already_delivered_dialogue_rule_points_at_the_payload_field_when_non_empty():
    rule = already_delivered_dialogue_rule([(1, "id_a", "台词")], [])
    assert "already_delivered_dialogue" in rule
    assert "不要原样再说一遍" in rule


def test_preempting_a_later_segment_required_line_is_blocked_even_when_paraphrased() -> None:
    """EP1 重跑实测：段 4 写「跟我去公司，别出声。」，它是段 5 必保台词的压缩版。"""
    reserved = [(5, "算了，跟我去公司当社畜吧，记住了，到了公司你就是个“没有感情的摆件”，绝对不能出声，知道吗？")]
    errors = repeated_delivery_errors(
        [], [("bible:李麦麦", "跟我去公司，别出声。")], current_segment_no=4, reserved=reserved,
    )
    assert len(errors) == 1 and "第 5 段必保台词" in errors[0] and "改写" in errors[0]


def test_short_or_unrelated_lines_do_not_count_as_preemption() -> None:
    reserved = [(5, "算了，跟我去公司当社畜吧，记住了，到了公司你就是个“没有感情的摆件”，绝对不能出声，知道吗？")]
    errors = repeated_delivery_errors(
        [], [("bible:李麦麦", "好的。"), ("bible:李麦麦", "明天九点的会我不能迟到。")],
        current_segment_no=4, reserved=reserved,
    )
    assert errors == []


def test_reserved_lines_for_only_returns_later_segments() -> None:
    required = {3: [{"text": "早说的"}], 4: [{"text": "本段的"}], 6: [{"text": "后面的"}], 5: [{"text": ""}]}
    assert reserved_lines_for(required, 4) == [(6, "后面的")]
    assert reserved_dialogue_payload([(6, "后面的")]) == [{"segment_no": 6, "line": "后面的"}]
    assert "reserved_dialogue 列出了" in already_delivered_dialogue_rule([], [(6, "后面的")])
