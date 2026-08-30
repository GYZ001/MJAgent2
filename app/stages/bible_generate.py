"""人物谱生成——角色详情生成与 generate_bible/generate_scene_bible 主入口。"""
from __future__ import annotations

import asyncio
import hashlib
import time

from pydantic import BaseModel

from app import config
from app.db import get_setting, log_provider_call
from app.harness import model_gateway
from app.loops import AgentLoop, AgentLoopPolicy
from app.schemas import (Bible, Character, Scene)
from app.validators import (validate_bible,
                            validate_scene_bible)

from .alias_backfill import (
    _verify_character_aliases_for_subset,
    _verify_character_aliases_in_place,
)
from .bible_models import (
    _BibleRosterDraft,
    _BibleRosterEntry,
    _CharacterDetail,
    _character_detail_evidence_pack,
    _character_stub_from_roster,
    _normalize_must_cover_rows,
    _normalize_roster_against_candidates,
    _sanitize_character_detail_payload,
    _validate_bible_roster,
)
from .bible_paratext import (
    BIBLE_APPEARANCE_FIELD_RULE,
    BIBLE_DETAIL_EVIDENCE_MAX_CHARS,
    BIBLE_DETAIL_FIRST_TOKEN_TIMEOUT_S,
    BIBLE_DETAIL_MAX_ATTEMPTS,
    BIBLE_DETAIL_MAX_TOKENS,
    BIBLE_DETAIL_TIMEOUT_S,
    _chapters_without_paratext,
)
from .bible_shared import _bible_short_json_call_meta, _chapters_by_idx, _render_bible_source
from .common import StageError, _run_with_agent_loop
from .constants import SYSTEM_PREFIX
from .identity_evidence import _appearance_evidence_verified, _validate_appearance_evidence
from .roster_recurring import (
    _attach_roster_source_appellations,
    _bible_covers_name,
    _recurring_character_names,
)


