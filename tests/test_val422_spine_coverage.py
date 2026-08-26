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
    outline_key_line_speaker_errors,
    relieve_outline_key_line_capacity_overflow,
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
                scene_id="SC-authority",
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
    assert outline.shots[1].scene_id == "SC-authority"
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


def _ep6_two_fragment_screenplay() -> EpisodeScreenplay:
    """原型：EP6 第六轮真实事故（ERR-20260825-8fee67）KL05(29字)+KL06(15字)
    合计 44 字，超过当时 10s/36 字上限。A1 时长上限改造（10s→15s，36 字→54 字）
    后该原始数字不再溢出，故按等比放大到 KL05(30字)+KL06(30字)=60 字，
    继续超过新的 15s/54 字上限，以保留这条回归对"容量溢出下移"机制本身的覆盖。

    两条台词模拟同一句原句被 projection 层按标点切出的相邻碎片，同一说话人
    「孟浩」。key_lines 前 4 条只是占位，让 KL05/KL06 编号与真实事故对齐。
    """
    return EpisodeScreenplay(
        episode_no=6,
        key_lines=[
            "孟浩：占位一。",
            "孟浩：占位二。",
            "孟浩：占位三。",
            "孟浩：占位四。",
            "孟浩：" + ("甲" * 30),
            "孟浩：" + ("乙" * 30),
        ],
    )


def test_relieve_overflow_moves_trailing_key_line_ep6_numbers() -> None:
    sp = _ep6_two_fragment_screenplay()
    catalog = key_line_catalog(sp)
    assert list(catalog)[-2:] == ["KL05", "KL06"]

    # 占位镜先吃掉 KL01..KL04，避免全集覆盖检查掺进无关噪音——本用例只关心
    # KL05/KL06 的容量溢出处置。
    shot_prelude = StoryboardOutlineShot(
        shot_no=1, scene_id="SC-cave", beat="孟浩此前的感叹",
        key_line_ids=["KL01", "KL02", "KL03", "KL04"], duration_s=5,
    )
    shot20 = StoryboardOutlineShot(
        shot_no=20, scene_id="SC-cave", beat="孟浩感叹灵气积累的原因",
        key_line_ids=["KL05", "KL06"], duration_s=10,
    )
    shot21 = StoryboardOutlineShot(
        shot_no=21, scene_id="SC-cave", beat="孟浩起身准备离开",
        key_line_ids=[], duration_s=5,
    )
    outline = StoryboardOutline(episode_no=6, shots=[shot_prelude, shot20, shot21])

    # 红：移动前，模型的原始分配确实不可满足（复现真实报错的判据）。
    before = outline_key_line_capacity_errors(outline, sp)
    assert any("[OUTLINE_KEY_LINE_CAPACITY_INVALID]" in e and "60" in e for e in before), before

    changes = relieve_outline_key_line_capacity_overflow(outline, sp)

    assert changes == [{
        "from_shot_no": 20,
        "to_shot_no": 21,
        "key_line_id": "KL06",
        "reason": "key_line_capacity_overflow_same_speaker_scene",
    }]
    assert shot20.key_line_ids == ["KL05"]
    assert shot21.key_line_ids == ["KL06"]

    # 绿：重算时长后两镜均在容量内，且未打乱交付顺序、说话人仍单一。
    normalize_outline_spoken_durations(outline, sp)
    assert outline_key_line_capacity_errors(outline, sp) == []
    assert outline_key_line_speaker_errors(outline, sp) == []
    # 只升不降：shot20 原已是 10s（模型的动作铺陈意图），30 字本只需 9s 也不回压。
    assert shot20.duration_s == 10
    assert shot21.duration_s == 9   # 30 字需要 9s 档位(32字)才够，原 5s 被抬高


def test_relieve_overflow_prepends_before_existing_next_shot_key_lines() -> None:
    """接收镜已有台词时，下移的台词必须排在原有台词之前（保持时序）。"""
    sp = _ep6_two_fragment_screenplay()
    shot20 = StoryboardOutlineShot(
        shot_no=20, scene_id="SC-cave", beat="孟浩感叹",
        key_line_ids=["KL05", "KL06"], duration_s=10,
    )
    shot21 = StoryboardOutlineShot(
        shot_no=21, scene_id="SC-cave", beat="孟浩继续说明",
        key_line_ids=["KL01"], duration_s=5,
    )
    outline = StoryboardOutline(episode_no=6, shots=[shot20, shot21])

    changes = relieve_outline_key_line_capacity_overflow(outline, sp)

    assert changes, "应触发一次下移"
    assert shot21.key_line_ids == ["KL06", "KL01"], "下移台词必须排在原有台词之前"


