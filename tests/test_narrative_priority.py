import pytest

from app.errors import ArtifactNeedsRebuildError
from app.narrative_priority import (
    merge_outline_delivery_beats,
    picture_screenplay_projection,
)
from app.schemas import (
    AtomicAction,
    EpisodeScreenplay,
    InformationItem,
    NarrativeContinuityPlan,
    NarrativeEvent,
    NarrativeIdentityContract,
    PlotSpine,
    PlotSpineBeat,
    SceneDramaticContract,
    ScriptScene,
    ShotCapacityBudget,
    SourceCoverageDecision,
    StoryboardOutline,
    StoryboardOutlineShot,
    StoryEvent,
)


def test_legacy_terminal_non_story_scene_requires_rebuild() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=3,
        id="episode-3",
        plot_spine=PlotSpine(
            spine_beats=[
                PlotSpineBeat(
                    beat_id="S01",
                    who="主角",
                    does="完成剧情行动",
                    turn="剧情状态完成",
                    source_segment_ids=["SRC0001"],
                    information_ids=["I1"],
                ),
                PlotSpineBeat(
                    beat_id="S02",
                    who="场外发布者",
                    does="向受众传递非剧情信息",
                    turn="旁文本传递完成",
                    source_segment_ids=["SRC0002"],
                    information_ids=["I2"],
                ),
            ],
            must_keep_ending="旁文本传递完成",
        ),
        source_coverage=[
            SourceCoverageDecision(
                source_segment_id="SRC0001",
                disposition="deliver",
                beat_ids=["S01"],
            ),
            SourceCoverageDecision(
                source_segment_id="SRC0002",
                disposition="deliver",
                beat_ids=["S02"],
            ),
        ],
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】连续时间 / 剧情空间",
                story_function="完成剧情状态变化",
                summary="主角完成剧情行动",
            ),
            ScriptScene(
                scene_no=2,
                scene_heading="【场2】时域外 / 非剧情呈现空间",
                story_function="传递来源附带信息",
                summary="场外发布者向受众传递附带信息",
            ),
        ],
        events=[
            StoryEvent(event_id="E1", source_span="SRC0001"),
            StoryEvent(event_id="E2", source_span="SRC0002"),
        ],
        information_ledger=[
            InformationItem(info_id="I1", event_id="E1", content="剧情状态"),
            InformationItem(info_id="I2", event_id="E2", content="附带信息"),
        ],
        narrative_plan=NarrativeContinuityPlan(
            contract_version="narrative-continuity.v1",
            scope_id="episode-3",
            events=[
                NarrativeEvent(
                    event_id="E1",
                    action_ids=["A1"],
                    downstream_dependency_event_ids=["E2"],
                    delivery_scope_id="episode-3",
                ),
                NarrativeEvent(
                    event_id="E2",
                    action_ids=["A2"],
                    causal_parent_ids=["E1"],
                    delivery_scope_id="episode-3",
                ),
            ],
            atomic_actions=[
                AtomicAction(
                    action_id="A1",
                    actor_ids=["hero"],
                    participant_deliveries=[],
                    semantic_intent="完成剧情行动",
                    completion_condition="剧情状态完成",
                ),
                AtomicAction(
                    action_id="A2",
                    actor_ids=["context"],
                    participant_deliveries=[],
                    semantic_intent="传递来源附带信息",
                    completion_condition="旁文本传递完成",
                ),
            ],
            scene_contracts=[
                SceneDramaticContract(
                    scene_id="SC01",
                    turn_event_ids=["E1"],
                ),
                SceneDramaticContract(
                    scene_id="SC02",
                    turn_event_ids=["E2"],
                ),
            ],
            identity_contracts=[
                NarrativeIdentityContract(
                    identity_id="hero",
                    display_name="主角",
                    kind="named_character",
                    visual_policy="canonical",
                    visual_canonical="稳定主角形象",
                    asset_requirement="required",
                ),
                NarrativeIdentityContract(
                    identity_id="context",
                    display_name="来源附带呈现主体",
                    kind="source_backed_scene_context_actor",
                    visual_policy="collective",
                    visual_canonical="仅在当前来源段成立的呈现主体",
                    asset_requirement="optional",
                ),
            ],
        ),
    )

    with pytest.raises(
        ArtifactNeedsRebuildError,
        match="ARTIFACT_NEEDS_REBUILD",
    ):
        picture_screenplay_projection(screenplay)


