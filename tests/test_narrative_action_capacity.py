"""Authority-path action capacity is structural and surface-language invariant."""

from app.domain import video_ops
from app import narrative as narrative_module
from app import validators as validators_module
from app.continuity import (
    action_capacity_errors,
    apply_shot_contract,
    dialogue_framing_errors,
    narrative_action_capacity_profile,
    preflight_seedance_gates,
    shot_contract_dict,
    state_chain_errors,
)
from app.schemas import (
    AtomicAction,
    AtomicActionPhase,
    Bible,
    Dialogue,
    EpisodeScreenplay,
    IdentityContractEvidence,
    NarrativeIdentityContract,
    NarrativeContinuityPlan,
    Shot,
    ShotCapacityBudget,
    SourceEvidence,
    SourceSpan,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
    World,
)
from app.validators import (
    narrative_outline_action_capacity_errors,
    prefer_default_shot_durations,
    split_outline_over_action_capacity,
    validate_storyboard,
    validate_storyboard_preserves_key_content,
    validate_storyboard_shot_covers_outline,
)
from app.stages import _storyboard_output_contract, _storyboard_preflight_contract


def _plan(*, phase_durations: list[float]) -> NarrativeContinuityPlan:
    return NarrativeContinuityPlan(
        scope_id="episode-fictional",
        atomic_actions=[
            AtomicAction(
                action_id="ACT-QUORVEX",
                actor_ids=["entity-performer"],
                target_ids=["entity-target"],
                semantic_intent="Reconfigure the target into the intended observable state.",
                precondition_fact_ids=["FACT-BEFORE"],
                effects_add=["FACT-AFTER"],
                effects_remove=["FACT-BEFORE"],
                completion_condition="The target visibly holds the resulting configuration.",
                temporal_phases=[
                    AtomicActionPhase(
                        phase_id=f"PHASE-{index}",
                        start_condition=f"phase {index} may start",
                        end_condition=f"phase {index} is observably complete",
                        estimated_min_s=duration,
                    )
                    for index, duration in enumerate(phase_durations, start=1)
                ],
                splittable_boundaries=["PHASE-1"] if len(phase_durations) > 1 else [],
            )
        ],
    )


def _shot(
    action_desc: str,
    *,
    duration_s: int = 5,
    phase_ids: list[str] | None = None,
    action_phase_s: float | None = None,
) -> Shot:
    return Shot(
        shot_no=1,
        duration_s=duration_s,
        shot_size="中景",
        camera_move="固定",
        scene_name="semantic-space",
        characters=["entity-performer"],
        characters_visible=["entity-performer"],
        action_desc=action_desc,
        primary_action=action_desc,
        first_frame_desc="The performer and target hold the declared initial configuration.",
        last_frame_desc="The same composition shows the declared resulting configuration.",
        source_excerpt="Authorized fictional source evidence.",
        state_in="The declared precondition is visibly established.",
        state_out="The declared completion condition is visibly established.",
        continuity_mode="same_scene_cut",
        primary_action_id="ACT-QUORVEX",
        action_phase_ids=list(phase_ids if phase_ids is not None else ["PHASE-1"]),
        visible_entity_ids=["entity-performer", "entity-target"],
        capacity_budget=(
            ShotCapacityBudget(action_phase_s=action_phase_s)
            if action_phase_s is not None
            else None
        ),
        planned_state_in_fact_ids=["FACT-BEFORE"],
        planned_delta_add_fact_ids=["FACT-AFTER"],
        planned_delta_remove_fact_ids=["FACT-BEFORE"],
        planned_state_out_fact_ids=["FACT-AFTER"],
    )


def test_fictional_action_is_measured_from_temporal_phases_not_known_verbs() -> None:
    plan = _plan(phase_durations=[2.5])
    # Deliberately contains many legacy vocabulary hits.  They are surface
    # realization details, not additional authority-graph phases.
    shot = _shot(
        "走进后转身、伸手、触碰、抬头并举起，完成一次 Quorvex 重构。",
        action_phase_s=2.5,
    )

    phases, minimum_s, profile_errors = narrative_action_capacity_profile(shot, plan)
    assert (phases, minimum_s) == (1, 2.5)
    assert profile_errors == []
    assert action_capacity_errors(
        shot, narrative_authority=True, narrative_plan=plan,
    ) == []


