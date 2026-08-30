"""角色点名——候选是否为真实人物（非代称/非群体）的裁决。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, model_validator

from app.harness import model_gateway
from app.source_excerpt import (
    index_source_segments,
)

from .alias_backfill import _roster_presence_dossier
from .bible_shared import _bible_short_json_call_meta
from .common import BIBLE_SMALL_VERDICT_TIMEOUT_S
from .constants import SYSTEM_PREFIX
from .roster_candidates import (
    _RosterCandidate,
    _candidate_appellations,
    _normalize_roster_verdict_payload,
    _require_explicit_verdict,
)


class _RosterPersonhoodResolution(BaseModel):
    """点名候选是不是可定妆的人物。三态由问题结构决定，不枚举器物/野兽名单。"""

    verdict: Literal["person", "non_person", "uncertain"] = "uncertain"
    supporting_chapter_index: int = -1
    name_form: Literal[
        "personal_name", "honorific", "referential", "uncertain",
    ] = "uncertain"

    @model_validator(mode="before")
    @classmethod
    def _require_verdict(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = _normalize_roster_verdict_payload(value)
        return _require_explicit_verdict(value)


class _RosterTrueNameResolution(BaseModel):
    """发现窗口之后原文是否揭示了这个称呼对应的正式姓名。"""

    verdict: Literal["revealed", "unrevealed", "uncertain"] = "uncertain"
    true_name: str = ""
    supporting_chapter_index: int = -1
    supporting_quote: str = ""

    @model_validator(mode="before")
    @classmethod
    def _require_verdict(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = _normalize_roster_verdict_payload(value)
        return _require_explicit_verdict(value)


BIBLE_PERSONHOOD_DOSSIER_SEGMENTS = 12


def _roster_personhood_dossier(
    candidate: _RosterCandidate, chapters_by_idx: dict[int, str],
) -> list[dict[str, Any]]:
    """资格卷宗必须含候选称呼本身。真实故障：孟浩的在场引句扩成「孟兄」段，
    模型据此判 uncertain，主角从人物谱消失。

    段落跨整个窗口取样而不是取最靠前的几段：判「这是人还是器物」要看它在不同
    场合怎么被使用，只给开头连着的几段，模型看到的可能全是同一个场景。
    """
    blocks: list[dict[str, Any]] = []
    names = [value for value in _candidate_appellations(candidate) if value]
    if not names:
        return blocks

    def _add(item: dict[str, Any]) -> bool:
        text = item.get("text") or ""
        if not any(name in text for name in names):
            return False
        if item not in blocks:
            blocks.append(item)
        return len(blocks) >= BIBLE_PERSONHOOD_DOSSIER_SEGMENTS

    for evidence in candidate.onstage_evidence[:3]:
        chapter_text = chapters_by_idx.get(evidence.chapter_index, "")
        for item in _roster_presence_dossier(
            evidence.chapter_index, chapter_text, evidence.quote,
        ):
            if _add(item):
                return blocks
    for item in _spread_named_segments(
        names, chapters_by_idx, limit=BIBLE_PERSONHOOD_DOSSIER_SEGMENTS,
    ):
        if _add(item):
            return blocks
    return blocks


def _named_hit_chapters(names: list[str], chapters_by_idx: dict[int, str]) -> list[int]:
    anchors = [value.strip() for value in names if value and value.strip()]
    if not anchors:
        return []
    return [
        chapter_idx for chapter_idx in sorted(chapters_by_idx)
        if any(anchor in (chapters_by_idx.get(chapter_idx) or "") for anchor in anchors)
    ]


def _spread_named_segments(
    names: list[str], chapters_by_idx: dict[int, str], *, limit: int,
    segment_max_chars: int = 240, offset: int = 0,
) -> list[dict[str, Any]]:
    """检索含这些称呼的原文段，跨全部章节交错取样。

    程序只负责把上下文找齐给模型，不在这里做任何关于这个人的判断。命中章按
    固定步长挑选，offset 让相邻的几批取到互不重叠的章，多批合起来就能把
    跨度铺满——一个只在某一章交代的身份，靠单批均匀取样很容易正好被跳过。
    """
    anchors = [value.strip() for value in names if value and value.strip()]
    if not anchors or limit <= 0:
        return []
    hit_chapters = _named_hit_chapters(anchors, chapters_by_idx)
    if not hit_chapters:
        return []
    if len(hit_chapters) > limit:
        stride = max(1, math.ceil(len(hit_chapters) / limit))
        hit_chapters = hit_chapters[offset % stride::stride][:limit]
    elif offset:
        return []
    blocks: list[dict[str, Any]] = []
    for chapter_idx in hit_chapters:
        chapter_text = chapters_by_idx.get(chapter_idx) or ""
        for index, segment in enumerate(
            index_source_segments(chapter_text, max_chars=segment_max_chars)
        ):
            if not any(anchor in segment.text for anchor in anchors):
                continue
            blocks.append({
                "chapter_idx": chapter_idx,
                "segment_index": index + 1,
                "text": segment.text,
            })
            break
        if len(blocks) >= limit:
            break
    return blocks


async def _filter_non_person_roster_candidates(
    candidates: list[_RosterCandidate],
    chapters_by_idx: dict[int, str],
    *,
    project_id: str | None = None,
) -> list[_RosterCandidate]:
    """铜镜、没有自己姓名的野兽、一次性描述不是人物谱角色。

    建卡用延迟绑定：只有明确 non_person 才丢掉；uncertain 先留着，交给真名/在场闸。
    """

    async def _judge(candidate: _RosterCandidate) -> _RosterCandidate | None:
        label = (candidate.formal_name or candidate.primary_appellation).strip()
        dossier = _roster_personhood_dossier(candidate, chapters_by_idx)
        if not label:
            return None
        if not dossier:
            return candidate.model_copy(update={"personhood": "uncertain"})
        catalog = "\n\n".join(
            f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
            for item in dossier
        )
        valid_chapters = {int(item["chapter_idx"]) for item in dossier}
        names = sorted(_candidate_appellations(candidate))
        prompt = f"""任务：判断「{label}」是不是可单独指认、能画定妆照的人物。