def test_strict_unit_picture_projection_recovers_delivery_merge_policy() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        source_text_range="screenplay-generation-ir.v4",
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】日 / 测验广场",
                story_function="完成连续测验动作",
                summary="两个相邻来源单元在同一连续场景内发生。",
            ),
        ],
        narrative_plan=NarrativeContinuityPlan(
            contract_version="narrative-continuity.v2",
            scope_id="episode-1",
            events=[
                NarrativeEvent(
                    event_id="E1",
                    narrative_layer="story",
                    event_priority="causal",
                    render_policy="standalone",
                    downstream_dependency_event_ids=["E2"],
                ),
                NarrativeEvent(
                    event_id="E2",
                    narrative_layer="story",
                    event_priority="causal",
                    render_policy="standalone",
                    causal_parent_ids=["E1"],
                ),
            ],
            scene_contracts=[
                SceneDramaticContract(
                    scene_id="SC01",
                    turn_event_ids=["E2"],
                ),
            ],
        ),
    )

    projected, report = picture_screenplay_projection(screenplay)

    assert [
        event.render_policy
        for event in screenplay.narrative_plan.events
    ] == ["standalone", "standalone"]
    assert [
        event.render_policy
        for event in projected.narrative_plan.events
    ] == ["merge_adjacent", "standalone"]
    assert report["delivery_merge_policy_change_count"] == 1


def test_adjacent_structured_tasks_merge_without_losing_delivery_ids() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(
            scope_id="episode-1",
            events=[
                NarrativeEvent(
                    event_id="E1",
                    render_policy="merge_adjacent",
                ),
                NarrativeEvent(event_id="E2"),
            ],
        ),
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                shot_id="SH001",
                scene_id="SC01",
                event_ids=["E1"],
                story_event_id="E1",
                information_ids=["I1"],
                capacity_budget=ShotCapacityBudget(action_phase_s=2),
                duration_s=5,
            ),
            StoryboardOutlineShot(
                shot_no=2,
                shot_id="SH002",
                scene_id="SC01",
                event_ids=["E2"],
                story_event_id="E2",
                information_ids=["I2"],
                capacity_budget=ShotCapacityBudget(action_phase_s=2),
                duration_s=5,
            ),
        ],
    )

    changes = merge_outline_delivery_beats(outline, screenplay)

    assert len(changes) == 1
    assert len(outline.shots) == 1
    assert outline.shots[0].event_ids == ["E1", "E2"]
    assert outline.shots[0].information_ids == ["I1", "I2"]
    assert outline.shots[0].duration_s == 5


def test_right_event_cannot_retroactively_authorize_delivery_merge() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(
            scope_id="episode-1",
            events=[
                NarrativeEvent(
                    event_id="E1",
                    render_policy="standalone",
                    downstream_dependency_event_ids=["E2"],
                ),
                NarrativeEvent(
                    event_id="E2",
                    render_policy="merge_adjacent",
                ),
            ],
        ),
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                shot_id="SH001",
                scene_id="SC01",
                event_ids=["E1"],
                capacity_budget=ShotCapacityBudget(action_phase_s=2),
                duration_s=5,
            ),
            StoryboardOutlineShot(
                shot_no=2,
                shot_id="SH002",
                scene_id="SC01",
                event_ids=["E2"],
                capacity_budget=ShotCapacityBudget(action_phase_s=2),
                duration_s=5,
            ),
        ],
    )

    assert merge_outline_delivery_beats(outline, screenplay) == []
    assert [shot.event_ids for shot in outline.shots] == [["E1"], ["E2"]]
