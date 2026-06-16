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


def _empty_bible() -> Bible:
    return Bible(
        characters=[],
        world=World(era="", genre="", visual_style_canonical="国漫风格，非真人CG渲染，统一电影感光影，暖灰色调"),
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


def _contract() -> dict:
    """单集戏剧契约（§3.4/§3.5）的通用合法取值，供"应通过"的剧本样本复用。"""
    return dict(
        dramatic_question="谷言能否在不被牵连的情况下问出旧友失踪的真相？",
        protagonist_goal="弄清旧友这几天的去向并拿到他要交付的东西",
        obstacle="旧友闪烁其词、门外似乎有人尾随，谷言又难辨真假",
        stakes="若信错人或被发现，谷言会被卷进危险甚至送命",
    )


# 与各自 full_script_text 主干一致的必保留台词/剧情点（雨夜敲门样本）。
_RAINY_KEY_LINES = [
    "还有十分钟，他要是再不来，我就走。",
    "你这几天到底躲到哪去了？",
    "你到底想说什么？",
]
_RAINY_KEY_POINTS = [
    "谷言独自守在咖啡厅最里侧等待迟迟未到的旧友",
    "失踪多日的旧友带着血迹现身咖啡厅门口",
    "旧友把储物柜钥匙推到谷言手边并低声示警",
]


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
        key_lines=_RAINY_KEY_LINES,
        key_plot_points=_RAINY_KEY_POINTS,
        opening="雨夜等待",
        development="旧友现身并递出钥匙",
        conflict="旧友警告有人将至，谷言难辨真假",
        climax="门外再次响起敲门声，危险逼近",
        **_contract(),
    )

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert errors == []


