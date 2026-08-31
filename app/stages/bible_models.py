"""人物谱生成——名单草稿模型、角色详情证据包组装与名单校验。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas import (AppearanceEvidence, Character, CharacterAlias,
                         Relationship, World)

from .bible_paratext import BIBLE_DETAIL_EVIDENCE_MAX_CHARS, BIBLE_DETAIL_EVIDENCE_MAX_SEGMENTS
from .common import BIBLE_STATISTICAL_MIN_MENTIONS
from .roster_personhood import _spread_named_segments
from .roster_recurring import _pick_canonical_display_name


class _BibleRosterEntry(BaseModel):
    name: str
    role: str
    source_appellations: list[str] = Field(default_factory=list)
    # source_appellations 里哪几项只是点名模型顺口报的别名，没有经过任何核验。
    # 其余各项（primary_appellation / formal_name / 被降级的那个显示名）是这个
    # 候选赖以进入必收名单的身份标识本身——在场证据逐条过了结构闸、独立裁决闸和
    # 段号钉证，名单成立就意味着它们成立。candidate.aliases 没有这层保证：点名
    # 提示词允许模型随手申报，代码一路没有核对过它们指的是不是同一个人。
    # 检索用途（证据包召回、详情提示词的"原文称呼"）照旧吃全集，宽一点无害；
    # 只有"登记进人物谱 aliases"这一步必须把两者分开，见
    # _attach_roster_source_appellations。
    unverified_appellations: list[str] = Field(default_factory=list)
    presence_status: Literal["onstage", "mentioned_only"] = "onstage"
    importance_score: float = 0.0
    importance_signals: list[str] = Field(default_factory=list)
    portrait_eligible: bool = True
    appearance_status: Literal["grounded", "insufficient_evidence", "deferred"] = "grounded"


class _BibleRosterDraft(BaseModel):
    characters: list[_BibleRosterEntry] = Field(default_factory=list)
    world: World


class _CharacterDetail(BaseModel):
    appearance_canonical: str
    period_costume_canonical: str = ""
    personality: str = ""
    speech_style: str = ""
    relationships: list[Relationship] = Field(default_factory=list)
    aliases: list[CharacterAlias] = Field(default_factory=list)
    source_evidence: list[AppearanceEvidence] = Field(default_factory=list)


def _sanitize_character_detail_payload(payload: dict) -> dict:
    """丢掉缺证据锚点的别名/外观证据，不让整条角色详情校验失败。

    真实事故：孟浩详情三次都因 aliases[].evidence_chapter_index=null 整单作废，
    随后被 `_generate_character_detail_batch` 从名单静默删除，人物谱里没有主角。
    别名合同是「不确定不登记」，缺锚点应丢那一条，不是拒绝这个人。
    """
    data = dict(payload)
    aliases = data.get("aliases")
    if isinstance(aliases, list):
        data["aliases"] = [
            item for item in aliases
            if isinstance(item, dict)
            and item.get("evidence_chapter_index") is not None
            and str(item.get("text") or "").strip()
            and str(item.get("evidence_quote") or "").strip()
        ]
    evidence = data.get("source_evidence")
    if isinstance(evidence, list):
        data["source_evidence"] = [
            item for item in evidence
            if isinstance(item, dict)
            and item.get("evidence_chapter_index") is not None
            and str(item.get("evidence_quote") or "").strip()
        ]
    return data


def _character_stub_from_roster(entry: _BibleRosterEntry) -> Character:
    """名单已锁定时，详情失败也要留下这个人，外观留空待补，不编造长相。"""
    return Character(
        name=entry.name,
        role=entry.role,
        appearance_canonical="外观待补全，详情生成未通过校验，当前不自动定妆",
        personality="",
        speech_style="",
        relationships=[],
        aliases=[],
        source_evidence=[],
        presence_status=entry.presence_status,
        importance_score=entry.importance_score,
        importance_signals=entry.importance_signals,
        portrait_eligible=False,
        appearance_status="insufficient_evidence",
        period_costume_canonical="待详情通过后再依据年代与身份补全",
    )


def _character_detail_evidence_pack(
    chapters: list[dict], appellations: list[str], *, max_chars: int = BIBLE_DETAIL_EVIDENCE_MAX_CHARS,
) -> str:
    """给一个角色检索有界的原文卷宗：命中章跨全书取样，不是只取最靠前的几段。

    只取最前面的命中段，模型看到的会全是这个人早期的固定称呼；真名揭示、性别
    交代、外貌描写往往在后文，取不到就只能靠猜。这里只负责把上下文找齐，
    外貌和性别怎么写全部由模型依据这些原文决定。
    """
    anchors = [value.strip() for value in appellations if value and value.strip()]
    selected: list[str] = []
    if anchors:
        chapters_by_idx: dict[int, str] = {}
        for chapter in chapters:
            content = (chapter.get("content") or "").strip()
            if not content:
                continue
            try:
                chapters_by_idx[int(chapter.get("idx"))] = content
            except (TypeError, ValueError):
                continue
        for item in _spread_named_segments(
            anchors, chapters_by_idx,
            limit=BIBLE_DETAIL_EVIDENCE_MAX_SEGMENTS, segment_max_chars=800,
        ):
            block = f"【第{item['chapter_idx']}章·证据】\n{item['text'].strip()}"
            if sum(len(value) for value in selected) + len(block) > max_chars:
                break
            selected.append(block)
    if selected:
        return "\n\n".join(selected)
    # No lexical hit: bounded fallback only, never the 60K source corpus.
    for chapter in chapters[:3]:
        content = (chapter.get("content") or "").strip()
        if not content:
            continue
        block = f"【第{chapter.get('idx', '?')}章·有限背景】\n{content[:1200]}"
        if sum(len(item) for item in selected) + len(block) > max_chars:
            break
        selected.append(block)
    return "\n\n".join(selected)


def _normalize_must_cover_rows(
    rows: list[tuple],
) -> list[tuple[str, str, int, int, int, list[str]]]:
    """兼容旧三元组调用方；生产路径使用带全文统计与别名的六元组。"""
    normalized: list[tuple[str, str, int, int, int, list[str]]] = []
    for row in rows:
        if len(row) >= 6:
            appellation, formal, onstage, mentions, chapters, aliases = row[:6]
        else:
            appellation, formal, onstage = row[:3]
            mentions, chapters, aliases = onstage, 1, []
        normalized.append((
            str(appellation), str(formal), int(onstage), int(mentions), int(chapters),
            [str(alias) for alias in aliases if str(alias).strip()],
        ))
    return normalized


def _character_importance_metadata(
    onstage: int, mentions: int, chapters: int,
) -> tuple[float, list[str]]:
    """把独立 Harness 信号压成可解释分数；准入仍由证据门禁决定，不由分数单独决定。"""
    score = min(100.0, onstage * 22.0 + min(chapters, 12) * 4.0 + min(mentions, 30) * 0.8)
    signals = [f"verified_onstage:{onstage}", f"fulltext_mentions:{mentions}", f"chapter_coverage:{chapters}"]
    return round(score, 1), signals


def _normalize_roster_against_candidates(
    draft: _BibleRosterDraft,
    must_cover: list[tuple[str, str, int, int, int, list[str]]],
    chapters: list[dict] | None = None,
) -> _BibleRosterDraft:
    """代码拥有名单最终权：模型只分配 role，不得拆人、改名或漏人。"""
    if not must_cover:
        return draft
    model_entries = list(draft.characters)
    normalized: list[_BibleRosterEntry] = []
    for appellation, formal, onstage, mentions, chapters_hit, aliases in must_cover:
        if chapters:
            canonical, demoted = _pick_canonical_display_name(appellation, formal, chapters)
        else:
            canonical, demoted = (formal or appellation), ([appellation] if formal and formal != appellation else [])
        # 真名与绰号都留在检索键里：下游遇到任一称呼都要能映射回这一个角色的定妆图。
        source_names = [
            name for name in dict.fromkeys([appellation, formal, *demoted, *aliases])
            if name and name != canonical
        ]
        all_names = {canonical, appellation, formal, *aliases} - {""}
        matched = next((
            item for item in model_entries
            if item.name in all_names or bool(set(item.source_appellations) & all_names)
        ), None)
        score, signals = _character_importance_metadata(onstage, mentions, chapters_hit)
        # onstage=0 只说明"没有一条引句通过单次模型裁决"，不等于这个人没出场。
        # 全文命中量达标时按已出场处理：主角引句叙述密集，裁决闸判 other 的概率
        # 反而更高，若据此判成 mentioned_only，主角和高频配角会被踢出定妆。
        # 章节覆盖率只用于点名窗口准入，不得拿全书章数做分母——1616 章小说的
        # 0.15 会要求覆盖 242 章，王腾飞这种反派会被标成仅提及、不给定妆。
        statistically_present = mentions >= BIBLE_STATISTICAL_MIN_MENTIONS
        mentioned_only = onstage == 0 and not statistically_present
        if onstage == 0 and statistically_present:
            signals = signals + ["presence_by_fulltext_coverage"]
        normalized.append(_BibleRosterEntry(
            name=canonical,
            role=(matched.role if matched else ("关键伏笔角色" if mentioned_only else "重要配角")),
            source_appellations=source_names,
            unverified_appellations=[
                name for name in source_names
                if name in set(aliases) and name not in {appellation, formal, *demoted}
            ],
            presence_status="mentioned_only" if mentioned_only else "onstage",
            importance_score=score,
            importance_signals=signals + (["retained_by_plot_authority"] if mentioned_only else []),
            portrait_eligible=True,
            appearance_status="grounded",
        ))
    draft.characters = normalized
    _assign_protagonist_by_signals(draft)
    return draft


def _assign_protagonist_by_signals(draft: _BibleRosterDraft) -> None:
    """主角由统计信号确定性指派，不交给模型自由发挥。

    must_cover 已按「章节覆盖 → 全文命中」降序排好，排在最前且真实出场的角色就是
    全书出现最广、最密的人物。真实故障：模型把只出现 1 次的「李富贵」标成主角，
    而覆盖 20/20 章、提及 991 次的孟浩连名单都没进。这里在名单定稿后统一改写 role，
    保证有且只有一个主角，且主角必然是统计上最核心的那个已出场角色。
    """
    onstage = [item for item in draft.characters if item.presence_status == "onstage"]
    if not onstage:
        return
    protagonist = onstage[0]
    for item in draft.characters:
        if item is protagonist:
            item.role = "主角"
        elif item.role == "主角":
            item.role = "重要配角"


def _validate_bible_roster(draft: _BibleRosterDraft) -> list[str]:
    names = [(item.name or "").strip() for item in draft.characters]
    errors: list[str] = []
    if not names:
        errors.append("characters 数量 0，要求至少 1 个")
    if any(not name for name in names):
        errors.append("characters.name 不能为空")
    if len(names) != len(set(names)):
        errors.append("characters.name 存在重复")
    if getattr(draft.world, "visual_style_canonical", None) is None:
        errors.append("world.visual_style_canonical 缺失")
    return errors
