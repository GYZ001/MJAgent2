from __future__ import annotations


from app.production.screenplay_document import (
    document_to_screenplay,
    screenplay_to_document,
)
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    PlotSpine,
    PlotSpineBeat,
    Scene,
    ScriptScene,
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
    score_storyboard_direction_readability,
    validate_screenplay_source_coverage,
    validate_storyboard_direction_contract,
    validate_storyboard_outline_scene_alignment,
    validate_storyboard_screenplay_scene_alignment,
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


def test_direction_contract_allows_distinct_structured_contribution_gain() -> None:
    first = _shot(1, focus="context", size="全景", move="固定").model_copy(update={
        "resulting_change": "观众确认门外危险已经逼近",
        "shot_contribution": ShotContribution(
            shot_contribution_id="SCONTRIB-SH0001",
            evidence_ids=["EV-DOOR"],
        ),
    })
    second = _shot(2, focus="action", size="中景", move="跟随").model_copy(update={
        "resulting_change": "观众确认门外危险已经逼近",
        "shot_contribution": ShotContribution(
            shot_contribution_id="SCONTRIB-SH0002",
            story_delta_fact_ids=["FACT-DOOR-LOCKED"],
            character_state_delta_ids=["CSD-GUYAN-ALERT"],
        ),
    })
    outline = _outline()
    outline.shots = outline.shots[:2]

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
    warnings = score_storyboard_direction_readability(board, _outline())
    assert any("情绪转折" in warning for warning in warnings)


def test_direction_readability_preferences_are_score_only() -> None:
    outline = _outline()
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot(1, focus="context", size="全景", move="固定", context_ids=["CTX-SC001-01"]),
            _shot(2, focus="action", size="近景", move="固定"),
            _shot(3, focus="emotion", size="全景", move="横摇"),
        ],
    )

    assert validate_storyboard_direction_contract(board, outline) == []
    warnings = score_storyboard_direction_readability(board, outline)
    assert any("空间可读镜头" in warning for warning in warnings)
    assert any("情绪可读镜头" in warning for warning in warnings)


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


