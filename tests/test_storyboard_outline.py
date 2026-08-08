"""分镜大纲（先规划后逐镜填充，方案 B）的单测：

- validate_storyboard_outline：镜头数范围 / shot_no 连续 / 反停留 / 必保留内容全覆盖；
- _render_storyboard_outline / _outline_brief：把大纲渲染进逐镜 prompt 并标出"本镜"。
"""

from app.schemas import (Bible, Character, EpisodeScreenplay, StoryboardOutline,
                         StoryboardOutlineShot, World)
from app.stages import (_maybe_split_outline_covers, _outline_brief,
                        _split_atoms_to_content_budget,
                        _render_storyboard_outline)
from app.validators import (_atomize_claim, _condense, _covers_outside_spoken,
                            downgrade_outline_offbible_spoken,
                            rewrite_outline_abstract_covers,
                            split_outline_over_action_capacity,
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


def test_action_heavy_outline_is_split_to_video_capacity() -> None:
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                scene_setting="日，萧家广场",
                beat="萧薰儿转身穿过人群走向萧炎，在他面前停下叫他萧炎哥哥",
                covers="萧薰儿走到萧炎面前停下，叫出萧炎哥哥",
                story_event_id="E03",
                spine_beat_ids=["S05"],
                key_line_ids=["KL06"],
                information_ids=["I6"],
                state_in="萧薰儿受称赞后站在石碑前",
                primary_action="萧薰儿转身穿过人群走向萧炎并停下",
                state_out="萧薰儿站在萧炎面前",
                continuity_mode="same_scene_cut",
                duration_s=5,
                characters_visible=["萧薰儿"],
                audio_cast=["萧薰儿"],
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_setting="日，萧家广场",
                beat="萧炎抬头回应",
                duration_s=5,
            ),
        ],
    )

    events = split_outline_over_action_capacity(outline, max_shots=16)

    assert len(events) == 1
    assert len(outline.shots) == 3
    front, back = outline.shots[:2]
    assert "动作容量拆分：前段" in front.beat
    assert "动作容量拆分：后段" in back.beat
    assert front.state_out == back.state_in
    assert front.key_line_ids == [] and back.key_line_ids == ["KL06"]
    assert front.information_ids == [] and back.information_ids == ["I6"]
    assert front.audio_cast == [] and back.audio_cast == ["萧薰儿"]
    assert back.continuity_mode == "action_continuation"
    assert [shot.shot_no for shot in outline.shots] == [1, 2, 3]
    # 同一来源节点只允许 +1 相邻镜，重复运行不得继续碎拆。
    assert split_outline_over_action_capacity(outline, max_shots=16) == []


def test_action_split_can_be_forced_when_detailed_shot_expands_outline() -> None:
    outline = StoryboardOutline(
        episode_no=1,
        shots=[StoryboardOutlineShot(
            shot_no=1,
            scene_setting="日，萧家广场",
            beat="萧薰儿走到魔石碑前触碑，石碑亮起耀眼光芒",
            covers="萧薰儿走到魔石碑前，手掌触碑面，石碑亮起耀眼光芒",
            primary_action="萧薰儿走到碑前触碑",
            state_in="萧薰儿尚在人群中",
            state_out="石碑亮起耀眼光芒",
            duration_s=5,
            characters_visible=["萧薰儿"],
        )],
    )

    # 紧凑大纲本身只显式命中两个词表动作；逐镜补写“走出人群”后若被硬门禁拦截，
    # Repair Router 可强制把当前节点拆为“到碑前触碑 → 石碑亮起”。
    events = split_outline_over_action_capacity(
        outline, max_shots=16, shot_nos={1}, force=True,
    )

    assert len(events) == 1
    assert len(outline.shots) == 2
    assert "走到魔石碑前" in outline.shots[0].primary_action
    assert "亮起" in outline.shots[1].primary_action
    assert outline.shots[0].state_out == outline.shots[1].state_in


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


