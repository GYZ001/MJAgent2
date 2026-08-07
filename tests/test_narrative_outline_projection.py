from __future__ import annotations

from app import config
from app.continuity import state_chain_errors
from app.narrative import (
    validate_screenplay_narrative,
    validate_storyboard_narrative,
)
from app.narrative_outline import (
    normalize_narrative_storyboard_outline,
    normalize_split_action_owner_completions,
)
from app.production.patch import apply_patch_operation_to_document
from app.production.policy import assert_patch_ops_allowed
from app.production.screenplay_document import (
    document_to_screenplay,
    screenplay_to_document,
)
from app.production.screenplay_repair import plan_screenplay_patch
from app.production.structured_issues import issues_from_validator_messages
from app.schemas import (
    AudioTimelineItem,
    AtomicAction,
    AtomicActionPhase,
    KeyDialogueChain,
    KeyDialogueTurn,
    NarrativeEvent,
    StoryboardOutline,
    StoryboardOutlineShot,
    TargetDelta,
    VoiceCanonical,
)
from app.spoken_contract import content_char_count
from app.validators import (
    outline_key_line_speaker_errors,
    validate_dialogue_chains,
    normalize_screenplay_dialogue_chains,
)
from tests.test_narrative_continuity import _board, _screenplay


def _attach_generic_action(screenplay) -> AtomicAction:
    action = AtomicAction(
        action_id="ACT-1",
        actor_ids=["character-1"],
        target_ids=["entity-1"],
        semantic_intent="The actor changes the target after speaking.",
        precondition_fact_ids=["F-before"],
        effects_add=["F-after"],
        effects_remove=["F-before"],
        completion_condition="The target visibly holds the completed state.",
        decision_requirement="not_applicable",
        decision_not_applicable_reason="The event directly requires the action.",
        temporal_phases=[
            AtomicActionPhase(
                phase_id="ACT-1/finish",
                start_condition="The dialogue has finished.",
                end_condition="The completed target state is visible.",
                estimated_min_s=1.0,
            ),
        ],
    )
    screenplay.narrative_plan.atomic_actions.append(action)
    screenplay.narrative_plan.events[0].action_ids = [action.action_id]
    return action


def test_narrative_outline_projects_graph_owned_fields_deterministically() -> None:
    screenplay = _screenplay()
    screenplay.dialogue_chains = [
        KeyDialogueChain(
            chain_id="DC1",
            topic="Observable event delivery",
            turns=[
                KeyDialogueTurn(
                    speaker="character-1",
                    line="The observable result becomes visible.",
                    source_text="The observable result becomes visible.",
                )
            ],
        )
    ]
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-generic",
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="The declared event becomes visible.",
                covers="The observable result is delivered.",
                duration_s=5,
                characters_visible=["character-1"],
            )
        ],
    )

    changes = normalize_narrative_storyboard_outline(outline, screenplay)

    assert changes
    assert outline.shots[0].shot_id == "SH001"
    assert any(shot.key_line_ids == ["KL01"] for shot in outline.shots)
    assert outline.shots[0].planned_state_in_fact_ids == ["F-before"]
    assert outline.shots[-1].planned_state_out_fact_ids == ["F-after"]
    assert validate_storyboard_narrative(
        None,
        screenplay,
        outline=outline,
        complete=True,
        expected_scope_id="episode-generic",
    ) == []


def test_narrative_outline_does_not_readd_already_established_fact() -> None:
    screenplay = _screenplay()
    first_event = screenplay.narrative_plan.events[0]
    first_event.precondition_fact_ids = ["F-before"]
    first_event.effects_add = ["F-before", "F-after"]
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-generic",
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="The declared event becomes visible.",
                covers="The observable result is delivered.",
                duration_s=5,
            )
        ],
    )

    normalize_narrative_storyboard_outline(outline, screenplay)

    owner = outline.shots[-1]
    assert owner.planned_state_in_fact_ids == ["F-before"]
    assert owner.planned_delta_add_fact_ids == ["F-after"]
    assert owner.planned_state_out_fact_ids == ["F-after"]


