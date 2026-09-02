"""别名取证——候选判别证据解析与别名回填/复核主入口。

本文件原有的人物点名在场裁决闸（卷宗检索 + 独立模型裁决）已随「人物谱旧点名
管线整体退场」（2026-09-01）删除：唯一调用方是同批删除的点名管线协作模块，
除测试外零生产调用方。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from app import config
from app.db import log_provider_call
from app.harness import model_gateway
from app.schemas import (Bible, Character, CharacterAlias,
                         extract_json)

from .alias_verdict import (
    _ALIAS_VERDICT_NO_MATCH_LABEL,
    _alias_verdict_call,
    _alias_verdict_candidates,
    _alias_verdict_dossier,
    _alias_verdict_pin_segment,
)
from .bible_shared import (
    ALIAS_BACKFILL_SOURCE_BUDGET_CHARS,
    _bible_short_json_call_meta,
    _chapters_by_idx,
)
from .cognition import build_chapter_cognition_card
from .constants import SYSTEM_PREFIX
from .identity_evidence import (
    _alias_declaration_verified,
    _find_alias_bridge_chapter,
)


def _alias_verdict_roster(bible: Bible) -> dict[str, list[str]]:
    """裁决候选面快照：规范名 -> [规范名, 已登记别名...]，取 `bible.characters` 当前
    状态的一次性快照。调用方（`_verify_character_aliases_in_place` /
    `backfill_character_aliases` / `reverify_character_aliases`）各自在本轮核验开始前
    构造一次，循环内对同一个 bible 的所有裁决调用共用同一份，不随本轮核验进度中途
    变化——结构判据要求同一输入任何时候重跑结果一致，如果候选面随每条别名的核验
    结果实时增减，同一批别名先后处理顺序不同会算出不同的候选集，不可复现。"""
    return {c.name: [c.name, *(a.text for a in c.aliases)] for c in bible.characters}


async def _alias_evidence_resolution(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    text: str,
    true_name: str,
    evidence_chapter_index: int,
    evidence_quote: str,
    *,
    roster: dict[str, list[str]],
    project_id: str | None = None,
    bible: Bible | None = None,
) -> dict[str, Any]:
    """别名证据判定的统一入口，供三处调用方（`_verify_character_aliases_in_place` /
    `backfill_character_aliases` / `reverify_character_aliases`）共用。分两段：

    第一段（既有逻辑不变）：模型申报的章节直接核验通过，就采信模型申报的
    (evidence_chapter_index, evidence_quote)；不通过时不直接拒绝——模型定位错了章节
    不代表申报的语义假设本身是错的，退一步交给 `_find_alias_bridge_chapter` 在全书
    范围内确定性检索桥接章。两条路径都没有可核验的证据 → 拒绝
    （reason="no_bridge_chapter"）。

    第二段（裁决闸，见本节顶部大注释）：无论证据来自哪条路径，"该章节同时出现别名与
    角色规范名（或已确认别名）"只证明"同章共现"，证明不了"指代同一人"——必须让模型
    看着这一章的真实原文，从该章出场的全部人物谱候选（`roster` 经
    `_alias_verdict_candidates` 结构性算出，见"真实误登记事故 2"）里判别称谓 text
    最可能指代谁；裁决结果必须恰好选中 true_name 本人，且支撑段号钉证在卷宗段号
    集合内，才算真正核验通过。选中候选集里的其他人（reason="candidate_mismatch"）、
    选"都不是/无法确定"（reason="candidate_uncertain"）、候选集为空（防御性分支，
    reason="no_verdict_candidates"，正常不应触发——true_name 或其已登记别名命中
    该章是本函数走到这一步的前提，必然会被 `_alias_verdict_candidates` 收进候选集，
    见该函数 docstring）、裁决调用失败（reason="verdict_call_failed"）、段号钉证
    失败（reason="segment_not_pinned"），一律拒绝（不确定不登记）。

    返回统一结构 {"accepted": bool, "chapter_idx": int|None, "quote": str,
    "reason": str}：accepted=True 时 chapter_idx/quote 是应当登记的证据（来自第一段，
    与裁决闸引用的卷宗原文无关——裁决闸只是额外必须通过的门槛，不改写已核验证据的
    内容），另带一个 "is_exclusive" 键（`response.is_exclusive_reference`，见裁决闸
    提示词"任务二"）供调用方构造 `CharacterAlias(..., is_exclusive=...)`；
    accepted=False 时 reason 是机器可读的拒绝原因，供调用方记账与复核报告，字典里
    不含 "is_exclusive"（不确定不登记的分支不产生任何应当登记的字段）。

    `bible`（可选，见 docs/CHARACTER_COGNITION_LAYER_DESIGN.md §4.3）：调用方若提供
    完整 `Bible`，会额外组装一张章级认知卡（`build_chapter_cognition_card`，候选范围
    限定为本次裁决闸算出的 `candidates`）注入 `_alias_verdict_call` 的提示词，帮助
    模型看到候选人跨章建立的归属/关系背景，不改变裁决规则本身。缺省 `None`（例如
    测试直接构造裁决场景、不需要认知卡）时行为与认知卡引入前完全一致。"""
    empty = {"accepted": False, "chapter_idx": None, "quote": "", "reason": ""}
    if _alias_declaration_verified(
        chapters_by_idx, anchor_texts, text, evidence_chapter_index, evidence_quote,
    ):
        resolved_chapter_index, resolved_quote = evidence_chapter_index, evidence_quote
    else:
        bridge = _find_alias_bridge_chapter(chapters_by_idx, anchor_texts, text)
        if bridge is None:
            return {**empty, "reason": "no_bridge_chapter"}
        resolved_chapter_index, resolved_quote = bridge

    chapter_text = chapters_by_idx.get(resolved_chapter_index, "")
    candidates = _alias_verdict_candidates(chapter_text, roster)
    if not candidates:
        return {**empty, "reason": "no_verdict_candidates"}
    # 卷宗证据锚点必须覆盖全部候选人，不能只锚定被测的这一位（真实误登记事故 2、
    # 见本节顶部大注释）：只把候选名单摆给模型看，卷宗本身若只收录"text 与被测
    # true_name 共现"的段落，模型就永远看不到"另一个候选人在这章别的地方被点名"
    # 这条关键证据——第 189 章"王有材默默站起身站在孟浩身后"这段原文既不含
    # "王师弟"也不含被测的"王腾飞"，只按 anchor_texts={"王腾飞"} 检索会把它漏掉，
    # 模型只能看着反复出现的"王腾飞"就近作答，重演确认偏误。把全部候选人（结构性
    # 算出，来自 `_alias_verdict_candidates`）的规范名与已登记别名一并纳入锚点，
    # dossier 的 anchor_only 类别就能把"王有材"那一段也按接近别名段落的程度收录
    # 进来，模型才有机会看到真正指向正确候选的证据，而不只是被反复出现的名字带偏。
    dossier_anchor_texts = set(anchor_texts) | {
        form for name in candidates for form in roster.get(name, [])
    }
    dossier = _alias_verdict_dossier(
        resolved_chapter_index, chapter_text, text, dossier_anchor_texts,
    )
    if not dossier:
        return {**empty, "reason": "no_verdict_dossier"}
    cognition_card = (
        build_chapter_cognition_card(
            bible, chapters_by_idx, resolved_chapter_index, character_names=candidates,
        )
        if bible is not None else None
    )
    try:
        response = await _alias_verdict_call(
            alias=text, true_name=true_name, dossier=dossier,
            candidates=candidates, project_id=project_id,
            cognition_card=cognition_card,
        )
    except Exception as exc:  # noqa: BLE001 - 裁决调用失败按不确定处理：不确定不登记
        log_provider_call(
            "character_alias_backfill_verdict", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"alias": text, "true_name": true_name, "error": str(exc)[:300]},
        )
        return {**empty, "reason": "verdict_call_failed"}
    if response.selected_candidate != true_name:
        reason = (
            "candidate_uncertain"
            if response.selected_candidate == _ALIAS_VERDICT_NO_MATCH_LABEL
            else "candidate_mismatch"
        )
        return {**empty, "reason": reason}
    if _alias_verdict_pin_segment(dossier, response.supporting_segment_index) is None:
        return {**empty, "reason": "segment_not_pinned"}
    return {
        "accepted": True, "chapter_idx": resolved_chapter_index,
        "quote": resolved_quote, "reason": "",
        # 排他性判据（见 _AliasExclusivityVerdictResponse.is_exclusive_reference 与
        # _alias_verdict_call 提示词"任务二"）：透传给三处调用方构造
        # CharacterAlias(..., is_exclusive=...)，不新增拒绝分支——三闸通过就照常
        # accepted=True，排他性只影响这条别名是否折进身份决议的 source_labels
        # （app.identity_authority.identity_authority_registry），不影响是否登记。
        "is_exclusive": response.is_exclusive_reference,
    }


async def _verify_character_aliases_for_subset(
    bible: Bible, characters: list[Character], chapters_by_idx: dict[int, str], *,
    project_id: str | None = None,
) -> dict[str, list[str]]:
    """`_verify_character_aliases_in_place` 的内层循环，抽成可传入显式 `characters`
    子集的辅助函数——供 `_verify_character_aliases_in_place`（传入 `bible.characters`
    全量，行为与抽取前完全一致）与 `_supplement_bible_characters`（补录 append 成功
    后只对本次新增角色调用，不对已核验过的角色重复发起模型调用）共用。

    候选面快照（`roster`，供裁决闸判别"这个称呼指代候选中的谁"）永远取自完整
    `bible.characters`，不受 `characters` 子集影响：核验范围可以只挑几个角色，
    但候选集必须是整本人物谱——否则会重演"真实误登记事故 2"同一形状的问题
    （裁决模型看不到正确候选，只能矮子里拔将军）。只处理 aliases 字段，绝不触碰
    角色的任何其它既有字段。"""
    roster = _alias_verdict_roster(bible)
    added: dict[str, list[str]] = {}

    async def _verify_one(character: Character) -> tuple[str, list[str]]:
        # 同一角色的别名必须串行：后一条要用前面已确认的 anchor_texts。
        # 不同角色互相独立，下面 gather 只并行角色，不并行同一角色内部。
        anchor_texts = {character.name}
        verified: list[CharacterAlias] = []
        added_texts: list[str] = []
        for item in character.aliases:
            text = (item.text or "").strip()
            if not text or text == character.name or text in anchor_texts:
                continue
            resolved = await _alias_evidence_resolution(
                chapters_by_idx, anchor_texts, text, character.name,
                item.evidence_chapter_index, item.evidence_quote,
                roster=roster, project_id=project_id, bible=bible,
            )
            if resolved["accepted"]:
                verified.append(CharacterAlias(
                    text=text, name_kind=item.name_kind,
                    evidence_chapter_index=resolved["chapter_idx"],
                    evidence_quote=resolved["quote"],
                    is_exclusive=resolved["is_exclusive"],
                ))
                anchor_texts.add(text)
                added_texts.append(text)
        character.aliases = verified
        return character.name, added_texts

    for name, added_texts in await asyncio.gather(*(_verify_one(character) for character in characters)):
        if added_texts:
            added[name] = added_texts
    return added


async def _verify_character_aliases_in_place(
    bible: Bible, chapters: list[dict], *, project_id: str | None = None,
) -> dict[str, list[str]]:
    """`generate_bible` 主链路核验：模型随人物谱正文一并申报的 aliases 同样只是申报，
    落库前必须过同一套代码核验（`_alias_evidence_resolution`，与回填函数共用；模型
    申报章节没通过共现闸时，退一步做全书桥接章检索，通过共现闸后还要再过一道桥接章
    原文独立裁决，见该函数 docstring）。只处理 aliases 字段，绝不触碰角色的任何其它
    既有字段。核验范围是 `bible.characters` 全量（内层循环见
    `_verify_character_aliases_for_subset`）。"""
    chapters_by_idx = _chapters_by_idx(chapters)
    return await _verify_character_aliases_for_subset(
        bible, bible.characters, chapters_by_idx, project_id=project_id,
    )


def _render_alias_backfill_source(
    chapters: list[dict], budget: int = ALIAS_BACKFILL_SOURCE_BUDGET_CHARS,
) -> str:
    """为别名回填渲染全书原文：块头强制显示原文章节序号（idx），不像 `_render_bible_source`
    那样优先用章节标题——回填要求模型精确报出 `evidence_chapter_index`，标题文本
    （可能是任意小说章节名）无法保证与 idx 对应，块头必须显式给出数字。"""
    valid = [ch for ch in chapters if (ch.get("content") or "").strip()]
    blocks: list[str] = []
    used = 0
    for chapter in valid:
        remain = budget - used
        if remain <= 200:
            break
        content = chapter["content"].strip()
        clipped = content[:remain]
        suffix = "……（原文过长已截断）" if len(content) > remain else ""
        blocks.append(f"【第 {chapter.get('idx', '?')} 章】\n{clipped}{suffix}")
        used += len(clipped)
    return "\n\n".join(blocks)


class _AliasBackfillDeclaration(BaseModel):
    """别名回填申报合同：模型只申报，是否登记由后端核验决定。"""

    character_name: str = ""
    text: str = ""
    name_kind: str = ""
    evidence_chapter_index: int = -1
    evidence_quote: str = ""


class _AliasBackfillDraft(BaseModel):
    aliases: list[_AliasBackfillDeclaration] = Field(default_factory=list)


async def backfill_character_aliases(
    bible: Bible, chapters: list[dict], *, project_id: str | None = None,
) -> dict[str, list[str]]:
    """窄口径别名回填（层一，用于当前项目一次性回填历史人物谱）：全书上下文，只产出并
    核验 `Character.aliases`，绝不改写人物谱任何其它既有字段（name/role/appearance_canonical/
    personality/speech_style/relationships/ref_image_path/portrait_prompt_override 全部
    原样保留，本函数不读写它们）。

    调用方式：协调层在部署窗口拿到已定稿的 `bible`（`Bible` 实例）与该项目全书 `chapters`
    （`list[dict]`，需含 `idx`/`content` 字段，与 `generate_bible` 输入同构）后直接：

        added = await backfill_character_aliases(bible, chapters, project_id=project_id)

    函数原地把核验通过的别名追加进对应 `Character.aliases`（幂等：已存在的别名文本、或与
    `character.name` 相同的文本不会重复追加，可安全重跑）；调用方随后自行把更新后的
    `bible` 序列化落库（本函数不做任何数据库读写——app/db.py 由其它 agent 并行改动，
    不在本函数职责范围内）。

    返回值 `{character_name: [本次新增别名文本, ...]}`，供调用方记账/日志展示；返回空 dict
    不代表失败（可能全书确实没有可核验的别名，也可能模型调用失败——两者都已通过
    `log_provider_call` 记录，失败时 status="FAILED"，全书无可核验别名时 status="EMPTY"）。

    核验规则见 `_alias_evidence_resolution`：模型只负责申报语义假设（character_name+text），
    代码逐字核验证据；模型申报的章节没通过共现闸时，代码在全书范围内确定性检索桥接章
    （`_find_alias_bridge_chapter`）作为兜底，找不到才真正拒绝——不确定不登记。
    禁止任何具体称谓的硬编码——判据只看结构（逐字子串命中 + 章节内共现），不针对
    "许师姐""小胖子"等具体词做特判分支。
    """
    chapters_by_idx = _chapters_by_idx(chapters)
    source = _render_alias_backfill_source(chapters)
    if not source.strip() or not bible.characters:
        return {}
    verdict_roster = _alias_verdict_roster(bible)
    roster_text = "、".join(
        c.name + (f"（已登记别名：{'、'.join(a.text for a in c.aliases)}）" if c.aliases else "")
        for c in bible.characters
    )
    prompt = f"""任务：通读下面的全书正文，为【已收录角色】找出他们在原文中出现过的其它称谓
