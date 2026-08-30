"""人物谱补充——按缺失候选补录角色详情。"""
from __future__ import annotations



from app import config
from app.db import log_provider_call
from app.harness import model_gateway
from app.schemas import (Bible, Character, extract_json)

from .alias_backfill import _verify_character_aliases_for_subset
from .bible_shared import _bible_short_json_call_meta
from .constants import SYSTEM_PREFIX
from .identity_evidence import _appearance_evidence_verified
from .roster_recurring import _BibleSupplement, _bible_covers_name


async def _supplement_bible_characters(bible: Bible, missing: list[tuple[str, str, int]],
                                       chapters_text: str, *,
                                       chapters_by_idx: dict[int, str],
                                       visual_style_prompt: str | None = None,
                                       project_id: str | None = None) -> list[str]:
    """为必收名单里仍然缺席的角色补一次条目；失败或不合格就放弃该角色。

    这一步刻意放在 AgentLoop 之外：人物谱缺角色是质量问题，不该把整个项目
    卡在 bible_status=warning 上（那会连带停掉定妆照与场景库）。

    `missing` 是 `_recurring_character_names` 产出的 (primary_appellation,
    formal_name, verified_onstage_count) 三元组：formal_name 非空时指示模型把它
    用作 character.name、并把 primary_appellation 登记为一条别名（绰号做正式姓名
    的补充记录，而不是丢弃）；formal_name 为空时直接用 primary_appellation 作
    character.name。补录角色新增的 aliases 同样只是模型申报，append 成功后必须
    过与主生成同一套核验（`_verify_character_aliases_for_subset`，只核验本次新增
    的角色，不对已核验过的角色重复发起模型调用）才会真正登记。

    chapters_by_idx：全书原文查找表（`_chapters_by_idx(chapters)`），用于核验模型
    随外观一并申报的 source_evidence（本函数没有 AgentLoop 重试，核验失败的证据
    条目直接从列表里剔除，不拒绝整个角色）与随 name 一并申报的 aliases（核验失败
    的别名条目同样直接剔除，不影响角色本身的补录）。
    """
    from app.refs import (
        PRODUCTION_APPEARANCE_MAX_CHARS,
        PRODUCTION_APPEARANCE_MIN_CHARS,
    )

    expected_names = {(formal_name or appellation) for appellation, formal_name, _ in missing}
    wanted_lines = [
        (
            f'{formal_name}（原文常用称呼"{appellation}"，已核验在场证据 {count} 条）'
            if formal_name else
            f"{appellation}（已核验在场证据 {count} 条）"
        )
        for appellation, formal_name, count in missing
    ]
    wanted = "、".join(wanted_lines)
    style = visual_style_prompt or bible.world.visual_style_canonical
    prompt = f"""任务：为下列【已确认重要但人物谱漏收】的角色补出角色条目，用于 AI 视频生成的一致性控制。

必须补录的角色（name 取值规则见要求 6；不得改写或合并）：
{wanted}

已收录角色（不要重复输出）：{'、'.join(c.name for c in bible.characters) or '无'}
全片统一画风（角色外观必须服从）：{style}

要求：
1. 只输出上面「必须补录」的角色，也不要多输出别人。唯一例外：其中某个名字如果其实不是人物（是宗门、地名或法宝名），跳过它，不要硬编成角色。
2. appearance_canonical 是固定外观锚点串：{PRODUCTION_APPEARANCE_MIN_CHARS}~{PRODUCTION_APPEARANCE_MAX_CHARS} 字，只写常规完整着装、中性站姿下
   可直接看见并能跨镜稳定复现的静态形态；不写性格、情绪、眼神行为，不得写裸体或私密身体
   部位。通用形态（性别年龄感/发型发色/服装款式颜色）原文没写时可按题材合理设定，不需要
   举证；是否再写 1 个标志性特征取决于原文对这个角色本人是否确有描写——有就写且逐字取用
   并在 source_evidence 里举证（evidence_chapter_index + 40 字以内的原文逐字短句，短句里
   要能直接读出是在写这个角色本人，不是同段落里的其他人），没有就不写，不必凑数。
3. role 取"主角|重要配角|反派"之一。
4. speech_style 15~30 字，描述句长习惯/口头禅/敬语习惯。
5. relationships.to 只能指向【已收录角色或本次补录角色】的 name；无法确定就留空数组。
6. name 取值：上面的写法括号里标了"原文常用称呼『XX』"的条目，character.name 用括号外给出的
   正式姓名，并把括号里那个原文常用称呼登记为一条 aliases（text=该称呼，name_kind 按语境判断
   取 personal_name/honorific/referential，evidence_chapter_index + evidence_quote 给一条
   能同时看到这个称呼与这个正式姓名的原文逐字引句，原样照抄不得改写；找不到这种共现就不要
   申报这条别名，不影响角色本身的补录）；没有标注原文常用称呼的条目，character.name 直接用
   给出的那个写法，不需要另外申报别名。

小说文本：
{chapters_text}

输出 JSON Schema：
{{"characters": [{{"name": str, "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}], "source_evidence": [{{"evidence_chapter_index": int, "evidence_quote": str}}], "aliases": [{{"text": str, "name_kind": "personal_name|honorific|referential", "evidence_chapter_index": int, "evidence_quote": str}}]}}]}}"""
    try:
        raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX},
             {"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=8192,
            call_meta=_bible_short_json_call_meta({
                "stage": "人物谱补录",
                "stage_key": "character_bible_supplement",
                "call_role": "stage_generate",
                "call_role_label": "人物谱补录",
                "expected_json": True,
            }),
        )
        drafted = _BibleSupplement.model_validate(extract_json(raw)).characters
    except Exception as exc:  # noqa: BLE001 - 补录失败保留已有人物谱，不阻断下游
        log_provider_call(
            "character_bible_supplement", config.MODEL_TEXT, "FAILED", None, 0,
            meta={"error": str(exc)[:300]},
        )
        return []
    added: list[str] = []
    added_characters: list[Character] = []
    for character in drafted:
        name = (character.name or "").strip()
        if (
            name not in expected_names
            or _bible_covers_name(bible, {name})
            or not PRODUCTION_APPEARANCE_MIN_CHARS
            <= len(character.appearance_canonical)
            <= PRODUCTION_APPEARANCE_MAX_CHARS
        ):
            continue
        character.name = name
        character.ref_image_path = None
        character.portrait_prompt_override = None
        # 没有 AgentLoop 重试可用：核验失败的证据条目直接剔除（角色照常补录，只是
        # 这条特征失去了申报的举证），不因为一条证据不实就放弃整个角色补录。
        character.source_evidence = [
            evidence for evidence in character.source_evidence
            if _appearance_evidence_verified(
                chapters_by_idx, {character.name},
                evidence.evidence_chapter_index, evidence.evidence_quote,
            )
        ]
        bible.characters.append(character)
        added.append(name)
        added_characters.append(character)
    # 关系只能指向最终名单里的人，否则 validate_bible 会因「关系指向未知角色」退回。
    names = {c.name for c in bible.characters}
    for character in bible.characters:
        character.relationships = [
            relation for relation in character.relationships if relation.to in names
        ]
    # 补录角色声明的 aliases 同样只是申报，必须过与主生成同一套核验才能真正登记——
    # 只对本次新增的角色调用，避免对已核验过的角色重复发起模型调用（难点 C 第 4 点）。
    if added_characters:
        await _verify_character_aliases_for_subset(
            bible, added_characters, chapters_by_idx, project_id=project_id,
        )
    return added
