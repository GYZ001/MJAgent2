"""角色点名——候选归并、称谓作用域裁决与泛指候选消解。"""
from __future__ import annotations

import asyncio
import hashlib
import json


from app.harness import model_gateway

from .alias_backfill import _roster_presence_dossier
from .bible_shared import _bible_short_json_call_meta
from .common import BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE, BIBLE_SMALL_VERDICT_TIMEOUT_S
from .constants import SYSTEM_PREFIX
from .roster_candidates import (
    _RosterAppellationScope,
    _RosterCandidate,
    _RosterIdentityResolution,
    _RosterOnstageEvidence,
    _candidate_appellations,
    _identity_merge_keys,
    _normalize_roster_verdict_payload,
    _roster_candidate_stands_alone,
    _roster_label_needs_identity_resolution,
    _shared_appellations,
)
from .roster_personhood import _spread_named_segments


def _merge_roll_call_candidates(
    chunk_results: list[list[_RosterCandidate]],
) -> list[_RosterCandidate]:
    """按同章明确身份链接做连通分量归并；规范名始终优先使用正式姓名。"""
    flattened = [candidate for group in chunk_results for candidate in group
                 if (candidate.primary_appellation or "").strip()]
    parent = list(range(len(flattened)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(flattened)):
        left_names = _identity_merge_keys(flattened[left])
        if not left_names:
            continue
        left_primary = (flattened[left].primary_appellation or "").strip()
        for right in range(left + 1, len(flattened)):
            right_names = _identity_merge_keys(flattened[right])
            shared = left_names & right_names
            if not shared:
                continue
            right_primary = (flattened[right].primary_appellation or "").strip()
            # 两人碰巧被写成同一个 formal_name（王有材/小胖子都「揭示」成李富贵）
            # 时，共享的只能是那个真名，不是任何一方的主称呼，不得合并。
            if shared & {left_primary, right_primary}:
                union(left, right)

    groups: dict[int, list[_RosterCandidate]] = {}
    for index, candidate in enumerate(flattened):
        groups.setdefault(find(index), []).append(candidate)

    merged: list[_RosterCandidate] = []
    for group in groups.values():
        formal_names = [item.formal_name.strip() for item in group if item.formal_name.strip()]
        primary = group[0].primary_appellation.strip()
        formal = formal_names[0] if formal_names else ""
        aliases = list(dict.fromkeys(
            value for item in group for value in _candidate_appellations(item)
            if value and value not in {formal, primary}
        ))
        if formal and primary != formal and primary not in aliases:
            aliases.insert(0, primary)
        evidence: list[_RosterOnstageEvidence] = []
        identity_evidence: list[_RosterOnstageEvidence] = []
        for item in group:
            evidence.extend(item.onstage_evidence)
            identity_evidence.extend(item.identity_evidence)
        deduped = list({(item.chapter_index, item.quote): item for item in evidence}.values())
        deduped_identity = list({
            (item.chapter_index, item.quote): item for item in identity_evidence
        }.values())
        personhoods = {item.personhood for item in group}
        if "person" in personhoods:
            personhood = "person"
        elif personhoods == {"non_person"}:
            personhood = "non_person"
        else:
            personhood = "uncertain"
        merged.append(_RosterCandidate(
            primary_appellation=primary,
            formal_name=formal,
            aliases=aliases,
            identity_evidence=deduped_identity[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE],
            onstage_evidence=deduped[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE],
            personhood=personhood,
        ))
    return merged


BIBLE_APPELLATION_SCOPE_SEGMENTS = 8


async def _roster_appellation_scope(
    candidate: "_RosterCandidate",
    chapters_by_idx: dict[int, str],
    *,
    project_id: str | None = None,
) -> str:
    """归并不成立之后该问的问题：这个说法在全书里指一个人，还是指一类人。

    身份归一问的是「能不能并到名单里另一个实体身上」，它答不出来只说明并不过去。
    真正决定该不该建卡的是另一件事——这个称呼背后是不是一个稳定的个体。把这两件
    事当成一件，「全书只以代称出现」的角色就结构上必然出局：它因 name_form=
    referential 被送去归一，而归一的候选名单里恰恰没有它自己，必判 uncertain。

    真实故障（《罗刹海市》proj_1a3a92a9b248）：女主角「龙女」在资格裁决里已被
    模型读着卷宗判为 person（同一批里「村民」「龙宫」被正确判为 non_person），
    第 2、3 章各有在场证据；随后归一裁决被问「龙女是不是马骥/福海/异史氏里的
    某一个」，如实答 uncertain，她就此整个消失，三章语料最终只收下 1 张卡。

    卷宗跨全书相隔较远的几处取样，这正是两类称呼分得开的地方：真角色在相隔很远
    的两处仍是同一个人，身份、关系、经历连得成一条线；类别称谓换一处就换个人。
    少于两处用法时没有可比对的两端，返回 uncertain 让上层退回保守判据，不猜。
    """
    label = (candidate.formal_name or candidate.primary_appellation).strip()
    names = sorted(_candidate_appellations(candidate))
    if not label or not names:
        return "uncertain"
    dossier = _spread_named_segments(
        names, chapters_by_idx, limit=BIBLE_APPELLATION_SCOPE_SEGMENTS,
    )
    if len(dossier) < 2:
        return "uncertain"
    catalog = "\n\n".join(
        f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
        for item in dossier
    )
    valid_chapters = {int(item["chapter_idx"]) for item in dossier}
    prompt = f"""任务：判断「{label}」这个说法在本书里指的是同一个人，还是不同场合指不同的人。
同一人物的其它称呼：{json.dumps(names, ensure_ascii=False)}。

原文卷宗（取自全书相隔较远的几处）：
{catalog}

只根据卷宗原文判断，JSON 字段必须用 verdict：
- one_person：卷宗各处的「{label}」是同一个人，身份、关系、经历或所处情节能连成一条线。
- many_people：卷宗各处的「{label}」是不同的人，这个说法靠外形、衣着、职务或身份指人，换一处场合指的就是另一个人。
- uncertain：卷宗不足以判断，证据不够时选这个。

supporting_chapter_index 必须是卷宗里出现过的数字章号，例如 1。只看卷宗原文，不用作品常识补充。
"""
    try:
        resolution = await asyncio.wait_for(
            model_gateway.chat_structured(
                [{"role": "system", "content": SYSTEM_PREFIX},
                 {"role": "user", "content": prompt}],
                model_type=_RosterAppellationScope,
                validate=None,
                normalize_payload=_normalize_roster_verdict_payload,
                operation_id="character_appellation_scope:" + hashlib.sha256(
                    f"{label}:{catalog}".encode("utf-8")
                ).hexdigest(),
                temperature=0.0,
                max_tokens=256,
                call_meta=_bible_short_json_call_meta({
                    "stage": "代称指称范围",
                    "stage_key": "character_appellation_scope",
                    "call_role": "stage_validate",
                    "character_name": label,
                    "project_id": project_id,
                }),
            ),
            timeout=BIBLE_SMALL_VERDICT_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - 问不成就退回保守判据，不在这里定生死
        return "uncertain"
    # 只认「报得出卷宗里某一章」的答案。这里不再顺带查称呼在不在卷宗文本里——
    # 卷宗本来就是按称呼检索出来的，那个条件恒真，写上去等于把闸开成常开。
    if resolution.supporting_chapter_index not in valid_chapters:
        return "uncertain"
    return resolution.verdict


async def _resolve_generic_character_candidates(
    candidates: list[_RosterCandidate],
    chapters_by_idx: dict[int, str],
    *,
    project_id: str | None = None,
) -> list[_RosterCandidate]:
    """把描述性称呼裁决到已有实体；并不过去的再问它自己是不是一个人。"""
    known_names = {value for item in candidates for value in _candidate_appellations(item)}
    # 归并已经跑完，同一个人的多份点名结果已经合成一条。此刻还被多个候选共用的
    # 称呼，在这本书里就分不出人，必须交给模型消歧，不能各自建卡。
    ambiguous = _shared_appellations(candidates)
    specific = [
        candidate for candidate in candidates
        if not _roster_label_needs_identity_resolution(candidate, known_names, ambiguous)
    ]
    if not specific:
        return candidates
    kept: list[_RosterCandidate] = []
    unmerged: list[_RosterCandidate] = []
    jobs: list[tuple[_RosterCandidate, str, list[str]]] = []
    candidate_names = [
        {
            "canonical": item.formal_name or item.primary_appellation,
            "appellations": sorted(_candidate_appellations(item)),
        }
        for item in specific
    ]
    for candidate in candidates:
        label = (candidate.formal_name or candidate.primary_appellation).strip()
        if not _roster_label_needs_identity_resolution(candidate, known_names, ambiguous):
            kept.append(candidate)
            continue
        evidence_blocks: list[str] = []
        for evidence in candidate.onstage_evidence[:2]:
            chapter_text = chapters_by_idx.get(evidence.chapter_index, "")
            dossier = _roster_presence_dossier(
                evidence.chapter_index, chapter_text, evidence.quote,
            )
            if dossier:
                evidence_blocks.append(json.dumps(dossier, ensure_ascii=False))
        if not evidence_blocks:
            # 卷宗都建不出来就没法问归一，但「问不成」同样不是「这个人不存在」，
            # 判据与归一失败那条路径共用（见下方 unmerged 的处置）。
            unmerged.append(candidate)
            continue
        prompt = f"""任务：判断描述性称呼「{label}」是否是候选实体名单中的同一个人物。

候选实体：
{json.dumps(candidate_names, ensure_ascii=False)}

描述性称呼的原文卷宗：
{chr(10).join(evidence_blocks)}

硬规则：
1. 只有原文中的同场连续指代、动作连续、对话连续或明确命名句才能判 same。
2. 外貌相似、年龄相近、都在同一宗门、常识猜测都不能判 same。
3. canonical_appellation 只能逐字选择候选实体的 canonical 值。
4. 无法充分证明时 verdict=uncertain；描述性称呼不能因此创建独立人物谱角色。
"""
        jobs.append((candidate, prompt, evidence_blocks))

    async def _resolve_one(
        candidate: _RosterCandidate, prompt: str, evidence_blocks: list[str],
    ) -> tuple[_RosterCandidate, _RosterIdentityResolution] | None:
        label = (candidate.formal_name or candidate.primary_appellation).strip()
        try:
            resolution = await asyncio.wait_for(
                model_gateway.chat_structured(
                    [{"role": "system", "content": SYSTEM_PREFIX},
                     {"role": "user", "content": prompt}],
                    model_type=_RosterIdentityResolution,
                    validate=None,
                    operation_id="character_identity_resolution:" + hashlib.sha256(
                        f"{label}:{chr(10).join(evidence_blocks)}".encode("utf-8")
                    ).hexdigest(),
                    temperature=0.0,
                    max_tokens=512,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "人物身份归一",
                        "stage_key": "character_identity_resolution",
                        "call_role": "stage_validate",
                        "character_name": label,
                        "project_id": project_id,
                    }),
                ),
                timeout=BIBLE_SMALL_VERDICT_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - 不确定时宁可不新建泛称角色
            return None
        if resolution.verdict != "same":
            return None
        return candidate, resolution

    resolved = await asyncio.gather(*(
        _resolve_one(candidate, prompt, evidence_blocks)
        for candidate, prompt, evidence_blocks in jobs
    ))
    # 合并回 specific 必须串行：两个泛称可能指向同一实体，并行写 aliases 会丢条目。
    for (asked, _prompt, _blocks), item in zip(jobs, resolved, strict=True):
        if item is None:
            unmerged.append(asked)
            continue
        candidate, resolution = item
        target = next((
            entry for entry in specific
            if (entry.formal_name or entry.primary_appellation) == resolution.canonical_appellation
        ), None)
        if target is None:
            continue
        aliases = list(dict.fromkeys([
            *target.aliases, candidate.primary_appellation, candidate.formal_name,
            *candidate.aliases,
        ]))
        target.aliases = [
            value for value in aliases
            if value and value not in {target.primary_appellation, target.formal_name}
        ]
        target.onstage_evidence = list({
            (entry.chapter_index, entry.quote): entry
            for entry in [*target.onstage_evidence, *candidate.onstage_evidence]
        }.values())[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE]
        target.identity_evidence = list({
            (entry.chapter_index, entry.quote): entry
            for entry in [*target.identity_evidence, *candidate.onstage_evidence]
        }.values())[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE]
    # 并不到别人身上的称呼，改问它自己：全书里它指一个人还是一类人。答不出来才
    # 退回保守判据（比名单里最常见的还常见才留），三档都不放行时按泛称丢弃。
    scopes = await asyncio.gather(*(
        _roster_appellation_scope(item, chapters_by_idx, project_id=project_id)
        for item in unmerged
    ))
    for candidate, scope in zip(unmerged, scopes, strict=True):
        if scope == "one_person":
            kept.append(candidate)
        elif scope == "many_people":
            continue
        elif _roster_candidate_stands_alone(candidate, specific, chapters_by_idx):
            kept.append(candidate)
    return kept
