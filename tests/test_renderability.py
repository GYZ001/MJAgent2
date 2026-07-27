"""Renderability First 合同单测。"""
from app.renderability import (
    SHOT_HARD_MAX,
    SHOT_SOFT_MAX,
    find_overdetail_hits,
    overdetail_errors,
    shot_count_budget_errors,
)
from app.schemas import (
    Bible, Character, EpisodeScreenplay, PlotSpine, PlotSpineBeat, World,
)
from app.validators import validate_plot_spine, validate_screenplay, storyboard_shot_count_range


def test_find_overdetail_hits() -> None:
    assert "衣角" in find_overdetail_hits("她攥紧衣角站定")
    assert find_overdetail_hits("他走向石碑并抬手贴上碑面") == []


def test_overdetail_errors_message() -> None:
    errs = overdetail_errors("泪珠滑落，嘴角抽搐", "action_desc")
    assert errs and "超纲细节词" in errs[0]


def test_shot_count_budget() -> None:
    assert shot_count_budget_errors(12) == []
    soft = shot_count_budget_errors(18)
    assert any("软预算" in e for e in soft)
    hard = shot_count_budget_errors(SHOT_HARD_MAX + 1)
    assert any("硬上限" in e for e in hard)


def test_storyboard_shot_count_range_is_renderability_budget() -> None:
    lo, hi = storyboard_shot_count_range(50)
    assert lo == 8
    assert hi == SHOT_HARD_MAX
    assert hi >= SHOT_SOFT_MAX


def test_validate_plot_spine_requires_beats_and_drops() -> None:
    script = EpisodeScreenplay(episode_no=1)
    errors = validate_plot_spine(script)
    assert any("plot_spine 缺失" in e for e in errors)

    script.plot_spine = PlotSpine(
        episode_premise="主角要证明自己并守住本章结局",
        spine_beats=[
            PlotSpineBeat(beat_id=f"S0{i}", who="谷言", does="采取行动推进局势", turn="局势变化一次")
            for i in range(1, 6)
        ],
        must_keep_ending="本章当场收束，不提前下一章",
        drop_list=["路人起哄的多轮对话", "服饰材质散文描写"],
    )
    assert validate_plot_spine(script) == []


def _bible_guyan() -> Bible:
    return Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="二十八岁男性，黑色短发，深灰西装，眉眼冷峻，腕戴银色手表",
                personality="冷静",
                speech_style="短句直接",
            )
        ],
        world=World(era="现代", genre="都市", visual_style_canonical="都市漫剧厚涂风，柔和侧光"),
    )


def _minimal_valid_body(**overrides) -> EpisodeScreenplay:
    """合法主线 + 场次正文骨架；可叠加空壳 ledger 等覆盖项。"""
    from app.schemas import ScriptScene
    base = dict(
        episode_no=1,
        mode="full_script",
        title="雨夜敲门",
        logline="谷言在雨夜等来失踪旧友，真相逼近门槛。",
        script_format_note="场次化台本稿，含场标与对白",
        plot_spine=PlotSpine(
            episode_premise="谷言要问出旧友失踪的真相",
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
        dramatic_question="谷言能否问出真相？",
        protagonist_goal="弄清去向",
        obstacle="旧友闪烁其词",
        stakes="被卷进危险",
        key_lines=["还有十分钟，他要是再不来，我就走。", "你这几天到底躲到哪去了？", "你到底想说什么？"],
        key_plot_points=[
            "谷言独自守在咖啡厅等待旧友",
            "失踪旧友带着血迹现身门口",
            "旧友把储物柜钥匙推到谷言手边",
            "门外再次响起敲门声危险逼近",
        ],
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】夜 / 咖啡厅最里侧",
                story_function="建立等待与不安，推进本集核心冲突",
                characters=["谷言"],
                summary="谷言雨夜独自守在咖啡厅，等待迟迟未到的旧友，内心愈发不安。",
                conflict="信任与戒备之间被迫选择",
                turn="局势向更大的危险推进一步",
                source_basis="保留原文中雨夜会面与示警的关键事件",
            ),
            ScriptScene(
                scene_no=2,
                scene_heading="【场2】夜 / 咖啡厅门口",
                story_function="旧友现身并递出钥匙，冲突升级",
                characters=["谷言"],
                summary="失踪多日的旧友带着血迹现身门口，谷言惊起追问对方的去向。",
                conflict="去向不明与血迹示警",
                turn="谷言被迫接手线索",
                source_basis="保留原文中旧友现身与递钥匙的关键事件",
            ),
            ScriptScene(
                scene_no=3,
                scene_heading="【场3】夜 / 咖啡厅座位",
                story_function="示警收束本章，危险逼近",
                characters=["谷言"],
                summary="旧友低声示警后门外再次敲门，谷言陷入更大的不安与戒备。",
                conflict="危险当场逼近",
                turn="本章当场收束",
                source_basis="保留原文中门外再次敲门的收束",
            ),
        ],
        full_script_text="\n\n".join([
            "【场1】夜 / 咖啡厅最里侧",
            "谷言独自守在最里面的位置，目光钉在门口。",
            "谷言：还有十分钟，他要是再不来，我就走。",
            "【场2】夜 / 咖啡厅门口",
            "门上的风铃忽然响起，失踪多日的旧友站在雨幕里，袖口沾着血迹。",
            "谷言：你这几天到底躲到哪去了？",
            "【场3】夜 / 咖啡厅座位",
            "旧友把一把储物柜钥匙推到谷言手边，门外再次响起更重的敲门声。",
            "谷言：你到底想说什么？",
        ]),
        emotional_curve="从压抑等待到骤然紧绷，最后落到更大的不安。",
        ending_hook="门外第二次响起更重的敲门声。",
        source_basis="保留雨夜会面、旧友递钥匙、门外再敲门的核心事件。",
        character_state_changes=["谷言从克制等待转为警觉戒备"],
        opening="雨夜等待",
        development="旧友现身并递出钥匙",
        conflict="旧友示警，谷言难辨真假",
        climax="门外再次响起敲门声，危险逼近",
        # 空壳台账：有条目但 content/event_id 为空——旧 QA 会硬拦
        events=[
            {"event_id": "", "state_in": "", "visible_change": "", "state_out": ""},
            {"event_id": "", "state_in": "", "visible_change": "", "state_out": ""},
            {"event_id": "", "state_in": "", "visible_change": "", "state_out": ""},
            {"event_id": "", "state_in": "", "visible_change": "", "state_out": ""},
            {"event_id": "", "state_in": "", "visible_change": "", "state_out": ""},
            {"event_id": "", "state_in": "", "visible_change": "", "state_out": ""},
        ],
        information_ledger=[
            {"info_id": "I1", "event_id": "", "content": ""},
            {"info_id": "I2", "event_id": "", "content": ""},
            {"info_id": "I3", "event_id": "", "content": ""},
            {"info_id": "I4", "event_id": "", "content": ""},
            {"info_id": "I5", "event_id": "", "content": ""},
            {"info_id": "I6", "event_id": "", "content": ""},
        ],
    )
    base.update(overrides)
    return EpisodeScreenplay(**base)


