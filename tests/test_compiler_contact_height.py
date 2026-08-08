"""接触动作侧面机位 + 同框同身高：编译期确定性规则。"""
from app.compiler import (
    compile_prompt,
    compile_scene_prompt,
    has_contact_action,
    has_explicit_height_difference,
)
from app.schemas import Bible, Character, Shot, World


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="萧炎",
                role="主角",
                appearance_canonical=(
                    "十五岁左右男性少年，墨色利落短发，常穿灰黑色劲装，"
                    "左手指戴古朴黑色戒指，面容清秀眼神坚韧"
                ),
                personality="坚韧",
            ),
            Character(
                name="萧薰儿",
                role="女主",
                appearance_canonical=(
                    "十五岁左右少女，青丝挽成云髻，月白襦裙配淡金纹样，"
                    "眉目清丽，气质温婉沉静"
                ),
                personality="聪慧",
            ),
        ],
        world=World(
            era="架空玄幻古代",
            genre="高武玄幻",
            visual_style_canonical="3D国漫风，光线层次分明，色调浓郁鲜亮",
        ),
    )


def _contact_shot(**kwargs) -> Shot:
    base = dict(
        shot_no=1,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        scene_setting="日，萧家测验广场",
        characters=["萧炎", "萧薰儿"],
        action_desc="萧炎抬手按住石碑，萧薰儿侧身注视。",
        first_frame_desc="萧炎手掌刚贴上石碑。",
        last_frame_desc="萧炎手掌仍按住石碑，碑面亮起。",
        state_in="萧炎抬起右手靠近石碑，萧薰儿立于一侧。",
        primary_action="萧炎抬手按住冰冷石碑，碑面光纹扩散。",
        state_out="萧炎手掌仍按住石碑，碑面亮起。",
        continuity_mode="same_scene_cut",
        risk_tags=["contact_phase:established"],
        source_excerpt="萧炎抬起手掌，轻轻按在了那块黑色石碑之上。",
    )
    base.update(kwargs)
    return Shot(**base)


def test_has_contact_action_detects_press() -> None:
    assert has_contact_action(_contact_shot()) is True
    assert has_contact_action(_contact_shot(
        primary_action="萧炎独自站在广场中央环顾四周。",
        action_desc="萧炎独自站在广场中央环顾四周。",
        state_in="萧炎站在广场中央。",
        state_out="萧炎仍站在广场中央。",
        first_frame_desc="萧炎站在广场中央。",
        last_frame_desc="萧炎环顾四周。",
        risk_tags=[],
    )) is False


def test_compile_prompt_forces_side_view_for_contact() -> None:
    shot = _contact_shot(camera_angle="平视")
    prompt = compile_prompt(shot, _bible())
    assert "[CAMERA]" in prompt
    assert "侧面" in prompt
    assert "已接触动作必须从互动轴侧面拍摄" in prompt
    assert "接触动作禁止正面摆拍" in prompt
    assert shot.camera_angle == "侧面"


def test_compile_prompt_normalizes_typed_contact_to_side_axis() -> None:
    shot = _contact_shot(camera_angle="侧面俯视")
    compile_prompt(shot, _bible())
    assert shot.camera_angle == "侧面"


def test_compile_prompt_equal_height_for_multi_character() -> None:
    prompt = compile_prompt(_contact_shot(), _bible())
    assert "站立身高与眼线尽量齐平" in prompt
    assert "禁止同框人物随意一高一低" in prompt


def test_compile_prompt_skips_equal_height_when_diff_stated() -> None:
    shot = _contact_shot(
        primary_action="萧薰儿仰头看高他一头的萧炎，伸手扶住他手臂。",
        action_desc="萧薰儿仰头看高他一头的萧炎，伸手扶住他手臂。",
        first_frame_desc="萧薰儿仰头看高他一头的萧炎。",
        last_frame_desc="萧薰儿手扶住萧炎手臂。",
        risk_tags=["contact_phase:established", "explicit_height_difference"],
    )
    assert has_explicit_height_difference(shot, _bible()) is True
    prompt = compile_prompt(shot, _bible())
    assert "站立身高与眼线尽量齐平" not in prompt
    assert "禁止同框人物随意一高一低" not in prompt


def test_compile_prompt_skips_equal_height_for_single_character() -> None:
    shot = _contact_shot(characters=["萧炎"])
    prompt = compile_prompt(shot, _bible())
    assert "站立身高与眼线尽量齐平" not in prompt


def test_compile_scene_prompt_contact_side_and_height() -> None:
    prompt = compile_scene_prompt(_contact_shot(), _bible(), kind="head")
    assert "侧面视角构图" in prompt or "机位：侧面" in prompt
    assert "站立身高与眼线齐平" in prompt
    assert "接触类动作优先侧面构图" in prompt
