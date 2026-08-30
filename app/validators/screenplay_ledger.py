"""剧本台账归一化（normalize_screenplay_ledgers/normalize_screenplay_candidate）、
剧情脊柱（plot spine）与原文覆盖率校验。
"""
from __future__ import annotations

import re
from collections import defaultdict

from app.renderability import SPINE_BEATS_MIN
from app.schemas import (
    DELIVERY_OWNERS,
    EpisodeScreenplay,
    InformationItem,
    StoryEvent,
)
from app.source_excerpt import index_source_segments

from .dialogue_chains import normalize_screenplay_dialogue_chains
from .screenplay_text import _bigram_coverage

def normalize_screenplay_ledgers(script: EpisodeScreenplay) -> EpisodeScreenplay:
    """Renderability：清洗空壳 events/ledger，必要时从 plot_spine 确定性回填。

    模型常输出「有壳无肉」的 information_ledger（content/event_id 为空），旧 QA 会硬拦并卡在
    WARNING 候选。主线权威是 plot_spine；台账只是下游拆镜索引，允许从 spine 合成最小完备集。
    """
    spine = script.plot_spine
    plan = script.narrative_plan
    if plan is not None:
        action_by_id = {
            action.action_id: action
            for action in plan.atomic_actions
        }
        evidence_by_event: defaultdict[str, list[str]] = defaultdict(list)
        for evidence in plan.evidence:
            if evidence.anchor.type == "event":
                evidence_by_event[evidence.anchor.id].append(
                    (evidence.observable_claim or "").strip()
                )
        authority_by_event: dict[str, str] = {}
        for narrative_event in plan.events:
            candidates = [
                *evidence_by_event.get(narrative_event.event_id, []),
                *[
                    (action_by_id[action_id].semantic_intent or "").strip()
                    for action_id in narrative_event.action_ids
                    if action_id in action_by_id
                ],
                *[
                    (
                        action_by_id[action_id].completion_condition
                        or ""
                    ).strip()
                    for action_id in narrative_event.action_ids
                    if action_id in action_by_id
                ],
            ]
            authority = next(
                (candidate for candidate in candidates if len(candidate) >= 4),
                "",
            )
            if authority:
                authority_by_event[narrative_event.event_id] = authority
        for event in script.events or []:
            if len((event.visible_change or "").strip()) >= 4:
                continue
            authority = authority_by_event.get(
                (event.event_id or "").strip(),
                "",
            )
            if authority:
                event.visible_change = authority

    # 1) 清洗 events：丢掉缺 id / 缺状态链的空壳
    cleaned_events: list[StoryEvent] = []
    seen_eids: set[str] = set()
    for event in script.events or []:
        eid = (event.event_id or "").strip()
        if not eid or eid in seen_eids:
            continue
        if len((event.visible_change or "").strip()) < 2 and len((event.state_out or "").strip()) < 2:
            continue
        seen_eids.add(eid)
        cleaned_events.append(event)

    # 2) 若 events 过稀且有 spine → 按 must_keep 节拍合成
    must_beats = [b for b in (spine.spine_beats if spine else []) if b.must_keep] or list(
        (spine.spine_beats if spine else []) or []
    )
    if (
        plan is None
        and spine
        and must_beats
        and len(cleaned_events) < min(3, len(must_beats))
    ):
        cleaned_events = []
        for i, beat in enumerate(must_beats, start=1):
            cleaned_events.append(StoryEvent(
                event_id=f"E{i}",
                source_span=(beat.beat_id or f"S{i:02d}"),
                source_fact=f"{(beat.who or '').strip()}{(beat.does or '').strip()}".strip() or f"主线节拍{i}",
                state_in="节拍开始",
                trigger=(beat.does or "").strip() or "主线推进",
                visible_change=f"{(beat.who or '').strip()}{(beat.does or '').strip()}".strip() or f"主线动作{i}",
                state_out=(beat.turn or "").strip() or "局势变化",
                must_keep=bool(beat.must_keep),
            ))
    script.events = cleaned_events
    event_ids = {(e.event_id or "").strip() for e in cleaned_events if (e.event_id or "").strip()}
    known_voice_ids = {
        str(item.speaker_id or "").strip()
        for item in (script.voice_bible or [])
        if str(item.speaker_id or "").strip()
    }
    known_voice_ids.update(
        str(turn.speaker or "").strip()
        for chain in (script.dialogue_chains or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip()
    )

    def normalized_speaker_id(item: InformationItem) -> str | None:
        raw = str(item.speaker_id or "").strip()
        if not raw or raw in known_voice_ids:
            return raw or None
        candidates = [
            speaker
            for speaker in known_voice_ids
            if speaker and speaker in raw
        ]
        if len(candidates) == 1:
            return candidates[0]
        evidence = "；".join(
            value
            for value in (
                str(item.content or "").strip(),
                str(item.exact_text or "").strip(),
            )
            if value
        )
        positions = sorted(
            (
                evidence.find(speaker),
                speaker,
            )
            for speaker in candidates
            if evidence.find(speaker) >= 0
        )
        if positions and (
            len(positions) == 1
            or positions[0][0] < positions[1][0]
        ):
            return positions[0][1]
        return raw

    # 3) 清洗 ledger：丢掉无中文 content 的空壳；event_id 空/非法时按序号挂到事件
    cleaned_ledger: list[InformationItem] = []
    seen_iids: set[str] = set()
    event_list = list(cleaned_events)
    for idx, item in enumerate(script.information_ledger or []):
        content = (item.content or "").strip()
        if len(content) < 4 or not re.search(r"[\u3400-\u9fff]", content):
            continue
        iid = (item.info_id or "").strip() or f"I{idx + 1}"
        if not re.fullmatch(r"I\d{1,4}", iid, flags=re.IGNORECASE):
            iid = f"I{idx + 1}"
        if iid in seen_iids:
            continue
        eid = (item.event_id or "").strip()
        if eid not in event_ids:
            eid = event_list[min(idx, len(event_list) - 1)].event_id if event_list else ""
        if not eid:
            continue
        seen_iids.add(iid)
        cleaned_ledger.append(InformationItem(
            info_id=iid,
            event_id=eid,
            content=content,
            delivery_owner=item.delivery_owner if item.delivery_owner in DELIVERY_OWNERS else "visual_action",
            speaker_id=normalized_speaker_id(item),
            exact_text=item.exact_text,
            reinforcement_allowed=bool(item.reinforcement_allowed),
            status=(item.status or "unassigned"),
            assigned_shot_no=item.assigned_shot_no,
        ))

    # 4) ledger 仍空且有 events → 每事件一条主线信息
    if not cleaned_ledger and cleaned_events:
        for i, event in enumerate(cleaned_events, start=1):
            content = (
                (event.visible_change or "").strip()
                or (event.source_fact or "").strip()
                or (event.state_out or "").strip()
                or f"主线信息{i}"
            )
            if not re.search(r"[\u3400-\u9fff]", content):
                content = f"主线节拍{i}的局势变化"
            cleaned_ledger.append(InformationItem(
                info_id=f"I{i}",
                event_id=event.event_id,
                content=content[:80],
                delivery_owner="visual_action",
                status="unassigned",
            ))

    # 5) 加帽：≤ spine×2
    spine_n = len(must_beats) if must_beats else 0
    if spine_n:
        cap = max(SPINE_BEATS_MIN, spine_n * 2)
        cleaned_ledger = cleaned_ledger[:cap]

    script.information_ledger = cleaned_ledger
    if not (script.episode_premise or "").strip() and spine and (spine.episode_premise or "").strip():
        script.episode_premise = spine.episode_premise.strip()
    return script


def normalize_screenplay_candidate(
    script: EpisodeScreenplay,
    *,
    source_text: str = "",
) -> EpisodeScreenplay:
    """在 QA 之前生成规范化副本；QA 本身不得修改候选内容。"""
    normalized = script.model_copy(deep=True)
    normalize_screenplay_ledgers(normalized)
    normalize_screenplay_dialogue_chains(normalized, source_text)
    return normalized


def validate_plot_spine(
    script: EpisodeScreenplay,
    *,
    narrative_authority: bool = False,
) -> list[str]:
    """先校验主线骨架，再允许正文通过（Renderability First）。"""
    from app.screenplay_ir import screenplay_beat_fields_repeat

    errors: list[str] = []
    spine = script.plot_spine
    if spine is None:
        errors.append(
            "plot_spine 缺失；请先输出主线骨架（episode_premise / spine_beats / must_keep_ending / drop_list），"
            "再写正文——只保改变局势的主线，禁止抠细节"
        )
        return errors
    if len((spine.episode_premise or "").strip()) < 8:
        errors.append("plot_spine.episode_premise 过短；请用一句话写本集主角要什么、碰到什么阻力")
    beats = spine.spine_beats or []
    if len(beats) < SPINE_BEATS_MIN:
        errors.append(
            f"plot_spine.spine_beats 共 {len(beats)} 条；至少需要 {SPINE_BEATS_MIN} 条，"
            "并完整覆盖有效剧情单元，数量不设上限"
        )
    beat_ids: set[str] = set()
    must_keep_count = 0
    for i, beat in enumerate(beats):
        tag = f"plot_spine.spine_beats[{i}]"
        bid = (beat.beat_id or "").strip()
        if not bid:
            errors.append(f"{tag}.beat_id 不能为空")
        elif bid in beat_ids:
            errors.append(f"{tag}.beat_id=「{bid}」重复")
        else:
            beat_ids.add(bid)
        if len((beat.who or "").strip()) < 1:
            errors.append(f"{tag}.who 不能为空")
        if len((beat.does or "").strip()) < 4:
            errors.append(f"{tag}.does 过短；请写可见/可听的主动作")
        if len((beat.turn or "").strip()) < 4:
            errors.append(f"{tag}.turn 过短；请写局势变化")
        elif screenplay_beat_fields_repeat(beat.does, beat.turn):
            errors.append(
                "[SPINE_ACTION_TURN_DUPLICATE] "
                f"{tag}.does 与 turn 语义重复；does 应写可见/可听动作，"
                "turn 必须写该动作完成后新成立的人物、信息、关系或局势状态"
            )
        if beat.must_keep:
            must_keep_count += 1
    if beats and must_keep_count < 1:
        errors.append(
            "plot_spine 至少需要一条 must_keep=true 的实际剧情交付节拍"
        )
    if len((spine.must_keep_ending or "").strip()) < 8:
        errors.append(
            "plot_spine.must_keep_ending 过短；请锁定本章收束（与原文本章结局同向，禁止发明下一章钩子）"
        )
    return errors


def validate_screenplay_source_coverage(
    script: EpisodeScreenplay,
    source_text: str | None,
) -> list[str]:
    """Every indexed source segment is delivered or has an explicit disposition."""
    segments = index_source_segments(source_text or "")
    if not segments:
        return []
    if not script.source_coverage:
        return [
            "source_coverage 为空；每个 SRC* 原文段必须明确标记为 "
            "deliver/merge/context/duplicate/audit_only，禁止静默删戏"
        ]
    expected = {segment.segment_id for segment in segments}
    segments_by_id = {segment.segment_id: segment for segment in segments}
    beat_ids = {
        str(beat.beat_id or "").strip()
        for beat in ((script.plot_spine.spine_beats if script.plot_spine else []) or [])
        if str(beat.beat_id or "").strip()
    }
    beats_by_id = {
        str(beat.beat_id or "").strip(): beat
        for beat in ((script.plot_spine.spine_beats if script.plot_spine else []) or [])
        if str(beat.beat_id or "").strip()
    }
    seen: set[str] = set()
    errors: list[str] = []
    for index, decision in enumerate(script.source_coverage):
        segment_id = str(
            (decision.get("source_segment_id") if isinstance(decision, dict) else decision.source_segment_id)
            or ""
        ).strip()
        if segment_id not in expected:
            errors.append(
                f"source_coverage[{index}].source_segment_id={segment_id} 不属于当前原文"
            )
        if segment_id in seen:
            errors.append(f"source_coverage 中 {segment_id} 重复")
        seen.add(segment_id)
        raw_beat_ids = (
            decision.get("beat_ids", []) if isinstance(decision, dict) else decision.beat_ids
        )
        raw_beat_ids = list(raw_beat_ids or [])
        unknown_beats = sorted(set(raw_beat_ids or []) - beat_ids)
        if unknown_beats:
            errors.append(
                f"source_coverage[{index}] 引用了不存在的 beat_ids：{unknown_beats}"
            )
        disposition = (
            decision.get("disposition") if isinstance(decision, dict) else decision.disposition
        )
        duplicate_of = (
            decision.get("duplicate_of") if isinstance(decision, dict) else decision.duplicate_of
        )
        reason = (
            decision.get("reason", "") if isinstance(decision, dict) else decision.reason
        )
        projection_policy = (
            decision.get("projection_policy")
            if isinstance(decision, dict)
            else decision.projection_policy
        )
        if disposition in {"deliver", "merge"}:
            if not raw_beat_ids:
                errors.append(
                    f"[SOURCE_COVERAGE_LINK_MISSING] source_coverage[{index}] "
                    f"{segment_id} 标记为 {disposition}，但没有绑定 beat_ids"
                )
            for beat_id in raw_beat_ids:
                beat = beats_by_id.get(str(beat_id))
                if beat is not None and segment_id not in set(beat.source_segment_ids or []):
                    errors.append(
                        f"[SOURCE_COVERAGE_LINK_MISMATCH] source_coverage[{index}] "
                        f"{segment_id} 引用 {beat_id}，但该 beat 未反向引用此原文段"
                    )
        if disposition == "context" and len(str(reason or "").strip()) < 8:
            errors.append(
                f"[SOURCE_CONTEXT_UNLOCATED] source_coverage[{index}] {segment_id} "
                "标记为 context 时必须说明它在场景、关系、因果或环境中的具体保留位置"
            )
        if disposition == "audit_only":
            if raw_beat_ids:
                errors.append(
                    f"[SOURCE_AUDIT_ONLY_BEAT_FORBIDDEN] source_coverage[{index}] "
                    f"{segment_id} 不得绑定 beat_ids"
                )
            if projection_policy != "audit_only":
                errors.append(
                    f"[SOURCE_AUDIT_ONLY_PROJECTION_INVALID] "
                    f"source_coverage[{index}] {segment_id} 必须明确排除画面投影"
                )
            if len(str(reason or "").strip()) < 8:
                errors.append(
                    f"[SOURCE_AUDIT_ONLY_REASON_MISSING] source_coverage[{index}] "
                    f"{segment_id} 必须说明完整来源审计的保留方式"
                )
        if (
            disposition == "duplicate"
            and duplicate_of not in expected
        ):
            errors.append(
                f"source_coverage[{index}].duplicate_of={duplicate_of} 不属于当前原文"
            )
        elif disposition == "duplicate":
            source_segment = segments_by_id.get(segment_id)
            target_segment = segments_by_id.get(str(duplicate_of))
            if (
                source_segment is not None
                and target_segment is not None
                and segment_id == duplicate_of
            ):
                errors.append(
                    f"[SOURCE_DUPLICATE_INVALID] source_coverage[{index}] "
                    f"{segment_id} 不能声明自己重复自己"
                )
            elif source_segment is not None and target_segment is not None:
                similarity = min(
                    _bigram_coverage(source_segment.text, target_segment.text),
                    _bigram_coverage(target_segment.text, source_segment.text),
                )
                if similarity < 0.55:
                    errors.append(
                        f"[SOURCE_DUPLICATE_UNPROVEN] source_coverage[{index}] "
                        f"{segment_id} 与 {duplicate_of} 缺少可核验的重复关系"
                    )
    missing = sorted(expected - seen)
    if missing:
        shown = "、".join(missing[:20])
        extra = f"（另有 {len(missing) - 20} 段）" if len(missing) > 20 else ""
        errors.append(
            f"source_coverage 漏掉 {len(missing)} 个原文段：{shown}{extra}；"
            "必须交付、合并、作为上下文保留、仅审计保留，"
            "或给出可核验的重复指向"
        )
    return errors
