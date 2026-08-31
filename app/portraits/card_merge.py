"""建卡前的归并判断：``resolve_card_owner`` 判 ``none`` 只回答"这个称呼没有
逐字命中人物谱里任何 name/alias"，不回答"这不是已有的某个人"——两者是不同
的问题。真实事故：《我的女友井田》原文明写"以后别叫我井田了，叫我妈妈吧"，
但"妈妈"从未被连回"井田"的卡，全书 33 次"妈妈"一次都对不上，安静地生成了
第二套长相。本模块补的正是这一步判断，是 ``ensure_character_card`` 在
"none 之后、建卡之前"唯一的分岔口（见 ``resolve_card_build_or_merge``）。

判据范式对齐 ``app/production/prep_pack/true_name.py`` 的真名核验（卷宗检索
->候选选择题->段号钉证，读该模块的 ``_prep_pack_true_name_verdict_candidates``
/``_prep_pack_true_name_verdict`` 与其 docstring）：不跨包直接导入那两个
函数——它们绑定在"alias+true_name 双词检索"与 person/scene 双域派发上，
语义形状与本模块的"单词 label 检索、只判 person"不同；且 ``app.production``
整包在 ``__init__.py`` 里预先 import 了同层大量子模块，跨包导入会牵连一条
不必要的加载链。这里就地复刻同一套判据（卷宗零语义检索、候选集必须有真实
卷宗材料支撑、模型只做选择题、段号结构性钉证），逻辑保持同构，不是另起
炉灶。

方向必须 fail-open 到"建新卡"：错误合并会把两个人焊成一张卡，下游无从
分辨，比多一张卡更难发现（多一张卡至少是"两张卡"这个显式的、可复核的
状态；错误合并是"看起来正确"的静默污染）。判据链条上任何一步——卷宗为空、
候选集为空、模型选了候选集之外的东西、钉证段号非法、钉证段落缺 label 本身、
钉证段落缺候选称谓（双锚定）、独立的共现复核未通过——都直接返回
None/False，调用方据此照常建卡，不重试、不降级判据、不猜。

登记别名复用 ``app.portraits.card_aliases._cooccurrence_evidence`` 的既有
核验路径（同一纪律见该模块 docstring："不确定不登记"），模型的卷宗钉证只
决定"候选是谁"，真正登记进 aliases 的引句仍由这条独立的机械核验产出——两
层证据都要过，不单凭其中一层下结论。持久化复用
``app.portraits.card_rebind._cas_write_bible`` 的乐观并发 CAS 写回，不重写
第二套并发写入逻辑。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, ValidationError

from app.errors import code_ref
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.schemas import Bible, Character, CharacterAlias
from app.source_excerpt import index_source_segments

from ._db_probe import _has_column, _has_table
from .card_aliases import _cooccurrence_evidence
from .card_aliases import new_card_aliases
from .card_rebind import _cas_write_bible
from .constants import CAST_DISCOVERY_SOURCE_BUDGET, IDENTITY_NAME_FORM_REFERENTIAL
from .discovery_fragments import _bible_lock

_CARD_MERGE_NO_MATCH_LABEL = "都不是/无法确定"


class _CardMergeVerdictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_candidate: str
    supporting_entry_index: int
    supporting_quote: str = ""


def _card_merge_sample_within_budget(entries: list[dict], char_budget: int) -> list[dict]:
    """确定性等距采样（不是随机），预算不够时铺满全书范围而不是只取前几条
    命中——同一份输入任何时候重跑得到一模一样的结果，可复现、可审计。范式
    同 ``true_name.py`` 的 ``_prep_pack_sample_dossier_entries_within_budget``
    （见模块 docstring 关于不跨包导入的说明），逻辑保持一致，就地复刻一份。
    """
    if not entries or char_budget <= 0:
        return []
    total_chars = sum(len(item["text"]) for item in entries)
    if total_chars <= char_budget:
        return list(entries)
    average_chars = max(1.0, total_chars / len(entries))
    approx_count = max(1, int(char_budget / average_chars))
    step = max(1.0, len(entries) / approx_count)
    picked = sorted({min(len(entries) - 1, int(i * step)) for i in range(approx_count)})
    selected: list[dict] = []
    used = 0
    for index in picked:
        entry = entries[index]
        entry_chars = len(entry["text"])
        if used + entry_chars > char_budget:
            continue
        selected.append(entry)
        used += entry_chars
    return selected


def _card_merge_pin_entry(dossier: list[dict], entry_index: object) -> dict | None:
    """结构性钉证：``entry_index`` 必须落在这次卷宗集合内，不接受模型编造
    的编号；不是整数、或不在集合内一律返回 None（调用方据此不采信）。"""
    try:
        target = int(entry_index)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for item in dossier:
        if item["entry_index"] == target:
            return item
    return None


def _card_merge_dossier(
    conn, project_id: str, label: str,
) -> tuple[list[dict], dict[int, str]]:
    """全书范围内检索含 ``label`` 的原文段——不限当前集/前瞻窗口，同一根因见
    ``true_name.py`` 的 ``_prep_pack_true_name_dossier`` docstring（EP2 真实
    事故：旧版只查了一个有限窗口）。零语义，纯字符串包含判断。一并返回
    ``chapters_by_idx``（含 label 的全部章节原文，未截断），供
    ``_card_merge_alias_evidence`` 的独立机械复核复用，不重复查库。
    """
    entries: list[dict] = []
    chapters_by_idx: dict[int, str] = {}
    rows = conn.execute(
        "SELECT idx, content FROM chapters WHERE project_id=? ORDER BY idx",
        (project_id,),
    ).fetchall()
    for row in rows:
        content = str(row["content"] or "")
        if label not in content:
            continue
        chapter_idx = int(row["idx"])
        chapters_by_idx[chapter_idx] = content
        for segment_index, segment in enumerate(index_source_segments(content), start=1):
            if label in segment.text:
                entries.append({
                    "chapter_idx": chapter_idx, "segment_index": segment_index,
                    "text": segment.text,
                })
    entries = _card_merge_sample_within_budget(entries, CAST_DISCOVERY_SOURCE_BUDGET)
    for entry_index, item in enumerate(entries, start=1):
        item["entry_index"] = entry_index
    return entries, chapters_by_idx


def _card_merge_alias_evidence(
    chapters_by_idx: dict[int, str], forms: list[str], label: str,
) -> tuple[int, str] | None:
    """候选的规范名/已确认别名逐个当锚点，独立重新核验一次共现证据——不单凭
    模型的卷宗钉证下结论。复用 ``card_aliases._cooccurrence_evidence`` 的既有
    核验路径（同一纪律见该模块 docstring："不确定不登记"），不新写一套。
    """
    for anchor in forms:
        if not anchor:
            continue
        found = _cooccurrence_evidence(chapters_by_idx, anchor, label)
        if found is not None:
            return found
    return None


def _card_merge_prompt(
    label: str, dossier: list[dict], candidates: list[str],
) -> tuple[str, list[int], list[str]]:
    """构造候选选择题的 prompt；不携带任何"我猜 X 就是 Y"的推理引导，候选集
    之外强制一个"都不是/无法确定"选项。返回 prompt 与两份供 schema enum
    收紧用的合法取值（卷宗段号 / 候选名单）。"""
    catalog = "\n\n".join(
        f"[候选{item['entry_index']}][第{item['chapter_idx']}章·段{item['segment_index']}] "
        f"{item['text']}"
        for item in dossier
    )
    entry_indexes = [item["entry_index"] for item in dossier]
    candidate_options = [*candidates, _CARD_MERGE_NO_MATCH_LABEL]
    candidate_list = "、".join(candidates)
    prompt = f"""下面是从原著全书范围内检索到的、含有称谓"{label}"的原文段落
