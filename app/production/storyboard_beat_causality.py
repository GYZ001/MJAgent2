"""P0-A：情绪转折/决定性动作的因果闭环核验（2026-09-27）。

背景（docs/AI视频行业方法论对标_可学做法_2026-09-26.md，方法论对标 P0）：
真实回归里出现过"他握紧行李箱拉杆，慢慢点了点头"这类决定性动作只被写进
节拍摘要、从未真正拍成画面，也出现过决定性动作被拍了但促成它的刺激（前面
具体写出的某个事件）从未单独成镜的情况——观众看不到"为什么"，只看到结果。

与短剧节奏档钩子（``app.production.storyboard_short_drama_hooks``）不是
同一件事：钩子只关心开篇/结尾各一个节拍，这里关心**全集范围内所有**推动
人物处境走向的决定/转折，忠实档与短剧档一视同仁（挂在基类
``_AiBeatSheetDraft``，不像钩子只在短剧档生效）。

模型提名、代码核验（CLAUDE.md「判据从数据推导」）：模型在
``_AiBeatSheetDraft.emotional_turns`` 里逐条提名一次决定/转折及其刺激来源，
代码核验这条提名是否真实成立——判据见 ``_turn_problems`` 模块文档。

判据从数据推导，不是黑白名单：``turn_kind``/``signal_kind`` 是开放的两类
标签，不穷举"什么算转折"；提名多少条完全由原文内容决定
（``default_factory=list``，不设最小值）。

阶段一（``causality_beat_sheet_rules``）只接入 ``_generate_beat_sheet`` 的
``validate`` lambda（通过 ``EmotionalTurnSoftCheck``），**绝不写入
``_validate_beat_sheet_draft``**——这是对已确认缺陷 D-α（软检查被自己的
硬校验废掉，见评审记录）的直接修正，也是本模块的验收红线：
``EmotionalTurnSoftCheck`` 的"最后一次放行"必须真的放行，不能被
``_validate_beat_sheet_draft`` 里的同一份判据无条件挡回去。

阶段二"是否真的被写成画面"（``segment_advisories``）是纯事后 advisory，
不参与 ``chat_structured`` 重试、不使用 ``StagingSoftGate``——``StagingSoftGate``
的放行分支只写后端日志，不是产品里的可见信号；这里复用已经在生产可见的
``degraded_capabilities`` 通道（WallPage/BoardPage/StoryboardPackSegmentView
三处渲染 + 一个后期文字导出函数）。能力边界：只能判定"证据文字是否以改写
后的措辞出现在 prompt_text 里"，判不出"是否真的单独成镜"（shots 无结构化
schema）——advisory 措辞必须体现这一点，不得断言代码做不到的判断。

``assemble_adaptation_summary``（2026-09-27，从 ``storyboard_pack.
_assemble_adaptation_summary`` 整体搬移，改名后对外公开）：``storyboard_
pack.py`` 在 ``line_count`` 棘轮基线上零余量，本函数是纯粹的字典组装、不
依赖 ``storyboard_pack`` 任何私有对象，搬到这里（新文件，行数预算充足）
比在原地新增两个 key 更省文件行数；``storyboard_pack.py`` 用 ``as`` 自
别名继续以原名调用，调用点一行不用改（CLAUDE.md「拆包用真包」）。
"""
from __future__ import annotations

from typing import Any

from app import textmatch
from app.production import storyboard_beat_foreshadowing as _beat_foreshadowing
from app.production import storyboard_prop_entrance as _prop_entrance
from app.production import storyboard_short_drama as _short_drama
from app.production import storyboard_short_drama_budget as _short_drama_budget
from app.production import storyboard_wardrobe_plan as _wardrobe_plan
from app.production import storyboard_short_drama_hooks as _short_drama_hooks
from app.production.screenplay_markers import beat_is_shot
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_segment_ranges import evidence_quote_unit_keys
from app.source_excerpt import SourceSegment


