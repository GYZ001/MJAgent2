import asyncio

from app import stages
from app.schemas import (Bible, Character, EpisodeScreenplay, InformationItem,
                         KeyDialogueChain, KeyDialogueTurn, PlotSpine,
                         PlotSpineBeat, ScriptScene, StoryEvent,
                         VoiceCanonical, World)
from app.stages import _render_screenplay_source, generate_screenplay
from app.validators import (
    normalize_screenplay_candidate,
    source_dialogue_fragments,
    validate_dialogue_chains,
    validate_screenplay,
    validate_screenplay_spine_delivery,
)


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
        plot_spine=PlotSpine(
            episode_premise="谷言要在不被牵连的情况下问出旧友失踪的真相",
            spine_beats=[
                PlotSpineBeat(beat_id="S01", who="谷言", does="独自在咖啡厅等待旧友", turn="等待升级为不安", must_keep=True),
                PlotSpineBeat(beat_id="S02", who="旧友", does="带血迹推门现身", turn="谷言震惊起身追问", must_keep=True),
                PlotSpineBeat(beat_id="S03", who="旧友", does="把储物柜钥匙推到谷言手边", turn="谷言被迫接手线索", must_keep=True),
                PlotSpineBeat(beat_id="S04", who="谷言", does="追问旧友到底想说什么", turn="示警信息即将说清", must_keep=True),
                PlotSpineBeat(beat_id="S05", who="门外", does="再次响起更重的敲门声", turn="危险当场逼近并收束本章", must_keep=True),
            ],
            must_keep_ending="门外再次敲门，危险逼近，本章当场收束",
            drop_list=["咖啡厅其他顾客闲聊", "雨水与玻璃的散文级材质描写"],
        ),
        events=[{
            "event_id": "E1",
            "source_span": "雨夜会面",
            "source_fact": "失踪旧友带血现身并递出储物柜钥匙",
            "state_in": "谷言独自在咖啡厅等待旧友",
            "trigger": "失踪旧友带着血迹推门出现",
            "visible_change": "旧友坐下并把储物柜钥匙推到谷言手边",
            "state_out": "谷言拿到钥匙并意识到危险正在逼近",
        }],
        information_ledger=[{
            "info_id": "I1",
            "event_id": "E1",
            "content": "失踪旧友带血现身并向谷言交付储物柜钥匙",
            "delivery_owner": "visual_action",
        }],
    )


# 与各自 full_script_text 主干一致的主线台词/剧情点（雨夜敲门样本）。
_RAINY_KEY_LINES = [
    "还有十分钟，他要是再不来，我就走。",
    "你这几天到底躲到哪去了？",
    "你到底想说什么？",
]
_RAINY_KEY_POINTS = [
    "谷言独自守在咖啡厅最里侧等待迟迟未到的旧友",
    "失踪多日的旧友带着血迹现身咖啡厅门口",
    "旧友把储物柜钥匙推到谷言手边并低声示警",
    "门外再次响起敲门声，危险逼近",
]


