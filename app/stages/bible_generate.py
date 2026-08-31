"""人物谱生成——角色详情生成与 generate_bible/generate_scene_bible 主入口。

架构转向（2026-08-31 用户拍板）：`generate_bible` 首版不再点名角色，只判定
世界观，见该函数 docstring 的完整理由。下方 `_BibleRosterDraft` /
`_normalize_must_cover_rows` / `_normalize_roster_against_candidates` /
`_validate_bible_roster` 与 `.alias_backfill` / `.roster_recurring` 的导入，
连同 `_generate_character_detail` / `_generate_character_detail_batch`
两个单角色详情生成原语，现在只服务于已退场的旧点名主链路——不再被
`generate_bible` 调用，保留只是因为：①它们仍被 `app/stages/__init__.py`
按原样重新导出、供 `tests/test_bible_parallelism.py` 等直接单元测试；
②与 roster_*.py 七个模块同批退场，删除是单独一轮任务（不在本次改动范围）。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time

from pydantic import BaseModel

from app import config
from app.db import get_setting, log_provider_call
from app.harness import model_gateway
from app.loops import AgentLoop, AgentLoopPolicy
from app.schemas import (Bible, Character, Scene, World)
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
from .bible_shared import _bible_short_json_call_meta, _render_bible_source
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


# ---------- A0. 世界观判定（首版人物谱唯一产出；不点名、不生成角色） ----------

BIBLE_WORLD_MAX_ATTEMPTS = 3
BIBLE_WORLD_TIMEOUT_S = 60.0
BIBLE_WORLD_MAX_TOKENS = 512

# 与 validate_bible 对 world 字段的判据同一份正则/同一条长度区间——世界观
# 判定单独成一次轻量调用后，仍不能比原来的整份人物谱校验更松。
_WORLD_PLACEHOLDER_PATTERN = re.compile(r"(待定|待补|未知|未命名|占位|暂定|TBD|unknown|placeholder)", re.I)


class _WorldOnlyDraft(BaseModel):
    """首版人物谱的唯一模型产出：只有世界观三要素，不含任何角色字段。"""

    era: str = ""
    genre: str = ""
    visual_style_canonical: str = ""


def _carry_forward_existing_bible_assets(
    previous_bible: dict | None,
) -> tuple[list[Character], list[Scene]]:
    """重新判定世界观时原样带出已有角色/场景，不重新生成也不清空。

    早期实现在没有 ``previous_bible`` 与"重新判定世界观"两种情况下都直接
    ``characters=[]``，把"首次生成、没有候选可点名"和"重新判定世界观、
    角色早已由映射台/分集反应式建卡积累出来"错误地合并成同一种「清空」——
    人工核验点出这会把用户攒了几十集的角色卡（连同人物谱里登记的场景卡）
    随手一个「重新判定世界观」按钮清零，已改正。

    ``previous_bible`` 是 ``json.loads(projects.bible_json)`` 的原始 dict（见
    app/domain/bible_ops/task_run.py 的 ``_bible_task``），没有旧 bible（真正
    首次生成）时两个列表都为空——这才是"没有候选可点名"的正确空态，不是
    「重新判定世界观也清空」。"""
    if not previous_bible:
        return [], []
    characters = [
        Character.model_validate(item) for item in previous_bible.get("characters", []) or []
    ]
    scenes = [
        Scene.model_validate(item) for item in previous_bible.get("scenes", []) or []
    ]
    return characters, scenes


def _validate_world_only(world: _WorldOnlyDraft) -> list[str]:
    errors: list[str] = []
    if _WORLD_PLACEHOLDER_PATTERN.search(world.era or ""):
        errors.append(f"world.era「{world.era}」是占位值，必须依据原文判定年代")
    if _WORLD_PLACEHOLDER_PATTERN.search(world.genre or ""):
        errors.append(f"world.genre「{world.genre}」是占位值，必须依据原文判定题材")
    if not 15 <= len(world.visual_style_canonical or "") <= 60:
        errors.append(
            f"world.visual_style_canonical 长度 {len(world.visual_style_canonical or '')} 字，"
            "要求 15~60 字"
        )
    return errors


async def generate_bible(chapters: list[dict], feedback: str = "", previous_bible: dict | None = None,
                         project_id: str | None = None,
                         visual_style_prompt: str | None = None) -> Bible:
    """首版人物谱只从原文判定世界观（era/genre/visual_style_canonical）；不再
    点名角色、不生成角色详情。首次生成时产出 ``characters=[]``；重新判定
    世界观时原样带出 ``previous_bible`` 已有的 characters/scenes（见下）。

    架构转向（2026-08-31 用户拍板，实测依据）：《我欲封天》1616 章只读前 20
    章点名，剩下 1596 章的人天然不在候选里——漏斗是候选 25 → 判为人 18 →
    最终 7，放宽出口变不出第 26 个候选，也没有任何窗口大小能解决（读 60 章
    仍漏 1556 章）。"这一集原文里出现了谁"是逐字可判的局部问题，"谁是全书
    重要角色"是模型也答不准的全局判断题，因此人物/场景改为按需增长：
      - 用户在映射台主动提名（``POST /projects/{project_id}/characters/nominate``，
        见 app/domain/bible_ops/nominate.py：命中已有角色登记别名，未命中的
        走 ``app.portraits.cards.ensure_character_card`` 建卡）；
      - 分集反应式发现在分镜展开前建卡（``ensure_character_card`` 的其它
        调用方，如 app/identity_adjudication.py，卡在展开前已建好）。

    本函数因此不再复用 roster_recurring/admission/chunk_plan/candidates/
    merge/personhood/truename 那整套点名-归并-核验流水线，也不再调用本文件
    自己的 `_generate_character_detail_batch`（单角色详情生成原语）——理由
    与保留方式见本文件顶部模块级注释。

    ``previous_bible``（重新判定世界观时的旧 bible_json）用于**原样带出**已有
    ``characters``/``scenes``，不重新生成也不清空：新架构下角色卡/场景卡是
    随分集陆续积累出来的（映射台提名或分镜展开前反应式建卡/assess_new_scene），
    「重新判定世界观并更换画风」这个动作的新语义只是替换 world 三要素，绝不
    能把用户攒了几十集的角色卡和场景卡清零——早期版本直接 `characters=[]`
    是把"首次生成没有候选可点名"和"重新判定世界观"两种情况错误地合并成了
    同一种「清空」处理，被人工核验点出后改正。没有 ``previous_bible``（真正
    首次生成）时才是空列表，这才是"没有候选可点名"的正确空态。
    """
    chapters = await _chapters_without_paratext(chapters)
    source_text = _render_bible_source(chapters)
    carried_characters, carried_scenes = _carry_forward_existing_bible_assets(previous_bible)
    forced_style = (visual_style_prompt or "").strip()
    feedback_part = (
        f"\n人工打回重生要求（最高优先级）：{feedback.strip()}\n" if feedback.strip() else ""
    )
    style_rule = (
        f"visual_style_canonical 必须逐字写成：{forced_style}" if forced_style else
        "visual_style_canonical：按题材生成 15~60 字的统一 CG/动画/漫画/插画画风描述"
    )
    prompt = f"""任务：只从原文判定这部作品的世界观三要素，不涉及任何具体角色、不输出角色名单。