（出现顺序不代表任何推断结论），每段前标了候选编号：
{catalog}

人物谱候选名单（判别范围仅限以下几项，不要引入名单之外的人）：
{candidate_list}

任务：仅依据以上原文段落本身，判断称谓"{label}"是否是候选名单中某一位的
别名/外号/自称/新称呼，是的话具体是哪一位。
- selected_candidate 必须从候选名单中选一个精确的人名；原文不足以确定
  "{label}"具体对应候选中的哪一个时，选"{_CARD_MERGE_NO_MATCH_LABEL}"，不要
  勉强给出确定结论；不要因为某个候选在段落里出现次数多就倾向选它，只依据
  原文是否真的能确定"{label}"就是在称呼这个人；
- supporting_entry_index 必须填上面某个候选编号（取值只能是 {entry_indexes}
  之一），选你得出这个结论最主要依据的那一段——理想情况下这段原文应同时
  出现"{label}"与该候选的人物谱称谓（例如"以后别叫我 X 了，叫我 {label} 吧"
  这类身份链接句）；
- supporting_quote 可选，若填写请给该段里的一句原文摘录供人工复核参考，
  不要求逐字精确，留空也可以。
只输出符合 Schema 的 JSON。"""
    return prompt, entry_indexes, candidate_options


async def _card_merge_verdict(
    *, label: str, dossier: list[dict], candidates: list[str],
) -> _CardMergeVerdictResponse:
    """唯一一次模型调用：候选选择题，范式同
    ``true_name.py`` 的 ``_prep_pack_true_name_verdict``（低温：同一份卷宗
    重跑不该一次选中一次不确定）。"""
    prompt, entry_indexes, candidate_options = _card_merge_prompt(label, dossier, candidates)
    schema = _CardMergeVerdictResponse.model_json_schema()
    schema["properties"]["supporting_entry_index"]["enum"] = entry_indexes
    schema["properties"]["selected_candidate"]["enum"] = candidate_options
    operation_id = "portraits_card_merge:" + evidence_repository.content_hash({
        "label": label, "candidates": candidates, "dossier": entry_indexes,
    })
    return await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_CardMergeVerdictResponse,
        validate=None,
        operation_id=operation_id,
        max_tokens=500,
        temperature=0.0,
        output_schema=schema,
        call_meta={
            "stage_key": "portraits_card_merge_verdict",
            "label": label,
            "candidates": candidates,
        },
    )


async def resolve_card_merge_target(
    conn, project_id: str, label: str, bible: Bible,
) -> tuple[str, dict] | None:
    """``label``（触发建卡判定的称呼）是否其实是人物谱里某个既有角色的另一
    个称呼——完整判据链见模块 docstring。只在 ``resolve_card_owner`` 判
    "none" 之后调用；候选集为空时不发起模型调用（没有正确答案的选择题只会
    逼模型瞎选，纯属浪费）。返回 ``(归属者规范名, 待登记的 alias dict)``；
    任意一步不通过都返回 ``None``（fail-open 到"建新卡"）。落库另见
    ``apply_card_merge_alias``，本函数不写库，调用方决定用哪个锁。
    """
    if not bible.characters:
        return None
    dossier, chapters_by_idx = _card_merge_dossier(conn, project_id, label)
    if not dossier:
        return None
    roster = {c.name: [c.name, *(a.text for a in c.aliases)] for c in bible.characters}
    dossier_text = "".join(item["text"] for item in dossier)
    candidates = [
        name for name, forms in roster.items() if any(f and f in dossier_text for f in forms)
    ]
    if not candidates:
        return None
    response = await _card_merge_verdict(label=label, dossier=dossier, candidates=candidates)
    if response.selected_candidate not in candidates:
        return None
    pinned = _card_merge_pin_entry(dossier, response.supporting_entry_index)
    if pinned is None or label not in pinned["text"]:
        return None
    forms = roster[response.selected_candidate]
    if not any(form and form in pinned["text"] for form in forms):
        return None
    evidence = _card_merge_alias_evidence(chapters_by_idx, forms, label)
    if evidence is None:
        return None
    chapter_idx, quote = evidence
    alias = CharacterAlias(
        text=label, name_kind=IDENTITY_NAME_FORM_REFERENTIAL,
        evidence_chapter_index=chapter_idx, evidence_quote=quote, is_exclusive=False,
    ).model_dump(mode="json")
    return response.selected_candidate, alias


def _card_merge_artifact(
    project_id: str, row, data: dict, character_name: str, alias_text: str,
) -> tuple[bool, str | None]:
    """产出登记别名后 bible_json 的证据 artifact，串进既有 lineage——同构
    ``card_rebind._create_rebind_artifact``，只是 ``model_snapshot`` 的
    operation 不同。"""
    try:
        previous_id = row["bible_artifact_id"]
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="character_bible", scope_type="project", scope_id=project_id,
            status="approved", trust_level="T2", content=data,
            parent_artifact_ids=[previous_id] if previous_id else [],
            contract_version="character-bible-1.0.0",
            prompt_version="card-merge-alias-1.0.0",
            model_snapshot={
                "operation": "card_merge_alias",
                "character_name": character_name, "alias_text": alias_text,
            },
        ))
        return True, artifact["id"]
    except Exception as exc:  # noqa: BLE001 - authority mutation must fail closed
        code_ref(
            exc, action="card_merge_alias_artifact",
            context={"project_id": project_id, "character_name": character_name},
        )
        return False, None


def apply_card_merge_alias(
    conn, project_id: str, character_name: str, alias: dict,
) -> bool:
    """把 ``resolve_card_merge_target`` 判定通过的别名落到
    ``character_name`` 的 aliases 列表——CAS 写回 + artifact lineage 同构
    ``card_rebind`` 模块，复用其 ``_cas_write_bible``，不重写第二套并发写入
    逻辑。已登记过同一 ``text`` 时直接幂等返回 True，不重复追加。调用方须
    自行持有 ``_bible_lock``（本函数不加锁，与 ``card_rebind`` 分工一致）。
    """
    artifact_supported = (
        _has_column(conn, "projects", "bible_artifact_id") and _has_table(conn, "artifacts")
    )
    select_cols = "bible_json, bible_version" + (
        ", bible_artifact_id" if artifact_supported else ""
    )
    row = conn.execute(f"SELECT {select_cols} FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    data = json.loads(row["bible_json"])
    target = next((c for c in data.get("characters", []) if c.get("name") == character_name), None)
    if target is None:
        return False
    existing_texts = {str(a.get("text") or "").strip() for a in target.get("aliases") or []}
    if alias["text"] in existing_texts:
        return True
    target.setdefault("aliases", []).append(alias)
    payload = json.dumps(data, ensure_ascii=False)
    next_artifact_id = None
    if artifact_supported:
        ok, next_artifact_id = _card_merge_artifact(project_id, row, data, character_name, alias["text"])
        if not ok:
            return False
    if not _cas_write_bible(conn, project_id, row, payload, artifact_supported, next_artifact_id):
        return False
    conn.commit()
    return True


async def resolve_card_build_or_merge(
    conn, project_id: str, name: str, bible: Bible, verdict: dict,
    identity_source_labels: list[str] | None, forward_chapters_by_idx: dict[int, str],
    write_guard,
) -> dict | Character:
    """``ensure_character_card`` "none 之后、建卡之前"唯一的分岔口：先问一遍
    这个称呼是不是人物谱里已有某个角色的另一种叫法（``resolve_card_merge_
    target``，fail-open 到"不是"）；判定是则登记别名、返回既有归属者的
    ``exists`` 结果，不建新卡；判定不是（或任何一步钉不住）则照原逻辑构造
    ``Character``。``card_owner.resolve_card_owner`` 的 "none" 只回答"没有
    逐字命中"，不回答"这是不是已有的某个人"，本函数补的正是这一步——完整
    判据见 ``resolve_card_merge_target`` docstring。
    """
    merged = await resolve_card_merge_target(conn, project_id, name, bible)
    if write_guard:
        write_guard()
    if merged is not None:
        merge_target, alias = merged
        lock = await _bible_lock(project_id)
        async with lock:
            if write_guard:
                write_guard()
            apply_card_merge_alias(conn, project_id, merge_target, alias)
        return {"status": "exists", "name": merge_target}
    aliases = new_card_aliases(name, identity_source_labels, forward_chapters_by_idx)
    try:
        return Character.model_validate({
            "name": name, "role": verdict["role"],
            "appearance_canonical": verdict["appearance_canonical"],
            "personality": verdict["personality"], "speech_style": verdict["speech_style"],
            "relationships": verdict["relationships"], "portrait_prompt_override": None,
            "source_evidence": verdict.get("source_evidence") or [], "aliases": aliases,
        })
    except ValidationError as exc:
        return {"status": "error", "name": name, "reason": f"card invalid {exc}"[:240]}
