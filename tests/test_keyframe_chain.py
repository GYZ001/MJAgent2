from app.compiler import compile_scene_prompt
from app.schemas import Bible, Character, Shot, World
import pytest

from app.worker import required_keyframe_kinds, scene_generation_kinds


def _shot(shot_no: int, continuity: bool) -> dict:
    return {"shot_no": shot_no, "continuity_from_prev": int(continuity)}


def test_keyframe_requirements_for_scene_heads_and_continuous_shots() -> None:
    assert required_keyframe_kinds(_shot(1, False)) == ["head", "tail"]
    assert required_keyframe_kinds(_shot(2, False)) == ["head", "tail"]
    assert required_keyframe_kinds(_shot(2, True)) == ["tail"]


def test_scene_generation_kinds_can_target_one_required_keyframe() -> None:
    assert scene_generation_kinds(_shot(1, False), ["tail"]) == ["tail"]
    assert scene_generation_kinds(_shot(1, False), ["head"]) == ["head"]
    assert scene_generation_kinds(_shot(1, False), None) == ["head", "tail"]


def test_scene_generation_kinds_rejects_unneeded_head_for_continuous_shot() -> None:
    with pytest.raises(ValueError):
        scene_generation_kinds(_shot(2, True), ["head"])


def test_tail_keyframe_prompt_targets_ending_moment() -> None:
    bible = Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="二十八岁男性，黑色短发，深灰西装，腕戴银色手表",
                personality="冷静",
            )
        ],
        world=World(era="现代", genre="都市", visual_style_canonical="都市漫剧厚涂风，柔和侧光"),
    )
    shot = Shot(
        shot_no=2,
        duration_s=10,
        shot_size="中景",
        camera_move="推近",
        scene_setting="当日，咖啡厅",
        characters=["谷言"],
        action_desc="谷言攥紧纸杯看向门口，脸色沉下去",
        source_excerpt="谷言攥紧纸杯看向门口。",
        continuity_from_prev=True,
    )

    prompt = compile_scene_prompt(shot, bible, kind="tail")

    assert "结束的瞬间" in prompt
    assert "动作结果清晰可见" in prompt
