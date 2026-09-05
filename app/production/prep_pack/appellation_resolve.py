"""叙述向称谓归属（WS2-A）：代词/年龄称谓/身份称谓/集体称谓的身份判定。

背景：``_resolve_assets`` 的既有两遍解析（``_pass()``）只处理"画面里真正出场"
的角色提及——这个判据由更早的一次模型调用（``_extract_chunk``）负责申报，
且它的定义严格排除"被叙述/回忆/自述提及，但原文本身从未描写这个人出现在
画面里"的情形。真实案例：跑不快的孩子 ep2（proj_ce9fcf749b23/
ep_b070f72e369a）整段是球员的第一人称自述回顾（"我八岁的时候……我三十五岁，
在卡塔尔的夜里"），该次模型调用如实判定"没有任何角色真正出场"（
characters=[]，这是它在自己严格定义下的正确答案，不是抽取遗漏）——于是
asset_manifest.characters/appellation_map 全空，即使人物谱里"里奥"已登记、
已有定妆照，一张都用不上；shots.characters 里"少年/球员/八岁男孩"这些原文
称谓因此永远没有机会被归到里奥身上。

本模块是一个独立、可加的第三条通路：不依赖 character_mentions 是否为空，
直接对本集原文分段做一次"这段/相邻段落里，有没有称谓/代词/描述短语指代
人物谱已登记的某个人——不论他是否在画面中出场"的归属判定，判据是正面陈述
且候选身份只能来自人物谱名单（模型不得引入候选之外的人），能确定是谁必须
给出本段原文的逐字证据（代码侧再核验一遍是否真的逐字出现在原文里，不信任
模型的结构性声明）；集体称谓（"众猴""百姓们"）标记为 collective，证据不足
一律 unresolved——不猜、不因为候选只有一个人就默认填他。

不改变、不重跑既有两遍解析：本模块只在两遍解析全部完成之后，把它的判定
结果合并进同一份 ``characters``/``functional_extras``/
``character_appellation_rows``（与主解析用完全相同的合并语义——
``setdefault`` 建条目、``segment_indexes`` 取并集、``aliases`` 记录本集内
出现过的其它称谓），因此对已经工作正常的分集零回归：主解析已经解析出的
条目只会被"追加更多 segment_indexes/aliases"，不会被覆盖或删除。

层号：随 ``app.production.prep_pack`` 包前缀归 L4（app/LAYERS.toml）。
"""
from __future__ import annotations

from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.identity_authority import visual_entity_id_for_resolution
from app.schemas import is_narrator_label
from pydantic import BaseModel, ConfigDict
from typing import Any

from .appellation_response_repair import repair_appellation_payload
from .asset_lookup import _resolve_portrait_id
from .chunking import _chunk_segments
from .provenance import _prep_pack_provenance

COLLECTIVE = "collective"
UNRESOLVED = "unresolved"
APPELLATION_RESOLUTION_METHOD = "appellation_resolution"


class _AppellationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_label: str
    identity: str
    evidence: str = ""
    segment_indexes: list[int] = []


class _AppellationResolutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    appellations: list[_AppellationVerdict] = []


