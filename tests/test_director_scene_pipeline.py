from __future__ import annotations

import asyncio

from app import stages
from app.production.screenplay_document import (
    document_to_screenplay,
    screenplay_to_document,
)
from app.schemas import (
    Bible,
    Character,
    EpisodeScreenplay,
    KeyDialogueChain,
    KeyDialogueTurn,
    NarrativeContinuityPlan,
    PlotSpine,
    PlotSpineBeat,
    ScriptScene,
    ShotCapacityBudget,
    ShotContribution,
    Shot,
    Storyboard,
    StoryboardContextRequirement,
    StoryboardOutline,
    StoryboardOutlineShot,
    StoryboardSceneContext,
    World,
)
from app.validators import (
    validate_screenplay_source_coverage,
    validate_storyboard_direction_contract,
)


def _shot(
    shot_no: int,
    *,
    focus: str,
    size: str,
    move: str,
    context_ids: list[str] | None = None,
) -> Shot:
    return Shot(
        shot_no=shot_no,
        shot_id=f"SH{shot_no:04d}",
        scene_id="SC001",
        duration_s=5,
        shot_size=size,
        camera_angle="平视侧面角度",
        camera_move=move,
        camera_motivation="让本镜主体、空间关系和戏剧变化清楚可读",
        scene_time="夜",
        scene_name="咖啡厅",
        characters=["谷言"],
        characters_visible=["谷言"],
        action_desc="谷言从桌边起身走向门口，在门前停下观察门外动静。",
        first_frame_desc="谷言坐在咖啡厅桌边，门位于画面右侧。",
        last_frame_desc="同一机位，谷言走到右侧门前停下。",
        source_excerpt="谷言从桌边起身，快步走到门口停下。",
        purpose="建立空间并推进谷言对门外危险的确认",
        resulting_change={
            "context": "观众明确咖啡厅、谷言和门的空间关系",
            "action": "谷言从等待转为主动走向门口确认危险",
            "emotion": "谷言确认危险后由迟疑转为警觉",
        }[focus],
        readability_focus=focus,
        context_requirement_ids=list(context_ids or []),
        spine_beat_ids=["S01"],
        prompt_contract_version="director_scene_pack_v1",
    )


def _outline() -> StoryboardOutline:
    briefs = [
        StoryboardOutlineShot(
            shot_no=index,
            shot_id=f"SH{index:04d}",
            scene_id="SC001",
            beat=f"第{index}镜承担独立剧情作用",
            purpose="承担本镜独立的导演和剧情交付作用",
            context_requirement_ids=["CTX-SC001-01"] if index == 1 else [],
            resulting_change=f"第{index}镜结束后状态发生变化",
            readability_focus=focus,
            camera_size=size,
            camera_angle="平视侧面角度",
            camera_movement=move,
            camera_motivation="让空间、动作或情绪变化清楚可读",
            spine_beat_ids=["S01"],
        )
        for index, (focus, size, move) in enumerate(
            [
                ("context", "全景", "固定"),
                ("action", "中景", "跟随"),
                ("emotion", "近景", "固定"),
            ],
            start=1,
        )
    ]
    return StoryboardOutline(
        episode_no=1,
        shots=briefs,
        scene_contexts=[
            StoryboardSceneContext(
                scene_id="SC001",
                scene_no=1,
                scene_name="咖啡厅",
                scene_time="夜",
                entry_state="谷言坐在桌边等待，门位于右侧",
                exit_state="谷言走到门前确认危险逼近",
                transition_from_previous="雨声延续进入咖啡厅",
                spatial_axis="桌、门与谷言保持同一横向轴线",
                context_requirements=[
                    StoryboardContextRequirement(
                        requirement_id="CTX-SC001-01",
                        description="建立咖啡厅、谷言与门的空间关系",
                        required_before_shot_no=2,
                    )
                ],
            )
        ],
    )


