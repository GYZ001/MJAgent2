"""分镜台阶段二：写一段（含边写边审）/写一条链——换场并行链的生成核心
（用户拍板，2026-10-01）。

背景：``storyboard_pack._generate_all_segment_prompts`` 此前逐段严格串行——第 N
段带着第 N-1 段定稿全文做「一镜参考」衔接，第 1 集 30 段一轮约 75-118 分钟，
几乎全是串行等待（分镜模型桥接最多 2 路并发）。用户拍板：在**换场**处把全集
切成多条链并行写，链内仍严格串行 + 边写边审——本模块是每条链内部实际执行
生成的部分；换场切分/并行调度/接缝复核/合并后 advisory 二次遍历在姊妹模块
``app.production.storyboard_segment_chain_plan``（避免单文件超过 500 行
line_count 默认上限，见该模块 docstring）。

``generate_one_segment``/``write_one_chain`` 的代码与此前 ``storyboard_pack.
_generate_all_segment_prompts`` 单循环体逐字同构（只是把本地变量换成
``SegmentChainContext``/``ChainState`` 的字段），不是重写——这保证「只有一条
链时产物与串行逐字一致」这条验收不依赖另一套新逻辑碰巧算出同样结果。

不得反向 import ``storyboard_pack``：需要的类型/函数通过 ``SegmentChainContext``
传入，由调用方（``storyboard_pack._generate_all_segment_prompts``）在
monkeypatch 已生效之后现取——``_ensure_segment_prompt_budget``/
``CAMERA_DIGEST_WINDOW`` 两项测试里被 ``monkeypatch.setattr(storyboard_pack_
module, ...)`` 直接打桩，必须按值在调用时现取，不能在本模块顶部静态 import
（那会在 monkeypatch 生效前就把原始对象冻结进本模块命名空间，桩从此失效，
见 CLAUDE.md「拆包会静默废掉 monkeypatch」）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.harness import model_gateway
from app.production import storyboard_action_beats as _action_beats
from app.production import storyboard_action_density as _action_density
from app.production import storyboard_beat_causality as _beat_causality
from app.production import storyboard_beat_foreshadowing as _beat_foreshadowing
from app.production import storyboard_cast_lock as _cast_lock
from app.production import storyboard_music_bed as _music_bed
from app.production import storyboard_prop_appearance_lock as _prop_lock
from app.production import storyboard_prop_count as _prop_count
from app.production import storyboard_prop_entrance as _prop_entrance
from app.production import storyboard_prop_visibility as _prop_visibility
from app.production import storyboard_prose_review as _prose_review
from app.production import storyboard_revision_notes as _revision_notes
from app.production import storyboard_shot_mandates as _shot_mandates
from app.production import storyboard_skin_blush as _skin_blush
from app.production import storyboard_stimulus_voice as _stim_voice
from app.production import storyboard_transition_plan as _transition_plan
from app.production import storyboard_wardrobe_plan as _wardrobe_plan
from app.production.screenplay_markers import joined_source_text, required_beats_errors, segment_structure
from app.production.storyboard_beat_sheet import _paratext_exclusion_rule
from app.production.storyboard_beat_sheet_schemas import SEGMENT_DURATION_S
from app.production.storyboard_continuity_memo import continuity_memo_payload, ensure_prop_form_matches_lock, ensure_wardrobe_continuity_in_prompt
from app.production.storyboard_dialogue_attribution import manifest_name_to_identity
from app.production.storyboard_dialogue_ledger import required_dialogue_rule
from app.production.storyboard_dialogue_repeat import already_delivered_dialogue_rule, already_delivered_payload, reserved_dialogue_payload, reserved_lines_for
from app.production.storyboard_identity_generation import finalize_generated_identity, generated_identity_errors
from app.production.storyboard_narrative_arc import _segment_continuity_rules, beats_payload_for_segment, phase2_segment_rules, segment_narrative_arc_payload_fields
from app.production.storyboard_reference_repair import strip_extra_reference_markers
from app.production.storyboard_overlay_text import overlay_text_errors
from app.production.storyboard_repair_context import known_character_identities, storyboard_repair_context
from app.production.storyboard_segment_output import segment_output_contract
from app.production.storyboard_segment_ranges import segment_source_payload
from app.production.storyboard_staging_repeat import StagingSoftGate, canonical_phrases, chain_prompt_texts, repeated_staging_errors, staging_continuation_rule
from app.production.storyboard_travel_direction import ensure_travel_direction_in_prompt


@dataclass
class SegmentChainContext:
    """一次生成调用内不随段/链变化的只读数据，由调用方
    （``storyboard_pack._generate_all_segment_prompts``）在每次调用时新建——
    ``ensure_budget``/``camera_digest_window`` 两个字段必须是调用那一刻现取的
    值/引用，见模块 docstring。"""

    episode_id: str; episode_no: int; beat_draft: Any; segments: list[Any]; payload: dict[str, Any]; bible: Any
    required_dialogue_by_segment_no: dict[int, list[dict[str, Any]]]; conn: Any; project_id: str; aspect_ratio: str
    enhance_music_bed: bool; narrator_voice_character: str; target_video_model: str; contract_version: str
    profile: Any; target_model_literal: str; dialect_instructions: str
    beats_by_id: dict[str, Any]; paratext_indexes: set[int]
    visual_style: str; visual_style_is_photographic: bool; shared_rules: list[str]
    props_plan: list[Any]; appearance_locks: Any; relevant_assets_by_segment_no: dict[int, dict[str, Any]]
    enable_prose_review: bool; draft_cls: Any; validate_segment_draft: Any
    ensure_budget: Any; camera_digest_window: int; segment_content_advisories: Any
    min_shots: int; max_shots: int; answer_tokens: int


@dataclass
class ChainState:
    """一条链在生成过程中逐段累加的可变状态——每条并行链持有自己独立的一份
    （见 ``storyboard_segment_chain_plan`` 模块 docstring「跨段累加状态的确定性
    重放」）。"""

    wardrobe_state: Any
    by_segment_no: dict[int, Any] = field(default_factory=dict)
    camera_digest_by_segment_no: dict[int, Any] = field(default_factory=dict)
    delivered_lines: list[tuple[int, str, str]] = field(default_factory=list)
    covered_turn_ids: set[str] = field(default_factory=set)
    covered_signal_ids: set[str] = field(default_factory=set)
    covered_prop_ids: set[str] = field(default_factory=set)
    covered_voice_ids: set[str] = field(default_factory=set)
    review_outcomes: list[dict[str, Any]] = field(default_factory=list)
    #: 非首链链首段的「上一段计划场景」近似（见 ``_segment_prep`` 用法）——
    #: 链首段 previous_draft=None，但换场切分本身就是用这份 relevant_assets.
    #: scenes 数据判的（见 storyboard_segment_chain_plan._is_scene_change_
    #: boundary），这里原样复用同一份数据，不是另起一套近似；只在
    #: storyboard_segment_chain_plan._chain_start_state 为非首链设置，首链/
    #: reuse_segments 全量路径保持 None（即「无近似，等同真正的第 1 段」）。
    approx_previous_scene_ids: set[str] | None = None


@dataclass
class _Moments:
    turns_here: list[Any]; signals_here: list[Any]; voice_here: list[Any]
    wardrobe_advance: tuple[Any, Any]; prop_entrances_here: list[Any]


@dataclass
class _Prep:
    previous_segment_no: int | None; previous_memo: Any; camera_history: list[dict[str, Any]]
    continuity_rules: list[str]; staging_chain: list[Any]
    staging_gate: Any; voice_gate: Any; action_gate: Any
    source_payload: dict[str, Any]; segment_paratext_hit: set[int]
    required_dialogue: list[dict[str, Any]]; palette_previous: str
    structure: dict[str, Any]; text_transition: str; previous_scene_ids: set[str]; relevant_assets: dict[str, Any]
    #: True 当且仅当 previous_draft 为空、但本段是换场并行链的链首段（上一段
    #: 真实存在，只是本地生成看不到）——与「真正的本集第一段」区分，供
    #: phase2_segment_rules 选文案（审查发现 1，2026-10-01，见 storyboard_
    #: narrative_arc._continuity_memo_rules_mid_episode_scene_change）。
    mid_episode_scene_change: bool


def _segment_moments(ctx: SegmentChainContext, state: ChainState, plan: Any) -> _Moments:
    """claim-once 提名只在本段第一次尝试时认领一次，重写时原样复用——必须在
    attempt 循环之外调用一次。"""
    return _Moments(
        turns_here=_beat_causality.moments_for_segment(plan.beat_ids, ctx.beat_draft.emotional_turns, state.covered_turn_ids),
        signals_here=_beat_foreshadowing.moments_for_segment(plan.beat_ids, ctx.beat_draft.foreshadowing_beats, state.covered_signal_ids),
        voice_here=_stim_voice.voice_claim_moments(plan.beat_ids, ctx.beat_draft.emotional_turns, state.covered_voice_ids),
        wardrobe_advance=state.wardrobe_state.advance(plan.beat_ids),
        prop_entrances_here=_prop_entrance.moments_for_segment(plan.beat_ids, ctx.props_plan, state.covered_prop_ids),
    )


def _camera_digest_window_payload(camera_digest_by_segment_no: dict[int, Any], *, segment_no: int, window: int) -> list[dict[str, Any]]:
    """最近 ``window`` 段（不含本段）的开场镜头语言，按 segment_no 升序；链首段
    （``camera_digest_by_segment_no`` 为空）天然返回空列表，即「camera_digest
    窗口为空」的近似，不需要额外分支——见模块 docstring。"""
    start = max(1, segment_no - window)
    return [
        {
            "segment_no": no,
            "opening_shot_size": camera_digest_by_segment_no[no].opening_shot_size,
            "opening_camera_move": camera_digest_by_segment_no[no].opening_camera_move,
        }
        for no in range(start, segment_no) if no in camera_digest_by_segment_no
    ]


def _segment_prep(ctx: SegmentChainContext, state: ChainState, plan: Any, previous_draft: Any | None) -> _Prep:
    """每次生成尝试都重新计算一遍（与原串行实现同一行为，即使多数字段不随
    attempt 变化）。"""
    previous_segment_no = plan.segment_no - 1 if previous_draft is not None else None
    previous_memo = previous_draft.continuity_memo if previous_draft is not None else None
    # 换场并行链的链首段：previous_draft 为空但上一段在本集里真实存在，只是
    # 本地生成看不到（state.approx_previous_scene_ids 由 _chain_start_state
    # 设置）——与真正的本集第一段（两者都是 None）区分，否则 rules 文案会
    # 误导模型把它当成整部作品的开场（审查发现 1，2026-10-01）。
    mid_episode_scene_change = previous_draft is None and state.approx_previous_scene_ids is not None
    camera_history = _camera_digest_window_payload(state.camera_digest_by_segment_no, segment_no=plan.segment_no, window=ctx.camera_digest_window)
    continuity_rules = _segment_continuity_rules(previous_segment_no=previous_segment_no, camera_history=camera_history, mid_episode_scene_change=mid_episode_scene_change)
    staging_chain = chain_prompt_texts(ctx.beat_draft.segments, state.by_segment_no, plan.segment_no)
    staging_gate = StagingSoftGate(hard_attempts=2, segment_no=plan.segment_no)
    voice_gate = _stim_voice.StimulusVoiceSoftCheck(hard_attempts=2, segment_no=plan.segment_no)
    action_gate = _action_density.ActionDensitySoftCheck(hard_attempts=2, segment_no=plan.segment_no)
    source_payload = segment_source_payload(plan, ctx.segments, ctx.paratext_indexes)
    segment_paratext_hit = set(plan.source_segment_indexes) & ctx.paratext_indexes
    required_dialogue = ctx.required_dialogue_by_segment_no.get(plan.segment_no, [])
    palette_previous = ctx.beat_draft.segments[plan.segment_no - 2].palette if plan.segment_no > 1 else ""
    structure = segment_structure(
        joined_source_text(ctx.segments, ctx.beat_draft.segments[plan.segment_no - 2].source_segment_indexes) if plan.segment_no > 1 else "",
        joined_source_text(ctx.segments, plan.source_segment_indexes),
    )
    # previous_draft 为空分两种情形，不能都按「没有上一段信息」处理：真正的第 1 段
    # （state.approx_previous_scene_ids 为 None）用空集合；非首链链首段换场切分已经
    # 判过「与上一段计划场景不同」（见 ChainState.approx_previous_scene_ids 字段
    # 注释），这里原样复用同一份判断依据，否则 resolve_transition_before_generation
    # 会因为拿到空集合而判不出换场，与切分理由自相矛盾。
    if previous_draft is not None:
        previous_scene_ids = {s.scene_id for s in previous_draft.resources.scenes}
    else:
        previous_scene_ids = state.approx_previous_scene_ids or set()
    relevant_assets = ctx.relevant_assets_by_segment_no[plan.segment_no]
    structure, text_transition = _transition_plan.resolve_transition_before_generation(
        structure, previous_scene_ids, {str(s.get("scene_id") or "") for s in relevant_assets["scenes"]},
    )
    return _Prep(
        previous_segment_no, previous_memo, camera_history, continuity_rules, staging_chain, staging_gate, voice_gate,
        action_gate, source_payload, segment_paratext_hit, required_dialogue, palette_previous, structure, text_transition,
        previous_scene_ids, relevant_assets, mid_episode_scene_change,
    )


def _task_payload_rules(ctx: SegmentChainContext, state: ChainState, plan: Any, moments: _Moments, prep: _Prep, revision_text: str) -> list[str]:
    paratext_rule = _paratext_exclusion_rule(prep.segment_paratext_hit) if prep.segment_paratext_hit else None
    staging_tail = prep.staging_chain[-1][1] if prep.staging_chain else ""
    return [
        *phase2_segment_rules(
            continuity_rules=prep.continuity_rules, shared_rules=ctx.shared_rules,
            required_dialogue_rule_text=required_dialogue_rule(prep.required_dialogue), paratext_exclusion_rule=paratext_rule,
            palette_current=plan.palette, palette_previous=prep.palette_previous, previous_memo=prep.previous_memo,
            staging_rule=staging_continuation_rule(staging_tail, previous_segment_no=plan.segment_no - 1, synopsis=plan.synopsis),
            structure=prep.structure, mid_episode_scene_change=prep.mid_episode_scene_change,
        ),
        already_delivered_dialogue_rule(state.delivered_lines, reserved_lines_for(ctx.required_dialogue_by_segment_no, plan.segment_no)),
        *_beat_causality.segment_rule_text(moments.turns_here, plan.beat_ids),
        *_beat_foreshadowing.segment_rule_text(moments.signals_here, plan.beat_ids),
        *_wardrobe_plan.segment_rule_text(*moments.wardrobe_advance, ctx.payload, prep.relevant_assets["characters"]),
        *_prop_entrance.segment_rule_text(moments.prop_entrances_here),
        *_prop_lock.segment_rule_text(_prop_lock.moments_for_segment(plan.beat_ids, ctx.appearance_locks)),
        *_revision_notes.segment_rule_text(revision_text), *_stim_voice.segment_rule_text(moments.voice_here),
    ]


def _task_payload_dialect_instructions(ctx: SegmentChainContext) -> str:
    return (
        f"{ctx.dialect_instructions}\n{_action_beats.decisive_action_dialect_rule(ctx.profile.render_format)}\n"
        f"{_action_density.shot_action_beats_rule(max_shots=ctx.max_shots)}\n{_shot_mandates.shot_mandates_dialect_rule(ctx.profile.render_format)}"
        f"\n{_prop_visibility.prop_visibility_dialect_rule(ctx.profile.render_format)}"
        f"\n{_prop_count.prop_count_dialect_rule(ctx.profile.render_format)}"
        f"{_music_bed.music_bed_dialect_addendum(ctx.profile.render_format, enabled=ctx.enhance_music_bed)}"
        f"{_skin_blush.skin_blush_dialect_addendum(ctx.profile.render_format, photographic=ctx.visual_style_is_photographic)}"
    )


def _build_task_payload(ctx: SegmentChainContext, state: ChainState, plan: Any, moments: _Moments, prep: _Prep, previous_draft: Any | None, revision_text: str) -> dict[str, Any]:
    return {
        "task": (
            "为下面这一段原文和节拍写一整段可直接投喂视频生成模型的提示词（prompt_text）。"
            "prompt_text 必须是完整、可直接复制使用的一整块文本，不要拆成多个片段或只写关键词"
            "——镜头正文保持你的安排，speech 占位符由已校验的台词合同展开，参考图由主体身份精确绑定。"
        ),
        "rules": _task_payload_rules(ctx, state, plan, moments, prep, revision_text),
        **segment_narrative_arc_payload_fields(
            segment_no=plan.segment_no, total_segments=len(ctx.beat_draft.segments),
            palette_current=plan.palette, palette_previous=prep.palette_previous,
        ),
        "segment_no": plan.segment_no, "synopsis": plan.synopsis, **prep.structure,
        "beats": beats_payload_for_segment(plan.beat_ids, ctx.beats_by_id),
        "emotional_turns": [t.model_dump(mode="json") for t in moments.turns_here],
        "foreshadowing_beats": [s.model_dump(mode="json") for s in moments.signals_here],
        "duration_s": SEGMENT_DURATION_S, "shot_count_range": [ctx.min_shots, ctx.max_shots],
        "source_segment_indexes": plan.source_segment_indexes,
        **prep.source_payload,
        "relevant_assets": prep.relevant_assets,
        "known_character_identities": known_character_identities(ctx.payload),
        "required_dialogue": prep.required_dialogue,
        "already_delivered_dialogue": already_delivered_payload(state.delivered_lines),
        "reserved_dialogue": reserved_dialogue_payload(reserved_lines_for(ctx.required_dialogue_by_segment_no, plan.segment_no)),
        "previous_segment_prompt": previous_draft.prompt_text if previous_draft is not None else None,
        "previous_continuity_memo": continuity_memo_payload(prep.previous_memo),
        "recent_camera_language": prep.camera_history,
        "visual_style": ctx.visual_style, "aspect_ratio": ctx.aspect_ratio, "target_video_model": ctx.target_model_literal,
        "dialect_instructions": _task_payload_dialect_instructions(ctx),
        "profile_generation_rules": list(ctx.profile.generation_rules),
        "output_contract": segment_output_contract(plan.source_segment_indexes, min_shots=ctx.min_shots, max_shots=ctx.max_shots),
        "output_schema": ctx.draft_cls.model_json_schema(),
    }


def _segment_validate(value: Any, ctx: SegmentChainContext, state: ChainState, plan: Any, moments: _Moments, prep: _Prep) -> list[str]:
    name_to_identity = manifest_name_to_identity(ctx.payload, plan.source_segment_indexes)
    locks_by_label = _prop_lock.locks_by_label(_prop_lock.moments_for_segment(plan.beat_ids, ctx.appearance_locks))
    delivered_snapshot = list(state.delivered_lines)
    reserved = reserved_lines_for(ctx.required_dialogue_by_segment_no, plan.segment_no)
    drop_phrases = canonical_phrases(ctx.payload)
    errors = [
        *ensure_travel_direction_in_prompt(value, prep.previous_memo), *ensure_wardrobe_continuity_in_prompt(value),
        *ensure_prop_form_matches_lock(value, locks_by_label), *_cast_lock.ensure_cast_lock_in_prompt(value),
        *strip_extra_reference_markers(value, ctx.payload), *overlay_text_errors(value),
        *required_beats_errors(value, prep.structure["required_beats"]),
    ]
    errors.extend(ctx.validate_segment_draft(
        value, dialect_render_format=ctx.profile.render_format, required_dialogue=prep.required_dialogue, name_to_identity=name_to_identity,
        previous_memo=prep.previous_memo, segment_source_text=prep.source_payload["source_text_by_segment"],
        delivered_lines=delivered_snapshot, reserved_lines=reserved, current_segment_no=plan.segment_no, relevant_scenes=prep.relevant_assets["scenes"],
    ))
    errors.extend(generated_identity_errors(
        value, payload=ctx.payload, source_indexes=plan.source_segment_indexes, required_dialogue=prep.required_dialogue,
        dialect=ctx.profile.render_format, narrator_voice_character=ctx.narrator_voice_character,
    ))
    errors.extend(prep.staging_gate.filter(repeated_staging_errors(prep.staging_chain, value.prompt_text, current_segment_no=plan.segment_no, synopsis=plan.synopsis, drop_phrases=drop_phrases)))
    errors.extend(prep.voice_gate.filter(list(moments.voice_here), value.dialogue))
    errors.extend(prep.action_gate.filter(value.shot_action_beats, shot_count=value.shot_count, dialogue_shot_nos=_action_density.dialogue_shot_numbers(value.prompt_text)))
    errors.extend(_music_bed.ensure_no_music_bed_in_prompt(value, render_format=ctx.profile.render_format, enabled=ctx.enhance_music_bed))
    return errors


async def _call_segment_model(ctx: SegmentChainContext, state: ChainState, plan: Any, moments: _Moments, prep: _Prep, task_payload: dict[str, Any]) -> Any:
    fingerprint = hashlib.sha256(json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return await model_gateway.chat_structured(
        [
            {
                "role": "system",
                "content": (
                    "你是短剧分镜师和视频生成提示词撰写者。"
                    f"当前目标模型是 {ctx.profile.model_family}。"
                    "只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释；"
                    "prompt_text 字段内部可以换行，但整体是一个字符串。"
                ),
            },
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
        ],
        model_type=ctx.draft_cls,
        validate=lambda value: _segment_validate(value, ctx, state, plan, moments, prep),
        operation_id=f"storyboard_pack_segment_{ctx.episode_id}_{plan.segment_no}_{fingerprint}",
        max_tokens=ctx.answer_tokens, format_retry_limit=1, semantic_retry_limit=2, temperature=0.6,
        call_meta={
            "stage_key": "storyboard_pack_segment", "call_role": "storyboard_pack_segment_single",
            "initiator_label": "分镜台逐段提示词", "episode_id": ctx.episode_id, "segment_no": plan.segment_no,
            "segment_count": len(ctx.beat_draft.segments), "target_video_model": ctx.target_video_model,
            "contract_version": ctx.contract_version,
        },
        repair_context=storyboard_repair_context(task_payload), format_repair_context=storyboard_repair_context(task_payload),
    )


def _finalize_segment(plan: Any, draft: Any, ctx: SegmentChainContext, prep: _Prep) -> Any:
    draft = finalize_generated_identity(
        draft, payload=ctx.payload, source_indexes=plan.source_segment_indexes, required_dialogue=prep.required_dialogue,
        dialect=ctx.profile.render_format, narrator_voice_character=ctx.narrator_voice_character,
    )
    draft.camera_digest.transition_from_previous = _transition_plan.finalize_transition_after_generation(
        prep.text_transition, prep.previous_scene_ids, {s.scene_id for s in draft.resources.scenes},
        told_transition=prep.structure["transition_from_previous"], segment_no=plan.segment_no,
    )
    return draft


async def _generate_segment_attempt(
    ctx: SegmentChainContext, state: ChainState, plan: Any, moments: _Moments, previous_draft: Any | None,
    revision_text: str, attempt: int, enable_prose_review: bool,
) -> tuple[Any, str]:
    prep = _segment_prep(ctx, state, plan, previous_draft)
    task_payload = _build_task_payload(ctx, state, plan, moments, prep, previous_draft, revision_text)
    draft = await _call_segment_model(ctx, state, plan, moments, prep, task_payload)
    draft = _finalize_segment(plan, draft, ctx, prep)
    next_revision = await _prose_review.review_segment_inline(
        draft, previous_draft=previous_draft, episode_id=ctx.episode_id, segment_no=plan.segment_no,
        photographic=ctx.visual_style_is_photographic, max_shots=ctx.max_shots, attempt=attempt,
        enabled=enable_prose_review, outcomes=state.review_outcomes,
    )
    return draft, next_revision


def _replay_reused_segment(ctx: SegmentChainContext, state: ChainState, plan: Any, draft: Any) -> None:
    """``reuse_segments`` 命中时只推进 claim-once 状态，不发起模型调用——与原
    串行实现的 reuse 分支逐字同构。"""
    state.by_segment_no[plan.segment_no] = draft
    state.camera_digest_by_segment_no[plan.segment_no] = draft.camera_digest
    state.delivered_lines.extend((plan.segment_no, line.speaker_identity_id, line.line) for line in draft.dialogue)
    _beat_causality.moments_for_segment(plan.beat_ids, ctx.beat_draft.emotional_turns, state.covered_turn_ids)
    _beat_foreshadowing.moments_for_segment(plan.beat_ids, ctx.beat_draft.foreshadowing_beats, state.covered_signal_ids)
    state.wardrobe_state.advance(plan.beat_ids)
    _prop_entrance.moments_for_segment(plan.beat_ids, ctx.props_plan, state.covered_prop_ids)
    _stim_voice.voice_claim_moments(plan.beat_ids, ctx.beat_draft.emotional_turns, state.covered_voice_ids)


async def generate_one_segment(
    ctx: SegmentChainContext, state: ChainState, plan: Any, *, reuse_segments: dict[int, Any] | None, revision_notes: str, enable_prose_review: bool,
) -> Any:
    """写一段（含边写边审）：``reuse_segments`` 命中时只重放状态，否则最多
    ``storyboard_prose_review.INLINE_MAX_ATTEMPTS`` 次生成尝试。"""
    ctx.ensure_budget()
    if reuse_segments and plan.segment_no in reuse_segments:
        draft = reuse_segments[plan.segment_no]
        _replay_reused_segment(ctx, state, plan, draft)
        return draft
    previous_draft = state.by_segment_no.get(plan.segment_no - 1)
    moments = _segment_moments(ctx, state, plan)
    revision_text = revision_notes
    attempts = _prose_review.INLINE_MAX_ATTEMPTS if enable_prose_review else 1
    draft = None
    for attempt in range(attempts):
        draft, revision_text = await _generate_segment_attempt(ctx, state, plan, moments, previous_draft, revision_text, attempt, enable_prose_review)
        if not revision_text:
            break
    state.camera_digest_by_segment_no[plan.segment_no] = draft.camera_digest
    state.by_segment_no[plan.segment_no] = draft
    state.delivered_lines.extend((plan.segment_no, line.speaker_identity_id, line.line) for line in draft.dialogue)
    return draft


async def write_one_chain(
    ctx: SegmentChainContext, plans: list[Any], state: ChainState, *, reuse_segments: dict[int, Any] | None, revision_notes: str, enable_prose_review: bool,
) -> ChainState:
    """写一条链：段号顺序严格串行 + 边写边审，状态累加进 ``state``。"""
    for plan in plans:
        await generate_one_segment(ctx, state, plan, reuse_segments=reuse_segments, revision_notes=revision_notes, enable_prose_review=enable_prose_review)
    return state


def fresh_chain_state(ctx: SegmentChainContext) -> ChainState:
    """空起始态（首链/``reuse_segments`` 全量路径/接缝重写公用）。"""
    wardrobe_state = _wardrobe_plan.build_wardrobe_state(ctx.beat_draft.wardrobe_plan, ctx.payload, set(ctx.beats_by_id))
    return ChainState(wardrobe_state=wardrobe_state)
