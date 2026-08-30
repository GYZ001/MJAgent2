"""角色点名——候选模型定义与称谓/在场证据的基础判据。"""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator



class _RosterOnstageEvidence(BaseModel):
    """一条「本人在场」证据申报：模型只负责申报，是否真的成立由后端结构闸 +
    独立裁决闸核验（见 `_recurring_character_names` docstring）。"""

    chapter_index: int = -1
    quote: str = ""


class _RosterCandidate(BaseModel):
    """人物点名候选；aliases/identity_evidence 让跨章归一可以晚于首次点名完成。

    personhood 是建卡资格，不是最终身份：person 可建卡，non_person 明确不建卡，
    uncertain 延迟绑定——先留着称呼，等真名/在场证据，而不是直接从名单抹掉。
    """

    primary_appellation: str = ""
    formal_name: str = ""
    aliases: list[str] = Field(default_factory=list)
    identity_evidence: list[_RosterOnstageEvidence] = Field(default_factory=list)
    onstage_evidence: list[_RosterOnstageEvidence] = Field(default_factory=list)
    personhood: Literal["person", "non_person", "uncertain"] = "uncertain"
    # 称呼形态由资格裁决里的模型判定，程序不查词表。referential 这类只描述外形或
    # 身份的代称不能自己建卡，要先由身份归一裁决认领到某个人身上。
    name_form: Literal[
        "personal_name", "honorific", "referential", "uncertain",
    ] = "uncertain"


class _CharacterRollCall(BaseModel):
    """人物点名合同：候选 + 在场证据，不再只是名字字符串。"""

    candidates: list[_RosterCandidate] = Field(default_factory=list)


class _RosterIdentityResolution(BaseModel):
    """描述性称呼与候选实体的局部消歧结果。"""

    verdict: Literal["same", "different", "uncertain"] = "uncertain"
    canonical_appellation: str = ""
    supporting_chapter_index: int = -1