同一人物的其它称呼：{json.dumps(names, ensure_ascii=False)}。这些称呼指向同一个人时，按一个人判断。

原文卷宗：
{catalog}

只根据卷宗原文判断，JSON 字段必须用 verdict：
- person：卷宗里这个称呼指一个能说话或行动的人物，有稳定身份，可以作为人物谱角色。
- non_person：这个称呼指器物、法宝、地点、组织、没有自己姓名的野兽，或无法对应到具体人名的一次性描述。
- uncertain：卷宗不够，还不能决定。证据不足时选 uncertain，不要猜。

同时用 name_form 说明「{label}」这个写法本身是哪一种称呼形态：
- personal_name：人物的姓名，包括姓名、单名、以及被当作固定名字使用的绰号。
- honorific：姓氏或关系加上称呼，例如某某师姐、某某爷，指人但不是姓名。
- referential：靠外形、衣着、年龄、身份或方位来指人的说法，换个场合就可能指别人。
- uncertain：卷宗不足以判断这个写法属于哪一种。

supporting_chapter_index 必须是卷宗里出现过的数字章号，例如 1，不要写「第1章」或数组。不得根据常识或作品知识补充。
"""
        try:
            resolution = await asyncio.wait_for(
                model_gateway.chat_structured(
                    [{"role": "system", "content": SYSTEM_PREFIX},
                     {"role": "user", "content": prompt}],
                    model_type=_RosterPersonhoodResolution,
                    validate=None,
                    normalize_payload=_normalize_roster_verdict_payload,
                    operation_id="character_personhood:" + hashlib.sha256(
                        f"{label}:{catalog}".encode("utf-8")
                    ).hexdigest(),
                    temperature=0.0,
                    max_tokens=256,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "人物候选资格",
                        "stage_key": "character_personhood",
                        "call_role": "stage_validate",
                        "character_name": label,
                        "project_id": project_id,
                    }),
                ),
                timeout=BIBLE_SMALL_VERDICT_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - 不确定就延迟绑定，不从名单抹掉
            return candidate.model_copy(update={"personhood": "uncertain"})
        chapter_ok = resolution.supporting_chapter_index in valid_chapters
        name_in_dossier = any(
            any(name in (item.get("text") or "") for name in names)
            for item in dossier
        )
        evidence_ok = chapter_ok or name_in_dossier
        if resolution.verdict == "non_person" and evidence_ok:
            return None
        name_form = resolution.name_form if evidence_ok else "uncertain"
        if resolution.verdict == "person" and evidence_ok:
            return candidate.model_copy(update={
                "personhood": "person", "name_form": name_form,
            })
        return candidate.model_copy(update={
            "personhood": "uncertain", "name_form": name_form,
        })

    judged = await asyncio.gather(*(_judge(item) for item in candidates))
    return [item for item in judged if item is not None]
