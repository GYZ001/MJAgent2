"""VAL-422 P0：结构化主线覆盖与大纲容量拆镜。"""
from __future__ import annotations

from app.schemas import (
    Dialogue,
    EpisodeScreenplay,
    InformationItem,
    PlotSpine,
    PlotSpineBeat,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
)
from app.validators import (
    assign_outline_delivery_ids,
    key_line_catalog,
    normalize_outline_spoken_durations,
    outline_key_line_capacity_errors,
    split_outline_over_key_line_capacity,
    validate_spine_delivery_ledger,
)
from app.continuity import shot_id_space_errors as continuity_id_errors
from tests.test_validators import _compact_shot


def _screenplay_with_spine() -> EpisodeScreenplay:
    return EpisodeScreenplay(
        episode_no=1,
        title="陨落的天才",
        logline="测试",
        full_script_text="场1 内景 测试\n对白若干",
        key_lines=[
            "萧炎：我不过是个废物。",
            "薰儿相信，你会重新站起来，取回属于你的荣耀与尊严",
        ],
        key_plot_points=["萧媚测出七段", "薰儿追上萧炎"],
        plot_spine=PlotSpine(
            episode_premise="天才陨落",
            spine_beats=[
                PlotSpineBeat(
                    beat_id="S04",
                    who="萧媚",
                    does="测出七段斗之气收获追捧",
                    turn="成为新焦点并与萧炎形成对比",
                    must_keep=True,
                    information_ids=["I3.1", "I3.3"],
                ),
                PlotSpineBeat(
                    beat_id="S07",
                    who="薰儿",
                    does="安慰萧炎并相信他会重新站起来",
                    turn="旧天才被弃后仍有人站在他身边",
                    must_keep=True,
                    key_line_ids=["KL02"],
                ),
            ],
            must_keep_ending="薰儿追上",
            drop_list=["无关支线"],
        ),
        information_ledger=[
            InformationItem(
                info_id="I3.1", content="萧媚测出斗之气七段",
                delivery_owner="visual_action",
            ),
            InformationItem(
                info_id="I3.3", content="萧媚望向萧炎后选择不过去",
                delivery_owner="visual_action",
            ),
        ],
    )


def test_key_line_catalog_stable_ids() -> None:
    sp = _screenplay_with_spine()
    catalog = key_line_catalog(sp)
    assert list(catalog) == ["KL01", "KL02"]
    assert "重新站起来" in catalog["KL02"]


def test_s04_split_across_adjacent_shots_passes() -> None:
    sp = _screenplay_with_spine()
    s5 = _compact_shot(5)
    s5.spine_beat_ids = ["S04"]
    s5.characters = ["萧媚"]
    s5.characters_visible = ["萧媚"]
    s5.information_ids = ["I3.1"]
    s5.action_desc = "萧媚上前测出斗之气七段，碑面亮起，人群追捧赞叹"
    s5.dialogues = []
    s6 = _compact_shot(6)
    s6.spine_beat_ids = ["S04"]
    s6.characters = ["萧媚"]
    s6.characters_visible = ["萧媚"]
    s6.information_ids = ["I3.3"]
    s6.action_desc = "萧媚望向后排萧炎一眼后转身走开，不再过去"
    s6.dialogues = []
    # S07 也先落地，避免整集校验被另一条挡住
    s9 = _compact_shot(9)
    s9.spine_beat_ids = ["S07"]
    s9.key_line_ids = ["KL02"]
    s9.characters = ["薰儿", "萧炎"]
    s9.dialogues = [
        Dialogue(
            speaker="薰儿",
            line="薰儿相信，你会重新站起来，取回属于你的荣耀与尊严",
            emotion="坚定",
        )
    ]
    s9.action_desc = "薰儿上前握住萧炎的手，认真安慰他"
    board = Storyboard(episode_no=1, shots=[s5, s6, s9])
    errs = validate_spine_delivery_ledger(board, sp)
    assert not any("S04" in e and "未覆盖" in e for e in errs), errs
    assert not any("I3.1" in e or "I3.3" in e for e in errs), errs


def test_s04_missing_atom_fails_precisely() -> None:
    sp = _screenplay_with_spine()
    s5 = _compact_shot(5)
    s5.spine_beat_ids = ["S04"]
    s5.characters = ["萧媚"]
    s5.characters_visible = ["萧媚"]
    s5.information_ids = ["I3.1"]
    s5.action_desc = "萧媚测出斗之气七段，人群赞叹"
    s5.dialogues = []
    s9 = _compact_shot(9)
    s9.spine_beat_ids = ["S07"]
    s9.key_line_ids = ["KL02"]
    s9.characters = ["薰儿"]
    s9.dialogues = [
        Dialogue(
            speaker="薰儿",
            line="薰儿相信，你会重新站起来，取回属于你的荣耀与尊严",
            emotion="坚定",
        )
    ]
    board = Storyboard(episode_no=1, shots=[s5, s9])
    errs = validate_spine_delivery_ledger(board, sp)
    assert any("I3.3" in e for e in errs), errs


