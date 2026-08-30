"""叙事大纲动作/台词容量拆分与场景覆盖校验——V5 动作节拍容量判据在
分镜大纲阶段的落地。
"""
from __future__ import annotations

from typing import Any

from app import config
from app.continuity import (
    action_capacity_errors,
    action_capacity_limit,
    count_sequential_action_beats,
)
from app.scene_contract import split_legacy_scene_setting
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    NarrativeContinuityPlan,
    StoryboardOutline,
)
from app.spoken_contract import (
    content_char_count,
    max_speech_chars,
)

from .outline_dialogue import _split_outline_text_outside_quotes
from .primitives import _normalize_scene_label
from .scene_match import match_scene_name
from .screenplay_text import (
    _atomize_claim,
    _strip_speaker,
)
from .storyboard_delivery import key_line_catalog

_ACTION_CAPACITY_SPLIT_MARKER = "动作容量拆分"


def narrative_outline_action_capacity_errors(
    outline: StoryboardOutline,
    narrative_plan: NarrativeContinuityPlan | None,
) -> list[str]:
    """Validate outline ShotTasks from AtomicAction structure, never prose.

    This is the authority-path counterpart of the legacy deterministic
    splitter.  It reports an invalid allocation for semantic AI repair instead
    of rewriting IDs/state ownership after the fact.
    """
    errors: list[str] = []
    for shot in outline.shots or []:
        errors.extend(action_capacity_errors(
            shot,  # StoryboardOutlineShot exposes the same narrative task fields.
            narrative_authority=True,
            narrative_plan=narrative_plan,
        ))
    return list(dict.fromkeys(errors))


def _outline_action_candidate(shot: Any) -> tuple[str, int, int]:
    """Choose the outline field that exposes the richest action sequence."""
    candidates = [
        str(value or "").strip()
        for value in (shot.primary_action, shot.beat, shot.covers)
        if str(value or "").strip()
    ]
    if not candidates:
        return "", 0, 0
    scored = [
        (text, count_sequential_action_beats(text), len(_atomize_claim(text)))
        for text in candidates
    ]
    return max(scored, key=lambda item: (item[1], item[2], len(item[0])))


