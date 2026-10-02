"""按段决定要不要省略场景参考图：scene_state_matches_card 三个取值 + 缺省/
脏值的纯函数判据，覆盖《顾念长安》第 1 集出租屋被淹场景根因调查落地的改造。
"""
from __future__ import annotations

import logging

from app.video_modes.scene_state_selection import scene_reference_omission_reason

_TAG = "[STORYBOARD_SCENE_REF_OMITTED_STATE_CHANGED][未拦截]"


def test_yes_sends_reference_without_logging(caplog):
    with caplog.at_level(logging.INFO):
        result = scene_reference_omission_reason("温念的出租屋", "yes")
    assert result is None
    assert _TAG not in caplog.text


def test_no_omits_reference_and_logs_tagged_signal(caplog):
    with caplog.at_level(logging.INFO):
        result = scene_reference_omission_reason("温念的出租屋", "no")
    assert result is not None and "不一致" in result
    assert _TAG in caplog.text
    assert "温念的出租屋" in caplog.text


def test_unsure_is_conservative_and_omits_reference(caplog):
    """场景没有人物侧「头像照」那种退一步仍然诚实的降级素材，拿不准时与
    显式 no 同一分支——保守方向与人物 wardrobe_matches_default 相反。"""
    with caplog.at_level(logging.INFO):
        result = scene_reference_omission_reason("温念的出租屋", "unsure")
    assert result is not None and "未确认" in result
    assert _TAG in caplog.text


def test_missing_field_preserves_pre_existing_behavior_and_sends(caplog):
    """此字段上线前生成的存量分镜没有这个 key（读出来是空字符串，不是 pydantic
    侧的默认值 "unsure"）：那是"从未被问过"，不是"问了答不出来"，不能把新限制
    追溯套到从未评估过的旧数据上——否则一次性影响全部存量分镜，远超本次要修的
    几段，照常发送、不记省略信号。"""
    with caplog.at_level(logging.INFO):
        result = scene_reference_omission_reason("温念的出租屋", "")
    assert result is None
    assert _TAG not in caplog.text


def test_unexpected_nonempty_value_is_conservative_not_treated_as_yes(caplog):
    """空字符串之外，任何不是逐字 "yes" 的取值都不得被当成"相符"——判据只认
    正面匹配，不维护反面黑名单（CLAUDE.md「完整正面陈述，不写禁令」）。"""
    with caplog.at_level(logging.INFO):
        result = scene_reference_omission_reason("温念的出租屋", "maybe")
    assert result is not None
    assert _TAG in caplog.text
