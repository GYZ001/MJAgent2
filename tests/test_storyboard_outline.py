"""分镜大纲（先规划后逐镜填充，方案 B）的单测：

- validate_storyboard_outline：镜头数范围 / shot_no 连续 / 反停留 / 必保留内容全覆盖；
- _render_storyboard_outline / _outline_brief：把大纲渲染进逐镜 prompt 并标出"本镜"。
"""

from app import config
from app.schemas import (Bible, Character, EpisodeScreenplay, StoryboardOutline,
                         StoryboardOutlineShot, World)
from app.stages import (_maybe_split_outline_covers, _outline_brief,
                        _split_atoms_to_content_budget,
                        _render_storyboard_outline)
from app.validators import (_atomize_claim, _condense, _covers_outside_spoken,
                            downgrade_outline_offbible_spoken,
                            validate_storyboard_outline)

KEY_LINE = "我一定要查清斗气消失的真相。"
KEY_POINT = "萧炎测出斗之力三段被族人嘲讽"


def _bible_with(*names: str) -> Bible:
    return Bible(
        characters=[
            Character(name=n, role="角色", appearance_canonical=f"{n}的外貌设定，发型服饰眼神齐全",
                      personality="坚韧")
            for n in names
        ],
        world=World(era="玄幻古代", genre="玄幻", visual_style_canonical="国风玄幻漫剧厚涂风"),
    )


def _screenplay() -> EpisodeScreenplay:
    return EpisodeScreenplay(
        episode_no=1,
        title="陨落的天才",
        full_script_text="略",
        key_lines=[KEY_LINE],
        key_plot_points=[KEY_POINT],
        ending_hook="斗气消失的真相仍未揭开。",
    )


def _outline(beats: list[str], *, scene: str = "日，萧家测验广场",
            covers: dict[int, str] | None = None) -> StoryboardOutline:
    covers = covers or {}
    return StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(shot_no=i + 1, scene_setting=scene, beat=b, covers=covers.get(i + 1, ""))
            for i, b in enumerate(beats)
        ],
    )


def _valid_beats() -> list[str]:
    return [
        "萧炎站上测验台，魔石碑亮起准备测验",
        "魔石碑显出斗之力三段，全场哗然",
        "测验员宣布等级低级，族人哄笑嘲讽",
        "萧炎强忍屈辱，落寞转身回到队伍末尾",
        "萧炎暗下决心立誓查清斗气消失的真相",
    ]


def test_valid_outline_passes() -> None:
    outline = _outline(_valid_beats(), covers={5: KEY_LINE})
    assert validate_storyboard_outline(outline, _screenplay(), 50) == []


def test_outline_does_not_enforce_target_derived_shot_count() -> None:
    outline = _outline(_valid_beats()[:2], covers={2: KEY_LINE})
    errors = validate_storyboard_outline(outline, _screenplay(), 50)
    assert not any("大纲镜头数" in e for e in errors)


def test_outline_rejects_noncontinuous_shot_no() -> None:
    outline = _outline(_valid_beats(), covers={5: KEY_LINE})
    outline.shots[2].shot_no = 9
    errors = validate_storyboard_outline(outline, _screenplay(), 50)
    assert any("连续递增" in e for e in errors)


def test_outline_rejects_lingering_adjacent_beats() -> None:
    beats = _valid_beats()
    beats[3] = beats[2]  # 第3、4镜剧情逐字相同 = 停留
    outline = _outline(beats, covers={5: KEY_LINE})
    errors = validate_storyboard_outline(outline, _screenplay(), 50)
    assert any("停留在同一节拍" in e for e in errors)


def test_outline_rejects_missing_key_line() -> None:
    # 关键台词在任何 beat/covers 中都没出现 → 大纲漏戏，必须拦下。
    screenplay = EpisodeScreenplay(
        episode_no=1, title="陨落的天才", full_script_text="略",
        key_lines=["你们终将后悔今日的嘲笑。"], key_plot_points=[KEY_POINT])
    outline = _outline(_valid_beats())  # beats 覆盖 KEY_POINT，但不含这句关键台词
    errors = validate_storyboard_outline(outline, screenplay, 50)
    assert any("未安排" in e and "关键台词" in e for e in errors)


def test_render_outline_marks_current_shot() -> None:
    outline = _outline(_valid_beats(), covers={5: KEY_LINE})
    rendered = _render_storyboard_outline(outline, current_shot_no=3)
    assert "第3/5镜" in rendered
    # 行级标记用两个前导空格，唯一标在第 3 镜那一行（表头说明里的「← 本镜」不带前导空格）
    assert rendered.count("  ← 本镜") == 1
    marked = [ln for ln in rendered.splitlines() if "  ← 本镜" in ln][0]
    assert marked.startswith("第3/5镜")


def test_render_outline_hides_legacy_information_ids() -> None:
    outline = _outline(_valid_beats(), covers={5: KEY_LINE})
    outline.shots[0].new_information_ids = ["I1", "legacy_snake_case"]

    rendered = _render_storyboard_outline(outline, current_shot_no=1, valid_info_ids={"I1"})

    assert "info:I1" in rendered
    assert "legacy_snake_case" not in rendered


