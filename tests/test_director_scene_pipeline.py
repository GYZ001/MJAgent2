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
    Scene,
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
    _storyboard_scene_contiguity_key,
    validate_screenplay_source_coverage,
    validate_storyboard_direction_contract,
    validate_storyboard_outline_scene_alignment,
    validate_storyboard_screenplay_scene_alignment,
)
from app.storyboard_supervisor import _storyboard_scene_pack_batches


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


def test_direction_contract_allows_valid_split_action_continuation() -> None:
    boundary = "钟成抬手准备敲门完成，准备承接检查门窗的后续动作"
    first = _shot(
        1,
        focus="action",
        size="中景",
        move="跟随",
    ).model_copy(update={
        "primary_action": "钟成走到门前并抬手准备敲门",
        "state_out": boundary,
        "resulting_change": "钟成到达门前并开始确认屋内是否有人",
    })
    second = _shot(
        2,
        focus="action",
        size="中景",
        move="跟随",
    ).model_copy(update={
        "primary_action": "钟成拧动门把并抬眼检查紧闭的窗帘",
        "state_in": boundary,
        "state_out": "钟成确认门窗紧闭但屋内有人",
        "continuity_mode": "action_continuation",
        "resulting_change": "钟成到达门前并开始确认屋内是否有人",
    })
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=shot.shot_no,
                shot_id=shot.shot_id,
                scene_id="SC001",
                beat=f"动作容量拆分第 {shot.shot_no} 段",
                purpose=shot.purpose,
                resulting_change=shot.resulting_change,
                readability_focus="action",
                camera_size="中景",
                camera_angle=shot.camera_angle,
                camera_movement="跟随",
                camera_motivation=shot.camera_motivation,
                spine_beat_ids=["S01"],
            )
            for shot in (first, second)
        ],
        scene_contexts=[StoryboardSceneContext(
            scene_id="SC001",
            scene_no=1,
            scene_name="咖啡厅",
            scene_time="夜",
            entry_state="钟成来到门前准备确认屋内情况",
            exit_state="钟成确认门窗紧闭但屋内有人",
            transition_from_previous="沿钟成的行进动作进入门前",
            spatial_axis="钟成、门把与窗户保持同一横向轴线",
        )],
    )

    errors = validate_storyboard_direction_contract(
        Storyboard(episode_no=1, shots=[first, second]),
        outline,
    )

    assert not any("交付内容和结果几乎相同" in error for error in errors)


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


def test_direction_fields_are_derived_from_approved_outline() -> None:
    outline = _outline()
    outline.shots[1].resulting_change = (
        "谷言从桌边移动到门口，人物和空间位置发生变化"
    )
    outline.shots[2].resulting_change = (
        "谷言确认门外危险后由迟疑转为警觉"
    )
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot(
                index,
                focus=focus,
                size=size,
                move=move,
            )
            for index, (focus, size, move) in enumerate(
                [
                    ("context", "全景", "固定"),
                    ("action", "中景", "跟随"),
                    ("emotion", "近景", "固定"),
                ],
                start=1,
            )
        ],
    )
    for shot in board.shots:
        shot.purpose = ""
        shot.resulting_change = ""
        shot.readability_focus = ""
        shot.camera_angle = ""
        shot.camera_motivation = ""
        shot.context_requirement_ids = []

    changes = stages.normalize_storyboard_direction_fields(
        board,
        outline,
        EpisodeScreenplay(episode_no=1, title="E"),
    )

    assert changes
    assert board.shots[0].context_requirement_ids == ["CTX-SC001-01"]
    assert outline.shots[1].camera_size == "中景"
    assert outline.shots[1].camera_movement == "跟随"
    assert outline.shots[2].readability_focus == "emotion"
    assert validate_storyboard_direction_contract(board, outline) == []


def test_authority_scene_ids_allow_later_location_revisit() -> None:
    first = _shot(1, focus="context", size="全景", move="固定")
    middle = _shot(2, focus="context", size="全景", move="固定")
    revisit = _shot(3, focus="context", size="全景", move="固定")
    first.scene_id = "SC001"
    middle.scene_id = "SC002"
    middle.scene_name = "走廊"
    revisit.scene_id = "SC003"

    assert [
        _storyboard_scene_contiguity_key(
            shot,
            narrative_authority=True,
        )
        for shot in (first, middle, revisit)
    ] == ["SC001", "SC002", "SC003"]
    assert (
        _storyboard_scene_contiguity_key(
            first,
            narrative_authority=False,
        )
        == _storyboard_scene_contiguity_key(
            revisit,
            narrative_authority=False,
        )
    )