def test_full_script_screenplay_passes_with_single_newline_lines() -> None:
    """复现真实失败样本：3 场台本、场内各行用单换行分隔（合规的“分行”写法），
    旧实现按空行块计数只得 1~3 块、误判“段落过少”。修复后按非空行计数，应通过。"""
    full_script_text = "\n".join([
        "【场1】夜 / 咖啡厅最里侧",
        "雨水顺着玻璃滑下，谷言独自守在最里面的位置。",
        "他指尖一直压着已经凉透的纸杯，目光钉在门口。",
        "谷言（压低声音）：还有十分钟，他要是再不来，我就走。",
        "【场2】夜 / 咖啡厅门口",
        "门上的风铃忽然响起，谷言抬头看向门口。",
        "失踪多日的旧友站在雨幕里，脸色苍白，袖口沾着暗红血迹。",
        "谷言（猛地起身）：你这几天到底躲到哪去了？",
        "【场3】夜 / 咖啡厅座位",
        "旧友坐下后没有寒暄，只把一把冰凉的储物柜钥匙缓缓推到谷言手边。",
        "他声音压得极低，眼神不停瞟向门外，仿佛随时都会有人闯进来抓他。",
        "谷言（攥紧钥匙）：你到底想说什么？别再绕了，把今晚的事一次讲清楚。",
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
        key_lines=_RAINY_KEY_LINES,
        key_plot_points=_RAINY_KEY_POINTS,
        **_contract(),
    )

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert errors == []


def test_full_script_screenplay_still_rejects_synopsis_blob() -> None:
    """把整集挤成几行梗概（每场一句总结、没有动作/对白分行）仍必须报“段落过少”。"""
    full_script_text = "\n".join([
        "【场1】夜 / 咖啡厅：谷言雨夜独自等待旧友，内心不安，反复看向门口，旧友迟迟未到。",
        "【场2】夜 / 门口：旧友带血现身，谷言惊起追问去向，对方却闪烁其词不肯明说。",
        "【场3】夜 / 座位：旧友递出钥匙低声示警，谷言陷入信任与戒备，门外又响起敲门。",
    ])
    script = EpisodeScreenplay(
        episode_no=1,
        mode="full_script",
        title="雨夜敲门",
        logline="谷言在雨夜等来失踪旧友，真相逼近门槛。",
        script_format_note="场次化台本稿",
        scene_outline=[
            _scene(1, "【场1】夜 / 咖啡厅最里侧", "谷言雨夜独自守在咖啡厅，等待迟迟未到的旧友，内心愈发不安。"),
            _scene(2, "【场2】夜 / 咖啡厅门口", "失踪多日的旧友带着血迹现身门口，谷言惊起追问对方的去向。"),
            _scene(3, "【场3】夜 / 咖啡厅座位", "旧友递出储物柜钥匙并低声示警，谷言陷入信任与戒备的两难。"),
        ],
        full_script_text=full_script_text,
        emotional_curve="从压抑等待到骤然紧绷，最后落到更大的不安与悬念。",
        ending_hook="谷言刚要追问，门外第二次响起更重的敲门声。",
        source_basis="保留雨夜会面、旧友递钥匙、警告不要信任来人的核心事件，并压缩原文过渡。",
    )

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert any("段落过少" in error for error in errors)


def _valid_rainy_script(**overrides) -> EpisodeScreenplay:
    """构造一份完全合法的"雨夜敲门"剧本，供单点变异测试复用。"""
    full_script_text = "\n".join([
        "【场1】夜 / 咖啡厅最里侧",
        "雨水顺着玻璃滑下，谷言独自守在最里面的位置，指尖压着凉透的纸杯，目光钉在门口。",
        "他又看了一眼手机上没有任何新消息的屏幕，喉结上下滚动了一下，最终把杯子推远。",
        "谷言（压低声音）：还有十分钟，他要是再不来，我就走。",
        "【场2】夜 / 咖啡厅门口",
        "门上风铃忽然响起，谷言抬头，看见失踪多日的旧友站在雨幕里，袖口沾着暗红血迹。",
        "旧友撑着门框，肩膀剧烈起伏，像是一路被人追赶着才勉强逃到这里，眼神惊惶不定。",
        "谷言（猛地起身）：你这几天到底躲到哪去了？",
        "【场3】夜 / 咖啡厅座位",
        "旧友坐下没有寒暄，只把一把冰凉的储物柜钥匙缓缓推到谷言手边，眼神不停瞟向门外。",
        "他压低声音反复叮嘱，谁来找都不能把钥匙交出去，说完又死死攥住谷言的手腕。",
        "谷言（攥紧钥匙）：你到底想说什么？别绕了，把今晚的事一次讲清楚。",
    ])
    base = dict(
        episode_no=1,
        mode="full_script",
        title="雨夜敲门",
        logline="谷言在雨夜等来失踪旧友，真相逼近门槛。",
        script_format_note="场次化台本稿，含场标、动作段与对白段",
        scene_outline=[
            _scene(1, "【场1】夜 / 咖啡厅最里侧", "谷言雨夜独自守在咖啡厅，等待迟迟未到的旧友。"),
            _scene(2, "【场2】夜 / 咖啡厅门口", "失踪多日的旧友带着血迹现身门口，谷言惊起追问。"),
            _scene(3, "【场3】夜 / 咖啡厅座位", "旧友递出储物柜钥匙并低声示警，谷言陷入两难。"),
        ],
        full_script_text=full_script_text,
        emotional_curve="从压抑等待到骤然紧绷，最后落到更大的不安与悬念。",
        ending_hook="谷言刚要追问，门外第二次响起更重的敲门声。",
        source_basis="保留雨夜会面、旧友递钥匙、警告不要信任来人的核心事件。",
        key_lines=list(_RAINY_KEY_LINES),
        key_plot_points=list(_RAINY_KEY_POINTS),
        **_contract(),
    )
    base.update(overrides)
    return EpisodeScreenplay(**base)


def test_screenplay_rejects_missing_dramatic_contract() -> None:
    script = _valid_rainy_script(dramatic_question="", protagonist_goal="", obstacle="", stakes="")

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert any("dramatic_question" in e for e in errors)
    assert any("protagonist_goal" in e for e in errors)
    assert any("stakes" in e for e in errors)


def test_screenplay_rejects_too_few_key_lines() -> None:
    script = _valid_rainy_script(key_lines=["还有十分钟，他要是再不来，我就走。"])

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert any("key_lines 仅" in e for e in errors)


def test_screenplay_rejects_key_line_absent_from_body() -> None:
    """清单里挂了一句正文里根本没有的台词——必须报"未真正写进 full_script_text"。"""
    script = _valid_rainy_script(key_lines=[
        "还有十分钟，他要是再不来，我就走。",
        "你这几天到底躲到哪去了？",
        "这句完全不在剧本正文里的凭空台词。",
    ])

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert any("未真正写进 full_script_text" in e for e in errors)


def test_screenplay_preserves_all_source_dialogues_from_bible_characters() -> None:
    script = _valid_rainy_script()
    source_text = "\n".join([
        "谷言：还有十分钟，他要是再不来，我就走。",
        "路人：雨这么大，还等人啊？",
        "谷言：你这几天到底躲到哪去了？",
        "谷言：别碰那只杯子。",
        "谷言：你到底想说什么？",
    ])

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1, source_text=source_text)

    assert any("人物谱角色在原文中的台词" in e and "key_lines" in e for e in errors)
    assert any("人物谱角色在原文中的台词" in e and "full_script_text" in e for e in errors)
    assert not any("路人" in e for e in errors)


