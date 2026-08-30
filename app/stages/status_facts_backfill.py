"""状态事实回填——证据解析与 backfill_character_status_facts 主入口。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app import config
from app.db import log_provider_call
from app.harness import model_gateway
from app.schemas import (Bible, Character, CharacterAffiliation, CharacterRelation, extract_json)

from .alias_backfill import _alias_verdict_roster, _render_alias_backfill_source
from .alias_verdict import (
    _ALIAS_VERDICT_NO_MATCH_LABEL,
    _alias_verdict_candidates,
    _alias_verdict_dossier,
    _alias_verdict_pin_segment,
)
from .bible_shared import _bible_short_json_call_meta, _chapters_by_idx
from .constants import SYSTEM_PREFIX
from .identity_evidence import _alias_declaration_verified, _find_alias_bridge_chapter
from .status_facts_verdict import (
    _STATUS_FACT_VERDICT_STAGE_KEY,
    _status_fact_interval_resolution,
    _status_fact_quote_dual_anchor_verified,
    _status_fact_verdict_call,
)


async def _status_fact_evidence_resolution(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    claim_text: str,
    subject_name: str,
    evidence_chapter_index: int,
    evidence_quote: str,
    declared_valid_from_chapter: int | None,
    declared_valid_to_chapter: int | None,
    *,
    fact_noun: str,
    roster: dict[str, list[str]],
    project_id: str | None = None,
) -> dict[str, Any]:
    """状态事实证据判定的统一入口，与 `_alias_evidence_resolution` 同一流程骨架（该函数
    docstring 有完整的两段式说明，这里不重复）：核心证据核验（声明核验 → 桥接检索兜底）
    → 候选判别裁决（候选覆盖该章全部人物谱角色，卷宗证据覆盖全部候选而不止被测对象）
    → 有效区间核验（`_status_fact_interval_resolution`，别名机制没有这一步，因为别名
    恒真、不需要区间）。`claim_text` 是归属对象（org）或关系对象（to）的逐字文本，
    `subject_name` 是被测角色的规范名（裁决闸要求 `selected_candidate` 恰好等于它）。

    返回结构 `{"accepted": bool, "chapter_idx": int|None, "quote": str, "reason": str,
    "valid_from_chapter": int|None, "valid_from_is_fallback": bool, "valid_to_chapter":
    int|None, "valid_to_is_fallback": bool}`：`accepted=True` 时后面这些字段是应当登记
    的证据与区间——`valid_from_is_fallback`/`valid_to_is_fallback` 为 True 表示对应边界
    是代码回落的默认值（模型申报的边界未能独立核验、不予采信），不是模型申报并核验
    通过的原始边界（见 `_status_fact_interval_resolution` docstring"拆分处置"一节）；
    `accepted=False` 时 `reason` 是机器可读拒绝原因（`no_bridge_chapter`/
    `quote_missing_dual_anchor`/`no_verdict_candidates`/`no_verdict_dossier`/
    `verdict_call_failed`/`candidate_mismatch`/`candidate_uncertain`/
    `segment_not_pinned`/`interval_contradiction`——最后一项特指申报区间与核心证据点
    逻辑矛盾，不包含"边界外推缺乏独立支撑"这种情况，后者现在走拆分处置、不再整条
    拒绝；`quote_missing_dual_anchor` 见 `_status_fact_quote_dual_anchor_verified`
    docstring——章级共现通过不代表被登记的这一句引句里真的锚定了主体）。
    """
    empty = {
        "accepted": False, "chapter_idx": None, "quote": "", "reason": "",
        "valid_from_chapter": None, "valid_from_is_fallback": False,
        "valid_to_chapter": None, "valid_to_is_fallback": False,
    }
    if _alias_declaration_verified(
        chapters_by_idx, anchor_texts, claim_text, evidence_chapter_index, evidence_quote,
    ):
        resolved_chapter_index, resolved_quote = evidence_chapter_index, evidence_quote
    else:
        bridge = _find_alias_bridge_chapter(chapters_by_idx, anchor_texts, claim_text)
        if bridge is None:
            return {**empty, "reason": "no_bridge_chapter"}
        resolved_chapter_index, resolved_quote = bridge

    # 引句双锚定闸（事故修复，见 `_status_fact_quote_dual_anchor_verified` docstring）：
    # object 锚点复用 `roster`——claim_text 若恰好是某角色的规范名（关系事实的 to 必然
    # 是，因为调用方已核验 `to in known_names`），取其规范名+已确认别名全集；claim_text
    # 不是任何角色规范名时（归属事实的 org，自由文本，没有别名概念），`roster.get` 落空，
    # 回退为 {claim_text} 本身——两种情况用同一行代码表达，不需要按归属/关系分支特判。
    object_anchor_texts = set(roster.get(claim_text, [claim_text]) or [claim_text])
    if not _status_fact_quote_dual_anchor_verified(
        resolved_quote, set(anchor_texts), object_anchor_texts,
    ):
        return {**empty, "reason": "quote_missing_dual_anchor"}

    chapter_text = chapters_by_idx.get(resolved_chapter_index, "")
    candidates = _alias_verdict_candidates(chapter_text, roster)
    if subject_name not in candidates:
        # 防御性分支：subject_name 对应的 anchor_texts 已经通过声明核验/桥接检索命中
        # 该章，理论上必然被 `_alias_verdict_candidates` 收进候选集（正常不应触发）。
        return {**empty, "reason": "no_verdict_candidates"}
    # 卷宗证据锚点必须覆盖全部候选人，不能只锚定被测对象一方（与 `_alias_evidence_resolution`
    # 同一理由，见该函数关于"真实误登记事故 2"的说明——只给卷宗看被测对象周围的证据，
    # 候选判别题就会名存实亡）。
    dossier_anchor_texts = set(anchor_texts) | {
        form for name in candidates for form in roster.get(name, [])
    }
    dossier = _alias_verdict_dossier(
        resolved_chapter_index, chapter_text, claim_text, dossier_anchor_texts,
    )
    if not dossier:
        return {**empty, "reason": "no_verdict_dossier"}
    # claim_text 本身若恰好也在候选集里（关系事实的 to 就是这种情况——它本就是候选中
    # 已收录的另一个人），结构上永远不可能是"拥有这层事实的那个人"（`to == name` 早在
    # 调用方过滤掉了自关系）。留在候选列表里会诱导裁决模型把"claim_text 这个名字指代
    # 候选中的谁"（trivial，答案就是它自己）误当成本次要判别的问题，见
    # `_status_fact_verdict_call` docstring 对真实事故的说明。此处剔除，双重保险：
    # 提示词已经明确说明，这里再从 Schema enum 层面彻底堵死这个选项。
    verdict_candidates = [c for c in candidates if c != claim_text]
    try:
        response = await _status_fact_verdict_call(
            fact_noun=fact_noun, claim_text=claim_text, dossier=dossier,
            candidates=verdict_candidates, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001 - 裁决调用失败按不确定处理：不确定不登记
        log_provider_call(
            _STATUS_FACT_VERDICT_STAGE_KEY, config.MODEL_TEXT, "FAILED", None, 0,
            meta={
                "claim_text": claim_text, "subject_name": subject_name,
                "error": str(exc)[:300],
            },
        )
        return {**empty, "reason": "verdict_call_failed"}
    if response.selected_candidate != subject_name:
        reason = (
            "candidate_uncertain"
            if response.selected_candidate == _ALIAS_VERDICT_NO_MATCH_LABEL
            else "candidate_mismatch"
        )
        return {**empty, "reason": reason}
    if _alias_verdict_pin_segment(dossier, response.supporting_segment_index) is None:
        return {**empty, "reason": "segment_not_pinned"}

    interval = _status_fact_interval_resolution(
        chapters_by_idx, anchor_texts, object_anchor_texts, resolved_chapter_index,
        declared_valid_from_chapter, declared_valid_to_chapter,
    )
    if interval is None:
        return {**empty, "reason": "interval_contradiction"}
    valid_from_chapter, valid_from_is_fallback, valid_to_chapter, valid_to_is_fallback = interval

    return {
        "accepted": True, "chapter_idx": resolved_chapter_index, "quote": resolved_quote,
        "reason": "", "valid_from_chapter": valid_from_chapter,
        "valid_from_is_fallback": valid_from_is_fallback,
        "valid_to_chapter": valid_to_chapter,
        "valid_to_is_fallback": valid_to_is_fallback,
    }


class _StatusFactAffiliationDeclaration(BaseModel):
    """归属回填申报合同：模型只申报，是否登记由后端核验决定（与
    `_AliasBackfillDeclaration` 同一纪律）。"""

    character_name: str = ""
    org: str = ""
    relation_kind: str = ""
    evidence_chapter_index: int = -1
    evidence_quote: str = ""
    valid_from_chapter: int | None = None    # 不申报=交给代码回退为证据所在章
    valid_to_chapter: int | None = None      # 不申报=尚无证据表明已失效


class _StatusFactRelationDeclaration(BaseModel):
    """关系回填申报合同：结构与 `_StatusFactAffiliationDeclaration` 同构，唯一差别是
    `org`（归属对象，自由文本）换成 `to`（关系对象，必须是人物谱里已有的另一个人）。"""

    character_name: str = ""
    to: str = ""
    relation_kind: str = ""
    evidence_chapter_index: int = -1
    evidence_quote: str = ""
    valid_from_chapter: int | None = None
    valid_to_chapter: int | None = None


class _StatusFactBackfillDraft(BaseModel):
    affiliations: list[_StatusFactAffiliationDeclaration] = Field(default_factory=list)
    relations: list[_StatusFactRelationDeclaration] = Field(default_factory=list)


def _status_fact_roster_hint(character: Character) -> str:
    """为回填提示词的已收录角色清单附上该角色已登记的归属/关系摘要（帮助模型不重复
    申报、也不与已知事实矛盾），纯字符串拼装、无模型调用。"""
    parts: list[str] = []
    if character.affiliations:
        parts.append("归属：" + "、".join(a.org for a in character.affiliations))
    if character.relations:
        parts.append("关系：" + "、".join(
            f"{r.to}({r.relation_kind})" for r in character.relations
        ))
    return "；".join(parts)


async def backfill_character_status_facts(
    bible: Bible, chapters: list[dict], *, project_id: str | None = None,
) -> dict[str, dict[str, list[str]]]:
    """窄口径状态事实回填（认知层，用于当前项目一次性回填历史人物谱）：全书上下文，
    只产出并核验 `Character.affiliations`/`Character.relations`，绝不改写人物谱任何
    其它既有字段（包括层一的 `aliases`——与 `backfill_character_aliases` 互不干扰，
    两个函数各自只读写自己负责的字段）。

    调用方式：协调层在部署窗口拿到已定稿的 `bible`（`Bible` 实例，建议已经跑过
    `backfill_character_aliases` 回填过别名——状态事实的共现锚点会用到角色已确认的
    别名，先跑别名回填能提高召回率，但不是强制前置条件，`bible` 只有规范名也能跑）与
    该项目全书 `chapters`（`list[dict]`，需含 `idx`/`content` 字段，与
    `backfill_character_aliases`/`generate_bible` 输入同构）后直接：

        added = await backfill_character_status_facts(bible, chapters, project_id=project_id)

    函数原地把核验通过的归属/关系追加进对应 `Character.affiliations`/`Character.relations`
    （幂等：同一角色已登记过的 org / (to, relation_kind) 组合不会重复追加，可安全重跑）；
    调用方随后自行把更新后的 `bible` 序列化落库（本函数不做任何数据库读写）。

    返回值 `{"affiliations": {character_name: [本次新增归属org, ...]}, "relations":
    {character_name: [本次新增关系对象to, ...]}}`，供调用方记账/日志展示；两个子 dict
    都为空不代表失败（可能全书确实没有可核验的状态事实，也可能模型调用失败——两者都已
    通过 `log_provider_call` 记录，失败时 status="FAILED"，全书无可核验状态事实时
    status="EMPTY"）。

    核验规则见 `_status_fact_evidence_resolution`：模型只负责申报语义假设
    （character_name + org/to + 可选的有效区间），代码逐字核验证据、候选判别裁决、
    区间是否有证据支撑；任一环节不过 → 不登记（不确定不登记，安全默认，绝不放松）。
    禁止任何具体人名/势力名/称谓的硬编码——判据只看结构（逐字子串命中 + 章节内共现 +
    候选判别 + 区间证据），不针对具体词做特判分支。
    """
    chapters_by_idx = _chapters_by_idx(chapters)
    source = _render_alias_backfill_source(chapters)
    empty_result: dict[str, dict[str, list[str]]] = {"affiliations": {}, "relations": {}}
    if not source.strip() or not bible.characters:
        return empty_result
    roster = _alias_verdict_roster(bible)
    roster_text = "、".join(
        c.name + (f"（已登记{hint}）" if (hint := _status_fact_roster_hint(c)) else "")
        for c in bible.characters
    )
    prompt = f"""任务：通读下面的全书正文，为【已收录角色】找出他们在原文中有明确证据支撑的