def causality_beat_sheet_rules() -> list[str]:
    """阶段一 rules[]，两档都无条件追加（不按 adaptation_mode 分支）。"""
    return [
        "找出本集里每一个改变人物处境走向的重大决定或情绪转折（例如：从犹豫到"
        "点头同意、从隐瞒到坦白、从压抑到爆发），在 emotional_turns 里逐条提名："
        "beat_id 是这个决定/转折本身所在的节拍，turn_kind 填 decisive_action"
        "（人物做出了改变处境的具体动作）或 emotional_reaction（人物产生了强烈"
        "情绪反应但尚未转化为动作），turn_evidence_quote 逐字取自该节拍覆盖的"
        "原文、写出转折本身的具体动作或反应（不是概括）。原文里有几个就提名"
        "几个，不设上限，也不要把普通对话算作转折。",
        "每条提名都要交代刺激来源：原文写清楚了促使这个决定/转折发生的具体"
        "刺激时，把 stimulus_beat_id 填成刺激所在的节拍（可以与转折是同一个"
        "节拍）、stimulus_evidence_quote 逐字取自该节拍覆盖的原文，"
        "stimulus_missing_reason 留空（空字符串）；刺激必须发生在转折之前或"
        "同一节拍，不能是后面才出现的内容。真实案例：原文「他握紧行李箱拉杆，"
        "慢慢点了点头」是一次决定性动作，它的刺激是前面具体写出的某个事件——"
        "不要只标决定本身、漏标促成它的刺激。",
        "如果这次转折/决定确实找不到原文写出的诱因，stimulus_beat_id 与 "
        "stimulus_evidence_quote 都留空（空字符串），并在 stimulus_missing_"
        "reason 里如实说明原文缺了什么——不要为了凑因果链编造原文没写的刺激，"
        "缺了就如实标出来，这本身也是有用信息。stimulus_beat_id/stimulus_"
        "evidence_quote 与 stimulus_missing_reason 必须恰好二选一：有刺激时"
        "前两者非空、后者留空；没有刺激时前两者都留空、后者非空，不能同时"
        "填或同时留空。",
    ]


def _stimulus_problems(
    turn: Any, beats_by_id: dict[str, Any], referenced_beat_ids: set[str],
    source_segments: list[Any], dropped_units: frozenset[tuple[int, int]],
) -> list[str]:
    """有 ``stimulus_beat_id`` 时的四条判据（存在/子串/被引用/删减区间）+
    时序，供 ``_turn_problems`` 复用，各自腾函数行数。"""
    problems: list[str] = []
    stim_beat = beats_by_id.get(turn.stimulus_beat_id)
    if stim_beat is None:
        return [f"引用的刺激节拍 {turn.stimulus_beat_id} 不存在"]
    stim_covered_text = "".join(
        source_segments[i - 1].text for i in stim_beat.segment_indexes if 1 <= i <= len(source_segments)
    )
    condensed_stim_quote = textmatch.condense(turn.stimulus_evidence_quote)
    if not condensed_stim_quote or condensed_stim_quote not in textmatch.condense(stim_covered_text):
        problems.append(f"stimulus_evidence_quote 不是刺激节拍 {turn.stimulus_beat_id} 覆盖原文的子串")
    if turn.stimulus_beat_id not in referenced_beat_ids:
        problems.append(f"刺激节拍 {turn.stimulus_beat_id} 没有被任何段的 beat_ids 引用")
    stim_hit_units = evidence_quote_unit_keys(turn.stimulus_evidence_quote, stim_beat.segment_indexes, source_segments)
    if stim_hit_units and stim_hit_units <= dropped_units:
        problems.append(f"stimulus_evidence_quote 所在原文单元 {sorted(stim_hit_units)} 已被声明为删减区间")
    beat = beats_by_id.get(turn.beat_id)
    if beat is not None and stim_beat.segment_indexes and beat.segment_indexes:
        if min(stim_beat.segment_indexes) > min(beat.segment_indexes):
            problems.append(
                f"刺激节拍 {turn.stimulus_beat_id} 的时序晚于转折节拍 {turn.beat_id}，"
                "刺激必须发生在转折之前或同一节拍"
            )
    return problems


