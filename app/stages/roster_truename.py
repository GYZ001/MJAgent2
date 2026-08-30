"""角色点名——候选真名揭示裁决与同名冲突消解。"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any


from app.harness import model_gateway

from .bible_shared import _bible_short_json_call_meta, _chapters_by_idx
from .common import BIBLE_SMALL_VERDICT_TIMEOUT_S
from .constants import SYSTEM_PREFIX
from .roster_candidates import _RosterCandidate, _normalize_roster_verdict_payload
from .roster_personhood import _RosterTrueNameResolution, _spread_named_segments


BIBLE_TRUE_NAME_DOSSIER_SEGMENTS = 12
BIBLE_TRUE_NAME_DOSSIER_BATCHES = 4


def _roster_true_name_dossier_batches(
    names: list[str], chapters_by_idx: dict[int, str],
    *, limit: int = BIBLE_TRUE_NAME_DOSSIER_SEGMENTS,
    batches: int = BIBLE_TRUE_NAME_DOSSIER_BATCHES,
) -> list[list[dict[str, Any]]]:
    """真名裁决的卷宗，切成几批互不重叠的取样。

    姓名往往只在一两章里交代过，一本上千章的书按单批均匀取样几乎必然跳过
    那一章（真实故障：「许师姐→许清」的揭示在第 37 章，八段取样落在 29 和 70
    之间）。分批交错让模型有机会读到跨度里的其它章，读到就停，不必跑满。
    """
    return [
        batch for offset in range(max(1, batches))
        if (batch := _spread_named_segments(
            names, chapters_by_idx, limit=limit, offset=offset,
        ))
    ]


async def _discover_roster_true_names(
    candidates: list[_RosterCandidate],
    chapters: list[dict],
    *,
    project_id: str | None = None,
) -> list[_RosterCandidate]:
    """由模型读原文卷宗决定每个称呼的正式姓名，程序只做检索与钉证。

    点名模型顺手填的 formal_name 也要在这里复核：它可能把身边的物件或半句话
    当成姓名（真实故障：「王腾飞」的真名被写成「这阵法」）。复核对不上就退回
    称呼，宁可没有真名，也不让一个不是名字的串当主名。

    真实事故：许清在第 34 章才以真名出现，前 20 章点名只收到「许师姐」，全文检索
    因此只数到两百次尊称，女主角被标成低频配角。
    """
    chapters_by_idx = _chapters_by_idx(chapters)

    async def _judge_batch(
        candidate: _RosterCandidate, anchors: list[str], dossier: list[dict[str, Any]],
    ) -> _RosterCandidate | None:
        """一批卷宗的裁决：钉证过了就返回带真名的候选，否则 None 让上层换下一批。"""
        appellation = (candidate.primary_appellation or "").strip()
        catalog = "\n\n".join(
            f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
            for item in dossier
        )
        valid_chapters = {int(item["chapter_idx"]) for item in dossier}
        prompt = f"""任务：判断后文是否揭示了「{appellation}」的正式姓名。

原文卷宗：
{catalog}

只根据卷宗原文判断，JSON 字段必须用 verdict：
- revealed：卷宗里出现了这个人的正式姓名，true_name 必须逐字抄自卷宗连续原文。
- unrevealed：卷宗没有揭示正式姓名。
- uncertain：证据不够，不要猜一个名字。

supporting_chapter_index 必须是卷宗里出现过的章号；supporting_quote 必须是该章原文逐字引句。
不得根据常识或作品知识补一个名字。
"""
        try:
            resolution = await asyncio.wait_for(
                model_gateway.chat_structured(
                    [{"role": "system", "content": SYSTEM_PREFIX},
                     {"role": "user", "content": prompt}],
                    model_type=_RosterTrueNameResolution,
                    validate=None,
                    normalize_payload=_normalize_roster_verdict_payload,
                    operation_id="character_true_name:" + hashlib.sha256(
                        f"{appellation}:{catalog}".encode("utf-8")
                    ).hexdigest(),
                    temperature=0.0,
                    max_tokens=512,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "人物真名揭示",
                        "stage_key": "character_true_name",
                        "call_role": "stage_validate",
                        "character_name": appellation,
                        "project_id": project_id,
                    }),
                ),
                timeout=BIBLE_SMALL_VERDICT_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - 这批没跑成就换下一批，别把整个候选判死
            return None
        true_name = (resolution.true_name or "").strip()
        chapter_text = chapters_by_idx.get(resolution.supporting_chapter_index, "")
        quote = (resolution.supporting_quote or "").strip()
        occupied_elsewhere = {
            (item.primary_appellation or "").strip()
            for item in candidates
            if (item.primary_appellation or "").strip()
            and (item.primary_appellation or "").strip() != appellation
        }
        # 钉证的锚点是这个候选的任一已确认称呼，与检索卷宗时用的口径一致：
        # 卷宗段可能来自只写了绰号的那一章，硬要求主名逐字出现会把模型答对的
        # 真名判死（真实故障：「小胖子」的揭示章原文只写「胖子」）。
        if (
            resolution.verdict != "revealed"
            or not true_name
            or true_name in {*anchors, appellation}
            or true_name in occupied_elsewhere
            or resolution.supporting_chapter_index not in valid_chapters
            or true_name not in chapter_text
            or not any(anchor in chapter_text for anchor in anchors)
            or (quote and quote not in chapter_text)
            or (quote and true_name not in quote)
        ):
            return None
        aliases = list(dict.fromkeys([*candidate.aliases, appellation]))
        return candidate.model_copy(update={
            "formal_name": true_name,
            "aliases": [value for value in aliases if value and value != true_name],
            "personhood": "person",
        })

    async def _discover(candidate: _RosterCandidate) -> _RosterCandidate:
        appellation = (candidate.primary_appellation or "").strip()
        if not appellation:
            return candidate
        claimed = (candidate.formal_name or "").strip()
        # 点名申报过真名却一批都没复核过，说明那个串没被原文证明是这个人的姓名。
        unconfirmed = candidate.model_copy(update={"formal_name": ""}) if claimed else candidate
        anchors = [value for value in dict.fromkeys([appellation, *candidate.aliases]) if value]
        batches = _roster_true_name_dossier_batches(anchors, chapters_by_idx)
        for dossier in batches:
            resolved = await _judge_batch(candidate, anchors, dossier)
            if resolved is not None:
                return resolved
        return unconfirmed

    return list(await asyncio.gather(*(_discover(item) for item in candidates)))


def _resolve_conflicting_formal_names(
    candidates: list[_RosterCandidate],
) -> list[_RosterCandidate]:
    """同一个真名被两个候选同时申报时，至少有一个是错的。

    只有主名本身就是这个真名的候选能留住它；一个都没有就两边都清空，
    宁可退回称呼，也不要把两个人并成同一张卡。
    """
    by_formal: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        formal = (candidate.formal_name or "").strip()
        if formal:
            by_formal.setdefault(formal, []).append(index)
    drop: set[int] = set()
    for formal, indices in by_formal.items():
        if len(indices) < 2:
            continue
        keep = {
            index for index in indices
            if (candidates[index].primary_appellation or "").strip() == formal
        }
        drop.update(index for index in indices if index not in keep)
    resolved: list[_RosterCandidate] = []
    for index, candidate in enumerate(candidates):
        if index not in drop:
            resolved.append(candidate)
            continue
        resolved.append(candidate.model_copy(update={"formal_name": ""}))
    return resolved
