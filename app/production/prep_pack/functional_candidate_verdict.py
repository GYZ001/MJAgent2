"""Functional-label candidate verdict: the model call that picks (or rejects)
a candidate identity for an unresolved functional label, quote-pinned
against its dossier, and the resulting extra-candidate resolution.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.schemas import Bible
from app.source_excerpt import SourceSegment
from pydantic import (
    BaseModel,
    ConfigDict,
)
from typing import Any

from .alias_resolution import _prep_pack_cross_episode_alias_conflict
from .asset_lookup import _resolve_portrait_id
from .functional_candidates import (
    _PREP_PACK_FUNCTIONAL_CANDIDATE_NO_MATCH_LABEL,
    _prep_pack_functional_candidate_dossier,
    _prep_pack_functional_candidate_label_segments,
    _prep_pack_functional_candidate_names,
    _prep_pack_functional_candidate_roster,
)


class _PrepPackFunctionalCandidateVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 候选判别（见本节顶部大注释）：不是"标签是不是候选 A"的是非题，而是在
    # 候选集（本集出场的全部人物谱角色 + "都不是/无法确定"）里选一个。schema
    # 层面用 enum 收紧到 _prep_pack_functional_candidate_call 构造的候选集
    # （与段号 enum 同一写法，参照 app/portraits.py _current_identity_
    # schema() 给 evidence_ref 注入 enum 的写法）。
    selected_candidate: str
    # 钉证判据（见本节顶部大注释）：模型只需引用卷宗目录里某一条的段号，不
    # 要求逐字复述原文。schema 层面用 enum 把候选值限定为本次卷宗实际收录
    # 的段号集合，代码层面 _prep_pack_functional_candidate_pin_segment 再做
    # 一次结构性核验。
    supporting_segment_index: int
    # 可选的观测字段，供人工复核参考，不作为通过与否的判据。
    supporting_quote: str = ""


async def _prep_pack_functional_candidate_call(
    *, label: str, dossier: list[dict[str, Any]], candidates: list[str],
    episode_id: str, project_id: str | None,
) -> _PrepPackFunctionalCandidateVerdict:
    """唯一一次模型调用：只给卷宗原文与候选人名单，不点名"你猜是不是某个
    候选"——把"这个标签到底指候选里的哪一位"完全交给模型自己独立判别，
    与 app.stages._alias_verdict_call 同一范式（本文件独立实现，两个模块
    不互相导入内部函数）。

    候选集单一来源（1.8.4 回退，见 PREP_PACK_VERSION 上方 1.8.4 大注释）：
    ``candidates`` 只是"规范名或已确认别名在本集原文逐字出现"这一甲类
    判据的结果，不再有分区展示的乙类（人物谱注册区间覆盖本集但原文未
    点名）——那一类候选天然没有本集原文里的锚点段落，"钉证仍须钉住真实
    卷宗段落"这道保险对它们原理上不成立（卷宗里没有它的锚点段，模型只能
    钉在任意一段无关证据上，钉证因此只证明"这段话真实存在"，证明不了
    "这段话支持这个指代关系"），真实数据已经出现赵武刚（人物谱登记本集
    活跃，但原文一次都没提到他）被误判为"绿袍男子"的事故（method=
    candidate_verdict）。"""
    catalog = "\n\n".join(
        f"[段{item['segment_index']}] {item['text']}" for item in dossier
    )
    segment_indexes = [item["segment_index"] for item in dossier]
    candidate_options = [*candidates, _PREP_PACK_FUNCTIONAL_CANDIDATE_NO_MATCH_LABEL]
    candidate_list = "、".join(candidates)
    prompt = f"""下面是本集原文中的段落（含前后语境，出现顺序不代表任何推断结论），
每段前面标了段号：
{catalog}

本集出场的人物谱角色候选（判别范围仅限这些人，不要引入候选之外的人）：
{candidate_list}

任务：仅依据以上原文段落本身，判断标签"{label}"最可能指候选中的哪一位本人。
- selected_candidate 必须从候选列表中选一个精确姓名，或者在证据不足以确定具体是谁时
  选"{_PREP_PACK_FUNCTIONAL_CANDIDATE_NO_MATCH_LABEL}"；不要因为某个候选在段落里出现
  次数多就倾向选他，只依据原文是否真的能确定"{label}"说的就是他本人；