def test_relieve_overflow_blocks_on_speaker_mismatch() -> None:
    """相邻镜说话人不同：不得强行安放，原样保留，交给容量校验硬失败。"""
    sp = EpisodeScreenplay(
        episode_no=6,
        key_lines=[
            "孟浩：" + ("甲" * 30),
            "孟浩：" + ("乙" * 30),
            "许师姐：" + ("丙" * 10),
        ],
    )
    shot20 = StoryboardOutlineShot(
        shot_no=20, scene_id="SC-cave", beat="孟浩感叹",
        key_line_ids=["KL01", "KL02"], duration_s=10,
    )
    shot21 = StoryboardOutlineShot(
        shot_no=21, scene_id="SC-cave", beat="许师姐接话",
        key_line_ids=["KL03"], duration_s=5,
    )
    outline = StoryboardOutline(episode_no=6, shots=[shot20, shot21])

    changes = relieve_outline_key_line_capacity_overflow(outline, sp)

    assert changes == []
    assert shot20.key_line_ids == ["KL01", "KL02"]
    assert shot21.key_line_ids == ["KL03"]
    errors = outline_key_line_capacity_errors(outline, sp)
    assert any("[OUTLINE_KEY_LINE_CAPACITY_INVALID]" in e for e in errors), errors


def test_relieve_overflow_blocks_on_scene_mismatch() -> None:
    """相邻镜场次不同：不得跨场次瞬移台词，原样保留交给硬失败。"""
    sp = _ep6_two_fragment_screenplay()
    shot20 = StoryboardOutlineShot(
        shot_no=20, scene_id="SC-cave", beat="孟浩感叹",
        key_line_ids=["KL05", "KL06"], duration_s=10,
    )
    shot21 = StoryboardOutlineShot(
        shot_no=21, scene_id="SC-corridor", beat="换场",
        key_line_ids=[], duration_s=5,
    )
    outline = StoryboardOutline(episode_no=6, shots=[shot20, shot21])

    changes = relieve_outline_key_line_capacity_overflow(outline, sp)

    assert changes == []
    assert shot20.key_line_ids == ["KL05", "KL06"]


def test_relieve_overflow_blocks_when_last_shot() -> None:
    """本镜已是最后一镜、无处可移：原样保留交给硬失败。"""
    sp = _ep6_two_fragment_screenplay()
    shot20 = StoryboardOutlineShot(
        shot_no=20, scene_id="SC-cave", beat="孟浩感叹",
        key_line_ids=["KL05", "KL06"], duration_s=10,
    )
    outline = StoryboardOutline(episode_no=6, shots=[shot20])

    changes = relieve_outline_key_line_capacity_overflow(outline, sp)

    assert changes == []
    assert shot20.key_line_ids == ["KL05", "KL06"]
    assert outline_key_line_capacity_errors(outline, sp)


def test_relieve_overflow_cascades_through_chain_in_a_single_call() -> None:
    """连锁下移：第一镜溢出移入第二镜后，第二镜自身又超容，须在同一次调用内
    继续移到第三镜——验证单趟线性扫描足以收敛，不需要重复调用本函数。
    """
    sp = EpisodeScreenplay(
        episode_no=6,
        key_lines=[
            "甲：" + ("A" * 30),  # KL01
            "甲：" + ("B" * 30),  # KL02
            "甲：" + ("C" * 30),  # KL03
        ],
    )
    shots = [
        StoryboardOutlineShot(
            shot_no=1, scene_id="SC-x", beat="第一镜",
            key_line_ids=["KL01", "KL02"], duration_s=10,
        ),
        StoryboardOutlineShot(
            shot_no=2, scene_id="SC-x", beat="第二镜",
            key_line_ids=["KL03"], duration_s=10,
        ),
        StoryboardOutlineShot(
            shot_no=3, scene_id="SC-x", beat="第三镜",
            key_line_ids=[], duration_s=5,
        ),
    ]
    outline = StoryboardOutline(episode_no=6, shots=shots)

    changes = relieve_outline_key_line_capacity_overflow(outline, sp)

    assert [s.key_line_ids for s in shots] == [["KL01"], ["KL02"], ["KL03"]]
    assert len(changes) == 2
    assert changes[0] == {
        "from_shot_no": 1, "to_shot_no": 2, "key_line_id": "KL02",
        "reason": "key_line_capacity_overflow_same_speaker_scene",
    }
    assert changes[1] == {
        "from_shot_no": 2, "to_shot_no": 3, "key_line_id": "KL03",
        "reason": "key_line_capacity_overflow_same_speaker_scene",
    }
    # 第三镜接住 KL03 后 duration_s 仍是原始的 5s（容量 18 字装不下 30 字）；
    # relieve 本身不碰 duration_s，交由归一化器按最终分配统一重算。
    normalize_outline_spoken_durations(outline, sp)
    assert outline_key_line_capacity_errors(outline, sp) == []


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