def test_declared_narrator_capacity_splits_keep_shared_source_evidence() -> None:
    source = (
        "我知道你现在一定很瞧不起我，可我有什么办法，"
        "你也知道连你都保护不了我，我一个女孩子又能怎么样？"
    )
    screenplay = _screenplay()
    screenplay.voice_bible = [
        VoiceCanonical(
            speaker_id="旁白",
            role_type="narrator",
            voice_canonical="calm letter reading",
        ),
    ]
    screenplay.dialogue_chains = [
        KeyDialogueChain(
            chain_id="DC-NARRATOR",
            topic="A source-grounded letter",
            turns=[
                KeyDialogueTurn(
                    speaker="旁白",
                    line="我知道你现在一定很瞧不起我，可我有什么办法，",
                    source_text=source,
                ),
                KeyDialogueTurn(
                    speaker="旁白",
                    line="你也知道连你都保护不了我，我一个女孩子又能怎么样？",
                    source_text=source,
                ),
            ],
        ),
    ]

    normalize_screenplay_dialogue_chains(screenplay)

    assert len(screenplay.dialogue_chains[0].turns) == 2


def test_narrative_outline_splits_dialogue_when_speaker_changes() -> None:
    screenplay = _screenplay()
    action = _attach_generic_action(screenplay)
    screenplay.dialogue_chains = [
        KeyDialogueChain(
            chain_id="DC1",
            topic="Alternating speakers",
            turns=[
                KeyDialogueTurn(
                    speaker="character-1",
                    line="The first speaker states the observable result.",
                    source_text="The first speaker states the observable result.",
                ),
                KeyDialogueTurn(
                    speaker="character-2",
                    line="The second speaker responds from the reverse angle.",
                    source_text="The second speaker responds from the reverse angle.",
                ),
            ],
        )
    ]
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-generic",
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="The declared event becomes visible.",
                covers="The observable result is delivered.",
                duration_s=5,
            )
        ],
    )

    changes = normalize_narrative_storyboard_outline(
        outline,
        screenplay,
    )

    dialogue_shots = [
        shot for shot in outline.shots if shot.key_line_ids
    ]
    assert changes
    assert [shot.key_line_ids for shot in dialogue_shots] == [
        ["KL01"],
        ["KL02"],
    ]
    assert [shot.audio_cast for shot in dialogue_shots] == [
        ["character-1"],
        ["character-2"],
    ]
    action_owner = next(
        shot for shot in outline.shots
        if shot.primary_action_id == action.action_id
    )
    assert action_owner.shot_no > max(
        shot.shot_no for shot in dialogue_shots
    )
    assert action_owner.key_line_ids == []
    assert action_owner.audio_cast == []
    assert action_owner.state_in == ""
    assert action_owner.primary_action == action.completion_condition
    assert action_owner.state_out == action.completion_condition
    assert action_owner.beat == action.completion_condition
    assert action_owner.covers == action.completion_condition
    assert outline_key_line_speaker_errors(outline, screenplay) == []


def test_stale_split_action_owner_is_reprojected_before_shot_generation() -> None:
    screenplay = _screenplay()
    action = _attach_generic_action(screenplay)
    screenplay.dialogue_chains = [
        KeyDialogueChain(
            chain_id="DC1",
            topic="Action with dialogue",
            turns=[
                KeyDialogueTurn(
                    speaker="character-1",
                    line="The actor speaks before the action completes.",
                    source_text="The actor speaks before the action completes.",
                ),
            ],
        )
    ]
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                event_ids=["E-1"],
                story_event_id="E-1",
                key_line_ids=["KL01"],
                audio_cast=["character-1"],
            ),
            StoryboardOutlineShot(
                shot_no=2,
                event_ids=["E-1"],
                story_event_id="E-1",
                primary_action_id=action.action_id,
                state_in="The event has not started.",
                primary_action="The actor repeats the opening speech.",
                state_out="The opening speech is about to begin.",
                beat="The actor repeats the opening speech.",
                covers="The actor repeats the opening speech.",
            ),
        ],
    )
    changes = normalize_split_action_owner_completions(
        outline,
        screenplay,
    )

    owner = outline.shots[1]
    assert changes
    assert owner.state_in == ""
    assert owner.primary_action == action.completion_condition
    assert owner.state_out == action.completion_condition
    assert owner.beat == action.completion_condition
    assert owner.covers == action.completion_condition


