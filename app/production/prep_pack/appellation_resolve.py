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

设计变更（2026-09-30，PREP_PACK_VERSION 2.0.8）：identity 从三选一改为四选一，
新增 ``FUNCTIONAL``。根因是原三选一里的 unresolved 同时装着两种判定完全不同的
情形：①候选名单之外、原文明确写到的另一个具体的人（房东、摊主）——给一个实体
是诚实的；②看不清到底是谁、甚至可能就是候选名单里已经登记的某个人，只是原文
没有能逐字对上号的依据——给实体等于编造一个人（CLAUDE.md「不得兜底填充」）。
真实案例（B 机隔离沙箱，第2集 ep_7623b7b0a49a）：「温老师」（原文「陆一舟笑嘻嘻
地说："温老师，顺路来蹭个饭的路引子。"」，指的就是温念本人）、「他」「有人」都
被旧实现判成 unresolved，各自铸出一个独立群演 entity:...；「你俩」（指温念+
顾屿两位已登记角色）被判 collective，也铸了一个群演。第1集的"妈妈"与"温书棋"
（同一人）同样被拆成两个实体。下游 ``storyboard_pack._segment_relevant_assets``
把 functional_extras 交给分镜模型当本段合法出场身份，``storyboard_identity_
scope.scoped_identity_candidates``/``scoped_name_map`` 用它做台词说话人绑定——
一个"查不清是谁"的称谓被当成一个独立出镜的人，就是成片里"多出一个人/两个
同一人"的上游来源之一。

现在：①functional（原文明确写到候选之外的另一个具体的人）与 collective
（候选之外的一群人）仍然落 functional_extras，语义诚实——这两种都是"确实
存在的另一个人/一群人"；②unresolved（查不清、或可能是候选里已登记的人但
原文没有逐字依据）不再铸虚假实体，只记进 ``asset_manifest.unresolved_
appellations``（按 label 合并段号）供映射台界面人工核查，不进任何身份体系、
不参与分镜台的台词/出场判定。这推翻了本模块更早版本"缺陷2：unresolved 必须
带 label 与 visual_entity_id"的要求——可见性改由 unresolved_appellations 列表
+ 映射台界面承担，不再靠铸实体冒充"查清楚了、是另一个人"。

