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
    s5.information_ids = ["I3.1"]
    s5.action_desc = "萧媚上前测出斗之气七段，碑面亮起，人群追捧赞叹"
    s5.dialogues = []
    s6 = _compact_shot(6)
    s6.spine_beat_ids = ["S04"]
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


def test_s07_split_across_shots_passes() -> None:
    sp = _screenplay_with_spine()
    s5 = _compact_shot(5)
    s5.spine_beat_ids = ["S04"]
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
    events = split_outline_over_key_line_capacity(outline, sp, max_shots=16)
    assert events, "应触发容量拆镜"
    assert len(outline.shots) >= 3
    after = outline_key_line_capacity_errors(outline, sp)
    assert not after, after


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
