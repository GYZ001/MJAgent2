"""evaluate_storyboard_for_confirmation 与确认幂等相关测试。"""
from __future__ import annotations

from types import SimpleNamespace

from app.domain.video_ops import (ConfirmationEvaluation,
                                  _is_storyboard_terminal_for_confirmation,
                                  evaluate_storyboard_for_confirmation)
from app.schemas import Bible, Character, Dialogue, EpisodeScreenplay, Shot, Storyboard, World
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


def test_automated_confirmation_accepts_fully_validated_confirming_checkpoint() -> None:
    episode = {"status": "scripting", "script_error": None}
    checkpoint = SimpleNamespace(
        phase="CONFIRMING", validated_prefix_end=11, expected_total=11,
    )

    assert _is_storyboard_terminal_for_confirmation(
        episode, checkpoint, shot_count=11, planned_shots=11,
        final_shot_valid=True, automated=True,
    )
    # Manual confirmation remains blocked while the supervisor is actively writing.
    assert not _is_storyboard_terminal_for_confirmation(
        episode, checkpoint, shot_count=11, planned_shots=11,
        final_shot_valid=True, automated=False,
    )

    stopped_episode = {"status": "scripted", "script_error": "stale confirmation error"}
    assert _is_storyboard_terminal_for_confirmation(
        stopped_episode, checkpoint, shot_count=11, planned_shots=11,
        final_shot_valid=True, automated=False,
    )


def test_automated_confirmation_rejects_incomplete_validated_prefix() -> None:
    episode = {"status": "scripting", "script_error": None}
    checkpoint = SimpleNamespace(
        phase="CONFIRMING", validated_prefix_end=10, expected_total=11,
    )

    assert not _is_storyboard_terminal_for_confirmation(
        episode, checkpoint, shot_count=11, planned_shots=11,
        final_shot_valid=True, automated=True,
    )


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


def test_dialogue_composition_is_confirmation_blocker_not_score_warning() -> None:
    ep = {"id": "ep1", "target_duration_s": 50}
    shot = _shot(
        shot_size="中景",
        characters=["萧炎", "测验员"],
        characters_visible=["萧炎", "测验员"],
    )

    result = evaluate_storyboard_for_confirmation(
        ep,
        Storyboard(episode_no=1, shots=[shot]),
        screenplay=None,
        bible=_minimal_bible(),
        has_real_bible=False,
    )

    assert any("只保留说话人" in error for error in result.errors)
    assert not any("只保留说话人" in warning for warning in result.warnings)