def test_full_script_screenplay_validation_passes() -> None:
    full_script_text = "\n\n".join([
        "【场1】夜 / 咖啡厅最里侧",
        "雨水顺着玻璃滑下，谷言独自在咖啡厅等待旧友，指尖一直压着已经凉透的纸杯，目光钉在门口。",
        "谷言（压低声音）：还有十分钟，他要是再不来，我就走。",
        "【场2】夜 / 咖啡厅门口",
        "门上的风铃忽然响起，失踪多日的旧友带着血迹推门现身，谷言猛地抬头。",
        "谷言（猛地起身）：你这几天到底躲到哪去了？",
        "【场3】夜 / 咖啡厅座位",
        "旧友坐下后没有寒暄，只把一把冰凉的储物柜钥匙缓缓推到谷言手边，声音压得极低，眼神不停瞟向门外，仿佛随时会有人闯进来。",
        "谷言（攥紧钥匙）：你到底想说什么？别绕了，把今晚的事一次讲清楚。",
        "话音未落，门外再次响起更重的敲门声。",
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
        "雨水顺着玻璃滑下，谷言独自在咖啡厅等待旧友。",
        "他指尖一直压着已经凉透的纸杯，目光钉在门口。",
        "谷言（压低声音）：还有十分钟，他要是再不来，我就走。",
        "【场2】夜 / 咖啡厅门口",
        "门上的风铃忽然响起，失踪多日的旧友带着血迹推门现身。",
        "谷言抬头看向门口，旧友脸色苍白，肩膀剧烈起伏。",
        "谷言（猛地起身）：你这几天到底躲到哪去了？",
        "【场3】夜 / 咖啡厅座位",
        "旧友坐下后没有寒暄，只把一把冰凉的储物柜钥匙缓缓推到谷言手边。",
        "他声音压得极低，眼神不停瞟向门外，仿佛随时都会有人闯进来抓他。",
        "谷言（攥紧钥匙）：你到底想说什么？别再绕了，把今晚的事一次讲清楚。",
        "话音未落，门外再次响起更重的敲门声。",
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


def test_screenplay_rejects_named_speaker_missing_from_bible() -> None:
    script = _valid_rainy_script()
    script.full_script_text = script.full_script_text.replace(
        "谷言（猛地起身）：你这几天到底躲到哪去了？",
        "陌生杀手（冷笑）：你这几天到底躲到哪去了？",
    )

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert any("未进入人物谱的具名说话人" in error and "陌生杀手" in error for error in errors)


def test_glued_scene_heading_dialogue_is_not_offbible_speaker() -> None:
    """【场1】角色：台词 粘连行应识别角色名，不能把整段场次标题当说话人。"""
    from app.validators import _iter_script_sound_matches

    text = "\n".join([
        "【场1】魂天帝：今日便结束一切。",
        "萧炎：那就一战。",
        "【场2】夜 / 中州天际：这是地点梗概不是对白",
        "【场3】测验广场",
        "测验员：报出成绩。",
    ])
    speakers = [m.group(1).strip() for m in _iter_script_sound_matches(text)]
    assert speakers == ["魂天帝", "萧炎", "测验员"]
    assert all("【" not in s and "/" not in s for s in speakers)


def _valid_rainy_script(**overrides) -> EpisodeScreenplay:
    """构造一份完全合法的"雨夜敲门"剧本，供单点变异测试复用。"""
    full_script_text = "\n".join([
        "【场1】夜 / 咖啡厅最里侧",
        "雨水顺着玻璃滑下，谷言独自在咖啡厅等待旧友，指尖压着凉透的纸杯，目光钉在门口。",
        "他又看了一眼手机上没有任何新消息的屏幕，喉结上下滚动了一下，最终把杯子推远。",
        "谷言（压低声音）：还有十分钟，他要是再不来，我就走。",
        "【场2】夜 / 咖啡厅门口",
        "门上风铃忽然响起，失踪多日的旧友带着血迹推门现身，谷言猛地抬头。",
        "旧友撑着门框，肩膀剧烈起伏，像是一路被人追赶着才勉强逃到这里，眼神惊惶不定。",
        "谷言（猛地起身）：你这几天到底躲到哪去了？",
        "【场3】夜 / 咖啡厅座位",
        "旧友坐下没有寒暄，只把一把冰凉的储物柜钥匙缓缓推到谷言手边，眼神不停瞟向门外。",
        "他压低声音反复叮嘱，谁来找都不能把钥匙交出去，说完又死死攥住谷言的手腕。",
        "谷言（攥紧钥匙）：你到底想说什么？别绕了，把今晚的事一次讲清楚。",
        "话音未落，门外再次响起更重的敲门声。",
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


def test_screenplay_rejects_key_line_only_copied_into_action_prose() -> None:
    """关键台词出现在动作描述里不等于角色真的说出了这句台词。"""
    script = _valid_rainy_script()
    script.full_script_text = script.full_script_text.replace(
        "谷言（猛地起身）：你这几天到底躲到哪去了？",
        "谷言猛地起身，纸条上写着“你这几天到底躲到哪去了？”。",
    )

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert any("角色对白" in e and "动作描述或梗概" in e for e in errors), errors


def test_untyped_dialogue_wording_does_not_infer_response_function() -> None:
    """自然语言措辞不替代 dialogue_chain.turn.function 合同。"""
    reply = "我相信，你会重新站起来。"
    script = _valid_rainy_script()
    script.full_script_text = script.full_script_text.replace(
        "谷言（攥紧钥匙）：你到底想说什么？别绕了，把今晚的事一次讲清楚。",
        f"谷言：{reply}",
    )
    script.key_lines[-1] = reply

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert not any("主线对白上下文断裂" in e for e in errors), errors


def test_screenplay_accepts_reply_with_prior_other_character_turn() -> None:
    reply = "我相信，你会重新站起来。"
    script = _valid_rainy_script()
    script.full_script_text = script.full_script_text.replace(
        "谷言（攥紧钥匙）：你到底想说什么？别绕了，把今晚的事一次讲清楚。",
        f"测验员：你还相信他会回来吗？\n谷言：{reply}",
    )
    script.key_lines = script.key_lines[:2] + ["你还相信他会回来吗？", reply]

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert not any("主线对白上下文断裂" in e for e in errors), errors


def test_orphan_reply_check_prefers_exact_speaker_for_short_line() -> None:
    script = _valid_rainy_script()
    script.full_script_text = "\n".join([
        "【场1】日 / 门厅",
        "谷言：你好啊，今天来得早。",
        "测验员：一起行动好吗？",
        "谷言：好啊！",
    ])
    script.key_lines = [
        "谷言：你好啊，今天来得早。",
        "测验员：一起行动好吗？",
        "谷言：好啊！",
    ]
    script.dialogue_chains = [KeyDialogueChain(
        chain_id="DC1",
        topic="确认一起行动",
        turns=[
            KeyDialogueTurn(
                speaker="谷言",
                line="你好啊，今天来得早。",
                function="statement",
                source_text="你好啊，今天来得早。",
            ),
            KeyDialogueTurn(
                speaker="测验员",
                line="一起行动好吗？",
                function="question",
                source_text="一起行动好吗？",
            ),
            KeyDialogueTurn(
                speaker="谷言",
                line="好啊！",
                function="response",
                source_text="好啊！",
            ),
        ],
    )]

    errors = validate_screenplay(
        script,
        _bible(),
        expected_beats=5,
        episode_no=1,
    )

    assert not any("主线对白上下文断裂" in error for error in errors), errors


def test_screenplay_uses_structured_announcement_over_context_marker() -> None:
    """“你可是某人”是身份确认；结构化 announcement 不应被“可是”误判为回应。"""
    announcement = "你……可是谷言，先坐下再说。"
    script = _valid_rainy_script()
    script.full_script_text = script.full_script_text.replace(
        "谷言（攥紧钥匙）：你到底想说什么？别绕了，把今晚的事一次讲清楚。",
        f"谷言：{announcement}",
    )
    script.key_lines[-1] = f"谷言：{announcement}"
    script.dialogue_chains = [
        KeyDialogueChain(
            chain_id="DC1",
            topic="确认来人身份",
            turns=[
                KeyDialogueTurn(
                    speaker="谷言",
                    line=announcement,
                    function="announcement",
                    source_text=announcement,
                ),
            ],
        ),
    ]

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert not any("主线对白上下文断裂" in e for e in errors), errors


def test_screenplay_rejects_key_lines_out_of_story_order() -> None:
    script = _valid_rainy_script()
    script.key_lines = list(reversed(script.key_lines))

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert any("打乱了主线对白顺序" in e for e in errors), errors


def test_dialogue_chain_normalization_derives_key_lines_in_script_order() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text="【场1】日 / 广场\n谷言：先把门打开。\n【场2】夜 / 室内\n谷言：最后再关灯。",
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC2", topic="收尾",
                turns=[KeyDialogueTurn(speaker="谷言", line="最后再关灯。")],
            ),
            KeyDialogueChain(
                chain_id="DC1", topic="开场",
                turns=[KeyDialogueTurn(speaker="谷言", line="先把门打开。")],
            ),
        ],
    )

    normalized = normalize_screenplay_candidate(script)

    assert normalized.key_lines == ["谷言：先把门打开。", "谷言：最后再关灯。"]
    assert script.key_lines == []


def test_dialogue_chain_normalization_excludes_narrator_from_key_lines() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text="【场1】日 / 山林\n旁白：砰、轰。\n谷言：门开了。",
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="开门",
                turns=[
                    KeyDialogueTurn(speaker="旁白", line="砰、轰。"),
                    KeyDialogueTurn(speaker="谷言", line="门开了。"),
                ],
            ),
        ],
    )

    normalized = normalize_screenplay_candidate(script)

    assert normalized.key_lines == ["谷言：门开了。"]