def _turn_problems(
    turn: Any, beats_by_id: dict[str, Any], referenced_beat_ids: set[str],
    source_segments: list[Any], dropped_units: frozenset[tuple[int, int]],
) -> list[str]:
    """单条提名的确定性核验，返回问题文案（空=通过）。判据：
    (1) beat 存在 (2) turn_evidence_quote 是该 beat 覆盖原文子串
    (3) beat_id 被某段 beat_ids 引用 (4) 短剧档：证据命中单元不得全部落入
    dropped_source_spans (5) stimulus_beat_id 与 stimulus_missing_reason
    恰好给出一个、且 stimulus_beat_id 非空时 stimulus_evidence_quote 也必须
    非空 (6)-(9) 有 stimulus_beat_id 时：存在/子串/被引用/短剧档删减区间同上
    四条 (10) 时序：min(stim.segment_indexes) <= min(turn.segment_indexes)。
    """
    beat = beats_by_id.get(turn.beat_id)
    if beat is None:
        return [f"引用的节拍 {turn.beat_id} 不存在"]
    problems: list[str] = []
    covered_text = "".join(
        source_segments[i - 1].text for i in beat.segment_indexes if 1 <= i <= len(source_segments)
    )
    condensed_quote = textmatch.condense(turn.turn_evidence_quote)
    if not condensed_quote or condensed_quote not in textmatch.condense(covered_text):
        problems.append(f"turn_evidence_quote 不是节拍 {turn.beat_id} 覆盖原文的子串")
    if turn.beat_id not in referenced_beat_ids:
        problems.append(f"节拍 {turn.beat_id} 没有被任何段的 beat_ids 引用")
    hit_units = evidence_quote_unit_keys(turn.turn_evidence_quote, beat.segment_indexes, source_segments)
    if hit_units and hit_units <= dropped_units:
        problems.append(f"turn_evidence_quote 所在原文单元 {sorted(hit_units)} 已被声明为删减区间")
    has_stimulus = bool(turn.stimulus_beat_id)
    has_reason = bool(turn.stimulus_missing_reason)
    if has_stimulus == has_reason:
        problems.append(
            "stimulus_beat_id 与 stimulus_missing_reason 必须恰好给出一个"
            "（有刺激填 stimulus_beat_id/stimulus_evidence_quote，没有刺激"
            "只填 stimulus_missing_reason）"
        )
    elif has_stimulus and not turn.stimulus_evidence_quote:
        problems.append("stimulus_beat_id 非空时 stimulus_evidence_quote 也必须非空")
    elif has_stimulus:
        problems.extend(_stimulus_problems(turn, beats_by_id, referenced_beat_ids, source_segments, dropped_units))
    return problems


def _dropped_units_for(draft: Any, source_segments: list[Any]) -> frozenset[tuple[int, int]]:
    """忠实档 ``dropped_source_spans`` 恒缺失，``getattr`` 兜底为空集合，与
    ``storyboard_short_drama_hooks`` 同一份口径（不做 beat 归属过滤，见模块
    docstring）。"""
    spans = getattr(draft, "dropped_source_spans", None) or []
    return frozenset(_short_drama._declared_units(spans, source_segments))


