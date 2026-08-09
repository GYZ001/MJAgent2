from __future__ import annotations

from math import ceil

from app import config
from app.continuity import state_chain_errors
from app.narrative import (
    validate_screenplay_narrative,
    validate_storyboard_screenplay_authority,
    validate_storyboard_narrative,
)
from app.narrative_outline import (
    narrative_outline_action_delivery_errors,
    normalize_narrative_storyboard_outline,
    normalize_split_action_owner_completions,
    reconcile_narrative_outline_action_deliveries,
)
from app.schemas import (
    AudioTimelineItem,
    AtomicAction,
    AtomicActionPhase,
    KeyDialogueChain,
    KeyDialogueTurn,
    NarrativeEvent,
    NarrativeIdentityContract,
    SceneDramaticContract,
    ScriptScene,
    StoryboardOutline,
    StoryboardOutlineShot,
    TargetDelta,
    VoiceCanonical,
)
from app.validators import (
    normalize_outline_dialogue_ownership,
    outline_scene_coverage_errors,
    outline_key_line_capacity_errors,
    outline_key_line_speaker_errors,
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


def test_outline_rebinds_shifted_dialogue_to_atomic_action_relation() -> None:
    screenplay = _screenplay()
    action = _attach_generic_action(screenplay)
    action.actor_ids = ["小晶"]
    action.target_ids = []
    action.semantic_intent = "小晶说出对白「喜欢……」"
    action.completion_condition = "小晶完成回答，话轮状态向前推进"
    screenplay.key_lines = [
        "陈三：妈的，怎么不叫了？叫啊！",
        "小晶：喜欢……",
    ]
    screenplay.dialogue_chains = []
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-generic",
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="陈三说出本话轮",
                covers="陈三：妈的，怎么不叫了？叫啊！",
                primary_action="陈三单人近景说出本话轮",
                key_line_ids=["KL01"],
                characters_visible=["陈三"],
                audio_cast=["陈三"],
            )
        ],
    )

    changes = normalize_narrative_storyboard_outline(outline, screenplay)
    ownership_changes = normalize_outline_dialogue_ownership(
        outline,
        screenplay,
    )

    assert changes
    assert ownership_changes == []
    dialogue_owner = next(shot for shot in outline.shots if shot.key_line_ids)
    action_owner = next(
        shot
        for shot in outline.shots
        if shot.primary_action_id == action.action_id
    )
    assert dialogue_owner.key_line_ids == ["KL02"]
    assert dialogue_owner.characters_visible == ["小晶"]
    assert dialogue_owner.audio_cast == ["小晶"]
    assert dialogue_owner.covers == "小晶：喜欢……"
    assert action_owner.key_line_ids == []
    assert narrative_outline_action_delivery_errors(
        outline,
        screenplay,
    ) == []


def test_outline_action_delivery_gate_rejects_stale_projected_key_line() -> None:
    screenplay = _screenplay()
    action = _attach_generic_action(screenplay)
    action.actor_ids = ["小晶"]
    action.target_ids = []
    action.semantic_intent = "小晶说出对白「喜欢……」"
    screenplay.key_lines = [
        "陈三：妈的，怎么不叫了？叫啊！",
        "小晶：喜欢……",
    ]
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                shot_id="SH001",
                scene_id="SC-generic",
                story_event_id="E-1",
                event_ids=["E-1"],
                primary_action_id=action.action_id,
                key_line_ids=["KL01"],
            )
        ],
    )

    errors = narrative_outline_action_delivery_errors(outline, screenplay)

    assert len(errors) == 1
    assert "OUTLINE_ACTION_DIALOGUE_RELATION_MISMATCH" in errors[0]
    reconcile_narrative_outline_action_deliveries(outline, screenplay)
    assert outline.shots[0].key_line_ids == ["KL02"]
    assert narrative_outline_action_delivery_errors(
        outline,
        screenplay,
    ) == []


def test_outline_action_relation_keeps_canonical_segments_of_one_quote() -> None:
    screenplay = _screenplay()
    action = _attach_generic_action(screenplay)
    action.actor_ids = ["陈三"]
    action.target_ids = []
    action.semantic_intent = (
        "陈三说出对白「你不是不让我干她吗？"
        "老子今天就在你面前好好教训她。现在看清楚。」"
    )
    screenplay.key_lines = [
        "陈三：你不是不让我干她吗？",
        "陈三：老子今天就在你面前好好教训她。",
        "陈三：现在看清楚。",
    ]
    screenplay.dialogue_chains = []
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-generic",
                story_event_id="E-1",
                event_ids=["E-1"],
                key_line_ids=["KL01", "KL02", "KL03"],
            )
        ],
    )

    normalize_narrative_storyboard_outline(outline, screenplay)

    assigned = list(dict.fromkeys(
        key_id
        for shot in outline.shots
        for key_id in shot.key_line_ids
    ))
    assert assigned == ["KL01", "KL02", "KL03"]
    assert narrative_outline_action_delivery_errors(
        outline,
        screenplay,
    ) == []


