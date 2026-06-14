from app.schemas import Dialogue, EpisodeScreenplay, Shot, Storyboard
from app.validators import validate_storyboard_soundtrack


def _shot(no: int, narration: str | None = None, dialogues: list[Dialogue] | None = None) -> Shot:
    return Shot(
        shot_no=no,
        duration_s=10,
        shot_size="中景",
        camera_move="固定",
        scene_setting="日，萧家广场",
        characters=["萧炎"],
        action_desc="萧炎站在测验石碑前攥紧手掌，萧炎听见周围议论后低下眼，掌心血痕慢慢渗出。",
        first_frame_desc="萧炎站在测验石碑前，手掌贴着碑面，神情平静。",
        last_frame_desc="同一机位，萧炎手掌攥成拳，指缝渗出血迹。",
        source_excerpt="少年面无表情，唇角有着一抹自嘲。",
        narration=narration,
        dialogues=dialogues or [],
    )


def _screenplay() -> EpisodeScreenplay:
    return EpisodeScreenplay(
        episode_no=1,
        title="陨落的天才",
        logline="萧炎测出三段斗之气后遭到嘲讽，萧薰儿仍坚定站在他身边。",
        script_format_note="标准影视台本格式",
        full_script_text="\n\n".join([
            "【场1】日 / 萧家广场",
            "人群中爆发出嘲讽声：“三段？这废物真是把家族的脸都丢光了！”",
            "萧炎（内心）：这就是现实，弱肉强食，人情冷暖。",
            "萧战（低沉自语）：炎儿，为父能护你一时，却护不了一世啊。",
            "【场2】日 / 萧家广场边缘",
            "萧炎：我现在还有资格让你这么叫么？",
            "萧薰儿：萧炎哥哥，薰儿相信你会重新站起来。",
        ]),
        emotional_curve="压抑屈辱到微光陪伴",
        ending_hook="萧炎斗气消失的真相仍未揭开。",
        source_basis="保留测验三段、族人嘲讽、父子隐忍、薰儿鼓励等核心情节。",
    )


def test_storyboard_soundtrack_rejects_mostly_silent_full_script_split() -> None:
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot(1),
            _shot(2, dialogues=[Dialogue(speaker="萧炎", line="我不会一直这样。", emotion="坚定")]),
            _shot(3),
            _shot(4),
            _shot(5, dialogues=[Dialogue(speaker="萧炎", line="斗气为什么会消失，我一定会查清。", emotion="坚定")]),
        ],
    )

    errors = validate_storyboard_soundtrack(board, _screenplay(), 50)

    assert any("分镜声轨过少" in error for error in errors)
    assert any("内心OS" in error for error in errors)


def test_storyboard_soundtrack_accepts_dialogue_and_inner_os_coverage() -> None:
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot(1, "人群嘲笑声压过广场：三段斗之气，让萧家蒙羞。"),
            _shot(2, "内心OS：这就是现实，弱肉强食，人情冷暖。"),
            _shot(3, dialogues=[Dialogue(speaker="萧炎", line="我现在还有资格让你这么叫么？", emotion="讥讽")]),
            _shot(4, dialogues=[Dialogue(speaker="萧炎", line="我不会一直这样。", emotion="坚定")]),
            _shot(5, dialogues=[Dialogue(speaker="萧炎", line="斗气为什么会消失，我一定会查清。", emotion="坚定")]),
        ],
    )

    errors = validate_storyboard_soundtrack(board, _screenplay(), 50)

    assert errors == []