def test_source_coverage_is_exhaustive_and_round_trips() -> None:
    source = "第一段发生关键事件。\n\n第二段补充人物关系。"
    screenplay = EpisodeScreenplay(
        episode_no=1,
        plot_spine=PlotSpine(
            episode_premise="谷言必须确认门外危险",
            spine_beats=[
                PlotSpineBeat(
                    beat_id="S01",
                    who="谷言",
                    does="走到门前确认危险",
                    turn="等待转为行动",
                    source_segment_ids=["SRC0001", "SRC0002"],
                )
            ],
            must_keep_ending="谷言确认危险已经来到门外",
        ),
        source_coverage=[
            {
                "source_segment_id": "SRC0001",
                "disposition": "deliver",
                "beat_ids": ["S01"],
            },
            {
                "source_segment_id": "SRC0002",
                "disposition": "context",
                "reason": "作为人物关系和行动动机保留",
            },
        ],
    )

    assert validate_screenplay_source_coverage(screenplay, source) == []
    restored = document_to_screenplay(screenplay_to_document(screenplay))
    assert [item.source_segment_id for item in restored.source_coverage] == [
        "SRC0001",
        "SRC0002",
    ]

    screenplay.source_coverage.pop()
    errors = validate_screenplay_source_coverage(screenplay, source)
    assert any("SRC0002" in error and "漏掉" in error for error in errors)


def test_direction_contract_requires_context_and_camera_readability() -> None:
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot(1, focus="context", size="全景", move="固定", context_ids=["CTX-SC001-01"]),
            _shot(2, focus="action", size="中景", move="跟随"),
            _shot(3, focus="emotion", size="近景", move="固定"),
        ],
    )

    assert validate_storyboard_direction_contract(board, _outline()) == []

    board.shots[1].camera_angle = ""
    board.shots[2].shot_size = "全景"
    errors = validate_storyboard_direction_contract(board, _outline())
    assert any("camera_angle" in error for error in errors)
    assert any("情绪转折" in error for error in errors)


def test_scene_pack_hydrates_director_fields_without_per_shot_calls(monkeypatch) -> None:
    outline = _outline()
    context = outline.scene_contexts[0]
    bible = Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="二十八岁男性，黑色短发，深灰西装，佩戴银色手表",
            )
        ],
        world=World(visual_style_canonical="都市国漫厚涂风，统一电影光影"),
    )
    screenplay = EpisodeScreenplay(
        episode_no=1,
        full_script_text="【场1】夜 / 咖啡厅\n谷言从桌边起身，快步走到门口停下。",
    )
    source = "谷言从桌边起身，快步走到门口停下。"

    async def fake_loop(*_args, **_kwargs):
        return stages.DirectedScenePackDraft(
            episode_no=1,
            scene_id="SC001",
            shots=[
                stages.DirectedSceneShotDraft(
                    shot_no=brief.shot_no,
                    purpose=brief.purpose,
                    context_requirement_ids=brief.context_requirement_ids,
                    resulting_change=brief.resulting_change,
                    readability_focus=brief.readability_focus,
                    duration_s=5,
                    shot_size=brief.camera_size,
                    camera_angle=brief.camera_angle,
                    camera_move=brief.camera_movement,
                    camera_motivation=brief.camera_motivation,
                    characters=["谷言"],
                    action_desc="谷言从桌边起身走向门口，在门前停下观察外面。",
                    first_frame_desc="谷言位于咖啡厅桌边，门在画面右侧。",
                    last_frame_desc="同一机位，谷言走到右侧门前停下。",
                    source_excerpt=source,
                )
                for brief in outline.shots
            ],
        )

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)
    pack = asyncio.run(stages.generate_storyboard_scene_pack(
        {"id": "e1", "episode_no": 1, "target_duration_s": 50},
        source,
        bible,
        screenplay,
        outline,
        context,
    ))

    assert [shot.shot_no for shot in pack.shots] == [1, 2, 3]
    assert pack.shots[1].camera_move == "跟随"
    assert pack.shots[1].camera_angle == "平视侧面角度"
    assert pack.shots[1].prompt_contract_version == "director_scene_pack_v2"


def test_scene_pack_model_contract_only_requires_creative_fields() -> None:
    required = set(
        stages.DirectedSceneShotDraft.model_json_schema().get("required") or []
    )

    assert required == {
        "shot_no",
        "shot_size",
        "camera_angle",
        "camera_move",
        "camera_motivation",
        "action_desc",
        "first_frame_desc",
        "last_frame_desc",
    }
    assert required.isdisjoint({
        "purpose",
        "duration_s",
        "characters",
        "dialogues",
        "source_excerpt",
        "context_requirement_ids",
        "readability_focus",
    })