def test_dialogue_chain_normalization_removes_duplicate_evidence_action_as_speaker() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】日 / 楼梯间\n"
            "胡太太：一起去吃饭吧。\n"
            "阿宾：好啊！\n"
            "两人转身走出楼梯间：前往快餐店。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="相约吃饭",
                turns=[
                    KeyDialogueTurn(
                        speaker="胡太太",
                        line="一起去吃饭吧。",
                        source_text="一起去吃饭吧。",
                    ),
                    KeyDialogueTurn(
                        speaker="两人转身走出楼梯间",
                        line="前往快餐店。",
                        source_text="一起去吃饭吧。",
                    ),
                    KeyDialogueTurn(
                        speaker="阿宾",
                        line="好啊！",
                        source_text="好啊！",
                    ),
                ],
            )
        ],
        voice_bible=[
            VoiceCanonical(speaker_id="阿宾", voice_canonical="青年男声"),
            VoiceCanonical(speaker_id="胡太太", voice_canonical="温柔女声"),
        ],
    )

    normalized = normalize_screenplay_candidate(script)

    assert [turn.speaker for turn in normalized.dialogue_chains[0].turns] == [
        "胡太太",
        "阿宾",
    ]
    assert [turn.speaker for turn in script.dialogue_chains[0].turns] == [
        "胡太太",
        "两人转身走出楼梯间",
        "阿宾",
    ]
    assert "两人转身走出楼梯间，前往快餐店。" in normalized.full_script_text
    assert "两人转身走出楼梯间：" not in normalized.full_script_text


