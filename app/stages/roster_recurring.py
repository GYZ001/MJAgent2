"""角色点名——全书重要角色统计判据与点名管线主入口 _recurring_character_names。"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import re
from typing import Any

from pydantic import BaseModel, Field

from app import config
from app.db import log_provider_call
from app.harness import model_gateway
from app.schemas import (Bible, Character, CharacterAlias,
                         extract_json)

from typing import TYPE_CHECKING

from .alias_backfill import _roster_presence_dossier, _roster_presence_verdict_call
from .alias_verdict import _alias_verdict_pin_segment
from .bible_shared import _bible_short_json_call_meta, _chapters_by_idx, _render_bible_source
from .common import (
    BIBLE_FORMAL_NAME_MIN_RATIO,
    BIBLE_HEAD_CHAPTERS,
    BIBLE_LOOKAHEAD_CHAPTERS,
    BIBLE_RECURRING_MIN_ONSTAGE_QUOTES,
    BIBLE_ROLL_CALL_CHUNK_CHAPTERS,
    BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS,
    BIBLE_ROLL_CALL_CHUNK_MAX_TOKENS,
    BIBLE_ROLL_CALL_CONCURRENCY,
    BIBLE_ROLL_CALL_MAX_ATTEMPTS,
    BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE,
    BIBLE_ROLL_CALL_TIMEOUT_S,
    BIBLE_ROSTER_RUNAWAY_MAX,
    BIBLE_STATISTICAL_MIN_CHAPTER_RATIO,
    StageError,
)
from .constants import SYSTEM_PREFIX
from .identity_evidence import _alias_text_is_independent_appellation, _quote_comparison_variants
from .roster_admission import (
    _roster_mentioned_importance_verdict,
    _roster_onstage_chapter_floor,
    _roster_statistical_mention_floor,
)
from .roster_candidates import (
    _CharacterRollCall,
    _RosterCandidate,
    _coerce_roster_chapter_index,
    _pin_roster_candidates_to_source,
    _shared_appellations,
)
from .roster_chunk_plan import _expand_chunk_plan, _failed_chunk_meta
from .roster_merge import _merge_roll_call_candidates, _resolve_generic_character_candidates
from .roster_personhood import _filter_non_person_roster_candidates
from .roster_truename import _discover_roster_true_names, _resolve_conflicting_formal_names

if TYPE_CHECKING:
    from .bible_models import _BibleRosterEntry


def _corpus_scoped_chapter_threshold(threshold: int, available_chapters: int) -> int:
    """把「至少覆盖 N 章」的门槛压回语料实际有的章数之内。

    「跨 N 章复现」在只有 M < N 章的语料里不是更严格的标准，而是结构上永远
    判不过的判据——它挂在了语料被切成几章上，不挂在「这个人是不是反复登场」上。
    真实故障：《王六郎》全文 2944 字只切出 1 章，3 个候选、4 条在场证据全部通过
    结构闸与裁决闸，必收名单仍是空的，人物谱以「未产出任何经原文核验的角色候选」
    整体失败。章数够的语料上封顶不生效，跨章判据原样保留。
    """
    return max(1, min(int(threshold), max(1, int(available_chapters))))


class _BibleRollCallChunkFailed(StageError):
    """点名分块在退避重试后仍失败：宁可整体失败，也不允许无证据兜底生成人物谱。

    继承 StageError，让 classify() 走内容生成（GEN），而不是 RuntimeError 落到
    系统内部（SYS）。真实故障 ERR-20260827-a2f706：8/20 分块耗尽后界面写成
    「服务器内部错误」。
    """

    def __init__(self, message: str):
        super().__init__("人物点名", [message])


class _BibleSupplement(BaseModel):
    """补录合同：只为「必收名单里还缺的人」补出完整角色条目。"""

    characters: list[Character] = Field(default_factory=list)


def _pick_canonical_display_name(
    appellation: str, formal: str, chapters: list[dict],
) -> tuple[str, list[str]]:
    """选主名：真名与绰号都是同一角色的检索键，只决定「人物谱里显示哪个」。

    真名优先只在真名确实被原文常用、且能和名单称呼同窗共现时成立。真实故障：
    「小胖子」全书出现 152 次、「李富贵」只出现 1 次，机械套用真名优先会把主名
    改成正文里几乎不存在的写法；「靠山老祖 / 白主」则是反过来，0.2 比例把更常用
    的原文称呼挤出 aliases。
    """
    appellation = (appellation or "").strip()
    formal = (formal or "").strip()
    if not formal or formal == appellation:
        return appellation, []
    if _cooccurrence_quote(chapters, appellation, formal) is None:
        return appellation, []
    appellation_hits = sum((ch.get("content") or "").count(appellation) for ch in chapters)
    formal_hits = sum((ch.get("content") or "").count(formal) for ch in chapters)
    if formal_hits >= max(1, appellation_hits * BIBLE_FORMAL_NAME_MIN_RATIO):
        return formal, [appellation]
    return appellation, [formal]


def _cooccurrence_quote(
    chapters: list[dict], left: str, right: str,
) -> tuple[int, str] | None:
    """找一条同时含两个称呼、不超过 80 字的原文引句。"""
    if not left or not right or left == right:
        return None
    for chapter in chapters:
        text = chapter.get("content") or ""
        if left not in text or right not in text:
            continue
        try:
            idx = int(chapter.get("idx"))
        except (TypeError, ValueError):
            continue
        right_positions = [match.start() for match in re.finditer(re.escape(right), text)]
        for start in right_positions:
            window_start = max(0, start - 40)
            quote = text[window_start:window_start + 80]
            if left in quote and right in quote:
                return idx, quote.replace("\n", "")
    return None


def _attach_roster_source_appellations(
    character: Character, entry: _BibleRosterEntry, chapters: list[dict],
) -> None:
    """名单里已经程序绑定的称呼必须能检索到同一张卡，不能等详情模型再报一遍别名。

    真实故障：必收名单已是「李富贵（小胖子）」/ 绑定后的「许师姐→许清」，详情模型
    没把真名写进 aliases，核验闸再一丢，人物谱只剩绰号。
    """
    from app.portraits import IDENTITY_NAME_FORM_REFERENTIAL

    known = {character.name, *(item.text for item in character.aliases if item.text)}
    unverified = set(entry.unverified_appellations)
    for raw in entry.source_appellations:
        text = (raw or "").strip()
        if not text or text in known:
            continue
        # 这条免检通道成立的前提是「这个称呼是名单赖以成立的身份标识」：候选能
        # 进必收名单，靠的就是它，在场证据已经逐条过了结构闸、裁决闸和段号钉证。
        # 点名模型顺手申报的 aliases 没有这层保证，走到这里等于零核验入谱——它们
        # 只能走详情侧那条正规闸（_alias_declaration_verified + 别名裁决）。
        #
        # 真实故障 ERR-20260828-9fcabe（《罗刹海市》EP1）：点名把「大夫」报成主角
        # 马骥的别名，共现闸在「那些士绅大夫争着想开开眼界，便叫村民邀请马骥前去」
        # 这句里同时看到两个词就放行了——可这句话里大夫是发出邀请的人，马骥是被
        # 邀请的人，恰恰是两拨人。「大夫」就此成为马骥的登记称谓，进了
        # reserved_authority_labels；映射台随后正确地把本集朝堂上的众大夫判成
        # functional，撞上「不得冒用已登记身份称谓」，整集失败且重试必然复现。
        if text in unverified:
            continue
        # 词形闸不属于证据强弱问题：一个切碎的短语残片无论共现多少次都不指代任何
        # 人，登记它只会让下游的子串匹配到处误命中。
        if not _alias_text_is_independent_appellation(text):
            continue
        found = None
        for anchor in list(known):
            if not anchor:
                continue
            found = _cooccurrence_quote(chapters, anchor, text)
            if found is not None:
                break
        if found is None:
            continue
        chapter_idx, quote = found
        character.aliases.append(CharacterAlias(
            text=text,
            # 这条别名是程序按共现补回来的，没有模型标注过形态，就不替它下结论。
            name_kind=IDENTITY_NAME_FORM_REFERENTIAL,
            evidence_chapter_index=chapter_idx,
            evidence_quote=quote,
            # 显式写 False，不依赖 schema 默认值：这条免检通道（本函数顶部大注释、
            # ERR-20260828-9fcabe）只做共现检查，从来没有为"排他性"做过任何核验，
            # 不该假装做过。别名仍然登记（供 _prep_pack_bible_alias_owner 等通道
            # 解析），只是不参与 identity_authority_registry 的 source_labels 折叠。
            is_exclusive=False,
        ))
        known.add(text)


async def _recurring_character_names(
    chapters: list[dict], *, project_id: str | None = None,
) -> list[tuple[str, str, int, int, int, list[str]]]:
    """产出「必收角色名单」：先点名+自报在场证据，再用结构闸+独立裁决闸核验每条
    证据是不是真的证明本人在场，核验通过的证据条数（`verified_onstage_count`）
    才是判据——不再是"名字字符串在原文窗口里出现的次数"。

    根因：旧判据把字符串出现次数当"重不重要"的代理信号，两个方向都会失效。
    假阳性方向——王伯/周员外/靠山老祖的命中全部来自旁白交代身份或他人台词提及，
    本人从未真正在场，却因为次数够多进了必收名单。假阴性方向——这个信号只统计
    模型报出的候选名字本身的出现次数，原文如果通篇用绰号称呼一个人（本人几乎
    只以"小胖子"出现，正式姓名仅出现一两次）而此刻圣经正文还没生成、没有别名表
    能把绰号翻译回正式姓名，这个人就会被判定为不重要。

    新流程：
    1. 模型只在前 BIBLE_HEAD_CHAPTERS 章里点名，每个候选申报 primary_appellation
       （原文最常用写法，允许绰号）+ formal_name（原文已揭示的正式姓名，未揭示则
       空）+ onstage_evidence（能证明本人在场的原文引句列表）——绰号本身就能直接
       充当"必收货币"，不需要一张此刻还不存在的别名表做转译。
    2. 代码结构闸，逐条证据：G1 引句所在章节必须落在统计窗口（前 HEAD+LOOKAHEAD
       章）内；G2 引句必须逐字命中该章原文（允许模型自行加/脱一层引号的噪音）；
       G3 称呼（primary_appellation 或 formal_name 中非空的那个）必须是引句子串。
       任一不满足直接丢弃该条证据，不发起裁决调用。点名也允许申报虽未出场、但原文
       已明确赋予持续剧情作用的具名人物，后续由 mentioned_only 通道独立判断。
    3. 结构闸通过的证据才发起独立低温模型裁决（`_roster_presence_verdict_call`，
       与别名裁决闸同一分工范式：代码检索卷宗 → 模型独立裁决 → 代码结构性钉证）：
       只问这段原文里称呼所指的人物本人是不是真的在场（本人说话/动作/被叙述在场，
       而不是被谈论、被指涉、被交代来历的对象）；裁决通过（verdict=="onstage" 且
       段号钉证通过）才计入该候选的 `verified_onstage_count`。
    4. 按 `verified_onstage_count` 降序（同分按 primary_appellation 字典序打破
       平局）排序，取 >= BIBLE_RECURRING_MIN_ONSTAGE_QUOTES 的候选，不设人数
       上限（超过 BIBLE_ROSTER_RUNAWAY_MAX 时判失控直接报错，见下方）。

    点名调用失败时返回空名单，绝不阻断人物谱本身；结构闸/裁决闸任一步不通过，
    该条证据直接丢弃，不确定不登记（不会因为某一条证据没通过就拒绝整个候选，
    只是那一条不计数）。
    """
    valid = [ch for ch in chapters if (ch.get("content") or "").strip()]
    if not valid:
        return []
    head = valid[:BIBLE_HEAD_CHAPTERS]
    chunks = _expand_chunk_plan([
        head[index:index + BIBLE_ROLL_CALL_CHUNK_CHAPTERS]
        for index in range(0, len(head), BIBLE_ROLL_CALL_CHUNK_CHAPTERS)
    ], BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS)
    chapters_by_idx = _chapters_by_idx(valid)
    roll_call_sem = asyncio.Semaphore(BIBLE_ROLL_CALL_CONCURRENCY)

    async def _call_chunk(chunk: list[dict], chunk_index: int) -> list[_RosterCandidate]:
        chunk_text = _render_bible_source(
            chunk, budget=BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS,
            head_chapters=len(chunk),
        )
        if not chunk_text.strip():
            return []
        prompt = f"""任务：从下面的小说正文里找出【出场人物】，为每个人物申报能证明他本人真的出现在画面中的证据，不要只给名字。