def test_narrative_outline_keeps_coarse_snapshot_until_later_deadline() -> None:
    screenplay = _screenplay()
    plan = screenplay.narrative_plan
    plan.events.append(NarrativeEvent(
        event_id="E-2",
        proposition_ids=["P-story"],
        delivery_scope_id="episode-generic",
        primary_delivery_window_id="RW-2",
    ))
    cold_path = next(
        path
        for path in plan.experience_intents[0].audience_paths
        if path.audience_prior_id == "AP-cold"
    )
    cold_path.target_deltas.append(TargetDelta(
        target_delta_id="XD-cold-late-affect",
        dimension="affective",
        description="The later event changes the final affective state.",
        from_state={"affective_state": {}},
        to_state={"affective_state": {"registration": "settled"}},
        required_processing_s=1.0,
        deadline_event_id="E-2",
        primary_delivery_window_id="RW-2",
    ))
    cold_out = next(
        state
        for state in plan.audience_states
        if state.audience_state_id
        == cold_path.audience_state_out_target_id
    )
    cold_out.affective_state = {"registration": "settled"}
    later_window = plan.readability_windows[0].model_copy(deep=True)
    later_window.readability_window_id = "RW-2"
    later_window.event_ids = ["E-2"]
    later_window.target_delta_ids = ["XD-cold-late-affect"]
    later_window.shot_ids = ["SH002"]
    plan.readability_windows.append(later_window)
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-generic",
                event_ids=["E-1"],
                story_event_id="E-1",
                beat="The first event is delivered.",
                covers="The first event becomes observable.",
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_id="SC-generic",
                event_ids=["E-2"],
                story_event_id="E-2",
                beat="The later event changes the final audience state.",
                covers="The later affective result becomes observable.",
            ),
        ],
    )

    changes = normalize_narrative_storyboard_outline(
        outline,
        screenplay,
    )
    errors = validate_storyboard_narrative(
        None,
        screenplay,
        outline=outline,
        complete=True,
        expected_scope_id="episode-generic",
    )

    assert changes
    cold_paths = [
        path
        for shot in outline.shots
        for path in shot.audience_state_paths
        if path.audience_prior_id == "AP-cold"
    ]
    assert cold_paths[0].audience_state_in_id == "AS-cold-in"
    assert cold_paths[0].audience_state_out_target_id == "AS-cold-in"
    assert cold_paths[-1].audience_state_out_target_id == "AS-cold-out"
    assert not any(
        "SHOT_TARGET_TO_STATE_MISMATCH" in error
        for error in errors
    )


def _screenplay_needing_staged_audience_state():
    screenplay = _screenplay()
    plan = screenplay.narrative_plan
    plan.audience_states = [
        state
        for state in plan.audience_states
        if state.audience_state_id != "AS-cold-settled"
    ]
    cold_path = next(
        path
        for path in plan.experience_intents[0].audience_paths
        if path.audience_prior_id == "AP-cold"
    )
    cold_path.target_deltas.append(
        TargetDelta(
            target_delta_id="XD-cold-attention",
            dimension="attention",
            proposition_ids=["P-story"],
            description="The later attention residue becomes active.",
            from_state={"attention_residue_ids": []},
            to_state={"attention_residue_ids": ["attention-later"]},
            required_processing_s=10.0,
            deadline_event_id="E-1",
            primary_delivery_window_id="RW-1",
        )
    )
    cold_out = next(
        state
        for state in plan.audience_states
        if state.audience_state_id == cold_path.audience_state_out_target_id
    )
    cold_out.attention_residue_ids = ["attention-later"]
    window = plan.readability_windows[0]
    window.target_delta_ids.append("XD-cold-attention")
    window.scheduled_processing_s = 11.0
    window.planned_available_s = 11.0
    return screenplay


