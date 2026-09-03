"""规划器契约两侧同源 + 缓存复用前校验（2026-09-03 ep_8db333a7187c ``end_only`` 事故的回归）。"""
from __future__ import annotations

import json
from typing import get_args

from app.video_plan import planner_contract as pc
from app.video_plan.models import PlannerShotAnalysis, SHOT_RELATION_ENUM_CONTRACT


def _literal(field: str) -> list[str]:
    return list(get_args(PlannerShotAnalysis.model_fields[field].annotation))


def test_dependency_enum_contract_is_derived_from_pydantic_literals():
    assert pc.DEPENDENCY_ENUM_CONTRACT == {
        "state_dependency": _literal("state_dependency"),
        "motion_dependency": _literal("motion_dependency"),
    }
    assert "end_only" not in pc.DEPENDENCY_ENUM_CONTRACT["state_dependency"]


def test_prompt_states_every_legal_value_positively():
    prompt = pc.planner_system_prompt()
    for field, values in pc.DEPENDENCY_ENUM_CONTRACT.items():
        assert field in prompt
        for value in values:
            assert f"{value}=" in prompt, f"{field}.{value} 没有语义陈述"
    # 「结尾要接下一镜」的写法必须被正面告知，而不是留给模型自造 end_only
    assert "下一镜的 state_dependency" in prompt
    assert "dependency_enum_contract" in prompt


def test_output_contract_pipes_match_both_enum_contracts():
    contract = pc.planner_output_contract()
    for field, values in pc.DEPENDENCY_ENUM_CONTRACT.items():
        assert f'"{field}":"{"|".join(values)}"' in contract
    for field, values in SHOT_RELATION_ENUM_CONTRACT.items():
        assert f'"{field}":"{"|".join(values)}"' in contract


def _response(shots: list[dict]) -> str:
    return json.dumps({"shots": shots}, ensure_ascii=False)


def test_cached_window_with_invented_enum_is_not_reused():
    bad = _response([
        {"shot_id": "s1", "state_dependency": "start_only", "motion_dependency": "none"},
        {"shot_id": "s2", "state_dependency": "end_only", "motion_dependency": "none"},
    ])
    assert pc.cached_window_is_valid(bad) is False


def test_cached_window_that_passes_the_same_validation_is_reused():
    good = _response([
        {"shot_id": "s1", "state_dependency": "start_only", "motion_dependency": "pose",
         "relations": {"temporal": "same_moment", "spatial": "same_space",
                       "edit": "continuous_take", "action": "continues_same_action"}},
    ])
    assert pc.cached_window_is_valid(good) is True


def test_cached_window_without_shots_is_not_reused():
    assert pc.cached_window_is_valid("not json at all") is False
    assert pc.cached_window_is_valid(json.dumps({"shots": []})) is False
    assert pc.cached_window_is_valid(json.dumps({"shots": ["x"]})) is False