势力/宗门归属，以及与其它已收录角色之间有明确证据支撑的人物关系（如同门、师徒、敌对、
盟友等），逐条给出可核验的证据。

已收录角色（只为这些人申报归属/关系，不要发明角色列表之外的人；关系的对象也必须是下面
列表里的另一个人，不能是角色本人，也不能是列表外的人）：
{roster_text}

归属（affiliations）每条给七个字段：
1. character_name（必须逐字等于上面角色列表中的某个名字）；
2. org（该角色所属的宗门/阵营/势力名，逐字照抄原文写法）；
3. relation_kind（该角色与该势力的关系性质，自由描述，如"成员""效忠""敌对"等，不强制
   使用固定词表）；
4. evidence_chapter_index（该角色姓名或已确认别名与该势力名同时出现、且原文明确交代
   归属关系的那一章的章节序号，取该章节【第 N 章】块头里的数字 N——只是同章出现不算，
   原文必须真的能看出这层归属关系）；
5. evidence_quote（该章节原文中的逐字引句，必须原样照抄，一个字都不能改，也不要自己
   在引句前后加引号包裹）；
6. valid_from_chapter（可选）：该归属从哪一章开始生效——不确定就不要填这个字段，后端
   会用 evidence_chapter_index 作为默认起点；只有原文明确交代了这层归属并非从头就有
   （比如后来才拜入门下）时才需要申报，且必须是能找到相应原文依据的章节，编造的起点
   会导致整条归属都不被采信；
