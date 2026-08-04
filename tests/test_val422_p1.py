"""VAL-422 P1：迁移、指标、相邻插镜策略。"""
from __future__ import annotations

from app.continuity import migrate_shot_id_spaces
from app.repair_router import route_issues
from app.spoken_contract import audit_legacy_spoken_contract
from app.schemas import AudioTimelineItem, Dialogue
from tests.test_validators import _compact_shot


def test_migrate_shot_id_spaces_moves_s_star() -> None:
    shot = _compact_shot(1)
    shot.story_event_id = "S07"
    actions = migrate_shot_id_spaces(shot)
    assert any("spine_beat_ids" in a for a in actions)
    assert shot.story_event_id == ""
    assert "S07" in (shot.spine_beat_ids or [])
    assert shot.legacy_unvalidated is True


def test_audit_legacy_spoken_conflict() -> None:
    shot = _compact_shot(9)
    shot.dialogues = [Dialogue(speaker="薰儿", line="走吧。", emotion="平静")]
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.3, end_s=5.0, type="spoken_dialogue",
            speaker_id="薰儿", text="你会重新站起来", lip_sync=True,
        )
    ]
    assert audit_legacy_spoken_contract(shot) == "conflict"


def test_route_capacity_keeps_split_as_candidate_until_semantically_selected() -> None:
    plan = route_issues(
        ["shot_no=9 台词纯文字 65 字，超过 10s 口播上限 36 字；请拆镜或把必保留台词挪走"],
        validated_prefix_end=8,
        next_shot_no=9,
    )
    assert plan.strategy == "repair_window"
    assert plan.needs_semantic_selection is True
    assert "split_adjacent_shot" in {
        candidate.strategy for candidate in plan.candidates
    }
    assert plan.invalidation_frontier == 8


def test_route_capacity_escalates_to_split_after_stall() -> None:
    msg = "shot_no=9 台词纯文字 65 字，超过 10s 口播上限 36 字；请拆镜"
    first = route_issues([msg], validated_prefix_end=8, next_shot_no=9)
    assert first.strategy == "repair_window"
    # 连续 stalled 只升级影响范围；具体是否拆镜仍由语义诊断选择。
    stalled = route_issues(
        [msg],
        validated_prefix_end=8,
        next_shot_no=9,
        current_level="L3",
        issue_fingerprint_counts={first.fingerprint: 2},
        semantic_diagnosis={
            "scope": "adjacent_window",
            "selected_strategy": "split_adjacent_shot",
            "reason": "现镜的台词与处理时间无法同时满足",
        },
    )
    assert stalled.level == "L4"
    assert stalled.strategy == "split_adjacent_shot"
    assert all(
        candidate.strategy not in {"redo_suffix", "replan_outline"}
        for candidate in stalled.candidates
    )