def emotional_turn_errors(draft: Any, source_segments: list[Any]) -> list[str]:
    """聚合 ``draft.emotional_turns`` 的全部 ``_turn_problems``；不接收
    ``adaptation_mode``——判据本身两档都跑，短剧档特有的删减区间检查靠
    ``getattr`` 自然退化为空集合。"""
    beats_by_id = {beat.beat_id: beat for beat in draft.beat_sheet}
    referenced_beat_ids = {beat_id for seg in draft.segments for beat_id in seg.beat_ids}
    dropped_units = _dropped_units_for(draft, source_segments)
    errors: list[str] = []
    for turn in draft.emotional_turns:
        errors.extend(
            f"情绪转折提名 {turn.beat_id}：{problem}"
            for problem in _turn_problems(turn, beats_by_id, referenced_beat_ids, source_segments, dropped_units)
        )
    return errors


class EmotionalTurnSoftCheck:
    """前 ``retry_limit`` 次不满足当业务错误打回模型重试，最后一次仍不满足
    则放行、不再产生错误（同 ``HookBeatSoftCheck``/``SegmentCountSoftCap``
    同一套让步策略）。不要 ``adaptation_mode`` 参数——判据本身两档都跑，短剧档
    特有的删减区间检查靠 ``getattr`` 自然退化。"""

    def __init__(self, *, retry_limit: int, source_segments: list[Any]) -> None:
        self._retry_limit = retry_limit
        self._attempt = 0
        self._source_segments = source_segments

    def errors(self, draft: Any) -> list[str]:
        problems = emotional_turn_errors(draft, self._source_segments)
        is_last_attempt = self._attempt >= self._retry_limit
        self._attempt += 1
        if not problems or is_last_attempt:
            return []
        return problems


def causality_summary(draft: Any, source_segments: list[Any]) -> dict[str, Any]:
    """按最终持久化 beat_draft 事后重算（同 ``hook_summary`` 哲学，只需
    ``source_segments``，不需要 ``segment_drafts``）。三态：
    ``{"status": "no_turns_nominated", "problem_count": 0}``（``emotional_
    turns`` 为空）；``{"status": "ok"/"warning", "problem_count": N}``
    （N = 有 >=1 条 ``_turn_problems`` 非空的提名数）。另带
    ``missing_stimulus_count``：如实声明原文缺诱因的提名数。两档都计算（不像
    ``hook_summary`` 那样忠实档返回 None）——这正是覆盖忠实档的落点。"""
    turns = draft.emotional_turns
    if not turns:
        return {"status": "no_turns_nominated", "problem_count": 0, "missing_stimulus_count": 0}
    beats_by_id = {beat.beat_id: beat for beat in draft.beat_sheet}
    referenced_beat_ids = {beat_id for seg in draft.segments for beat_id in seg.beat_ids}
    dropped_units = _dropped_units_for(draft, source_segments)
    problem_count = sum(
        1 for turn in turns
        if _turn_problems(turn, beats_by_id, referenced_beat_ids, source_segments, dropped_units)
    )
    # 原文没写诱因是剧本层问题，不是提名不合格（不计入 problem_count、不改 status），
    # 但它正是用户要在面板上看到的信号：只挂在逐段 advisory 里会被埋在分镜网格中。
    missing_stimulus_count = sum(1 for turn in turns if turn.stimulus_missing_reason)
    return {
        "status": "warning" if problem_count else "ok", "problem_count": problem_count,
        "missing_stimulus_count": missing_stimulus_count,
    }


def moments_for_segment(segment_beat_ids: list[str], turns: list[Any], covered: set[str]) -> list[Any]:
    """本段（source: ``plan.beat_ids``）首次认领的提名——覆盖
    ``storyboard_capacity_normalize._split_one_segment`` 把 ``beat_ids``
    完整继承给每个拆分子段这一事实（否则同一处决定性动作会被连续几个续段
    反复索要画面）。``covered`` 由调用方传入并原地 ``update``，两处调用点
    （阶段二 payload 与阶段二 advisory）各自维护独立的 ``covered`` 累加器，
    不共享。"""
    claimed: list[Any] = []
    for turn in turns:
        if turn.beat_id in segment_beat_ids and turn.beat_id not in covered:
            claimed.append(turn)
            covered.add(turn.beat_id)
    return claimed


