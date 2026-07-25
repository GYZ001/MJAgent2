from __future__ import annotations

import asyncio
import json

from app import stages
from app.loops import AgentLoop, AgentLoopPolicy
from app.stages import (StoryboardShotDraft, _run_with_agent_loop,
                        normalize_storyboard_shot_candidate)


def _loop() -> AgentLoop[StoryboardShotDraft]:
    return AgentLoop(
        stage_key="storyboard_shot_2",
        contract_key="storyboard",
        goal="normalize and validate shot 2",
        scope_type="storyboard_checkpoint",
        scope_id="ep1:2",
        artifact_type="storyboard_shot",
        policy=AgentLoopPolicy(max_iterations=4, stall_rounds=2, no_gain_rounds=2),
    )


def test_storyboard_candidate_normalizes_nullable_contract_fields() -> None:
    candidate = {
        "episode_no": 99,
        "shot": {
            "shot_no": 88,
            "story_event_id": None,
            "state_in": None,
            "new_information_ids": None,
            "audio_cast": None,
        },
    }

    normalized, changes = normalize_storyboard_shot_candidate(
        candidate, episode_no=1, shot_no=2
    )

    assert normalized["episode_no"] == 1
    assert normalized["shot"]["shot_no"] == 2
    assert normalized["shot"]["story_event_id"] == ""
    assert normalized["shot"]["state_in"] == ""
    assert normalized["shot"]["new_information_ids"] == []
    assert normalized["shot"]["audio_cast"] == []
    assert {change["field"] for change in changes} >= {
        "episode_no", "shot.shot_no", "shot.story_event_id",
        "shot.state_in", "shot.new_information_ids", "shot.audio_cast",
    }


def test_storyboard_candidate_uses_outline_event_id_but_does_not_coerce_objects() -> None:
    normalized, _ = normalize_storyboard_shot_candidate(
        {"episode_no": 1, "shot": {"shot_no": 2, "story_event_id": {"bad": True}}},
        episode_no=1,
        shot_no=2,
    )
    assert normalized["shot"]["story_event_id"] == {"bad": True}

    authoritative, _ = normalize_storyboard_shot_candidate(
        {"episode_no": 1, "shot": {"shot_no": 2, "story_event_id": None}},
        episode_no=1,
        shot_no=2,
        outline_story_event_id="E07",
    )
    assert authoritative["shot"]["story_event_id"] == "E07"


def test_storyboard_loop_recovers_broken_json_then_null_event_id(monkeypatch) -> None:
    """Use the two structural failure shapes observed in ERR-20260725-c8bb0d."""
    outputs = [
        '{"episode_no":1,"shot":{"source_excerpt":"他说"你好""}}',
        json.dumps({
            "episode_no": 1,
            "is_final": False,
            "shot": {
                "shot_no": 2,
                "duration_s": 8,
                "shot_size": "中景",
                "camera_move": "跟随",
                "scene_setting": "日，测验广场",
                "characters": ["萧炎"],
                "action_desc": "萧炎转身走回队伍最后一排。",
                "story_event_id": None,
            },
        }, ensure_ascii=False),
    ]
    calls = 0

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        value = outputs[calls]
        calls += 1
        return value

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(stages, "log_provider_call", lambda *_args, **_kwargs: None)

    result = asyncio.run(_run_with_agent_loop(
        "分镜脚本",
        "storyboard",
        "生成第2镜",
        StoryboardShotDraft,
        lambda _draft: [],
        loop=_loop(),
        repair_output_contract="story_event_id 无值时输出空字符串，禁止 null",
        prefill={"episode_no": 1},
        storyboard_candidate_context={
            "episode_id": "ep1", "episode_no": 1, "shot_no": 2,
            "outline_story_event_id": "",
        },
    ))

    assert calls == 2
    assert result.shot.shot_no == 2
    assert result.shot.story_event_id == ""
