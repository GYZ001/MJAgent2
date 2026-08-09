"""evaluate_storyboard_for_confirmation 与确认幂等相关测试。"""
from __future__ import annotations

from types import SimpleNamespace

from app.domain.video_ops import (ConfirmationEvaluation,
                                  _is_storyboard_terminal_for_confirmation,
                                  _storyboard_operational_projection_errors,
                                  _storyboard_structural_errors,
                                  evaluate_storyboard_for_confirmation)
from app.schemas import (Bible, Character, Dialogue, EpisodeScreenplay, Scene,
                         ScriptScene, Shot, Storyboard, StoryEvent, World)
from app.validators import validate_storyboard_preserves_key_content


def _minimal_bible() -> Bible:
    return Bible(
        characters=[
            Character(name="萧炎", role="主角", appearance_canonical="黑发少年", personality="倔强"),
        ],
        world=World(era="玄幻", genre="玄幻", visual_style_canonical="国风厚涂"),
    )


def _shot(no: int = 1, **kwargs) -> Shot:
    base = dict(
        shot_no=no,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        scene_setting="日，广场",
        characters=["萧炎"],
        action_desc="萧炎站在测验石碑前测验斗气，碑面只亮起三段斗气微光，他被当众羞辱攥拳。",
        first_frame_desc="萧炎手贴石碑，神情紧绷，准备开始测验。",
        last_frame_desc="同一机位，石碑仅亮三段，萧炎攥拳垂眸。",
        source_excerpt="测验石碑只亮起三段斗气，全场哗然议论纷纷。",
        dialogues=[Dialogue(speaker="萧炎", line="三年斗气十段？", emotion="愤怒")],
        is_final=True,
    )
    base.update(kwargs)
    return Shot(**base)


def test_manual_confirmation_accepts_succeeded_checkpoint() -> None:
    episode = {"status": "scripted", "script_error": None}
    checkpoint = SimpleNamespace(
        phase="SUCCEEDED", validated_prefix_end=11, expected_total=11,
    )

    assert _is_storyboard_terminal_for_confirmation(
        episode, checkpoint, shot_count=11, planned_shots=11,
        final_shot_valid=True,
    )

    running_episode = {"status": "scripting", "script_error": None}
    assert not _is_storyboard_terminal_for_confirmation(
        running_episode, checkpoint, shot_count=11, planned_shots=11,
        final_shot_valid=True,
    )

    failed_episode = {"status": "scripted", "script_error": "门禁未通过"}
    assert not _is_storyboard_terminal_for_confirmation(
        failed_episode, checkpoint, shot_count=11, planned_shots=11,
        final_shot_valid=True,
    )
    confirmed_episode = {"status": "confirmed", "script_error": None}
    assert _is_storyboard_terminal_for_confirmation(
        confirmed_episode, checkpoint, shot_count=11, planned_shots=11,
        final_shot_valid=True,
    )


def test_manual_confirmation_rejects_incomplete_validated_prefix() -> None:
    episode = {"status": "scripted", "script_error": None}
    checkpoint = SimpleNamespace(
        phase="SUCCEEDED", validated_prefix_end=10, expected_total=11,
    )

    assert not _is_storyboard_terminal_for_confirmation(
        episode, checkpoint, shot_count=11, planned_shots=11,
        final_shot_valid=True,
    )


def test_structural_gate_accepts_canonical_scene_fields_without_legacy_setting() -> None:
    shot = _shot(scene_time="日", scene_name="广场", scene_setting="")

    errors = _storyboard_structural_errors(
        Storyboard(episode_no=1, shots=[shot])
    )

    assert not any("scene_name" in error for error in errors)


