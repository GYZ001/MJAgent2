"""P0 单镜动作密度上限：正面规则文案、阻断判据、ActionDensitySoftCheck 语义
重试-降级、advisory 文案、旧数据兼容（无 shot_action_beats 字段时默认空列表）。
写法与 tests/test_storyboard_stimulus_voice.py 同构。
"""
from __future__ import annotations

from app.production.storyboard_action_density import (
    MAX_KEY_ACTIONS_PER_SHOT,
    ActionDensitySoftCheck,
    ShotActionBeats,
    action_density_errors,
    segment_advisories,
    shot_action_beats_rule,
    undeclared_shot_errors,
)


def _beats(shot_no: int, *actions: str) -> ShotActionBeats:
    return ShotActionBeats(shot_no=shot_no, key_actions=list(actions))


# ---------------------------------------------------------------------------
# shot_action_beats_rule：正面陈述，不写禁令
# ---------------------------------------------------------------------------

def test_rule_states_the_limit_and_the_positive_way_out():
    rule = shot_action_beats_rule()
    assert str(MAX_KEY_ACTIONS_PER_SHOT) in rule
    assert "拆成更多镜头" in rule
    assert "硬切省略" in rule
    assert "用硬切直接呈现换好后的样子" in rule


def test_rule_respects_custom_max():
    assert "3" in shot_action_beats_rule(max_per_shot=3)


# ---------------------------------------------------------------------------
# action_density_errors：阻断判据，只数模型自报条数
# ---------------------------------------------------------------------------

def test_no_errors_when_every_shot_within_limit():
    beats = [_beats(1, "睡着", "被火花惊醒"), _beats(2, "冲去拔插头")]
    assert action_density_errors(beats) == []


def test_flags_shot_exceeding_the_limit():
    beats = [_beats(1, "推门", "开灯", "跟进", "看照片")]
    errors = action_density_errors(beats)
    assert len(errors) == 1
    assert "镜头 1" in errors[0]
    assert "4 个关键动作" in errors[0]
    assert "推门、开灯、跟进、看照片" in errors[0]
    assert f"每镜 {MAX_KEY_ACTIONS_PER_SHOT} 个的上限" in errors[0]


def test_only_flags_the_offending_shots_not_the_whole_segment():
    beats = [_beats(1, "走三步"), _beats(2, "扣纽扣", "直身", "示意")]
    errors = action_density_errors(beats)
    assert len(errors) == 1
    assert "镜头 2" in errors[0]


def test_empty_declaration_produces_no_errors():
    """旧数据/模型未申报时是空列表——不是误报的来源，也不是"必须申报"闸门本身。"""
    assert action_density_errors([]) == []


# ---------------------------------------------------------------------------
# ActionDensitySoftCheck：前 hard_attempts 次打回，之后放行
# ---------------------------------------------------------------------------

def test_soft_check_blocks_first_attempts_then_warns_on_last():
    check = ActionDensitySoftCheck(hard_attempts=2, segment_no=1)
    over_limit = [_beats(1, "推门", "开灯", "跟进")]
    assert check.filter(over_limit, shot_count=1) != [], "第 1 次（calls=1）应打回"
    assert check.filter(over_limit, shot_count=1) != [], "第 2 次（calls=2）应打回"
    assert check.filter(over_limit, shot_count=1) == [], "第 3 次（calls=3 > hard_attempts=2）应降级为放行"


def test_soft_check_never_errors_when_within_limit():
    check = ActionDensitySoftCheck(hard_attempts=2, segment_no=1)
    within_limit = [_beats(1, "推门", "开灯")]
    assert check.filter(within_limit, shot_count=1) == []
    assert check.filter(within_limit, shot_count=1) == []


def test_soft_check_blocks_undeclared_shot_same_as_over_limit():
    """未申报（缺镜/空清单）与超限走同一套 hard_attempts 重试-降级节奏——给
    模型真实机会补申报，不是一上来就判死，也不是完全不检查。"""
    check = ActionDensitySoftCheck(hard_attempts=1, segment_no=1)
    undeclared: list[ShotActionBeats] = []
    assert check.filter(undeclared, shot_count=2) != [], "第 1 次（calls=1）应打回"
    assert check.filter(undeclared, shot_count=2) == [], "第 2 次（calls=2 > hard_attempts=1）应降级为放行"


# ---------------------------------------------------------------------------
# undeclared_shot_errors：覆盖度判据——缺镜/空清单与「未检查」同等对待
# ---------------------------------------------------------------------------

def test_undeclared_errors_empty_when_every_shot_covered():
    beats = [_beats(1, "推门"), _beats(2, "开灯")]
    assert undeclared_shot_errors(beats, shot_count=2) == []


def test_undeclared_errors_flags_completely_empty_list():
    assert undeclared_shot_errors([], shot_count=2) != []


def test_undeclared_errors_flags_missing_shot_no():
    beats = [_beats(1, "推门")]
    errors = undeclared_shot_errors(beats, shot_count=2)
    assert len(errors) == 1
    assert "2" in errors[0]


def test_undeclared_errors_treats_empty_key_actions_as_undeclared():
    """申报了 shot_no 但 key_actions 为空——与完全没申报同等对待，不能靠
    「有申报这一条但内容是空的」绕过覆盖度判据。"""
    beats = [_beats(1, "推门"), ShotActionBeats(shot_no=2, key_actions=[])]
    errors = undeclared_shot_errors(beats, shot_count=2)
    assert len(errors) == 1
    assert "2" in errors[0]


# ---------------------------------------------------------------------------
# segment_advisories：可见告警，独立于 SoftCheck 内部尝试计数重算
# ---------------------------------------------------------------------------

def test_advisories_empty_when_within_limit():
    assert segment_advisories([_beats(1, "推门", "开灯")], shot_count=1) == []


def test_advisories_flag_over_limit_with_visible_tag():
    advisories = segment_advisories([_beats(1, "推门", "开灯", "跟进")], shot_count=1)
    assert len(advisories) == 1
    assert "STORYBOARD_PACK_ACTION_DENSITY" in advisories[0]
    assert "未拦截" in advisories[0]


def test_advisories_flag_undeclared_with_distinct_visible_tag():
    """未申报不能与「超限」共用同一个标记——必须能区分「真的检查过且合规」
    和「模型压根没申报，没被核验过」（CLAUDE.md「空集合不等于无需检查」）。"""
    advisories = segment_advisories([], shot_count=1)
    assert len(advisories) == 1
    assert "STORYBOARD_PACK_ACTION_DENSITY_UNDECLARED" in advisories[0]
    assert "未拦截" in advisories[0]


def test_advisories_recompute_independent_of_soft_check_attempt_count():
    """SoftCheck 用尽重试放行后，advisory 仍要在最终产物上独立发现超限——不依赖
    SoftCheck 的内部状态（「放行分支必须是产品里的可见信号」）。"""
    check = ActionDensitySoftCheck(hard_attempts=1, segment_no=1)
    over_limit = [_beats(1, "推门", "开灯", "跟进")]
    check.filter(over_limit, shot_count=1)  # calls=1，仍打回
    assert check.filter(over_limit, shot_count=1) == []  # calls=2，放行
    assert segment_advisories(over_limit, shot_count=1) != []


# ---------------------------------------------------------------------------
# 旧数据兼容：ShotActionBeats 的 key_actions 默认空列表
# ---------------------------------------------------------------------------

def test_shot_action_beats_key_actions_defaults_to_empty_list():
    assert ShotActionBeats(shot_no=1).key_actions == []