def _split_outline_action_text(
    text: str,
    *,
    limit: int,
    force: bool,
) -> tuple[str, str] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    atoms = _split_outline_text_outside_quotes(
        raw,
        separators="；;。.！!？?，,、\n",
    )
    if len(atoms) < 2:
        return None
    split_at = max(1, len(atoms) // 2)
    return "；".join(atoms[:split_at]), "；".join(atoms[split_at:])


def split_outline_over_action_capacity(
    outline: StoryboardOutline,
    *,
    max_shots: int,
    shot_nos: set[int] | None = None,
    force: bool = False,
    narrative_authority: bool = False,
    narrative_plan: NarrativeContinuityPlan | None = None,
) -> list[dict]:
    """Apply an explicitly requested structural split to a legacy outline."""
    from app.schemas import StoryboardOutlineShot

    if narrative_authority:
        # The authority graph owns action identity, state effects and legal
        # phase boundaries.  A text splitter cannot safely manufacture a new
        # ShotTask, so semantic repair consumes
        # narrative_outline_action_capacity_errors() and proposes a complete
        # candidate allocation instead.  ``narrative_plan`` is accepted here
        # to make accidental authority-path calls explicit and auditable.
        _ = narrative_plan
        return []

    if not force:
        return []

    if not outline.shots or len(outline.shots) >= max_shots:
        return []
    restrict_to_targets = shot_nos is not None
    targets = {int(no) for no in (shot_nos or set()) if int(no) > 0}
    events: list[dict] = []
    index = 0
    while index < len(outline.shots) and len(outline.shots) < max_shots:
        if restrict_to_targets and not targets:
            break
        shot = outline.shots[index]
        original_no = int(shot.shot_no)
        if restrict_to_targets and original_no not in targets:
            index += 1
            continue
        if _ACTION_CAPACITY_SPLIT_MARKER in (shot.beat or ""):
            targets.discard(original_no)
            index += 1
            continue
        plan_text, beats, atom_count = _outline_action_candidate(shot)
        limit = action_capacity_limit(shot.duration_s)
        if not force and beats <= limit and atom_count <= limit:
            targets.discard(original_no)
            index += 1
            continue
        plan_parts = _split_outline_action_text(plan_text, limit=limit, force=force)
        if plan_parts is None:
            targets.discard(original_no)
            index += 1
            continue
        front_action, back_action = plan_parts
        if not front_action.strip() or not back_action.strip():
            targets.discard(original_no)
            index += 1
            continue

        cover_parts = _split_outline_action_text(
            shot.covers or "", limit=limit, force=True,
        )
        front_covers, back_covers = (
            cover_parts if cover_parts is not None else ("", shot.covers or "")
        )
        before_count = len(outline.shots)
        original_state_out = shot.state_out
        original_information = list(shot.information_ids or [])
        original_new_information = list(shot.new_information_ids or [])
        original_key_lines = list(shot.key_line_ids or [])
        original_audio_cast = list(shot.audio_cast or [])
        intermediate_state = f"{front_action.rstrip('。')}完成，准备承接后续动作"

        shot.beat = f"（{_ACTION_CAPACITY_SPLIT_MARKER}：前段）{front_action}"
        shot.primary_action = front_action
        shot.covers = front_covers
        shot.state_out = intermediate_state
        shot.key_line_ids = []
        shot.information_ids = []
        shot.new_information_ids = []
        shot.audio_cast = []

        outline.shots.insert(
            index + 1,
            StoryboardOutlineShot(
                shot_no=original_no + 1,
                scene_id=shot.scene_id,
                scene_time=shot.scene_time,
                scene_name=shot.scene_name,
                scene_setting=shot.scene_setting,
                beat=f"（{_ACTION_CAPACITY_SPLIT_MARKER}：后段）{back_action}",
                covers=back_covers,
                story_event_id=shot.story_event_id,
                spine_beat_ids=list(shot.spine_beat_ids or []),
                key_line_ids=original_key_lines,
                information_ids=original_information,
                new_information_ids=original_new_information,
                state_in=intermediate_state,
                primary_action=back_action,
                emotion_beat=shot.emotion_beat,
                state_out=original_state_out,
                continuity_mode="action_continuation",
                duration_s=shot.duration_s or config.DEFAULT_VIDEO_DURATION_S,
                characters_visible=list(shot.characters_visible or []),
                audio_cast=original_audio_cast,
            ),
        )
        for shot_index, item in enumerate(outline.shots, start=1):
            item.shot_no = shot_index
        events.append({
            "shot_no": original_no,
            "estimated_action_beats": beats,
            "action_atoms": atom_count,
            "capacity": limit,
            "front_action": front_action,
            "back_action": back_action,
            "shots_before": before_count,
            "shots_after": len(outline.shots),
            "reason": "sequential_action_beats_exceed_video_capacity",
            "forced": force,
        })
        targets.discard(original_no)
        index += 2
    return events


def split_outline_over_key_line_capacity(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    *,
    max_shots: int,
) -> list[dict]:
    """把超出口播容量的 key_line_ids 拆到相邻镜头（PRD §4.2）。

    在进入逐镜生成前执行；拆分后重排 shot_no。返回每次拆分的遥测记录。
    """
    from app.schemas import StoryboardOutlineShot

    catalog = key_line_catalog(screenplay)
    if not catalog or not outline.shots:
        return []
    events: list[dict] = []
    # 反复拆直到不再超容或触顶；单轮最多拆 len(shots) 次避免死循环。
    for _ in range(max(1, len(outline.shots))):
        if len(outline.shots) >= max_shots:
            break
        overflow_index: int | None = None
        overflow_kids: list[str] = []
        capacity = 0
        required = 0
        for idx, shot in enumerate(outline.shots):
            duration = int(shot.duration_s or config.DEFAULT_VIDEO_DURATION_S)
            capacity = max_speech_chars(duration)
            kids = [str(k).strip().upper() for k in (shot.key_line_ids or []) if str(k).strip()]
            required = sum(
                content_char_count(_strip_speaker(catalog[k]))
                for k in kids if k in catalog
            )
            if required > capacity and len(kids) >= 2:
                overflow_index = idx
                overflow_kids = kids
                break
        if overflow_index is None:
            break
        # 尽量让前半不超过容量：从后往前挪出可移动的 KL*。
        keep: list[str] = []
        move: list[str] = []
        running = 0
        for kid in overflow_kids:
            chars = content_char_count(_strip_speaker(catalog.get(kid, "")))
            if not keep or running + chars <= capacity:
                keep.append(kid)
                running += chars
            else:
                move.append(kid)
        if not move:
            # 单条已超容：仍拆出最后一条，逼迫下游用 adapted_line / 人工处理。
            keep, move = overflow_kids[:-1] or overflow_kids[:1], overflow_kids[-1:]
        current = outline.shots[overflow_index]
        before_count = len(outline.shots)
        current.key_line_ids = keep
        # covers 也按句读拆半，避免新镜 covers 空。
        atoms = _atomize_claim(current.covers or "")
        if len(atoms) >= 2:
            split_at = max(1, len(atoms) // 2)
            front, back = "；".join(atoms[:split_at]), "；".join(atoms[split_at:])
            current.covers = front
        else:
            back = current.covers or "；".join(move)
        insert_at = overflow_index + 1
        outline.shots.insert(
            insert_at,
            StoryboardOutlineShot(
                shot_no=current.shot_no + 1,
                scene_id=current.scene_id,
                scene_time=current.scene_time,
                scene_name=current.scene_name,
                scene_setting=current.scene_setting,
                beat=f"（容量拆分：承接第{current.shot_no}镜关键台词）{back}",
                covers=back,
                key_line_ids=move,
                spine_beat_ids=list(current.spine_beat_ids or []),
                information_ids=list(current.information_ids or []),
                story_event_id=current.story_event_id,
                state_in=current.state_out or current.state_in,
                primary_action=back[:40] or current.primary_action,
                state_out=current.state_out,
                continuity_mode=current.continuity_mode or "same_scene_cut",
                duration_s=current.duration_s or config.DEFAULT_VIDEO_DURATION_S,
                characters_visible=list(current.characters_visible or []),
                audio_cast=list(current.audio_cast or []),
            ),
        )
        for i, s in enumerate(outline.shots):
            s.shot_no = i + 1
        events.append({
            "shot_no": current.shot_no,
            "required_chars": required,
            "capacity": capacity,
            "kept_key_line_ids": keep,
            "moved_key_line_ids": move,
            "shots_before": before_count,
            "shots_after": len(outline.shots),
            "reason": "required_spoken_chars_exceed_max_capacity",
        })
    return events


def outline_scene_coverage_errors(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    bible: Bible | None = None,
) -> list[str]:
    """Require every screenplay scene to own an ordered outline shot."""
    if not screenplay.scene_outline:
        return []

    scene_contracts = list(
        getattr(getattr(screenplay, "narrative_plan", None), "scene_contracts", None)
        or []
    )
    expected_scene_ids = [
        str(contract.scene_id or "").strip()
        for contract in scene_contracts
    ]
    shot_scene_ids = [
        str(shot.scene_id or "").strip()
        for shot in outline.shots
    ]
    stable_scene_contract = (
        len(expected_scene_ids) == len(screenplay.scene_outline)
        and all(expected_scene_ids)
        and len(set(expected_scene_ids)) == len(expected_scene_ids)
    )
    if stable_scene_contract and any(shot_scene_ids):
        errors: list[str] = []
        search_from = 0
        for scene, expected_scene_id in zip(
            screenplay.scene_outline,
            expected_scene_ids,
            strict=True,
        ):
            matched_index = next(
                (
                    index
                    for index in range(search_from, len(shot_scene_ids))
                    if shot_scene_ids[index] == expected_scene_id
                ),
                None,
            )
            if matched_index is None:
                errors.append(
                    "[OUTLINE_SCENE_COVERAGE_MISSING] "
                    f"剧本第 {scene.scene_no} 场「{scene.scene_heading}」"
                    f"（scene_id={expected_scene_id}）没有按剧情顺序分配任何镜头；"
                    "不得用同一物理地点的其他场次替代该戏剧场次"
                )
                continue
            search_from = matched_index + 1
        return errors

    # Legacy outlines do not carry a dramatic scene identity. Retain their
    # location-based compatibility check, but never use mutable Bible aliases
    # to reinterpret a modern outline that already carries stable scene IDs.
    bible_scenes = list(getattr(bible, "scenes", None) or [])

    def canonical_scene(value: str) -> str:
        matched = (
            match_scene_name(value, bible_scenes, allow_fuzzy=False)
            if bible_scenes
            else None
        )
        if matched:
            return _normalize_scene_label(matched)
        _time, location = split_legacy_scene_setting(value)
        return _normalize_scene_label(location or value)

    shot_scenes = [
        canonical_scene(
            str(shot.scene_name or shot.scene_setting or "")
        )
        for shot in outline.shots
    ]
    errors: list[str] = []
    search_from = 0
    for scene in screenplay.scene_outline:
        expected_scene = canonical_scene(scene.scene_heading)
        matched_index = next(
            (
                index
                for index in range(search_from, len(shot_scenes))
                if expected_scene
                and shot_scenes[index] == expected_scene
            ),
            None,
        )
        if matched_index is None:
            errors.append(
                "[OUTLINE_SCENE_COVERAGE_MISSING] "
                f"剧本第 {scene.scene_no} 场「{scene.scene_heading}」"
                "没有按剧情顺序分配任何镜头；plot_spine 只是最低覆盖线，"
                "不得省略完整剧本中的场次"
            )
            continue
        search_from = matched_index + 1
    return errors
