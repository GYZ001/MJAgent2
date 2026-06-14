from app.schemas import Bible, Character, EpisodeScreenplay, ScreenplayBeat, ScriptScene, World
from app.validators import validate_screenplay


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="二十八岁男性，黑色短发，深灰西装，眉眼冷峻，腕戴银色手表",
                personality="冷静",
                speech_style="短句直接，语气克制",
            )
        ],
        world=World(era="现代", genre="都市", visual_style_canonical="都市漫剧厚涂风，柔和侧光，冷暖对比色"),
    )


def _beat(no: int, beat_type: str = "钩子", source_excerpt: str = "谷言攥着纸杯看向门口。") -> ScreenplayBeat:
    return ScreenplayBeat(
        beat_no=no,
        day_offset=0,
        time_of_day="上午",
        location="咖啡厅",
        characters=["谷言"],
        dramatic_event="谷言发现门口的人影停下",
        visible_action="谷言攥紧纸杯抬头看向门口，肩膀明显绷紧",
        key_dialogues=["你终于来了。"],
        turn="来者身份暴露",
        carry="谷言准备追问真相",
        beat_type=beat_type,
        source_excerpt=source_excerpt,
    )


def test_screenplay_rejects_legacy_beats_payload() -> None:
    script = EpisodeScreenplay(episode_no=1, beats=[_beat(1, source_excerpt="")])

    errors = validate_screenplay(script, _bible(), expected_beats=1, episode_no=1)

    assert any("不再接受 beats" in error for error in errors)


def _scene(no: int, heading: str, summary: str) -> ScriptScene:
    return ScriptScene(
        scene_no=no,
        scene_heading=heading,
        story_function="推进本集核心冲突并交接到下一场",
        characters=["谷言"],
        summary=summary,
        conflict="谷言在信任与戒备之间被迫做出选择",
        turn="局势向更大的危险推进一步",
        source_basis="保留原文中雨夜会面与示警的关键事件",
    )


def test_full_script_screenplay_validation_passes() -> None:
    full_script_text = "\n\n".join([
        "【场1】夜 / 咖啡厅最里侧",
        "雨水顺着玻璃滑下，谷言独自守在最里面的位置，指尖一直压着已经凉透的纸杯，目光钉在门口。",
        "谷言（压低声音）：还有十分钟，他要是再不来，我就走。",
        "【场2】夜 / 咖啡厅门口",
        "门上的风铃忽然响起，谷言抬头，看见失踪多日的旧友站在雨幕里，脸色苍白，袖口还沾着暗红的血迹。",
        "谷言（猛地起身）：你这几天到底躲到哪去了？",
        "【场3】夜 / 咖啡厅座位",
        "旧友坐下后没有寒暄，只把一把冰凉的储物柜钥匙缓缓推到谷言手边，声音压得极低，眼神不停瞟向门外，仿佛随时会有人闯进来。",
        "谷言（攥紧钥匙）：你到底想说什么？别绕了，把今晚的事一次讲清楚。",
    ])
    script = EpisodeScreenplay(
        episode_no=1,
        mode="full_script",
        title="雨夜敲门",
        logline="谷言在雨夜等来失踪旧友，真相逼近门槛。",
        script_format_note="场次化台本稿，含场标、动作段与对白段",
        scene_outline=[
            _scene(1, "【场1】夜 / 咖啡厅最里侧", "谷言雨夜独自守在咖啡厅，等待迟迟未到的旧友，内心愈发不安。"),
            _scene(2, "【场2】夜 / 咖啡厅门口", "失踪多日的旧友带着血迹现身门口，谷言惊起追问对方的去向。"),
            _scene(3, "【场3】夜 / 咖啡厅座位", "旧友递出储物柜钥匙并低声示警，谷言陷入信任与戒备的两难。"),
        ],
        full_script_text=full_script_text,
        emotional_curve="从压抑等待到骤然紧绷，最后落到更大的不安与悬念。",
        ending_hook="谷言刚要追问，门外第二次响起更重的敲门声。",
        source_basis="保留雨夜会面、旧友递钥匙、警告不要信任来人的核心事件，并压缩原文过渡。",
        character_state_changes=["谷言从克制等待转为警觉戒备", "旧友从强撑冷静转为急切示警"],
        opening="雨夜等待",
        development="旧友现身并递出钥匙",
        conflict="旧友警告有人将至，谷言难辨真假",
        climax="门外再次响起敲门声，危险逼近",
    )

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert errors == []


def test_full_script_screenplay_rejects_shot_language() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        mode="full_script",
        title="雨夜敲门",
        logline="谷言等来旧友。",
        full_script_text="拍01：镜头推近谷言，首帧是纸杯，尾帧切到门口。",
        emotional_curve="等待到惊疑。",
        ending_hook="门外再响一声。",
        source_basis="保留旧友现身和门外敲门。",
    )

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert any("禁用词" in error for error in errors)