要求：
1. primary_appellation：本章里称呼这个人物最常用、最稳定的一种写法，可以是正式姓名、外号、绰号、尊称或代称，必须逐字照抄。
2. formal_name：本章已经明确揭示的正式姓名；未揭示就填空字符串，禁止猜测。若“孙天地自称……”或“众人称小胖子李富贵”这种同一人物身份链接出现，必须填 formal_name。
3. aliases：本章明确指向同一人物的其它称呼；只有本章有明确身份链接才填，不能凭外貌相似猜。
4. identity_evidence：证明 formal_name/aliases 与 primary_appellation 是同一人的逐字引句，最多 2 条；引句需同时包含两种称呼或明确自称结构。
5. onstage_evidence：每人最多 {BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE} 条，格式为 chapter_index + quote；quote 必须是本块原文不超过约 80 字的逐字引句，并包含 primary_appellation、formal_name 或 alias。
6. 只有本人说话、行动或被直接叙述为在场才算 onstage_evidence；被谈论、回忆或背景介绍不算。
7. 但具名人物即使当前仅被提及，只要原文明示其建立宗门/制度、造成持续冲突、留下关键规则或后续行动目标，也可输出候选；onstage_evidence 填包含该剧情作用的逐字引句，后续程序会把它判为 mentioned_only，不得伪装成已出场。
8. 只申报可单独指认、能作为定妆对象的人物；同一个人在本块只输出一次。器物与法宝、没有自己姓名的野兽、用「某人的客人」这类描述指代且无法对应到具体人名的路人，都不是人物候选。
9. 本块最多输出 20 个候选，优先输出戏份最重的人物；引句只保留能证明在场的最小片段，不要复述剧情。