要求：
1. era：简短年代/社会形态标签（如"上古洪荒""现代都市""架空古代修真"），依据原文的社会制度、称谓与器物判断，不得复述小说内容，不得留占位符。
2. genre：简短题材标签（如"东方玄幻""都市异能""历史架空"）。
3. {style_rule}
{feedback_part}
小说文本（节选，供年代/题材/画风判断）：
{source_text}

输出 JSON Schema：
{{"era": str, "genre": str, "visual_style_canonical": str}}"""
    last_error = "未知错误"
    for attempt in range(1, BIBLE_WORLD_MAX_ATTEMPTS + 1):
        started = time.time()
        try:
            draft = await asyncio.wait_for(
                model_gateway.chat_structured(
                    [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": prompt}],
                    model_type=_WorldOnlyDraft,
                    validate=None,
                    operation_id=f"bible_world_only:{project_id or ''}:{attempt}",
                    temperature=0.3 if attempt == 1 else 0.1,
                    max_tokens=BIBLE_WORLD_MAX_TOKENS,
                    format_retry_limit=1,
                    semantic_retry_limit=0,
                    call_meta=_bible_short_json_call_meta({
                        "stage": "世界观判定",
                        "stage_key": "character_bible_world",
                        "call_role": "stage_generate" if attempt == 1 else "stage_repair",
                        "call_role_label": "世界观判定",
                        "attempt": attempt,
                        "project_id": project_id,
                    }),
                ),
                timeout=BIBLE_WORLD_TIMEOUT_S,
            )
            if forced_style:
                draft.visual_style_canonical = forced_style
            errors_found = _validate_world_only(draft)
            if errors_found:
                raise ValueError("；".join(errors_found))
            log_provider_call(
                "character_bible_world", config.MODEL_TEXT, "OK", None,
                int((time.time() - started) * 1000),
                meta={"attempt": attempt, "project_id": project_id},
            )
            world = World(
                era=draft.era, genre=draft.genre,
                visual_style_canonical=draft.visual_style_canonical,
            )
            return Bible(world=world, characters=carried_characters, scenes=carried_scenes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 换温度重试，用尽后转 StageError
            last_error = str(exc)
            log_provider_call(
                "character_bible_world", config.MODEL_TEXT,
                "TIMEOUT" if isinstance(exc, TimeoutError) else "FAILED", None,
                int((time.time() - started) * 1000),
                meta={"attempt": attempt, "project_id": project_id, "error": last_error[:300]},
            )
    raise StageError("角色圣经", [f"世界观判定失败：{last_error}"], exit_reason="world_only_failed")


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