def test_narrative_scene_pack_hydrates_authority_without_per_shot_model_fields() -> None:
    source = "谷言从桌边起身，快步走到门口停下。他压低声音说门外有人。"
    screenplay = EpisodeScreenplay(
        episode_no=1,
        full_script_text="【场1】夜 / 咖啡厅\n谷言从桌边起身。谷言：门外有人。",
        events=[{
            "event_id": "E1",
            "source_span": source,
        }],
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】夜 / 咖啡厅",
                story_function="确认门外危险",
                summary="谷言走到门前确认危险。",
                entry_state="谷言坐在桌边等待",
                exit_state="谷言停在门前保持警觉",
                context_requirements=["建立谷言、桌子与门的空间关系"],
            ),
        ],
        plot_spine=PlotSpine(
            episode_premise="谷言确认门外危险",
            spine_beats=[
                PlotSpineBeat(
                    beat_id="S01",
                    who="谷言",
                    does="走到门前确认危险",
                    source_segment_ids=["SRC0001"],
                )
            ],
            must_keep_ending="谷言确认门外有人",
        ),
        key_lines=["谷言：门外有人。"],
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="确认危险",
                turns=[
                    KeyDialogueTurn(
                        speaker="谷言",
                        line="门外有人。",
                        source_text="他压低声音说门外有人。",
                    )
                ],
            )
        ],
        narrative_plan=NarrativeContinuityPlan(
            scope_id="e1",
        ),
    )
    bible = Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="二十八岁男性，黑色短发，深灰西装，佩戴银色手表",
            )
        ],
        world=World(visual_style_canonical="都市国漫厚涂风，统一电影光影"),
    )
    budget = ShotCapacityBudget(
        spoken_and_text_s=2.0,
        action_phase_s=3.0,
    )
    contribution = ShotContribution(
        shot_contribution_id="SCONTRIB-SH001",
        evidence_ids=["EV1"],
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                shot_id="SH001",
                scene_id="SC001",
                event_ids=["E-1"],
                story_event_id="E-1",
                spine_beat_ids=["S01"],
                key_line_ids=["KL01"],
                visible_entity_ids=["谷言"],
                characters_visible=["谷言"],
                capacity_budget=budget,
                shot_contribution=contribution,
                scene_time="夜",
                scene_name="咖啡厅",
                beat="谷言走到门前确认危险并压低声音示警",
                state_in="谷言坐在桌边等待",
                primary_action="谷言起身走到门前停下",
                state_out="谷言停在门前保持警觉",
                continuity_mode="scene_change",
                duration_s=7,
            )
        ],
    )

    changes = stages.ensure_storyboard_scene_contexts(outline, screenplay)
    draft = stages.DirectedScenePackDraft(
        episode_no=1,
        scene_id=outline.scene_contexts[0].scene_id,
        shots=[
            stages.DirectedSceneShotDraft(
                shot_no=1,
                shot_size="中景",
                camera_angle="平视侧面角度",
                camera_move="跟随",
                camera_motivation="完整看清谷言从桌边走到门前的动作路径",
                action_desc="谷言从桌边起身走向门口，在门前停下并压低声音示警。",
                first_frame_desc="谷言坐在咖啡厅桌边，门位于画面右侧。",
                last_frame_desc="同一机位，谷言停在右侧门前保持警觉。",
                dialogue_emotions={"KL01": "警觉"},
            )
        ],
    )
    pack = stages._hydrate_directed_scene_pack(
        draft,
        outline=outline,
        source_text=source,
        screenplay=screenplay,
        bible=bible,
    )

    assert changes and outline.scene_contexts[0].scene_id == "SC01"
    shot = pack.shots[0]
    assert shot.duration_s == 7
    assert shot.shot_id == "SH001"
    assert shot.event_ids == ["E-1"]
    assert shot.story_event_id == "E1"
    assert shot.capacity_budget == budget
    assert shot.shot_contribution == contribution
    assert shot.characters == ["谷言"]
    assert [(item.speaker, item.line) for item in shot.dialogues] == [
        ("谷言", "门外有人。"),
    ]
    assert shot.source_excerpt in source
    assert shot.context_requirement_ids == ["CTX-SC01-01"]
    assert shot.is_final is True


def test_scene_pack_preserves_offscreen_voice_without_forcing_speaker_visible() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        key_lines=["门外人：别动。"],
        narrative_plan=NarrativeContinuityPlan(scope_id="e1"),
    )
    bible = Bible(
        characters=[
            Character(
                name=name,
                role="角色",
                appearance_canonical=f"成年{name}，黑色短发，深色常服，外观稳定清晰",
            )
            for name in ("谷言", "门外人")
        ],
        world=World(visual_style_canonical="都市国漫"),
    )
    brief = StoryboardOutlineShot(
        shot_no=1,
        key_line_ids=["KL01"],
        characters_visible=["谷言"],
        visible_entity_ids=["谷言", "门外人"],
        audio_cast=["门外人"],
    )

    dialogues = stages._scene_pack_dialogues(
        brief,
        screenplay,
        {},
        bible=bible,
    )
    characters = stages._scene_pack_characters(
        brief,
        dialogues,
        bible=bible,
        screenplay=screenplay,
        fallback=[],
    )

    assert len(dialogues) == 1
    assert dialogues[0].speaker == "门外人"
    assert dialogues[0].delivery == "offscreen_voice"
    assert characters == ["谷言"]