7. valid_to_chapter（可选）：该归属到哪一章为止——不确定/仍在持续就不要填这个字段，
   后端默认视为尚未失效；只有原文明确交代了归属结束（叛出师门、转投他派等）时才需要
   申报，同样必须有原文依据支撑。

关系（relations）每条给七个字段，结构与归属完全相同，唯一差别：把 org 换成 to（关系
对象，必须是【已收录角色】列表中的另一个名字）。

不确定就不要申报：证据不足、记不清原文原句、原文没有明确交代归属/关系（只是同章出现
不算），宁可漏报，绝不能编造或近似改写引句——后端会逐字核对，改写过的引句或自行添加
的引号包裹都无法通过、白白浪费申报。只申报归属/关系，不要输出角色的外观、性格等其它
信息——这些字段本次不会被采用。

全书正文（部分较长章节可能已截断，仅代表你能看到的范围，不代表原文实际只有这些）：
{source}

输出 JSON Schema：
{{"affiliations": [{{"character_name": str, "org": str, "relation_kind": str, "evidence_chapter_index": int, "evidence_quote": str, "valid_from_chapter": int, "valid_to_chapter": int}}], "relations": [{{"character_name": str, "to": str, "relation_kind": str, "evidence_chapter_index": int, "evidence_quote": str, "valid_from_chapter": int, "valid_to_chapter": int}}]}}"""
    try:
        raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX},
             {"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=8192,
            call_meta=_bible_short_json_call_meta({
                "stage": "人物状态事实回填",
                "stage_key": "character_status_fact_backfill",
                "call_role": "stage_generate",
                "call_role_label": "归属关系回填",
                "expected_json": True,
                "project_id": project_id,
            }),
        )
        declared = _StatusFactBackfillDraft.model_validate(extract_json(raw))
    except Exception as exc:  # noqa: BLE001 - 回填失败保留已有人物谱，不阻断调用方
        log_provider_call(
            "character_status_fact_backfill", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"error": str(exc)[:300]},
        )
        return empty_result

    by_name = {c.name: c for c in bible.characters}
    known_names = set(by_name.keys())

    added_affiliations: dict[str, list[str]] = {}
    for item in declared.affiliations:
        name = (item.character_name or "").strip()
        character = by_name.get(name)
        org = (item.org or "").strip()
        if character is None or not org:
            continue
        if org in {a.org for a in character.affiliations}:
            continue  # 幂等：已登记过的归属不重复追加，可安全重跑
        anchor_texts = {character.name, *(a.text for a in character.aliases)}
        resolved = await _status_fact_evidence_resolution(
            chapters_by_idx, anchor_texts, org, character.name,
            item.evidence_chapter_index, item.evidence_quote,
            item.valid_from_chapter, item.valid_to_chapter,
            fact_noun="势力归属", roster=roster, project_id=project_id,
        )
        if resolved["accepted"]:
            character.affiliations.append(CharacterAffiliation(
                org=org, relation_kind=item.relation_kind,
                evidence_chapter_index=resolved["chapter_idx"],
                evidence_quote=resolved["quote"],
                valid_from_chapter=resolved["valid_from_chapter"],
                valid_from_is_fallback=resolved["valid_from_is_fallback"],
                valid_to_chapter=resolved["valid_to_chapter"],
                valid_to_is_fallback=resolved["valid_to_is_fallback"],
            ))
            added_affiliations.setdefault(name, []).append(org)

    added_relations: dict[str, list[str]] = {}
    for item in declared.relations:
        name = (item.character_name or "").strip()
        character = by_name.get(name)
        to = (item.to or "").strip()
        if character is None or not to or to == name or to not in known_names:
            continue  # 关系对象必须是人物谱里已有的另一个人（不能是角色本人或未知的人）
        if (to, item.relation_kind) in {(r.to, r.relation_kind) for r in character.relations}:
            continue  # 幂等：同一对象+同一关系性质已登记过的不重复追加
        anchor_texts = {character.name, *(a.text for a in character.aliases)}
        resolved = await _status_fact_evidence_resolution(
            chapters_by_idx, anchor_texts, to, character.name,
            item.evidence_chapter_index, item.evidence_quote,
            item.valid_from_chapter, item.valid_to_chapter,
            fact_noun="人物关系", roster=roster, project_id=project_id,
        )
        if resolved["accepted"]:
            character.relations.append(CharacterRelation(
                to=to, relation_kind=item.relation_kind,
                evidence_chapter_index=resolved["chapter_idx"],
                evidence_quote=resolved["quote"],
                valid_from_chapter=resolved["valid_from_chapter"],
                valid_from_is_fallback=resolved["valid_from_is_fallback"],
                valid_to_chapter=resolved["valid_to_chapter"],
                valid_to_is_fallback=resolved["valid_to_is_fallback"],
            ))
            added_relations.setdefault(name, []).append(to)

    log_provider_call(
        "character_status_fact_backfill", config.MODEL_TEXT,
        "OK" if (added_affiliations or added_relations) else "EMPTY", None, 0,
        meta={
            "declared_affiliations": len(declared.affiliations),
            "declared_relations": len(declared.relations),
            "verified_affiliations": sum(len(v) for v in added_affiliations.values()),
            "verified_relations": sum(len(v) for v in added_relations.values()),
        },
    )
    return {"affiliations": added_affiliations, "relations": added_relations}
