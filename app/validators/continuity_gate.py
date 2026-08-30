"""C2 基于完整剧本的分镜连续性合同校验（V6）：连续性归一化、口播溢出处理、
镜头时长偏好。
"""
from __future__ import annotations

from app import config
from app.continuity import (
    action_capacity_errors,
    action_capacity_limit,
    count_sequential_action_beats,
    information_ledger_errors,
    narrative_action_capacity_profile,
    normalize_board_continuity,
    speech_capacity_errors,
    spoken_chars_from_shot,
    state_chain_errors,
    sync_shot_continuity_fields,
)
from app.renderability import (
    DURATION_REVIEW_RISK_TAG,
    HUMAN_DURATION_REVIEW_TAG,
    PREFERRED_SHOT_DURATION_S,
    shot_duration_should_prefer_five,
)
from app.schemas import (
    EpisodeScreenplay,
    NarrativeContinuityPlan,
    Shot,
    Storyboard,
)
from app.spoken_contract import (
    RULE_SPOKEN_COHERENCE,
    build_timeline_from_segments,
    segments_from_timeline,
    validate_spoken_contract,
)

from .primitives import _shot_capacity_budget_total

def normalize_continuity(board: Storyboard) -> None:
    """保持旧调用入口；实际连续性归一化由 continuity 模块按 continuity_mode 执行。"""
    for i, shot in enumerate(board.shots):
        prev = board.shots[i - 1] if i > 0 else None
        sync_shot_continuity_fields(shot, prev)
    normalize_board_continuity(board)


def validate_storyboard_continuity_contract(
    board: Storyboard,
    screenplay: EpisodeScreenplay | None = None,
) -> list[str]:
    """PRD 连续性合同校验：状态链、信息台账、单镜动作/口播容量。"""
    errors: list[str] = []
    narrative_plan = screenplay.narrative_plan if screenplay else None
    for shot in board.shots:
        errors.extend(action_capacity_errors(
            shot,
            narrative_authority=narrative_plan is not None,
            narrative_plan=narrative_plan,
        ))
        errors.extend(speech_capacity_errors(shot))
    errors.extend(state_chain_errors(
        board,
        narrative_authority=narrative_plan is not None,
    ))
    errors.extend(information_ledger_errors(board, screenplay))
    return errors


def spoken_char_count(shot) -> int:
    """本镜真实台词纯文字字数（去空白与标点），与单镜口播上限校验同口径。"""
    return spoken_chars_from_shot(shot)


def strip_all_narration(board: Storyboard) -> list[dict]:
    """确定性清空旁白：产品禁止 narration / 内心OS / timeline narration 轨。

    不清空内容翻译成台词（避免发明对白）；信息改由已有 dialogues 或 action_desc 画面承载。
    """
    changes: list[dict] = []
    for shot in board.shots:
        changed = False
        narration = (shot.narration or "").strip()
        if narration:
            shot.narration = ""
            changed = True
            changes.append({"shot_no": shot.shot_no, "cleared_narration": narration[:40]})
        if shot.audio_timeline:
            kept = [item for item in shot.audio_timeline if item.type != "narration"]
            if len(kept) != len(shot.audio_timeline):
                shot.audio_timeline = kept
                changed = True
                if not any(c.get("shot_no") == shot.shot_no and "cleared_narration" in c for c in changes):
                    changes.append({"shot_no": shot.shot_no, "stripped_timeline_narration": True})
            elif changed and not kept:
                shot.audio_timeline = []
        if changed and not (shot.narration or "").strip():
            # 确保字段为规范化空值
            shot.narration = ""
    return changes


def relieve_spoken_overflow(board: Storyboard) -> list[dict]:
    """兼容旧调用：先清空全部旁白，再按口播上限检查（旁白不再参与口播）。"""
    return strip_all_narration(board)


