"""分镜台阶段二：换场切分 / 并行调度 / 接缝复核 / 合并后终局 advisory
（用户拍板，2026-10-01）。单段生成的核心（写一段/写一条链）在姊妹模块
``app.production.storyboard_segment_chains``——这里是围绕多条链的编排，拆成
两个文件纯粹是 ``app/FILE_CONVENTIONS.toml`` 新文件 500 行上限（本次改造的
全部逻辑合在一处约 630 行，装不下，见 CLAUDE.md「装不下时先想怎么拆」）。

## 切分点：只在换场处切

判据不另造一套：直接复用既有的「这一段相对上一段换场」信号——
``screenplay_markers.scene_changed``（原文段头/时段，静态）与
``scene_changed_by_resource_scenes``（计划场景集合变化，用两段各自
``relevant_assets.scenes`` 近似——真实生成期「上一段实际写出的场景」此刻还
不存在，这是开链前唯一能拿到的场景信号，见 ``_is_scene_change_boundary``）。
同一场戏内的段落永远在同一条链里；没有换场时只有 1 条链，与串行逐字一致。

## 跨段累加状态的确定性重放（非首链起始态）

* 只依赖节拍表计划（``plan.beat_ids``）的——情绪转折/伏笔/刺激发声/服装/
  道具入场「认领一次」——按段号顺序对全部计划重放一遍即可精确复现串行时的
  ``covered`` 集合与服装表推进状态，见 ``_chain_start_state``。这些函数本来
  就只读 ``plan.beat_ids`` + 累加器，不读已生成的草稿，可放心重放。
* 依赖已写出草稿的——``delivered_lines``（跨段台词去重账本）用计划阶段已经
  分配好的 ``required_dialogue`` 近似（见 ``_approximate_delivered_lines``）；
  ``camera_digest`` 窗口为空（链内从零累积，不借用其它链写的镜头语言，由
  ``ChainState`` 每条链各自独立天然满足，不需要额外代码）；``previous_draft``/
  连贯性备忘按第 1 段同等处理（``None``）。这些近似带来的偏差由接缝复核
  （``_run_seam_review``）用合并后的真实数据补一次。
* 例外，不能照搬「当第 1 段处理」的一项：``structure["scene_change"]``/
  ``transition_from_previous``（见 ``storyboard_transition_plan.resolve_
  transition_before_generation``）。核对后发现——链首段之所以起新链，正是
  因为换场切分已经判定它与上一段的计划场景（``relevant_assets.scenes``）
  不同；如果这里也把 previous_draft 当 None、previous_scene_ids 传空集合，
  该函数会因为拿不到任何「上一段」信息而判不出换场，与切分的理由自相矛盾
  （真实后果：task_payload 里的 ``scene_change``/``transition_from_previous``
  字段会错误地告诉模型「这是同一场戏」）。修法：``_chain_start_state`` 把
  上一段的计划场景原样写进 ``ChainState.approx_previous_scene_ids``，
  ``storyboard_segment_chains._segment_prep`` 在 previous_draft 为空时改用
  它代替「上一段实际写出的场景」——复用的仍是切分时已经读过的同一份数据，
  不是新近似；这一项如果判不出一致就必须设为切分禁止点，但核对后确认可以
  确定性重放，不需要禁切。

## 接缝复核

全部链写完、按段号合并后，对每个链首段（除第 1 段）做三项检查：① 用
``storyboard_prose_review`` 的同一套复核（``screen_side``/``prop_appearance``/
``repeated_transition_action`` 等跨段判据，用合并后真实的上一段定稿）再审一次
（见 ``_seam_violations``）；② 跨段台词重复检查（见 ``_seam_dialogue_repeat_
errors``）——判据复用既有的 ``storyboard_dialogue_repeat.repeated_delivery_
errors``，喂合并后真实的前序台词（链首段生成时只看得到本链内、从空开始累积的
``delivered_lines``，看不到前一条链末段已经说过的台词，这是并行拆链结构性带来
的盲区）；③ continuity_memo 核验（独立审查发现，2026-10-01 补，见
``_seam_continuity_memo_errors``）——链首段生成时本地没有真实上一段的
continuity_memo 可对齐（只能当近似处理，见 storyboard_segment_chains 模块
docstring 与 storyboard_narrative_arc._continuity_memo_rules_mid_episode_
scene_change），合并后复用生成期本来就用的同一套阻断判据
（``storyboard_continuity_memo.continuity_memo_errors``）用真实上一段核验一次，
顺带就地自愈 time_of_day。三者任一命中就以真实上一段为 previous_draft 重写
该链首段一次（复用 ``reuse_segments`` 机制——与「修订本段」单段重生成同一条
路径，天然拿到真实 previous_draft/continuity_memo/camera_digest/delivered_
lines，不是近似，见 ``_rewrite_chain_head``），然后对它的下一段做一次复核
（只复核 ①，不再连锁重写；仍违规写 degraded_capabilities，前缀沿用现有
``[STORYBOARD_PROSE_REVIEW_REMAINING]``）。只有真正并行出 ≥2 条链时才跑这一步。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.production import storyboard_action_density as _action_density
from app.production import storyboard_beat_causality as _beat_causality
from app.production import storyboard_beat_foreshadowing as _beat_foreshadowing
from app.production import storyboard_prop_appearance_lock as _prop_lock
from app.production import storyboard_prop_entrance as _prop_entrance
from app.production import storyboard_prose_review as _prose_review
from app.production import storyboard_stimulus_voice as _stim_voice
from app.production.screenplay_markers import joined_source_text, scene_changed, scene_changed_by_resource_scenes
from app.production.storyboard_continuity_advisories import segment_continuity_location_advisories
from app.production.storyboard_continuity_memo import continuity_memo_errors
from app.production.storyboard_dialogue_repeat import repeated_delivery_errors, reserved_lines_for
from app.production.storyboard_segment_chains import ChainState, SegmentChainContext, fresh_chain_state, write_one_chain
from app.production.storyboard_segment_ranges import segment_source_payload

_LOGGER = logging.getLogger(__name__)

#: 分镜模型桥接最多 2 路并发（任务给定的硬约束，不得假设更多）；链数可以更多，
#: 排队执行。
MAX_CONCURRENT_CHAINS = 2


@dataclass
class _CoveredIds:
    turn_ids: set[str] = field(default_factory=set)
    signal_ids: set[str] = field(default_factory=set)
    prop_ids: set[str] = field(default_factory=set)
    voice_ids: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# 换场切分
# ---------------------------------------------------------------------------


def _is_scene_change_boundary(ctx: SegmentChainContext, previous: Any, current: Any) -> bool:
    """复用既有信号，不另造判据：见模块 docstring。"""
    previous_text = joined_source_text(ctx.segments, previous.source_segment_indexes)
    current_text = joined_source_text(ctx.segments, current.source_segment_indexes)
    if scene_changed(previous_text, current_text):
        return True
    previous_scenes = {str(s.get("scene_id") or "") for s in ctx.relevant_assets_by_segment_no[previous.segment_no]["scenes"]}
    current_scenes = {str(s.get("scene_id") or "") for s in ctx.relevant_assets_by_segment_no[current.segment_no]["scenes"]}
    return scene_changed_by_resource_scenes(previous_scenes, current_scenes)


def plan_chains(ctx: SegmentChainContext) -> list[list[Any]]:
    """按换场切分 ``ctx.beat_draft.segments``；没有换场时返回只含 1 条链的列表
    （与串行逐字一致所需的前提，见 ``run_segment_generation``）。"""
    plans = ctx.beat_draft.segments
    if not plans:
        return []
    chains: list[list[Any]] = [[plans[0]]]
    for previous, current in zip(plans, plans[1:]):
        if _is_scene_change_boundary(ctx, previous, current):
            chains.append([current])
        else:
            chains[-1].append(current)
    return chains


def _log_chain_plan(ctx: SegmentChainContext, chains: list[list[Any]]) -> None:
    ranges = [f"{chain[0].segment_no}-{chain[-1].segment_no}" for chain in chains]
    _LOGGER.info(
        "[STORYBOARD_CHAIN_PLAN] episode=%s chains=%s ranges=%s max_concurrent=%s",
        ctx.episode_id, len(chains), ranges, MAX_CONCURRENT_CHAINS,
    )


# ---------------------------------------------------------------------------
# 非首链起始状态的确定性重放
# ---------------------------------------------------------------------------


def _approximate_delivered_lines(ctx: SegmentChainContext, before_segment_no: int) -> list[tuple[int, str, str]]:
    """非首链起始态近似：用计划阶段已分配好的 ``required_dialogue`` 代替「实际
    已写台词」——真实 ``dialogue`` 要等模型生成才有，这是开链前唯一能确定性
    拿到的台词数据；去重判据只看跨段文本是否完全相同，``required_dialogue`` 的
    文本就是最终会被写进对应段落的原文，近似缺口由接缝复核补上。"""
    approximated: list[tuple[int, str, str]] = []
    for plan in ctx.beat_draft.segments:
        if plan.segment_no >= before_segment_no:
            break
        for item in ctx.required_dialogue_by_segment_no.get(plan.segment_no, []):
            text = str(item.get("text") or "")
            if text:
                approximated.append((plan.segment_no, str(item.get("speaker_identity_id") or ""), text))
    return approximated


def _chain_start_state(ctx: SegmentChainContext, chain_plans: list[Any]) -> ChainState:
    """首链（含第 1 段）起始态与串行完全一致（全部累加器为空）；非首链按段号
    顺序重放全部计划，确定性复现串行跑到这里时的 ``covered``/服装表状态。"""
    first_no = chain_plans[0].segment_no
    state = fresh_chain_state(ctx)
    if first_no == 1:
        return state
    # 链首段 previous_draft=None，但它之所以起一条新链正是因为换场切分已经判定
    # 「与上一段计划场景不同」；这里把上一段（first_no - 1）的计划场景原样带进
    # 起始态，_segment_prep 用它代替「上一段实际写出的场景」，让生成前的 structure/
    # transition 推导与切分理由保持一致（见 ChainState.approx_previous_scene_ids）。
    state.approx_previous_scene_ids = {
        str(s.get("scene_id") or "") for s in ctx.relevant_assets_by_segment_no[first_no - 1]["scenes"]
    }
    for plan in ctx.beat_draft.segments:
        if plan.segment_no >= first_no:
            break
        _beat_causality.moments_for_segment(plan.beat_ids, ctx.beat_draft.emotional_turns, state.covered_turn_ids)
        _beat_foreshadowing.moments_for_segment(plan.beat_ids, ctx.beat_draft.foreshadowing_beats, state.covered_signal_ids)
        state.wardrobe_state.advance(plan.beat_ids)
        _prop_entrance.moments_for_segment(plan.beat_ids, ctx.props_plan, state.covered_prop_ids)
        _stim_voice.voice_claim_moments(plan.beat_ids, ctx.beat_draft.emotional_turns, state.covered_voice_ids)
    state.delivered_lines.extend(_approximate_delivered_lines(ctx, first_no))
    return state


# ---------------------------------------------------------------------------
# 多链并行调度（Semaphore(MAX_CONCURRENT_CHAINS)：链数可以更多，排队执行）
# ---------------------------------------------------------------------------


async def _run_one_chain_gated(ctx: SegmentChainContext, chain_plans: list[Any], semaphore: asyncio.Semaphore) -> ChainState:
    state = _chain_start_state(ctx, chain_plans)
    async with semaphore:
        return await write_one_chain(ctx, chain_plans, state, reuse_segments=None, revision_notes="", enable_prose_review=ctx.enable_prose_review)


async def _cancel_pending_chains(pending: set[asyncio.Task]) -> None:
    """取消还在跑的链任务并等它们真正收尾（独立审查发现 2，2026-10-01）：裸
    ``asyncio.gather`` 在一条链失败后不会取消其余仍在进行中的链——那些链的
    模型调用会在后台继续跑完、结果无人读取被静默丢弃，白白耗费本机有限的
    CPU/网络资源，调用方捕获异常后立刻重试还会与孤儿调用撞上同一集的重复
    真实模型调用。``return_exceptions=True`` 吞掉每个任务的 ``CancelledError``
    收尾噪音，不让它们变成"exception was never retrieved"。"""
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _run_chains_parallel(ctx: SegmentChainContext, chains: list[list[Any]]) -> tuple[dict[int, Any], list[dict[str, Any]]]:
    """任一链失败时取消其余仍在进行的链、重新抛出原始异常（不包装成
    ExceptionGroup——上层按原异常类型 except 的分支不受影响，见
    ``_cancel_pending_chains`` 文档）；全部成功时按链的原始顺序合并结果。"""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHAINS)
    tasks = [asyncio.create_task(_run_one_chain_gated(ctx, chain, semaphore)) for chain in chains]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    failed = next((t for t in tasks if t.done() and not t.cancelled() and t.exception() is not None), None)
    if failed is not None or pending:
        await _cancel_pending_chains(pending)
        if failed is not None:
            raise failed.exception()
    merged: dict[int, Any] = {}
    outcomes: list[dict[str, Any]] = []
    for task in tasks:
        state = task.result()
        merged.update(state.by_segment_no)
        outcomes.extend(state.review_outcomes)
    return merged, outcomes


# ---------------------------------------------------------------------------
# 接缝复核：链首段（除第 1 段）用真实上一段定稿再审一次
# ---------------------------------------------------------------------------


async def _seam_violations(ctx: SegmentChainContext, draft: Any, previous_draft: Any, segment_no: int) -> list[Any]:
    raw = await _prose_review._review_segment(
        episode_id=ctx.episode_id, segment_no=segment_no, draft=draft, previous_draft=previous_draft,
        photographic=ctx.visual_style_is_photographic, max_shots=ctx.max_shots,
    )
    return _prose_review._verified_violations(raw, segment_no=segment_no, draft=draft, previous_draft=previous_draft)


def _seam_dialogue_repeat_errors(ctx: SegmentChainContext, merged: dict[int, Any], head_no: int) -> list[str]:
    """跨段台词重复检查（任务 3「并做跨段台词重复检查」）：判据复用既有的
    ``storyboard_dialogue_repeat.repeated_delivery_errors``，不新造第二套——
    与串行循环内 ``_segment_validate`` 用的是同一个判据函数，只是这里喂的
    ``delivered`` 是合并后真实的前序台词（链首段生成时只看得到本链内的台词，
    看不到前一条链的末段，这是并行拆链结构性带来的盲区，必须在合并后补一次）。
    """
    delivered: list[tuple[int, str, str]] = []
    for plan in ctx.beat_draft.segments:
        if plan.segment_no >= head_no:
            break
        draft = merged.get(plan.segment_no)
        if draft is None:
            continue
        delivered.extend((plan.segment_no, line.speaker_identity_id, line.line) for line in draft.dialogue)
    current = [(line.speaker_identity_id, line.line) for line in merged[head_no].dialogue]
    reserved = reserved_lines_for(ctx.required_dialogue_by_segment_no, head_no)
    required_texts = [str(item.get("text") or "") for item in ctx.required_dialogue_by_segment_no.get(head_no, [])]
    return repeated_delivery_errors(delivered, current, current_segment_no=head_no, reserved=reserved, required_texts=required_texts)


def _seam_continuity_memo_errors(ctx: SegmentChainContext, merged: dict[int, Any], head_no: int) -> list[str]:
    """接缝 continuity_memo 核验（审查发现 1 的第二道补偿，2026-10-01）：链首段
    生成时本地只能把 continuity_memo 当近似处理（没有真实上一段数据可对齐，见
    ``storyboard_narrative_arc._continuity_memo_rules_mid_episode_scene_change``）；
    合并后用真实的上一段定稿，复用生成期本来就用的同一套阻断判据
    （``continuity_memo_errors``：道具外观改变是否有原文依据、time_of_day_basis
    是否自洽——layout 变化按既有设计只做 advisory 不阻断，见该函数文档，这里
    不改变那条既有策略）。副作用：判据内部会就地把 ``merged[head_no]``
    的 ``continuity_memo.time_of_day`` 修正为沿用真实上一段的值（自洽分支，
    不计入返回的 errors），这是设计内的确定性自愈，不需要额外发起模型重写。
    """
    head_plan = next(p for p in ctx.beat_draft.segments if p.segment_no == head_no)
    source_text = segment_source_payload(head_plan, ctx.segments, ctx.paratext_indexes)["source_text_by_segment"]
    return continuity_memo_errors(merged[head_no].continuity_memo, merged[head_no - 1].continuity_memo, source_text)


def _seam_revision_text(violations: list[Any], dialogue_errors: list[str], memo_errors: list[str]) -> str:
    parts = [p for p in (
        _prose_review._revision_notes_text(violations) if violations else "",
        ("跨段台词重复，请逐条修正（改写成反应、动作或画面呼应，不要原样再说一遍）：\n" + "\n".join(dialogue_errors)) if dialogue_errors else "",
        ("跨段连贯性备忘（continuity_memo）与真实上一段不一致，请逐条修正：\n" + "\n".join(memo_errors)) if memo_errors else "",
    ) if p]
    return "\n".join(parts)


async def _rewrite_chain_head(ctx: SegmentChainContext, merged: dict[int, Any], head_no: int, revision_text: str) -> Any:
    """以真实上一段为 previous_draft 重写一次：复用 ``reuse_segments`` 机制——
    除目标段外全部用合并后的真实定稿，与「修订本段」单段重生成同一条路径，天然
    拿到真实的 previous_draft/continuity_memo/camera_digest 窗口/delivered_lines
    （不再是近似），不新造第二套重写逻辑。"""
    reuse_segments = {no: draft for no, draft in merged.items() if no != head_no}
    state = fresh_chain_state(ctx)
    await write_one_chain(ctx, ctx.beat_draft.segments, state, reuse_segments=reuse_segments, revision_notes=revision_text, enable_prose_review=False)
    return state.by_segment_no[head_no]


async def _review_chain_second_segment(ctx: SegmentChainContext, chain: list[Any], merged: dict[int, Any]) -> int:
    """只复核，不再连锁重写：链首段重写后，它的下一段天然衔接的「上一段」变了，
    需要用新定稿重新核验一次跨段判据；仍违规写 degraded_capabilities。"""
    if len(chain) < 2:
        return 0
    second_no = chain[1].segment_no
    violations = await _seam_violations(ctx, merged[second_no], merged[chain[0].segment_no], second_no)
    if not violations:
        return 0
    merged[second_no].degraded_capabilities = [*merged[second_no].degraded_capabilities, *_prose_review._remaining_advisory_texts(violations)]
    return len(violations)


async def _run_seam_review(ctx: SegmentChainContext, chains: list[list[Any]], merged: dict[int, Any]) -> tuple[int, int]:
    seam_rewritten = 0
    seam_remaining = 0
    for chain in chains[1:]:
        head_no = chain[0].segment_no
        violations = await _seam_violations(ctx, merged[head_no], merged[head_no - 1], head_no)
        dialogue_errors = _seam_dialogue_repeat_errors(ctx, merged, head_no)
        memo_errors = _seam_continuity_memo_errors(ctx, merged, head_no)
        if not violations and not dialogue_errors and not memo_errors:
            continue
        seam_rewritten += 1
        merged[head_no] = await _rewrite_chain_head(ctx, merged, head_no, _seam_revision_text(violations, dialogue_errors, memo_errors))
        seam_remaining += await _review_chain_second_segment(ctx, chain, merged)
    return seam_rewritten, seam_remaining


# ---------------------------------------------------------------------------
# 合并后的终局 advisory 二次遍历（与链数无关，始终跑一次）
# ---------------------------------------------------------------------------


def _segment_advisories_for(ctx: SegmentChainContext, merged: dict[int, Any], plan: Any, draft: Any, relevant_scene_ids: set[str], manifest: Any, covered: _CoveredIds) -> list[str]:
    turns_here2 = _beat_causality.advisory_moments(plan.beat_ids, ctx.beat_draft.emotional_turns, covered.turn_ids)
    voice_here2 = _stim_voice.voice_claim_moments(plan.beat_ids, ctx.beat_draft.emotional_turns, covered.voice_ids)
    signals_here2 = _beat_foreshadowing.moments_for_segment(plan.beat_ids, ctx.beat_draft.foreshadowing_beats, covered.signal_ids)
    props_here2 = _prop_entrance.moments_for_segment(plan.beat_ids, ctx.props_plan, covered.prop_ids)
    location_advisories = segment_continuity_location_advisories(draft, plan, merged, ctx.segments, ctx.paratext_indexes)
    content = ctx.segment_content_advisories(
        draft, source_segment_indexes=plan.source_segment_indexes, segment_relevant_scene_ids=relevant_scene_ids, manifest=manifest,
        emotional_turns_here=turns_here2, foreshadowing_here=signals_here2, prop_entrances_here=props_here2,
        prop_locks_here=_prop_lock.moments_for_segment(plan.beat_ids, ctx.appearance_locks), continuity_location_advisories=location_advisories,
    )
    return [
        *content, *_stim_voice.segment_advisories(voice_here2, draft.dialogue),
        *_action_density.segment_advisories(draft.shot_action_beats, shot_count=draft.shot_count, dialogue_shot_nos=_action_density.dialogue_shot_numbers(draft.prompt_text)),
    ]


def _final_advisory_pass(ctx: SegmentChainContext, merged: dict[int, Any]) -> dict[int, Any]:
    # 延迟导入：app.multiview<->app.validators 互相依赖，模块级导入会触发循环导入。
    from app.multiview import _storyboard_pack_asset_dependencies

    result: dict[int, Any] = {}
    covered = _CoveredIds()
    for plan in ctx.beat_draft.segments:
        draft = merged[plan.segment_no]
        relevant_scene_ids = {str(s.get("scene_id") or "") for s in ctx.relevant_assets_by_segment_no[plan.segment_no]["scenes"]}
        manifest = (
            _storyboard_pack_asset_dependencies(
                project_id=ctx.project_id, episode_no=ctx.episode_no, shot_id=f"draft:{plan.segment_no}",
                segment={"resources": draft.resources.model_dump(mode="json")}, conn=ctx.conn, bible=ctx.bible,
            ) if ctx.conn is not None and ctx.bible is not None else None
        )
        advisories = _segment_advisories_for(ctx, merged, plan, draft, relevant_scene_ids, manifest, covered)
        if advisories:
            draft.degraded_capabilities = [*draft.degraded_capabilities, *advisories]
        result[plan.segment_no] = draft
    return result


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


async def run_segment_generation(ctx: SegmentChainContext, *, reuse_segments: dict[int, Any] | None, revision_notes: str) -> dict[int, Any]:
    """阶段二的唯一入口。``reuse_segments`` 非空时（「修订本段」单段重生成，或
    本模块自己的接缝重写）永远按 1 条链（全集段落）跑，不做换场切分——与该
    路径此前的行为逐字一致；``reuse_segments`` 为空时按换场切分，只有 1 条链
    （没有换场）时同样与串行逐字一致，不触发接缝复核（只有真正并行出多条链
    才需要接缝）。"""
    if reuse_segments:
        state = fresh_chain_state(ctx)
        await write_one_chain(ctx, ctx.beat_draft.segments, state, reuse_segments=reuse_segments, revision_notes=revision_notes, enable_prose_review=ctx.enable_prose_review)
        merged, outcomes, seam_rewritten, seam_remaining = state.by_segment_no, state.review_outcomes, 0, 0
    else:
        chains = plan_chains(ctx)
        _log_chain_plan(ctx, chains)
        merged, outcomes = await _run_chains_parallel(ctx, chains)
        seam_rewritten, seam_remaining = (0, 0)
        if len(chains) > 1 and ctx.enable_prose_review:
            seam_rewritten, seam_remaining = await _run_seam_review(ctx, chains, merged)
    result = _final_advisory_pass(ctx, merged)
    _prose_review.log_review_summary(outcomes, episode_id=ctx.episode_id, enabled=ctx.enable_prose_review, seam_rewritten=seam_rewritten, seam_remaining=seam_remaining)
    return result
