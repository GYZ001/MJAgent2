"""分镜大纲（先规划后逐镜填充，方案 B）的单测：

- validate_storyboard_outline：镜头数范围 / shot_no 连续 / 反停留 / 必保留内容全覆盖。

（_render_storyboard_outline / _outline_brief 连同它们所属的逐镜生成管线
generate_storyboard_next_shot 已删除——storyboard 2.0.0 起 prep_pack 集全部
走 app/production/storyboard_pack.py，不再逐镜渲染大纲；这两个函数原有的
测试已随之移除。）
"""

from app.schemas import (Bible, Character, EpisodeScreenplay, StoryboardOutline,
                         StoryboardOutlineShot, World)
from app.validators import (split_outline_over_action_capacity,
                            validate_storyboard_outline)

KEY_LINE = "我一定要查清斗气消失的真相。"
KEY_POINT = "甲一测出测验力三段被族人嘲讽"


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


def _outline(beats: list[str], *, scene: str = "日，甲家测验广场",
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
        "甲一站上测验台，魔石碑亮起准备测验",
        "魔石碑显出测验力三段，全场哗然",
        "测验员宣布等级低级，族人哄笑嘲讽",
        "甲一强忍屈辱，落寞转身回到队伍末尾",
        "甲一暗下决心立誓查清斗气消失的真相",
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
                scene_setting="日，甲家广场",
                beat="甲二儿转身穿过人群走向甲一，在他面前停下叫他甲一哥哥",
                covers="甲二儿走到甲一面前停下，叫出甲一哥哥",
                story_event_id="E03",
                spine_beat_ids=["S05"],
                key_line_ids=["KL06"],
                information_ids=["I6"],
                state_in="甲二儿受称赞后站在石碑前",
                primary_action="甲二儿转身穿过人群走向甲一并停下",
                state_out="甲二儿站在甲一面前",
                continuity_mode="same_scene_cut",
                duration_s=5,
                characters_visible=["甲二儿"],
                audio_cast=["甲二儿"],
            ),
            StoryboardOutlineShot(
                shot_no=2,
                scene_setting="日，甲家广场",
                beat="甲一抬头回应",
                duration_s=5,
            ),
        ],
    )

    events = split_outline_over_action_capacity(outline, max_shots=16)

    assert events == []
    assert len(outline.shots) == 2
    assert outline.shots[0].key_line_ids == ["KL06"]
    assert outline.shots[0].information_ids == ["I6"]
    assert outline.shots[0].audio_cast == ["甲二儿"]
    assert split_outline_over_action_capacity(outline, max_shots=16) == []


def test_action_split_can_be_forced_when_detailed_shot_expands_outline() -> None:
    outline = StoryboardOutline(
        episode_no=1,
        shots=[StoryboardOutlineShot(
            shot_no=1,
            scene_setting="日，甲家广场",
            beat="甲二儿走到魔石碑前触碑，石碑亮起耀眼光芒",
            covers="甲二儿走到魔石碑前，手掌触碑面，石碑亮起耀眼光芒",
            primary_action="甲二儿走到碑前触碑",
            state_in="甲二儿尚在人群中",
            state_out="石碑亮起耀眼光芒",
            duration_s=5,
            characters_visible=["甲二儿"],
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


def test_outline_key_line_ids_override_paraphrased_covers_for_missing_check() -> None:
    """真实回归（EP6 run_9bfcd5cbe128，2026-08-25）：模型用 key_line_ids 正确、完整
    地把关键台词分配到镜头，但 covers 摘要写的是转述而非近似引用原句，散文模糊
    匹配因此把已经结构化分配的台词误判成"未安排"（真实产物：孟浩的 3 条内心独白
    被完整分配了 key_line_ids，covers 却写成情节转述，散文匹配判定"未安排"）。
    只要 key_line_ids 已覆盖该 catalog 条目，就不应该再被 covers 散文否决——
    覆盖率的权威判据是结构化 ID 台账，不是散文摘要像不像台词，见
    validate_storyboard_outline 里 missing_lines 分支的完整论证。

    关键台词/beat/covers 三处文字刻意互不重叠（与 KEY_LINE/_valid_beats() 不同，
    _valid_beats() 第 5 镜 beat 本身近似复述了 KEY_LINE，会让散文匹配意外通过，
    掩盖这条判据到底测的是什么），确保本用例只有在真正读取 key_line_ids 时才通过。
    """
    key_line = "你若敢再靠近半步，我便让你后悔一生。"
    screenplay = EpisodeScreenplay(
        episode_no=1, title="陨落的天才", full_script_text="略",
        key_lines=[key_line], key_plot_points=[KEY_POINT])
    outline = _outline(_valid_beats(), covers={5: "甲一神情冷冽地撂下一句话，转身离开测验场"})
    # 第 5 镜 covers 只写了动作转述，从未出现 key_line 的任何字面片段；
    # 是否"安排"完全靠 key_line_ids 结构化声明来判定。
    outline.shots[4].key_line_ids = ["KL01"]

    errors = validate_storyboard_outline(outline, screenplay, 50)

    assert not any("未安排" in e and "关键台词" in e for e in errors), errors


def test_outline_key_line_ids_used_but_one_catalog_entry_unassigned_still_flagged() -> None:
    """结构化判据不是"用了 key_line_ids 就整体放行"：某条 catalog 台词从未出现在
    任何一镜的 key_line_ids 里时，仍必须硬拦——这是真正的漏戏，不能因为大纲用了
    ID 规划就一并放过。"""
    screenplay = EpisodeScreenplay(
        episode_no=1, title="陨落的天才", full_script_text="略",
        key_lines=[KEY_LINE, "你们终将后悔今日的嘲笑。"], key_plot_points=[KEY_POINT])
    outline = _outline(_valid_beats())
    outline.shots[4].key_line_ids = ["KL01"]
    # KL02（"你们终将后悔今日的嘲笑。"）从未分配到任何一镜的 key_line_ids。

    errors = validate_storyboard_outline(outline, screenplay, 50)

    assert any("未安排" in e and "关键台词" in e for e in errors), errors


def test_outline_allows_large_storyboards_when_every_shot_advances() -> None:
    beats = [f"主线推进节拍第{i}镜发生独立局势变化" for i in range(1, 51)]
    outline = _outline(beats, covers={50: KEY_LINE})
    errors = validate_storyboard_outline(outline, _screenplay(), 50)
    assert not any("上限" in e or "镜头数" in e for e in errors)


def test_outline_allows_long_atom_for_deterministic_pre_split() -> None:
    covers = KEY_LINE + "甲一握紧拳头决心查清斗气消失真相" * 3
    outline = _outline(_valid_beats(), covers={5: covers})

    assert validate_storyboard_outline(outline, _screenplay(), 50) == []