（外号、尊称、代称、未揭晓真名前的描述性代称等），逐条给出可核验的证据。

已收录角色（只为这些人申报别名，不要发明角色列表之外的人）：
{roster_text}

要求：
1. 每条别名给五个字段：character_name（必须逐字等于上面角色列表中的某个名字）、
   text（该别名在原文中的逐字写法）、name_kind（personal_name=真名/honorific=尊称/
   referential=代称，按原文语境判断该称谓的性质）、evidence_chapter_index（该别名出现的
   证据所在章节序号，取该章节【第 N 章】块头里的数字 N——注意这不是该别名第一次出现的
   章节，而是该别名与角色正式姓名（或本角色另一条已确认别名）同时出现的那一章；很多别名
   （尤其是真名揭晓前的描述性代称）最早出现时全书还没交代过角色真名，那一章通不过共现
   核验，要在全书范围内找到两者共现的章节再申报——一旦登记成功，该别名会覆盖它在全书的
   所有出现，不局限于你引用的这一章）、evidence_quote（该共现章节原文中的逐字引句，必须
   原样照抄，一个字都不能改，也不要自己在引句前后加引号包裹——原文本来有没有引号就照抄
   有没有，不要额外添加；且这句引文所在章节里必须能同时找到该角色的正式姓名或本角色的
   另一条别名——如果找不到这种共现，说明这条证据站不住，不要申报）。