def test_dialogue_chain_normalization_removes_duplicate_evidence_fake_narration() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】日 / 楼梯间\n"
            "胡太太：一起去吃饭吧。\n"
            "旁白：前往快餐店。\n"
            "阿宾：好啊！"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="相约吃饭",
                turns=[
                    KeyDialogueTurn(
                        speaker="胡太太",
                        line="一起去吃饭吧。",
                        source_text="一起去吃饭吧。",
                    ),
                    KeyDialogueTurn(
                        speaker="旁白",
                        line="前往快餐店。",
                        source_text="一起去吃饭吧。",
                    ),
                    KeyDialogueTurn(
                        speaker="阿宾",
                        line="好啊！",
                        source_text="好啊！",
                    ),
                ],
            )
        ],
        voice_bible=[
            VoiceCanonical(
                speaker_id="旁白",
                voice_canonical="中性旁白",
                role_type="narrator",
            ),
            VoiceCanonical(speaker_id="阿宾", voice_canonical="青年男声"),
            VoiceCanonical(speaker_id="胡太太", voice_canonical="温柔女声"),
        ],
    )

    normalized = normalize_screenplay_candidate(script)

    assert [turn.speaker for turn in normalized.dialogue_chains[0].turns] == [
        "胡太太",
        "阿宾",
    ]
    assert "旁白：前往快餐店。" not in normalized.full_script_text
    assert "前往快餐店。" in normalized.full_script_text


def test_dialogue_chain_normalization_merges_same_topic_continuation_response() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】日 / 厨房\n"
            "胡太太：帮我拿一下电炉好吗？\n"
            "阿宾：没有看见电炉。\n"
            "胡太太：那你下来扶梯。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="厨房拿电炉",
                turns=[
                    KeyDialogueTurn(
                        speaker="胡太太",
                        line="帮我拿一下电炉好吗？",
                        function="question",
                        source_text="帮我拿一下电炉好吗？",
                    )
                ],
            ),
            KeyDialogueChain(
                chain_id="DC2",
                topic="厨房拿电炉（续）",
                turns=[
                    KeyDialogueTurn(
                        speaker="阿宾",
                        line="没有看见电炉。",
                        function="response",
                        source_text="没有看见电炉。",
                    ),
                    KeyDialogueTurn(
                        speaker="胡太太",
                        line="那你下来扶梯。",
                        function="statement",
                        source_text="那你下来扶梯。",
                    ),
                ],
            ),
        ],
    )

    normalized = normalize_screenplay_candidate(script)

    assert len(normalized.dialogue_chains) == 1
    assert [turn.speaker for turn in normalized.dialogue_chains[0].turns] == [
        "胡太太",
        "阿宾",
        "胡太太",
    ]


def test_dialogue_chain_normalization_keeps_cross_scene_continuation_separate() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】日 / 客厅\n"
            "胡太太：帮我拿一下电炉好吗？\n"
            "【场2】日 / 厨房\n"
            "阿宾：没有看见电炉。\n"
            "胡太太：那你下来扶梯。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="厨房拿电炉",
                turns=[
                    KeyDialogueTurn(
                        speaker="胡太太",
                        line="帮我拿一下电炉好吗？",
                        function="question",
                        source_text="帮我拿一下电炉好吗？",
                    )
                ],
            ),
            KeyDialogueChain(
                chain_id="DC2",
                topic="厨房拿电炉（续）",
                turns=[
                    KeyDialogueTurn(
                        speaker="阿宾",
                        line="没有看见电炉。",
                        function="response",
                        source_text="没有看见电炉。",
                    ),
                    KeyDialogueTurn(
                        speaker="胡太太",
                        line="那你下来扶梯。",
                        function="statement",
                        source_text="那你下来扶梯。",
                    ),
                ],
            ),
        ],
    )

    normalized = normalize_screenplay_candidate(script)

    assert len(normalized.dialogue_chains) == 2
    assert normalized.dialogue_chains[1].turns[0].function == "statement"


def test_ledger_normalization_resolves_composite_speaker_from_content() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        events=[
            StoryEvent(
                event_id="E1",
                visible_change="胡太太邀请阿宾一起吃饭。",
                state_out="二人准备同行。",
            )
        ],
        information_ledger=[
            InformationItem(
                info_id="I1",
                event_id="E1",
                content="胡太太今天放假，邀请阿宾一起去快餐店。",
                delivery_owner="spoken_dialogue",
                speaker_id="阿宾、胡太太",
            )
        ],
        voice_bible=[
            VoiceCanonical(speaker_id="阿宾", voice_canonical="青年男声"),
            VoiceCanonical(speaker_id="胡太太", voice_canonical="温柔女声"),
        ],
    )

    normalized = normalize_screenplay_candidate(script)

    assert normalized.information_ledger[0].speaker_id == "胡太太"
    assert script.information_ledger[0].speaker_id == "阿宾、胡太太"


def test_ledger_normalization_keeps_ambiguous_composite_speaker_for_repair() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        events=[
            StoryEvent(
                event_id="E1",
                visible_change="二人完成问答。",
                state_out="信息已经交付。",
            )
        ],
        information_ledger=[
            InformationItem(
                info_id="I1",
                event_id="E1",
                content="二人通过问答确认了出行安排。",
                delivery_owner="spoken_dialogue",
                speaker_id="阿宾、胡太太",
            )
        ],
        voice_bible=[
            VoiceCanonical(speaker_id="阿宾", voice_canonical="青年男声"),
            VoiceCanonical(speaker_id="胡太太", voice_canonical="温柔女声"),
        ],
    )

    normalized = normalize_screenplay_candidate(script)

    assert normalized.information_ledger[0].speaker_id == "阿宾、胡太太"