def test_episode_target_from_spine_shrinks() -> None:
    from app.renderability import episode_target_from_spine
    assert episode_target_from_spine(5) == 50
    assert episode_target_from_spine(8) == 80
    assert episode_target_from_spine(12) == 80


def test_duration_gt5_blocked_when_fits_five() -> None:
    from app.renderability import duration_gt5_errors, shot_duration_should_prefer_five
    assert shot_duration_should_prefer_five(spoken_chars=4, action_beats=1)
    errs = duration_gt5_errors(shot_no=1, duration_s=8, spoken_chars=4, action_beats=1)
    assert errs and "改回 5s" in errs[0]
    assert duration_gt5_errors(shot_no=1, duration_s=8, spoken_chars=20, action_beats=2) == []


def test_empty_shell_ledger_normalized_from_spine() -> None:
    """空壳 information_ledger / events 应从 plot_spine 回填，不再卡 WARNING。"""
    from app.validators import normalize_screenplay_ledgers

    script = _minimal_valid_body()
    normalize_screenplay_ledgers(script)
    assert script.events, "应合成 events"
    assert script.information_ledger, "应合成 information_ledger"
    assert all((e.event_id or "").strip() for e in script.events)
    assert all((i.content or "").strip() and (i.event_id or "").strip() for i in script.information_ledger)

    errors = validate_screenplay(script, _bible_guyan(), expected_beats=5, episode_no=1)
    ledger_errs = [e for e in errors if "information_ledger" in e or "events[" in e or "events 不能为空" in e]
    assert ledger_errs == [], ledger_errs


def test_screenplay_allows_dialogue_rich_key_lines_without_fixed_cap() -> None:
    bible = Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="二十八岁男性，黑色短发，深灰西装，眉眼冷峻，腕戴银色手表",
                personality="冷静",
                speech_style="短句直接",
            )
        ],
        world=World(era="现代", genre="都市", visual_style_canonical="都市漫剧厚涂风，柔和侧光"),
    )
    script = EpisodeScreenplay(
        episode_no=1,
        mode="full_script",
        title="雨夜敲门",
        logline="谷言在雨夜等来失踪旧友，真相逼近门槛。",
        script_format_note="场次化台本稿",
        plot_spine=PlotSpine(
            episode_premise="谷言要问出真相",
            spine_beats=[
                PlotSpineBeat(beat_id=f"S0{i}", who="谷言", does="推进主线动作", turn="局势变化一次")
                for i in range(1, 6)
            ],
            must_keep_ending="本章当场收束",
            drop_list=["路人闲聊", "材质描写"],
        ),
        dramatic_question="谷言能否问出真相？",
        protagonist_goal="弄清去向",
        obstacle="旧友闪烁其词",
        stakes="被卷进危险",
        key_lines=[f"台词{i}内容足够长。" for i in range(1, 8)],
        key_plot_points=[f"剧情点{i}局势变化足够长" for i in range(1, 5)],
        scene_outline=[],
        full_script_text="短",
        events=[{"event_id": "E1", "state_in": "开始状态", "visible_change": "可见变化", "state_out": "结束状态"}],
        information_ledger=[{"info_id": "I1", "event_id": "E1", "content": "关键信息交付内容"}],
    )
    errors = validate_screenplay(script, bible, expected_beats=5, episode_no=1)
    assert not any("超过上限" in e and "key_lines" in e for e in errors)
