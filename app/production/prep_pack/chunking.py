"""Source-segment chunking for model calls (chunk sizing/rendering, the
segment-index structural gate, and known-name/chapter-title lookups used to
seed a chunk's prompt).

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

import json
from typing import Sequence

from app.schemas import Prop
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


# appearance 预览截断长度（2.0.13，defect 2 完整案情见
# chunk_extraction._KNOWN_PROP_NAME_FIELD_RULE 上方注释）：appearance_canonical
# 是三项以上可视觉验证特征拼成的单行锚点串（见 app/schemas/world.py::Prop
# docstring），真实数据多在 20~60 字（例如"米白色灯芯绒、翻领单排扣、藏青色
# 罗纹袖口"20 字）；取 80 留出覆盖四到五个特征的余量，同时给出上界——道具库
# 条目可能有几十张，每条都全文拼进提示词会让"已登记名单"本身膨胀到跟一个
# chunk 的原文一样长，挤占模型真正要读的正文。
_KNOWN_PROP_APPEARANCE_PREVIEW_CHARS = 80


# 「此前出场」证据（2.0.14，defect② 续修——见 chunk_extraction.
# _KNOWN_PROP_NAME_FIELD_RULE 完整案情）：appearance_canonical 结构上装不下
# "这是谁的"（关系型事实，不是可视觉验证特征，本次冻结 Prop 数据结构不扩展
# 字段）。真实回归（proj_ca86b15ab7d7 顾念长安第2集）证明了单靠外观不够：
# 第6段原文"顾屿……又顺手把自己的外套搭在她肩上"本身完全没有描述这件外套的
# 材质/颜色，模型手里没有任何可比对的外观依据，于是仅凭名字对上就直接提名
# 了第1集登记的「外套」卡（该卡 description 明确是"温念穿的外套，顾屿曾
# 帮她扣好扣子"——真正物主是温念，不是顾屿）。
#
# 不扩展数据结构、不回填任何数据，改为从项目内已有数据推导：这张卡在更早
# 集数里真实出现过的原始素材映射条目（``episodes.screenplay_json`` 的
# ``asset_manifest.props``）本来就带着当时的 ``description``/
# ``plot_significant_quote``，往往直接写明了物主/来源（第1集那条就是）。
# 把这些历史证据原样附给模型，判断"同一件实物"时就不止有外观可比，还有
# 物主线索可比对——仍然是"模型提名、代码核验"（card_match.py 的既有判据
# 不变，只是提名前模型手里的参考信息更完整）。
_KNOWN_PROP_PRIOR_APPEARANCE_MAX_ENTRIES = 2
# 每张卡最多带几条「此前出场」：取 2——单条证据可能恰好是背景一笔带过的
# 弱描述（这张卡在那一集也只是順带出现，见真实数据第1集 plot_significant=
# false 的「外套」条目），两条给模型多一次交叉核对的机会；不取更多是因为
# 这里的目的是"给出归属线索"，不是穷举这张卡的全部历史，条目一多反而把
# 已登记名单拉长，挤占模型真正要读的本集正文（同 _KNOWN_PROP_APPEARANCE_
# PREVIEW_CHARS 上方"膨胀"顾虑同一道理）。
_KNOWN_PROP_PRIOR_APPEARANCE_DESC_CHARS = 60
# 单条「此前出场」截断长度：与 _KNOWN_PROP_APPEARANCE_PREVIEW_CHARS（80）
# 同一数量级但更短——这里只需要物主/来源这一句话（真实数据"温念穿的外套，
# 顾屿曾帮她扣好扣子"14 字），不需要完整外观描述，60 字留出单条归属线索
# 还能带一点上下文的余量。


def _prep_pack_other_episode_prop_mentions(
    conn, project_id: str, episode_id: str, episode_no: int,
) -> list[tuple[int, dict]]:
    """项目内严格早于本集（``episode_no`` 更小）、已有 ``screenplay_json`` 的
    其它集，``asset_manifest.props`` 的全部原始条目，一次性扫描出来供全部
    道具卡复用（不逐卡重复查询/解析 JSON）。返回 ``(episode_no, entry)``。

    用 ``episode_no<?`` 而不只是 ``id!=?``：「此前出场」按字面就是"更早"，
    不是"项目内任何其它集"——同一轮批量生产里后续集数的映射可能比本集
    更晚才跑完，但它们在故事时间线上发生在本集之后，不构成本集可比对的
    历史证据；``id!=?`` 作为第二道防线一起留着，防止 ``episode_no`` 异常
    （理论上不该发生，``episodes`` 表对 ``(project_id, episode_no)`` 有
    UNIQUE 约束）时仍然读进本集自己。"""
    rows = conn.execute(
        "SELECT episode_no, screenplay_json FROM episodes WHERE project_id=? "
        "AND id!=? AND episode_no<? AND screenplay_json IS NOT NULL",
        (project_id, episode_id, episode_no),
    ).fetchall()
    mentions: list[tuple[int, dict]] = []
    for row in rows:
        try:
            payload = json.loads(row["screenplay_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        manifest = payload.get("asset_manifest") or {}
        for entry in manifest.get("props") or []:
            if isinstance(entry, dict):
                mentions.append((int(row["episode_no"]), entry))
    return mentions


def _prep_pack_prop_prior_appearance_lines(
    mentions: list[tuple[int, dict]], card: Prop,
) -> list[str]:
    """``mentions``（``_prep_pack_other_episode_prop_mentions`` 的原样结果）
    里命中这张卡的条目——``canonical_name`` 等于卡名，或 ``label`` 命中卡名/
    某个别名——按集号降序取最近 ``_KNOWN_PROP_PRIOR_APPEARANCE_MAX_ENTRIES``
    条，每条格式化成 ``"第N集 <证据文字>"``（截断见上方常量）。证据文字优先
    取 ``description``，为空时退回 ``plot_significant_quote``（真实数据里
    ``plot_significant=false`` 的条目 quote 恒为空串，两者不会同时为空却都
    有用信息，取非空的那个不是武断二选一）。同一集同一张卡只取第一条命中
    （同一张卡同一集的归属描述理应一致，不逐条堆叠制造噪音）。"""
    identifiers = {card.name, *(str(a or "").strip() for a in card.aliases)}
    hits: dict[int, str] = {}
    for episode_no, entry in mentions:
        canonical = str(entry.get("canonical_name") or "").strip()
        label = str(entry.get("label") or "").strip()
        if canonical != card.name and label not in identifiers:
            continue
        text = (
            str(entry.get("description") or "").strip()
            or str(entry.get("plot_significant_quote") or "").strip()
        )
        if not text:
            continue
        hits.setdefault(episode_no, text)
    lines: list[str] = []
    for episode_no in sorted(hits, reverse=True)[:_KNOWN_PROP_PRIOR_APPEARANCE_MAX_ENTRIES]:
        text = hits[episode_no]
        if len(text) > _KNOWN_PROP_PRIOR_APPEARANCE_DESC_CHARS:
            text = text[:_KNOWN_PROP_PRIOR_APPEARANCE_DESC_CHARS] + "…"
        lines.append(f"第{episode_no}集 {text}")
    return lines


def _prep_pack_props_with_prior_appearance_evidence(
    conn, project_id: str, episode_id: str, episode_no: int, cards: Sequence[Prop],
) -> frozenset[str]:
    """哪些卡在 ``_prep_pack_known_prop_names`` 的展示名单里会带上「此前出场」
    一段（即 ``_prep_pack_prop_prior_appearance_lines`` 对它返回非空列表）——
    供 ``app.props.card_match.match_existing_prop_card`` 的常规判据收紧使用
    （2026-10-01，见该模块 docstring 完整案情：模型手里有归属证据却明确不
    提名时，字面包含判据不该替模型重新做出相反的判断）。与
    ``_prep_pack_known_prop_names`` 共用同一次「其它集道具提及」扫描
    （``_prep_pack_other_episode_prop_mentions``），判据完全一致——不是
    另算一遍，两处"这张卡有没有此前出场证据"的结论不会分叉。"""
    mentions = _prep_pack_other_episode_prop_mentions(conn, project_id, episode_id, episode_no)
    return frozenset(
        card.name for card in cards
        if card.name and _prep_pack_prop_prior_appearance_lines(mentions, card)
    )


def _prep_pack_known_prop_names(
    conn, project_id: str, episode_id: str, episode_no: int,
) -> list[str]:
    """已登记道具库的展示名单：每条一行，含这张卡的名称/别名/外观特征
    （2.0.13 起，此前只有 name+alias 纯名字列表），以及这张卡在更早集数里
    「此前出场」的归属证据（2.0.14 起）。

    ``episode_id``/``episode_no`` 必传（CLAUDE.md「Ownership Must Be
    Explicit」：排除当前集这件事不能留可选默认值悄悄漏传）——用于
    ``_prep_pack_other_episode_prop_mentions`` 排除本集自己，不拿本次正在
    重算、尚未定稿的结果自证。

    真实案例（顾念长安第2集）：道具库里有一张名字很泛的「外套」卡（画的是
    温念的米白灯芯绒外套），模型只看到这个名字，无从判断原文里顾屿「自己的
    外套」是不是同一件实物，于是把两件不同人的外套误提名成同一张卡（见
    chunk_extraction._KNOWN_PROP_NAME_FIELD_RULE 完整案情）。只给名字/别名
    不够——模型需要外观信息才能核对"是不是同一件"，这里把每张卡的外观
    （过长按 ``_KNOWN_PROP_APPEARANCE_PREVIEW_CHARS`` 截断）一并带上；外观
    信息本身装不下"这是谁的"，不够时再给「此前出场」的历史归属证据（见
    ``_prep_pack_prop_prior_appearance_lines`` 完整说明）。

    道具（``Prop``，见 app/schemas/world.py）没有 ``ep_start``/``ep_end`` 时间
    范围字段，不需要像 ``_known_scene_names`` 那样按集次过滤；也不做
    ``_prep_pack_character_shortlist`` 那种"本集原文里逐字命中才入选"的裁剪——
    这里只是给模型看的对齐/核对提示，命中判据本身由
    ``app.props.card_match.match_existing_prop_card`` 在清单构建阶段独立核验
    （核验按卡的 name/alias 逐字核对，不读这里拼出的展示行——显示格式的变化
    不影响名字核验，见该模块 docstring），不依赖这份名单是否精确。"""
    bible = _load_project_bible(conn, project_id)
    mentions = _prep_pack_other_episode_prop_mentions(conn, project_id, episode_id, episode_no)
    lines: list[str] = []
    for prop in bible.props:
        name = str(prop.name or "").strip()
        if not name:
            continue
        alias_text = "、".join(
            alias for alias in (str(a or "").strip() for a in prop.aliases) if alias
        )
        appearance = str(prop.appearance_canonical or "").strip()
        if len(appearance) > _KNOWN_PROP_APPEARANCE_PREVIEW_CHARS:
            appearance = appearance[:_KNOWN_PROP_APPEARANCE_PREVIEW_CHARS] + "…"
        prior = _prep_pack_prop_prior_appearance_lines(mentions, prop)
        parts = [f"名称：{name}"]
        if alias_text:
            parts.append(f"别名：{alias_text}")
        if appearance:
            parts.append(f"外观：{appearance}")
        if prior:
            parts.append(f"此前出场：{'；'.join(prior)}")
        lines.append("｜".join(parts))
    return sorted(lines)


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