def test_long_screenplay_source_retains_head_middle_dialogue_and_tail() -> None:
    source = (
        "开场事实" + ("甲" * 500)
        + "中段人物说道：“这句主线台词绝对不能丢。”"
        + ("乙" * 500) + "结尾真相落定"
    )

    rendered = _render_screenplay_source(source, budget=360)

    assert rendered.startswith("开场事实")
    assert "“这句主线台词绝对不能丢。”" in rendered
    assert rendered.endswith("结尾真相落定")
    assert len(rendered) <= 360


def _screenplay_with_source_dialogue_chain() -> tuple[EpisodeScreenplay, str]:
    source = "\n".join([
        "测验员：“斗之力，三段！”",
        "谷言：“只有三段？”",
        "测验员：“结果无误。”",
    ])
    script = _valid_rainy_script()
    script.full_script_text = script.full_script_text.replace(
        "谷言（压低声音）：还有十分钟，他要是再不来，我就走。",
        "测验员：斗之力，三段！\n谷言：只有三段？\n测验员：结果无误。",
    )
    script.dialogue_chains = [KeyDialogueChain(
        chain_id="DC1",
        topic="测验员宣布结果并确认",
        turns=[
            KeyDialogueTurn(
                speaker="测验员", line="斗之力，三段！",
                function="announcement", source_text="斗之力，三段！",
            ),
            KeyDialogueTurn(
                speaker="谷言", line="只有三段？",
                function="response", source_text="只有三段？",
            ),
            KeyDialogueTurn(
                speaker="测验员", line="结果无误。",
                function="response", source_text="结果无误。",
            ),
        ],
    )]
    script.key_lines = ["模型错误挑选的孤立金句"]
    return script, source


def test_source_dialogue_inventory_keeps_first_utterance_in_order() -> None:
    _script, source = _screenplay_with_source_dialogue_chain()

    assert source_dialogue_fragments(source) == [
        "斗之力，三段！", "只有三段？", "结果无误。",
    ]


def test_dialogue_chain_is_authoritative_and_allows_functional_trigger() -> None:
    script, source = _screenplay_with_source_dialogue_chain()
    script.voice_bible.append(VoiceCanonical(
        speaker_id="测验员",
        voice_canonical="中性播报声线",
        role_type="functional_character",
    ))
    normalized = normalize_screenplay_candidate(script)

    errors = validate_screenplay(
        normalized, _bible(), expected_beats=5, episode_no=1,
        source_text=source, require_dialogue_chains=True,
    )

    assert script.key_lines == ["模型错误挑选的孤立金句"]
    assert normalized.key_lines == [
        "测验员：斗之力，三段！", "谷言：只有三段？", "测验员：结果无误。",
    ]
    assert not any("dialogue_chains" in error or "开场第一句对白" in error for error in errors), errors
    assert not any("非人物谱角色台词" in error for error in errors), errors


def test_dialogue_chain_allows_seven_continuous_turns_but_keeps_hard_limit() -> None:
    script, source = _screenplay_with_source_dialogue_chain()
    seed = script.dialogue_chains[0].turns
    script.dialogue_chains[0].turns = [*seed, seed[1], seed[2], seed[1], seed[2]]

    seven_turn_errors = validate_dialogue_chains(
        script,
        source_text=source,
        required=True,
    )

    assert not any("turns 需包含" in error for error in seven_turn_errors)

    script.dialogue_chains[0].turns.extend([seed[1], seed[2]])
    nine_turn_errors = validate_dialogue_chains(
        script,
        source_text=source,
        required=True,
    )

    assert any("turns 需包含 1~8 个连续话轮" in error for error in nine_turn_errors)


def test_single_speaker_monologue_may_span_adjacent_scene_blocks() -> None:
    script, source = _screenplay_with_source_dialogue_chain()
    script.dialogue_chains[0].turns = script.dialogue_chains[0].turns[:2]
    script.dialogue_chains[0].turns[1].speaker = "测验员"
    script.dialogue_chains[0].turns[1].function = "statement"
    script.full_script_text = (
        "【场1】夜 / 山崖\n测验员：斗之力，三段！\n"
        "【场2】夜 / 山崖\n测验员：只有三段？"
    )

    errors = validate_dialogue_chains(
        script,
        source_text=source,
        required=True,
    )

    assert not any("被拆到多个场次" in error for error in errors), errors


def test_dialogue_chain_may_continue_into_adjacent_subarea_of_same_location() -> None:
    script, source = _screenplay_with_source_dialogue_chain()
    script.scene_outline = [
        _scene(1, "【场1】日 / 萧家迎客大厅", "长老当众刁难，冲突在大厅爆发。"),
        _scene(2, "【场2】日 / 萧家迎客大厅角落", "薰儿从大厅角落回应并完成解围。"),
    ]
    script.full_script_text = (
        "【场1】日 / 萧家迎客大厅\n"
        "测验员：斗之力，三段！\n"
        "谷言：只有三段？\n"
        "【场2】日 / 萧家迎客大厅角落\n"
        "测验员：结果无误。"
    )

    errors = validate_dialogue_chains(script, source_text=source, required=True)

    assert not any("被拆到多个场次" in error for error in errors), errors