2. 不确定就不要申报：证据不足、记不清原文原句、全书都找不到别名与正式姓名共现、或章节
   序号可能有误的情况，宁可漏报，绝不能编造或近似改写引句——后端会逐字核对，改写过的
   引句或自行添加的引号包裹都无法通过、白白浪费申报。
3. 同一个别名同一个角色只申报一次；角色的正式姓名本身不算别名，不要重复申报。
4. 只申报别名本身，不要输出角色的外观、性格、关系等其它信息——这些字段本次不会被采用。

全书正文（部分较长章节可能已截断，仅代表你能看到的范围，不代表原文实际只有这些）：
{source}

输出 JSON Schema：
{{"aliases": [{{"character_name": str, "text": str, "name_kind": "personal_name|honorific|referential", "evidence_chapter_index": int, "evidence_quote": str}}]}}"""
    try:
        raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX},
             {"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=8192,
            call_meta=_bible_short_json_call_meta({
                "stage": "人物别名回填",
                "stage_key": "character_alias_backfill",
                "call_role": "stage_generate",
                "call_role_label": "别名回填",
                "expected_json": True,
                "project_id": project_id,
            }),
        )
        declared = _AliasBackfillDraft.model_validate(extract_json(raw)).aliases
    except Exception as exc:  # noqa: BLE001 - 回填失败保留已有人物谱，不阻断调用方
        log_provider_call(
            "character_alias_backfill", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"error": str(exc)[:300]},
        )
        return {}

    by_name = {c.name: c for c in bible.characters}
    grouped: dict[str, list[_AliasBackfillDeclaration]] = defaultdict(list)
    for item in declared:
        name = (item.character_name or "").strip()
        if name in by_name:
            grouped[name].append(item)

    added: dict[str, list[str]] = {}
    for name, items in grouped.items():
        character = by_name[name]
        anchor_texts = {character.name, *(a.text for a in character.aliases)}
        added_texts: list[str] = []
        for item in items:
            text = (item.text or "").strip()
            if not text or text in anchor_texts:
                continue
            resolved = await _alias_evidence_resolution(
                chapters_by_idx, anchor_texts, text, character.name,
                item.evidence_chapter_index, item.evidence_quote,
                roster=verdict_roster, project_id=project_id, bible=bible,
            )
            if resolved["accepted"]:
                character.aliases.append(CharacterAlias(
                    text=text, name_kind=item.name_kind,
                    evidence_chapter_index=resolved["chapter_idx"],
                    evidence_quote=resolved["quote"],
                    is_exclusive=resolved["is_exclusive"],
                ))
                anchor_texts.add(text)
                added_texts.append(text)
        if added_texts:
            added[name] = added_texts

    log_provider_call(
        "character_alias_backfill", config.MODEL_TEXT,
        "OK" if added else "EMPTY", None, 0,
        meta={
            "declared": len(declared),
            "verified": sum(len(v) for v in added.values()),
            "characters_touched": list(added.keys()),
        },
    )
    return added


async def reverify_character_aliases(
    bible: Bible, chapters: list[dict], *, project_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """层一别名复核：对 bible 中已登记的全部 `Character.aliases` 逐条重跑
    `_alias_evidence_resolution`（含裁决闸，见该函数与上方"A1b. 裁决闸"注释）。判不过
    不再删除条目——别名不删（见 CharacterAlias.is_exclusive 与
    app.production.prep_pack._prep_pack_bible_alias_owner 对"可检索的称呼线索"这条
    通道的依赖）：判不过就把该条目的 `is_exclusive` 标为 False、原样保留证据锚点，
    只是不再充当身份决议的排他凭证（不会折进
    `app.identity_authority.identity_authority_registry` 的 source_labels）。用于
    清理裁决闸补上之前落库的历史别名对排他性的误判，也可作为未来任何别名批次的通用
    复核工具重复调用（幂等：已经通过新闸的别名重跑仍然通过，不会被误降级）。

    背景（真实事故）：裁决闸补上之前落库的别名只过了"同章共现"这一道更弱的核验，
    证明不了"指代同一人"——已发生的误登记是模型在没看到桥接章原文的情况下凭全书
    记忆断言的语义假设，实际那一章里该称谓明确是另一个人；共现闸对几乎每章都出场的
    角色近乎零过滤力。本函数让所有既有别名重新过一遍现在的完整核验链，是清理这类
    历史误登记的通用工具，不针对任何具体人名做特判。

    调用方式：

        report = await reverify_character_aliases(bible, chapters, project_id=project_id)

    与 `backfill_character_aliases` 共享同一套核验入口、同一个"不确定不登记"默认——
    唯一区别是候选来源：这里不发起新的模型申报，直接把 `character.aliases` 里已有的
    (text, evidence_chapter_index, evidence_quote) 当作待复核的申报重新核验一遍。每个
    角色内部按既有别名的列表顺序增量建立 anchor_texts（与 backfill 的建表方式一致）：
    前面的别名先通过复核才会被后面同角色的别名当作共现锚点，避免"用一条本身尚未证实
    的别名去证明另一条别名"的循环依赖——判不过而被降级 is_exclusive=False 的别名同
    旧行为一样不加入 anchor_texts（它没有重新被核验为可靠共现锚点）。

    返回 `{character_name: [{"text":, "kept": bool, "reason": str}, ...]}`，逐条给出
    复核结论与拒绝原因（`kept=True` 时 `reason==""`）——`kept` 字段语义随本次改动调整
    为"是否仍保有排他凭证资格"，不再是"是否仍留在 aliases 里"（条目现在恒定保留），
    供调用方生成复核报告；只原地改写 `Character.aliases`，不触碰角色的任何其它既有
    字段。角色本来就没有别名的不出现在返回结果里。"""
    chapters_by_idx = _chapters_by_idx(chapters)
    roster = _alias_verdict_roster(bible)
    report: dict[str, list[dict[str, Any]]] = {}
    for character in bible.characters:
        if not character.aliases:
            continue
        anchor_texts = {character.name}
        kept: list[CharacterAlias] = []
        entries: list[dict[str, Any]] = []
        for item in character.aliases:
            text = (item.text or "").strip()
            resolved = await _alias_evidence_resolution(
                chapters_by_idx, anchor_texts, text, character.name,
                item.evidence_chapter_index, item.evidence_quote,
                roster=roster, project_id=project_id, bible=bible,
            )
            if resolved["accepted"]:
                kept.append(CharacterAlias(
                    text=text, name_kind=item.name_kind,
                    evidence_chapter_index=resolved["chapter_idx"],
                    evidence_quote=resolved["quote"],
                    is_exclusive=resolved["is_exclusive"],
                ))
                anchor_texts.add(text)
                entries.append({"text": text, "kept": True, "reason": ""})
            else:
                # 判不过就删的旧语义在这里退役：条目原样保留（证据锚点不改写，
                # 复核本身没有产出可信的替代证据），只把排他性降级为 False——
                # 该别名仍可用于共现/身份解析等"可检索的称呼线索"通道，只是不再
                # 充当身份决议的排他凭证。
                kept.append(CharacterAlias(
                    text=text, name_kind=item.name_kind,
                    evidence_chapter_index=item.evidence_chapter_index,
                    evidence_quote=item.evidence_quote,
                    is_exclusive=False,
                ))
                entries.append({
                    "text": text, "kept": False, "reason": resolved["reason"],
                })
        character.aliases = kept
        report[character.name] = entries
    return report
