"""全集服装表：同场可见状态变化复核（2026-10-02）。

背景（B 隔离沙箱真实回归，proj_ca86b15ab7d7《顾念长安》EP1，ep_a3c61162b4ce，
连跑 5 次）：阶段一 ``wardrobe_plan_beat_sheet_rules`` 第三条已经要求模型——原文
明写某个人物身上衣物/配饰的可见状态变化时，即使同一场戏也要追加一条
``wardrobe_plan`` 记录。规则写进长提示词后 5/5 次仍没有登记「顾屿看着她瞬间
发白的脸色，没多问，只是站起身，先替她把外套的扣子扣好，才说要陪她过去看看。」
这一句；「把自己的围巾解下来绕在她脖子上」类句子同样多数漏记，即使节拍摘要里
已经写明了这个动作。写进"阶段一要同时产出节拍表/分段/对白台账/服装表/道具表/
体貌锚点……"这条长调用的规则，会被同一次调用里的其它职责挤掉注意力——这正是
CLAUDE.md「Prompts」一节「单次长调用会漏掉整个类别」同一形状，本仓库已有两处
成立的修法先例：``app.production.prep_pack.scene_recheck``/``prop_recheck``
（映射台「只问场景/道具」的复核调用）与同包内的
``storyboard_short_drama_review``（分镜台删减复核，``stage_key=
storyboard_pack_drop_review``）——单独开一次只问一件事的独立调用，模型提名、
代码核验，核验通过的结果再并入阶段一草稿，不跟别的职责抢注意力。

与 drop review 的一处刻意不同：drop review 的复核调用 ``validate=None``，
先拿到模型原始输出，再在代码里逐条拆分 must_keep/droppable（不合格的直接按
droppable 处理，不触发语义重试）——那是因为"复核结论本身是语义判断"，代码
拆不出"对不对"，只能拆"引用是否逐字成立"。本模块的核验维度（identity_id 是否
在名单里、source_segment_index 是否落在某个节拍覆盖范围内、quote 是否逐字
出自该段原文、wardrobe_after 是否非空）全部是结构判据，可以直接当
``chat_structured`` 的 ``validate`` 回调使用：不合格就带着问题清单回去给模型
一次语义重试机会（复用 ``storyboard_beat_sheet._BEAT_SHEET_SEMANTIC_RETRY_
LIMIT``），而不是退化成"结构错了就丢掉这一条"。identity_id/source_segment_
index 的合法取值域额外收进 ``model_type`` 的动态 ``Literal``（同一手法见
``storyboard_short_drama_review._review_response_model``）——落在 schema 上
才会真正下发到供应商侧的 json_schema，从源头降低模型选错的概率，不是单靠事后
校验兜底。

复核调用重试耗尽（格式修复耗尽/供应商错误/语义重试耗尽）时，``_run_wardrobe_
recheck`` 吞掉异常只记告警，返回 ``None``；``recheck_wardrobe_mid_scene_
changes`` 据此原样保留 ``beat_draft.wardrobe_plan``、返回
``{"status": "failed", ...}``——这次复核增强是补漏，不是门禁：失败不能让整集
生成失败，与 drop review 失败时「按无必保项继续，``drop_review.status=
"failed"`` 如实记录」同一处置（见该模块 docstring）。

合并判据（CLAUDE.md「判据从数据推导，禁止关键词词表」）：代码从不检查某句话
是否包含「扣/解/围/脱」这类字面词——是否构成「衣物/配饰可见状态变化」完全是
``_RECHECK_TASK`` 提示词里向模型描述的语义判断，模型提名、代码只核验结构
（quote 是否逐字出自原文等）。合并本身也是结构操作：按 ``source_segment_
index`` 找到覆盖它的节拍（``_segment_to_beat``，与 ``beat_sheet`` 的声明顺序
一致，同一段号被多个节拍覆盖时取第一个声明的），该人物在这个节拍还没有
``wardrobe_plan`` 记录时才追加（``(identity_id, beat_id)`` 判重），已有记录
的不重复追加、只记一条 info 日志——不覆盖模型自己已经给出的更完整判断。

编排接入点：``generate_beat_sheet_with_wardrobe_recheck`` 是
``storyboard_pack.generate_storyboard_pack`` 的新唯一接线点，取代直接调用
``storyboard_short_drama_review.generate_beat_sheet_with_drop_review``——
先跑未改动的 ``_beat_sheet_with_drop_review``（节拍表 + 删减复核），再跑本模块
的服装表复核，最后覆盖体貌专用锚点（``storyboard_physical_anchor``，覆盖点
必须晚于本次复核可能新增的 ``wardrobe_plan`` 条目之后，但两者实际互不读写
对方字段，顺序只是把收尾动作收在同一个出口，不分散）。之所以新开一个顶层
入口而不是直接改 ``storyboard_short_drama_review.generate_beat_sheet_with_
drop_review`` 本体：那个文件已顶在 500 行硬顶（``check_file_conventions.py``），
一行都加不进去；本模块是新文件，行数预算充足。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.harness import model_gateway
from app.production import storyboard_physical_anchor as _physical_anchor
from app.production import storyboard_short_drama_review as _drop_review
from app.production.storyboard_beat_sheet import (
    _BEAT_SHEET_SEMANTIC_RETRY_LIMIT,
    _AiBeatSheetDraft,
    _paratext_segment_indexes,
    _source_block_for_prompt,
)
from app.production.storyboard_beat_sheet_schemas import _AiWardrobeState
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_repair_context import known_character_identities
from app.production.storyboard_wardrobe_plan import known_identity_ids
from app.source_excerpt import SourceSegment

log = logging.getLogger(__name__)

_RECHECK_TASK = (
    "你在复核全集服装表规划：另一次改写已经为每个人物规划了换装节点，但那次判断"
    "只覆盖「跨场景/跨情境边界」的整体换装，容易漏掉「同一场戏里原文明写的可见"
    "状态变化」——例如一句话写某个人物把外套的扣子扣好或解开、围上或摘下围巾、"
    "拉上或拉开拉链、挽起袖子、把外套脱下来搭在手臂上（这些只是举例，不是判据，"
    "任何原文明写的、人物身上衣物/配饰可见状态发生变化的句子都算，不要局限于"
    "举例里的具体动作）。你的任务只有一件事：逐句扫一遍下面的原文，找出所有这类"
    "句子，对每一句给出：这句话写的是谁（identity_id）、它出现在哪个原文段"
    "（source_segment_index）、从原文逐字摘录这句话本身（quote，不改写、不"
    "概括、不拼接其它句子），以及变化之后这个人物身上完整的可见着装"
    "（wardrobe_after——正面陈述，不是只写变了的那一小块，没变的部分也要带上，"
    "让它单独看也是一套完整装扮）。原文里没有这类句子时，changes 给空列表，不要"
    "为了填满而虚构；人物名单与节拍表仅供你核对身份与理解上下文，不是复核对象。"
)


def _skipped_recheck() -> dict[str, Any]:
    return {"status": "skipped", "changes_found": 0, "added_count": 0}


def _latest_wardrobe_by_identity(wardrobe_plan: list[Any]) -> dict[str, str]:
    """按声明顺序取每个人物"最近一条"着装文字——同 ``WardrobePlanState``
    「声明顺序」口径，只是这里要最后一条而不是第一条，供复核提示词展示上下文。"""
    latest: dict[str, str] = {}
    for state in wardrobe_plan:
        latest[state.identity_id] = state.wardrobe
    return latest


def _segment_to_beat(beat_sheet: list[Any]) -> dict[int, Any]:
    """原文段号 -> 覆盖它的第一个节拍（按 ``beat_sheet`` 声明顺序，同一段号被
    多个节拍覆盖时取先声明的那个，供合并时反查 beat_id 用）。"""
    mapping: dict[int, Any] = {}
    for beat in beat_sheet:
        for index in beat.segment_indexes:
            mapping.setdefault(index, beat)
    return mapping


def _character_brief(payload: dict[str, Any], wardrobe_plan: list[Any]) -> list[dict[str, Any]]:
    latest = _latest_wardrobe_by_identity(wardrobe_plan)
    briefs: list[dict[str, Any]] = []
    for item in known_character_identities(payload):
        identity_id = str(item.get("identity_id") or "")
        if not identity_id:
            continue
        briefs.append({
            "identity_id": identity_id,
            "display_name": item.get("display_name") or "",
            "current_wardrobe": latest.get(identity_id) or "（本集服装表尚无记录）",
        })
    return briefs


def _beat_brief(beat_sheet: list[Any]) -> list[dict[str, Any]]:
    return [{"beat_id": beat.beat_id, "source_segment_indexes": list(beat.segment_indexes)} for beat in beat_sheet]


def _recheck_prompt(
    segments: list[SourceSegment], paratext_indexes: set[int],
    characters: list[dict[str, Any]], beats: list[dict[str, Any]],
) -> str:
    return (
        f"{_RECHECK_TASK}\n\n"
        f"人物名单（identity_id/显示名/当前服装表里该人物最近一条着装）："
        f"{json.dumps(characters, ensure_ascii=False)}\n\n"
        f"节拍表（beat_id 对应覆盖的原文段号）：{json.dumps(beats, ensure_ascii=False)}\n\n"
        f"原文：\n{_source_block_for_prompt(segments, paratext_indexes)}"
    )


def _recheck_response_model(identity_ids: list[str], segment_indexes: list[int]) -> type[BaseModel]:
    """合法 identity_id/source_segment_index 取值域用动态 Literal 钉死（同一
    手法见 ``storyboard_short_drama_review._review_response_model``）——落在
    ``model_type`` 上才会真正下发到供应商侧的 response_format.json_schema。"""
    mention = create_model(
        "_WardrobeChangeMention", __config__=ConfigDict(extra="forbid"),
        identity_id=(Literal[tuple(identity_ids)], ...),  # type: ignore[valid-type]
        source_segment_index=(Literal[tuple(segment_indexes)], ...),  # type: ignore[valid-type]
        quote=(str, Field(min_length=1)),
        wardrobe_after=(str, Field(min_length=1)),
    )
    return create_model(
        "_WardrobeRecheckResponse", __config__=ConfigDict(extra="forbid"), changes=(list[mention], ...),
    )


def _quote_is_verbatim(quote: str, segment_text: str) -> bool:
    """结构归一化空白后比较（去空白、不去标点，与 ``storyboard_wardrobe_plan.
    _wardrobe_grounded_in_appearance`` 同一口径）：quote 必须是该段原文的连续
    子串，不是关键词/语义相似度匹配。"""
    candidate = "".join(quote.split())
    source = "".join(segment_text.split())
    return bool(candidate) and candidate in source


def _validate_recheck_response(
    response: Any, *, identity_ids: set[str], segments_by_index: dict[int, str],
) -> list[str]:
    """``chat_structured`` 的 ``validate`` 回调：四条结构判据——identity_id 在
    名单里、source_segment_index 落在某个节拍覆盖范围内、quote 是该段原文的
    逐字子串、wardrobe_after 非空。前两条已经由动态 Literal 在 schema 层拦住，
    这里仍重复核验是防御性兜底（Literal 域本身就是从同一份数据算出来的，不是
    另一份口径）；quote/wardrobe_after 两条是跨字段判断，Literal 做不到，必须
    在这里核验。不合格时返回的问题清单会触发语义重试（见模块 docstring）。"""
    problems: list[str] = []
    for index, item in enumerate(response.changes):
        if item.identity_id not in identity_ids:
            problems.append(f"changes[{index}] identity_id「{item.identity_id}」不在本集人物名单里")
            continue
        segment_text = segments_by_index.get(item.source_segment_index)
        if segment_text is None:
            problems.append(f"changes[{index}] source_segment_index={item.source_segment_index} 不在任何节拍覆盖的原文段范围内")
            continue
        if not _quote_is_verbatim(item.quote, segment_text):
            problems.append(f"changes[{index}] quote「{item.quote}」不是第{item.source_segment_index}段原文的逐字子串")
        if not item.wardrobe_after.strip():
            problems.append(f"changes[{index}] wardrobe_after 不能是空白")
    return problems


async def _run_wardrobe_recheck(
    *, episode_id: str, contract_version: str, segments: list[SourceSegment], paratext_indexes: set[int],
    identity_ids: list[str], eligible_segment_indexes: list[int],
    characters: list[dict[str, Any]], beats: list[dict[str, Any]],
) -> list[Any] | None:
    """独立复核调用；失败（格式修复耗尽/语义重试耗尽/供应商错误等）返回
    ``None``，调用方据此原样保留服装表，不许让整集生成失败（见模块 docstring）。"""
    fingerprint = hashlib.sha256(
        json.dumps({"characters": characters, "beats": beats}, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    ).hexdigest()[:24]
    segments_by_index = {index: segments[index - 1].text for index in eligible_segment_indexes}
    try:
        response = await model_gateway.chat_structured(
            [
                {"role": "system", "content": "你是全集服装表的复核员。只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释。"},
                {"role": "user", "content": _recheck_prompt(segments, paratext_indexes, characters, beats)},
            ],
            model_type=_recheck_response_model(identity_ids, eligible_segment_indexes),
            validate=lambda value: _validate_recheck_response(
                value, identity_ids=set(identity_ids), segments_by_index=segments_by_index,
            ),
            operation_id=f"storyboard_pack_wardrobe_recheck_{episode_id}_{fingerprint}",
            max_tokens=4000,
            format_retry_limit=1,
            semantic_retry_limit=_BEAT_SHEET_SEMANTIC_RETRY_LIMIT,
            temperature=0.2,
            call_meta={
                "stage_key": "storyboard_pack_wardrobe_recheck",
                "call_role": "storyboard_wardrobe_recheck",
                "initiator_label": "分镜台服装表同场变化复核",
                "episode_id": episode_id,
                "contract_version": contract_version,
            },
        )
        return list(response.changes)
    except Exception:  # noqa: BLE001 -- 复核失败不许让整集失败，见模块 docstring
        log.warning(
            "[STORYBOARD_WARDROBE_RECHECK] 服装表同场变化复核调用失败，服装表保持不变 episode=%s",
            episode_id, exc_info=True,
        )
        return None


def _beat_for_segment_index(beat_sheet: list[Any], segment_index: int) -> Any | None:
    for beat in beat_sheet:
        if segment_index in beat.segment_indexes:
            return beat
    return None


def _merge_recheck_changes(beat_draft: Any, changes: list[Any]) -> int:
    """把核验通过的变化合并进 ``beat_draft.wardrobe_plan``；返回实际追加的
    条数（供可见信号统计）。合并逻辑见模块 docstring。"""
    existing = {(state.identity_id, state.beat_id) for state in beat_draft.wardrobe_plan}
    added = 0
    for item in changes:
        beat = _beat_for_segment_index(beat_draft.beat_sheet, item.source_segment_index)
        if beat is None:
            # 防御性兜底：Literal 域已经限定到节拍覆盖的段号，正常不会落到这里。
            log.warning(
                "[STORYBOARD_WARDROBE_RECHECK][未拦截] identity_id=「%s」source_segment_index=%s "
                "找不到覆盖它的节拍，跳过",
                item.identity_id, item.source_segment_index,
            )
            continue
        key = (item.identity_id, beat.beat_id)
        if key in existing:
            log.info(
                "[STORYBOARD_WARDROBE_RECHECK] identity_id=「%s」beat_id=「%s」已有服装表记录，"
                "复核补登的「%s」不重复追加",
                item.identity_id, beat.beat_id, item.quote,
            )
            continue
        beat_draft.wardrobe_plan.append(_AiWardrobeState(
            identity_id=item.identity_id, beat_id=beat.beat_id,
            wardrobe=item.wardrobe_after, change_reason=item.quote,
        ))
        existing.add(key)
        added += 1
    return added


async def recheck_wardrobe_mid_scene_changes(
    *, episode_id: str, contract_version: str, beat_draft: Any,
    segments: list[SourceSegment], payload: dict[str, Any],
) -> dict[str, Any]:
    """公开入口：就地把复核新发现的同场可见状态变化合并进
    ``beat_draft.wardrobe_plan``，返回留档用的三态 summary——
    ``{"status": "skipped"|"ok"|"failed", "changes_found": int, "added_count": int}``。
    没有已知人物、或没有任何节拍覆盖的非 paratext 段号时直接跳过，不发起调用
    （同 drop review「没有删减时不发起复核」的立场）。
    """
    paratext_indexes = _paratext_segment_indexes(payload)
    identity_ids = sorted(known_identity_ids(payload))
    segment_to_beat = _segment_to_beat(beat_draft.beat_sheet)
    eligible_segment_indexes = sorted(
        index for index in segment_to_beat
        if index not in paratext_indexes and 1 <= index <= len(segments)
    )
    if not identity_ids or not eligible_segment_indexes:
        return _skipped_recheck()
    characters = _character_brief(payload, beat_draft.wardrobe_plan)
    beats = _beat_brief(beat_draft.beat_sheet)
    changes = await _run_wardrobe_recheck(
        episode_id=episode_id, contract_version=contract_version, segments=segments,
        paratext_indexes=paratext_indexes, identity_ids=identity_ids,
        eligible_segment_indexes=eligible_segment_indexes, characters=characters, beats=beats,
    )
    if changes is None:
        return {"status": "failed", "changes_found": 0, "added_count": 0}
    added = _merge_recheck_changes(beat_draft, changes)
    return {"status": "ok", "changes_found": len(changes), "added_count": added}


async def generate_beat_sheet_with_wardrobe_recheck(
    *, episode_id: str, episode_no: int, segments: list[SourceSegment], payload: dict[str, Any],
    dialogue_quotes: list[DialogueQuote], contract_version: str, adaptation_mode: str,
) -> tuple[_AiBeatSheetDraft, int | None, dict[str, Any], dict[str, Any]]:
    """``storyboard_pack.generate_storyboard_pack`` 的唯一接线点（取代原来直接
    调用 ``storyboard_short_drama_review.generate_beat_sheet_with_drop_review``，
    见模块 docstring）：节拍表 + 删减复核 -> 服装表同场变化复核 -> 覆盖体貌
    专用锚点。返回 ``(beat_draft, projected_segment_count, drop_review,
    wardrobe_recheck)``，比原函数多一个 ``wardrobe_recheck`` summary。"""
    beat_draft, projected_segment_count, drop_review = await _drop_review._beat_sheet_with_drop_review(
        episode_id=episode_id, episode_no=episode_no, segments=segments, payload=payload,
        dialogue_quotes=dialogue_quotes, contract_version=contract_version, adaptation_mode=adaptation_mode,
    )
    wardrobe_recheck = await recheck_wardrobe_mid_scene_changes(
        episode_id=episode_id, contract_version=contract_version, beat_draft=beat_draft,
        segments=segments, payload=payload,
    )
    _physical_anchor.apply_physical_anchor_overrides(payload, _physical_anchor.build_physical_anchor_overrides(beat_draft, payload))
    return beat_draft, projected_segment_count, drop_review, wardrobe_recheck