class _RosterAppellationScope(BaseModel):
    """一个代称在全书里指的是一个人，还是不同场合指不同的人。"""

    verdict: Literal["one_person", "many_people", "uncertain"] = "uncertain"
    supporting_chapter_index: int = -1

    @model_validator(mode="before")
    @classmethod
    def _require_verdict(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = _normalize_roster_verdict_payload(value)
        return _require_explicit_verdict(value)


class _MentionedCharacterImportanceResolution(BaseModel):
    """仅被提及角色是否值得进入人物谱的证据裁决。"""

    verdict: Literal["retain", "drop", "uncertain"] = "uncertain"
    supporting_chapter_index: int = -1
    reason: str = ""


def _shared_appellations(candidates: list["_RosterCandidate"]) -> set[str]:
    """本次点名里被多个不同候选共用的称呼，不能当个体标识。

    判据从输入推导，不枚举「少年/胖子/弟子」这类词表——类别词是开放集合，
    穷举不完，而且同一个词在别的作品里可能就是某个人的固定绰号。一个称呼
    只要被两个以上候选各自申报，它在这本书里就分不出人，只能送模型消歧。
    """
    owners: dict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        owner = (candidate.primary_appellation or "").strip()
        if not owner:
            continue
        for name in _candidate_appellations(candidate):
            owners[name].add(owner)
    return {name for name, group in owners.items() if len(group) > 1}


def _is_composite_appellation(value: str, known_names: set[str]) -> bool:
    """带属格的组合指称（「X 的 Y」）指向的是关系，不是稳定人物身份。

    「的」是汉语属格标记，属于语法结构而不是某本书的词表；这里只判结构，
    是不是同一个人交给身份归一裁决。
    """
    text = (value or "").strip()
    if not text or "的" not in text:
        return False
    if any(name and name != text and name in text for name in known_names):
        return True
    return text.index("的") > 0 and text.index("的") < len(text) - 1


def _roster_label_needs_identity_resolution(
    candidate: "_RosterCandidate", known_names: set[str], ambiguous: set[str],
) -> bool:
    """要不要送身份归一：形态由模型裁决，剩下两条判据从本次点名数据推导。"""
    text = (candidate.formal_name or candidate.primary_appellation or "").strip()
    return (
        candidate.name_form == "referential"
        or text in ambiguous
        or _is_composite_appellation(text, known_names)
    )


def _roster_appellation_mentions(
    candidate: "_RosterCandidate", chapters_by_idx: dict[int, str],
) -> int:
    """这个候选的全部称呼在原文里的合计命中数。"""
    terms = _candidate_appellations(candidate)
    if not terms:
        return 0
    return sum(text.count(term) for text in chapters_by_idx.values() for term in terms)


def _roster_candidate_stands_alone(
    candidate: "_RosterCandidate",
    specific: list["_RosterCandidate"],
    chapters_by_idx: dict[int, str],
) -> bool:
    """`_roster_appellation_scope` 也答不出来时的兜底：这个称呼是不是「比名单里
    最常见的那个还常见」。

    真实故障（《王六郎》proj_177d147e16c7）：主角「许某」全篇提及 34 次、自报 3 条
    在场证据全部通过结构闸，被归一裁决拿去和「王六郎/异史氏」比对判 uncertain 后
    整个丢弃，必收名单只剩 1 人；人物谱里那张「许」卡是主生成模型事后自造的单字名，
    拿它做子串检索会命中「也许」「许多」「许姓」。

    判据取「高于 specific 里的最大提及数」，而不是任何绝对次数门槛。绝对门槛在
    这里必然失效：类别称谓在长篇里比真配角出现得更多——《我欲封天》1616 章语料中
    「绿袍男子」498 次 / 覆盖 138 章、「精明男子」503 次，都远高于真角色「王有材」
    的 58 次，任何够低到能救《王六郎》许某（34 次）的门槛都会把它们一并放回来。
    相对位置才分得开：许某比名单里最常见的「王六郎」（25 次）还常见，只可能是被
    误删的主角；绿袍男子相对孟浩的 55137 次差三个数量级，仍是类别称谓。

    这条通道刻意保守，只救「比主角还常见却被整个删掉」这一种极端情形——规模不及
    主角的第二主角（《罗刹海市》龙女 20 次 vs 马骥 95 次）救不回来，那种情形归
    `_roster_appellation_scope` 管，靠读原文而不是数次数。比较取严格大于：平局在
    小样本里太廉价（两个各出现一次的称呼谁也不比谁常见），放行平局等于把闸常开。
    """
    if not specific:
        return False
    mentions = _roster_appellation_mentions(candidate, chapters_by_idx)
    if mentions <= 0:
        return False
    strongest = max(
        _roster_appellation_mentions(item, chapters_by_idx) for item in specific
    )
    return mentions > strongest


def _candidate_appellations(candidate: _RosterCandidate) -> set[str]:
    return {
        value.strip() for value in [
            candidate.primary_appellation, candidate.formal_name, *candidate.aliases,
        ] if value and value.strip()
    }


def _coerce_roster_chapter_index(value: Any) -> int:
    """程序拥有章号：模型常写「第1章」、[1]、['1']，不能因此把已判定的 person 打成 uncertain。"""
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value if value > 0 else -1
    if isinstance(value, float) and value.is_integer():
        return int(value) if value > 0 else -1
    if isinstance(value, list) and value:
        return _coerce_roster_chapter_index(value[0])
    if isinstance(value, str):
        match = re.search(r"(\d+)", value.strip())
        if match:
            number = int(match.group(1))
            return number if number > 0 else -1
    return -1


def _normalize_roster_verdict_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """模型常把 verdict 写成 judge_result/判断结果；缺字段时不能默成 uncertain 再淘汰。"""
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    if "verdict" not in normalized:
        for key in ("judge_result", "result", "判断结果", "reveal_status"):
            if key in normalized:
                normalized["verdict"] = normalized[key]
                break
    if "supporting_chapter_index" in normalized:
        normalized["supporting_chapter_index"] = _coerce_roster_chapter_index(
            normalized["supporting_chapter_index"],
        )
    return normalized


def _require_explicit_verdict(payload: Any) -> Any:
    if isinstance(payload, dict) and "verdict" not in payload:
        raise ValueError("verdict is required")
    return payload


def _pin_roster_name_to_source(
    name: str, texts: list[str], *, fallback_texts: list[str] | None = None,
) -> str:
    """名称必须钉在原文上：逐字命中优先；仅当证据文本没有该写法时，才允许唯一的一字之差。"""
    text_name = (name or "").strip()
    if not text_name:
        return ""
    if any(text_name in (text or "") for text in texts):
        return text_name
    length = len(text_name)
    if length >= 2:
        found: list[str] = []
        for text in texts:
            body = text or ""
            for index in range(0, max(0, len(body) - length + 1)):
                span = body[index:index + length]
                if not all("\u4e00" <= char <= "\u9fff" for char in span):
                    continue
                if sum(left != right for left, right in zip(text_name, span, strict=True)) == 1:
                    found.append(span)
        unique = list(dict.fromkeys(found))
        if len(unique) == 1:
            return unique[0]
    if fallback_texts and any(text_name in (text or "") for text in fallback_texts):
        return text_name
    return ""


def _candidate_source_texts(
    candidate: _RosterCandidate, chapters_by_idx: dict[int, str],
) -> list[str]:
    texts: list[str] = []
    for evidence in [*candidate.onstage_evidence, *candidate.identity_evidence]:
        quote = (evidence.quote or "").strip()
        if quote:
            texts.append(quote)
        chapter_text = chapters_by_idx.get(evidence.chapter_index, "")
        if chapter_text:
            texts.append(chapter_text)
    return texts


def _pin_roster_candidates_to_source(
    candidates: list[_RosterCandidate],
    chapters_by_idx: dict[int, str],
) -> list[_RosterCandidate]:
    """程序拥有名称匹配：模型申报的称呼若钉不进原文，就不能建卡。"""
    pinned: list[_RosterCandidate] = []
    for candidate in candidates:
        texts = _candidate_source_texts(candidate, chapters_by_idx)
        chapter_texts = list(chapters_by_idx.values())
        primary = _pin_roster_name_to_source(
            candidate.primary_appellation, texts, fallback_texts=chapter_texts,
        )
        if not primary:
            continue
        formal = _pin_roster_name_to_source(
            candidate.formal_name, texts, fallback_texts=chapter_texts,
        )
        aliases = []
        for alias in candidate.aliases:
            pinned_alias = _pin_roster_name_to_source(
                alias, texts, fallback_texts=chapter_texts,
            )
            if pinned_alias and pinned_alias not in {primary, formal, *aliases}:
                aliases.append(pinned_alias)
        pinned.append(candidate.model_copy(update={
            "primary_appellation": primary,
            "formal_name": "" if not formal or formal == primary else formal,
            "aliases": aliases,
        }))
    return pinned


def _identity_merge_keys(candidate: _RosterCandidate) -> set[str]:
    """连通分量的候选键就是这个候选自己申报的全部称呼。

    这里不做「哪些词算类别词」的过滤——那需要词表，而词表是开放集合。
    合并的真正约束在下面：共享称呼必须是某一方的主称呼，且两人要在原文里
    共现得上；分不出人的称呼由后面的身份归一裁决交给模型判。
    """
    return {value for value in _candidate_appellations(candidate) if value}