def test_action_relation_does_not_reuse_short_line_inside_longer_quote() -> None:
    screenplay = _screenplay()
    short_action = _attach_generic_action(screenplay)
    short_action.actor_ids = ["character-1"]
    short_action.target_ids = []
    short_action.semantic_intent = "character-1 says 「啊……啊……」"
    long_action = AtomicAction(
        action_id="ACT-2",
        actor_ids=["character-1"],
        semantic_intent="character-1 says 「前面啊……啊……后面」",
        completion_condition="character-1 finishes the longer line.",
        decision_requirement="not_applicable",
        decision_not_applicable_reason="The event directly requires the line.",
    )
    screenplay.narrative_plan.atomic_actions.append(long_action)
    screenplay.narrative_plan.events.append(NarrativeEvent(
        event_id="E-2",
        action_ids=[long_action.action_id],
    ))
    screenplay.key_lines = [
        "character-1：啊……啊……",
        "character-1：前面啊……啊……后面",
    ]
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-generic",
                story_event_id="E-1",
                event_ids=["E-1"],
                primary_action_id=short_action.action_id,
                key_line_ids=["KL01"],
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_id="SC-generic",
                story_event_id="E-2",
                event_ids=["E-2"],
                primary_action_id=long_action.action_id,
                key_line_ids=["KL02"],
            ),
        ],
    )

    reconcile_narrative_outline_action_deliveries(outline, screenplay)

    assert outline.shots[0].key_line_ids == ["KL01"]
    assert outline.shots[1].key_line_ids == ["KL02"]
    assert narrative_outline_action_delivery_errors(
        outline,
        screenplay,
    ) == []


def test_outline_projection_drops_redundant_compiler_context_actor() -> None:
    screenplay = _screenplay()
    action = _attach_generic_action(screenplay)
    contextual_name = "Home scene unnamed participant"
    screenplay.narrative_plan.identity_contracts.append(
        NarrativeIdentityContract(
            identity_id="ID-CONTEXT",
            display_name=contextual_name,
            kind="source_backed_scene_context_actor",
            visual_policy="collective",
            visual_canonical="An event-local compiler fallback participant.",
            asset_requirement="optional",
        )
    )
    action.actor_ids.append("ID-CONTEXT")
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-generic",
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="The declared event begins.",
                covers="The observable action begins.",
                characters_visible=["character-1", contextual_name],
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_id="SC-generic",
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="The declared event becomes visible.",
                covers="The observable result is delivered.",
                characters_visible=["character-1", contextual_name],
            ),
        ],
    )

    normalize_narrative_storyboard_outline(outline, screenplay)

    assert all(
        "ID-CONTEXT" not in shot.visible_entity_ids
        for shot in outline.shots
    )
    assert all(
        contextual_name not in shot.characters_visible
        for shot in outline.shots
    )
    assert outline.shots[0].offscreen_action_actor_ids == []
    assert outline.shots[-1].offscreen_action_actor_ids == ["ID-CONTEXT"]


def test_outline_dialogue_ownership_repairs_split_fragments_and_duplicates() -> None:
    screenplay = _screenplay()
    screenplay.dialogue_chains = [
        KeyDialogueChain(
            chain_id="DC1",
            topic="办公室话轮",
            turns=[
                KeyDialogueTurn(
                    speaker="高义",
                    line="白洁，你来了，这次评你为先进是我的意思。",
                    source_text="白洁，你来了，这次评你为先进是我的意思。",
                ),
                KeyDialogueTurn(
                    speaker="白洁",
                    line="校长，我才毕业这么几年，别人会不会……",
                    source_text="校长，我才毕业这么几年，别人会不会……",
                ),
            ],
        )
    ]
    screenplay.key_lines = [
        "高义：白洁，你来了，这次评你为先进是我的意思。",
        "白洁：校长，我才毕业这么几年，别人会不会……",
    ]
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="白洁敲门进入；高义说“白洁",
                covers="白洁敲门进入；高义说“白洁",
                primary_action="白洁敲门进入；高义说“白洁",
                characters_visible=["白洁", "高义"],
            ),
            StoryboardOutlineShot(
                shot_no=2,
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="高义说明评先进是他的意思",
                covers="高义：白洁，你来了，这次评你为先进是我的意思。",
                primary_action="高义说出台词",
                key_line_ids=["KL01"],
                characters_visible=["高义"],
            ),
            StoryboardOutlineShot(
                shot_no=3,
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="高义再次说明评先进是他的意思",
                covers="高义：白洁，你来了，这次评你为先进是我的意思。",
                primary_action="高义重复说出台词",
                key_line_ids=["KL01"],
                characters_visible=["高义"],
            ),
            StoryboardOutlineShot(
                shot_no=4,
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="白洁表达担忧",
                covers="白洁：校长，我才毕业这么几年，别人会不会……",
                primary_action="白洁说出台词",
                key_line_ids=["KL02"],
                characters_visible=["白洁"],
            ),
        ],
    )

    changes = normalize_outline_dialogue_ownership(outline, screenplay)

    assert changes
    assert outline.shots[0].covers == "白洁敲门进入"
    assert outline.shots[0].audio_cast == []
    assert outline.shots[1].key_line_ids == ["KL01"]
    assert outline.shots[1].covers == "高义：白洁，你来了，这次评你为先进是我的意思。"
    assert outline.shots[2].key_line_ids == []
    assert outline.shots[2].characters_visible == ["白洁"]
    assert outline.shots[2].continuity_mode == "reaction_cut"
    assert "闭口作出可见反应" in outline.shots[2].covers
    assert outline_key_line_capacity_errors(outline, screenplay) == []


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