def test_synonymous_surface_rewrites_have_identical_capacity_result() -> None:
    plan = _plan(phase_durations=[1.1, 1.2, 1.3])
    phase_ids = ["PHASE-1", "PHASE-2", "PHASE-3"]
    first = _shot(
        "角色完成一次虚构的 Quorvex 重构。",
        phase_ids=phase_ids,
        action_phase_s=3.6,
    )
    second = _shot(
        "执行者以完全不同的说法使目标达到同一结果。",
        phase_ids=phase_ids,
        action_phase_s=3.6,
    )

    first_errors = action_capacity_errors(
        first, narrative_authority=True, narrative_plan=plan,
    )
    second_errors = action_capacity_errors(
        second, narrative_authority=True, narrative_plan=plan,
    )
    assert first_errors == second_errors
    assert first_errors == []


def test_action_free_contribution_is_not_invented_from_long_prose() -> None:
    plan = _plan(phase_durations=[2.0, 2.0, 2.0])
    shot = _shot("这是一段很长的观众处理与空间定向描写，可以出现走进、转身、伸手等词面表达。")
    shot.primary_action_id = None
    shot.action_phase_ids = []

    assert action_capacity_errors(
        shot, narrative_authority=True, narrative_plan=plan,
    ) == []


def test_authority_outline_is_never_mutated_by_legacy_text_splitter() -> None:
    plan = _plan(phase_durations=[2.0])
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                shot_id="SHOT-1",
                duration_s=5,
                beat="走进、转身、伸手、触碰、举起、放下。",
                primary_action="虚构 Quorvex 重构",
                primary_action_id="ACT-QUORVEX",
                action_phase_ids=["PHASE-1"],
                visible_entity_ids=["entity-performer", "entity-target"],
                capacity_budget=ShotCapacityBudget(action_phase_s=2.0),
                planned_state_in_fact_ids=["FACT-BEFORE"],
                planned_delta_add_fact_ids=["FACT-AFTER"],
                planned_delta_remove_fact_ids=["FACT-BEFORE"],
                planned_state_out_fact_ids=["FACT-AFTER"],
            )
        ],
    )
    before = outline.model_dump(mode="json")

    assert split_outline_over_action_capacity(
        outline,
        max_shots=16,
        force=True,
        narrative_authority=True,
        narrative_plan=plan,
    ) == []
    assert outline.model_dump(mode="json") == before
    assert narrative_outline_action_capacity_errors(outline, plan) == []


def test_duration_normalization_uses_phase_time_not_action_wording() -> None:
    plan = _plan(phase_durations=[5.5])
    board = Storyboard(
        episode_no=1,
        shots=[_shot(
            "角色简短地完成虚构动作。",
            duration_s=6,
            action_phase_s=5.5,
        )],
    )

    prefer_default_shot_durations(
        board,
        narrative_authority=True,
        narrative_plan=plan,
    )
    assert board.shots[0].duration_s == 6


def test_authority_role_and_action_are_not_blocked_by_legacy_word_lists() -> None:
    plan = _plan(phase_durations=[2.0])
    plan.source_evidence = [
        SourceEvidence(
            source_evidence_id="SE-identity",
            source_span=SourceSpan(chapter_id="fictional", start=0, end=1),
            verbatim_excerpt="An open-domain performer and target are present.",
        )
    ]
    plan.identity_contracts = [
        NarrativeIdentityContract(
            identity_id=identity_id,
            display_name=identity_id,
            kind="fictional open-domain entity",
            visual_policy="contextual",
            visual_canonical=f"The declared appearance of {identity_id}.",
            asset_requirement="optional",
            evidence=IdentityContractEvidence(
                source_evidence_ids=["SE-identity"],
                rationale="The source explicitly establishes this visual identity.",
            ),
        )
        for identity_id in ("entity-performer", "entity-target")
    ]
    screenplay = EpisodeScreenplay(episode_no=1, narrative_plan=plan)
    shot = _shot(
        "虚构执行者的指节只是画面表达，本镜仍只完成 Quorvex 重构。",
        action_phase_s=2.0,
    )
    bible = Bible(
        characters=[],
        world=World(era="fictional", genre="fictional", visual_style_canonical="abstract"),
    )

    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]),
        bible,
        target_duration_s=5,
        narrative_authority=True,
        narrative_plan=plan,
        screenplay=screenplay,
    )

    assert not any("功能性路人" in error or "角色圣经中" in error for error in errors)
    assert not any("超纲细节词" in error for error in errors)
    assert not any("NARRATIVE_CHARACTER_REF_MISSING" in error for error in errors)