def test_spine_id_cannot_replace_visible_action_subject() -> None:
    sp = _screenplay_with_spine()
    s5 = _compact_shot(5)
    s5.spine_beat_ids = ["S04"]
    s5.characters = ["测验员"]
    s5.characters_visible = ["测验员"]
    s5.information_ids = ["I3.1", "I3.3"]
    s5.action_desc = "测验员站在石碑旁宣布七段成绩，随后看向画外。"
    s5.dialogues = []
    s9 = _compact_shot(9)
    s9.spine_beat_ids = ["S07"]
    s9.key_line_ids = ["KL02"]
    s9.action_desc = "薰儿走到萧炎面前，认真安慰萧炎。"
    s9.dialogues = [
        Dialogue(
            speaker="薰儿",
            line="薰儿相信，你会重新站起来，取回属于你的荣耀与尊严",
            emotion="坚定",
        )
    ]

    errs = validate_spine_delivery_ledger(Storyboard(episode_no=1, shots=[s5, s9]), sp)

    assert any("主线节拍缺少可见动作主体" in error and "S04/萧媚" in error for error in errs), errs


def test_s07_split_across_shots_passes() -> None:
    sp = _screenplay_with_spine()
    s5 = _compact_shot(5)
    s5.spine_beat_ids = ["S04"]
    s5.characters = ["萧媚"]
    s5.characters_visible = ["萧媚"]
    s5.action_desc = "萧媚测出斗之气七段，人群赞叹；望向萧炎后选择不过去"
    s5.information_ids = ["I3.1", "I3.3"]
    s9 = _compact_shot(9)
    s9.spine_beat_ids = ["S07"]
    s9.key_line_ids = ["KL02"]
    s9.characters = ["薰儿", "萧炎"]
    s9.dialogues = [
        Dialogue(
            speaker="薰儿",
            line="薰儿相信，你会重新站起来，取回属于你的荣耀与尊严",
            emotion="坚定",
        )
    ]
    s9.action_desc = "薰儿安慰萧炎"
    s10 = _compact_shot(10)
    s10.spine_beat_ids = ["S07"]
    s10.action_desc = "萧炎转身离场，薰儿追上与他并肩而行"
    s10.dialogues = []
    board = Storyboard(episode_no=1, shots=[s5, s9, s10])
    errs = validate_spine_delivery_ledger(board, sp)
    assert not any("S07" in e and "未覆盖" in e for e in errs), errs


def test_spine_mixed_visible_and_spoken_delivery_uses_the_right_evidence() -> None:
    """真实回归：药老现身是可见动作，背景暗示由对白交付，不能整句做字面动作匹配。"""
    screenplay = EpisodeScreenplay(
        episode_no=1,
        plot_spine=PlotSpine(
            episode_premise="药老提醒萧炎留意薰儿的来历",
            spine_beats=[PlotSpineBeat(
                beat_id="S02",
                who="药老",
                does="在卧室现身并暗示薰儿背景不凡",
                turn="萧炎好奇并追问",
                must_keep=True,
                key_line_ids=["KL01"],
            )],
            must_keep_ending="萧炎开始追问",
            drop_list=[],
        ),
        key_lines=["药老：那小丫头，来历似乎有点不一般啊。"],
    )
    shot = _compact_shot(1)
    shot.spine_beat_ids = ["S02"]
    shot.key_line_ids = ["KL01"]
    shot.characters = ["萧炎", "药老"]
    shot.characters_visible = ["萧炎", "药老"]
    shot.primary_action = "萧炎放下卷轴，药老现身开口提及薰儿来历"
    shot.action_desc = "萧炎把卷轴放在桌上，药老半透明身影从戒指上方浮现于桌旁"
    shot.first_frame_desc = "夜晚卧室内，萧炎站在桌前，戒指发出微光"
    shot.last_frame_desc = "同一机位，药老半透明身影已经悬浮在木桌右侧"
    shot.dialogues = [Dialogue(
        speaker="药老",
        line="那小丫头，来历似乎有点不一般啊。",
        emotion="平静",
    )]

    errors = validate_spine_delivery_ledger(
        Storyboard(episode_no=1, shots=[shot]), screenplay,
    )

    assert not any("S02/药老" in error for error in errors), errors


def test_spine_receptive_delivery_can_come_from_another_speaker() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        plot_spine=PlotSpine(
            episode_premise="孟浩救人并得知众人被抓",
            spine_beats=[PlotSpineBeat(
                beat_id="S03",
                who="孟浩",
                does="找来藤条顺下崖救人，与王有材对话得知他们被会飞的女人抓来",
                turn="孟浩开始相信仙人存在",
                must_keep=True,
            )],
            must_keep_ending="孟浩得知众人被抓",
            drop_list=[],
        ),
    )
    rescue = _compact_shot(1)
    rescue.spine_beat_ids = ["S03"]
    rescue.characters = ["孟浩"]
    rescue.characters_visible = ["孟浩"]
    rescue.primary_action = "孟浩找来藤条顺下山崖救人"
    rescue.action_desc = "孟浩抱着藤条跑回崖边，弯腰将藤条顺下山崖救人"
    rescue.dialogues = []
    explanation = _compact_shot(2)
    explanation.spine_beat_ids = ["S03"]
    explanation.characters = ["王有材"]
    explanation.characters_visible = ["王有材"]
    explanation.action_desc = "王有材抓住藤条，抬头向崖上的孟浩解释"
    explanation.dialogues = [Dialogue(
        speaker="王有材",
        line="我们是被一个会飞的女人抓来的。",
        emotion="惊恐",
    )]

    errors = validate_spine_delivery_ledger(
        Storyboard(episode_no=1, shots=[rescue, explanation]),
        screenplay,
    )

    assert not any("S03/孟浩" in error for error in errors), errors


