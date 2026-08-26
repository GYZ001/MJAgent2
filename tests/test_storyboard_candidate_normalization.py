from __future__ import annotations

from app.stages import normalize_storyboard_outline_candidate


def test_storyboard_outline_candidate_joins_string_list_covers_only() -> None:
    candidate = {
        "episode_no": 1,
        "shots": [
            {"shot_no": 1, "covers": ["主线动作", "关键回应"]},
            {"shot_no": 2, "covers": [{"bad": True}]},
        ],
    }

    normalized, changes = normalize_storyboard_outline_candidate(candidate)

    assert normalized["shots"][0]["covers"] == "主线动作；关键回应"
    assert normalized["shots"][1]["covers"] == [{"bad": True}]
    assert changes == [{
        "field": "shots.0.covers",
        "from": ["主线动作", "关键回应"],
        "to": "主线动作；关键回应",
        "reason": "join_string_list",
    }]


def test_storyboard_outline_candidate_normalizes_nullable_default_fields() -> None:
    candidate = {
        "episode_no": 1,
        "shots": [{
            "shot_no": 1,
            "story_event_id": None,
            "characters_visible": None,
            "primary_action_id": None,
        }],
    }

    normalized, changes = normalize_storyboard_outline_candidate(candidate)

    assert normalized["shots"][0]["story_event_id"] == ""
    assert normalized["shots"][0]["characters_visible"] == []
    assert normalized["shots"][0]["primary_action_id"] is None
    assert {
        change["field"] for change in changes
    } == {
        "shots.0.story_event_id",
        "shots.0.characters_visible",
    }