async def _generate_character_detail(
    entry: _BibleRosterEntry,
    *,
    roster_names: list[str],
    evidence_pack: str,
    style: str,
    era: str = "",
    chapters_by_idx: dict[int, str],
    project_id: str | None,
) -> Character | None:
    from app.refs import PRODUCTION_APPEARANCE_MAX_CHARS, PRODUCTION_APPEARANCE_MIN_CHARS

    base_pack = evidence_pack[:BIBLE_DETAIL_EVIDENCE_MAX_CHARS]
    last_error = ""
    for attempt in range(1, BIBLE_DETAIL_MAX_ATTEMPTS + 1):
        pack = base_pack if attempt == 1 else base_pack[: max(2000, len(base_pack) // 2)]
        prompt = f"""任务：只为一个已确认角色生成角色详情。角色名字与角色类型已经由上游锁定，不得更改。
目标角色：{entry.name}
角色类型：{entry.role}
原文称呼：{'、'.join(entry.source_appellations) or entry.name}
完整角色名单（relationships.to 只能从这里选择）：{'、'.join(roster_names)}
统一画风：{style}
世界年代/社会形态：{era or '原文未明确，必须从证据包的社会制度、材质和服装称谓保守判断'}

{BIBLE_APPEARANCE_FIELD_RULE}

要求：appearance_canonical {PRODUCTION_APPEARANCE_MIN_CHARS}~{PRODUCTION_APPEARANCE_MAX_CHARS} 字；period_costume_canonical 20~60 字，明确该年代、地域/宗门、身份层级下可用的服装形制、面料、鞋履、束发与禁用的现代/错代元素，并与原文直接服装描写一致；speech_style 15~30 字；只写该角色；不确定的关系、别名、标志性特征证据留空。source_evidence 引句必须不超过 40 字且逐字来自证据包。

该角色的小证据包（不是全书）：
{pack}

输出 JSON Schema：
{{"appearance_canonical": str, "period_costume_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}], "aliases": [{{"text": str, "name_kind": str, "evidence_chapter_index": int, "evidence_quote": str}}], "source_evidence": [{{"evidence_chapter_index": int, "evidence_quote": str}}]}}"""
        started = time.time()
        try:
            detail = await asyncio.wait_for(
                model_gateway.chat_structured(
                    [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": prompt}],
                    model_type=_CharacterDetail,
                    validate=None,
                    normalize_payload=_sanitize_character_detail_payload,
                    operation_id=(
                        f"character_bible_detail:{project_id or ''}:{entry.name}:{attempt}"
                    ),
                    temperature=0.35 if attempt == 1 else 0.15,
                    max_tokens=BIBLE_DETAIL_MAX_TOKENS,
                    # 外层 attempt 循环是本函数唯一的重试预算：内层结构化重试关掉，
                    # 让格式/语义失败原样抛出，由外层换温度重跑（保持"每个角色各自
                    # 重试、互不影响"的语义），不再让网关吞掉一轮。
                    format_retry_limit=0,
                    semantic_retry_limit=0,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "角色详情生成",
                        "stage_key": "character_bible_detail",
                        "call_role": "stage_generate" if attempt == 1 else "stage_repair",
                        "call_role_label": "单角色详情",
                        "character_name": entry.name,
                        "attempt": attempt,
                        "input_chars": len(pack),
                        "project_id": project_id,
                        "first_token_timeout_s": BIBLE_DETAIL_FIRST_TOKEN_TIMEOUT_S,
                    }),
                ),
                timeout=BIBLE_DETAIL_TIMEOUT_S,
            )
            character = Character(
                name=entry.name,
                role=entry.role,
                appearance_canonical=detail.appearance_canonical,
                personality=detail.personality,
                speech_style=detail.speech_style,
                relationships=detail.relationships,
                aliases=detail.aliases,
                source_evidence=detail.source_evidence,
                presence_status=entry.presence_status,
                importance_score=entry.importance_score,
                importance_signals=entry.importance_signals,
                portrait_eligible=True,
                appearance_status="grounded",
                period_costume_canonical=detail.period_costume_canonical,
            )
            if not PRODUCTION_APPEARANCE_MIN_CHARS <= len(character.appearance_canonical) <= PRODUCTION_APPEARANCE_MAX_CHARS:
                raise ValueError("appearance_canonical 长度越界")
            if not 20 <= len(character.period_costume_canonical) <= 60:
                raise ValueError("period_costume_canonical 长度越界")
            character.relationships = [item for item in character.relationships if item.to in roster_names]
            character.source_evidence = [
                item for item in character.source_evidence
                if _appearance_evidence_verified(
                    chapters_by_idx, {entry.name}, item.evidence_chapter_index, item.evidence_quote,
                )
            ]
            log_provider_call(
                "character_bible_detail", config.MODEL_TEXT, "OK", None,
                int((time.time() - started) * 1000),
                meta={"character_name": entry.name, "attempt": attempt, "input_chars": len(pack)},
            )
            return character
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - only this character retries
            last_error = str(exc)
            log_provider_call(
                "character_bible_detail", config.MODEL_TEXT,
                "TIMEOUT" if isinstance(exc, TimeoutError) else "FAILED", None,
                int((time.time() - started) * 1000),
                meta={"character_name": entry.name, "attempt": attempt, "input_chars": len(pack), "error": last_error[:300]},
            )
    return None


async def _generate_character_detail_batch(
    entries: list[_BibleRosterEntry], chapters: list[dict], *, style: str, era: str = "",
    chapters_by_idx: dict[int, str], project_id: str | None,
) -> list[Character]:
    roster_names = [entry.name for entry in entries]
    tasks = [asyncio.create_task(_generate_character_detail(
        entry,
        roster_names=roster_names,
        evidence_pack=_character_detail_evidence_pack(
            chapters, [entry.name, *entry.source_appellations]
        ),
        style=style,
        era=era,
        chapters_by_idx=chapters_by_idx,
        project_id=project_id,
    )) for entry in entries]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    characters: list[Character] = []
    for entry, result in zip(entries, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException) or result is None:
            # 名单已锁定。详情失败留下占位，禁止把主角/配角从人物谱抹掉。
            characters.append(_character_stub_from_roster(entry))
            continue
        characters.append(result)
    return characters


async def generate_bible(chapters: list[dict], feedback: str = "", previous_bible: dict | None = None,
                         project_id: str | None = None,
                         visual_style_prompt: str | None = None) -> Bible:
    """Generate a small roster first, then fan out bounded per-character requests."""
    chapters = await _chapters_without_paratext(chapters)
    chapters_by_idx = _chapters_by_idx(chapters)
    must_cover = _normalize_must_cover_rows(
        await _recurring_character_names(chapters, project_id=project_id)
    )
    if not must_cover:
        raise StageError(
            "角色圣经",
            ["人物点名未产出任何经原文核验的角色候选，拒绝在无证据情况下编造人物谱"],
            exit_reason="empty_verified_roster",
        )

    must_cover_lines = [
        (
            f"{formal or appellation}（原文称呼：{'、'.join(dict.fromkeys([appellation, *aliases]))}；"
            f"核验在场 {onstage_count} 次；全文命中 {mention_count} 次；覆盖 {chapter_count} 章）"
        )
        for appellation, formal, onstage_count, mention_count, chapter_count, aliases in must_cover
    ]
    previous_names = [
        item.get("name", "") for item in (previous_bible or {}).get("characters", [])
        if item.get("name")
    ]
    forced_style = visual_style_prompt or ""
    roster_context = "\n".join(f"- {line}" for line in must_cover_lines) or "- 暂无已核验候选"
    roster_prompt = f"""任务：根据已经完成代码归并和在场核验的候选摘要，只确定人物谱最终角色名单、角色类型和世界观；不要生成外观、性格、台词风格、关系或证据。

已核验候选摘要：
{roster_context}

规则：
1. 候选摘要来自前 20 章单章点名、身份归一、在场核验与全文检索；不得新增摘要中没有的人物，总数不超过 20。
2. 所有候选都必须收录；role 只负责区分主次，不得删除低频但已核验在场的候选。全文命中/覆盖章节用于判断重要程度，在场证据用于判断是否真实出场，二者不能互相替代。
3. name 必须使用括号外的正式姓名；若括号外仍是描述性称呼，说明全文尚未揭示真名，才可暂用该称呼。source_appellations 必须完整收录括号内原文称呼。
4. 同一候选行内的正式姓名、绰号、描述性称呼属于同一人物，严禁拆成多个角色。
5. 用户反馈：{feedback.strip() or '无'}。
6. 历史人物谱角色仅供返工对照，不得绕过候选摘要新增人物：{'、'.join(previous_names) or '无'}。
7. visual_style_canonical：{forced_style or '按古典修仙题材生成 25~40 字的统一 CG/动画/漫画/插画画风'}。
8. era 与 genre 只写简短题材标签；不得复述小说内容。

输出 JSON Schema：
{{"characters": [{{"name": str, "role": "主角|重要配角|反派", "source_appellations": [str]}}], "world": {{"era": str, "genre": str, "visual_style_canonical": str}}}}"""
    roster_loop = AgentLoop(
        stage_key="character_bible_roster",
        contract_key="character_bible_roster",
        goal="确定人物谱角色名单与统一世界观",
        scope_type="project",
        scope_id=project_id or hashlib.sha256(roster_context.encode("utf-8")).hexdigest()[:16],
        artifact_type="character_bible_roster",
        policy=AgentLoopPolicy(
            max_iterations=2,
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=1,
            allow_warning_candidate=False,
            repair_all_blockers=True,
        ),
    )

    def validate_roster(candidate: _BibleRosterDraft) -> list[str]:
        _normalize_roster_against_candidates(candidate, must_cover, chapters)
        if visual_style_prompt:
            candidate.world.visual_style_canonical = visual_style_prompt
        return _validate_bible_roster(candidate)

    roster = await _run_with_agent_loop(
        "人物名单", "character_bible_roster", roster_prompt, _BibleRosterDraft,
        validate_roster, loop=roster_loop, temperature=0.3, max_tokens=4096,
        repair_user_prompt_limit=16000, repair_candidate_limit=5000,
    )
    _normalize_roster_against_candidates(roster, must_cover, chapters)
    if visual_style_prompt:
        roster.world.visual_style_canonical = visual_style_prompt
    style = roster.world.visual_style_canonical
    characters = await _generate_character_detail_batch(
        roster.characters,
        chapters,
        style=style,
        era=roster.world.era,
        chapters_by_idx=chapters_by_idx,
        project_id=project_id,
    )
    bible = Bible(world=roster.world, characters=characters)
    await _verify_character_aliases_in_place(bible, chapters, project_id=project_id)
    for character in bible.characters:
        entry = next((item for item in roster.characters if item.name == character.name), None)
        if entry is not None:
            _attach_roster_source_appellations(character, entry, chapters)

    missing = [
        item for item in must_cover
        if not _bible_covers_name(
            bible, {value for value in (item[0], item[1], *item[5]) if value}
        )
    ]
    if missing:
        # Reuse the same single-character primitive; no batch/full-source supplement request.
        existing_names = {character.name for character in bible.characters}
        entries = [
            _BibleRosterEntry(
                name=formal or appellation,
                role="重要配角",
                source_appellations=list(dict.fromkeys([appellation, *aliases])),
                # 跟主路径同一条线：appellation 是这个候选进名单的身份标识，
                # 点名申报的 aliases 没核验过，不能走免检通道入谱。
                unverified_appellations=[
                    name for name in dict.fromkeys(aliases) if name != appellation
                ],
            )
            for appellation, formal, _onstage, _mentions, _chapters, aliases in missing
            if (formal or appellation) not in existing_names
        ]
        supplemented = await _generate_character_detail_batch(
            entries,
            chapters,
            style=style,
            era=roster.world.era,
            chapters_by_idx=chapters_by_idx,
            project_id=project_id,
        )
        bible.characters.extend(supplemented)
        if supplemented:
            await _verify_character_aliases_for_subset(
                bible, supplemented, chapters_by_idx, project_id=project_id,
            )
            for character, entry in zip(supplemented, entries, strict=False):
                _attach_roster_source_appellations(character, entry, chapters)

    valid_names = {character.name for character in bible.characters}
    for character in bible.characters:
        character.relationships = [
            relation for relation in character.relationships if relation.to in valid_names
        ]
    errors = validate_bible(bible) + _validate_appearance_evidence(bible, chapters_by_idx)
    if errors:
        raise StageError("角色圣经", errors, exit_reason="local_detail_blockers")
    return bible


# ---------- A2. 场景圣经（场景图素材库的规范场景，跨集场景一致性核心） ----------

class _SceneBibleDraft(BaseModel):
    """场景圣经输出合同（仅生成期使用）：一组规范场景。"""

    scenes: list[Scene]


async def generate_scene_bible(chapters: list[dict], bible: Bible,
                               feedback: str = "", project_id: str | None = None) -> list[Scene]:
    """从原文提取「规范场景」清单，作为场景图素材库的底稿（与 generate_bible 同构）。
    每个场景给 name（稳定短标签）+ scene_canonical（固定场景锚点串，画风约束与人物锚点一致，
    按 bible.world.visual_style_canonical 是否为照片级真人摄影预设二选一：非摄影风格必须
    CG/动画/漫画类非真人风格，否则后续 Seedance/Seedream 易因疑似真人报错；摄影风格则相反，
    要求真实材质与摄影级细节）。"""
    from app.refs import SCENE_CANONICAL_MAX_CHARS, SCENE_CANONICAL_MIN_CHARS
    from app.visual_styles import is_photographic_style_prompt
    chapters_text = _render_bible_source(chapters)
    style = bible.world.visual_style_canonical
    genre = bible.world.genre or ""
    feedback_part = ""
    if feedback.strip():
        feedback_part = f"\n人工打回重生要求（最高优先级）：\n{feedback.strip()}\n"
    if is_photographic_style_prompt(style):
        scene_style_rule = (
            f'4. 【硬性约束】scene_canonical 必须贴合全片画风「{style}」，是照片级摄影质感的'
            "实景环境描述，允许并鼓励真实材质、自然光影与摄影级细节；场景本身仍是虚构地点，"
            "不指向可识别的真实地标、真实机构或真实商业品牌名称。"
        )
    else:
        scene_style_rule = (
            f'4. 【硬性约束】scene_canonical 必须贴合全片画风「{style}」，是 CG/动画/漫画/插画类的'
            '非真人渲染场景（写实质感氛围词可保留），严禁"真人实拍/实景照片/摄影棚实拍"这类描述'
            "（否则后续图像/视频接口会因疑似真人实景报错）。"
        )
    prompt = f"""任务：从小说文本中提取【规范场景清单】，用于后续 AI 视频生成的场景一致性控制（场景图素材库）。

全片画风（场景锚点必须与之一致）：{style}
题材：{genre or '（未标注）'}

要求：
1. 只收录【反复出现 / 有戏份 / 画面感强】的关键场景（如主角居所、宗门广场、夜晚密林、朝堂等），最多 12 个；一次性出现的过场地点不要收录。
2. name：稳定的场景短标签（4~10 字，如"宗门广场""破败客栈内"），后续所有分镜的场景都收敛到这些名字，便于跨集复用同一张场景图。name 之间不要语义重复。
3. scene_canonical 是该场景的"固定场景锚点串"：{SCENE_CANONICAL_MIN_CHARS}~{SCENE_CANONICAL_MAX_CHARS} 字（这是硬门禁，多一个字整份清单都会被拒收，写完请数一遍），必须包含 地点/室内外/典型光线时段/标志性陈设或建筑/整体氛围色调。只写视觉可见的环境信息，不写人物、不写剧情动作。原著未描写处按题材与画风合理补全并保持内部一致。
{scene_style_rule}
5. location_kind 取"室内/室外/其他"之一。

小说文本：
{chapters_text}{feedback_part}

输出 JSON Schema：
{{"scenes": [{{"name": str, "scene_canonical": str, "location_kind": "室内|室外|其他"}}]}}"""
    loop = AgentLoop(
        stage_key="scene_bible",
        contract_key="scene_bible",
        goal="从原文章节提取跨集复用、来源可追溯的规范场景",
        scope_type="project",
        scope_id=project_id or hashlib.sha256((chapters_text + style).encode("utf-8")).hexdigest()[:16],
        artifact_type="scene_bible",
        policy=AgentLoopPolicy(
            max_iterations=min(max(int(get_setting("max_repair_attempts") or 4), 1), 4),
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=False,
        ),
    )
    draft = await _run_with_agent_loop(
        "场景圣经", "scene_bible", prompt, _SceneBibleDraft,
        lambda d: validate_scene_bible(d.scenes), loop=loop, temperature=0.5,
        # 与人物谱同因：修复轮不能把小说正文截掉，否则只会反复重排开头几个场景。
        repair_user_prompt_limit=None,
    )
    return list(draft.scenes)