小说正文：
{chunk_text}

输出 JSON Schema：
{{"candidates": [{{"primary_appellation": str, "formal_name": str, "aliases": [str], "identity_evidence": [{{"chapter_index": int, "quote": str}}], "onstage_evidence": [{{"chapter_index": int, "quote": str}}]}}]}}"""
        if len(chunk_text) > BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS:
            raise ValueError("人物点名分块输入超过硬上限")
        last_error: Exception | None = None
        for attempt in range(1, BIBLE_ROLL_CALL_MAX_ATTEMPTS + 1):
            try:
                async with roll_call_sem:
                    raw = await asyncio.wait_for(
                        model_gateway.chat(
                            [{"role": "system", "content": SYSTEM_PREFIX},
                             {"role": "user", "content": prompt}],
                            temperature=0.2,
                            max_tokens=BIBLE_ROLL_CALL_CHUNK_MAX_TOKENS,
                            call_meta=_bible_short_json_call_meta({
                                "stage": "人物点名",
                                "stage_key": "character_roll_call",
                                "call_role": "stage_generate",
                                "call_role_label": "人物点名分块",
                                "expected_json": True,
                                "chunk_index": chunk_index,
                                "chunk_count": len(chunks),
                                "input_chars": len(chunk_text),
                                "attempt": attempt,
                            }),
                        ),
                        timeout=BIBLE_ROLL_CALL_TIMEOUT_S,
                    )
                return _CharacterRollCall.model_validate(extract_json(raw)).candidates
            except Exception as exc:  # noqa: BLE001 - 限流/超时退避重试，仍不阻断其它块
                last_error = exc
                if attempt < BIBLE_ROLL_CALL_MAX_ATTEMPTS:
                    await asyncio.sleep(min(20.0, 2.0 * (2 ** (attempt - 1))))
        log_provider_call(
            "character_roll_call", config.MODEL_TEXT, "FAILED", None, 0,
            meta={
                "chunk_index": chunk_index,
                "outcome": "roll_call_chunk_exhausted",
                "attempts": BIBLE_ROLL_CALL_MAX_ATTEMPTS,
                "error": str(last_error)[:300],
            },
        )
        raise _BibleRollCallChunkFailed(
            f"人物点名分块 {chunk_index} 连续 {BIBLE_ROLL_CALL_MAX_ATTEMPTS} 次失败：{last_error}"
        )

    chunk_results = await asyncio.gather(*(
        _call_chunk(chunk, index) for index, chunk in enumerate(chunks)
    ), return_exceptions=True)
    failed_chunks = [item for item in chunk_results if isinstance(item, BaseException)]
    if failed_chunks and len(failed_chunks) == len(chunk_results):
        raise _BibleRollCallChunkFailed(
            f"人物点名全部 {len(chunk_results)} 个分块均失败，拒绝在无原文证据下生成人物谱："
            f"{failed_chunks[0]}"
        )
    if len(failed_chunks) > max(1, len(chunk_results) // 3):
        raise _BibleRollCallChunkFailed(
            f"人物点名失败分块过多（{len(failed_chunks)}/{len(chunk_results)}），"
            f"名单不可信，拒绝继续生成：{failed_chunks[0]}"
        )
    candidates = _merge_roll_call_candidates([
        item for item in chunk_results if not isinstance(item, BaseException)
    ])
    candidates = _pin_roster_candidates_to_source(candidates, chapters_by_idx)
    # 资格裁决先跑：它顺带判出每个称呼是姓名、尊称还是代称，身份归一要靠这个
    # 结论决定谁该被消歧，程序不再用词表预判。
    candidates = await _filter_non_person_roster_candidates(
        candidates, chapters_by_idx, project_id=project_id,
    )
    candidates = await _resolve_generic_character_candidates(
        candidates, chapters_by_idx, project_id=project_id,
    )
    candidates = await _discover_roster_true_names(
        candidates, valid, project_id=project_id,
    )
    candidates = _resolve_conflicting_formal_names(candidates)
    candidates = _merge_roll_call_candidates([[item] for item in candidates])
    candidates = [
        item.model_copy(update={"personhood": "person"})
        if item.personhood != "non_person" and (item.formal_name or "").strip()
        else item
        for item in candidates
    ]

    # 结构闸 G1 用的窗口原文：前 HEAD 章 + 往后 LOOKAHEAD 章，按章节序号建索引
    # （复用 `_chapters_by_idx`，与别名核验同一个查找表构造方式）。
    window_chapters_by_idx = _chapters_by_idx(
        valid[:BIBLE_HEAD_CHAPTERS + BIBLE_LOOKAHEAD_CHAPTERS]
    )
    seen: set[str] = set()
    ambiguous_appellations = _shared_appellations(candidates)
    verified_counts: dict[str, int] = {}
    formal_names: dict[str, str] = {}
    aliases_by_appellation: dict[str, list[str]] = {}
    mention_counts: dict[str, int] = {}
    chapter_counts: dict[str, int] = {}
    personhood_by_appellation: dict[str, str] = {}
    name_form_by_appellation: dict[str, str] = {}
    evidence_total = 0
    structural_pass = 0
    # 结构闸（G1-G3）零模型调用、纯同步核对，先把候选证据筛成「值得送裁决闸」的
    # 卷宗清单；裁决闸才是本函数唯一的模型调用，放到下面统一并发发起。
    verdict_jobs: list[tuple[str, list[dict[str, Any]]]] = []
    for candidate in candidates:
        appellation = (candidate.primary_appellation or "").strip()
        formal = (candidate.formal_name or "").strip()
        if not appellation or appellation in seen:
            continue
        seen.add(appellation)
        formal_names[appellation] = formal
        aliases = list(dict.fromkeys(
            value for value in candidate.aliases
            if value and value not in {appellation, formal}
        ))
        if formal and formal != appellation and appellation not in aliases:
            aliases.insert(0, appellation)
        aliases_by_appellation[appellation] = aliases
        personhood_by_appellation[appellation] = candidate.personhood
        name_form_by_appellation[appellation] = candidate.name_form
        search_terms = {value for value in [appellation, formal, *aliases] if value}
        mention_counts[appellation] = sum(
            (chapter.get("content") or "").count(term)
            for chapter in valid for term in search_terms
        )
        chapter_counts[appellation] = sum(
            1 for chapter in valid
            if any(term in (chapter.get("content") or "") for term in search_terms)
        )
        verified_counts[appellation] = 0
        # 防御性兜底：即便模型没听提示词的话报多了，这里也只取前 N 条送进结构闸/
        # 裁决闸，保证下游裁决调用数量有上界，不随模型的自由发挥线性增长。
        evidence_list = candidate.onstage_evidence[:BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE]
        for evidence in evidence_list:
            evidence_total += 1
            quote = (evidence.quote or "").strip()
            if not quote:
                continue
            # G1：chapter_index 必须落在本轮统计窗口内，防止模型编造窗口外的章号。
            chapter_text = window_chapters_by_idx.get(evidence.chapter_index, "")
            if not chapter_text:
                continue
            # G2：quote 必须是该章原文的逐字子串（允许脱一层配对引号的噪音）。
            if not any(v in chapter_text for v in _quote_comparison_variants(quote)):
                continue
            # G3：任一已绑定称呼都必须能在引句里逐字找到——绰号和真名是同一人的 Mention。
            appellations = [value for value in (appellation, formal, *aliases) if value]
            if not any(value in quote for value in appellations):
                continue
            structural_pass += 1
            dossier = _roster_presence_dossier(evidence.chapter_index, chapter_text, quote)
            if not dossier:
                continue  # no_presence_dossier：不确定不登记，不是跳过检查
            verdict_jobs.append((appellation, dossier))

    # 裁决闸并发发起：每条证据一次独立模型调用，此前是嵌套 for/await 全程串行——
    # 一个出场上千次的主角单独就能把这里拖成几十次排队调用（真实故障：
    # run_8ebe1225aa69，18 条证据串行裁决耗时 91.6s，仍被 900s 总超时拦腰截断）。
    # 这里只是把发起方式从"一条条 await"改成"一起 gather"，真正的并发上限由
    # `model_gateway.chat`→`run_with_provider_call_slot` 那道进程级 `text_provider_calls`
    # 优先级闸门统一节流（见 app/generation_concurrency.py），不额外起一套并发框架。
    # 失败隔离：单条证据裁决失败/不通过只让这一个 job 判 0 票，不影响其它 job，
    # 语义与原来的 continue 完全一致（不确定不登记）；裁决闸本身的提示词/温度/
    # 候选集算法（`_roster_presence_verdict_call`）原样未动。
    verdict_pass = 0
    mentioned_counts: dict[str, int] = defaultdict(int)
    mentioned_dossiers: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # 每条通过裁决的在场证据落在哪一章：通道 A 判「复现」要用它，光数条数分不出
    # 「跨章反复登场的人物」和「在某一章里连说三句话的路人」。章号取被裁决钉住的
    # 那一段，不取整份卷宗——卷宗可能跨章检索，钉证段才是模型真正认定在场的那条。
    verified_chapters: dict[str, set[int]] = defaultdict(set)

    async def _judge_evidence(
        appellation: str, dossier: list[dict[str, Any]],
    ) -> tuple[str, int]:
        try:
            verdict = await _roster_presence_verdict_call(
                appellation=appellation, dossier=dossier, project_id=project_id,
            )
        except Exception as exc:  # noqa: BLE001 - 裁决失败按不确定处理：不确定不登记
            log_provider_call(
                "character_roster_presence_verdict", config.MODEL_TEXT,
                "FAILED", None, 0,
                meta={"appellation": appellation, "error": str(exc)[:300]},
            )
            return "uncertain", -1
        pinned = _alias_verdict_pin_segment(dossier, verdict.supporting_segment_index)
        if pinned is None:
            return "uncertain", -1
        return verdict.verdict, _coerce_roster_chapter_index(pinned.get("chapter_idx"))

    judged = await asyncio.gather(
        *(_judge_evidence(appellation, dossier) for appellation, dossier in verdict_jobs)
    )
    for (appellation, dossier), (verdict, chapter_idx) in zip(
        verdict_jobs, judged, strict=True,
    ):
        if verdict == "onstage":
            verdict_pass += 1
            verified_counts[appellation] += 1
            if chapter_idx > 0:
                verified_chapters[appellation].add(chapter_idx)
        elif verdict == "mentioned_only":
            mentioned_counts[appellation] += 1
            mentioned_dossiers[appellation].extend(dossier)

    mentioned_retain: set[str] = set()

    mentioned_jobs = [
        _roster_mentioned_importance_verdict(
            appellation,
            mentioned_dossiers=mentioned_dossiers,
            mention_counts=mention_counts,
            chapter_counts=chapter_counts,
            ambiguous_appellations=ambiguous_appellations,
            project_id=project_id,
        )
        for appellation, count in mentioned_counts.items()
        if count >= 1 and mention_counts.get(appellation, 0) >= 2
    ]
    if mentioned_jobs:
        for appellation, retain in await asyncio.gather(*mentioned_jobs):
            if retain:
                mentioned_retain.add(appellation)

    # 准入分三条独立通道，任一命中即可进入名单：
    # A. 在场证据通道：裁决闸核验通过 >= BIBLE_RECURRING_MIN_ONSTAGE_QUOTES 条，
    #    且这些证据跨到达标章节数（按 name_form 分档，见
    #    `_roster_onstage_chapter_floor`；章数不足的短篇再按语料实际章数封顶，
    #    见 `_corpus_scoped_chapter_threshold`）；
    # B. 剧情权威通道：仅被提及，但原文赋予其持续剧情作用（mentioned_retain）；
    # C. 全文统计通道：全文命中与章节覆盖同时达标——主角/核心配角在原文里持续出现，
    #    这本身就是比"某一条引句能否通过单次模型裁决"更稳的重要性证据。
    #    真实故障：孟浩前 20 章提及 991 次、覆盖 20/20 章，却因 3 条引句裁决全判
    #    other 被整个淘汰，而只出现 1 次的「李富贵」反被当成主角，人物谱不可用。
    window_size = max(1, len(head))
    # 通道 A 要的是「复现人物」（本函数名即 recurring），所以在场证据必须跨章：
    # 全部挤在同一章说明这个人在那一章之外没有存在感，而人物谱的作用域是全书。
    # 真实故障：「绿袍男子」——靠山宗那批绿袍修士的类别称谓，不是谁的专名——三条
    # 在场证据全在第 2 章，靠通道 A 建了正式角色卡；它随后被映射器裸命中，把整集
    # 映射卡死在「称谓未逐字出现在本集原文」的反幻觉闸上，且重试必然复现。挡它的
    # 是「靠衣着指人」这个性质（name_form=referential），不是「只出现一章」本身：
    # 姓名/尊称形态跨章门槛降到 1 章（`_roster_onstage_chapter_floor`），只有
    # 代称/未判定形态仍要求跨 2 章。漏判不是永久损失：真在某一章挑大梁的角色由
    # 分镜阶段的按集新角色发现补建卡。语料本身只有一章时门槛再按实际章数封顶
    # （见 `_corpus_scoped_chapter_threshold`），钉不住章号的证据照旧不计。
    onstage_recurring = {
        appellation
        for appellation, count in verified_counts.items()
        if count >= BIBLE_RECURRING_MIN_ONSTAGE_QUOTES
        and len(verified_chapters.get(appellation, ())) >= _corpus_scoped_chapter_threshold(
            _roster_onstage_chapter_floor(name_form_by_appellation.get(appellation, "uncertain")),
            len(window_chapters_by_idx),
        )
    }
    # 统计通道章节门槛按语料实际章数封顶（数的是全书命中章，用全书章数封顶，见
    # `_corpus_scoped_chapter_threshold`）；提及量门槛改成本次通道 A 候选的相对
    # 分布（`_roster_statistical_mention_floor`），不再是跨作品固定值。
    min_statistical_chapters = _corpus_scoped_chapter_threshold(
        max(2, round(window_size * BIBLE_STATISTICAL_MIN_CHAPTER_RATIO)), len(valid),
    )
    statistical_min_mentions = _roster_statistical_mention_floor(
        [mention_counts.get(appellation, 0) for appellation in onstage_recurring]
    )
    statistical_retain = {
        appellation
        for appellation in verified_counts
        # 是不是人由资格裁决说了算，程序不再拿施事动词表去猜。判不出来的候选
        # 走不了统计通道，正确做法是把卷宗做厚让模型判得出，不是绕过它。
        if personhood_by_appellation.get(appellation) == "person"
        and appellation not in ambiguous_appellations
        and mention_counts.get(appellation, 0) >= statistical_min_mentions
        and chapter_counts.get(appellation, 0) >= min_statistical_chapters
    }
    ranked = [
        (
            appellation,
            formal_names.get(appellation, ""),
            count,
            mention_counts.get(appellation, 0),
            chapter_counts.get(appellation, 0),
            aliases_by_appellation.get(appellation, []),
        )
        for appellation, count in verified_counts.items()
        if appellation in onstage_recurring
        or appellation in mentioned_retain
        or appellation in statistical_retain
    ]
    # 排序主键换成"全文覆盖广度 + 命中量"，在场证据数退为次级信号：裁决闸对出场
    # 密集的主角反而更容易判 other（引句里叙述多、纯对话少），用它当主键会系统性
    # 地把主角排到配角后面。
    ranked.sort(key=lambda item: (
        0 if item[0] in mentioned_retain and item[2] == 0 else -1,
        -item[4], -item[3], -item[2], item[1] or item[0],
    ))
    # 不再截断人数：旧的 20 上限来自过期前提（首版曾约束 ≤8 个）。这里只留一道
    # 失控护栏（不是质量门槛）——真实作品不会触及，触发多半是资格裁决整体失效，
    # 会让下游详情生成扇出成几百次调用。
    if len(ranked) > BIBLE_ROSTER_RUNAWAY_MAX:
        raise StageError(
            "人物点名",
            [f"候选 {len(ranked)} 个超过 {BIBLE_ROSTER_RUNAWAY_MAX} 人失控上限，疑似资格裁决整体失效"],
        )
    result = ranked
    # 记账：供人工从数字上判断「这次点名是不是明显偏少/裁决通过率是不是异常低」，
    # 不是核验闸门本身。
    log_provider_call(
        "character_roll_call_coverage", config.MODEL_TEXT, "OK", None, 0,
        meta={
            **_failed_chunk_meta(chunks, chunk_results),
            "candidates": len(candidates),
            "evidence_total": evidence_total,
            "structural_gate_passed": structural_pass,
            "presence_verdict_passed": verdict_pass,
            "must_cover": len(result),
            "fulltext_mentions": sum(item[3] for item in result),
            "personhood_person": sum(
                1 for value in personhood_by_appellation.values() if value == "person"
            ),
            "personhood_deferred": sum(
                1 for value in personhood_by_appellation.values() if value == "uncertain"
            ),
            "true_names_bound": sum(
                1 for appellation, formal in formal_names.items()
                if formal and formal != appellation
            ),
        },
    )
    return result


def _bible_covers_name(bible: Bible, appellations: set[str]) -> bool:
    """必收名单条目是否已经在人物谱里覆盖。`appellations` 是调用方传入的待匹配称呼
    集合（如 `{primary_appellation, formal_name}`，已过滤空值）——传集合而不是单个
    字符串，是因为一个必收条目现在可能同时有原文常用称呼（可以是绰号）和正式姓名
    两种写法，任一种在人物谱里出现都算已覆盖。

    命中条件二选一：
    1. 待匹配称呼中任一项与角色 `character.name` 存在子串关系（原有行为不变，
       允许模型用更完整的正式姓名收录同一人）；
    2. 待匹配称呼中任一项与角色 `character.aliases[].text` **精确相等**（不用
       子串——别名本身已经是核验过的精确称谓，用子串关系反而可能对上不相关的短
       别名，比如单字"老"作为子串命中一堆无关别名；相等判断更安全，且 aliases.text
       本身就是逐字原文称谓，绰号能否被人物谱覆盖就看这一条）。

    `appellations` 为空集合时内层循环天然不执行、直接返回 False（未覆盖）——不是
    因为显式判断"集合为空就跳过检查"而短路，是 `any()`/for 循环对空可迭代对象的
    自然行为，不会误判为已覆盖。
    """
    for character in bible.characters:
        for appellation in appellations:
            if not appellation:
                continue
            if (
                appellation == character.name
                or appellation in character.name
                or character.name in appellation
            ):
                return True
            if any(appellation == alias.text for alias in character.aliases):
                return True
    return False