def test_source_grounded_single_character_reply_is_not_rejected_as_empty() -> None:
    script, _source = _screenplay_with_source_dialogue_chain()
    script.dialogue_chains[0].turns[-1].line = "哦。"
    script.dialogue_chains[0].turns[-1].source_text = "哦。"
    script.full_script_text = script.full_script_text.replace("结果无误。", "哦。")
    source = "测验员：“斗之力，三段！”\n谷言：“只有三段？”\n测验员：“哦。”"

    errors = validate_dialogue_chains(script, source_text=source, required=True)

    assert not any("过短或为空" in error for error in errors), errors
    assert not any("source_text 不能为空" in error for error in errors), errors


def test_narrative_grounded_single_character_reply_is_not_rejected_as_empty() -> None:
    script, _source = _screenplay_with_source_dialogue_chain()
    script.dialogue_chains[0].turns[-1].line = "好。"
    script.dialogue_chains[0].turns[-1].source_text = "测验员点头同意。"
    script.full_script_text = script.full_script_text.replace("结果无误。", "好。")
    source = "测验员：“斗之力，三段！”\n谷言：“只有三段？”\n测验员点头同意。"

    errors = validate_dialogue_chains(script, source_text=source, required=True)

    assert not any("过短或为空" in error for error in errors), errors
    assert not any("source_text 未在本集原文中找到" in error for error in errors), errors


def test_single_character_reply_with_placeholder_source_is_rejected() -> None:
    script, source = _screenplay_with_source_dialogue_chain()
    script.dialogue_chains[0].turns[-1].line = "好。"
    script.dialogue_chains[0].turns[-1].source_text = "（原文叙述转为对白）"
    script.full_script_text = script.full_script_text.replace("结果无误。", "好。")

    errors = validate_dialogue_chains(script, source_text=source, required=True)

    assert any("过短或为空" in error for error in errors), errors
    assert any("source_text 未在本集原文中找到" in error for error in errors), errors


def test_dialogue_chain_allows_skipping_unadapted_earlier_source_utterance() -> None:
    script, source = _screenplay_with_source_dialogue_chain()
    script.dialogue_chains[0].turns = script.dialogue_chains[0].turns[1:]
    script.dialogue_chains[0].turns.append(KeyDialogueTurn(
        speaker="谷言", line="今晚必须查清。", function="decision", source_text="结果无误。",
    ))
    script.full_script_text = script.full_script_text.replace(
        "测验员：结果无误。", "测验员：结果无误。\n谷言：今晚必须查清。",
    )

    errors = validate_screenplay(
        script, _bible(), expected_beats=5, episode_no=1,
        source_text=source, require_dialogue_chains=True,
    )

    assert not any(
        "dialogue_chains[0].turns[0].source_text 与改编台词语义不匹配" in error
        for error in errors
    ), errors


def test_dialogue_chain_rejects_first_adapted_turn_with_unrelated_source() -> None:
    script, source = _screenplay_with_source_dialogue_chain()
    script.dialogue_chains[0].turns[0].line = "今天天气不错。"
    script.full_script_text = script.full_script_text.replace(
        "测验员：斗之力，三段！", "测验员：今天天气不错。",
    )

    errors = validate_screenplay(
        script, _bible(), expected_beats=5, episode_no=1,
        source_text=source, require_dialogue_chains=True,
    )

    assert any(
        "dialogue_chains[0].turns[0].source_text 与改编台词语义不匹配" in error
        for error in errors
    ), errors


def test_dialogue_chain_does_not_bind_quoted_sound_before_adapted_dialogue() -> None:
    script, source = _screenplay_with_source_dialogue_chain()
    source = f"门外传来“砰、砰”的敲击声。\n{source}"

    errors = validate_screenplay(
        script, _bible(), expected_beats=5, episode_no=1,
        source_text=source, require_dialogue_chains=True,
    )

    assert not any(
        "dialogue_chains[0].turns[0].source_text 与改编台词语义不匹配" in error
        for error in errors
    ), errors


def test_dialogue_chain_accepts_digit_identifier_spoken_in_chinese() -> None:
    script, _source = _screenplay_with_source_dialogue_chain()
    script.dialogue_chains[0].turns[0].source_text = "7-3-1"
    script.dialogue_chains[0].turns[0].line = "七、三、一。"
    script.full_script_text = script.full_script_text.replace(
        "测验员：斗之力，三段！", "测验员：七、三、一。",
    )
    source = "测验员：“7-3-1”\n谷言：“只有三段？”\n测验员：“结果无误。”"

    errors = validate_screenplay(
        script, _bible(), expected_beats=5, episode_no=1,
        source_text=source, require_dialogue_chains=True,
    )

    assert not any("开场对白锚点被改写到失去原意" in error for error in errors), errors