def _retime_coherent_spoken_timeline(shot: Shot) -> bool:
    """Duration normalization must not manufacture an out-of-range timeline.

    A generated candidate may legitimately choose any duration above
    PREFERRED_SHOT_DURATION_S (up to config.VIDEO_DURATION_MAX_S) and place
    its spoken segments across that interval.  ``prefer_default_shot_durations``
    can subsequently compress the shot to five seconds.  When dialogues and
    timeline still describe the same speech, retiming is an unambiguous
    derived-field repair.  A genuine dialogues/timeline fork remains untouched
    so the spoken-contract gate can report it instead of silently picking a
    side.
    """
    if not shot.audio_timeline:
        return False
    issues = validate_spoken_contract(shot)
    if any(issue.rule_id == RULE_SPOKEN_COHERENCE for issue in issues):
        return False
    spoken = segments_from_timeline(shot)
    if not spoken:
        return False
    shot.audio_timeline = build_timeline_from_segments(shot, spoken)
    return True


def prefer_default_shot_durations(
    board: Storyboard,
    *,
    narrative_authority: bool = False,
    narrative_plan: NarrativeContinuityPlan | None = None,
) -> list[dict]:
    """主线压缩：能 5s 讲完的镜压回 5s；仍需更长时长的镜打上 AI 审核标记。"""
    changes: list[dict] = []
    for shot in board.shots:
        spoken = spoken_char_count(shot)
        if narrative_authority:
            beats, minimum_s, contract_errors = narrative_action_capacity_profile(
                shot, narrative_plan,
            )
            # Missing/drifted authority data must be reported by the validator;
            # duration normalization may not guess a safe rewrite.
            fits_default = (
                not contract_errors
                and minimum_s <= PREFERRED_SHOT_DURATION_S
                and beats <= action_capacity_limit(PREFERRED_SHOT_DURATION_S)
                and _shot_capacity_budget_total(shot) <= PREFERRED_SHOT_DURATION_S
                and spoken <= config.max_spoken_chars_for_duration(PREFERRED_SHOT_DURATION_S)
            )
        else:
            beats = count_sequential_action_beats(
                (shot.primary_action or shot.action_desc or "").strip()
            )
            fits_default = shot_duration_should_prefer_five(
                spoken_chars=spoken,
                action_beats=beats,
            )
        tags = list(shot.risk_tags or [])
        if HUMAN_DURATION_REVIEW_TAG in tags:
            if DURATION_REVIEW_RISK_TAG in tags:
                shot.risk_tags = [
                    tag for tag in tags if tag != DURATION_REVIEW_RISK_TAG
                ]
            changes.append({
                "shot_no": shot.shot_no,
                "duration_s": shot.duration_s,
                "reason": "human_duration_review_preserved",
            })
            continue
        if fits_default:
            duration_changed = int(shot.duration_s or 0) != PREFERRED_SHOT_DURATION_S
            if duration_changed:
                changes.append({
                    "shot_no": shot.shot_no,
                    "from": shot.duration_s,
                    "to": PREFERRED_SHOT_DURATION_S,
                    "reason": "content_fits_5s",
                })
                shot.duration_s = PREFERRED_SHOT_DURATION_S
                if _retime_coherent_spoken_timeline(shot):
                    changes.append({
                        "shot_no": shot.shot_no,
                        "duration_s": shot.duration_s,
                        "reason": "retimed_audio_after_duration_normalization",
                    })
            if DURATION_REVIEW_RISK_TAG in tags:
                tags = [t for t in tags if t != DURATION_REVIEW_RISK_TAG]
                shot.risk_tags = tags
            continue
        if int(shot.duration_s or 0) > PREFERRED_SHOT_DURATION_S:
            if DURATION_REVIEW_RISK_TAG not in tags:
                tags.append(DURATION_REVIEW_RISK_TAG)
                shot.risk_tags = tags
                changes.append({
                    "shot_no": shot.shot_no,
                    "duration_s": shot.duration_s,
                    "reason": "needs_ai_duration_review",
                })
        elif DURATION_REVIEW_RISK_TAG in tags:
            shot.risk_tags = [t for t in tags if t != DURATION_REVIEW_RISK_TAG]
    return changes