def test_narrative_outline_splits_joint_dialogue_and_action_budget() -> None:
    screenplay = _screenplay()
    action = _attach_generic_action(screenplay)
    line = "x" * ceil(config.SPOKEN_CHARS_PER_5_SECONDS * 1.9)
    screenplay.key_lines = [f"character-1: {line}"]
    screenplay.dialogue_chains = [
        KeyDialogueChain(
            chain_id="DC1",
            topic="Joint viewing capacity",
            turns=[
                KeyDialogueTurn(
                    speaker="character-1",
                    line=line,
                    source_text=line,
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
                beat=line,
                covers=line,
                primary_action=line,
                key_line_ids=["KL01"],
                characters_visible=["character-1"],
            )
        ],
    )

    normalize_narrative_storyboard_outline(outline, screenplay)

    assert len(outline.shots) >= 2
    dialogue = next(shot for shot in outline.shots if shot.key_line_ids)
    action_owner = next(
        shot for shot in outline.shots
        if shot.primary_action_id == action.action_id
    )
    assert dialogue.key_line_ids == ["KL01"]
    assert dialogue.primary_action_id is None
    assert action_owner.key_line_ids == []
    assert action_owner.primary_action_id == action.action_id
    for shot in outline.shots:
        budget = shot.capacity_budget.model_dump()
        total = sum(
            value for value in budget.values()
            if isinstance(value, (int, float))
        )
        assert total <= shot.duration_s


def test_narrative_outline_projection_is_structurally_idempotent() -> None:
    screenplay = _screenplay()
    _attach_generic_action(screenplay)
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
    first_ids = [shot.shot_id for shot in outline.shots]
    first_count = len(outline.shots)
    normalize_narrative_storyboard_outline(outline, screenplay)

    assert len(outline.shots) == first_count
    assert [shot.shot_id for shot in outline.shots] == first_ids


def test_narrative_outline_normalizes_unique_event_id_punctuation() -> None:
    screenplay = _screenplay()
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC-generic",
                story_event_id="E1",
                event_ids=["E1"],
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

    assert changes
    assert outline.shots[0].story_event_id == "E-1"
    assert outline.shots[0].event_ids == ["E-1"]
    assert validate_storyboard_narrative(
        None,
        screenplay,
        outline=outline,
        complete=True,
        expected_scope_id="episode-generic",
    ) == []


def test_narrative_outline_preserves_all_model_shots_for_same_event() -> None:
    screenplay = _screenplay()
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC01",
                scene_name="Home",
                scene_time="night",
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="The character asks for support.",
                covers="The request becomes visible.",
                duration_s=5,
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_id="SC01",
                scene_name="Bedroom",
                scene_time="night",
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="The relationship conflict continues in private.",
                covers="The character turns away disappointed.",
                duration_s=5,
            ),
            StoryboardOutlineShot(
                shot_no=3,
                scene_id="SC01",
                scene_name="Street",
                scene_time="morning",
                story_event_id="E-1",
                event_ids=["E-1"],
                beat="A transition carries the result into the next day.",
                covers="The character arrives at the next location.",
                duration_s=5,
            ),
        ],
    )

    normalize_narrative_storyboard_outline(outline, screenplay)

    assert len(outline.shots) >= 3
    assert [
        shot.scene_name for shot in outline.shots
        if shot.scene_name in {"Home", "Bedroom", "Street"}
    ] == ["Home", "Bedroom", "Street"]