def test_dialogue_chain_scene_check_prefers_declared_speaker_over_fuzzy_name_match() -> None:
    source = "\n".join([
        "测验员：“斗之力，三段！”",
        "围观者：“三段？果然不出所料。”",
        "萧薰儿：“萧炎哥哥。”",
        "萧炎：“我现在还有资格让你这么叫吗？”",
        "萧薰儿：“薰儿相信，你会重新站起来。”",
    ])
    script = _valid_rainy_script()
    script.full_script_text = "\n".join([
        "【场1】日 / 测验广场",
        "测验员：萧炎，斗之力，三段！级别：低级！",
        "围观者：三段？果然不出所料。",
        "【场2】日 / 队伍末尾",
        "萧薰儿：萧炎哥哥。",
        "萧炎：我现在还有资格让你这么叫吗？",
        "萧薰儿：薰儿相信，你会重新站起来。",
    ])
    script.dialogue_chains = [
        KeyDialogueChain(
            chain_id="DC1",
            topic="测验结果公布",
            turns=[
                KeyDialogueTurn(
                    speaker="测验员",
                    line="萧炎，斗之力，三段！级别：低级！",
                    function="announcement",
                    source_text="斗之力，三段！",
                ),
                KeyDialogueTurn(
                    speaker="围观者",
                    line="三段？果然不出所料。",
                    function="response",
                    source_text="三段？果然不出所料。",
                ),
            ],
        ),
        KeyDialogueChain(
            chain_id="DC2",
            topic="薰儿鼓励萧炎",
            turns=[
                KeyDialogueTurn(
                    speaker="萧薰儿",
                    line="萧炎哥哥。",
                    function="trigger",
                    source_text="萧炎哥哥。",
                ),
                KeyDialogueTurn(
                    speaker="萧炎",
                    line="我现在还有资格让你这么叫吗？",
                    function="response",
                    source_text="我现在还有资格让你这么叫吗？",
                ),
                KeyDialogueTurn(
                    speaker="萧薰儿",
                    line="薰儿相信，你会重新站起来。",
                    function="statement",
                    source_text="薰儿相信，你会重新站起来。",
                ),
            ],
        ),
    ]

    errors = validate_dialogue_chains(script, source_text=source, required=True)

    assert not any("被拆到多个场次" in error for error in errors), errors


def test_initial_screenplay_prompt_contains_d001_and_dialogue_chain_contract(monkeypatch) -> None:
    script, source = _screenplay_with_source_dialogue_chain()
    prompts: list[str] = []

    async def fake_loop(_stage, _stage_key, prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return script

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)
    episode = {
        "id": "ep-test",
        "episode_no": 1,
        "title": "测试集",
        "target_duration_s": 50,
        "hook": "开场",
        "cliffhanger": "收束",
        "synopsis": "测验结果引发回应",
        "authorized_source_chapters": {"chapter-1": source},
    }

    asyncio.run(generate_screenplay(episode, source, _bible()))

    assert "【首条改编对白来源锚点·硬门禁】" in prompts[0]
    assert "D001 是本集实际采用的第一条对白" in prompts[0]
    assert "D001：斗之力，三段！" not in prompts[0]
    assert "用户多选的必保留台词" not in prompts[0]
    assert '"dialogue_chains"' in prompts[0]
    assert "`key_lines` 由后端按 dialogue_chains.turns 确定性回填" in prompts[0]
    assert "最终时长由完整剧情、对白容量、主线节拍和场次建立成本自动扩展，不设上限" in prompts[0]
    assert "每组 dialogue_chain 最多 8 个连续话轮" in prompts[0]
    assert "顶层必须输出 narrative_plan" in prompts[0]
    assert "授权章节 ID：['chapter-1']" in prompts[0]
    assert '"chapter_id":"chapter-1"' in prompts[0]


def test_screenplay_baseline_forbids_full_ir_regeneration(monkeypatch) -> None:
    captured = {}

    async def fake_loop(_stage, _stage_key, _prompt, *_args, **kwargs):
        captured["policy"] = kwargs["loop"].policy
        return EpisodeScreenplay(episode_no=9)

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)
    monkeypatch.setattr(stages, "get_setting", lambda key: "8" if key == "max_repair_attempts" else None)
    episode = {
        "id": "ep-json-bootstrap",
        "episode_no": 9,
        "target_duration_s": 50,
    }

    asyncio.run(stages.generate_screenplay_baseline(
        episode,
        "原文",
        _empty_bible(),
        _prompt="输出剧本 JSON",
    ))

    policy = captured["policy"]
    assert policy.max_iterations == 1
    assert policy.stall_rounds == 1
    assert policy.no_gain_rounds == 2
    assert policy.baseline_only is True
    assert policy.repair_all_blockers is True