def test_spine_spoken_delivery_still_requires_the_subject_to_speak() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        plot_spine=PlotSpine(
            episode_premise="药老提醒萧炎留意薰儿的来历",
            spine_beats=[PlotSpineBeat(
                beat_id="S02", who="药老", does="现身并暗示薰儿背景不凡",
                turn="萧炎开始追问", must_keep=True,
            )],
            must_keep_ending="萧炎开始追问", drop_list=[],
        ),
    )
    shot = _compact_shot(1)
    shot.spine_beat_ids = ["S02"]
    shot.characters = ["药老"]
    shot.characters_visible = ["药老"]
    shot.primary_action = "药老现身"
    shot.action_desc = "药老半透明身影从戒指上方浮现于桌旁，随后保持沉默"
    shot.dialogues = []

    errors = validate_spine_delivery_ledger(
        Storyboard(episode_no=1, shots=[shot]), screenplay,
    )

    assert any("S02/药老" in error for error in errors), errors


def test_story_event_id_cannot_be_spine() -> None:
    shot = _compact_shot(1)
    shot.story_event_id = "S07"
    errs = continuity_id_errors(shot)
    assert any("spine_beat_ids" in e for e in errs), errs


def test_legacy_uncertain_when_no_ids_and_no_text() -> None:
    sp = _screenplay_with_spine()
    s1 = _compact_shot(1)
    s1.action_desc = "人群散去，夜色降临广场"
    s1.dialogues = []
    s1.spine_beat_ids = []
    board = Storyboard(episode_no=1, shots=[s1])
    errs = validate_spine_delivery_ledger(board, sp)
    assert any("LEGACY_COVERAGE_UNCERTAIN" in e for e in errs), errs
    assert not any("分镜未覆盖" in e and "must_keep" in e for e in errs), errs


def test_outline_capacity_split_moves_key_lines() -> None:
    # 专用超长关键台词：两条合计远超 10s/36 字，确保预检与拆镜被触发。
    sp = EpisodeScreenplay(
        episode_no=1,
        title="容量拆镜",
        logline="测试",
        full_script_text="场1 内景 测试",
        key_lines=[
            "角色甲：" + ("甲" * 30),
            "角色乙：" + ("乙" * 30),
        ],
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                beat="对白过载",
                covers="甲开口长台词；乙再接长台词",
                key_line_ids=["KL01", "KL02"],
                duration_s=10,
            ),
            StoryboardOutlineShot(
                shot_no=2,
                beat="离场",
                covers="两人并肩离场",
                duration_s=5,
            ),
        ],
    )
    before = outline_key_line_capacity_errors(outline, sp)
    assert before, before
    assert all(
        error.startswith("[OUTLINE_KEY_LINE_CAPACITY_INVALID]")
        for error in before
    )
    events = split_outline_over_key_line_capacity(outline, sp, max_shots=16)
    assert events, "应触发容量拆镜"
    assert len(outline.shots) >= 3
    after = outline_key_line_capacity_errors(outline, sp)
    assert not after, after


def test_outline_spoken_duration_normalizer_only_raises_to_supported_capacity() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        key_lines=["角色甲：" + ("甲" * 24)],
    )
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                beat="角色甲说出完整台词",
                key_line_ids=["KL01"],
                duration_s=2,
            ),
            StoryboardOutlineShot(
                shot_no=2,
                beat="无对白反应镜",
                duration_s=2,
            ),
        ],
    )

    changes = normalize_outline_spoken_durations(outline, screenplay)

    assert [shot.duration_s for shot in outline.shots] == [7, 5]
    assert [change["to_duration_s"] for change in changes] == [7, 5]
    assert outline_key_line_capacity_errors(outline, screenplay) == []


def test_assign_outline_delivery_ids_from_covers() -> None:
    sp = _screenplay_with_spine()
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                beat="萧媚测出七段并望向萧炎后不过去",
                covers="萧媚测出七段斗之气收获追捧，望向萧炎后选择不过去",
                duration_s=5,
            ),
            StoryboardOutlineShot(
                shot_no=2,
                beat="薰儿安慰",
                covers="薰儿相信，你会重新站起来，取回属于你的荣耀与尊严",
                duration_s=10,
            ),
        ],
    )
    changes = assign_outline_delivery_ids(outline, sp)
    assert changes
    assert "KL02" in (outline.shots[1].key_line_ids or [])