def test_valid_rainy_script_passes() -> None:
    assert validate_screenplay(_valid_rainy_script(), _bible(), expected_beats=5, episode_no=1) == []


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


def test_full_script_screenplay_allows_new_names_without_bible() -> None:
    full_script_text = "\n\n".join([
        "【场1】夜 / 旧宅门口",
        "萧炎站在门外盯着半开的门缝，掌心慢慢收紧，呼吸压得很低。",
        "萧炎：门既然开了，就别躲着不见我。",
        "【场2】夜 / 旧宅前厅",
        "薰儿从暗处走出来，没有立刻解释，只把一枚染血的玉牌递到萧炎眼前，逼他先看清裂痕。",
        "薰儿：先看这个，再决定该不该进去。",
        "【场3】夜 / 旧宅回廊",
        "两人一前一后沿着回廊逼近尽头，脚步声被风声吞掉，尽头那扇门却自己慢慢打开。",
        "萧炎：里面的人，已经知道我们来了。",
    ])
    script = EpisodeScreenplay(
        episode_no=1,
        mode="full_script",
        title="旧宅开门",
        logline="萧炎夜探旧宅，薰儿递出血玉引出更深的埋伏。",
        script_format_note="场次化台本稿，含场标、动作段与对白段",
        scene_outline=[
            _scene(1, "【场1】夜 / 旧宅门口", "萧炎夜探旧宅，在门口试探暗中的回应。").model_copy(update={"characters": ["萧炎"]}),
            _scene(2, "【场2】夜 / 旧宅前厅", "薰儿现身递出血玉，逼迫萧炎先看线索再做选择。").model_copy(update={"characters": ["萧炎", "薰儿"]}),
            _scene(3, "【场3】夜 / 旧宅回廊", "两人沿回廊逼近尽头，未知埋伏正式露出威胁。").model_copy(update={"characters": ["萧炎", "薰儿"]}),
        ],
        full_script_text=full_script_text,
        emotional_curve="从试探压抑一路拉升到共同逼近危险的紧绷感。",
        ending_hook="回廊尽头那扇门自己打开，门后的人却始终没有露面。",
        source_basis="保留夜探旧宅、递出血玉、回廊逼近与暗门自开的关键推进。",
        character_state_changes=["萧炎从试探转为警觉进逼", "薰儿从隐身观察转为主动示警"],
        key_lines=[
            "门既然开了，就别躲着不见我。",
            "先看这个，再决定该不该进去。",
            "里面的人，已经知道我们来了。",
        ],
        key_plot_points=[
            "萧炎夜探旧宅在门口试探暗中的回应",
            "薰儿现身把染血的玉牌递到萧炎眼前逼他先看线索",
            "回廊尽头那扇门自己慢慢打开",
        ],
        dramatic_question="萧炎能否在看清血玉线索后安全闯过这座旧宅？",
        protagonist_goal="进入旧宅查清薰儿示警背后的真相",
        obstacle="暗处埋伏未明、血玉线索可疑，萧炎又急于求证",
        stakes="若贸然深入或信错薰儿，两人会落入门后埋伏",
        opening="夜探旧宅",
        development="薰儿现身递出血玉",
        conflict="两人必须决定是否继续深入",
        climax="尽头暗门无声打开，危险被提前唤醒",
    )

    errors = validate_screenplay(script, _empty_bible(), expected_beats=5, episode_no=1)

    assert not any("角色圣经外角色" in error for error in errors)