def test_screenplay_baseline_rejects_missing_narrative_graph(monkeypatch) -> None:
    captured = {}

    async def fake_loop(_stage, _stage_key, _prompt, _model, business_validate, **kwargs):
        captured["policy"] = kwargs["loop"].policy
        script = EpisodeScreenplay(episode_no=9, ending_hook="")
        captured["errors"] = business_validate(script)
        return script

    def fake_validate_screenplay(*_args, **kwargs):
        captured["require_source_coverage"] = kwargs.get("require_source_coverage")
        return []

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)
    monkeypatch.setattr(stages, "validate_screenplay", fake_validate_screenplay)
    episode = {
        "id": "ep-json-bootstrap",
        "episode_no": 9,
        "target_duration_s": 50,
        "authorized_source_chapters": {"1": "第一章正文", "2": ""},
    }

    asyncio.run(stages.generate_screenplay_baseline(
        episode,
        "原文",
        _empty_bible(),
        _prompt="输出剧本 JSON",
    ))

    assert any(
        error.startswith("[NARRATIVE_PLAN_REQUIRED]")
        for error in captured["errors"]
    )
    assert captured["require_source_coverage"] is True


def test_screenplay_allows_dropping_non_spine_source_dialogues() -> None:
    """Renderability：不再要求人物谱原文台词全量进 key_lines / 正文。"""
    script = _valid_rainy_script()
    source_text = "\n".join([
        "谷言：还有十分钟，他要是再不来，我就走。",
        "路人：雨这么大，还等人啊？",
        "谷言：你这几天到底躲到哪去了？",
        "谷言：别碰那只杯子。",
        "谷言：你到底想说什么？",
    ])

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1, source_text=source_text)

    assert not any("人物谱角色在原文中的台词" in e for e in errors)


def test_screenplay_rejects_missing_plot_spine() -> None:
    script = _valid_rainy_script(plot_spine=None)
    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)
    assert any("plot_spine 缺失" in e for e in errors)


def test_valid_rainy_script_passes() -> None:
    assert validate_screenplay(_valid_rainy_script(), _bible(), expected_beats=5, episode_no=1) == []


def test_screenplay_rejects_must_keep_spine_missing_from_full_script() -> None:
    script = _valid_rainy_script()
    script.plot_spine.spine_beats[0].does = "在屋顶点燃红色信号并等待远处回应"

    errors = validate_screenplay(script, _bible(), expected_beats=5, episode_no=1)

    assert any(
        "full_script_text 未交付" in error and "S01/谷言" in error
        for error in errors
    )


def test_screenplay_spine_delivery_accepts_source_bound_paraphrase() -> None:
    script = _valid_rainy_script()
    script.plot_spine = PlotSpine(
        episode_premise="白洁面对婚姻和职业压力",
        spine_beats=[
            PlotSpineBeat(
                beat_id="S03",
                who="白洁",
                does="晚上与丈夫王申谈论评职称，王申不以为然。两人行房，王申早泄，白洁性欲未满足。",
                turn="白洁对婚姻生活产生不满，性欲被唤醒。",
                purpose="展现白洁的婚姻状况和情绪缺口，为后续选择埋下伏笔。",
                source_segment_ids=["SRC0003"],
                must_keep=True,
            )
        ],
        must_keep_ending="白洁的情绪缺口被建立",
    )
    script.source_coverage = [
        {
            "source_segment_id": "SRC0003",
            "disposition": "deliver",
            "beat_ids": ["S03"],
        }
    ]
    script.full_script_text = "\n".join([
        "【场1】夜 / 白洁家客厅",
        "白洁和王申坐在餐桌前吃饭。",
        "白洁：我评上职称就好了。",
        "王申：你不可能评上的。",
        "两人闷闷不乐上床。王申抚摸白洁，脱下她的内裤，插入。",
        "很快王申就射了，趴在白洁身上不动。白洁推开他，擦下身，翻来覆去睡不着。",
    ])

    assert not validate_screenplay_spine_delivery(
        script,
        action_text=script.full_script_text,
    )


def test_full_script_screenplay_does_not_reject_story_vocabulary() -> None:
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

    assert not any("禁用词" in error for error in errors)


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
            "两人沿回廊逼近尽头面对未知埋伏",
        ],
        plot_spine=PlotSpine(
            episode_premise="萧炎要安全查清旧宅血玉背后的真相",
            spine_beats=[
                PlotSpineBeat(beat_id="S01", who="萧炎", does="夜探旧宅门口试探", turn="确认门后有人", must_keep=True),
                PlotSpineBeat(beat_id="S02", who="薰儿", does="现身递出血玉", turn="萧炎被迫先看线索", must_keep=True),
                PlotSpineBeat(beat_id="S03", who="两人", does="沿回廊逼近尽头", turn="危险感升级", must_keep=True),
                PlotSpineBeat(beat_id="S04", who="暗门", does="自己慢慢打开", turn="埋伏被提前唤醒", must_keep=True),
                PlotSpineBeat(beat_id="S05", who="萧炎", does="停在门前警戒", turn="本章收束于未知门后", must_keep=True),
            ],
            must_keep_ending="暗门自开，危险逼近，本章当场收束",
            drop_list=["回廊风声的散文描写", "无关路人对话"],
        ),
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
