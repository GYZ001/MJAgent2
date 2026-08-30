"""章节认知卡——按章节号确定性组装角色状态事实摘要视图。"""
from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from app.schemas import (Bible, Character, CharacterAffiliation, CharacterRelation)

from .identity_evidence import (
    CHAPTER_COGNITION_CARD_MAX_CHARACTERS,
    CHAPTER_COGNITION_FACTS_MAX_PER_KIND,
    CHAPTER_COGNITION_SUMMARY_MAX_CHARS,
)


class ChapterCognitionEntry(BaseModel):
    """认知卡单个角色条目（§4.2）：全部字段均为确定性拼装的只读展示值，不是新的存储
    字段——真实数据仍在 `Character.aliases`/`affiliations`/`relations`，这里只是按
    章节号 N 过滤/统计后的摘要视图。"""

    name: str                                              # 人物谱规范名
    matched_surface_forms: list[str] = Field(default_factory=list)  # 命中的称谓：规范名
    # 或已确认别名，逐字子串命中，零语义、不针对具体称谓特判（复用
    # `_alias_verdict_candidates` 的判据模式）
    affiliations_as_of: list[str] = Field(default_factory=list)  # 截至本章生效的归属摘要
    # （org + relation_kind 拼装只读字符串，供提示词展示，不是新的存储字段）
    relations_as_of: list[str] = Field(default_factory=list)  # 截至本章生效的关系摘要，
    # 拼装方式同上
    forward_appearance_hits: int = 0  # 前瞻窗口内该角色规范名/别名的逐字出现次数


class ChapterCognitionCard(BaseModel):
    """章级认知卡（§4.2）：同一 (bible 快照, chapter_idx, forward_window_chapters) 输入
    任何时候重建结果逐字节相同（§11 判据 2，机械回归测试）。"""

    chapter_idx: int                            # 本卡对应的原著章节序号（进度锚点）
    forward_window_chapters: int                # 本次使用的前瞻窗口大小 K，记账供审计复现
    present_characters: list[ChapterCognitionEntry] = Field(default_factory=list)


def _status_facts_as_of_chapter(
    entries: list[Any], chapter_idx: int, *, group_key: Callable[[Any], str],
) -> list[Any]:
    """状态事实"截至第 N 章"区间过滤 + 同对象最新一条优先（§4.2 point 2，与
    `character_portraits` 表 `ORDER BY ep_start DESC LIMIT 1` 的既有惯例同构，见
    `app/portraits.py` `portrait_for_episode`）：先筛出 `valid_from_chapter <=
    chapter_idx` 且（`valid_to_chapter` 为空或 `>= chapter_idx`）的条目；`group_key`
    相同的多条（同一归属对象 org、或同一关系对象 to）若区间重叠都满足，只保留
    `valid_from_chapter` 最大（最近生效）的一条——同一角色可以同时对不同 org/to 各有
    一条独立生效的事实，只有指向同一对象的多条才互相竞争。返回顺序按 `group_key`
    首次出现顺序（即 `entries` 原始顺序）排列，保证同一输入任何时候重建结果逐字节
    相同。"""
    valid = [
        item for item in entries
        if item.valid_from_chapter <= chapter_idx
        and (item.valid_to_chapter is None or item.valid_to_chapter >= chapter_idx)
    ]
    best: dict[str, Any] = {}
    order: list[str] = []
    for item in valid:
        key = group_key(item)
        if key not in best:
            order.append(key)
            best[key] = item
        elif item.valid_from_chapter > best[key].valid_from_chapter:
            best[key] = item
    return [best[key] for key in order]


def _cognition_affiliation_summary(item: CharacterAffiliation) -> str:
    """归属摘要拼装：纯字符串运算，不做任何语义判断。"""
    label = item.org + (f"（{item.relation_kind}）" if item.relation_kind else "")
    text = f"{label}，第{item.evidence_chapter_index}章证据"
    return text[:CHAPTER_COGNITION_SUMMARY_MAX_CHARS]


def _cognition_relation_summary(item: CharacterRelation) -> str:
    """关系摘要拼装：与 `_cognition_affiliation_summary` 同构，`org` 换成 `to`。"""
    label = item.to + (f"（{item.relation_kind}）" if item.relation_kind else "")
    text = f"{label}，第{item.evidence_chapter_index}章证据"
    return text[:CHAPTER_COGNITION_SUMMARY_MAX_CHARS]