def test_scene_alignment_preserves_revisits_and_allows_nested_subscenes() -> None:
    scene_names = ["客厅", "办公室", "学校", "卧室", "高义家"]
    bible = Bible(
        characters=[],
        world=World(visual_style_canonical="写实动画"),
        scenes=[
            Scene(
                name=name,
                scene_canonical=f"{name}固定空间环境锚点",
            )
            for name in scene_names
        ],
    )
    expected = ["客厅", "办公室", "学校", "办公室", "卧室", "高义家", "高义家"]
    screenplay = EpisodeScreenplay(
        episode_no=1,
        title="E",
        scene_outline=[
            ScriptScene(
                scene_no=index,
                scene_heading=f"白天 / {name}",
                story_function=f"推进第{index}场",
                summary=f"第{index}场剧情",
            )
            for index, name in enumerate(expected, start=1)
        ],
    )
    actual = [
        "客厅",
        "卧室",
        "办公室",
        "高义家",
        "办公室",
        "学校",
        "办公室",
        "卧室",
        "高义家",
        "高义家",
    ]
    shots: list[Shot] = []
    briefs: list[StoryboardOutlineShot] = []
    for index, name in enumerate(actual, start=1):
        shot = _shot(index, focus="context", size="全景", move="固定")
        shot.scene_id = f"SC{index:02d}"
        shot.scene_name = name
        shot.scene_setting = f"白天，{name}"
        shots.append(shot)
        briefs.append(StoryboardOutlineShot(
            shot_no=index,
            scene_id=shot.scene_id,
            scene_name=name,
            scene_setting=f"白天，{name}",
            beat=f"第{index}镜推进剧情",
        ))

    assert validate_storyboard_screenplay_scene_alignment(
        Storyboard(episode_no=1, shots=shots),
        screenplay,
        bible,
    ) == []
    assert validate_storyboard_outline_scene_alignment(
        StoryboardOutline(episode_no=1, shots=briefs),
        screenplay,
        bible,
    ) == []


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


def test_scene_batches_follow_contiguous_scene_name_and_time_not_stale_id() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        scene_outline=[
            ScriptScene(
                scene_no=index,
                scene_heading=f"【场{index}】日 / 场景{index}",
                story_function=f"第{index}场承担独立剧情功能",
                summary=f"第{index}场剧情摘要",
            )
            for index in range(1, 4)
        ],
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=index,
                scene_id="SC01",
                scene_name=scene_name,
                scene_time=scene_time,
                beat=f"第{index}镜任务",
            )
            for index, (scene_name, scene_time) in enumerate([
                ("客厅", "晚上"),
                ("客厅", "晚上"),
                ("办公室", "白天"),
                ("办公室", "白天"),
                ("客厅", "晚上"),
            ], start=1)
        ],
    )

    changes = stages.ensure_storyboard_scene_contexts(outline, screenplay)

    assert len(changes) == 3
    assert [context.scene_id for context in outline.scene_contexts] == [
        "SC01", "SC02", "SC03",
    ]
    assert [shot.scene_id for shot in outline.shots] == [
        "SC01", "SC01", "SC02", "SC02", "SC03",
    ]


def test_extra_physical_scene_does_not_shift_screenplay_context_contracts() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="Home",
                story_function="Opening",
                summary="The story starts at home.",
                context_requirements=["Establish the family relationship."],
            ),
            ScriptScene(
                scene_no=2,
                scene_heading="School",
                story_function="Development",
                summary="The character reaches school.",
                context_requirements=["Establish the school destination."],
            ),
        ],
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_name="Home",
                scene_time="night",
                beat="Opening at home.",
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_name="Bedroom",
                scene_time="night",
                beat="An extra physical subscene.",
            ),
            StoryboardOutlineShot(
                shot_no=3,
                scene_name="School",
                scene_time="day",
                beat="Arrival at school.",
            ),
        ],
    )

    stages.ensure_storyboard_scene_contexts(outline, screenplay)

    assert [
        [
            requirement.description
            for requirement in context.context_requirements
        ]
        for context in outline.scene_contexts
    ] == [
        ["Establish the family relationship."],
        [],
        ["Establish the school destination."],
    ]


def test_long_scene_is_partitioned_into_bounded_model_outputs() -> None:
    context = StoryboardSceneContext(
        scene_id="SC001",
        scene_no=1,
        scene_name="长场景",
        scene_time="日",
        entry_state="场景开始",
        exit_state="场景结束",
    )
    outline = StoryboardOutline(
        episode_no=1,
        scene_contexts=[context],
        shots=[
            StoryboardOutlineShot(
                shot_no=shot_no,
                scene_id="SC001",
                beat=f"第 {shot_no} 镜",
            )
            for shot_no in range(1, 19)
        ],
    )

    batches = _storyboard_scene_pack_batches(
        outline,
        max_shots=8,
    )

    assert [
        sorted(batch["shot_nos"])
        for batch in batches
    ] == [
        list(range(1, 9)),
        list(range(9, 17)),
        [17, 18],
    ]
    assert [batch["key"] for batch in batches] == [
        "SC001:1-8",
        "SC001:9-16",
        "SC001:17-18",
    ]