def _appellation_resolution_prompt(
    *, catalog: str, candidate_list: str, segment_indexes: list[int],
) -> str:
    return f"""下面是本集原文中的段落（按顺序，出现顺序不代表任何推断结论），每段前面标了段号：
{catalog}

本集人物谱已登记角色（候选范围仅限这些人，不要引入候选之外的人）：
{candidate_list}

任务：找出以上段落里所有指代某个人物、但字面上不是人物谱正名的称谓——代词
（他/她/我/你）、年龄称谓（八岁男孩/少年）、身份称谓（球员/官员/老人）、
集体称谓（众猴/百姓们）等。这个人物是否在画面中出场、还是只被叙述/回忆/
自述提及，都要一并申报，不要因为原文只是第一人称自述或旁白转述就跳过。

对每一条申报：
- raw_label 必须逐字使用原文里出现的这个称谓/代词/描述短语本身；
- identity 三选一：
  1) 依据本段及相邻段落原文本身，能确定这个称谓指的就是候选名单中的某一位
     本人——填该候选的精确姓名，并在 evidence 里逐字摘录原文中能证明这一点
     的一段（不超过约80字，不得改写/概括/编造，必须是能把这个称谓与候选
     本人对上号的直接原文依据，例如同一段自述里出现的年龄/经历与人物谱
     已知背景吻合）；
  2) 原文本身明确是复数/集体，天然不指向某一个具体的人（例如"众猴"
     "百姓们"）——identity 填"{COLLECTIVE}"，evidence 留空；
  3) 证据不足以确定具体是谁——包括"这个称谓像是某个人但原文没有给出可以
     逐字对上号的依据"——identity 必须填"{UNRESOLVED}"，不得因为候选名单
     只有一个人就默认填他，不得猜测；
- segment_indexes 必须是这个称谓在上面目录中实际出现的段号（只能取
  {segment_indexes} 中的值），不要填目录之外的段号；
- 旁白/叙述者讲述这件事这个事实本身不需要申报——只申报原文里被称呼/描述的
  那个"人"，不要把旁白自己列为一条 raw_label。

没有任何符合条件的称谓就返回空列表，不要为了填满而虚构。只输出符合 Schema 的 JSON。"""


async def _appellation_resolution_call(
    *, dossier: list[dict[str, Any]], candidates: list[str],
    episode_id: str, project_id: str | None,
) -> _AppellationResolutionResponse:
    catalog = "\n\n".join(f"[段{item['segment_index']}] {item['text']}" for item in dossier)
    segment_indexes = [item["segment_index"] for item in dossier]
    prompt = _appellation_resolution_prompt(
        catalog=catalog, candidate_list="、".join(candidates), segment_indexes=segment_indexes,
    )
    schema = _AppellationResolutionResponse.model_json_schema()
    verdict_props = schema["$defs"]["_AppellationVerdict"]["properties"]
    verdict_props["identity"]["enum"] = [*candidates, COLLECTIVE, UNRESOLVED]
    verdict_props["segment_indexes"]["items"]["enum"] = segment_indexes
    operation_id = (
        f"episode_prep_pack:{episode_id}:appellation_resolution:"
        + evidence_repository.content_hash({"candidates": candidates, "segments": segment_indexes})
    )
    return await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_AppellationResolutionResponse,
        validate=None,
        operation_id=operation_id,
        max_tokens=2000,
        temperature=0.0,
        format_retry_limit=1,
        semantic_retry_limit=1,
        output_schema=schema,
        normalize_payload=repair_appellation_payload,
        call_meta={
            "stage": "叙述向称谓归属",
            "stage_key": "episode_prep_pack_appellation_resolution",
            "call_role": "stage_generate",
            "call_role_label": "叙述向称谓归属",
            "expected_json": True,
            "project_id": project_id,
            "episode_id": episode_id,
            "candidates": candidates,
        },
    )


def _apply_named_verdict(
    verdict: _AppellationVerdict, *, conn, project_id: str, episode_no: int,
    characters: dict[str, Any], character_appellation_rows: list[dict[str, Any]],
) -> None:
    identity = verdict.identity
    portrait_id = _resolve_portrait_id(conn, project_id, identity, episode_no)
    entry = characters.setdefault(portrait_id or f"bible:{identity}", {
        "identity_id": f"bible:{identity}",
        "display_name": identity,
        "portrait_id": portrait_id,
        "segment_indexes": [],
        "aliases": [],
        "visual_entity_id": visual_entity_id_for_resolution({
            "resolution": "future_identity", "canonical_name": identity,
        }),
        "display_appellation": verdict.raw_label,
        "provenance": _prep_pack_provenance(
            APPELLATION_RESOLUTION_METHOD, verdict.segment_indexes, verdict.evidence,
        ),
    })
    entry["segment_indexes"] = sorted(set(entry["segment_indexes"]) | set(verdict.segment_indexes))
    if verdict.raw_label not in entry["aliases"] and verdict.raw_label != entry["display_name"]:
        entry["aliases"].append(verdict.raw_label)
    character_appellation_rows.append({
        "raw_mention": verdict.raw_label,
        "segment_indexes": list(verdict.segment_indexes),
        "identity_id": entry["identity_id"],
        "canonical_appellation": entry["display_name"],
    })