def test_screenplay_repair_creates_required_intermediate_audience_state() -> None:
    screenplay = _screenplay_needing_staged_audience_state()
    errors = validate_screenplay_narrative(screenplay, require=True)
    message = next(
        error
        for error in errors
        if "AUDIENCE_TARGET_DELTA_STAGING_REQUIRED" in error
    )
    issue = issues_from_validator_messages(
        [message],
        subject="screenplay",
        stage="screenplay",
    )[0]

    operations = plan_screenplay_patch(issue, screenplay)

    assert len(operations) == 1
    assert operations[0].op == "create_node"
    assert operations[0].target["collection"] == "audience_states"
    document, _event = apply_patch_operation_to_document(
        screenplay_to_document(screenplay),
        operations[0],
    )
    repaired = document_to_screenplay(document)
    assert not any(
        "AUDIENCE_TARGET_DELTA_STAGING_REQUIRED" in error
        for error in validate_screenplay_narrative(repaired, require=True)
    )


def test_screenplay_repair_splits_oversized_dialogue_turn() -> None:
    screenplay = _screenplay()
    oversized = "这是一个必须按原文标点拆分的连续对白，" * 5
    screenplay.dialogue_chains = [
        KeyDialogueChain(
            chain_id="DC1",
            topic="容量测试对白",
            turns=[
                KeyDialogueTurn(
                    speaker="character-1",
                    line=oversized,
                    source_text=oversized,
                )
            ],
        )
    ]
    turn = screenplay.dialogue_chains[0].turns[0]
    turn.line = oversized
    turn.source_text = oversized
    message = next(
        error
        for error in validate_dialogue_chains(
            screenplay,
            source_text=oversized,
            required=True,
        )
        if "DIALOGUE_TURN_CAPACITY_EXCEEDED" in error
    )
    issue = issues_from_validator_messages(
        [message],
        subject="screenplay",
        stage="screenplay",
    )[0]

    operations = plan_screenplay_patch(issue, screenplay)

    assert len(operations) == 1
    assert operations[0].op == "split_dialogue_turn_by_capacity"
    assert_patch_ops_allowed([
        operation.model_dump(mode="json")
        for operation in operations
    ])
    document, _event = apply_patch_operation_to_document(
        screenplay_to_document(screenplay),
        operations[0],
    )
    repaired = document_to_screenplay(document)
    assert len(repaired.dialogue_chains[0].turns) > 1
    assert all(
        content_char_count(item.line)
        <= config.MAX_SPOKEN_CHARS_PER_SHOT
        for item in repaired.dialogue_chains[0].turns
    )


def test_ambient_audio_is_not_charged_as_spoken_capacity() -> None:
    screenplay = _screenplay()
    board = _board()
    baseline = validate_storyboard_narrative(board, screenplay)
    board.shots[0].audio_timeline.append(
        AudioTimelineItem(
            start_s=0,
            end_s=board.shots[0].duration_s,
            type="ambient_sound",
            text="A deliberately verbose environmental sound description.",
            lip_sync=False,
        )
    )

    assert validate_storyboard_narrative(board, screenplay) == baseline


def test_same_late_day_bucket_does_not_require_scene_change() -> None:
    board = _board()
    first = board.shots[0]
    second = first.model_copy(deep=True)
    second.shot_no = 2
    board.shots = [first, second]
    first.scene_name = second.scene_name = "same-place"
    first.scene_time = "午后"
    second.scene_time = "午后三点多"
    second.continuity_mode = "same_scene_cut"

    errors = state_chain_errors(board)

    assert not any(
        "continuity_mode" in error and "scene_change" in error
        for error in errors
    )