def advisory_moments(segment_beat_ids: list[str], turns: list[Any], covered: set[str]) -> list[Any]:
    """阶段二告警用的认领：与 ``moments_for_segment`` 同一条认领规则；刺激节拍不在
    本段 ``beat_ids`` 里时（刺激在更早的段落交代过），刺激原句不该拿本段镜头稿去
    核对——返回的副本清空 ``stimulus_evidence_quote``，只核对转折本身。
    2026-09-28《顾念长安》第二版第 1 集第 16 段实测：刺激在第 9 段，却因为拿第
    16 段镜头稿核对刺激原句而报「没有被写成画面」。"""
    return [
        turn if turn.stimulus_beat_id in segment_beat_ids else turn.model_copy(update={"stimulus_evidence_quote": ""})
        for turn in moments_for_segment(segment_beat_ids, turns, covered)
    ]


def segment_rule_text(turns_here: list[Any], segment_beat_ids: list[str]) -> list[str]:
    """阶段二 per-segment 正面陈述，三分支，每条都引用
    ``turn_evidence_quote``/``stimulus_evidence_quote`` 原文，不写泛泛的话。"""
    rules: list[str] = []
    for turn in turns_here:
        if turn.stimulus_missing_reason:
            rules.append(
                f"节拍 {turn.beat_id} 对应的决定/转折「{turn.turn_evidence_quote}」原文没有写出"
                f"明确诱因（{turn.stimulus_missing_reason}）：仍要把这个决定/转折单独写成一镜，"
                "按原文实际发生的样子拍出来，不要编造原文没有的刺激"
            )
        elif turn.stimulus_beat_id in segment_beat_ids:
            rules.append(
                f"节拍 {turn.beat_id} 是一次决定性动作/情绪转折，刺激「{turn.stimulus_evidence_quote}」"
                f"与决定/转折本身「{turn.turn_evidence_quote}」都在本段：先把刺激画成一镜，紧接着把"
                "决定/转折动作单独画成下一镜，不要揉进同一镜头描述，也不要停在别的画面"
                "（例如对方的表情）上就切走"
            )
        else:
            rules.append(
                f"节拍 {turn.beat_id} 的刺激「{turn.stimulus_evidence_quote}」已在更早段落交代过，"
                f"本段把决定/转折动作本身「{turn.turn_evidence_quote}」单独写成一镜拍出来"
            )
    return rules


def segment_advisories(turns_here: list[Any], prompt_text: str) -> list[str]:
    """非阻断，供 ``_segment_content_advisories`` 合并进 ``degraded_
    capabilities``。能力边界：只能判定"证据文字是否以改写后的措辞出现"，
    判不出"是否真的单独成镜"（shots 无结构化 schema），advisory 措辞体现
    这一点，不断言代码做不到的判断；用户可见文案给出路（原文层面的问题
    如何补救、分镜台如何编辑补上）。"""
    advisories: list[str] = []
    for turn in turns_here:
        if turn.stimulus_missing_reason:
            advisories.append(
                "[STORYBOARD_PACK_EMOTIONAL_TURN_NO_STIMULUS][未拦截] 情绪转折"
                f"「{turn.turn_evidence_quote[:40]}」原文未交代明确诱因：{turn.stimulus_missing_reason}"
                "——这是原文层面的问题，可在原文补写诱因后重跑本集分镜"
            )
        quotes = [turn.turn_evidence_quote]
        if turn.stimulus_evidence_quote:
            quotes.append(turn.stimulus_evidence_quote)
        for quote in quotes:
            if not beat_is_shot(f"必现内容：{quote}", prompt_text):
                advisories.append(
                    "[STORYBOARD_PACK_EMOTIONAL_TURN_NOT_SHOWN][未拦截] 决定性动作/情绪转折"
                    f"「{quote[:40]}」看起来没有被写成画面（只能判断证据文字有没有以改写措辞"
                    "出现，判不出是否单独成镜），请人工核查——可在分镜台编辑本段镜头稿补上"
                )
    return advisories


