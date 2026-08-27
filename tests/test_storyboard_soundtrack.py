from app.continuity import implicit_speech_without_dialogue_errors
from app.schemas import Dialogue, EpisodeScreenplay, Shot, Storyboard
from app.validators import validate_storyboard_soundtrack


def _shot(no: int, narration: str | None = None, dialogues: list[Dialogue] | None = None) -> Shot:
    return Shot(
        shot_no=no,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        scene_setting="日，甲家广场",
        characters=["甲一"],
        action_desc="甲一站在测验石碑前攥紧手掌，甲一听见周围议论后低下眼，掌心血痕慢慢渗出。",
        first_frame_desc="甲一站在测验石碑前，手掌贴着碑面，神情平静。",
        last_frame_desc="同一机位，甲一手掌攥成拳，指缝渗出血迹。",
        source_excerpt="少年面无表情，唇角有着一抹自嘲。",
        narration=narration,
        dialogues=dialogues or [],
    )


def _screenplay() -> EpisodeScreenplay:
    return EpisodeScreenplay(
        episode_no=1,
        title="陨落的天才",
        logline="甲一测出三段测验力后遭到嘲讽，甲二儿仍坚定站在他身边。",
        script_format_note="标准影视台本格式",
        full_script_text="\n\n".join([
            "【场1】日 / 甲家广场",
            "人群中爆发出嘲讽声：“三段？这废物真是把家族的脸都丢光了！”",
            "甲一（内心）：这就是现实，弱肉强食，人情冷暖。",
            "甲四（低沉自语）：炎儿，为父能护你一时，却护不了一世啊。",
            "【场2】日 / 甲家广场边缘",
            "甲一：我现在还有资格让你这么叫么？",
            "甲二儿：甲一哥哥，二儿相信你会重新站起来。",
        ]),
        emotional_curve="压抑屈辱到微光陪伴",
        ending_hook="甲一斗气消失的真相仍未揭开。",
        source_basis="保留测验三段、族人嘲讽、父子隐忍、二儿鼓励等核心情节。",
    )


def test_storyboard_soundtrack_allows_ambient_only_reaction_shots() -> None:
    """PRD：不再强制约 75% 镜头有对白/旁白；安静反应镜可只有环境声。禁止内心OS强制。"""
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot(1),
            _shot(2, dialogues=[Dialogue(speaker="甲一", line="我不会一直这样。", emotion="坚定")]),
            _shot(3),
            _shot(4),
            _shot(5, dialogues=[Dialogue(speaker="甲一", line="斗气为什么会消失，我一定会查清。", emotion="坚定")]),
        ],
    )

    errors = validate_storyboard_soundtrack(board, _screenplay(), 50)

    assert not any("分镜声轨过少" in error for error in errors)
    assert not any("内心OS" in error for error in errors)


def test_storyboard_soundtrack_accepts_dialogue_coverage_without_narration() -> None:
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot(1, dialogues=[Dialogue(speaker="甲一", line="三段。", emotion="平静")]),
            _shot(2, dialogues=[Dialogue(speaker="甲一", line="这就是现实。", emotion="平静")]),
            _shot(3, dialogues=[Dialogue(speaker="甲一", line="我现在还有资格让你这么叫么？", emotion="讥讽")]),
            _shot(4, dialogues=[Dialogue(speaker="甲一", line="我不会一直这样。", emotion="坚定")]),
            _shot(5, dialogues=[Dialogue(speaker="甲一", line="斗气为什么会消失，我一定会查清。", emotion="坚定")]),
        ],
    )

    errors = validate_storyboard_soundtrack(board, _screenplay(), 50)

    assert errors == []
    assert not any("内心OS" in error for error in errors)


def test_explicit_silence_does_not_trigger_implicit_speech_gate() -> None:
    shot = _shot(1)
    shot.action_desc = "甲一转身走向门口，全程闭口，无台词，不做说话口型。"

    assert implicit_speech_without_dialogue_errors(shot) == []


def test_negated_speech_does_not_trigger_implicit_speech_gate() -> None:
    shot = _shot(1)
    shot.action_desc = "甲一听完后嘴唇紧闭，没有说话，只抬眼看向门口。"

    assert implicit_speech_without_dialogue_errors(shot) == []


def test_action_prose_does_not_invent_a_spoken_contract() -> None:
    shot = _shot(1)
    shot.action_desc = "甲一转身走向门口，开口询问门外来人。"

    assert implicit_speech_without_dialogue_errors(shot) == []
