"""叙事大纲（narrative outline）口播时长归一化、交付 ID 分配、按说话人切分、
对白归属重写。
"""
from __future__ import annotations

import re
from typing import Any

from app import config
from app.schemas import (
    EpisodeScreenplay,
    StoryboardOutline,
)
from app.spoken_contract import (
    content_char_count,
    max_speech_chars,
)

from .screenplay_text import (
    KEY_LINE_BIGRAM_COVERAGE,
    KEY_LINE_PRESENT_RATIO,
    KEY_POINT_COVERAGE,
    _bigram_coverage,
    _condense,
    _longest_run_ratio,
    _speaker_name,
    _strip_speaker,
)
from .storyboard_delivery import key_line_catalog

def normalize_outline_spoken_durations(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[dict]:
    """Raise outline durations to the smallest supported exact-speech window."""
    catalog = key_line_catalog(screenplay)
    allowed = sorted(
        duration
        for duration in config.ALLOWED_DURATIONS
        if config.VIDEO_DURATION_MIN_S
        <= duration
        <= config.VIDEO_DURATION_MAX_S
    )
    if not allowed:
        return []
    changes: list[dict] = []
    for shot in outline.shots or []:
        current = int(shot.duration_s or config.DEFAULT_VIDEO_DURATION_S)
        required_chars = sum(
            content_char_count(_strip_speaker(catalog[key_line_id]))
            for raw_id in (shot.key_line_ids or [])
            if (
                (key_line_id := str(raw_id).strip().upper())
                in catalog
            )
        )
        required_duration = next(
            (
                duration
                for duration in allowed
                if max_speech_chars(duration) >= required_chars
            ),
            allowed[-1],
        )
        normalized = max(
            allowed[0],
            min(allowed[-1], current),
            required_duration,
        )
        if normalized == current:
            continue
        shot.duration_s = normalized
        changes.append({
            "shot_no": shot.shot_no,
            "from_duration_s": current,
            "to_duration_s": normalized,
            "required_chars": required_chars,
            "reason": "exact_spoken_capacity",
        })
    return changes


def assign_outline_delivery_ids(
    outline: StoryboardOutline, screenplay: EpisodeScreenplay
) -> list[dict]:
    """确定性回填大纲 spine_beat_ids / key_line_ids（LLM 漏填时的安全网）。

    按 covers/beat 与剧本台账的模糊匹配把 KL*/S* 分配到镜头；已有 ID 不覆盖。
    返回变更日志供可观测性。
    """
    changes: list[dict] = []
    catalog = key_line_catalog(screenplay)
    spine = screenplay.plot_spine
    beats = list(spine.spine_beats or []) if spine else []
    assigned_kl: set[str] = set()
    for shot in outline.shots or []:
        for kid in shot.key_line_ids or []:
            assigned_kl.add(str(kid).strip().upper())
    for shot in outline.shots or []:
        plan = ((shot.covers or "") + (shot.beat or "")).strip()
        if not plan:
            continue
        if catalog and not (shot.key_line_ids or []):
            matched: list[str] = []
            for kid, text in catalog.items():
                if kid in assigned_kl:
                    continue
                core = _strip_speaker(text)
                if (
                    _longest_run_ratio(core, plan) >= KEY_LINE_PRESENT_RATIO
                    or _bigram_coverage(core, plan) >= KEY_LINE_BIGRAM_COVERAGE
                ):
                    matched.append(kid)
            if matched:
                shot.key_line_ids = matched
                assigned_kl.update(matched)
                changes.append({"shot_no": shot.shot_no, "key_line_ids": matched})
        if beats and not (shot.spine_beat_ids or []):
            matched_beats: list[str] = []
            for beat in beats:
                bid = (beat.beat_id or "").strip().upper()
                if not bid:
                    continue
                claim = f"{beat.who}{beat.does}{beat.turn}"
                if _bigram_coverage(claim, plan) >= KEY_POINT_COVERAGE:
                    matched_beats.append(bid)
            if matched_beats:
                shot.spine_beat_ids = matched_beats
                changes.append({"shot_no": shot.shot_no, "spine_beat_ids": matched_beats})
    return changes


def outline_key_line_speaker_errors(
    outline: StoryboardOutline, screenplay: EpisodeScreenplay
) -> list[str]:
    """大纲阶段禁止把不同说话人的关键台词塞进同一视频镜头。"""
    catalog = key_line_catalog(screenplay)
    errors: list[str] = []
    for shot in outline.shots or []:
        speaker_order: list[str] = []
        for kid in shot.key_line_ids or []:
            text = catalog.get(str(kid).strip().upper(), "")
            speaker = _speaker_name(text)
            if speaker and speaker not in speaker_order:
                speaker_order.append(speaker)
        if len(speaker_order) > 1:
            errors.append(
                "[OUTLINE_KEY_LINE_SPEAKER_MIXED] "
                f"大纲第 {shot.shot_no} 镜分配了多个说话人 {speaker_order}；"
                "请按话轮拆成相邻单人近景/特写，使用 reverse_angle 或 reaction_cut 正反打"
            )
    return errors


def _outline_dialogue_two_shot_required(
    shot: Any, speaker: str,
) -> bool:
    """大纲已有明确双人接触动作时，不把直接互动对象错误裁掉。"""
    visible = [str(name).strip() for name in (shot.characters_visible or []) if str(name).strip()]
    if len(visible) != 2 or speaker not in visible:
        return False
    other = next((name for name in visible if name != speaker), "")
    text = "；".join(
        str(value or "")
        for value in (shot.primary_action, shot.beat, shot.covers)
    )
    interaction = re.compile(
        r"搀扶|扶住|抱住|拥抱|握住|抓住|拉住|推开|挡住|托住|"
        r"递给|递出|接过|抢夺|碰杯|亲吻|背起|抱起|交手|对打|扭打"
    )
    return any(
        speaker in clause and other in clause and interaction.search(clause)
        for clause in re.split(r"[，,。；;！？\n]", text)
    )


def split_outline_on_speaker_changes(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    *,
    max_shots: int,
) -> list[dict]:
    """按关键台词说话人变化确定性拆镜，同一人的连续短句可保留在一镜。"""
    from app.schemas import StoryboardOutlineShot

    catalog = key_line_catalog(screenplay)
    if not catalog or not outline.shots:
        return []
    events: list[dict] = []
    index = 0
    while index < len(outline.shots):
        shot = outline.shots[index]
        kids = [
            str(kid).strip().upper()
            for kid in (shot.key_line_ids or [])
            if str(kid).strip().upper() in catalog
        ]
        groups: list[tuple[str, list[str]]] = []
        for kid in kids:
            speaker = _speaker_name(catalog[kid])
            if groups and groups[-1][0] == speaker:
                groups[-1][1].append(kid)
            else:
                groups.append((speaker, [kid]))
        distinct = [speaker for speaker, _ in groups if speaker]
        if len(set(distinct)) <= 1:
            if distinct:
                if not _outline_dialogue_two_shot_required(shot, distinct[0]):
                    shot.characters_visible = [distinct[0]]
                shot.audio_cast = [distinct[0]]
            index += 1
            continue
        needed = len(groups) - 1
        if len(outline.shots) + needed > max_shots:
            index += 1
            continue

        before_count = len(outline.shots)
        original_state_out = shot.state_out
        original_beat = shot.beat
        original_spine = list(shot.spine_beat_ids or [])
        original_information = list(shot.information_ids or [])
        original_event = shot.story_event_id
        original_duration = shot.duration_s or config.DEFAULT_VIDEO_DURATION_S
        previous_state = shot.state_in
        new_shots: list[StoryboardOutlineShot] = []
        for group_index, (speaker, group_kids) in enumerate(groups):
            lines = [catalog[kid] for kid in group_kids]
            covers = "；".join(lines)
            state_out = (
                original_state_out
                if group_index == len(groups) - 1
                else f"{speaker or '当前说话人'}说完本话轮，听者仍留在画外等待反应"
            )
            if group_index == 0:
                target = shot
                keep_two_shot = _outline_dialogue_two_shot_required(target, speaker)
                target.key_line_ids = list(group_kids)
                target.covers = covers
                if not keep_two_shot:
                    target.primary_action = f"{speaker}单人近景说出本话轮"
                target.state_out = state_out
                target.information_ids = (
                    original_information if group_index == len(groups) - 1 else []
                )
                target.new_information_ids = list(target.information_ids)
                if not keep_two_shot:
                    target.characters_visible = [speaker] if speaker else []
                target.audio_cast = [speaker] if speaker else []
                target.continuity_mode = (
                    "same_scene_cut"
                    if target.continuity_mode == "action_continuation"
                    else (target.continuity_mode or "same_scene_cut")
                )
            else:
                target = StoryboardOutlineShot(
                    shot_no=shot.shot_no + group_index,
                    scene_id=shot.scene_id,
                    scene_time=shot.scene_time,
                    scene_name=shot.scene_name,
                    scene_setting=shot.scene_setting,
                    beat=f"（对白正反打）{speaker}承接上一话轮作出回应；{original_beat}",
                    covers=covers,
                    story_event_id=original_event,
                    spine_beat_ids=original_spine,
                    key_line_ids=list(group_kids),
                    information_ids=(original_information if group_index == len(groups) - 1 else []),
                    new_information_ids=(original_information if group_index == len(groups) - 1 else []),
                    state_in=previous_state,
                    primary_action=f"{speaker}单人近景说出本话轮",
                    state_out=state_out,
                    continuity_mode="reverse_angle",
                    duration_s=original_duration,
                    characters_visible=[speaker] if speaker else [],
                    audio_cast=[speaker] if speaker else [],
                )
                new_shots.append(target)
            previous_state = state_out
        for offset, new_shot in enumerate(new_shots, start=1):
            outline.shots.insert(index + offset, new_shot)
        for shot_index, item in enumerate(outline.shots, start=1):
            item.shot_no = shot_index
        events.append({
            "shot_no": index + 1,
            "speakers": distinct,
            "groups": [group_kids for _speaker, group_kids in groups],
            "shots_before": before_count,
            "shots_after": len(outline.shots),
            "reason": "dialogue_speaker_change_requires_reverse_shot",
        })
        index += len(groups)
    return events


_OUTLINE_SPEECH_CUE_RE = re.compile(
    r"(?P<speaker>[^，,。；;！？\n“”\"']{1,16}?)"
    r"(?:说|说明|问|喊|答|道|告诉|表示|回应|交代|要求)"
    r"[：:]?[“\"']?(?P<line>[^”\"']*)"
)


def _split_outline_text_outside_quotes(
    text: str,
    *,
    separators: str,
) -> list[str]:
    """Split prose without cutting punctuation inside quoted dialogue."""
    parts: list[str] = []
    current: list[str] = []
    closer: str | None = None
    quote_pairs = {"“": "”", "「": "」", "『": "』", '"': '"', "'": "'"}
    for character in str(text or ""):
        if closer is None and character in quote_pairs:
            closer = quote_pairs[character]
            current.append(character)
            continue
        if closer is not None and character == closer:
            closer = None
            current.append(character)
            continue
        if closer is None and character in separators:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(character)
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts


def normalize_outline_dialogue_ownership(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[dict[str, Any]]:
    """Make structured key-line ownership executable after deterministic splits.

    Historical prose splitting could leave an unowned dialogue fragment in an
    action shot or duplicate one key line across adjacent shots. Canonical
    screenplay lines remain the only spoken authority: the first structured
    owner delivers the line, while later redundant nodes become silent
    listener reactions. Mixed action/dialogue fragments retain only the real
    non-dialogue action.
    """
    catalog = key_line_catalog(screenplay)
    if not catalog or not outline.shots:
        return []
    speaker_by_key = {
        key_id: _speaker_name(text) or ""
        for key_id, text in catalog.items()
    }
    line_by_key = {
        key_id: _strip_speaker(text)
        for key_id, text in catalog.items()
    }
    changes: list[dict[str, Any]] = []
    owner_index: dict[str, int] = {}

    for index, shot in enumerate(outline.shots):
        kept: list[str] = []
        removed: list[str] = []
        for raw_key_id in shot.key_line_ids or []:
            key_id = str(raw_key_id).strip().upper()
            if key_id not in catalog:
                kept.append(key_id)
                continue
            if key_id in owner_index:
                removed.append(key_id)
                continue
            owner_index[key_id] = index
            kept.append(key_id)
        if removed:
            shot.key_line_ids = kept
            changes.append({
                "shot_no": shot.shot_no,
                "field": "key_line_ids",
                "removed": removed,
                "reason": "duplicate_delivery_owner",
            })

    def _event_ids(shot: Any) -> set[str]:
        values = {
            str(value or "").strip()
            for value in (shot.event_ids or [])
            if str(value or "").strip()
        }
        if str(shot.story_event_id or "").strip():
            values.add(str(shot.story_event_id).strip())
        return values

    def _same_event(left: Any, right: Any) -> bool:
        left_ids = _event_ids(left)
        right_ids = _event_ids(right)
        return not left_ids or not right_ids or bool(left_ids & right_ids)

    def _matching_key_id(clause: str, shot_index: int) -> str | None:
        match = _OUTLINE_SPEECH_CUE_RE.search(clause)
        prefixed_speaker = _speaker_name(clause) or ""
        if match is None and not prefixed_speaker:
            return None
        spoken_speaker = _condense(
            match.group("speaker") if match is not None else prefixed_speaker
        )
        fragment = _condense(
            match.group("line") if match is not None else _strip_speaker(clause)
        )
        for key_id, key_owner in owner_index.items():
            if key_owner == shot_index:
                continue
            owner_shot = outline.shots[key_owner]
            if not _same_event(outline.shots[shot_index], owner_shot):
                continue
            canonical_speaker = _condense(speaker_by_key.get(key_id, ""))
            if canonical_speaker and canonical_speaker not in spoken_speaker:
                continue
            canonical_line = _condense(line_by_key.get(key_id, ""))
            if fragment and len(fragment) >= 2 and (
                canonical_line.startswith(fragment)
                or fragment in canonical_line
                or _bigram_coverage(fragment, canonical_line) >= 0.6
            ):
                return key_id
            if (
                not fragment
                and canonical_speaker
                and canonical_speaker in spoken_speaker
            ):
                return key_id
        return None

    def _reaction_actor(index: int, excluded_speakers: set[str]) -> str:
        current = outline.shots[index]
        current_visible = {
            str(name or "").strip()
            for name in (current.characters_visible or [])
            if str(name or "").strip()
        }
        relation_is_authoritative = bool(current.visible_entity_ids)
        for positions in (
            range(index + 1, len(outline.shots)),
            range(index - 1, -1, -1),
        ):
            for position in positions:
                candidate = outline.shots[position]
                if not _same_event(current, candidate):
                    continue
                for key_id in candidate.key_line_ids or []:
                    speaker = speaker_by_key.get(str(key_id).upper(), "")
                    if (
                        speaker
                        and speaker not in excluded_speakers
                        and (
                            not relation_is_authoritative
                            or speaker in current_visible
                        )
                    ):
                        return speaker
        non_speaker = next(
            (
                name
                for name in current.characters_visible or []
                if name not in excluded_speakers
            ),
            "",
        )
        if non_speaker:
            return non_speaker
        # A redundant dialogue node with no source-backed listener remains on
        # the actual speaker instead of inventing a scene-cast reaction actor.
        return next(iter(current.characters_visible or []), "")

    # Structured owners always deliver the canonical screenplay line. This
    # removes stale split prose from their beat/action fields.
    for shot in outline.shots:
        valid_keys = [
            str(key_id).strip().upper()
            for key_id in shot.key_line_ids or []
            if str(key_id).strip().upper() in catalog
        ]
        if not valid_keys:
            continue
        speakers = list(dict.fromkeys(
            speaker_by_key[key_id]
            for key_id in valid_keys
            if speaker_by_key[key_id]
        ))
        canonical = "；".join(
            catalog[key_id]
            for key_id in valid_keys
        )
        shot.covers = canonical
        if len(speakers) == 1:
            speaker = speakers[0]
            shot.beat = f"{speaker}说出本话轮"
            if not _outline_dialogue_two_shot_required(shot, speaker):
                shot.primary_action = f"{speaker}单人近景说出本话轮"
                shot.characters_visible = [speaker]
            shot.audio_cast = [speaker]

    # Remove speech copied into a different, unowned action node. Preserve any
    # genuine action clauses; a speech-only duplicate becomes a silent reaction.
    for index, shot in enumerate(outline.shots):
        if shot.key_line_ids:
            continue
        source = max(
            (
                str(shot.primary_action or ""),
                str(shot.covers or ""),
                str(shot.beat or ""),
            ),
            key=len,
        )
        clauses = _split_outline_text_outside_quotes(
            source,
            separators="；;。！？\n",
        )
        removed_keys: list[str] = []
        kept_clauses: list[str] = []
        for clause in clauses:
            key_id = _matching_key_id(clause, index)
            if key_id is None:
                kept_clauses.append(clause)
            else:
                removed_keys.append(key_id)
        if not removed_keys:
            continue
        speakers = {
            speaker_by_key.get(key_id, "")
            for key_id in removed_keys
            if speaker_by_key.get(key_id, "")
        }
        remaining = "；".join(kept_clauses).strip("； ")
        if remaining:
            shot.beat = remaining
            shot.covers = remaining
            shot.primary_action = remaining
            shot.state_out = f"{remaining.rstrip('。')}完成，准备承接后续动作"
            shot.audio_cast = []
            reason = "unowned_dialogue_fragment_removed"
        else:
            actor = _reaction_actor(index, speakers)
            reaction = (
                f"{actor}说完本话轮后闭口呈现状态变化"
                if actor in speakers
                else f"{actor}听完上一话轮后闭口作出可见反应"
                if actor
                else "当前画面以原有可见状态承接下一动作"
            )
            shot.beat = reaction
            shot.covers = reaction
            shot.primary_action = reaction
            shot.state_out = reaction
            shot.audio_cast = []
            if actor:
                shot.characters_visible = [actor]
            shot.continuity_mode = "reaction_cut"
            reason = "redundant_dialogue_converted_to_reaction"
        changes.append({
            "shot_no": shot.shot_no,
            "field": "dialogue_ownership",
            "removed_key_line_ids": sorted(set(removed_keys)),
            "reason": reason,
        })
    return changes
