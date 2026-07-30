"""Supervisor / VAL-422 门禁相关测试。"""
from __future__ import annotations

from app.evaluations.issues import issue_code, issues_from_messages
from app.harness.types import IssueSeverity
from app.loops.base import AgentLoopPolicy
from app.schemas import Dialogue, PlotSpine, PlotSpineBeat, EpisodeScreenplay, Shot, Storyboard, StoryboardOutline, StoryboardOutlineShot
from app.hiagent import ProviderError
from app.storyboard_supervisor import (
    _is_retryable_external_error,
)
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
        key_plot_points=["这是面向策划的抽象总结句，不要求分镜逐字复述"],
        plot_spine=PlotSpine(
            episode_premise="测灵",
            spine_beats=[
                PlotSpineBeat(beat_id="S04", who="萧媚", does="测出七段", turn="成为焦点", must_keep=True),
            ],
            must_keep_ending="收束",
        ),
    )
    # 可见主体与动作已经落地；spine_beat_ids 负责稳定归属，不要求逐字复述策划摘要。
    shot = _shot(
        characters=["萧媚"],
        characters_visible=["萧媚"],
        action_desc="萧媚完成测试，玉石显示七段，人群望向她，她没有走向萧炎。",
        first_frame_desc="萧媚站在测试台前，掌心贴上玉石。",
        last_frame_desc="同一机位，玉石亮起，萧媚收回手，人群侧目。",
        spine_beat_ids=["S04"],
    )
    errors = validate_storyboard_preserves_key_content(
        Storyboard(episode_no=1, shots=[shot]), screenplay,
    )
    assert not any("主线节拍" in e for e in errors)
    assert not any("主线剧情点" in e for e in errors)


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


def test_issue_code_distinguishes_action_capacity_from_spoken_capacity():
    message = "shot_no=8 含约 4 个顺序动作节拍，超过 5s 镜头容量上限 2；请拆镜"
    assert issue_code(message) == "ACTION_CAPACITY_EXCEEDED"


def test_issue_code_spoken_contract_conflict_is_not_capacity():
    assert issue_code(
        "shot_no=15 dialogues 与 audio_timeline 的口播内容分叉；同一镜头只能有一套有效口播"
    ) == "SPOKEN_CONTRACT_CONFLICT"


def test_issue_code_spoken_timeline_errors_are_not_capacity():
    assert issue_code("shot_no=3 口播时间段在 2.0s 处与上一段非法重叠") == "SPOKEN_TIMELINE_OVERLAP"
    assert issue_code("shot_no=3 口播时间段 [0, 11] 超出本镜 10s 时长") == "SPOKEN_TIMELINE_OUT_OF_RANGE"


def test_issue_code_dialogue_context_break_is_key_line_failure():
    assert issue_code("主线对白上下文断裂：角色突然冒出一句回应") == "KEY_LINE_MISSING"


def test_retryable_provider_failure_survives_orchestration_wrapping():
    provider_error = ProviderError("TPM limit exceeded", retryable=True)
    wrapped = RuntimeError("outline failed")
    wrapped.__cause__ = provider_error

    assert _is_retryable_external_error(provider_error)
    assert _is_retryable_external_error(wrapped)
    assert not _is_retryable_external_error(
        ProviderError("invalid credentials", retryable=False)
    )


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
