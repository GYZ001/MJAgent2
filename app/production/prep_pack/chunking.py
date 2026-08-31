"""Source-segment chunking for model calls (chunk sizing/rendering, the
segment-index structural gate, and known-name/chapter-title lookups used to
seed a chunk's prompt).

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

from app.source_excerpt import SourceSegment

from .contracts import _CHUNK_MAX_CHARS
from .discovery import _load_project_bible


def _chunk_segments(
    segments: list[SourceSegment], *, max_chars: int = _CHUNK_MAX_CHARS,
) -> list[list[tuple[int, SourceSegment]]]:
    """Group indexed segments into model-call-sized chunks (长章节切块)."""
    indexed = list(enumerate(segments, start=1))
    if not indexed:
        return []
    chunks: list[list[tuple[int, SourceSegment]]] = []
    current: list[tuple[int, SourceSegment]] = []
    current_chars = 0
    for item in indexed:
        _, segment = item
        segment_chars = len(segment.text)
        if current and current_chars + segment_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += segment_chars
    if current:
        chunks.append(current)
    return chunks


def _render_chunk(chunk: list[tuple[int, SourceSegment]]) -> str:
    return "\n\n".join(f"【{index}】\n{segment.text}" for index, segment in chunk)


# 段号结构闸（2.0.0，见 PREP_PACK_VERSION 上方 2.0.0 大注释"锚点从
# event_ids 换成 segment_indexes"一节）：一个提及（角色/场景/道具）自报的
# 每一个 segment_index，必须落在本次 chunk 自己的全局段号范围内——防止模型
# 把别的 chunk 的段号写到这里，每次 chunk 调用只看得到自己那一段原文，
# 声称之外的段号结构上不可信、必须丢弃。
#
# 刻意不在这里额外要求 display_name/label 逐字出现在该段落原文里：那道
# 逐字证据闸本来就已经存在（_prep_pack_mention_has_text_evidence，
# _resolve_assets 内"称谓证据闸"一节），但只对"裸直接命中"（没有经过
# alias/discovery/candidate_verdict 任何一条解析路径）生效，长期以来
# （1.5.x task②、1.8.0-1.8.5 五轮真实回归）刻意豁免经解析路径绑定的合成
# 描述性标签——例如真实 EP1 案例"银色长袍女子"从未逐字出现在原文（原文写
# "穿着一身银色长袍"），要靠候选判别（_prep_pack_resolve_functional_
# extra_candidate）独立的卷宗检索+钉证才能正确绑定许清；如果在这里（比
# _resolve_assets 更早的入口）就要求 display_name 逐字命中它自己声明的
# 段落，会在候选判别机会到来之前就把这整条提及连同它的 segment_indexes
# 一并丢弃，直接堵死候选判别机制——不是收紧反幻觉防线，是重新引入五轮
# 真实回归修过的同一个缺陷。评估过、放弃：per-segment 逐字闸看似能"更
# 精确"，但精确的代价是打断已经证明有效、职责单一的既有分工（模型申报语义
# 判断 -> _resolve_assets 按 method 分支各自核验）。
#
# "这段文字里出现了这个名字"从来不是也不该是"这个人真的在画面里出场"的
# 判据本身——后者是模型的语义职责（_extract_chunk 的提示词明确只要求申报
# "画面中出场"的段号，不是被提及/回忆/转述的段落），不针对任何具体人名/
# 称谓做特判，也不使用任何人名/称谓硬编码名单（no-blacklist-fixes 纪律）。
def _prep_pack_gate_segment_indexes(
    label: str, declared_indexes: list[int],
    chunk_global_indexes: set[int], chunk_by_index: dict[int, SourceSegment],
) -> list[int]:
    label = str(label or "").strip()
    if not label:
        return []
    verified: set[int] = set()
    for raw in declared_indexes:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        if index in chunk_global_indexes and index in chunk_by_index:
            verified.add(index)
    return sorted(verified)


def _known_character_names(conn, project_id: str, episode_no: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT character_name FROM character_portraits "
        "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY character_name",
        (project_id, episode_no, episode_no),
    ).fetchall()
    return [str(row["character_name"]) for row in rows]


# 逐字命中过滤（不是 RAG/关键词检索——见本函数下方"为什么不用 RAG"一节）：
# chunk_extraction.py._extract_chunk 把 known_characters 拼进每一个 chunk 的
# 提示词，措辞明确写着"仅供拼写对齐——原文没有这样称呼，就不要往上面靠"，
# 这是一条禁令，压力随名单长度线性上升。项目登记的角色可能有几十个，本集
# 原文通常只出现其中几个，另外那几十个不是中性噪音，是诱导错误归属的
# 噪音——模型会把本集的新角色往它们中某一个"看起来眼熟"的登记名上靠，
# 走建卡通道时因为"已经对齐"而漏建，制造重复卡（跟 true_name.py._prep_
# pack_true_name_verdict_candidates 挑中错误候选是同一类误差，只是这里
# 发生得更早、更隐蔽——连模型自己的"都不是"出口都没有，提示词直接把错误
# 候选摆在眼前）。
#
# 判据与 true_name.py._prep_pack_true_name_verdict_candidates 同一口径
# （该函数 docstring："人物谱/场景谱里，规范名或已确认别名在卷宗文本里
# 逐字命中的候选"）：已登记角色的全部称谓（character.name + Bible.
# characters[].aliases[].text，按名字匹配 Bible 条目）里，任一个在
# ``source_text``（本集原文）中逐字出现，该角色就入选——入选后放进名单
# 的是它的规范名，不是命中的那个别名，对齐目标保持唯一。
#
# 为什么不用 RAG/关键词检索：两者都有召回损失，而这里"召回失败"直接兑换
# 成最不想要的结果——一个本该被对齐的已登记角色没进名单，模型会把它当
# 新角色申报，走建卡通道，产生重复卡。逐字包含判断没有这个误差项：只要
# 称谓真的逐字出现，判据结构上不可能漏判它。
#
# 两条纪律（都不是新发明，是既有反黑白名单/反兜底纪律在这里的应用）：
# 1) 空集不回退全量——本集一个已登记角色都没命中，返回空列表就是诚实
#    结果；``if not shortlist: shortlist = known_names`` 这类短路会把
#    刚刚修掉的准确率问题原样带回来，是明确禁止的写法。
# 2) 不设数量上限——上限天然来自"本集原文能出现多少个不同称谓"，已经
#    有界；额外加"最多取前 N 个"是把不该存在的绝对门槛重新引入。
#
# 只砍"chunk 抽取时的拼写对齐提示"，不砍身份体系的可见性：返回的第一个
# 元素 known_names 是未经过滤的全量登记名单，调用方（_generate_prep_pack_
# once）必须继续把它单独喂给 character_manifest_anomaly 的 len() 判据——
# 未入选 shortlist 的角色仍然在 _resolve_portrait_id/character_portraits
# 与全书卷宗真名裁决（true_name.py）里正常可见，两者互不依赖这个 shortlist
# （见 tests/test_prep_pack_asset_discovery.py 对应的钉住测试）。
def _prep_pack_character_shortlist(
    conn, project_id: str, episode_no: int, source_text: str,
) -> tuple[list[str], list[str]]:
    """返回 (known_names, shortlist)：known_names 是 _known_character_names
    的原样全量结果（供调用方喂 anomaly 信号，见上方大注释"只砍…不砍…"一节，
    两者不得合并成一个变量）；shortlist 是其中全部称谓（name/alias）在
    ``source_text`` 里逐字命中的子集，元素是命中角色各自的规范名。"""
    known_names = _known_character_names(conn, project_id, episode_no)
    if not known_names:
        return known_names, []
    bible = _load_project_bible(conn, project_id)
    aliases_by_name: dict[str, list[str]] = {
        str(character.name or "").strip(): [
            str(alias.text or "").strip() for alias in (character.aliases or [])
        ]
        for character in bible.characters
    }
    shortlist = [
        name for name in known_names
        if any(
            form and form in source_text
            for form in (name, *aliases_by_name.get(name, []))
        )
    ]
    return known_names, shortlist


def _known_scene_names(conn, project_id: str, episode_no: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT scene_name FROM scene_references "
        "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY scene_name",
        (project_id, episode_no, episode_no),
    ).fetchall()
    return [str(row["scene_name"]) for row in rows]


def _prep_pack_chapter_titles(
    conn, project_id: str, chapter_indexes: list[int],
) -> list[str]:
    """This episode's own DB-anchored chapter titles (1.9.0, see
    PREP_PACK_VERSION's 1.9.0 note above). Only non-NULL, non-blank titles
    are returned -- a chapter whose ``chapters.title`` is NULL/blank is
    simply absent from the result, which is exactly the signal
    app.source_excerpt.chapter_title_segment_indexes and
    app.validators.build_prep_pack_span_ledger's chapter_titles parameter
    need to fall back to the pre-1.9.0 regex+model-declare path for that
    one chapter (see build_prep_pack_span_ledger's docstring)."""
    if not chapter_indexes:
        return []
    placeholders = ",".join("?" for _ in chapter_indexes)
    rows = conn.execute(
        f"SELECT title FROM chapters WHERE project_id=? AND idx IN ({placeholders})",
        (project_id, *chapter_indexes),
    ).fetchall()
    return [
        str(row["title"]) for row in rows
        if row["title"] is not None and str(row["title"]).strip()
    ]