def assemble_adaptation_summary(
    *, adaptation_mode: str, planned_segment_count: int, beat_draft: Any, dialogue_quotes: list[DialogueQuote],
    projected_segment_count: int | None, drop_review: Any, segments: list[SourceSegment], payload: dict[str, Any],
) -> dict[str, Any]:
    """adaptation 留档字典组装，从 ``storyboard_pack.generate_storyboard_pack``
    抽出腾 function_lines（该函数已顶 baseline 153）；hooks 见 ``storyboard_
    short_drama_hooks.hook_summary``（按最终 beat_draft 事后重算，忠实档恒
    None）。``causality``/``foreshadowing`` 两个新 summary（2026-09-27）对
    忠实档/短剧档一视同仁地计算，不像 ``hooks`` 那样忠实档返回 None——这正是
    覆盖忠实档的落点。``wardrobe_plan``/``prop_entrances``（2026-09-29，P0-D）
    同一哲学，见 ``storyboard_wardrobe_plan``/``storyboard_prop_entrance``
    模块 docstring；``payload`` 只为它们核验 identity_id 用，其余 summary
    不需要。

    ``wardrobe_plan_full``/``prop_entrances_full``（2026-09-29，同批）：模型
    原始提名的完整列表（不是上面两个 key 的三态统计），供
    ``storyboard_identity_regenerate._existing_plan`` 单段重生成时重建
    ``_AiBeatSheetDraft.wardrobe_plan``/``prop_entrances`` 用——那条路径只有
    ``shots.shot_contract_json`` 里落库的逐段结果，没有整集规划阶段的原始
    提名，此前只能重建出空列表（``storyboard_beat_sheet_schemas`` 字段
    docstring）。这两个 key 只是「新增字段，未改/未删已有字段名」的又一次
    additive 扩展（与本函数上面 2026-09-24/09-27 两批扩展同一条纪律，见
    ``storyboard_pack_evidence`` 模块 docstring「冻结契约」段：门禁只读
    ``adaptation_mode``/``dropped_source_spans`` 两个字段，新增字段不影响
    它们，也不占用已有 key 名——``wardrobe_plan``/``prop_entrances`` 两个
    key 已经被上面的三态统计占用，这里必须用不同名字，不能覆盖）；不新开
    artifact 类型、不建表，复用同一条 ``storyboard_pack_adaptation`` 产物。"""
    return {
        **_short_drama.adaptation_summary(
            adaptation_mode=adaptation_mode, planned_segment_count=planned_segment_count, segment_count=len(beat_draft.segments),
            dropped_spans=getattr(beat_draft, "dropped_source_spans", None) or [], dropped_quote_ids=_short_drama.dropped_line_quote_ids(beat_draft),
            kept_dialogue_chars=_short_drama_budget.kept_dialogue_chars(beat_draft.kept_lines, dialogue_quotes), projected_segment_count=projected_segment_count,
        ),
        "drop_review": drop_review,
        "hooks": _short_drama_hooks.hook_summary(beat_draft, segments, adaptation_mode=adaptation_mode),
        "causality": causality_summary(beat_draft, segments),
        "foreshadowing": _beat_foreshadowing.foreshadowing_summary(beat_draft, segments),
        "wardrobe_plan": _wardrobe_plan.wardrobe_plan_summary(beat_draft, payload),
        "prop_entrances": _prop_entrance.prop_entrance_summary(beat_draft),
        "wardrobe_plan_full": [item.model_dump(mode="json") for item in beat_draft.wardrobe_plan],
        "prop_entrances_full": [item.model_dump(mode="json") for item in beat_draft.prop_entrances],
    }