def test_outline_scene_coverage_requires_repeated_scenes_in_story_order() -> None:
    screenplay = _screenplay()
    screenplay.scene_outline = [
        ScriptScene(
            scene_no=1,
            scene_heading="Home",
            story_function="Opening",
            summary="The story starts at home.",
        ),
        ScriptScene(
            scene_no=2,
            scene_heading="School",
            story_function="Development",
            summary="The character reaches school.",
        ),
        ScriptScene(
            scene_no=3,
            scene_heading="Home",
            story_function="Return",
            summary="The story returns home.",
        ),
    ]
    missing_middle = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_name="Home",
                beat="Opening at home.",
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_name="Home",
                beat="Return home.",
            ),
        ],
    )
    complete = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_name="Home",
                beat="Opening at home.",
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_name="School",
                beat="Arrival at school.",
            ),
            StoryboardOutlineShot(
                shot_no=3,
                scene_name="Home",
                beat="Return home.",
            ),
        ],
    )

    errors = outline_scene_coverage_errors(
        missing_middle,
        screenplay,
    )

    assert any("第 2 场" in error for error in errors)
    assert outline_scene_coverage_errors(
        complete,
        screenplay,
    ) == []


def test_outline_scene_coverage_uses_stable_scene_ids_for_repeated_location() -> None:
    screenplay = _screenplay()
    screenplay.scene_outline = [
        ScriptScene(
            scene_no=1,
            scene_heading="Home",
            story_function="Opening",
            summary="The story starts at home.",
        ),
        ScriptScene(
            scene_no=2,
            scene_heading="Home",
            story_function="Return",
            summary="A later dramatic scene returns home.",
        ),
    ]
    screenplay.narrative_plan.scene_contracts = [
        SceneDramaticContract(scene_id="SC01"),
        SceneDramaticContract(scene_id="SC02"),
    ]
    complete = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC01",
                scene_name="Mutable alias A",
                beat="Opening event.",
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_id="SC02",
                scene_name="Mutable alias B",
                beat="Return event.",
            ),
        ],
    )

    assert outline_scene_coverage_errors(complete, screenplay) == []


def test_outline_scene_coverage_reports_missing_stable_scene_id() -> None:
    screenplay = _screenplay()
    screenplay.scene_outline = [
        ScriptScene(
            scene_no=1,
            scene_heading="Home",
            story_function="Opening",
            summary="The story starts at home.",
        ),
        ScriptScene(
            scene_no=2,
            scene_heading="Home",
            story_function="Return",
            summary="A later dramatic scene returns home.",
        ),
    ]
    screenplay.narrative_plan.scene_contracts = [
        SceneDramaticContract(scene_id="SC01"),
        SceneDramaticContract(scene_id="SC02"),
    ]
    missing_second_contract = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_id="SC01",
                scene_name="Home",
                beat="Opening event.",
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_id="SC01",
                scene_name="Home",
                beat="A location-only duplicate cannot replace SC02.",
            ),
        ],
    )

    errors = outline_scene_coverage_errors(
        missing_second_contract,
        screenplay,
    )

    assert len(errors) == 1
    assert "第 2 场" in errors[0]
    assert "scene_id=SC02" in errors[0]


def test_screenplay_qa_records_scene_contract_coverage_mismatch() -> None:
    screenplay = _screenplay()
    screenplay.scene_outline = [
        ScriptScene(
            scene_no=1,
            scene_heading="Home",
            story_function="Opening",
            summary="The story starts at home.",
        ),
        ScriptScene(
            scene_no=2,
            scene_heading="School",
            story_function="Development",
            summary="The character reaches school.",
        ),
    ]

    full_errors = validate_screenplay_narrative(
        screenplay,
        require=True,
        expected_scope_id="episode-generic",
    )

    assert any(
        error.startswith("[SCENE_CONTRACT_COVERAGE_MISMATCH]")
        for error in full_errors
    )
    assert not any(
        error.startswith("[SCENE_CONTRACT_COVERAGE_MISMATCH]")
        for error in validate_storyboard_screenplay_authority(
            screenplay,
            expected_scope_id="episode-generic",
        )
    )


def test_outline_key_line_owner_is_globally_unique() -> None:
    screenplay = _screenplay()
    screenplay.key_lines = ["Hero: The exact line is delivered once."]
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                beat="Hero delivers the line.",
                key_line_ids=["KL01"],
                duration_s=5,
            ),
            StoryboardOutlineShot(
                shot_no=2,
                beat="The same line is incorrectly repeated.",
                key_line_ids=["KL01"],
                duration_s=5,
            ),
        ],
    )

    errors = outline_key_line_capacity_errors(
        outline,
        screenplay,
    )

    assert any(
        error.startswith("[OUTLINE_KEY_LINE_OWNER_DUPLICATE]")
        for error in errors
    )


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


def test_split_action_owner_only_backfills_missing_directing_prose() -> None:
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