def test_authority_prompt_contract_uses_relations_not_story_examples() -> None:
    bible = Bible(
        characters=[],
        world=World(era="fictional", genre="fictional", visual_style_canonical="abstract"),
    )
    episode = {"episode_no": 2, "target_duration_s": 50}
    output_contract = _storyboard_output_contract(
        episode,
        bible,
        [5, 6, 7, 8, 9, 10],
        "",
        narrative_authority=True,
    )
    preflight_contract = _storyboard_preflight_contract(
        episode,
        narrative_authority=True,
    )
    combined = output_contract + preflight_contract

    assert "temporal_phases" in combined
    assert "action_phase_ids" in combined
    assert "capacity_budget" in combined
    assert "splittable_boundaries" in combined
    assert "precondition/effects/completion" in combined
    for story_bound_example in ("石碑", "测验员", "守卫", "触碰", "按压", "走进", "转身", "伸手"):
        assert story_bound_example not in combined


def test_video_preflight_uses_the_supplied_narrative_authority() -> None:
    plan = _plan(phase_durations=[2.0])
    screenplay = EpisodeScreenplay(episode_no=1, narrative_plan=plan)
    shot = _shot(
        "走进后转身、伸手、触碰、抬头并举起，但权威图只声明一个 phase。",
        action_phase_s=2.0,
    )

    errors = preflight_seedance_gates(shot, screenplay=screenplay)

    assert not any("ACTION_CAPACITY" in error for error in errors)
    assert not any("顺序动作节拍" in error for error in errors)


def test_narrative_dialogue_staging_is_intent_tagged_not_verb_matched() -> None:
    first = _shot("说话人与直接作用对象完成虚构 Quorvex 关系。")
    first.characters = ["entity-performer", "entity-target"]
    first.characters_visible = list(first.characters)
    first.dialogues = [Dialogue(speaker="entity-performer", line="Proceed.")]
    first.risk_tags = ["dialogue_two_shot_required"]
    second = first.model_copy(deep=True)
    second.action_desc = "说话人伸手、抓住并递出对象。"

    assert dialogue_framing_errors(first, narrative_authority=True) == []
    assert dialogue_framing_errors(second, narrative_authority=True) == []


def test_narrative_video_preflight_keeps_dialogue_composition_score_only() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=_plan(phase_durations=[2.0]),
    )
    shot = _shot("说话人一边移动一边完成对白。")
    shot.shot_size = "近景"
    shot.characters = ["entity-performer", "entity-target"]
    shot.characters_visible = list(shot.characters)
    shot.dialogues = [Dialogue(
        speaker="entity-performer",
        line="Proceed.",
    )]
    shot.risk_tags = ["dialogue_action_staging"]

    errors = preflight_seedance_gates(shot, screenplay=screenplay)

    assert not any("不能用单人大近景替代" in error for error in errors)


def test_narrative_state_handoff_never_uses_surface_language_overlap() -> None:
    """Modern boundaries are facts/contracts, never Chinese-bigram similarity."""
    first = _shot("A deliberately abstract first action.")
    first.state_out = "A non-Chinese representation of the completed state."
    second = _shot("A deliberately abstract next action.")
    second.shot_no = 2
    second.continuity_mode = "action_continuation"
    second.state_in = "完全不同措辞的承接状态，不共享任何连续汉字。"

    errors = state_chain_errors(
        Storyboard(episode_no=1, shots=[first, second]),
        narrative_authority=True,
    )

    assert not any("state_in 与上一镜" in error for error in errors)


def test_narrative_board_ignores_legacy_cut_and_frame_word_heuristics() -> None:
    plan = _plan(phase_durations=[2.0])
    shot = _shot(
        "镜头先以回忆般的画面切换，再呈现虚构 Quorvex 重构。",
        action_phase_s=2.0,
    )
    shot.first_frame_desc = "同一份自由文本表面描述。"
    shot.last_frame_desc = "同一份自由文本表面描述。"
    bible = Bible(
        characters=[],
        world=World(era="fictional", genre="fictional", visual_style_canonical="abstract"),
    )

    errors = validate_storyboard(
        Storyboard(episode_no=1, shots=[shot]),
        bible,
        target_duration_s=5,
        narrative_authority=True,
        narrative_plan=plan,
    )

    assert not any("多镜头/快切标记" in error for error in errors)
    assert not any("首帧与尾帧画面描述几乎相同" in error for error in errors)