- supporting_segment_index 必须填上面某一段落标注的段号（取值只能是 {segment_indexes}
  之一），选你得出这个结论最主要依据的那一段，不要凭空填一个没在目录里出现的段号；
- supporting_quote 可选，若填写请给该段里的一句原文摘录供人工复核参考，不要求逐字
  精确，留空也可以。
只输出符合 Schema 的 JSON。"""
    schema = _PrepPackFunctionalCandidateVerdict.model_json_schema()
    # 参照 app/portraits.py _current_identity_schema() 给 evidence_ref 注入
    # enum 的写法：候选段号、候选人名单都收紧到本次实际可用的集合，模型在
    # 协议层面就选不出卷宗外的段号或候选集之外的人；真正生效的核验仍在
    # _prep_pack_functional_candidate_pin_segment 与
    # _prep_pack_resolve_functional_extra_candidate 里做代码侧结构校验
    # （provider 对 enum 的遵守不是可证明保证）。
    schema["properties"]["supporting_segment_index"]["enum"] = segment_indexes
    schema["properties"]["selected_candidate"]["enum"] = candidate_options
    operation_id = (
        f"episode_prep_pack:{episode_id}:functional_extra_candidate_verdict:"
        + evidence_repository.content_hash({
            "label": label, "candidates": candidates,
            "dossier": [item["segment_index"] for item in dossier],
        })
    )
    return await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_PrepPackFunctionalCandidateVerdict,
        validate=None,
        operation_id=operation_id,
        max_tokens=500,
        # 低温：这道闸的语义判断要稳定——同一份卷宗重跑不该一次选中一次
        # 不确定（跟 stages.py 同一考量）。
        temperature=0.0,
        format_retry_limit=1,
        semantic_retry_limit=1,
        output_schema=schema,
        call_meta={
            "stage": "未解析角色候选判别",
            "stage_key": "episode_prep_pack_functional_extra_candidate_verdict",
            "call_role": "stage_generate",
            "call_role_label": "未解析角色候选判别",
            "expected_json": True,
            "project_id": project_id,
            "episode_id": episode_id,
            "label": label,
            "candidates": candidates,
        },
    )


def _prep_pack_functional_candidate_pin_segment(
    dossier: list[dict[str, Any]], segment_index: Any,
) -> dict[str, Any] | None:
    """钉证：结构性校验，不要求模型逐字复述原文（见本节顶部大注释）。模型
    只需要在响应里选一个段号，这里核对该段号是否落在本次卷宗实际收录的
    段号集合内——命中即视为钉证通过，因为卷宗内容本身就是代码检索出的
    真实原文，模型选中某一条不存在"编造"或"转录出错"的空间。非法输入
    （不是整数、或不在集合内）一律返回 None，交由调用方按无效裁决拒绝。"""
    try:
        target = int(segment_index)
    except (TypeError, ValueError):
        return None
    for item in dossier:
        if item["segment_index"] == target:
            return item
    return None


async def _prep_pack_resolve_functional_extra_candidate(
    conn, *, project_id: str, episode_id: str, episode_no: int,
    label: str, source_text: str, segments: list[SourceSegment], bible: Bible,
    character_mentions: list[dict[str, Any]],
) -> dict[str, Any]:
    """未解析标签的候选判别入口：候选集为空、卷宗为空、模型选"都不是/
    无法确定"、选中值不在候选集内（协议层已经不可能，这里仍防御性核验）、
    段号钉证失败、候选在本集没有可用定妆照、或这次改名会与跨集别名注册表
    冲突（复用既有 _prep_pack_cross_episode_alias_conflict，同一套"不确定
    不绑"纪律），一律 ``resolved=False``——调用方维持原行为，标签留在
    skip_character_names 正常落 functional_extras，绝不猜。

    ``character_mentions``（2.0.0，取代 1.8.1 引入的 ``events`` 参数——
    调用方 _resolve_assets 自己的扁平提及列表原样传入）：用于
    _prep_pack_functional_candidate_label_segments 算出这个标签自己申报
    （且已逐段核验）的段落，作为卷宗检索的主锚点——见该函数与
    _prep_pack_functional_candidate_dossier 的完整根因说明（标签字面定位
    在"标签是模型转述短语"时会打空，提及自报的段号不依赖字面命中）。

    返回值恒为 dict（1.10.0 起不再用 ``None`` 表示失败，见 PREP_PACK_
    VERSION 上方大注释"顺带修一处可观测性缺口"一节）：``resolved`` 是否
    真的绑定成功；``attempted`` 是否真的发起过一次候选判别模型调用（候选集
    非空且卷宗非空才会调用模型——调用方据此区分"从未获得候选判别机会"与
    "候选判别跑过但没选中"两种此前坍缩成同一个 method="discovery" 值、
    只能翻 provider_calls 反推的情形，见 _pass 对 functional_extras 的
    provenance.candidate_verdict_attempted 处理）。``resolved=True`` 时
    额外带 ``canonical_name``/``segment_index``/``text``：``canonical_name``
    供调用方写入 character_rename（重新走既有的具名解析路线，自然带出正确
    的 portrait_id/identity_id/visual_entity_id）；``segment_index``/
    ``text`` 是钉证命中的卷宗证据，供调用方写入 provenance 锚点（``text``
    是代码检索出的真实原文，不是模型转录，天然满足自校验的逐字命中要求）。

    候选集单一来源（1.8.4 回退，见 PREP_PACK_VERSION 上方 1.8.4 大注释）：
    ``candidates``＝规范名或已确认别名在本集原文逐字出现的人物谱角色（见
    _prep_pack_functional_candidate_names）。1.8.3 曾短暂扩展为"逐字命中∪
    人物谱注册区间覆盖本集"两类并集，已回退——乙类候选没有本集原文里的
    锚点段落，"钉证仍须钉住真实卷宗段落"这道保险对它们原理上不成立（卷宗
    里根本不存在它的锚点段，模型只能钉在任意一段无关证据上，钉证只能证明
    "这段话真实存在"，证明不了"这段话支持这个指代关系"），真实数据已经
    出现赵武刚（人物谱登记本集活跃，原文一次都没提到他）被误判为"绿袍
    男子"的事故（method=candidate_verdict）。"""
    not_attempted = {"resolved": False, "attempted": False}
    attempted_no_bind = {"resolved": False, "attempted": True}
    roster = _prep_pack_functional_candidate_roster(bible)
    candidates = _prep_pack_functional_candidate_names(source_text, roster)
    if not candidates:
        return not_attempted
    # 1.8.2：改传"候选名 -> 该候选自己的锚点文本"分组字典（而非拍平成一个
    # 集合），供 _prep_pack_functional_candidate_dossier 的 B 侧按候选做
    # 公平轮转合并，见该函数与 _prep_pack_functional_candidate_anchor_pool
    # 的完整说明。字典按 candidates 既有确定性顺序构造（保序，见
    # _prep_pack_functional_candidate_names 的 roster 保序说明）。
    candidate_anchor_texts = {name: roster.get(name, []) for name in candidates}
    event_span_segments = _prep_pack_functional_candidate_label_segments(
        character_mentions, label,
    )
    dossier = _prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts, event_span_segments,
    )
    if not dossier:
        return not_attempted
    response = await _prep_pack_functional_candidate_call(
        label=label, dossier=dossier, candidates=candidates,
        episode_id=episode_id, project_id=project_id,
    )
    if response.selected_candidate not in candidates:
        return attempted_no_bind
    pinned = _prep_pack_functional_candidate_pin_segment(
        dossier, response.supporting_segment_index,
    )
    if pinned is None:
        return attempted_no_bind
    canonical_name = response.selected_candidate
    if not _resolve_portrait_id(conn, project_id, canonical_name, episode_no):
        return attempted_no_bind
    conflicting_name = _prep_pack_cross_episode_alias_conflict(
        conn, project_id, episode_id,
        alias=label, canonical_name=canonical_name, bible=bible,
    )
    if conflicting_name:
        return attempted_no_bind
    return {
        "resolved": True, "attempted": True,
        "canonical_name": canonical_name,
        "segment_index": pinned["segment_index"], "text": pinned["text"],
    }