层号：随 ``app.production.prep_pack`` 包前缀归 L4（app/LAYERS.toml）。
"""
from __future__ import annotations

import logging
import re

from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.identity_authority import visual_entity_id_for_resolution
from app.schemas import is_narrator_label
from pydantic import BaseModel, ConfigDict
from typing import Any

from .appellation_response_repair import repair_appellation_payload
from .asset_lookup import _resolve_portrait_id
from .chunking import _chunk_segments
from .provenance import _prep_pack_locate_phrase, _prep_pack_provenance

COLLECTIVE = "collective"
FUNCTIONAL = "functional"
UNRESOLVED = "unresolved"
APPELLATION_RESOLUTION_METHOD = "appellation_resolution"

log = logging.getLogger(__name__)


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
- identity 四选一：
  1) 依据本段及相邻段落原文本身，能确定这个称谓指的就是候选名单中的某一位
     本人——填该候选的精确姓名，并在 evidence 里逐字摘录原文中能证明这一点
     的一段（不超过约80字，不得改写/概括/编造，必须是能把这个称谓与候选
     本人对上号的直接原文依据，例如同一段自述里出现的年龄/经历与人物谱
     已知背景吻合）；
  2) 原文明确写到候选名单之外的另一个具体的人（例如房东、摊主、陌生
     男子）——identity 填"{FUNCTIONAL}"，并在 evidence 里逐字摘录原文中
     能证明这是候选之外另一个具体的人的一段（不超过约80字，不得改写/
     拼接/概括，同第1条对证据的要求）；
  3) 原文明确是候选名单之外的一群人（例如众猴、百姓们、孩子们），天然
     不指向某一个具体的人——identity 填"{COLLECTIVE}"，evidence 留空；
     一个复数称谓如果指的就是候选名单里的几位本人（例如"你俩"指两位已
     登记角色），它不是集体称谓，按第4条申报；
  4) 原文证据不足以确定具体指谁——包括这个称谓可能就是候选名单里的某一位
     本人、但原文没有给出可以逐字对上号的依据，也包括代词（他/她/我/你）
     指代不明的情况——identity 必须填"{UNRESOLVED}"，不得因为候选名单
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
    verdict_props["identity"]["enum"] = [*candidates, FUNCTIONAL, COLLECTIVE, UNRESOLVED]
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


def _apply_functional_or_collective_verdict(
    verdict: _AppellationVerdict, *, functional_extras: dict[str, Any],
) -> None:
    """functional/collective 都落 functional_extras——两者都是"确实存在的、
    候选名单之外的另一个人/一群人"，给一个实体是诚实的（不是本模块 docstring
    "设计变更"一节推翻的那种 unresolved 兜底）。anchor_phrase 取
    ``verdict.evidence``：functional 已在 ``_verified_verdicts`` 里过了与具名
    分支同一套逐字核验，evidence 是核验通过后落库的那句原文；collective 按
    提示词要求 evidence 恒为空，anchor_phrase 因此仍是 ""，与此前行为一致。
    collective 在 provenance 上多标一个 collective=True，供消费方区分"这是
    一群人"与"这是候选之外的某一个具体的人"。
    """
    extra = functional_extras.setdefault(verdict.raw_label, {
        "segment_indexes": [],
        "visual_entity_id": visual_entity_id_for_resolution({
            "source_label": verdict.raw_label, "scope_qualifier": "",
        }),
        "provenance": _prep_pack_provenance(
            APPELLATION_RESOLUTION_METHOD, verdict.segment_indexes, verdict.evidence,
            candidate_verdict_attempted=(verdict.identity == COLLECTIVE),
        ),
    })
    extra["segment_indexes"] = sorted(set(extra["segment_indexes"]) | set(verdict.segment_indexes))
    if verdict.identity == COLLECTIVE:
        extra["provenance"]["collective"] = True


_UNRESOLVED_LOG_PREFIX = "[PREP_PACK_APPELLATION_UNRESOLVED][未拦截]"


def _record_unresolved_appellation(
    verdict: _AppellationVerdict, *, unresolved_by_label: dict[str, list[int]],
) -> None:
    """unresolved 不进 functional_extras、不铸 visual_entity_id（见本模块
    docstring"设计变更"一节）：原文证据不足以确定这是谁，甚至可能就是候选
    名单里已经登记的某个人，给一个独立群演等于把"查不清"伪造成"查清楚了、
    是另一个人"。按 raw_label 合并段号，写一条固定前缀 warning（供日志
    检索、同 discovery._prep_pack_record_unanchored_prop 的既有惯例），不
    阻断发布——调用方（resolve_narration_appellations）把合并结果转成列表
    交给 asset_manifest.unresolved_appellations，供映射台界面人工核查。
    """
    merged = sorted(set(unresolved_by_label.get(verdict.raw_label, [])) | set(verdict.segment_indexes))
    unresolved_by_label[verdict.raw_label] = merged
    log.warning(
        "%s 称谓「%s」在段落 %s 证据不足以确定具体是谁，不计入 functional_extras，请人工核查",
        _UNRESOLVED_LOG_PREFIX, verdict.raw_label, verdict.segment_indexes,
    )


def _verbatim_segments_for_label(
    raw_label: str, segment_indexes: list[int], segments: list[Any],
) -> list[int]:
    """unresolved/collective 判定没有可核验的 evidence 字段（collective 按提示词
    要求留空；unresolved 本身就是"证据不足"的结论），核验退回到 raw_label 本身：
    只保留 raw_label 逐字出现在它自己声明的那个段落原文里的段号——不做跨段
    搜索，不能用"raw_label 出现在别的段"证明"这段里有这个人"。与具名分支的
    ``_prep_pack_locate_phrase`` 同一纪律：不信任模型的结构性声明，代码侧再
    核验一遍是否真的逐字出现在原文里。

    真实故障：生产第2集"他"（segment_indexes=[5]，anchor_phrase=""）——第5段
    原文里根本没有一个独立的"他"字，这条声明没有任何逐字依据，却因为
    identity=unresolved 被既有实现直接跳过核验，原样进了 functional_extras。

    单字 raw_label（他/她/我/你/它……）额外走 ``_label_occurs_standalone``：
    纯子串包含会把"他们在村口说笑"里的"他"误判成独立出现过的"他"——复数
    后缀"们"把子串包含判断打穿，触发条件从"完全不出现"换成了"以复合词形式
    出现"，是同一类故障的另一个入口，必须同时堵上。
    """
    verified: list[int] = []
    for index in segment_indexes:
        if 1 <= index <= len(segments) and _label_occurs_standalone(
            raw_label, segments[index - 1].text,
        ):
            verified.append(index)
    return verified


def _label_occurs_standalone(raw_label: str, text: str) -> bool:
    """单字代词核验专用：排除"单字+们"这一个封闭的汉语复数后缀形态（他们/
    我们/你们/她们/它们）——这是语法规则（们作代词复数后缀），不是按具体
    称谓字面枚举的黑名单，对任意单字 raw_label 一视同仁。多字 raw_label
    （"老人""有人""众猴"）不受影响，仍按原有子串包含判断。

    已知局限：不覆盖"其他""自我"这类单字作为前缀复合词一部分出现的情形
    （如"其他人""自我介绍"）——排除这类需要真正的分词而本仓库未引入分词
    依赖，这里没有堵，逐字核验对这一类仍可能假阳性通过，见
    ``test_single_char_label_prefix_compound_is_a_known_gap``。
    """
    if len(raw_label) != 1:
        return raw_label in text
    return any(
        text[hit.end():hit.end() + 1] != "们"
        for hit in re.finditer(re.escape(raw_label), text)
    )


def _verified_verdicts(
    response: _AppellationResolutionResponse, *, candidates: set[str], source_text: str,
    valid_segment_indexes: set[int], segments: list[Any],
) -> list[_AppellationVerdict]:
    """代码侧结构核验：模型 enum 遵守不是可证明保证（同类既有闸门口径，见
    functional_candidate_verdict.py）。raw_label 为空/是旁白、segment_indexes
    越界或为空、以及"声称是候选本人但证据在原文里定位不到"，一律拒绝——
    拒绝的条目按 identity=unresolved 处理，不静默丢弃、也不假装通过。

    证据定位用 ``_prep_pack_locate_phrase``（与场景/真名引文同一原语）：引文两端的
    引号、收尾标点、跨段换行是引用格式不是内容，剥掉后仍须逐字连续命中；落库的
    evidence 换成原文里真实存在的形态。2026-09-14 第 13 集实测：模型判「大汉→曹阳」，
    证据「看到山下有一个大汉，正迈步临近公开区。“是曹阳……”」只差一个跨段换行和一个
    自补的收尾引号，原始子串比较把它打成 unresolved，曹阳在同一段里被拆成两个人。

    模型原始声明的 identity 落在 candidates 之外时分三种：functional 与具名
    分支同一套证据核验（``_prep_pack_locate_phrase``，定位不到就降级
    unresolved）；collective 按提示词要求 evidence 恒为空，没有证据可核验；
    unresolved 本身没有第三个证据要求。这三者（以及具名分支降级来的
    unresolved）都还要再过 raw_label 本身的核验（``_verbatim_segments_for_
    label``）：逐字找不到的段号剔除，全部段号都找不到就整条不发布
    （``continue``，不进 ``verified``）。这条只管"identity 原始声明就在候选
    人名之外"这一支——identity 命中候选人名、只是 evidence 定位失败被上面那
    段代码降级为 unresolved 的条目不重复受限，它已经过了自己那一套逐字核验
    （evidence），核验口径不重复加码。每次剔除都打一条 ``log.warning``，剔除
    不是静默发生的。
    """
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
            located, phrase = _prep_pack_locate_phrase(segments, evidence) if evidence else ([], "")
            if not located:
                identity = UNRESOLVED
            else:
                evidence = phrase
        else:
            if identity == FUNCTIONAL:
                located, phrase = _prep_pack_locate_phrase(segments, evidence) if evidence else ([], "")
                if located:
                    evidence = phrase
                else:
                    identity = UNRESOLVED
            elif identity != COLLECTIVE:
                identity = UNRESOLVED
            kept = _verbatim_segments_for_label(raw_label, segment_indexes, segments)
            if not kept:
                log.warning(
                    "叙述向称谓核验：「%s」声明的段落 %s 逐字核验全部未命中，整条不发布",
                    raw_label, segment_indexes,
                )
                continue
            if kept != segment_indexes:
                log.warning(
                    "叙述向称谓核验：「%s」声明的段落 %s 逐字核验未命中，已剔除仅保留 %s",
                    raw_label, sorted(set(segment_indexes) - set(kept)), kept,
                )
            segment_indexes = kept
        verified.append(_AppellationVerdict(
            raw_label=raw_label, identity=identity, evidence=evidence,
            segment_indexes=segment_indexes,
        ))
    return verified


_NAMING_CLAUSE = r"(?:他|她|此人|其人|那人|这人)?\s*(?:叫|名叫|名为|便是|正是|就是|乃是|唤作|唤做)\s*"


def _alias_contradicted_by_naming(
    verdict: _AppellationVerdict, candidates: set[str], texts_by_index: dict[int, str],
) -> bool:
    """「少年叹了口气，他叫孟浩」：原文在同一句里把这个称谓点名给了另一位候选，称谓就不能
    登记成 ``verdict.identity`` 的别名——别名一旦进人物谱，分镜台的台词账本会按别名表把整段
    戏判给错的人（我欲封天第 1 集：「少年」被登记为王有材别名，孟浩落榜独白全判给王有材，
    模型连原文「我孟浩」都改写成「我王有材」）。年龄/身份称谓在一章里常指不止一人，模型只
    按半山腰那处证据给了结论；这里用它申报的段落原文做结构复核。单字代词（他/她/我/你）
    天然逐段易主，不做此判定。"""
    label = verdict.raw_label
    if len(label) < 2:
        return False
    others = [name for name in candidates if name != verdict.identity]
    for index in verdict.segment_indexes:
        text = texts_by_index.get(index) or ""
        for hit in re.finditer(re.escape(label), text):
            sentence = re.split(r"[。！？\n]", text[hit.end():], maxsplit=1)[0]
            if any(re.search(_NAMING_CLAUSE + re.escape(other), sentence) for other in others):
                return True
    return False


async def resolve_narration_appellations(
    conn, project_id: str, episode_id: str, episode_no: int, source_text: str,
    bible: Any, segments: list[Any], characters: dict[str, Any],
    functional_extras: dict[str, Any], character_appellation_rows: list[dict[str, Any]],
    unresolved_appellations: list[dict[str, Any]] | None = None,
) -> None:
    """就地把叙述向称谓归属结果合并进主解析已经在维护的三份结构。候选集为
    空（项目还没有人物谱角色）直接跳过，不发起任何模型调用（同
    functional_candidate_verdict.py 的既有口径）。

    ``unresolved_appellations``（2026-09-30 出参，可选，默认 None——同
    ``_resolve_assets`` 的 ``appellation_resolutions``/``unanchored_prop_
    mentions`` 同一模式，保持既有调用点/测试签名不变）：本函数内部始终维护
    一份按 raw_label 合并段号的 unresolved 记录（``unresolved_by_label``），
    调用方传了列表才在函数末尾把合并结果追加进去；不传时这份记录只在函数
    内部生效，仍然确保 unresolved 不会混进 ``functional_extras``。
    """
    candidate_names = [
        name for character in getattr(bible, "characters", None) or []
        if (name := str(getattr(character, "name", "") or "").strip())
    ]
    if not candidate_names or not segments:
        return
    candidates_set = set(candidate_names)
    unresolved_by_label: dict[str, list[int]] = {}
    for chunk in _chunk_segments(segments):
        dossier = [{"segment_index": index, "text": segment.text} for index, segment in chunk]
        valid_segment_indexes = {index for index, _ in chunk}
        texts_by_index = {index: segment.text for index, segment in chunk}
        response = await _appellation_resolution_call(
            dossier=dossier, candidates=candidate_names,
            episode_id=episode_id, project_id=project_id,
        )
        for verdict in _verified_verdicts(
            response, candidates=candidates_set, source_text=source_text,
            valid_segment_indexes=valid_segment_indexes, segments=segments,
        ):
            if verdict.identity in candidates_set:
                if _alias_contradicted_by_naming(verdict, candidates_set, texts_by_index):
                    continue  # 原文点名给了别人，不能把这个称谓登记成 identity 的别名
                _apply_named_verdict(
                    verdict, conn=conn, project_id=project_id, episode_no=episode_no,
                    characters=characters, character_appellation_rows=character_appellation_rows,
                )
            elif verdict.identity == UNRESOLVED:
                _record_unresolved_appellation(verdict, unresolved_by_label=unresolved_by_label)
            else:
                _apply_functional_or_collective_verdict(verdict, functional_extras=functional_extras)
    if unresolved_appellations is not None:
        unresolved_appellations.extend(
            {"label": label, "segment_indexes": indexes}
            for label, indexes in unresolved_by_label.items()
        )


__all__ = ["resolve_narration_appellations", "COLLECTIVE", "FUNCTIONAL", "UNRESOLVED"]