def test_outline_brief_lookup() -> None:
    outline = _outline(_valid_beats(), covers={5: KEY_LINE})
    assert _outline_brief(outline, 5).covers == KEY_LINE
    assert _outline_brief(outline, 99) is None
    assert _outline_brief(None, 1) is None


def test_functional_extra_spoken_is_preserved_in_outline() -> None:
    """测验员是允许开口的功能性路人，不应被降级为旁白。"""
    bible = _bible_with("萧炎", "萧薰儿")
    names = {c.name for c in bible.characters}
    outline = _outline(_valid_beats(),
                       covers={3: "萧炎测验斗之气仅三段，被测验员宣布为低级"})
    assert _covers_outside_spoken(outline.shots[2].covers, names) == []

    changed = downgrade_outline_offbible_spoken(outline, bible)
    assert changed == []
    assert outline.shots[2].covers == "萧炎测验斗之气仅三段，被测验员宣布为低级"
    assert validate_storyboard_outline(outline, _screenplay(), 50, bible=bible) == []


def test_downgrade_preserves_inbible_speaker_and_is_idempotent() -> None:
    """圣经内角色的"被X当众宣告"合法可拍，不应被降级；非贪婪匹配不把"当众"吞进角色名。
    二次运行不再改写，beat 指令不重复追加。"""
    bible = _bible_with("萧炎", "萧战")
    names = {c.name for c in bible.characters}
    outline = _outline(_valid_beats(),
                       covers={3: "萧炎被萧战当众宣告为废物"})
    assert _covers_outside_spoken(outline.shots[2].covers, names) == []  # 萧战 在圣经内

    changed = downgrade_outline_offbible_spoken(outline, bible)
    assert changed == []
    assert outline.shots[2].covers == "萧炎被萧战当众宣告为废物"  # 原样保留

    # 既非圣经角色、也非功能性路人的具体人物仍会降级，且重复运行幂等。
    off = _bible_with("萧炎")
    o2 = _outline(_valid_beats(), covers={3: "萧炎被黑袍老者当众宣告为低级"})
    assert downgrade_outline_offbible_spoken(o2, off)  # 首次有改写
    assert o2.shots[2].covers == "萧炎被宣告为低级"
    assert downgrade_outline_offbible_spoken(o2, off) == []  # 再跑无改写
    assert o2.shots[2].beat.count("改由旁白转述") == 1


def test_over_budget_covers_are_not_mechanically_split() -> None:
    """PRD：废除按文本长度机械拆镜；仅超口播字数时不得自动插入无状态碎片。"""
    covers = (
        "萧炎低声说我会查清真相；他回望测验台；族人仍在哄笑；"
        "萧薰儿穿过人群走来；她让众人闭嘴；萧炎压下怒意；"
        "他转身离开广场；心中立誓夺回失去的斗气"
    )
    outline = _outline(
        ["萧炎承受嘲讽并离开", "下一段原有剧情"],
        covers={1: covers},
    )
    before = len(outline.shots)

    assert _maybe_split_outline_covers(outline, 1, _bible_with("萧炎", "萧薰儿"), 20) is False
    assert len(outline.shots) == before
    assert outline.shots[0].covers == covers


def test_semantic_spoken_and_crowd_covers_can_still_split() -> None:
    """语义原因（角色开口 + 人群声同镜）仍允许拆分，不属于字符机械拆镜。"""
    covers = "萧炎公布斗之气三段；周围人群哄笑嘲讽四起"
    outline = _outline(["萧炎成绩公布", "下一段原有剧情"], covers={1: covers})

    assert _maybe_split_outline_covers(outline, 1, _bible_with("萧炎"), 20)
    assert len(outline.shots) >= 3
    assert [shot.shot_no for shot in outline.shots] == list(range(1, len(outline.shots) + 1))


def test_single_long_cover_atom_is_not_char_split() -> None:
    covers = "萧炎" + "握紧拳头凝视石碑决心查清斗气消失真相" * 3
    outline = _outline(["萧炎立誓", "下一段原有剧情"], covers={1: covers})
    before = len(outline.shots)

    assert _maybe_split_outline_covers(outline, 1, _bible_with("萧炎"), 20) is False
    assert len(outline.shots) == before
    assert outline.shots[0].covers == covers


def test_outline_allows_long_atom_for_deterministic_pre_split() -> None:
    covers = KEY_LINE + "萧炎握紧拳头决心查清斗气消失真相" * 3
    outline = _outline(_valid_beats(), covers={5: covers})

    assert validate_storyboard_outline(outline, _screenplay(), 50) == []


def test_real_shot_12_cover_split_avoids_tiny_tail() -> None:
    covers = "萧炎哥哥；以前你曾经与薰儿说过；要能放下；才能拿起；提放自如；是自在人"

    chunks = _split_atoms_to_content_budget(
        _atomize_claim(covers), config.MAX_SPOKEN_CHARS_PER_SHOT
    )

    assert [len(_condense(chunk)) for chunk in chunks] == [14, 16]
    assert _condense("".join(chunks)) == _condense("".join(_atomize_claim(covers)))
