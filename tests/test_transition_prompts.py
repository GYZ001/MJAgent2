from app.compiler import compile_prompt, compile_scene_prompt
from app.schemas import Bible, Character, Shot, Storyboard, TRANSITIONS, World
from app.validators import normalize_continuity


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="萧炎",
                role="主角",
                appearance_canonical="十五岁少年，黑发束起，黑色劲装，眉眼倔强",
                personality="坚韧",
            ),
            Character(
                name="萧薰儿",
                role="重要配角",
                appearance_canonical="十五岁少女，长发垂肩，淡青衣裙，眼神清澈",
                personality="温柔",
            ),
        ],
        world=World(era="玄幻古代", genre="玄幻", visual_style_canonical="国风玄幻漫剧厚涂风，暖冷对比光"),
    )


def _shot(shot_no: int, scene: str, chars: list[str], action: str, **kwargs) -> Shot:
    return Shot(
        shot_no=shot_no,
        duration_s=10,
        shot_size="中景",
        camera_move="固定",
        scene_setting=scene,
        characters=chars,
        action_desc=action,
        source_excerpt="萧炎沉默片刻，萧薰儿怔住，话音仍在耳边回荡。",
        narration=kwargs.pop("narration", "萧炎的话音仍在耳边回荡。"),
        **kwargs,
    )


def test_transition_options_include_scene_cut_styles() -> None:
    assert {"淡出淡入", "闪白", "甩镜", "遮挡转场", "匹配剪辑", "声音延续+叠化"} <= TRANSITIONS


def test_normalize_continuity_chooses_emotional_scene_transition() -> None:
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot(1, "首日上午，广场", ["萧炎", "萧薰儿"], "萧炎转身离开，萧薰儿怔住，眼眶泛红。"),
            _shot(2, "首日傍晚，藏书阁", ["萧薰儿"], "萧薰儿独坐书案前，萧炎的话音仍在耳边回响。", transition="硬切"),
        ],
    )

    normalize_continuity(board)

    assert board.shots[0].transition == "硬切"
    assert board.shots[1].transition == "声音延续+叠化"
    assert board.shots[1].continuity_from_prev is False


def test_video_prompt_contains_incoming_and_outgoing_transition() -> None:
    bible = _bible()
    shot = _shot(4, "首日上午，广场", ["萧炎", "萧薰儿"], "萧炎背对石碑停住脚步，萧薰儿怔在原地。")

    prompt = compile_prompt(
        shot,
        bible,
        incoming_transition="声音延续+叠化",
        outgoing_transition="淡出淡入",
        next_scene="首日傍晚，藏书阁",
        next_first_frame_desc="藏书阁暖黄灯光下，萧薰儿独坐书案前。",
    )

    assert "本镜开头转场" in prompt
    assert "声音延续+叠化" in prompt
    assert "本镜结尾转场" in prompt
    assert "淡出淡入" in prompt
    assert "首日傍晚，藏书阁" in prompt


def test_tail_keyframe_prompt_contains_transition_tail_requirement() -> None:
    bible = _bible()
    shot = _shot(
        4,
        "首日上午，广场",
        ["萧炎", "萧薰儿"],
        "萧炎背对石碑停住脚步，萧薰儿怔在原地。",
        last_frame_desc="萧薰儿眼眶发红，广场人声渐弱。",
    )

    prompt = compile_scene_prompt(
        shot,
        bible,
        kind="tail",
        outgoing_transition="闪白",
        next_scene="首日傍晚，藏书阁",
    )

    assert "转场尾帧要求" in prompt
    assert "闪白" in prompt
