"""分镜大纲（先规划后逐镜填充，方案 B）的单测：

- validate_storyboard_outline：镜头数范围 / shot_no 连续 / 反停留 / 必保留内容全覆盖；
- _render_storyboard_outline / _outline_brief：把大纲渲染进逐镜 prompt 并标出"本镜"。
"""

from app.schemas import (Bible, Character, EpisodeScreenplay, StoryboardOutline,
                         StoryboardOutlineShot, World)
from app.stages import (_outline_brief, _split_atoms_to_content_budget,
                        _render_storyboard_outline)
from app.validators import (_atomize_claim, _condense,
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


def test_legacy_outline_prose_is_not_split_by_action_words() -> None:
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

    assert events == []
    assert len(outline.shots) == 2
    assert outline.shots[0].key_line_ids == ["KL06"]
    assert outline.shots[0].information_ids == ["I6"]
    assert outline.shots[0].audio_cast == ["萧薰儿"]
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

    # 只有语义修复明确选择拆分能力时，执行器才按结构边界拆成相邻两镜。
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
