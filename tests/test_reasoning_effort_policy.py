"""短 JSON 判定按阶段用 low 思考档位；优先级 call_meta > 运维覆盖 > 阶段表 > config 默认（2026-09-06 映射台 30 分钟根因）。"""
from __future__ import annotations

from app import config, hiagent
from app.harness.reasoning_effort_policy import LOW_EFFORT, stage_reasoning_effort


def test_stage_table_covers_the_mapping_stage_gates_and_not_the_writing_stages() -> None:
    for meta in (
        {"stage": "assess_new_character"}, {"stage": "assess_new_scene"}, {"stage": "assess_prop_appearance"},
        {"stage": "discover_character_candidates", "substage": "current_identity"},
        {"stage_key": "portraits_timeline_anchor"}, {"stage_key": "portraits_card_merge_verdict"},
        {"stage": "未解析角色候选判别"}, {"purpose": "叙述向称谓归属"},
    ):
        assert stage_reasoning_effort(meta) == LOW_EFFORT, meta
    for meta in ({"stage_key": "storyboard_pack_segment"}, {"stage_key": "storyboard_pack_beat_sheet"},
                 {"stage_key": "episode_prep_pack_event_chain"}, {}, None):
        assert stage_reasoning_effort(meta) == "", meta


def test_precedence_call_meta_then_operator_override_then_stage_table_then_default(monkeypatch) -> None:
    values: dict[str, str] = {}
    monkeypatch.setattr(hiagent, "get_setting", lambda key: values.get(key, ""))
    monkeypatch.setattr(config, "TEXT_REASONING_EFFORT", "")
    assert hiagent.text_reasoning_effort({"stage": "assess_new_scene"}) == "low"
    assert hiagent.text_reasoning_effort({"stage": "storyboard_pack_segment"}) == ""
    assert hiagent.text_reasoning_effort({"stage": "assess_new_scene", "reasoning_effort": "high"}) == "high"
    values["text_reasoning_effort"] = "medium"
    assert hiagent.text_reasoning_effort({"stage": "assess_new_scene"}) == "medium"  # 运维全局覆盖优先于阶段表
    values["text_reasoning_effort"] = ""
    monkeypatch.setattr(config, "TEXT_REASONING_EFFORT", "max")
    assert hiagent.text_reasoning_effort({"stage_key": "storyboard_pack_segment"}) == "max"


def test_substage_minimal_table_wins_over_stage_low(monkeypatch) -> None:
    from app.harness import reasoning_effort_policy as policy
    monkeypatch.setattr(policy, "MINIMAL_EFFORT_SUBSTAGES", frozenset({"current_identity_investigation"}))
    assert policy.stage_reasoning_effort({"stage": "discover_character_candidates", "substage": "current_identity_investigation"}) == "minimal"
    assert policy.stage_reasoning_effort({"stage": "discover_character_candidates", "substage": "current_identity"}) == "low"