def test_scene_pack_normalizes_same_scene_continuity() -> None:
    screenplay = EpisodeScreenplay(episode_no=1)
    bible = Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="成年男性，黑色短发，深色常服，外观稳定清晰",
            )
        ],
        world=World(visual_style_canonical="都市国漫"),
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=number,
                shot_id=f"SH00{number}",
                scene_id="SC001",
                scene_name="咖啡厅",
                scene_time="夜",
                continuity_mode="scene_change",
                duration_s=5,
                characters_visible=["谷言"],
            )
            for number in (1, 2)
        ],
    )
    draft = stages.DirectedScenePackDraft(
        episode_no=1,
        scene_id="SC001",
        shots=[
            stages.DirectedSceneShotDraft(
                shot_no=number,
                shot_size="中景",
                camera_angle="平视",
                camera_move="固定",
                camera_motivation="保持空间和动作方向稳定可读",
                action_desc=f"谷言在咖啡厅完成第{number}个连续动作并停下。",
                first_frame_desc=f"谷言准备执行第{number}个动作。",
                last_frame_desc=f"同一机位，谷言完成第{number}个动作。",
                source_excerpt="谷言从桌边起身，快步走到门口停下。",
            )
            for number in (1, 2)
        ],
    )

    pack = stages._hydrate_directed_scene_pack(
        draft,
        outline=outline,
        source_text="谷言从桌边起身，快步走到门口停下。",
        screenplay=screenplay,
        bible=bible,
    )

    assert pack.shots[0].continuity_mode == "scene_change"
    assert pack.shots[1].continuity_mode == "same_scene_cut"
    assert pack.shots[1].transition == "硬切"


def test_scene_pack_prompt_contains_all_planned_scene_names(monkeypatch) -> None:
    outline = _outline()
    outline.scene_contexts.append(StoryboardSceneContext(
        scene_id="SC002",
        scene_no=2,
        scene_name="第二场办公室",
        scene_time="日",
        entry_state="角色进入办公室",
        exit_state="角色离开办公室",
    ))
    outline.shots.append(StoryboardOutlineShot(
        shot_no=4,
        shot_id="SH0004",
        scene_id="SC002",
        scene_name="第二场办公室",
        scene_time="日",
        beat="第二场独立任务",
    ))
    bible = Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="成年男性，黑色短发，深色常服，外观稳定清晰",
            )
        ],
        world=World(visual_style_canonical="都市国漫"),
    )
    screenplay = EpisodeScreenplay(
        episode_no=1,
        full_script_text="谷言从桌边起身，快步走到门口停下。",
    )
    captured: dict[str, str] = {}

    async def fake_loop(*args, **_kwargs):
        captured["prompt"] = args[2]
        return stages.DirectedScenePackDraft(
            episode_no=1,
            scene_id="SC001",
            shots=[
                stages.DirectedSceneShotDraft(
                    shot_no=brief.shot_no,
                    shot_size=brief.camera_size,
                    camera_angle=brief.camera_angle,
                    camera_move=brief.camera_movement,
                    camera_motivation=brief.camera_motivation,
                    action_desc="谷言从桌边起身走向门口，在门前停下观察外面。",
                    first_frame_desc="谷言位于咖啡厅桌边，门在画面右侧。",
                    last_frame_desc="同一机位，谷言走到右侧门前停下。",
                    source_excerpt="谷言从桌边起身，快步走到门口停下。",
                    characters=["谷言"],
                )
                for brief in outline.shots
                if brief.scene_id == "SC001"
            ],
        )

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)
    asyncio.run(stages.generate_storyboard_scene_pack(
        {"id": "e1", "episode_no": 1, "target_duration_s": 50},
        "谷言从桌边起身，快步走到门口停下。",
        bible,
        screenplay,
        outline,
        outline.scene_contexts[0],
    ))

    assert "第二场办公室" in captured["prompt"]
