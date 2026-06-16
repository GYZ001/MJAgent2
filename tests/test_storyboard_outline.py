"""分镜大纲（先规划后逐镜填充，方案 B）的单测：

- validate_storyboard_outline：镜头数范围 / shot_no 连续 / 反停留 / 必保留内容全覆盖；
- _render_storyboard_outline / _outline_brief：把大纲渲染进逐镜 prompt 并标出"本镜"。
"""

from app.schemas import EpisodeScreenplay, StoryboardOutline, StoryboardOutlineShot
from app.stages import _outline_brief, _render_storyboard_outline
from app.validators import validate_storyboard_outline

KEY_LINE = "我一定要查清斗气消失的真相。"
KEY_POINT = "萧炎测出斗之力三段被族人嘲讽"


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


def test_outline_rejects_too_few_shots() -> None:
    outline = _outline(_valid_beats()[:2], covers={2: KEY_LINE})
    errors = validate_storyboard_outline(outline, _screenplay(), 50)
    assert any("大纲镜头数" in e for e in errors)


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


def test_outline_brief_lookup() -> None:
    outline = _outline(_valid_beats(), covers={5: KEY_LINE})
    assert _outline_brief(outline, 5).covers == KEY_LINE
    assert _outline_brief(outline, 99) is None
    assert _outline_brief(None, 1) is None
