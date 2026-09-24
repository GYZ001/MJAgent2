"""短剧节奏档：删减复核——独立调用核对「模型删的东西是不是真的可以删」。

背景（2026-09-24 B 机沙箱第四轮真实验证，我欲封天 EP3）：确定性核验（见
``storyboard_short_drama``/``storyboard_short_drama_schemas.verify_dropped_
source_spans``）只能核对「声明自洽」——beat_id 存在、所属节拍 optional、
segment_indexes 覆盖——核对不了「这段内容语义上是不是真的不重要」。EP3 的
修炼口诀 Q26「凝气入体，融散全身，经脉一通，天地共鸣。」与目标铺垫 Q28/Q33
第一次因区间被拒后，模型在重试里把这段剧情整体改标成一个更大的 optional
节拍再合法删掉——声明自洽，但删的恰恰是后续剧情要用的关键设定与目标铺垫。

用户拍板：加一次独立复核调用（先例是 ``app.production.prep_pack.
scene_recheck``——单一职责的调用不跟别的任务抢注意力，模型提名、代码核验）。
本模块只在 ``adaptation_mode="short_drama"`` 生效；忠实档、以及短剧档没有
任何删减时，直接返回第一遍草稿，不发起任何复核调用。

2026-09-24 B 机沙箱第五轮真实验证：关键内容全部保住，但时长不降反升
（我欲封天 EP3 165s→240s）——根因是送审粒度太粗，一整条删减区间（可能
几百字）只要有一句关键就整条判 must_keep，区间里其余真正无关的内容跟着
一起被救回。送审粒度下沉到句单元（``storyboard_short_drama_review_items.
_collect_review_items``，与 ``dropped_source_spans.from_unit/to_unit`` 同一套
单元编号）之后，复核结果精确到「哪几句」而不是「这一整条」，本模块只消费
拆分结果，不关心怎么拆——见该模块 docstring 的三条规则（极短单元不送审、
超长区间合并送审、区间外台词逐句不变）。

## 三步流程

1. ``_generate_beat_sheet``（未改动，直接复用真源）生成第一遍节拍表并通过
   全部既有校验。
2. 有删减（有效删减区间拆分出的句单元，或区间外弃置的整句台词——语气词/
   屏上文字/区间强制弃置/极短单元不算，见 ``storyboard_short_drama_review_
   items._collect_review_items``）时，发起一次独立复核调用：逐条判断这条
   被删内容是否交代了后续剧情会用到的目标/期限/计划/承诺、伏笔/悬念、
   关键设定、或人物关系变化；判 must_keep 的必须引用被删内容里承载这个
   判断的那一句（去标点空白归一化后须是子串，见 ``_resolve_review_
   verdicts``；不满足的按 droppable 处理并记日志，模型提名、代码核验）。
   复核调用失败（重试耗尽/供应商错误）不让整集失败：按无必保项继续，
   ``drop_review.status="failed"`` 如实记录。
3. 只有复核给出至少一条有效 must_keep 时，才生成第二遍节拍表：payload 里
   带上必保清单与「允许删减的候选」（= 第一遍删减里被复核判 droppable 的
   句单元/台词条目），第二遍的确定性强制见 ``_enforce_second_pass_
   constraints``——判据同样是句单元粒度，见该函数与 ``_clip_spans_to_
   allowed_units`` 各自 docstring。

## 第二遍强制为什么不改 ``storyboard_beat_sheet._validate_beat_sheet_draft``，也不死锁

那个函数与它所在的文件都在棘轮基线上零余量（500/500 行、50/50 代码行）；
给它传必保/候选上下文要么改签名要么腾行数，两条都会动一个已顶格的文件。
必保/候选上下文只有本模块自己的第二遍 ``chat_structured`` 调用需要，于是
把强制动作做成第二遍专属 ``validate`` 回调的**前置步骤**——在调用未改动的
``_validate_beat_sheet_draft`` 之前，先把「不在候选里的声明删减」从
``draft.dropped_source_spans``/``draft.dropped_lines`` 里剔除。剔除之后，
这些单元在它眼里就是「模型没声明删减、又没排进任何段」的普通缺口，与模型
单纯漏排一段原文同构：既有的 ``fix_order_and_fill_holes``/``append_segments_
for_uncovered_sources``（``storyboard_beat_sheet_repair.py``，同样未改动）
会把它们排进相邻段或新段，``reassign_kept_lines_to_covering_segments`` 会把
本模块暂挂在占位段号上的必保台词挪到真正覆盖它的段——全程复用已在生产验证
过的确定性回填机制。这也是不死锁的原因（CLAUDE.md「修补器与校验器死锁」：
死锁形状是「A 的产出触发 B 打回、B 的修补又让 A 的判断失效」）：前置步骤只
执行一次纯粹的集合裁剪，不产生"错误"、不参与语义重试循环，交给既有校验/
修补链之后，它不知道也不需要知道这个状态是复核强制出来的，走的是对任何
普通草稿都会走的同一条路径，因此第二遍必然在第一次 ``validate`` 调用即
满足这部分校验（模型自己在台词归属/色温等其它维度产生的问题仍走既有语义
重试预算，与本模块无关）。

必保单元被夹在两段仍然合法的删减中间时（同一条区间拆出的三个单元里只有
中间那个是 must_keep），只裁剪声明本身不够——``fix_order_and_fill_holes``/
``_gap_fill_plan``（``storyboard_beat_sheet_repair.py``，不改）对「两侧都是
有效删减、中间夹一个非删减单元」的缺口，找不到一段连续范围能只覆盖中间
不牵连两侧时会整体回填，等于把两侧本该继续删除的单元也一起救回——这是
该安全网蓄意的「偏向保留」设计（宁可多留不可错删），不是 bug，不能改。
唯一的正确解法在提示词层面：``_must_keep_rule`` 明确要求模型让覆盖必保
单元的段显式声明包含它的 ``source_unit_ranges``（单独一小段范围或并入
相邻段），这样它从一开始就不是「缺口」，``_gap_fill_plan`` 根本不会被
触发到这段范围。

## 契约

``drop_review`` 留档形状（``StoryboardPack.adaptation["drop_review"]``，
``app.domain.video_ops.storyboard_adaptation`` 只读透出给前端）::

    {"status": "ok" | "failed" | "skipped", "reviewed_count": int,
     "must_keep": [{"item_id": str, "kind": "unit" | "line", "text": str,
                     "evidence_quote": str}],
     "second_pass": bool}

老留档没有这个字段，读取方一律 ``dict.get("drop_review")`` 降级为
``None``，不抛异常（同一立场见 ``storyboard_pack_evidence`` 对 2026-09-24
新增字段的处理）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model

from app import textmatch
from app.harness import model_gateway
from app.production.storyboard_beat_sheet import (
    _BEAT_SHEET_SEMANTIC_RETRY_LIMIT,
    _AiBeatSheetDraft,
    _beat_sheet_draft_cls,
    _beat_sheet_task_payload,
    _generate_beat_sheet,
    _normalize_beat_sheet_payload,
    _paratext_segment_indexes,
    _validate_beat_sheet_draft,
)
from app.production.storyboard_beat_sheet_schemas import DROPPABLE_MAX_CHARS
from app.production.storyboard_context_segments import context_segment_indexes
from app.production.storyboard_dialogue_ledger import DialogueQuote, _AiKeptLine
from app.production import storyboard_short_drama as _short_drama
from app.production import storyboard_short_drama_budget as _short_drama_budget
from app.production.storyboard_short_drama_beat_guard import _find_covering_segment_no
from app.production.storyboard_repair_context import storyboard_repair_context
from app.production.storyboard_segment_ranges import quote_unit_index
from app.production.storyboard_short_drama_review_items import (
    _DropReviewItem,
    _collect_review_items,
    unit_is_trivial_at,
)
from app.source_excerpt import SourceSegment

_LOGGER = logging.getLogger(__name__)

#: 留档 must_keep[].text 的展示摘录上限——与 storyboard_pack_evidence 的
#: 原文摘录（span excerpt[:24]）不是同一个字段，这里给的是复核输入原文
#: （可能是合并后最多 3 个单元的拼接），留更多字数才看得出"关键内容"具体
#: 是什么。条目收集/单元拆分见 ``storyboard_short_drama_review_items``。
_EXCERPT_MAX_CHARS = 60


def _skipped_review() -> dict[str, Any]:
    return {"status": "skipped", "reviewed_count": 0, "must_keep": [], "second_pass": False}


# ---------------------------------------------------------------------------
# 复核调用：只问一件事（先例 app.production.prep_pack.scene_recheck）
# ---------------------------------------------------------------------------

_REVIEW_TASK = (
    "你在复核短剧节奏改编里的一批删减决定。下面每一条内容都已经被另一次改写判定为"
    "「可以不出现在最终视频里」，理由写在 drop_reason 里；但那次判断只权衡了篇幅与"
    "节奏，没有专门核对内容对后续剧情的作用。你的任务只有一件事：逐条判断这条被删"
    "内容有没有交代下面四类信息中的任意一类——\n"
    "1. 目标/期限/计划/承诺：人物说要在多久之内做成什么事、许下的约定；\n"
    "2. 伏笔/悬念：为后续情节埋下的线索，或留下悬而未决的问题；\n"
    "3. 关键设定：功法口诀、世界规则、人物身份、能力边界这类后续情节会引用或验证"
    "的具体内容；\n"
    "4. 人物关系的变化：两个人物之间关系状态的转折点。\n\n"
    "只依据这条内容自己的文字判断，不用去猜后面章节写了什么。属于以上任意一类时，"
    "must_keep 填 true，并从这条内容的原文里逐字摘录承载这个判断的那一句话，填入 "
    "evidence_quote——必须是这条内容原文里连续出现的一段文字，不得改写、概括、或"
    "引用其它条目/原文段的内容。都不属于时，must_keep 填 false，evidence_quote 填"
    "空字符串。不要考虑删了会不会影响时长、节奏或观感——那些已经由别的机制判断"
    "过，与你无关。\n\n"
    "items 里的每一条通常是一句原文，个别条目是把原删减区间里位置相邻的最多三句"
    "合并成一条（原区间很长时）——按这条 text 的整体文字判断即可，不需要在条目"
    "内部再拆分。部分条目带 region 字段，说明它出自哪一条更大的删减区间，仅供你"
    "理解上下文：region 里没有单独列成 items 的其它内容不属于这次判断范围，不要"
    "据此推断它们已经确定可删或必须保留。\n\n"
    "下面的原文段仅供你理解上下文，不是复核对象本身；真正要逐条判断的是 items "
    "清单里的每一条。"
)


def _review_response_model(item_ids: list[str]) -> type[BaseModel]:
    """合法 item_id 取值域用动态 Literal 钉死（同一手法见 app.production.
    prep_pack.scene_recheck.response_model：落在 model_type 上而不是
    output_schema，才会真正下发到供应商侧的 response_format.json_schema）。"""
    verdict = create_model(
        "_DropReviewVerdict", __config__=ConfigDict(extra="forbid"),
        item_id=(Literal[tuple(item_ids)], ...),  # type: ignore[valid-type]
        must_keep=(bool, ...),
        evidence_quote=(str, ...),
    )
    return create_model(
        "_DropReviewResponse", __config__=ConfigDict(extra="forbid"), items=(list[verdict], ...),
    )


def _review_item_json(item: _DropReviewItem) -> dict[str, Any]:
    """条目在 items 清单里的 JSON 形状——``region`` 只在非空（来自区间拆分的
    ``kind="unit"`` 条目）时才带上，台词条目没有所属区间，不写这个键。"""
    payload: dict[str, Any] = {"item_id": item.item_id, "text": item.text, "drop_reason": item.reason}
    if item.region_label:
        payload["region"] = item.region_label
    return payload


def _review_prompt(items: list[_DropReviewItem], source_segments: list[SourceSegment]) -> str:
    touched = sorted({it.source_segment_index for it in items if 1 <= it.source_segment_index <= len(source_segments)})
    segment_block = "\n".join(f"[段{i}] {source_segments[i - 1].text}" for i in touched)
    items_block = json.dumps([_review_item_json(it) for it in items], ensure_ascii=False)
    return f"{_REVIEW_TASK}\n\n原文段：\n{segment_block}\n\nitems：\n{items_block}"


def _resolve_review_verdicts(
    items: list[_DropReviewItem], response: Any,
) -> tuple[list[tuple[_DropReviewItem, str]], list[_DropReviewItem], list[str]]:
    """按 item_id 对齐模型判断：重复的取第一次、缺失的按 droppable、must_keep
    但 evidence_quote 经 ``textmatch.condense`` 归一化（同 quote_provenance_
    errors 口径，去标点/空白容忍引用差一个句末标点或引号）后仍不是原文子串
    的按 droppable——全部记日志，不打回整集（模型提名、代码核验）。返回
    ``(must_keep 及其证据引文, droppable, 人话核验日志)``。
    """
    notes: list[str] = []
    verdict_by_id: dict[str, Any] = {}
    for verdict in response.items:
        if verdict.item_id in verdict_by_id:
            notes.append(f"{verdict.item_id} 复核结果重复出现，取第一次判断")
            continue
        verdict_by_id[verdict.item_id] = verdict
    must_keep: list[tuple[_DropReviewItem, str]] = []
    droppable: list[_DropReviewItem] = []
    for item in items:
        verdict = verdict_by_id.get(item.item_id)
        if verdict is None:
            notes.append(f"{item.item_id} 复核未覆盖，按 droppable 处理")
            droppable.append(item)
            continue
        quote = str(verdict.evidence_quote or "").strip()
        if verdict.must_keep and textmatch.condense(quote) and textmatch.condense(quote) in textmatch.condense(item.text):
            must_keep.append((item, quote))
            continue
        if verdict.must_keep:
            notes.append(f"{item.item_id} 复核标 must_keep 但 evidence_quote 归一化后不是原文子串，按 droppable 处理")
        droppable.append(item)
    return must_keep, droppable, notes


async def _run_drop_review(
    *, episode_id: str, contract_version: str, items: list[_DropReviewItem], source_segments: list[SourceSegment],
) -> tuple[list[tuple[_DropReviewItem, str]], list[_DropReviewItem], list[str]] | None:
    """独立复核调用；失败（格式修复耗尽/供应商拒绝/网络错误等）返回 ``None``，
    调用方据此按「无必保项」继续，不许让整集失败（见模块 docstring）。"""
    fingerprint = hashlib.sha256(
        json.dumps([{"item_id": it.item_id, "text": it.text} for it in items], ensure_ascii=False, sort_keys=True).encode("utf-8"),
    ).hexdigest()[:24]
    try:
        response = await model_gateway.chat_structured(
            [
                {"role": "system", "content": "你是短剧改编的删减复核员。只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释。"},
                {"role": "user", "content": _review_prompt(items, source_segments)},
            ],
            model_type=_review_response_model([it.item_id for it in items]),
            validate=None,
            operation_id=f"storyboard_pack_drop_review_{episode_id}_{fingerprint}",
            max_tokens=4000,
            format_retry_limit=1,
            semantic_retry_limit=0,
            temperature=0.2,
            call_meta={
                "stage_key": "storyboard_pack_drop_review",
                "call_role": "storyboard_drop_review",
                "initiator_label": "分镜台删减复核",
                "episode_id": episode_id,
                "contract_version": contract_version,
            },
        )
        return _resolve_review_verdicts(items, response)
    except Exception:  # noqa: BLE001 -- 复核失败（含结果解析）不许让整集失败，见模块 docstring
        _LOGGER.warning("[STORYBOARD_SHORT_DRAMA_REVIEW] 删减复核调用失败，按无必保项继续 episode=%s", episode_id, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 第二遍：必保清单 + 候选清单，确定性强制
# ---------------------------------------------------------------------------

def _item_payload(item: _DropReviewItem) -> dict[str, Any]:
    base = {"item_id": item.item_id, "kind": item.kind, "source_segment_index": item.source_segment_index, "text": item.text}
    if item.kind == "unit":
        return {**base, "from_unit": item.from_unit, "to_unit": item.to_unit}
    return {**base, "quote_id": item.quote_id}


def _must_keep_rule(must_keep: list[_DropReviewItem], candidates: list[_DropReviewItem]) -> str:
    return (
        f"这是同一集的第二次节拍规划，删减复核已经跑过一轮：must_keep_items（共 {len(must_keep)} 条）"
        "是复核判定为交代了后续剧情会用到的目标/期限/计划/承诺、伏笔/悬念、关键设定或人物关系变化的"
        "原文内容，本次必须让它们的原文完整出现在某一段的画面或台词里（台词进 kept_lines，不得再出现"
        f"在任何 dropped_source_spans/dropped_lines 里）；droppable_candidates（共 {len(candidates)} 条）"
        "是复核判定为确实可以不拍的内容，你这次弃置或整段删除时只能从这份候选清单里选，不得弃置或删除"
        "候选清单之外的其它台词或原文区间，也不得扩大候选清单里某一条的删除范围。must_keep_items 里"
        "kind=unit 的条目是原删减区间里的一句或相邻几句原文（不是整条区间的全部内容），必须让某一段"
        "的 source_unit_ranges 显式覆盖到这几个单元——单独给它一小段范围，或并入相邻段的范围都可以，"
        "不要留空指望系统自动补上：系统只会在探测到「漏拍」时兜底回填，一旦回填就会连同它两侧本该继续"
        "删除的内容一起救回，达不到本次删减的目的。除了这些新约束，其余规则与上一次相同。"
    )


def _allowed_sets(candidates: list[_DropReviewItem]) -> tuple[set[str], frozenset[tuple[int, int]]]:
    allowed_quote_ids = {it.quote_id for it in candidates if it.kind == "line"}
    allowed_span_units = frozenset(
        (it.source_segment_index, u) for it in candidates if it.kind == "unit" for u in range(it.from_unit, it.to_unit + 1)
    )
    return allowed_quote_ids, allowed_span_units


def _revert_disallowed_lines(
    draft: Any, quotes: list[DialogueQuote], source_segments: list[SourceSegment], *, allowed_quote_ids: set[str],
) -> None:
    """第二遍弃置的整句台词不在候选内（必保台词、或模型新弃置的候选外台词）
    时，确定性放回 kept_lines——先取覆盖它的段，找不到就退而求其次挂在第一
    段，后续 ``_validate_beat_sheet_draft`` 里的 ``reassign_kept_lines_to_
    covering_segments`` 会再校正到真正覆盖它的段（见模块 docstring）。"""
    quotes_by_id = {q.quote_id: q for q in quotes}
    remaining: list[Any] = []
    for item in draft.dropped_lines:
        quote = quotes_by_id.get(item.quote_id)
        trivial = quote is None or not quote.speaker or quote.content_chars <= DROPPABLE_MAX_CHARS
        if trivial or item.quote_id in allowed_quote_ids:
            remaining.append(item)
            continue
        idx = quote.source_segment_index
        unit_no = quote_unit_index(quote, source_segments[idx - 1].text) if 1 <= idx <= len(source_segments) else -1
        segment_no = _find_covering_segment_no(draft, idx, unit_no)
        if segment_no is None and draft.segments:
            segment_no = draft.segments[0].segment_no
        if segment_no is None:
            remaining.append(item)
            continue
        draft.kept_lines.append(_AiKeptLine(quote_id=item.quote_id, segment_no=segment_no))
        _LOGGER.info(
            "[STORYBOARD_SHORT_DRAMA_REVIEW] %s 不在第二遍允许删减的候选内，放回 kept_lines（第 %s 段）",
            item.quote_id, segment_no,
        )
    draft.dropped_lines = remaining


def _clip_spans_to_allowed_units(
    draft: Any, source_segments: list[SourceSegment], *, allowed_span_units: frozenset[tuple[int, int]],
) -> None:
    """第二遍声明的删减区间与候选单元取交集，候选外的单元裁掉——裁掉后这些
    单元在 ``_validate_beat_sheet_draft`` 眼里就是普通缺口，既有回填机制会
    把它们排回某一段（见模块 docstring）。极短单元（``unit_is_trivial_at``，
    与 ``storyboard_short_drama_review_items._collect_review_items`` 判断"要
    不要送审"同一套口径）即使不在候选集合里也放行——它们本来就没被送审
    （见该模块 docstring 规则 1「极短单元不送审、直接按可删」），不能因为
    没出现在候选清单里就被这里误判成"未经允许的删减"裁掉。"""
    spans = getattr(draft, "dropped_source_spans", None) or []
    declared = _short_drama._declared_units(spans, source_segments)
    kept_units = {
        key: value for key, value in declared.items()
        if key in allowed_span_units or unit_is_trivial_at(key[0], key[1], source_segments)
    }
    clipped = len(declared) - len(kept_units)
    if clipped:
        _LOGGER.info("[STORYBOARD_SHORT_DRAMA_REVIEW] 第二遍声明的删减区间裁掉候选之外的 %d 个单元", clipped)
    draft.dropped_source_spans = _short_drama._canonical_spans(frozenset(kept_units), kept_units)


def _enforce_second_pass_constraints(
    draft: Any, quotes: list[DialogueQuote], source_segments: list[SourceSegment], *,
    allowed_quote_ids: set[str], allowed_span_units: frozenset[tuple[int, int]],
) -> None:
    """第二遍确定性强制的入口，挂在第二遍专属 validate 回调最前面，先于未
    改动的 ``_validate_beat_sheet_draft``（见模块 docstring 的顺序说明）。"""
    _revert_disallowed_lines(draft, quotes, source_segments, allowed_quote_ids=allowed_quote_ids)
    _clip_spans_to_allowed_units(draft, source_segments, allowed_span_units=allowed_span_units)


def _second_pass_task_payload(
    *, episode_no: int, payload: dict[str, Any], segments: list[SourceSegment], paratext_indexes: set[int],
    context_indexes: set[int], dialogue_quotes: list[DialogueQuote], adaptation_mode: str, draft_cls: type[_AiBeatSheetDraft],
    must_keep: list[_DropReviewItem], candidates: list[_DropReviewItem],
) -> dict[str, Any]:
    task_payload = _beat_sheet_task_payload(
        episode_no=episode_no, payload=payload, segments=segments, paratext_indexes=paratext_indexes,
        context_indexes=context_indexes, dialogue_quotes=dialogue_quotes, adaptation_mode=adaptation_mode,
        draft_cls=draft_cls,
    )
    task_payload["rules"] = [*task_payload["rules"], _must_keep_rule(must_keep, candidates)]
    task_payload["must_keep_items"] = [_item_payload(it) for it in must_keep]
    task_payload["droppable_candidates"] = [_item_payload(it) for it in candidates]
    return task_payload


async def _generate_second_pass(
    *, episode_id: str, episode_no: int, segments: list[SourceSegment], payload: dict[str, Any],
    dialogue_quotes: list[DialogueQuote], contract_version: str, adaptation_mode: str,
    must_keep: list[_DropReviewItem], candidates: list[_DropReviewItem],
) -> tuple[_AiBeatSheetDraft, int | None]:
    paratext_indexes = _paratext_segment_indexes(payload)
    context_indexes = context_segment_indexes(payload)
    draft_cls = _beat_sheet_draft_cls(adaptation_mode)
    task_payload = _second_pass_task_payload(
        episode_no=episode_no, payload=payload, segments=segments, paratext_indexes=paratext_indexes,
        context_indexes=context_indexes, dialogue_quotes=dialogue_quotes, adaptation_mode=adaptation_mode,
        draft_cls=draft_cls, must_keep=must_keep, candidates=candidates,
    )
    fingerprint = hashlib.sha256(json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    soft_cap = _short_drama.SegmentCountSoftCap(
        adaptation_mode=adaptation_mode, retry_limit=_BEAT_SHEET_SEMANTIC_RETRY_LIMIT, quotes=dialogue_quotes, source_segments=segments,
    )
    budget_cap = _short_drama_budget.DialogueBudgetSoftCap(
        adaptation_mode=adaptation_mode, retry_limit=_BEAT_SHEET_SEMANTIC_RETRY_LIMIT, quotes=dialogue_quotes,
    )
    allowed_quote_ids, allowed_span_units = _allowed_sets(candidates)

    def _validate(value: Any) -> list[str]:
        _enforce_second_pass_constraints(
            value, dialogue_quotes, segments, allowed_quote_ids=allowed_quote_ids, allowed_span_units=allowed_span_units,
        )
        return [
            *_validate_beat_sheet_draft(
                value, source_segments=segments, dialogue_quotes=dialogue_quotes,
                context_indexes=context_indexes, paratext_indexes=paratext_indexes, adaptation_mode=adaptation_mode,
            ),
            *soft_cap.errors(value), *budget_cap.errors(value),
        ]

    draft = await model_gateway.chat_structured(
        [
            {"role": "system", "content": "你是短剧分镜师。只输出符合 Schema 的一个 JSON 对象，不输出 Markdown或解释。"},
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
        ],
        model_type=draft_cls, validate=_validate, normalize_payload=_normalize_beat_sheet_payload,
        operation_id=f"storyboard_pack_beat_sheet_second_pass_{episode_id}_{fingerprint}",
        max_tokens=6000, format_retry_limit=1, semantic_retry_limit=_BEAT_SHEET_SEMANTIC_RETRY_LIMIT, temperature=0.4,
        call_meta={
            "stage_key": "storyboard_pack_beat_sheet",
            "call_role": "storyboard_beat_sheet_second_pass",
            "initiator_label": "分镜台节拍表复核后重排",
            "episode_id": episode_id,
            "contract_version": contract_version,
        },
        repair_context=storyboard_repair_context(task_payload), format_repair_context=storyboard_repair_context(task_payload),
    )
    return draft, soft_cap.last_projected_count


def _must_keep_record(item: _DropReviewItem, evidence_quote: str) -> dict[str, Any]:
    text = item.text if len(item.text) <= _EXCERPT_MAX_CHARS else f"{item.text[:_EXCERPT_MAX_CHARS]}…"
    return {"item_id": item.item_id, "kind": item.kind, "text": text, "evidence_quote": evidence_quote}


async def generate_beat_sheet_with_drop_review(
    *, episode_id: str, episode_no: int, segments: list[SourceSegment], payload: dict[str, Any],
    dialogue_quotes: list[DialogueQuote], contract_version: str, adaptation_mode: str,
) -> tuple[_AiBeatSheetDraft, int | None, dict[str, Any]]:
    """``generate_storyboard_pack`` 的唯一接线点：第一遍 -> （按需）复核 ->
    （按需）第二遍，见模块 docstring。忠实档、或短剧档没有任何删减时，
    第二、三步都是无副作用的空操作——只调用未改动的 ``_generate_beat_sheet``
    一次，行为与改造前逐字节相同（忠实档指纹冻结测试见 tests/test_
    storyboard_short_drama_schemas.py，本模块不改变它的前提）。
    """
    beat_draft, projected_segment_count = await _generate_beat_sheet(
        episode_id=episode_id, episode_no=episode_no, segments=segments, payload=payload,
        dialogue_quotes=dialogue_quotes, contract_version=contract_version, adaptation_mode=adaptation_mode,
    )
    if adaptation_mode != "short_drama":
        return beat_draft, projected_segment_count, _skipped_review()
    items = _collect_review_items(beat_draft, dialogue_quotes, segments)
    if not items:
        return beat_draft, projected_segment_count, _skipped_review()
    resolved = await _run_drop_review(
        episode_id=episode_id, contract_version=contract_version, items=items, source_segments=segments,
    )
    if resolved is None:
        return beat_draft, projected_segment_count, {
            "status": "failed", "reviewed_count": len(items), "must_keep": [], "second_pass": False,
        }
    must_keep_pairs, candidates, notes = resolved
    for note in notes:
        _LOGGER.info("[STORYBOARD_SHORT_DRAMA_REVIEW] %s", note)
    if not must_keep_pairs:
        return beat_draft, projected_segment_count, {
            "status": "ok", "reviewed_count": len(items), "must_keep": [], "second_pass": False,
        }
    must_keep_items = [item for item, _quote in must_keep_pairs]
    beat_draft2, projected2 = await _generate_second_pass(
        episode_id=episode_id, episode_no=episode_no, segments=segments, payload=payload,
        dialogue_quotes=dialogue_quotes, contract_version=contract_version, adaptation_mode=adaptation_mode,
        must_keep=must_keep_items, candidates=candidates,
    )
    return beat_draft2, projected2, {
        "status": "ok", "reviewed_count": len(items), "second_pass": True,
        "must_keep": [_must_keep_record(item, quote) for item, quote in must_keep_pairs],
    }