def test_narrative_delivery_uses_graph_not_covers_or_key_line_text_overlap() -> None:
    plan = _plan(phase_durations=[2.0])
    screenplay = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=plan,
        key_lines=["任意身份：完全不同的台词表面"],
        key_plot_points=["任意的自由文本剧情点"],
    )
    shot = _shot("Semantic delivery is bound by typed action and fact IDs.")

    assert validate_storyboard_preserves_key_content(
        Storyboard(episode_no=1, shots=[shot]), screenplay,
    ) == []
    assert validate_storyboard_shot_covers_outline(
        shot,
        "任意的自由文本剧情点",
        shot_no=1,
        narrative_authority=True,
    ) == []


def test_confirmation_gate_forwards_narrative_authority_to_shared_validators(
    monkeypatch,
) -> None:
    plan = _plan(phase_durations=[2.0])
    screenplay = EpisodeScreenplay(episode_no=1, narrative_plan=plan)
    shot = _shot(
        "执行者完成权威图声明的唯一虚构动作。",
        action_phase_s=2.0,
    )
    board = Storyboard(episode_no=1, shots=[shot])
    bible = Bible(
        characters=[],
        world=World(era="fictional", genre="fictional", visual_style_canonical="abstract"),
    )
    captured: dict[str, object] = {}

    def fake_prefer(_board, **kwargs):
        captured["prefer"] = kwargs
        return []

    def fake_validate(_board, _bible, _target, **kwargs):
        captured["validate"] = kwargs
        return []

    monkeypatch.setattr(validators_module, "prefer_default_shot_durations", fake_prefer)
    monkeypatch.setattr(video_ops, "validate_storyboard", fake_validate)
    monkeypatch.setattr(
        validators_module,
        "validate_storyboard_screenplay_scene_alignment",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        narrative_module,
        "validate_storyboard_narrative",
        lambda *_args, **_kwargs: [],
    )

    video_ops.evaluate_storyboard_for_confirmation(
        {"id": "episode-fictional", "status": "scripting", "target_duration_s": 5},
        board,
        screenplay,
        bible,
        has_real_bible=False,
    )

    assert captured["prefer"] == {
        "narrative_authority": True,
        "narrative_plan": plan,
    }
    assert captured["validate"] == {
        "narrative_authority": True,
        "narrative_plan": plan,
        "screenplay": screenplay,
    }


def test_cross_shot_action_charges_only_each_tasks_assigned_phases() -> None:
    plan = _plan(phase_durations=[1.1, 1.2, 1.3])
    first = _shot(
        "The performer begins the fictional relation in one surface phrasing.",
        phase_ids=["PHASE-1", "PHASE-2"],
        action_phase_s=2.3,
    )
    first.planned_delta_add_fact_ids = []
    first.planned_delta_remove_fact_ids = []
    first.planned_state_out_fact_ids = ["FACT-BEFORE"]
    second = _shot(
        "A synonymous rendering completes the same relation without replay.",
        phase_ids=["PHASE-3"],
        action_phase_s=1.3,
    )
    second.primary_action_id = None
    second.supporting_action_ids = ["ACT-QUORVEX"]

    assert narrative_action_capacity_profile(first, plan) == (2, 2.3, [])
    assert narrative_action_capacity_profile(second, plan) == (1, 1.3, [])


def test_narrative_phase_and_capacity_contract_survives_persistence_roundtrip() -> None:
    source = _shot(
        "The fictional action remains language-independent.",
        phase_ids=["PHASE-1"],
        action_phase_s=2.0,
    )
    source.offscreen_action_actor_ids = ["entity-performer"]
    source.completed_before_action_phase_ids = ["PHASE-PRIOR"]

    restored = apply_shot_contract(_shot("placeholder"), shot_contract_dict(source))

    assert restored.action_phase_ids == ["PHASE-1"]
    assert restored.visible_entity_ids == ["entity-performer", "entity-target"]
    assert restored.offscreen_action_actor_ids == ["entity-performer"]
    assert restored.completed_before_action_phase_ids == ["PHASE-PRIOR"]
    assert restored.capacity_budget == ShotCapacityBudget(action_phase_s=2.0)
