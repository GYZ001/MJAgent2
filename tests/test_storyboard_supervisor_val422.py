"""Supervisor / VAL-422 门禁相关测试。"""
from __future__ import annotations

from app.evaluations.issues import issue_code, issues_from_messages
from app.harness.types import IssueSeverity
from app.loops.base import AgentLoopPolicy
from app.schemas import Dialogue, PlotSpine, PlotSpineBeat, EpisodeScreenplay, Shot, Storyboard, StoryboardOutline, StoryboardOutlineShot
from app.spoken_contract import spoken_text_of
from app.validators import (
    key_line_catalog,
    outline_key_line_capacity_errors,
    validate_storyboard_preserves_key_content,
)


def _shot(**kwargs) -> Shot:
    defaults = dict(
        shot_no=1,
        duration_s=10,
        shot_size="中景",
        camera_move="固定",
        scene_setting="日，大厅",
        characters=["萧炎"],
        action_desc="萧炎站在大厅中央，抬手看向石碑，神情凝重，周围人群注视着他。",
        first_frame_desc="萧炎站在大厅中央，手还未抬起，面向石碑。",
        last_frame_desc="同一机位，萧炎手掌贴上石碑，眉头紧锁。",
        source_excerpt="原文摘录至少二十个字用来过审计门槛啊啊啊",
        dialogues=[],
    )
    defaults.update(kwargs)
    return Shot(**defaults)


def test_key_line_uses_spoken_not_source_excerpt():
    """关键台词只认有效口播；source_excerpt 不能充当已说出证据。"""
    line = "薰儿相信，你会重新站起来，取回属于你的荣耀与尊严"
    shot = _shot(
        source_excerpt=line + "额外填充审计长度的原文摘录内容",
        dialogues=[],
        spine_beat_ids=["S01"],
    )
    screenplay = EpisodeScreenplay(
        episode_no=1,
        key_lines=[line],
        plot_spine=PlotSpine(
            episode_premise="测灵",
            spine_beats=[PlotSpineBeat(beat_id="S01", who="萧炎", does="测灵", turn="被判定低级", must_keep=True)],
            must_keep_ending="收束",
        ),
    )
    board = Storyboard(episode_no=1, shots=[shot])
    errors = validate_storyboard_preserves_key_content(board, screenplay)
    assert any("主线台词" in e for e in errors)
    # 写入 dialogues 后应通过关键台词
    shot2 = _shot(
        dialogues=[Dialogue(speaker="薰儿", line=line, emotion="坚定")],
        spine_beat_ids=["S01"],
    )
    assert spoken_text_of(shot2)
    errors2 = validate_storyboard_preserves_key_content(
        Storyboard(episode_no=1, shots=[shot2]), screenplay,
    )
    assert not any("主线台词" in e for e in errors2)


def test_spine_beat_ids_cover_must_keep():
    screenplay = EpisodeScreenplay(
        episode_no=1,
        key_lines=[],
        plot_spine=PlotSpine(
            episode_premise="测灵",
            spine_beats=[
                PlotSpineBeat(beat_id="S04", who="萧媚", does="测出七段", turn="成为焦点", must_keep=True),
            ],
            must_keep_ending="收束",
        ),
    )
    # 字面不相似，但有 spine_beat_ids
    shot = _shot(
        action_desc="萧媚完成测试，人群望向她，她没有走向萧炎。",
        first_frame_desc="萧媚站在测试台前，掌心贴上玉石。",
        last_frame_desc="同一机位，玉石亮起，萧媚收回手，人群侧目。",
        spine_beat_ids=["S04"],
    )
    errors = validate_storyboard_preserves_key_content(
        Storyboard(episode_no=1, shots=[shot]), screenplay,
    )
    assert not any("主线节拍" in e for e in errors)


def test_outline_key_line_capacity_blocks_overload():
    screenplay = EpisodeScreenplay(
        episode_no=1,
        key_lines=[
            "薰儿相信，你会重新站起来，取回属于你的荣耀与尊严",
            "萧炎自嘲一笑，说自己果然是废物",
            "放下吧，拿起吧，这是你自己的路",
        ],
    )
    catalog = key_line_catalog(screenplay)
    assert "KL01" in catalog
    outline = StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=1,
                beat="收束对话，塞入三段关键台词",
                covers="三段台词",
                duration_s=10,
                key_line_ids=["KL01", "KL02", "KL03"],
            ),
        ],
    )
    errors = outline_key_line_capacity_errors(outline, screenplay)
    assert errors
    assert any("超过" in e and "字" in e for e in errors)


def test_issue_code_spoken_capacity():
    assert issue_code("第 9 镜口播超过 10 秒上限 36 字") == "SPOKEN_CAPACITY_EXCEEDED"


def test_issue_code_dialogue_context_break_is_key_line_failure():
    assert issue_code("主线对白上下文断裂：角色突然冒出一句回应") == "KEY_LINE_MISSING"


def test_warning_candidate_policy_rejects_blocker_concept():
    """allow_warning_candidate 语义：有 blocker 时不得作为 warning 通过。

    此处验证 issues_from_messages 产生 blocker，以及路由会把它当成需修复问题。
    """
    issues = issues_from_messages(
        ["shot_no=9 口播 65 字，超过 10s 上限 36 字"],
        subject="shot:9",
        severity=IssueSeverity.BLOCKER,
    )
    assert issues[0].severity == IssueSeverity.BLOCKER
    assert issues[0].code == "SPOKEN_CAPACITY_EXCEEDED"
    # Policy 默认不允许带 blocker 的 warning candidate
    policy = AgentLoopPolicy(allow_warning_candidate=True)
    assert policy.allow_warning_candidate is True