def _apply_unresolved_verdict(
    verdict: _AppellationVerdict, *, functional_extras: dict[str, Any],
) -> None:
    # collective/unresolved 都落 functional_extras（缺陷2的展示端要求：unresolved
    # 必须带 label 与 visual_entity_id，见本模块 docstring）；collective 在
    # provenance 上多标一个 collective=True，供消费方区分"这是一群人不是某个人"
    # 与"这是某个人但没能确定是谁"——两者在界面上应该有不同的措辞。
    extra = functional_extras.setdefault(verdict.raw_label, {
        "segment_indexes": [],
        "visual_entity_id": visual_entity_id_for_resolution({
            "source_label": verdict.raw_label, "scope_qualifier": "",
        }),
        "provenance": _prep_pack_provenance(
            APPELLATION_RESOLUTION_METHOD, verdict.segment_indexes, "",
            candidate_verdict_attempted=(verdict.identity == COLLECTIVE),
        ),
    })
    extra["segment_indexes"] = sorted(set(extra["segment_indexes"]) | set(verdict.segment_indexes))
    if verdict.identity == COLLECTIVE:
        extra["provenance"]["collective"] = True


def _verified_verdicts(
    response: _AppellationResolutionResponse, *, candidates: set[str], source_text: str,
    valid_segment_indexes: set[int],
) -> list[_AppellationVerdict]:
    """代码侧结构核验：模型 enum 遵守不是可证明保证（同类既有闸门口径，见
    functional_candidate_verdict.py）。raw_label 为空/是旁白、segment_indexes
    越界或为空、以及"声称是候选本人但证据不是原文逐字子串"，一律拒绝——
    拒绝的条目按 identity=unresolved 处理，不静默丢弃、也不假装通过。"""
    verified: list[_AppellationVerdict] = []
    for item in response.appellations:
        raw_label = item.raw_label.strip()
        if not raw_label or is_narrator_label(raw_label):
            continue
        segment_indexes = sorted({i for i in item.segment_indexes if i in valid_segment_indexes})
        if not segment_indexes:
            continue
        identity = item.identity.strip()
        evidence = item.evidence.strip()
        if identity in candidates:
            if not evidence or evidence not in source_text:
                identity = UNRESOLVED
        elif identity != COLLECTIVE:
            identity = UNRESOLVED
        verified.append(_AppellationVerdict(
            raw_label=raw_label, identity=identity, evidence=evidence,
            segment_indexes=segment_indexes,
        ))
    return verified


async def resolve_narration_appellations(
    conn, project_id: str, episode_id: str, episode_no: int, source_text: str,
    bible: Any, segments: list[Any], characters: dict[str, Any],
    functional_extras: dict[str, Any], character_appellation_rows: list[dict[str, Any]],
) -> None:
    """就地把叙述向称谓归属结果合并进主解析已经在维护的三份结构。候选集为
    空（项目还没有人物谱角色）直接跳过，不发起任何模型调用（同
    functional_candidate_verdict.py 的既有口径）。"""
    candidate_names = [
        name for character in getattr(bible, "characters", None) or []
        if (name := str(getattr(character, "name", "") or "").strip())
    ]
    if not candidate_names or not segments:
        return
    candidates_set = set(candidate_names)
    for chunk in _chunk_segments(segments):
        dossier = [{"segment_index": index, "text": segment.text} for index, segment in chunk]
        valid_segment_indexes = {index for index, _ in chunk}
        response = await _appellation_resolution_call(
            dossier=dossier, candidates=candidate_names,
            episode_id=episode_id, project_id=project_id,
        )
        for verdict in _verified_verdicts(
            response, candidates=candidates_set, source_text=source_text,
            valid_segment_indexes=valid_segment_indexes,
        ):
            if verdict.identity in candidates_set:
                _apply_named_verdict(
                    verdict, conn=conn, project_id=project_id, episode_no=episode_no,
                    characters=characters, character_appellation_rows=character_appellation_rows,
                )
            else:
                _apply_unresolved_verdict(verdict, functional_extras=functional_extras)


__all__ = ["resolve_narration_appellations", "COLLECTIVE", "UNRESOLVED"]