def build_chapter_cognition_card(
    bible: Bible,
    chapters_by_idx: dict[int, str],
    chapter_idx: int,
    *,
    character_names: list[str] | None = None,
    forward_window_chapters: int | None = None,
) -> ChapterCognitionCard:
    """章级认知卡组装（§4.2）：代码零语义，纯字符串/区间运算，不发起模型调用。
    `character_names` 是需要纳入的角色范围，由调用方给定（本文件唯一调用点
    `_alias_evidence_resolution` 传入的是裁决闸已经结构性算出的候选集
    `_alias_verdict_candidates`）；缺省（`None`）时对 `bible.characters` 全量扫描
    （§4.2 point 1 "遍历 bible.characters"）。`Character.affiliations`/`relations`
    当前项目尚未真实回填过（均为空列表）时，本函数优雅退化为只含
    `matched_surface_forms`（无归属/关系摘要）的条目，不报错、不拒绝工作——见 §12
    "回滚"对这一退化路径的要求。同一 `(bible 快照, chapter_idx, forward_window_
    chapters)` 输入任何时候重建结果逐字节相同（§11 判据 2）。"""
    if forward_window_chapters is None:
        from app.portraits import CHARACTER_IMPORTANCE_FORWARD_CHAPTERS
        forward_window_chapters = CHARACTER_IMPORTANCE_FORWARD_CHAPTERS

    chapter_text = chapters_by_idx.get(chapter_idx, "")
    forward_text = "\n".join(
        chapters_by_idx[idx]
        for idx in range(chapter_idx + 1, chapter_idx + forward_window_chapters + 1)
        if idx in chapters_by_idx
    )
    wanted = set(character_names) if character_names is not None else None

    present: list[tuple[Character, list[str], list[str]]] = []
    for character in bible.characters:
        if wanted is not None and character.name not in wanted:
            continue
        surface_forms = [character.name, *(a.text for a in character.aliases if a.text)]
        matched = [form for form in surface_forms if form and form in chapter_text]
        if matched:  # 在场判定：规范名或已确认别名逐字命中本章原文（§4.2 point 1）
            present.append((character, surface_forms, matched))

    entries: list[ChapterCognitionEntry] = []
    # 确定性截断：按调用方给定范围内 bible.characters 的原始顺序取前
    # CHAPTER_COGNITION_CARD_MAX_CHARACTERS 个在场角色，不做二次排序。
    for character, surface_forms, matched in present[:CHAPTER_COGNITION_CARD_MAX_CHARACTERS]:
        affiliations_as_of = [
            _cognition_affiliation_summary(item)
            for item in _status_facts_as_of_chapter(
                character.affiliations, chapter_idx, group_key=lambda a: a.org,
            )
        ][:CHAPTER_COGNITION_FACTS_MAX_PER_KIND]
        relations_as_of = [
            _cognition_relation_summary(item)
            for item in _status_facts_as_of_chapter(
                character.relations, chapter_idx, group_key=lambda r: r.to,
            )
        ][:CHAPTER_COGNITION_FACTS_MAX_PER_KIND]
        forward_hits = (
            sum(forward_text.count(form) for form in surface_forms if form)
            if forward_text else 0
        )
        entries.append(ChapterCognitionEntry(
            name=character.name,
            matched_surface_forms=matched,
            affiliations_as_of=affiliations_as_of,
            relations_as_of=relations_as_of,
            forward_appearance_hits=forward_hits,
        ))
    return ChapterCognitionCard(
        chapter_idx=chapter_idx,
        forward_window_chapters=forward_window_chapters,
        present_characters=entries,
    )


def _cognition_status_lines(card: ChapterCognitionCard | None) -> list[str]:
    """把认知卡中"有归属或关系摘要"的角色条目渲染成提示词"候选人已知状态"文本块的
    逐行文本（§4.3）：只展示状态事实（affiliations_as_of/relations_as_of），不展示
    `forward_appearance_hits`——前瞻信号服务重要性判断（§3.3），与判别式提问无关，
    §9 P1 第 7 项才会消费它，本函数不注入到裁决闸提示词里。角色没有任何状态事实
    摘要时不出现在结果里；`card` 为 `None`，或全部在场角色都没有状态事实摘要（当前
    真实状态：`backfill_character_status_facts` 尚未真实跑过，`affiliations`/
    `relations` 均为空）时返回空列表，供调用方据此把整段"候选人已知状态"文本块省略，
    不留空标题、不留占位噪声。"""
    if card is None:
        return []
    lines: list[str] = []
    for entry in card.present_characters:
        facts: list[str] = []
        if entry.affiliations_as_of:
            facts.append("归属 " + "、".join(entry.affiliations_as_of))
        if entry.relations_as_of:
            facts.append("关系 " + "、".join(entry.relations_as_of))
        if facts:
            lines.append(f"- {entry.name}：" + "；".join(facts))
    return lines
