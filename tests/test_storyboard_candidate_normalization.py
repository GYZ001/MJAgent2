from __future__ import annotations

import asyncio
import json

from app import stages
from app.continuity import dialogue_framing_errors
from app.loops import AgentLoop, AgentLoopPolicy
from app.stages import (StoryboardShotDraft, _run_with_agent_loop,
                        normalize_storyboard_outline_candidate,
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


def _dialogue_loop() -> AgentLoop[StoryboardShotDraft]:
    return AgentLoop(
        stage_key="storyboard_shot_2",
        contract_key="storyboard",
        goal="repair dialogue framing",
        scope_type="storyboard_checkpoint",
        scope_id="ep1:2",
        artifact_type="storyboard_shot",
        policy=AgentLoopPolicy(
            max_iterations=4,
            stall_rounds=2,
            no_gain_rounds=2,
            repair_issue_codes=frozenset({"DIALOGUE_FRAMING_INVALID"}),
        ),
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


def test_storyboard_candidate_inherits_complete_outline_narrative_authority() -> None:
    outline_task = {
        "shot_id": "SH002",
        "scene_id": "SC01",
        "event_ids": ["E2"],
        "visible_entity_ids": ["char-a", "char-b"],
        "capacity_budget": {
            "action_phase_s": 0.0,
            "spoken_and_text_s": 4.0,
        },
        "completed_before_action_ids": ["A1"],
        "narrative_boundary_from_previous": {
            "boundary_id": "NB-SH001-SH002",
            "previous_shot_id": "SH001",
            "next_shot_id": "SH002",
        },
    }
    candidate = {
        "episode_no": 1,
        "shot": {
            "shot_no": 2,
            "shot_id": "",
            "scene_id": "wrong",
            "event_ids": [],
            "visible_entity_ids": ["char-a"],
            "capacity_budget": None,
            "completed_before_action_ids": [],
            "narrative_boundary_from_previous": None,
        },
    }

    normalized, changes = normalize_storyboard_shot_candidate(
        candidate,
        episode_no=1,
        shot_no=2,
        outline_narrative_task=outline_task,
    )

    for field, value in outline_task.items():
        assert normalized["shot"][field] == value
    assert {
        change["field"] for change in changes
        if change["reason"] == "outline_narrative_authority"
    } == {
        f"shot.{field}"
        for field in outline_task
        if field != "capacity_budget"
    }
    assert any(
        change["field"] == "shot.capacity_budget"
        and change["reason"] == "derived_spoken_capacity"
        for change in changes
    )
    normalized["shot"]["event_ids"].append("mutated")
    assert outline_task["event_ids"] == ["E2"]


def test_storyboard_candidate_removes_unassigned_speech_and_expands_duration() -> None:
    candidate = {
        "episode_no": 1,
        "shot": {
            "shot_no": 3,
            "duration_s": 5,
            "dialogues": [{
                "speaker": "char-a",
                "line": "This speech was not assigned by the outline.",
            }],
            "audio_cast": ["char-a"],
            "audio_timeline": [
                {
                    "start_s": 0,
                    "end_s": 3,
                    "type": "spoken_dialogue",
                    "speaker_id": "char-a",
                    "text": "This speech was not assigned by the outline.",
                },
                {
                    "start_s": 0,
                    "end_s": 5,
                    "type": "ambient_sound",
                    "text": "A long environmental description.",
                },
            ],
        },
    }
    outline_task = {
        "key_line_ids": [],
        "audio_cast": [],
        "capacity_budget": {
            "action_phase_s": 3.0,
            "spoken_and_text_s": 0.0,
            "attention_switch_s": 0.0,
            "inference_processing_s": 0.0,
            "reaction_registration_s": 3.0,
            "spatial_reorientation_s": 0.0,
            "entry_exit_settle_s": 0.0,
            "other_s": 0.0,
        },
    }

    normalized, changes = normalize_storyboard_shot_candidate(
        candidate,
        episode_no=1,
        shot_no=3,
        outline_narrative_task=outline_task,
    )

    shot = normalized["shot"]
    assert shot["dialogues"] == []
    assert shot["audio_cast"] == []
    assert [item["type"] for item in shot["audio_timeline"]] == [
        "ambient_sound"
    ]
    assert shot["capacity_budget"]["spoken_and_text_s"] == 0
    assert shot["duration_s"] == 6
    assert {change["reason"] for change in changes} >= {
        "unassigned_spoken_content_removed",
        "derived_joint_capacity",
    }


def test_storyboard_candidate_derives_scene_change_and_transition() -> None:
    candidate = {
        "episode_no": 1,
        "shot": {
            "shot_no": 4,
            "scene_name": "wrong",
            "scene_time": "wrong",
            "continuity_mode": "same_scene_cut",
            "transition": "硬切",
        },
    }
    normalized, changes = normalize_storyboard_shot_candidate(
        candidate,
        episode_no=1,
        shot_no=4,
        outline_narrative_task={
            "scene_name": "同一客厅",
            "scene_time": "午后三点",
            "continuity_mode": "same_scene_cut",
        },
        previous_scene_name="同一客厅",
        previous_scene_time="早晨",
    )

    shot = normalized["shot"]
    assert shot["scene_name"] == "同一客厅"
    assert shot["scene_time"] == "午后三点"
    assert shot["continuity_mode"] == "scene_change"
    assert shot["transition"] == "叠化"
    assert {change["reason"] for change in changes} >= {
        "outline_scene_authority",
        "derived_scene_continuity",
        "derived_scene_transition",
    }


def test_storyboard_candidate_adds_lip_sync_speaker_to_visible_ids() -> None:
    normalized, changes = normalize_storyboard_shot_candidate(
        {
            "episode_no": 1,
            "shot": {
                "shot_no": 2,
                "duration_s": 5,
                "characters_visible": ["Display Name"],
                "audio_timeline": [{
                    "start_s": 0,
                    "end_s": 4,
                    "type": "spoken_dialogue",
                    "speaker_id": "character-id",
                    "text": "short line",
                }, {
                    "start_s": 4,
                    "end_s": 5,
                    "type": "spoken_dialogue",
                    "speaker_id": "listener-id",
                    "text": "reply",
                }],
            },
        },
        episode_no=1,
        shot_no=2,
        outline_narrative_task={
            "key_line_ids": ["KL01"],
            "audio_cast": ["Display Name"],
            "capacity_budget": {
                "spoken_and_text_s": 0,
            },
        },
    )

    assert normalized["shot"]["characters_visible"] == ["character-id"]
    assert normalized["shot"]["shot_size"] == "近景"
    assert normalized["shot"]["camera_move"] == "固定"
    assert normalized["shot"]["dialogues"] == [
        {
            "speaker": "character-id",
            "line": "short line",
            "emotion": "平静",
            "delivery": "spoken_dialogue",
        },
        {
            "speaker": "listener-id",
            "line": "reply",
            "emotion": "平静",
            "delivery": "offscreen_voice",
        },
    ]
    assert any(
        change["reason"] == "lip_sync_speaker_visible"
        for change in changes
    )
    assert any(
        change["reason"] == "timeline_spoken_authority"
        for change in changes
    )
    assert any(
        change["reason"] == "single_onscreen_speaker"
        for change in changes
    )


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


def test_storyboard_dialogue_framing_blocker_requests_repair(monkeypatch) -> None:
    base_shot = {
        "shot_no": 2,
        "duration_s": 5,
        "shot_size": "中景",
        "camera_move": "固定",
        "scene_setting": "日，广场",
        "characters": ["甲", "乙"],
        "characters_visible": ["甲", "乙"],
        "action_desc": "甲站在画面中央面向画外听者，清楚说出自己的决定。",
        "first_frame_desc": "甲与乙同处画面中央，甲看向画外，尚未开口。",
        "last_frame_desc": "同一机位，甲说完决定，乙仍站在一旁看着他。",
        "source_excerpt": "甲抬起头，终于当众说出了自己的决定。",
        "dialogues": [
            {
                "speaker": "甲",
                "line": "这件事由我来做。",
                "emotion": "坚定",
                "delivery": "spoken_dialogue",
            }
        ],
        "transition": "硬切",
    }
    repaired_shot = {
        **base_shot,
        "shot_size": "近景",
        "characters": ["甲"],
        "characters_visible": ["甲"],
        "first_frame_desc": "甲独自处于近景中央，面向画外听者，尚未开口。",
        "last_frame_desc": "同一机位，甲说完决定后仍看向画外，神情坚定。",
    }
    outputs = [
        json.dumps(
            {"episode_no": 1, "is_final": False, "shot": base_shot},
            ensure_ascii=False,
        ),
        json.dumps(
            {"episode_no": 1, "is_final": False, "shot": repaired_shot},
            ensure_ascii=False,
        ),
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
        lambda draft: dialogue_framing_errors(draft.shot),
        loop=_dialogue_loop(),
        prefill={"episode_no": 1},
        storyboard_candidate_context={
            "episode_id": "ep1",
            "episode_no": 1,
            "shot_no": 2,
            "outline_story_event_id": "",
        },
    ))

    assert calls == 2
    assert result.disposition == "PASS"
    assert dialogue_framing_errors(result.shot) == []
