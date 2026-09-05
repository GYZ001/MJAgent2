"""规划器输出的宽容解析：裸镜头数组、依赖枚举同义写法——契约两侧对齐，不整集打回。"""
from __future__ import annotations

from app.video_plan.planner_contract import window_shots_from_planner_response
from app.video_plan.normalize import normalize_ai_shot_plan_candidate


def test_bare_shot_array_is_accepted_as_window_shots():
    import json

    shots = [{"shot_id": "s1"}, {"shot_id": "s2"}]
    assert window_shots_from_planner_response(shots) == shots
    assert window_shots_from_planner_response({"shots": shots}) == shots
    assert window_shots_from_planner_response({"items": shots}) is None
    assert window_shots_from_planner_response("junk") is None
    # 原始文本形态：裸数组（extract_json 只认第一个对象，会把第一个镜头当整份输出）
    assert window_shots_from_planner_response(json.dumps(shots)) == shots
    assert window_shots_from_planner_response("```json\n" + json.dumps({"shots": shots}) + "\n```") == shots
    assert window_shots_from_planner_response("<think>想一想</think>\n" + json.dumps(shots)) == shots


def test_end_only_dependency_normalises_to_none_with_a_record():
    raw = {"shot_id": "s1", "mode": "REFERENCE_IMAGE_MODE", "state_dependency": "end_only",
           "motion_dependency": "none", "relations": {}}
    normalized, changes = normalize_ai_shot_plan_candidate(raw)
    assert normalized["state_dependency"] == "none"
    assert {"field": "state_dependency", "from": "end_only", "to": "none"} in changes