def test_operational_projection_is_a_hard_structure_contract() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        events=[StoryEvent(event_id="E1")],
    )
    invalid = Storyboard(
        episode_no=1,
        shots=[
            _shot(
                story_event_id="E-1",
                scene_time="白天",
                scene_name="办公室",
                continuity_mode="scene_change",
                transition="叠化",
                is_final=False,
            ),
            _shot(
                2,
                story_event_id="E-1",
                scene_time="白天",
                scene_name="办公室",
                continuity_mode="scene_change",
                transition="叠化",
            ),
        ],
    )

    errors = _storyboard_operational_projection_errors(
        invalid,
        screenplay,
    )

    assert sum("STORYBOARD_OPERATIONAL_EVENT_ID_INVALID" in e for e in errors) == 2
    assert any("STORYBOARD_OPERATIONAL_CONTINUITY_INVALID" in e for e in errors)

    for shot in invalid.shots:
        shot.story_event_id = "E1"
    invalid.shots[1].continuity_mode = "same_scene_cut"
    invalid.shots[1].transition = "硬切"
    assert _storyboard_operational_projection_errors(
        invalid,
        screenplay,
    ) == []


def test_evaluate_is_readonly_and_returns_structured_result():
    ep = {"id": "ep1", "target_duration_s": 50}
    board = Storyboard(episode_no=1, shots=[_shot()])
    screenplay = EpisodeScreenplay(
        episode_no=1,
        key_lines=["三年斗气十段？"],
        key_plot_points=["萧炎测验只剩三段斗气被当众羞辱"],
    )
    result = evaluate_storyboard_for_confirmation(
        ep, board, screenplay, _minimal_bible(), has_real_bible=False,
    )
    assert isinstance(result, ConfirmationEvaluation)
    assert isinstance(result.errors, list)
    assert isinstance(result.issues, list)
    # 同源：与 preserves_key_content 一致读取有效口播
    key_errs = validate_storyboard_preserves_key_content(result.board, screenplay)
    assert not any("主线台词" in e for e in key_errs)


def test_prompt_compile_probe_does_not_mutate_evaluation_board() -> None:
    shot = _shot(
        reference_roles=[],
        prompt_contract_version="renderability_v1",
    )

    result = evaluate_storyboard_for_confirmation(
        {"id": "ep1", "target_duration_s": 50},
        Storyboard(episode_no=1, shots=[shot]),
        screenplay=None,
        bible=_minimal_bible(),
        has_real_bible=True,
        record_metrics=False,
    )

    assert result.board.shots[0].reference_roles == []
    assert result.board.shots[0].prompt_contract_version == "renderability_v1"


def test_dialogue_composition_is_score_warning_not_confirmation_blocker() -> None:
    ep = {"id": "ep1", "target_duration_s": 50}
    shot = _shot(
        shot_size="中景",
        characters=["萧炎", "functional:observer"],
        characters_visible=["萧炎", "functional:observer"],
    )

    result = evaluate_storyboard_for_confirmation(
        ep,
        Storyboard(episode_no=1, shots=[shot]),
        screenplay=None,
        bible=_minimal_bible(),
        has_real_bible=False,
    )

    assert not any("只保留说话人" in error for error in result.errors)
    assert any("只保留说话人" in warning for warning in result.warnings)


def test_episode_scene_alignment_is_confirmation_blocker() -> None:
    bible = _minimal_bible()
    bible.scenes.extend([
        Scene(name="萧家广场", scene_canonical="石碑与青石地面固定空间锚点"),
        Scene(name="萧家后山", scene_canonical="山崖与树林固定空间锚点"),
    ])
    screenplay = EpisodeScreenplay(
        episode_no=1,
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="白天 / 萧家广场",
                story_function="萧炎完成斗气测验",
                summary="萧炎在广场石碑前接受测验。",
            ),
        ],
    )
    shot = _shot(
        scene_time="白天",
        scene_name="萧家后山",
        scene_setting="白天，萧家后山",
    )

    result = evaluate_storyboard_for_confirmation(
        {"id": "ep1", "target_duration_s": 50},
        Storyboard(episode_no=1, shots=[shot]),
        screenplay,
        bible,
        has_real_bible=False,
    )

    assert not result.passed
    assert any("与本集剧本不一致" in error for error in result.errors)
