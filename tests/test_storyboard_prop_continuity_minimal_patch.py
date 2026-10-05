"""app.domain.storyboard_ops.prop_continuity_minimal_patch -- 存量分镜「最小
修改重写」的核验/应用/类别判断（2026-10-05，见模块 docstring「为什么不能用
整段重生成修存量分镜」）。纯函数为主，不需要数据库；``propose_minimal_patch``
打桩 ``model_gateway.chat_structured``，不打真实供应商往返。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.domain.storyboard_ops import prop_continuity_minimal_patch as patch
from app.harness import model_gateway
from app.production.storyboard_prose_review import ProseViolation, _KIND_RULES


def _violation(kind: str = "prop_state_regression", quote: str = "", **kwargs) -> ProseViolation:
    return ProseViolation(kind=kind, quote=quote, **kwargs)


# ---------------------------------------------------------------------------
# is_locally_patchable / _LOCALLY_PATCHABLE_KINDS
# ---------------------------------------------------------------------------

def test_locally_patchable_kinds_cover_exactly_prose_review_kinds():
    """新增第 12 类判据时这里必须同步判断——漏判按不可局部改处理（更安全的
    一侧），但覆盖面必须与 ``_KIND_RULES`` 完全一致，不多不少。"""
    assert set(patch._LOCALLY_PATCHABLE_KINDS) == set(_KIND_RULES)


@pytest.mark.parametrize("kind", ["skin_blush", "screen_side", "prop_appearance", "prop_duplication", "repeated_transition_action", "negated_action", "prop_state_regression"])
def test_textual_kinds_are_locally_patchable(kind):
    assert patch.is_locally_patchable(kind) is True


@pytest.mark.parametrize("kind", ["action_density", "unvoiced_speech", "time_jump", "impossible_camera_move"])
def test_structural_kinds_are_not_locally_patchable(kind):
    assert patch.is_locally_patchable(kind) is False


def test_unknown_kind_defaults_to_not_patchable():
    assert patch.is_locally_patchable("not_a_real_kind") is False


# ---------------------------------------------------------------------------
# validate_replacements：模型提名、代码核验
# ---------------------------------------------------------------------------

def test_validate_accepts_a_quote_that_overlaps_the_violation_itself():
    text = "镜头1：墙根插座上插着白色插头。镜头2：她转身离开。"
    violations = [_violation(quote="墙根插座上插着白色插头")]
    accepted, rejected = patch.validate_replacements(text, [patch._PatchReplacement(quote="墙根插座上插着白色插头", replacement="插座两孔空着")], violations=violations)
    assert rejected == []
    assert len(accepted) == 1 and accepted[0].replacement == "插座两孔空着"
    assert text[accepted[0].start:accepted[0].end] == "墙根插座上插着白色插头"


def test_validate_rejects_quote_not_present_in_text():
    text = "镜头1：她转身离开。"
    accepted, rejected = patch.validate_replacements(text, [patch._PatchReplacement(quote="不存在的原文", replacement="x")], violations=[_violation(quote="她转身离开")])
    assert accepted == []
    assert rejected[0].reason == "quote 在正文里找不到或不止出现一次"


def test_validate_rejects_quote_appearing_more_than_once():
    text = "镜头1：她看了看插头。镜头2：插头还在桌上。"
    accepted, rejected = patch.validate_replacements(text, [patch._PatchReplacement(quote="插头", replacement="充电器")], violations=[_violation(quote="她看了看插头")])
    assert accepted == []
    assert "找不到或不止出现一次" in rejected[0].reason


def test_validate_rejects_quote_not_overlapping_any_violation():
    text = "镜头1：墙根插座上插着白色插头。镜头2：她转身离开。"
    violations = [_violation(quote="她转身离开")]  # 替换目标与这条违规的原文不在同一处
    accepted, rejected = patch.validate_replacements(text, [patch._PatchReplacement(quote="墙根插座上插着白色插头", replacement="插座两孔空着")], violations=violations)
    assert accepted == []
    assert rejected[0].reason == "quote 与已核验违规的原文区间不重叠"


def test_validate_rejects_empty_replacement():
    text = "镜头1：墙根插座上插着白色插头。"
    violations = [_violation(quote="墙根插座上插着白色插头")]
    accepted, rejected = patch.validate_replacements(text, [patch._PatchReplacement(quote="墙根插座上插着白色插头", replacement="   ")], violations=violations)
    assert accepted == []
    assert rejected[0].reason == "replacement 为空"


def test_validate_rejects_second_replacement_overlapping_first_accepted():
    text = "镜头1：她伸出右手拉着银色拉杆箱。"
    violations = [_violation(quote="她伸出右手拉着银色拉杆箱")]
    raw = [
        patch._PatchReplacement(quote="她伸出右手拉着银色拉杆箱", replacement="她伸出左手拉着银色拉杆箱"),
        patch._PatchReplacement(quote="右手拉着银色", replacement="左手拎着深蓝色"),  # 与上一条区间重叠
    ]
    accepted, rejected = patch.validate_replacements(text, raw, violations=violations)
    assert len(accepted) == 1 and accepted[0].quote == "她伸出右手拉着银色拉杆箱"
    assert len(rejected) == 1 and rejected[0].reason == "与另一条已接受的替换区间重叠"


def test_validate_overlap_judged_by_character_interval_not_keyword_list():
    """重叠判据必须是字符区间计算，不是关键词表——同一个「拉杆箱」字样在两个
    不相邻的位置各出现一次，区间不重叠就都该被接受。"""
    text = "镜头1：她放下拉杆箱。镜头2：他捡起拉杆箱。"
    violations = [_violation(quote="她放下拉杆箱"), _violation(quote="他捡起拉杆箱")]
    raw = [
        patch._PatchReplacement(quote="她放下拉杆箱", replacement="她放下行李箱"),
        patch._PatchReplacement(quote="他捡起拉杆箱", replacement="他捡起行李箱"),
    ]
    accepted, rejected = patch.validate_replacements(text, raw, violations=violations)
    assert rejected == [] and len(accepted) == 2


# ---------------------------------------------------------------------------
# apply_replacements：区间外文本逐字不变
# ---------------------------------------------------------------------------

def test_apply_replacements_keeps_text_outside_interval_byte_for_byte():
    text = "镜头1：墙根插座上插着白色插头。镜头2：她转身离开。"
    accepted = [patch.AcceptedReplacement(quote="墙根插座上插着白色插头", replacement="插座两孔空着，插头已被拔下放在地上", start=text.index("墙根"), end=text.index("墙根") + len("墙根插座上插着白色插头"))]
    result = patch.apply_replacements(text, accepted)
    prefix_len = accepted[0].start
    suffix = text[accepted[0].end:]
    assert result[:prefix_len] == text[:prefix_len], "替换区间之前的文本必须逐字不变"
    assert result[-len(suffix):] == suffix, "替换区间之后的文本必须逐字不变"
    assert "插座两孔空着，插头已被拔下放在地上" in result


def test_apply_replacements_applies_multiple_non_overlapping_replacements_right_to_left():
    text = "她伸出右手拉着银色拉杆箱，穿过走廊。"
    right_hand = "右手"
    silver = "银色"
    accepted = [
        patch.AcceptedReplacement(quote=right_hand, replacement="左手", start=text.index(right_hand), end=text.index(right_hand) + len(right_hand)),
        patch.AcceptedReplacement(quote=silver, replacement="深蓝色", start=text.index(silver), end=text.index(silver) + len(silver)),
    ]
    result = patch.apply_replacements(text, accepted)
    assert result == "她伸出左手拉着深蓝色拉杆箱，穿过走廊。"


# ---------------------------------------------------------------------------
# build_patch_candidate：speech_template 镜像
# ---------------------------------------------------------------------------

def test_build_patch_candidate_without_speech_template_only_returns_prompt_text():
    prompt_text = "镜头1：墙根插座上插着白色插头。"
    original = {"prompt_text": prompt_text}
    start = prompt_text.index("墙根")
    accepted = [patch.AcceptedReplacement(quote="墙根插座上插着白色插头", replacement="插座两孔空着", start=start, end=start + len("墙根插座上插着白色插头"))]
    candidate = patch.build_patch_candidate(original, accepted)
    assert candidate == {"prompt_text": "镜头1：插座两孔空着。"}


def test_build_patch_candidate_mirrors_replacement_into_speech_template():
    prompt_text = "镜头1：墙根插座上插着白色插头。镜头2：她说：你好。"
    speech_template = "镜头1：墙根插座上插着白色插头。镜头2：她说：{{speech:U01}}。"
    original = {"prompt_text": prompt_text, "speech_template": speech_template}
    span = (prompt_text.index("墙根"), prompt_text.index("墙根") + len("墙根插座上插着白色插头"))
    accepted = [patch.AcceptedReplacement(quote="墙根插座上插着白色插头", replacement="插座两孔空着", start=span[0], end=span[1])]
    candidate = patch.build_patch_candidate(original, accepted)
    assert candidate is not None
    assert candidate["prompt_text"] == "镜头1：插座两孔空着。镜头2：她说：你好。"
    assert candidate["speech_template"] == "镜头1：插座两孔空着。镜头2：她说：{{speech:U01}}。"


def test_build_patch_candidate_returns_none_when_quote_missing_from_speech_template():
    """替换目标恰好落在台词占位符被展开出来的那段文字里（speech_template 里
    还是占位符，没有这句话）——无法安全镜像，整体返回 None，不悄悄只改
    prompt_text 让保存时被原模板盖回去。"""
    prompt_text = "镜头1：她说：你好，墙根插座上插着白色插头。"
    speech_template = "镜头1：她说：{{speech:U01}}，墙根插座上插着白色插头。"
    original = {"prompt_text": prompt_text, "speech_template": speech_template}
    span = (prompt_text.index("你好"), prompt_text.index("你好") + len("你好"))
    accepted = [patch.AcceptedReplacement(quote="你好", replacement="您好", start=span[0], end=span[1])]
    assert patch.build_patch_candidate(original, accepted) is None


# ---------------------------------------------------------------------------
# continuity_memo_conflicts
# ---------------------------------------------------------------------------

def test_continuity_memo_conflicts_detects_stale_prop_state():
    memo = {"props": [{"name": "插座与插头", "location": "墙根插座上插着白色插头", "state": "插着"}]}
    accepted = [patch.AcceptedReplacement(quote="墙根插座上插着白色插头", replacement="插座两孔空着", start=0, end=10)]
    conflicts = patch.continuity_memo_conflicts(memo, accepted)
    assert len(conflicts) == 1 and "props[插座与插头].location" in conflicts[0]


def test_continuity_memo_conflicts_empty_when_memo_already_consistent():
    memo = {"props": [{"name": "插座与插头", "location": "插座两孔空着", "state": "已拔下"}]}
    accepted = [patch.AcceptedReplacement(quote="墙根插座上插着白色插头", replacement="插座两孔空着", start=0, end=10)]
    assert patch.continuity_memo_conflicts(memo, accepted) == []


def test_continuity_memo_conflicts_detects_stale_travel_direction():
    """``screen_side`` 的局部替换改了站位描述，但本段自己 ``continuity_memo.
    travel_direction`` 仍引用改之前的站位原文——必须被当成矛盾报告出来，不能
    只扫 ``layout``/``props``/``characters`` 三类（见
    ``_continuity_memo_text_fields`` docstring）。"""
    memo = {"travel_direction": "她站在画面左侧，与上一段站位相反"}
    accepted = [patch.AcceptedReplacement(quote="她站在画面左侧", replacement="她站在画面右侧", start=0, end=7)]
    conflicts = patch.continuity_memo_conflicts(memo, accepted)
    assert len(conflicts) == 1 and "travel_direction" in conflicts[0]


@pytest.mark.parametrize("field", ["time_of_day_source_quote", "layout_change_source_quote"])
def test_continuity_memo_conflicts_detects_stale_source_quote_fields(field):
    memo = {field: "她站在画面左侧时天色渐暗"}
    accepted = [patch.AcceptedReplacement(quote="她站在画面左侧", replacement="她站在画面右侧", start=0, end=7)]
    conflicts = patch.continuity_memo_conflicts(memo, accepted)
    assert len(conflicts) == 1 and field in conflicts[0]


# ---------------------------------------------------------------------------
# propose_minimal_patch
# ---------------------------------------------------------------------------

def test_propose_minimal_patch_returns_replacements_on_success(monkeypatch):
    async def chat(messages, **kwargs):
        request = json.loads(messages[1]["content"])
        assert request["segment_no"] == 2
        assert request["violations"][0]["kind"] == "prop_state_regression"
        return kwargs["model_type"](replacements=[{"quote": "墙根插座上插着白色插头", "replacement": "插座两孔空着"}])

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    result = asyncio.run(patch.propose_minimal_patch(
        episode_id="ep", segment_no=2, prompt_text="镜头1：墙根插座上插着白色插头。",
        violations=[_violation(quote="墙根插座上插着白色插头", fix="改回已拔下")],
    ))
    assert len(result) == 1 and result[0].replacement == "插座两孔空着"


def test_propose_minimal_patch_propagates_provider_failure(monkeypatch):
    """与 ``storyboard_prose_review._review_segment`` 不是同一取舍：这里的调用
    方（``rewrite_flagged_segments``）已经自带逐段 try/except 隔离，供应商
    失败必须原样冒泡，不能在这一层被吞成『没有可应用的修改』——否则『模型没
    找到能改的地方』与『请求根本没发出去』会变成同一种不可区分的结果。"""
    async def chat(messages, **kwargs):
        raise RuntimeError("模拟供应商 500")

    monkeypatch.setattr(model_gateway, "chat_structured", chat)
    with pytest.raises(RuntimeError, match="模拟供应商 500"):
        asyncio.run(patch.propose_minimal_patch(episode_id="ep", segment_no=1, prompt_text="x", violations=[_violation(quote="x")]))