def test_outline_rejects_adjacent_duplicate_key_line_assignment() -> None:
    outline = _outline(_valid_beats(), covers={5: KEY_LINE})
    outline.shots[3].key_line_ids = ["KL1"]
    outline.shots[4].key_line_ids = ["KL1"]

    errors = validate_storyboard_outline(outline, _screenplay(), 50)

    assert any("重复分配关键台词" in error and "KL1" in error for error in errors), errors


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
    """已签发的合成功能身份可开口，不应被降级为旁白。"""
    bible = _bible_with("萧炎", "萧薰儿")
    names = {c.name for c in bible.characters}
    outline = _outline(_valid_beats(),
                       covers={3: "萧炎测验斗之气仅三段，被路人甲宣布为低级"})
    assert _covers_outside_spoken(outline.shots[2].covers, names) == []

    changed = downgrade_outline_offbible_spoken(outline, bible)
    assert changed == []
    assert outline.shots[2].covers == "萧炎测验斗之气仅三段，被路人甲宣布为低级"
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


def test_outline_allows_large_storyboards_when_every_shot_advances() -> None:
    beats = [f"主线推进节拍第{i}镜发生独立局势变化" for i in range(1, 51)]
    outline = _outline(beats, covers={50: KEY_LINE})
    errors = validate_storyboard_outline(outline, _screenplay(), 50)
    assert not any("上限" in e or "镜头数" in e for e in errors)


def test_outline_allows_long_atom_for_deterministic_pre_split() -> None:
    covers = KEY_LINE + "萧炎握紧拳头决心查清斗气消失真相" * 3
    outline = _outline(_valid_beats(), covers={5: covers})

    assert validate_storyboard_outline(outline, _screenplay(), 50) == []


def test_real_shot_12_cover_split_avoids_tiny_tail() -> None:
    covers = "萧炎哥哥；以前你曾经与薰儿说过；要能放下；才能拿起；提放自如；是自在人"

    # 用固定预算测装箱算法；产品口播上限已随 VAL-422 上调到 36，不能再绑死 MAX_SPOKEN。
    chunks = _split_atoms_to_content_budget(_atomize_claim(covers), 16)

    assert [len(_condense(chunk)) for chunk in chunks] == [14, 16]
    assert _condense("".join(chunks)) == _condense("".join(_atomize_claim(covers)))


def test_outline_rejects_abstract_directing_covers() -> None:
    """P0：covers 写纯导演抽象（与萧炎形成反差）必须在大纲阶段硬拦。"""
    outline = _outline(_valid_beats(), covers={5: "与萧炎形成反差"})
    errors = validate_storyboard_outline(outline, _screenplay(), 50)
    assert any("导演抽象" in e and "反差" in e for e in errors), errors


def test_rewrite_outline_abstract_covers_strips_and_is_idempotent() -> None:
    """P1：确定性剥离纯抽象 covers，写入 beat 改写指引；二次运行幂等。"""
    outline = _outline(
        _valid_beats(),
        covers={5: f"{KEY_LINE}；与萧炎形成反差"},
    )
    changed = rewrite_outline_abstract_covers(outline)
    assert changed
    # atomize 会按句号切开，covers 重拼后可能无句末标点；比 condensed 内容即可。
    assert _condense(outline.shots[4].covers) == _condense(KEY_LINE)
    assert "反差" not in outline.shots[4].covers
    assert "导演意图不得写在 covers" in outline.shots[4].beat
    assert "双方可见状态" in outline.shots[4].beat
    assert rewrite_outline_abstract_covers(outline) == []
    assert validate_storyboard_outline(outline, _screenplay(), 50) == []


def test_rewrite_outline_abstract_covers_keeps_concrete_residue() -> None:
    """混合 covers：剥离「形成反差」后保留具体事实残段。"""
    outline = _outline(
        _valid_beats(),
        covers={4: "薰儿测出七段并与萧炎形成反差"},
    )
    # 关键台词仍需覆盖，挂在第 5 镜。
    outline.shots[4].covers = KEY_LINE
    changed = rewrite_outline_abstract_covers(outline)
    assert changed
    assert "形成反差" not in outline.shots[3].covers
    assert "薰儿测出七段" in outline.shots[3].covers
    assert validate_storyboard_outline(outline, _screenplay(), 50) == []
