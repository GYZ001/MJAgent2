"""LLM 流水线阶段：摘要 / 角色圣经 / 剧集规划 / 可拍剧本 / 分镜脚本。
每阶段 = prompt + Schema 校验 + 业务校验 + 修复回路（默认重试到 max_repair_attempts 次，失败抛 StageError——禁止兜底）。
校验类失败一律让模型继续修复；只有模型真正不可用（鉴权失败/参数 400/网关持续故障，
即 hiagent.ProviderError 透传）才立刻失败——重试同一 prompt 对这类错误无意义。
提示词正文与 docs/PROMPT_SPEC.md 保持同步，改动需先跑金样回归。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable

from pydantic import BaseModel

from app import config, hiagent
from app.character_policy import functional_extra_policy_text
from app.continuity import (adaptation_hook_errors, ensure_audio_timeline,
                            information_ledger_errors, ledger_context_for_shot,
                            sync_shot_continuity_fields)
from app.db import get_setting, log_provider_call
from app.evaluations.issues import issues_from_messages
from app.harness import model_gateway
from app.loops import AgentLoop, AgentLoopFailure, AgentLoopPolicy
from app.schemas import (Bible, CAMERA_MOVES, EMOTIONS, EpisodeScreenplay,
                         SHOT_SIZES, Scene, Shot, Storyboard, StoryboardOutline,
                         StoryboardOutlineShot, TRANSITIONS,
                         extract_json, schema_errors)
from app.validators import (ACTION_DESC_MIN_CHARS,
                            SCENE_SETTING_MAX_CHARS,
                            SOURCE_EXCERPT_MIN_CHARS,
                            defer_establishing_covers,
                            downgrade_outline_offbible_spoken,
                            rewrite_outline_abstract_covers,
                            TRANSITION_HINTS, _atomize_claim, _condense, _covers_has_crowd,
                            _covers_has_spoken, _covers_outside_spoken,
                            _too_similar,
                            normalize_action_desc, normalize_continuity,
                            normalize_offbible_characters, normalize_transition_visuals,
                            prefer_default_shot_durations,
                            relieve_spoken_overflow,
                            storyboard_shot_count_range,
                            validate_bible, validate_screenplay,
                            validate_scene_bible,
                            validate_storyboard,
                            validate_storyboard_shot_covers_outline,
                            validate_storyboard_outline,
                            validate_storyboard_preserves_key_content,
                            validate_storyboard_soundtrack)
from app.renderability import (
    ACTION_DESC_TARGET_MAX,
    ACTION_DESC_TARGET_MIN,
    KEY_LINES_MAX,
    KEY_PLOT_POINTS_MAX,
    KEY_PLOT_POINTS_MIN,
    PREFERRED_SHOT_DURATION_S,
    SHOT_HARD_MAX,
    SHOT_SOFT_MAX,
    SHOT_SOFT_MIN,
    renderability_prompt_block,
)

SYSTEM_PREFIX = (
    "你是专业的竖屏漫剧（动态漫画短剧）编剧与分镜师。\n"
    "你的观众看的是 AI 生成视频，不是摄影机实拍；请为模型能力写作，不为文学完整度炫技。\n"
    "输出规则：只输出一个 JSON 对象，无 Markdown 围栏，无解释文字。\n"
    "所有内容使用简体中文。"
)


class StageError(Exception):
    """阶段失败：errors 面向 UI 展示（PRD 原则 P2：失败要响）。"""

    def __init__(self, stage: str, errors: list[str]):
        self.stage = stage
        self.errors = errors
        super().__init__(f"[{stage}] " + "；".join(errors[:5]))


class StoryboardShotDraft(BaseModel):
    """逐镜头分镜输出合同：每次只让模型生成一个镜头，降低格式和内容同时失控的概率。"""

    episode_no: int
    shot: Shot
    is_final: bool = False


_SHOT_NULLABLE_TEXT_FIELDS = frozenset({
    "story_event_id", "purpose", "state_in", "primary_action", "emotion_beat",
    "state_out", "observed_state_out", "continuity_mode", "prompt_contract_version",
    "camera_angle", "spatial_anchor", "scene_name", "first_frame_desc",
    "last_frame_desc", "source_excerpt",
})
_SHOT_NULLABLE_LIST_FIELDS = frozenset({
    "dialogues", "new_information_ids", "reinforcement_info_ids", "characters_visible",
    "audio_cast", "audio_timeline", "reference_roles", "do_not_repeat", "risk_tags",
})


def normalize_storyboard_shot_candidate(
    obj: dict[str, Any],
    *,
    episode_no: int,
    shot_no: int,
    outline_story_event_id: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Losslessly normalize common LLM serialization mistakes at the boundary.

    The persisted ``Shot`` contract stays strict.  Only unambiguous null-to-
    empty conversions and server-owned identity fields are repaired here;
    objects and other incompatible values are deliberately left for schema
    validation instead of being hidden behind broad ``str(value)`` coercion.
    """
    normalized = dict(obj)
    changes: list[dict[str, Any]] = []

    if normalized.get("episode_no") != episode_no:
        changes.append({
            "field": "episode_no", "from": normalized.get("episode_no"),
            "to": episode_no, "reason": "server_authoritative",
        })
        normalized["episode_no"] = episode_no

    raw_shot = normalized.get("shot")
    if not isinstance(raw_shot, dict):
        return normalized, changes
    shot = dict(raw_shot)
    normalized["shot"] = shot

    if shot.get("shot_no") != shot_no:
        changes.append({
            "field": "shot.shot_no", "from": shot.get("shot_no"),
            "to": shot_no, "reason": "server_authoritative",
        })
        shot["shot_no"] = shot_no

    for field in _SHOT_NULLABLE_TEXT_FIELDS:
        if field in shot and shot[field] is None:
            shot[field] = ""
            changes.append({
                "field": f"shot.{field}", "from_type": "null", "to": "",
                "reason": "nullable_text_normalization",
            })
    for field in _SHOT_NULLABLE_LIST_FIELDS:
        if field in shot and shot[field] is None:
            shot[field] = []
            changes.append({
                "field": f"shot.{field}", "from_type": "null", "to": [],
                "reason": "nullable_list_normalization",
            })

    # The outline is the canonical event allocation when it provides one.
    if outline_story_event_id and shot.get("story_event_id") != outline_story_event_id:
        changes.append({
            "field": "shot.story_event_id", "from": shot.get("story_event_id"),
            "to": outline_story_event_id, "reason": "outline_authoritative",
        })
        shot["story_event_id"] = outline_story_event_id

    return normalized, changes


def _render_error_history(error_history: list[list[str]]) -> str:
    """渲染历次输出的问题记录（让模型看到自己反复犯的错）。
    与上一轮完全相同的轮次折叠成一行，避免把同样的错误抄 7 遍、把 prompt 撑爆。"""
    blocks: list[str] = []
    for i, errs in enumerate(error_history):
        if i > 0 and errs == error_history[i - 1]:
            blocks.append(f"【第 {i + 1} 次输出】问题与上一次完全相同（未改进）")
            continue
        keep = 12 if i >= len(error_history) - 2 else 5
        lines = [f"- {e}" for e in errs[:keep]]
        if len(errs) > keep:
            lines.append(f"- ……（另有 {len(errs) - keep} 条同轮问题从略）")
        blocks.append(f"【第 {i + 1} 次输出的问题】\n" + "\n".join(lines))
    return "\n".join(blocks)


async def _run_with_agent_loop(
    stage: str,
    stage_key: str,
    user_prompt: str,
    model_cls: type[BaseModel],
    business_validate: Callable[[BaseModel], list[str]],
    *,
    loop: AgentLoop,
    temperature: float = 0.7,
    max_tokens: int = 8192,
    repair_user_prompt_limit: int | None = 3000,
    repair_context: str | None = None,
    repair_output_contract: str | None = None,
    prefill: dict | None = None,
    storyboard_candidate_context: dict[str, Any] | None = None,
) -> BaseModel:
    """Phase 2 loop adapter: structured issues, bounded repair and persisted iterations."""
    base_call_meta = {
        "stage": stage,
        "stage_key": stage_key,
        "initiator_label": stage,
        "initiator_scope": "agent_loop",
        "contract_version": loop.contract.version,
    }

    async def producer(
        iteration_no: int,
        previous_raw: str | None,
        latest_issues,
        issue_history,
    ) -> str:
        if iteration_no == 1:
            return await model_gateway.chat(
                [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": user_prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                call_meta={
                    **base_call_meta,
                    "call_role": "stage_generate",
                    "call_role_label": "主生成",
                    "repair_round": 0,
                },
            )
        error_history = [[issue.message for issue in issues] for issues in issue_history]
        repair_index = iteration_no - 1
        repair_temp = 0.2 if repair_index < 3 else min(0.2 + 0.15 * (repair_index - 2), 0.8)
        emphasis = (
            ""
            if repair_index < 3
            else (
                f"\n\n【第 {repair_index} 次修复】历史问题已多次未解决。"
                "必须逐条定向修改，且不得引入新的合同违规。"
            )
        )
        if repair_context is not None:
            original_task = repair_context
        else:
            original_task = (
                user_prompt
                if repair_user_prompt_limit is None
                else user_prompt[:repair_user_prompt_limit]
            )
        repair_prompt = (
            "你此前的输出未通过校验。以下问题均为结构化硬门禁，不是泛泛建议：\n"
            + _render_error_history(error_history)
            + emphasis
            + "\n\n只修复上述问题，然后重新输出完整 JSON（不要解释，不要 Markdown）。"
            + "\n\n原任务要求：\n"
            + original_task
            + "\n\n最近一次候选：\n"
            + (previous_raw or "")[:6000]
            + (
                "\n\n本轮输出合同（最高优先级）：\n" + repair_output_contract
                if repair_output_contract
                else ""
            )
        )
        return await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": repair_prompt}],
            temperature=repair_temp,
            max_tokens=max_tokens,
            call_meta={
                **base_call_meta,
                "call_role": "stage_repair",
                "call_role_label": "定向修复",
                "repair_round": repair_index,
                "latest_issue_codes": [issue.code for issue in latest_issues[:10]],
                "latest_errors": [issue.message for issue in latest_issues[:10]],
            },
        )

    def evaluator(raw: str):
        try:
            obj = extract_json(raw)
        except ValueError as exc:
            messages = [str(exc)]
            return None, issues_from_messages(messages, subject=f"{loop.scope_type}:{loop.scope_id}")
        if model_cls is StoryboardShotDraft and storyboard_candidate_context is not None:
            obj, normalizations = normalize_storyboard_shot_candidate(
                obj,
                episode_no=int(storyboard_candidate_context["episode_no"]),
                shot_no=int(storyboard_candidate_context["shot_no"]),
                outline_story_event_id=str(
                    storyboard_candidate_context.get("outline_story_event_id") or ""
                ),
            )
            if normalizations:
                log_provider_call(
                    "storyboard_candidate_normalization", config.MODEL_TEXT,
                    "NORMALIZED", None, 0,
                    meta={**storyboard_candidate_context, "changes": normalizations},
                )
        if prefill and isinstance(obj, dict):
            obj.update(prefill)
        if (
            model_cls is StoryboardShotDraft
            and isinstance(obj, dict)
            and "shot" not in obj
            and isinstance(obj.get("shots"), list)
        ):
            messages = [
                "字段 shot：逐镜合同只允许单数 shot 对象，禁止 shots 数组；"
                f"当前一次输出了 {len(obj['shots'])} 个镜头。只保留当前镜，后续内容留给下一轮生成"
            ]
            instance = None
        else:
            instance, messages = schema_errors(model_cls, obj)
        if instance is not None:
            messages = business_validate(instance)
        return (
            instance,
            issues_from_messages(messages, subject=f"{loop.scope_type}:{loop.scope_id}"),
        )

    try:
        result = await loop.run(producer, evaluator)
    except AgentLoopFailure as exc:
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "LOOP_FAILED", None, 0,
            meta={
                "stage": stage,
                "iterations": exc.iterations,
                "exit_reason": exc.exit_reason,
                "issue_codes": [issue.code for issue in exc.issues[:10]],
            },
        )
        raise StageError(
            stage,
            [issue.message for issue in exc.issues]
            + [f"Agent Loop 退出：{exc.exit_reason}（{exc.iterations} 轮）"],
        ) from exc
    if result.status == "warning":
        object.__setattr__(result.value, "residual_errors", [issue.message for issue in result.issues])
        object.__setattr__(
            result.value, "residual_issues",
            [issue.model_dump(mode="json") for issue in result.issues],
        )
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "WARNING_CANDIDATE", None, 0,
            meta={
                "stage": stage,
                "iterations": result.iterations,
                "exit_reason": result.exit_reason,
                "artifact_id": result.artifact_id,
                "issue_codes": [issue.code for issue in result.issues[:10]],
            },
        )
    object.__setattr__(result.value, "loop_exit_reason", result.exit_reason)
    object.__setattr__(result.value, "evidence_artifact_id", result.artifact_id)
    return result.value


# ---------- A. 角色圣经 ----------

BIBLE_SOURCE_BUDGET_CHARS = 60000


_BIBLE_TAIL_SAMPLE_MAX = 12      # 后段最多抽样多少章（取其开头，角色多在章首登场）
_BIBLE_TAIL_SLICE_CHARS = 1500   # 每个抽样章节注入的开头字数


def _render_bible_source(chapters: list[dict], budget: int = BIBLE_SOURCE_BUDGET_CHARS) -> str:
    """为角色圣经渲染源文本：先顺序铺头部（主角通常在前期出场），再在剩余预算里
    跨越全书【抽样后段章节的开头】，让后期才登场的重要角色（如中后段反派）也能进圣经——
    否则分镜阶段引用这些角色会因"不在圣经"而反复返工或被迫漏掉。
    """
    valid = [ch for ch in chapters if (ch.get("content") or "").strip()]
    if not valid:
        return ""

    def _title(ch: dict) -> str:
        return ch.get("title") or f"第{ch.get('idx', '?')}章"

    # 头部顺序铺设：用至多 70% 预算（其余留给后段抽样）。
    head_budget = int(budget * 0.7)
    blocks: list[str] = []
    used = 0
    head_count = 0
    for ch in valid:
        remain = head_budget - used
        if remain <= 200:
            break
        content = ch["content"].strip()
        clipped = content[:remain]
        suffix = "……（原文过长已截断）" if len(content) > remain else ""
        blocks.append(f"【{_title(ch)}】\n{clipped}{suffix}")
        used += len(clipped)
        head_count += 1

    # 后段抽样：在头部未覆盖的章节里均匀取样，注入每章开头若干字，覆盖后期登场人物。
    later = valid[head_count:]
    remain_budget = budget - used
    if later and remain_budget > 200:
        sample_n = min(len(later), _BIBLE_TAIL_SAMPLE_MAX, max(1, remain_budget // _BIBLE_TAIL_SLICE_CHARS))
        if sample_n > 0:
            step = len(later) / sample_n
            picked_idx = sorted({min(len(later) - 1, int(i * step)) for i in range(sample_n)})
            for li in picked_idx:
                if remain_budget <= 200:
                    break
                ch = later[li]
                slice_chars = min(_BIBLE_TAIL_SLICE_CHARS, remain_budget)
                content = ch["content"].strip()
                clipped = content[:slice_chars]
                suffix = "……（节选开头，仅供识别后期登场角色）" if len(content) > slice_chars else ""
                blocks.append(f"【{_title(ch)}·节选】\n{clipped}{suffix}")
                remain_budget -= len(clipped)

    return "\n\n".join(blocks)


async def generate_bible(chapters: list[dict], feedback: str = "", previous_bible: dict | None = None,
                         project_id: str | None = None) -> Bible:
    chapters_text = _render_bible_source(chapters)
    previous_part = ""
    if previous_bible:
        names = "、".join(
            c.get("name", "") for c in previous_bible.get("characters", []) if c.get("name")
        )
        style = (previous_bible.get("world") or {}).get("visual_style_canonical", "")
        previous_part = f"\n当前人物谱摘要（用于对照返工，不可直接照抄错误）：\n已收录角色：{names or '无'}\n当前画风：{style or '无'}\n"
    feedback_part = ""
    if feedback.strip():
        feedback_part = f"""
人工打回重生要求（最高优先级）：
{feedback.strip()}

执行方式：
- 如果用户点名遗漏人物，必须回到原文中查找并收录；受 8 人上限影响时，删除更边缘的角色也要保留用户点名人物。
- 如果用户指出身份、关系、外观或称谓错误，必须按要求修正，并保持后续 relationships 一致。
- 不要把同一人物的外号、尊称、简称拆成多个角色；统一为原文最稳定的正式姓名。
"""
    prompt = f"""任务：从小说文本中提取角色圣经与世界观，用于后续 AI 视频生成的一致性控制。

要求：
1. 只收录出场 2 次以上或明显重要的角色，最多 8 个。
2. appearance_canonical 是该角色的"固定外观锚点串"：40~60 字，必须包含 性别年龄感/发型发色/服装款式与颜色/1 个标志性特征。只写视觉可见信息，不写性格。原著未描写的部分，按题材合理补全并保持内部一致。
3. visual_style_canonical：25~40 字的全局画风串，包含 美术风格/光线/色调，适配竖屏漫剧，必须依据本书题材定制。【硬性约束】必须是 CG/动画/漫画/插画类的非真人风格（如 3D 渲染、3D 写实 CG、2D 动画、动态漫画、厚涂插画、国漫风等，写实质感/照片级/胶片颗粒等氛围词可以保留），但严禁"真人实拍/真人出镜/实拍摄影"这类真人风格描述（否则后续 Seedance 视频接口会因疑似真人而报错 InputImageSensitiveContentDetected）。核心是画面为 CG/动画渲染而非真人拍摄。
4. speech_style 用于后续台词写作：句长习惯/口头禅/敬语习惯等，15~30 字。
5. name 必须互不重复：同一人物的别名/外号/尊称/简称统一成原文最稳定的正式姓名，不要拆成多个角色。
6. relationships 只描述【已收录角色之间】的关系：relationships.to 必须逐字等于本次 characters 里某个角色的 name，不要指向未收录的人物（否则代码校验会因「关系指向未知角色」退回重写）。与圈外人物的关系请省略，或并入 personality 文字描述。

小说文本：
{chapters_text}{previous_part}{feedback_part}

输出 JSON Schema：
{{"characters": [{{"name": str, "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}]}}], "world": {{"era": str, "genre": str, "visual_style_canonical": str}}}}"""
    loop = AgentLoop(
        stage_key="character_bible",
        contract_key="character_bible",
        goal="从原文章节生成来源可追溯、视觉锚点完整的人物圣经",
        scope_type="project",
        scope_id=project_id or hashlib.sha256(chapters_text.encode("utf-8")).hexdigest()[:16],
        artifact_type="character_bible",
        policy=AgentLoopPolicy(
            max_iterations=min(max(int(get_setting("max_repair_attempts") or 4), 1), 4),
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=True,
        ),
    )
    return await _run_with_agent_loop(
        "角色圣经", "character_bible", prompt, Bible, validate_bible,
        loop=loop, temperature=0.5, max_tokens=16384,
    )


# ---------- A2. 场景圣经（场景图素材库的规范场景，跨集场景一致性核心） ----------

class _SceneBibleDraft(BaseModel):
    """场景圣经输出合同（仅生成期使用）：一组规范场景。"""

    scenes: list[Scene]


async def generate_scene_bible(chapters: list[dict], bible: Bible,
                               feedback: str = "", project_id: str | None = None) -> list[Scene]:
    """从原文提取「规范场景」清单，作为场景图素材库的底稿（与 generate_bible 同构）。
    每个场景给 name（稳定短标签）+ scene_canonical（固定场景锚点串，画风约束与人物锚点一致：
    必须 CG/动画/漫画类非真人风格，否则后续 Seedance/Seedream 易因疑似真人报错）。"""
    chapters_text = _render_bible_source(chapters)
    style = bible.world.visual_style_canonical
    genre = bible.world.genre or ""
    feedback_part = ""
    if feedback.strip():
        feedback_part = f"\n人工打回重生要求（最高优先级）：\n{feedback.strip()}\n"
    prompt = f"""任务：从小说文本中提取【规范场景清单】，用于后续 AI 视频生成的场景一致性控制（场景图素材库）。

全片画风（场景锚点必须与之一致）：{style}
题材：{genre or '（未标注）'}

要求：
1. 只收录【反复出现 / 有戏份 / 画面感强】的关键场景（如主角居所、宗门广场、夜晚密林、朝堂等），最多 12 个；一次性出现的过场地点不要收录。
2. name：稳定的场景短标签（4~10 字，如"宗门广场""破败客栈内"），后续所有分镜的场景都收敛到这些名字，便于跨集复用同一张场景图。name 之间不要语义重复。
3. scene_canonical 是该场景的"固定场景锚点串"：30~60 字，必须包含 地点/室内外/典型光线时段/标志性陈设或建筑/整体氛围色调。只写视觉可见的环境信息，不写人物、不写剧情动作。原著未描写处按题材与画风合理补全并保持内部一致。
4. 【硬性约束】scene_canonical 必须贴合全片画风「{style}」，是 CG/动画/漫画/插画类的非真人渲染场景（写实质感氛围词可保留），严禁"真人实拍/实景照片/摄影棚实拍"这类描述（否则后续图像/视频接口会因疑似真人实景报错）。
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
    )
    return list(draft.scenes)


# 模型以为看到了全部，把后半章静默丢掉。改为命名常量 + 截断标记，让模型知道"后文还有，按依据补全"。
SCREENPLAY_SOURCE_BUDGET_CHARS = 24000


def _render_screenplay_source(source_text: str, budget: int = SCREENPLAY_SOURCE_BUDGET_CHARS) -> str:
    text = source_text or ""
    if len(text) <= budget:
        return text
    return (text[:budget]
            + f"\n\n……（本集源文还有约 {len(text) - budget} 字未展示；改编时请依据上方"
              "原文真实情节推进，不要遗漏后半段的关键事件与台词）")


async def generate_screenplay(episode: dict, source_text: str, bible: Bible,
                              prev_ending: str = "") -> EpisodeScreenplay:
    """小说 -> 完整剧本。

    新格式不在剧本台阶段强制拆成拍卡，而是先生成一份可读、可审、可拆镜的生产级剧本稿；
    拆镜与执行字段延后到分镜阶段。先显式锁定"本集必保留关键台词/关键剧情点"，
    再写正文，从机制上阻止重要台词与剧情在压缩中被丢弃。
    """
    speech_styles = "；".join(f"{c.name}：{c.speech_style}" for c in bible.characters if c.speech_style)
    bible_names_inline = "、".join(c.name for c in bible.characters) or "（角色圣经为空）"
    episode_hook = (episode.get("hook") or "").strip()
    episode_cliffhanger = (episode.get("cliffhanger") or "").strip()
    no_episode_hook = not episode_hook and not episode_cliffhanger
    screenplay_hook_rule = (
        "剧本开头按原文真实开场推进；本集 episode hook 为空，禁止为了格式发明额外开场钩子。"
        if not episode_hook
        else f"剧本开头必须尽快进入本集 hook：{episode_hook}"
    )
    screenplay_ending_rule = (
        "本集 episode hook 与 cliffhanger 均为空/空白：ending_hook 必须写成「无集级钩子」；"
        "禁止发明戒指发光、药老暗示、神秘人出现等原文没有的下一集钩子。"
        if no_episode_hook
        else (
            "本集 cliffhanger 为空：剧本结尾只收束到原文真实状态，ending_hook 可写「无集级钩子」，"
            "禁止为了尾钩发明原文没有的下一集事件。"
            if not episode_cliffhanger
            else f"剧本结尾必须落到本集尾钩：{episode_cliffhanger}"
        )
    )
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》把小说改写成【完整剧本】。

你现在处于“剧本台”阶段，不是分镜阶段。你的职责是先写出一整集完整、连续、可阅读、可拆镜的【生产级剧本稿】。

剧本层职责：
1. 生成一整集完整故事，而不是拍卡列表或摘要提纲。
2. 保证剧情连贯、人物情绪连贯、因果关系连贯。
3. 输出能直接进入导演/分镜阶段的剧本稿，不要只写成长梗概。
4. 保留原文依据，并明确改编方向。
5. 输出适合后续拆成约 {SHOT_SOFT_MIN}~{SHOT_SOFT_MAX} 个 5~10 秒视频分镜的连续剧本（硬上限 {SHOT_HARD_MAX} 镜）；
   只演主线骨架，禁止为细节无限拆镜。
6. 不在正文里输出“拍01/拍02/拍03”，不写景别、运镜、首尾帧、参考图、提示词。

{renderability_prompt_block()}

【导演级连续性要求】只写大形体可读调度，不要微表情/手指/衣褶：
- 每场开头交代本场已在场人物与大致位置；人物不能突然出现。
- 入场/离场/走近/退后用走、停、转身、伸手等大动作交代。
- 每场结尾留下「人物位置 + 情绪大方向 + 关键道具状态」供下一场承接。

【最重要·Renderability First·主线压缩】目标时长 {episode['target_duration_s']} 秒只作节奏参考。
你必须【先】输出 `plot_spine`，【再】写正文；正文只演骨架，绝对不要抠细节：
- `plot_spine.episode_premise`：一句话本集主角要什么、碰到什么阻力。
- `plot_spine.spine_beats`：5~12 条；每条含 beat_id/who/does/turn/must_keep；只写改变局势的事件。
- `plot_spine.must_keep_ending`：本章收束（与原文本章结局同向；禁止发明下一章钩子）。
- `plot_spine.drop_list`：至少 2 条「本章有但不拍」的支线/气氛戏/装饰对白。
- `key_lines`：只保留推动 spine 的主线台词，**最多 {KEY_LINES_MAX} 条**；禁止把人物谱原文台词全量入库。功能性路人台词可写进正文但不得进 key_lines。
- `key_plot_points`：{KEY_PLOT_POINTS_MIN}~{KEY_PLOT_POINTS_MAX} 条，与 spine 局势变化对齐，不是动作微描写。

【单集戏剧契约】（先想清楚再落笔，避免压缩后只剩事件、没有方向）：
- `dramatic_question`：用一句话写出本集观众心里追问的那个问题（例：他能否在不暴露底牌的情况下赢得资格？）。
- `protagonist_goal`：主角本集看得见、可完成的外在目标。
- `obstacle`：阻力 = 外部对手/规则 + 内部恐惧/执念。
- `stakes`：失败代价——输了会失去什么关系、尊严、目标或机会。

你必须同时输出两层内容：
A. `scene_outline`：场次级结构表，**3~5 场**，只覆盖 spine。
B. `full_script_text`：真正的剧本正文；只写大形体动作与主线对白，禁止舞台指示级细节。

`full_script_text` 必须采用以下剧本写法：
1. 使用场次标题，例如：`【场1】夜 / 旧仓库内`
2. 每场先写动作与场面调度，再写人物对白；动作段和对白段要分行。调度只用大形体动作。
3. 对白用“角色名：台词”格式；必要时可写“角色名（情绪/状态）：台词”。
4. 只写戏剧动作、人物反应、对白、必要旁白；不要写镜头语言。
5. 每场都要有明确戏剧任务：进入、升级、冲突、转折、收束中的至少一种。
6. 每场结尾都要把一个新的动作状态、情绪状态、人物位置或信息状态交给下一场。
7. 正文必须像真正台本，不得写成“本场讲了什么”的总结句堆叠。

硬性规则（代码校验，违反会被退回）：
1. episode_no 必须作为顶层字段出现且等于 {episode['episode_no']}（不可省略，也不可写进任何嵌套对象里）。
2. 必须先有合法 `plot_spine`；title / logline / scene_outline / full_script_text / emotional_curve / ending_hook / source_basis 必填；
   dramatic_question / protagonist_goal / obstacle / stakes 必填；
   key_lines {3}~{KEY_LINES_MAX} 条且须写进正文；key_plot_points {KEY_PLOT_POINTS_MIN}~{KEY_PLOT_POINTS_MAX} 条。
3. `scene_outline` 必须是 3~5 场的连续场次结构，scene_no 从 1 连续递增。
   【硬性·角色圣经】scene_outline[*].characters 中的具名角色只能填角色圣经准确姓名（{bible_names_inline}）；无需跨集定妆的临时人物允许使用测验员、守卫、围观者、路人甲等通用功能性身份标签。
4. full_script_text 必须是一篇连续故事正文，且必须带场次标题、动作段、对白段；「【场N】」数量必须与 scene_outline 一致。
5. full_script_text 不能是一大段梗概；必须像台本分行书写。
6. full_script_text 中禁止出现：拍01、拍1、拍 01、镜头、景别、运镜、首帧、尾帧、参考图、提示词、prompt；并禁止超纲细节词（微微/眼泪/指节/衣角/发丝/瞳孔/嘴角/分屏/闪回等）。
7. {screenplay_hook_rule}
8. {screenplay_ending_rule}
9. 人物姓名、关系、说话风格必须遵守角色圣经；台词自然口语化，服务主线冲突即可。
10. 信息密度服从主线：正文不能过度注水，必须讲清因果链与关键转折；drop_list 内容不得写回正文。
11. source_basis 必须概括本集改编依据的原文主线信息。

【连续性台账·也必须输出】主线权威是 plot_spine；events / information_ledger 只做下游拆镜索引，宁少勿滥：
- episode_premise：一句话本集目标（可与 plot_spine.episode_premise 一致）。
- events[]：建议与 must_keep spine 一一对应（约 5~12 条）。每条必须有非空 event_id（如 E1）、visible_change、state_out；禁止输出空壳事件。
- information_ledger[]：条数 ≤ spine_beats×2；每条必须有非空中文 content、合法 info_id（I1/I2）、以及对应 events[].event_id。
  【硬性】禁止输出 content 为空或 event_id 为空的台账项；写不出完整台账时，按每条 must_keep spine 只产出一条即可。
- voice_bible[]：每个元素包含 speaker_id、voice_canonical、language、role_type。
- approved_adaptations[] / forbidden_additions[]。
规则：原文没有的事件默认不得创建；若确需为连贯性补小动作，events[].adaptation_addition 必须为 true，必须写 adaptation_reason，且 approved 必须为 false。
空钩子规则：本集 hook=「{episode_hook or '（空）'}」、cliffhanger=「{episode_cliffhanger or '（空）'}」；若二者均为空/空白，ending_hook 只能写「无集级钩子」，不得发明原文没有的下一集钩子。

本集规划信息：
- 概要（只用于理解，不可替代原文）：{episode.get('synopsis') or ''}
- 本集 hook：{episode_hook or '（空）'}
- 本集 cliffhanger：{episode_cliffhanger or '（空）'}
- 上一集结尾：{prev_ending or '（本集为第一集）'}
- 本集目标时长：{episode['target_duration_s']} 秒

角色圣经（姓名、关系、说话风格必须遵守）：
{bible.model_dump_json()}

角色说话风格：
{speech_styles or '（无额外说话风格）'}

本集改编源文本：
{_render_screenplay_source(source_text)}

输出 JSON Schema：
{{"episode_no": {episode['episode_no']}, "mode": "full_script", "title": str, "logline": str, "script_format_note": "一句话说明正文采用的台本格式", "plot_spine": {{"episode_premise": "一句话本集目标", "spine_beats": [{{"beat_id": "S01", "who": str, "does": str, "turn": str, "must_keep": true}}], "must_keep_ending": str, "drop_list": [str, str]}}, "dramatic_question": "本集戏剧问题（一句话）", "protagonist_goal": "主角外在目标", "obstacle": "外部+内部阻力", "stakes": "失败代价", "key_lines": ["推动主线的台词，最多{KEY_LINES_MAX}条"], "key_plot_points": ["与spine对齐的局势变化，{KEY_PLOT_POINTS_MIN}~{KEY_PLOT_POINTS_MAX}条"], "scene_outline": [{{"scene_no": int, "scene_heading": "场次标题", "story_function": "本场戏剧功能", "characters": [str], "summary": "本场戏剧内容概括", "conflict": str, "turn": "交给下一场的状态变化", "source_basis": "原文依据"}}], "full_script_text": str, "character_state_changes": [str], "emotional_curve": str, "ending_hook": "若 hook/cliffhanger 均为空则固定为「无集级钩子」", "source_basis": str, "adaptation_direction": str, "opening": str, "development": str, "conflict": str, "climax": str, "episode_premise": "一句话本集目标", "events": [{{"event_id": "E1", "source_span": "原文位置或摘句", "source_fact": "原文事实", "state_in": "事件前状态", "trigger": "触发因素", "visible_change": "可见/可听变化", "state_out": "事件后状态", "must_keep": true, "adaptation_addition": false, "adaptation_reason": "", "approved": false}}], "information_ledger": [{{"info_id": "I1", "event_id": "E1", "content": "观众必须获得的信息", "delivery_owner": "visual_action|spoken_dialogue|offscreen_voice|narration|on_screen_text|ambient_sound", "speaker_id": "角色名/旁白/功能性身份或null", "exact_text": "需逐字交付时填写，否则null", "reinforcement_allowed": false, "status": "unassigned"}}], "voice_bible": [{{"speaker_id": "角色名/旁白/功能性身份", "voice_canonical": "声音与语气规范", "language": "普通话", "role_type": "named_character|functional_character|narrator"}}], "approved_adaptations": [str], "forbidden_additions": [str]}}"""
    configured_iterations = max(int(get_setting("max_repair_attempts") or 4), 1)
    loop = AgentLoop(
        stage_key="screenplay",
        contract_key="screenplay",
        goal=f"生成第 {episode['episode_no']} 集可拍剧本并通过全部确定性门禁",
        scope_type="episode",
        scope_id=str(episode.get("id") or f"episode-{episode['episode_no']}"),
        artifact_type="episode_screenplay",
        policy=AgentLoopPolicy(
            max_iterations=min(configured_iterations, 4),
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=True,
        ),
    )
    def _check_screenplay(s: EpisodeScreenplay) -> list[str]:
        errors = validate_screenplay(
            s, bible, max(1, episode["target_duration_s"] // config.VIDEO_DURATION_MIN_S),
            episode_no=episode["episode_no"], source_text=source_text)
        errors.extend(adaptation_hook_errors(s, episode))
        if no_episode_hook:
            ending = (s.ending_hook or "").strip()
            explicit_no_hook = ending in {"无", "无钩子", "无集级钩子", "（无）"} or ending.startswith("无集级")
            if not explicit_no_hook:
                errors.append(
                    "ending_hook 必须为「无集级钩子」：本集 hook/cliffhanger 均为空，禁止发明下一集钩子")
        return list(dict.fromkeys(errors))

    script = await _run_with_agent_loop(
        "可拍剧本", "screenplay", prompt, EpisodeScreenplay,
        _check_screenplay,
        loop=loop, temperature=0.7, max_tokens=65535,
        # episode_no/mode 是后端权威值（validator 也要求 episode_no 必须等于本集号），模型给的值不可信，
        # 直接确定性回填，避免再为这两个已知字段空转修复轮。
        prefill={"episode_no": episode["episode_no"], "mode": "full_script"})
    if no_episode_hook:
        if not (script.ending_hook or "").strip() or len((script.ending_hook or "").strip()) < 4:
            script.ending_hook = "无集级钩子"
    return script


def _storyboard_key_content_block(screenplay: EpisodeScreenplay) -> str:
    """把剧本台主线合同渲染成分镜 prompt 区块（spine + key_lines/points + drop_list）。"""
    key_lines = [ln.strip() for ln in (screenplay.key_lines or []) if ln and ln.strip()]
    key_points = [pt.strip() for pt in (screenplay.key_plot_points or []) if pt and pt.strip()]
    contract = [
        f"- 本集戏剧问题：{screenplay.dramatic_question}" if screenplay.dramatic_question else "",
        f"- 主角目标：{screenplay.protagonist_goal}" if screenplay.protagonist_goal else "",
        f"- 阻力：{screenplay.obstacle}" if screenplay.obstacle else "",
        f"- 失败代价：{screenplay.stakes}" if screenplay.stakes else "",
    ]
    contract_text = "\n".join(c for c in contract if c)
    lines_text = "\n".join(f"- {ln}" for ln in key_lines) or "（剧本未单列，请从完整剧本文本中提取主线对白）"
    points_text = "\n".join(f"- {pt}" for pt in key_points) or "（剧本未单列，请从完整剧本文本中提取主线剧情）"
    blocks: list[str] = []
    spine = screenplay.plot_spine
    if spine:
        beats = spine.spine_beats or []
        beat_lines = "\n".join(
            f"- {b.beat_id}｜{b.who}｜{b.does}→{b.turn}"
            + ("" if b.must_keep else "（可删过渡）")
            for b in beats
        ) or "（无）"
        drops = "\n".join(f"- {d}" for d in (spine.drop_list or []) if d) or "（无）"
        blocks.extend([
            "【主线骨架 plot_spine】（必须覆盖 must_keep 节拍；drop_list 禁止拍摄）：",
            f"- premise：{spine.episode_premise or screenplay.episode_premise or '（无）'}",
            f"- must_keep_ending：{spine.must_keep_ending or '（无）'}",
            "spine_beats：",
            beat_lines,
            "drop_list（禁止拍）：",
            drops,
            "",
        ])
    if contract_text:
        blocks.extend(["【单集戏剧契约】（指导取舍：服务它们的内容优先保留）：", contract_text, ""])
    blocks.extend([
        "【本集主线台词】（每条必须写进某镜的 dialogues，代码逐条校验）：",
        lines_text,
        "",
        "【本集主线剧情点】（每条必须在某镜的 action_desc 或声轨中体现，代码逐条校验）：",
        points_text,
    ])
    return "\n".join(blocks) + "\n"


def _scene_library_block(bible: Bible) -> str:
    """渲染「可用场景图素材库」清单注入分镜 prompt：要求 scene_setting 收敛到库内规范场景名，
    保证后续每个场景能复用同一张场景库图（跨镜/跨集一致）。库为空时返回空串（不约束）。"""
    scenes = getattr(bible, "scenes", None) or []
    if not scenes:
        return ""
    rows = "\n".join(
        f"- {sc.name}：{sc.scene_canonical}" for sc in scenes if getattr(sc, "name", ""))
    names = "、".join(sc.name for sc in scenes if getattr(sc, "name", ""))
    return (
        "【可用场景图素材库】（本片所有镜头的场景必须从下列规范场景中选用，scene_setting 的地点部分必须"
        "收敛到其中一个场景名，以便同场景跨镜/跨集复用同一张场景图、保持场景一致）：\n"
        f"{rows}\n"
        f"硬性要求：每个镜头的 scene_setting 写成「时间，{{库内场景名}}」，地点必须是上列之一（{names}）；"
        "确有剧情需要的新地点时，沿用语义最接近的库内场景名，不要自创库外场景。\n"
    )


def _render_completed_shots_context(shots: list[Shot]) -> str:
    if not shots:
        return "（尚无已通过镜头，本次是第 1 镜）"
    rows: list[dict] = []
    for index, shot in enumerate(shots):
        # 已完成镜头只提供“承接/防重复”的状态摘要，避免把完整动作历史再次喂给模型重演。
        state_out = (
            (getattr(shot, "observed_state_out", "") or "").strip()
            or (getattr(shot, "state_out", "") or "").strip()
            or (shot.last_frame_desc or "").strip()
        )
        narration = (shot.narration or "").strip()
        dialogue_text = "｜".join(d.line for d in shot.dialogues if (d.line or "").strip())
        rows.append({
            "shot_no": shot.shot_no,
            "duration_s": shot.duration_s,
            "scene_setting": shot.scene_setting,
            "characters_visible": shot.characters_visible or shot.characters,
            "continuity_mode": shot.continuity_mode,
            "承接状态": state_out[:160],
            "delivered_info_ids": list(shot.new_information_ids or []),
            "soundtrack_brief": (narration + ("｜" if narration and dialogue_text else "") + dialogue_text)[:120],
            "transition": shot.transition,
        })
    return json.dumps(rows, ensure_ascii=False, indent=2)


def _relevant_text_windows(text: str, hints: list[str], *, max_chars: int) -> str:
    """从长文中抽取当前镜相关窗口，用于逐镜生成/修复。

    完整原文已在剧本和分镜大纲阶段消化；逐镜阶段只需要能逐字摘录当前任务的
    局部上下文。命中多处时去重后合并，未命中时保留首尾，避免因节选失败丢掉尾钩。
    """
    source = (text or "").strip()
    if len(source) <= max_chars:
        return source
    keywords: list[str] = []
    for hint in hints:
        for atom in re.split(r"[\s，。！？；：、|｜/]+", hint or ""):
            atom = atom.strip()
            if not (2 <= len(atom) <= 24):
                continue
            candidates = [atom]
            # 大纲常是原文改写（如“谷言拿起钥匙” vs “谷言终于拿起钥匙”）；
            # 长句精确命中失败时，用较长连续子串定位，不做昂贵的语义检索。
            if len(atom) >= 6:
                for width in range(min(8, len(atom) - 1), 3, -1):
                    candidates.extend(atom[i:i + width] for i in range(0, len(atom) - width + 1))
            for candidate in candidates:
                if candidate not in keywords:
                    keywords.append(candidate)
    half = 700
    spans: list[tuple[int, int]] = []
    for keyword in keywords[:16]:
        start_at = 0
        while len(spans) < 6:
            pos = source.find(keyword, start_at)
            if pos < 0:
                break
            spans.append((max(0, pos - half), min(len(source), pos + len(keyword) + half)))
            start_at = pos + len(keyword)
        if len(spans) >= 6:
            break
    if not spans:
        side = max_chars // 2
        return source[:side] + "\n……（中间无关段落已省略）……\n" + source[-side:]
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 120:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    chunks: list[str] = []
    used = 0
    for start, end in merged:
        chunk = source[start:end]
        remain = max_chars - used
        if remain <= 0:
            break
        chunks.append(chunk[:remain])
        used += min(len(chunk), remain)
    return "\n……（无关段落已省略）……\n".join(chunks)


def _storyboard_progress_block(completed_shots: list[Shot]) -> str:
    used = sum(shot.duration_s for shot in completed_shots)
    return (
        f"\n【分镜进度】已通过 {len(completed_shots)} 镜、累计 {used}s；整集软预算约 "
        f"{SHOT_SOFT_MIN}~{SHOT_SOFT_MAX} 镜。\n"
        f"- 本镜 duration_s **默认 {PREFERRED_SHOT_DURATION_S}s**；仅当口播或连续动作确实放不下才取 "
        f"{PREFERRED_SHOT_DURATION_S + 1}~{config.VIDEO_DURATION_MAX_S}s，且须接受后续 AI 时长审核。\n"
        "- 继续按剧本主线推进，覆盖 must_keep spine 与主线台词后即可设置 is_final=true。\n"
    )


def _filter_partial_storyboard_errors(errors: list[str], *, current_index: int) -> list[str]:
    """逐镜头 QA 只拦当前镜头与前后承接问题；整集数量/全量声轨/关键内容在最后统一兜底。"""
    prefixes = (
        "镜头数 ",
        "总时长 ",
        "分镜声轨过少",
        "分镜对白不足",
        "完整剧本含 ",
        "分镜丢失了剧本标记的 ",
    )
    filtered: list[str] = []
    for error in errors:
        if error.startswith(prefixes):
            continue
        shot_refs = [int(m.group(1)) for m in re.finditer(r"shots\[(\d+)\]", error)]
        # 当前镜修复不了已落库镜头的字段（典型：上一镜 last_frame_desc 没写出换场视觉），
        # 不把这类错误喂回当前镜，避免模型原地修 8 次。
        if shot_refs and max(shot_refs) < current_index:
            continue
        filtered.append(error)
    return filtered


def _normalized_candidate_board(episode_no: int, completed_shots: list[Shot], shot: Shot,
                                bible: Bible | None = None,
                                target_duration_s: int | None = None) -> Storyboard:
    board = Storyboard(episode_no=episode_no, shots=[*completed_shots, shot])
    normalize_continuity(board)
    # ① 确定性角色分类：功能性路人保留并补入 characters；具体姓名服从角色圣经；其它未知角色剥离。
    # 须在清空旁白之前——未知角色剥离可能把宣告并入 action_desc。
    normalize_offbible_characters(board, bible)
    # ② 产品禁止旁白/内心OS：确定性清空 narration 与 timeline narration 轨。
    relieve_spoken_overflow(board)
    prefer_default_shot_durations(board)
    normalize_transition_visuals(board)
    for s in board.shots:
        s.action_desc = normalize_action_desc(s.action_desc)
    return board


def _validate_storyboard_shot_draft(draft: StoryboardShotDraft, *, episode: dict, bible: Bible,
                                    screenplay: EpisodeScreenplay, completed_shots: list[Shot],
                                    shot_no: int, allow_finish: bool, must_finish: bool,
                                    outline_covers: str = "", later_planned_covers: str = "") -> list[str]:
    errors: list[str] = []
    if draft.episode_no != episode["episode_no"]:
        errors.append(f"episode_no={draft.episode_no}，必须等于 {episode['episode_no']}")
    if draft.shot.shot_no != shot_no:
        errors.append(f"shot.shot_no={draft.shot.shot_no}，当前只允许输出第 {shot_no} 镜")
    if draft.is_final and not allow_finish:
        errors.append(f"当前第 {shot_no} 镜还不能作为最后一镜；本集至少需要更多镜头承接完整剧情")
    if must_finish and not draft.is_final:
        errors.append(
            f"当前已到本集收束位（大纲末镜/软预算/硬上限），第 {shot_no} 镜必须收束到尾钩并设置 is_final=true"
        )

    # 相邻镜允许共享同一主线段落的 source_excerpt（Renderability：不再用「必须推进原文」逼碎镜）。

    target = episode["target_duration_s"]
    board = _normalized_candidate_board(episode["episode_no"], completed_shots, draft.shot, bible, target)
    current = board.shots[-1]
    partial_errors = validate_storyboard(board, bible, target)
    errors.extend(_filter_partial_storyboard_errors(partial_errors, current_index=len(completed_shots)))
    errors.extend(information_ledger_errors(board, screenplay))
    # 向前承接：复合 covers 里已在前序镜头落实的事实不再算本镜漏戏（呼应大纲"可拆到相邻多镜"）。
    prior_text = "".join(
        (s.action_desc or "") + (s.narration or "") + "".join(d.line for d in s.dialogues)
        for s in board.shots[:-1]
    )
    errors.extend(validate_storyboard_shot_covers_outline(
        current, outline_covers, shot_no,
        prior_text=prior_text, later_planned_covers=later_planned_covers))

    if not (draft.is_final or must_finish):
        return errors

    # 收尾镜才跑整集兜底校验。必保留台词/剧情点、声轨这类"靠后续镜头分担"的缺口，
    # 在自愿收尾时不硬塞进单镜（那会让修复回路卡死），而是要求改判 is_final=false 继续补镜；
    # 撞到大纲末镜 / 软预算 / 硬上限（must_finish）时再无合法后续镜可分担——只硬拦主线缺口，
    # 禁止再用氛围声轨逼出计划外幻觉镜。
    episode_errors = (
        validate_storyboard_soundtrack(board, screenplay, target)
        + validate_storyboard_preserves_key_content(board, screenplay)
    )
    if episode_errors:
        hard = [
            e for e in episode_errors
            if ("must_keep" in e) or ("主线台词" in e) or ("主线剧情点" in e) or ("主线节拍" in e)
        ]
        if must_finish or shot_no >= SHOT_SOFT_MAX:
            errors.extend(hard)
        else:
            errors.append(
                f"本集整集必保留内容/声轨尚未达标，第 {shot_no} 镜暂不能收尾："
                "请将 is_final 设为 false 继续补镜，在后续镜头补齐——"
                + "；".join(episode_errors[:6]))
    return errors


async def generate_storyboard_outline(episode: dict, source_text: str, bible: Bible,
                                      prev_ending: str, screenplay: EpisodeScreenplay) -> StoryboardOutline:
    """先出整集分镜大纲（一次 LLM 调用）：把完整剧本铺成有序的 N 条镜头节拍，先定全局节奏。
    逐镜填充阶段据此让每镜知道"我该推进到剧情的哪个位置"，避免多镜停留同一情绪导致推进缓慢。"""
    if not (screenplay.full_script_text or "").strip():
        raise StageError("分镜大纲", ["请先生成完整剧本，再规划分镜大纲"])
    target = episode["target_duration_s"]
    min_shots, max_shots = storyboard_shot_count_range(target)
    key_content_block = _storyboard_key_content_block(screenplay)
    scene_library_block = _scene_library_block(bible)
    is_first = int(episode.get("episode_no") or 0) == 1
    episode_hook = (episode.get("hook") or "").strip()
    episode_cliffhanger = (episode.get("cliffhanger") or "").strip()
    first_rule = ("【本集是第一集】第 1 镜是全片开场建场镜：先交代世界观/主角处境/核心设定，再带出本集 hook。"
                  if is_first else (
                      f"第 1 镜要尽快进入本集 hook：{episode_hook}。"
                      if episode_hook else
                      "第 1 镜按剧本真实开场自然进入，不得因 episode hook 为空而发明额外钩子。"
                  ))
    ending_rule = (
        f"最后一镜落到本集尾钩：{episode_cliffhanger}。"
        if episode_cliffhanger else
        "本集 cliffhanger 为空：最后一镜只收束到剧本/原文已有的真实结束状态，不得发明下一集钩子。"
    )
    scene_block = (chr(10).join(
        f"场{sc.scene_no}｜{sc.scene_heading}｜功能：{sc.story_function}｜摘要：{sc.summary}｜"
        f"冲突：{sc.conflict or '（无）'}｜转折：{sc.turn or '（无）'}"
        for sc in screenplay.scene_outline) if screenplay.scene_outline else "（未提供场次结构）")
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》规划【分镜大纲】。

你现在做的是全局节奏规划：把下方【完整剧本 / plot_spine】铺成有序的 N 条主线镜头（1 条 must_keep spine ≈ 1~2 镜）。
目标约 {SHOT_SOFT_MIN}~{SHOT_SOFT_MAX} 镜（硬上限 {SHOT_HARD_MAX}）；超预算必须合并，禁止无限拆碎。
只覆盖 must_keep spine；drop_list 禁止安排。不写景别/运镜/首尾帧/台词原文。

{renderability_prompt_block()}

最重要的目标是节奏：后续会严格按这份大纲逐镜填充，所以——
- 每一条镜头都必须包含 state_in、primary_action、state_out，且 state_out 必须相对 state_in 发生可见/可听变化；禁止两条镜头停留在同一情绪、同一个动作或同一句信息上空耗时长。
- N 条镜头必须覆盖开端→发展→冲突→高潮→收束，篇幅按主线状态变化分配。
- {ending_rule}
- 禁止按文本长度机械拆分；超预算时合并。

【导演调度总则】只用大形体动作；单镜画面角色 ≤3，开口 ≤2。人物不能无动机地从无到有。

完整剧本：
标题：{screenplay.title}
一句话梗概：{screenplay.logline}
场次结构：
{scene_block}

完整剧本文本：
{screenplay.full_script_text}

情绪曲线：{screenplay.emotional_curve}
结尾钩子：{screenplay.ending_hook}

{key_content_block}
{scene_library_block}硬性约束：
1. 镜头数目标 {SHOT_SOFT_MIN}~{SHOT_SOFT_MAX}，硬上限 {SHOT_HARD_MAX}；shot_no 从 1 连续递增。大纲 duration_s **默认 5**；仅必要时取 6~10（>5 进 AI 审核）。同 spine 事件通常 1~2 镜；禁止为细节无限拆镜，超预算必须合并。
2. 每条保留 beat/covers 兼容旧流程，但重点必须填写 state_in、primary_action、state_out、continuity_mode、story_event_id、new_information_ids、duration_s、characters_visible、audio_cast。beat 只作为一句话摘要，不得替代状态字段。
3. 相邻两镜 state_out -> state_in 必须能承接，且 primary_action 必须不同、持续前进，严禁停留或复述同一节拍。
4. 上方主线台词/剧情点/spine 必须分配到 covers 或 new_information_ids；drop_list 禁止分配。new_information_ids 只能引用 screenplay.information_ledger 已有 info_id。同一拍内可合并相关事实，不要硬拆空耗时长。
5. covers 只写本镜必须拍出/说出的具体事实（可见动作、可听台词、可感知反应、可核对信息点）；禁止写「反差/对比/衬托/呼应/强调/暗示/氛围」等导演意图——意图写入 beat/primary_action/state_out，事实写成双方可见状态（如「薰儿测出七段、人群赞叹；萧炎低头不语」）。
6. {first_rule}
7. 每条 scene_setting 写时间+地点短标签；同一连续空间必须保持同一个标签，不要因为人物走到门口/桌边/人群前就改标签。
8. beat 必须写清人物调度：入画/出画用走、停、转身等大动作原因。
9. continuity_mode 必须从 action_continuation / same_scene_cut / reaction_cut / reverse_angle / insert_detail / scene_change 中选择；只有 action_continuation 表示承接上一镜同一动作尾状态，其他同场景切换不得冒充动作连续。

本集目标时长 {target}s。上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON（不要解释、不要 Markdown）：
{{"episode_no": {episode['episode_no']}, "shots": [{{"shot_no": int, "scene_setting": "时间+地点短标签", "beat": "兼容字段：本镜推进的剧情一句话", "covers": "本镜必须拍出/说出的具体事实（禁止反差/对比等导演抽象）", "state_in": "本镜开始时人物/道具/信息状态", "primary_action": "本镜唯一主动作/主交付", "state_out": "本镜结束时的新状态", "continuity_mode": "action_continuation|same_scene_cut|reaction_cut|reverse_angle|insert_detail|scene_change", "story_event_id": "对应 screenplay.events[].event_id 或空", "new_information_ids": ["本镜首次交付的信息ID，可空"], "duration_s": 5, "characters_visible": ["本镜画面可见角色"], "audio_cast": ["本镜发声角色/旁白/功能性声音，可空"]}}]}}"""
    log_provider_call(
        "storyboard_outline_prompt", config.MODEL_TEXT, "PROMPT_READY", None, 0,
        meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
              "target_duration_s": target, "shot_range": [min_shots, max_shots],
              "prompt_chars": len(prompt), "contract_version": "renderability_v1"})
    logged_downgrades: set[tuple[int, str]] = set()
    logged_abstract_rewrites: set[tuple[int, str]] = set()

    def _check(o: StoryboardOutline) -> list[str]:
        # 方案 A2：校验前先确定性降级——把 covers 里"被圣经外角色开口宣告"改写为旁白转述，
        # 从源头消灭"删角色↔保留角色"死循环，让模型不必反复 reroute（避免修复停滞）。再做大纲校验。
        for c in downgrade_outline_offbible_spoken(o, bible):
            key = (c["shot_no"], c["after"])
            if key in logged_downgrades:  # 跨修复轮去重，避免同一改写反复刷日志
                continue
            logged_downgrades.add(key)
            log_provider_call(
                "storyboard_outline_downgrade", config.MODEL_TEXT, "COVERS_DOWNGRADED", None, 0,
                meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                      "stage": "分镜大纲", **c})
        # P1：剥离 covers 导演抽象（反差/对比等），写入 beat 可拍改写指引，避免逐镜词匹配死循环。
        for c in rewrite_outline_abstract_covers(o):
            key = (c["shot_no"], c["after"])
            if key in logged_abstract_rewrites:
                continue
            logged_abstract_rewrites.add(key)
            log_provider_call(
                "storyboard_outline_abstract_covers", config.MODEL_TEXT, "COVERS_ABSTRACT_REWRITTEN", None, 0,
                meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                      "stage": "分镜大纲", **c})
        return validate_storyboard_outline(o, screenplay, target, bible=bible)

    outline_loop = AgentLoop(
        stage_key="storyboard_outline",
        contract_key="storyboard",
        goal=f"规划第 {episode['episode_no']} 集完整逐镜节奏与必保留内容分配",
        scope_type="episode",
        scope_id=str(episode.get("id") or f"episode-{episode['episode_no']}"),
        artifact_type="storyboard_outline",
        policy=AgentLoopPolicy(
            max_iterations=4, stall_rounds=2, min_quality_gain=0.03,
            no_gain_rounds=2, allow_warning_candidate=True,
        ),
    )
    outline = await _run_with_agent_loop(
        "分镜大纲", "storyboard_outline", prompt, StoryboardOutline, _check,
        loop=outline_loop, temperature=0.6, max_tokens=4096,
        repair_user_prompt_limit=None,
        prefill={"episode_no": episode["episode_no"]},
    )
    # 减重试 #2：第一集第 1 镜是强制建场镜，把派给它的判决/反转类 covers 顺延合并到第 2 镜，
    # 避免逐镜阶段"照建场写→漏 covers / 硬塞判决→引入圣经外角色"的连环重试。
    # 在校验通过后做确定性顺延，不扰动大纲修复回路。
    for c in defer_establishing_covers(outline, int(episode.get("episode_no") or 0)):
        log_provider_call(
            "storyboard_outline_defer_covers", config.MODEL_TEXT, "COVERS_DEFERRED", None, 0,
            meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
                  "stage": "分镜大纲", **c})
    return outline


def _render_storyboard_outline(
    outline: StoryboardOutline | None,
    current_shot_no: int,
    valid_info_ids: set[str] | None = None,
) -> str:
    """把整集大纲渲染进逐镜 prompt，并标出"本镜"在大纲里的位置，让模型按计划推进、不越位也不停留。"""
    if not outline or not outline.shots:
        return ""
    total = len(outline.shots)
    rows = []
    for s in outline.shots:
        scene = f"｜{s.scene_setting}" if (s.scene_setting or "").strip() else ""
        covers = f"｜落实：{s.covers}" if (s.covers or "").strip() else ""
        state = ""
        if (s.state_in or s.primary_action or s.state_out):
            state = f"｜状态：{s.state_in or '未填'} -> {s.primary_action or s.beat} -> {s.state_out or '未填'}"
        event = f"｜event:{s.story_event_id}" if (s.story_event_id or "").strip() else ""
        info_ids = [
            info_id for info_id in s.new_information_ids
            if valid_info_ids is None or info_id in valid_info_ids
        ]
        info = f"｜info:{','.join(info_ids)}" if info_ids else ""
        mark = "  ← 本镜" if s.shot_no == current_shot_no else ""
        rows.append(f"第{s.shot_no}/{total}镜{scene}：{s.beat}{state}{event}{info}{covers}{mark}")
    return "本集分镜大纲（全局节奏计划，按它推进；本镜只落实标注「← 本镜」的那一条）：\n" + "\n".join(rows)


def _outline_brief(outline: StoryboardOutline | None, shot_no: int):
    if outline and 1 <= shot_no <= len(outline.shots):
        return outline.shots[shot_no - 1]
    return None


def _split_text_by_content_budget(text: str, budget: int) -> list[str]:
    """按 ``_condense`` 的计数口径切文本，并保留原有字符顺序。"""
    if budget <= 0:
        raise ValueError("budget must be positive")
    chunks: list[str] = []
    buffer: list[str] = []
    content_chars = 0
    for char in text.strip():
        char_cost = len(_condense(char))
        if buffer and char_cost and content_chars + char_cost > budget:
            chunk = "".join(buffer).strip()
            if chunk:
                chunks.append(chunk)
            buffer = []
            content_chars = 0
        buffer.append(char)
        content_chars += char_cost
    chunk = "".join(buffer).strip()
    if chunk:
        chunks.append(chunk)
    return chunks


def _split_atoms_to_content_budget(atoms: list[str], budget: int) -> list[str]:
    """尽量沿句读贪心分组；单个长句仍超限时再确定性硬切。"""
    pieces = [
        piece
        for atom in atoms
        for piece in _split_text_by_content_budget(atom, budget)
    ]
    groups: list[list[str]] = []
    current: list[str] = []
    for piece in pieces:
        candidate = "；".join([*current, piece])
        if current and len(_condense(candidate)) > budget:
            groups.append(current)
            current = [piece]
        else:
            current.append(piece)
    if current:
        groups.append(current)

    # 贪心装箱可能留下极短尾段（真实镜12曾得到 26/4）。在不越预算且不改变
    # 原顺序的前提下，把边界处的完整语义原子后移，使相邻镜头更均衡、更可拍。
    changed = True
    while changed:
        changed = False
        for index in range(len(groups) - 1):
            left, right = groups[index], groups[index + 1]
            if len(left) <= 1:
                continue
            before = abs(
                len(_condense("；".join(left))) - len(_condense("；".join(right)))
            )
            shifted_left = left[:-1]
            shifted_right = [left[-1], *right]
            right_cost = len(_condense("；".join(shifted_right)))
            after = abs(
                len(_condense("；".join(shifted_left))) - right_cost
            )
            if right_cost <= budget and after < before:
                groups[index] = shifted_left
                groups[index + 1] = shifted_right
                changed = True
    return ["；".join(group) for group in groups]


def _maybe_split_outline_covers(outline: StoryboardOutline | None, shot_no: int,
                                bible: Bible, max_shots: int) -> bool:
    """只保留语义拆分；禁止按字符长度机械拆成“自动拆分自第X镜”碎片。"""
    if not outline or not outline.shots:
        return False
    if not (1 <= shot_no <= len(outline.shots)):
        return False
    if len(outline.shots) >= max_shots:
        return False
    current = outline.shots[shot_no - 1]
    covers = (current.covers or "").strip()
    if not covers:
        return False
    bible_names = {c.name for c in bible.characters}
    outside = _covers_outside_spoken(covers, bible_names)
    both_tracks = _covers_has_spoken(covers) and _covers_has_crowd(covers)
    over_budget = len(_condense(covers)) > config.MAX_SPOKEN_CHARS_PER_SHOT
    if not outside and not both_tracks:
        # 仅因文本/口播字符超预算时不再自动拆分；交给容量校验或人工调整。
        return False
    atoms = _atomize_claim(covers)
    if not atoms:
        return False
    if len(atoms) < 2:
        return False
    # 尽量让角色宣告落前半、人群反馈落后半；找不到则按中点拆。
    split_at = len(atoms) // 2
    for idx in range(1, len(atoms)):
        front = "".join(atoms[:idx])
        back = "".join(atoms[idx:])
        if _covers_has_spoken(front) and _covers_has_crowd(back):
            split_at = idx
            break
    chunks = ["；".join(atoms[:split_at]), "；".join(atoms[split_at:])]
    chunks = [chunk for chunk in chunks if chunk]
    if len(chunks) < 2:
        return False
    if len(outline.shots) + len(chunks) - 1 > max_shots:
        return False
    current.covers = chunks[0]
    for offset, chunk in enumerate(chunks[1:], start=1):
        outline.shots.insert(
            shot_no - 1 + offset,
            StoryboardOutlineShot(
                shot_no=shot_no + offset,
                scene_setting=current.scene_setting,
                beat=f"（语义拆分：第{shot_no}镜后续状态变化）{chunk}",
                covers=chunk,
            ),
        )
    for i, s in enumerate(outline.shots):
        s.shot_no = i + 1
    log_provider_call(
        "storyboard_outline_split", config.MODEL_TEXT, "COVERS_SPLIT", None, 0,
        meta={"shot_no": shot_no, "outside": outside, "both_tracks": both_tracks,
              "over_budget": over_budget, "chunks": [chunk[:80] for chunk in chunks],
              "new_total": len(outline.shots)})
    return True


async def generate_storyboard_next_shot(episode: dict, source_text: str, bible: Bible,
                                        prev_ending: str, screenplay: EpisodeScreenplay,
                                        completed_shots: list[Shot],
                                        final_feedback: list[str] | None = None,
                                        outline: StoryboardOutline | None = None) -> StoryboardShotDraft:
    """基于已通过镜头生成下一个镜头；业务校验通过才返回，调用方可立即落库给前端增量展示。"""
    if not (screenplay.full_script_text or "").strip():
        raise StageError("分镜脚本", ["旧版拍卡剧本已下线，请先重新生成完整剧本，再进入分镜台"])

    speech_styles = "；".join(f"{c.name}：{c.speech_style}" for c in bible.characters if c.speech_style)
    extra_policy = functional_extra_policy_text()
    durations = sorted(config.ALLOWED_DURATIONS)
    output_contract = _storyboard_output_contract(episode, bible, durations, speech_styles)
    preflight_contract = _storyboard_preflight_contract(episode)
    transition_options = "|".join(sorted(TRANSITIONS))
    key_content_block = _storyboard_key_content_block(screenplay)
    scene_library_block = _scene_library_block(bible)
    min_shots, max_shots = storyboard_shot_count_range(episode["target_duration_s"])
    shot_no = len(completed_shots) + 1
    # 方案 C：当前镜大纲 covers 若"不可单镜完成"（依赖圣经外角色开口 或 同时要求角色开口+人群声），
    # 在调用 LLM 前自动拆成足够多段并插入相邻节拍，让本镜只落实当前段，避免逐镜修复打转。
    # 拆分后 outline.shots 变长，下方 expected_total / allow_finish / must_finish 自动按新长度计算。
    _maybe_split_outline_covers(outline, shot_no, bible, max_shots)
    # 有大纲时由计划的镜头数决定收尾时机（执行完整份大纲，避免提前收尾把后段剧情挤掉）；
    # 无大纲时回退到基础镜头数下限。
    expected_total = len(outline.shots) if (outline and outline.shots) else min_shots
    allow_finish = shot_no >= max(min_shots if not (outline and outline.shots) else expected_total, 1)
    # P0：到达当前大纲末镜（或软预算上限）必须收束。禁止「计划已跑完仍 is_final=false /
    # 继续补镜」发明大纲外幻觉镜头（生产事故：12 镜通过后冒出无剧情的第 13 镜）。
    must_finish = bool(
        (outline and outline.shots and shot_no >= expected_total)
        or shot_no >= SHOT_SOFT_MAX
        or shot_no >= max_shots
    )
    episode_hook = (episode.get("hook") or "").strip()
    episode_cliffhanger = (episode.get("cliffhanger") or "").strip()
    final_shot_rule = (
        f"如果 is_final=true，本镜必须落到本集尾钩：{episode_cliffhanger}，并且整集必保留关键台词/剧情点都已经在已通过镜头或本镜中体现。"
        if episode_cliffhanger else
        "如果 is_final=true，本镜只收束到剧本/原文已有的真实结束状态；本集 cliffhanger 为空，禁止发明原文没有的下一集钩子。"
    )
    first_shot_entry_rule = (
        f"第 1 镜要尽快进入本集 hook：{episode_hook}。"
        if episode_hook else
        "第 1 镜按剧本真实开场自然进入；本集 hook 为空，禁止发明额外开场钩子。"
    )
    budget_block = _storyboard_progress_block(completed_shots)
    brief = _outline_brief(outline, shot_no)
    valid_info_ids = {item.info_id for item in screenplay.information_ledger or []}
    outline_block = _render_storyboard_outline(outline, shot_no, valid_info_ids)
    current_info_ids = [
        info_id for info_id in (brief.new_information_ids or [])
        if info_id in valid_info_ids
    ] if brief is not None else []
    ledger_context = ledger_context_for_shot(screenplay, completed_shots, current_info_ids)
    ledger_block = json.dumps(ledger_context, ensure_ascii=False, indent=2)
    brief_block = ""
    if brief is not None:
        brief_block = (
            f"\n【本镜大纲任务】（第 {shot_no}/{expected_total} 镜，必须落实这一条、不要停留在前面已覆盖的剧情）：\n"
            f"- 推进：{brief.beat}\n"
            + (f"- 状态链：{brief.state_in or '（未填）'} -> {brief.primary_action or brief.beat} -> {brief.state_out or '（未填）'}\n")
            + (f"- continuity_mode：{brief.continuity_mode or '（按规则选择）'}\n")
            + (f"- story_event_id：{brief.story_event_id}\n" if (brief.story_event_id or '').strip() else "")
            + (f"- 本镜新交付信息ID：{', '.join(current_info_ids)}\n" if current_info_ids else "")
            + (f"- 建议时长：{brief.duration_s}s\n" if brief.duration_s else "")
            + (f"- 画面可见角色：{', '.join(brief.characters_visible)}\n" if brief.characters_visible else "")
            + (f"- 声音演员/声源：{', '.join(brief.audio_cast)}\n" if brief.audio_cast else "")
            + (f"- 落实关键内容：{brief.covers}\n"
               "  这些内容必须明确写进本镜 action_desc、narration 或 dialogues；"
               "只出现在 covers/source_excerpt 里不算完成。\n" if (brief.covers or '').strip() else "")
            + (f"- 计划场景：{brief.scene_setting}\n" if (brief.scene_setting or '').strip() else "")
        )
    context_hints = [
        brief.beat if brief is not None else "",
        brief.covers if brief is not None else "",
        episode.get("hook") or "",
        episode.get("cliffhanger") or "",
        *[c.name for c in bible.characters],
    ]
    screenplay_window = _relevant_text_windows(
        screenplay.full_script_text, context_hints, max_chars=3000)
    source_window = _relevant_text_windows(source_text, context_hints, max_chars=2200)
    feedback_block = ""
    if final_feedback:
        # ④ 软剧情点保护：临近收尾时把"未落实必保留内容"升级为最高优先级，并明确剧情点可用
        # action_desc 直接拍出来（不必逐字台词）——避免"萧薰儿上前安慰"这类无台词软剧情被整段略过。
        head = ("\n【收尾前必须补齐·最高优先级】本集即将收尾，但以下剧本必保留内容仍未落实；"
                "请【本镜或紧接的下一镜】优先把它们拍出来/念出来，未全部落实前不得设 is_final=true：\n"
                if allow_finish else
                "\n【本集仍有未落实的必保留内容】（整集校验：以下内容尚未出现在已通过镜头中）：\n")
        tail = ("\n落实方式：关键台词写进 dialogues（人物开口）；"
                "其余主线事实写进 action_desc 的可见动作"
                "（例：'萧薰儿越过人群上前，伸手扶住萧炎、低声安慰'），只要画面或声轨体现即算落实，"
                "不要在压缩里整段略过。\n")
        feedback_block = head + "\n".join(f"- {e}" for e in final_feedback[:10]) + tail

    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》按顺序生成【第 {shot_no} 镜】。

你现在处于“逐镜头分镜台”：每次只输出一个镜头。前面已经 QA 通过的镜头不可重写，只能把它们作为上下文，继续往后承接剧情。

已确认完整剧本：
标题：{screenplay.title}
一句话梗概：{screenplay.logline}
剧本格式说明：{screenplay.script_format_note or '场次化台本稿'}
场次结构：
{chr(10).join(
    f"场{scene.scene_no}｜{scene.scene_heading}｜功能：{scene.story_function}｜人物：{'、'.join(scene.characters)}｜摘要：{scene.summary}｜冲突：{scene.conflict or '（无）'}｜转折：{scene.turn or '（无）'}"
    for scene in screenplay.scene_outline
) if screenplay.scene_outline else '（未提供场次结构）'}

与本镜任务相关的剧本节选（仅用于理解本镜任务；其余剧情由场次结构、分镜大纲和台账 ID 承接）：
{screenplay_window}

人物状态变化：
{chr(10).join(screenplay.character_state_changes) if screenplay.character_state_changes else '（无单列项）'}

情绪曲线：
{screenplay.emotional_curve}

结尾钩子：
{screenplay.ending_hook}

原文依据：
{screenplay.source_basis}

{key_content_block}
{scene_library_block}辅助结构：
- 开端：{screenplay.opening or '（未单列）'}
- 发展：{screenplay.development or '（未单列）'}
- 冲突：{screenplay.conflict or '（未单列）'}
- 高潮：{screenplay.climax or '（未单列）'}
- 改编方向：{screenplay.adaptation_direction or '（未单列）'}

{outline_block}

已通过镜头（必须作为上下文承接，不得改写）：
{_render_completed_shots_context(completed_shots[-1:])}

信息台账上下文（info_id 仅用于内部引用；创作与防重复必须理解其中的中文 content）：
{ledger_block}
{brief_block}{budget_block}{feedback_block}
当前镜头约束：
1. 只输出第 {shot_no} 镜，shot.shot_no 必须等于 {shot_no}。
2. 本集镜头软预算约 {SHOT_SOFT_MIN}~{SHOT_SOFT_MAX}（硬上限 {SHOT_HARD_MAX}）；当前按大纲推进到第 {shot_no}/{expected_total} 镜。本镜必须落实大纲第 {shot_no} 条、推进到新剧情，不得停留或复述已覆盖内容，也不得发明大纲/spine 之外的幻觉镜头。{"只有剧情已完整落到尾钩时才可设置 is_final=true，否则必须继续生成" if allow_finish else "剧情尚未铺到计划收尾，is_final 必须为 false"}。duration_s 默认 {PREFERRED_SHOT_DURATION_S}。
3. 从第 2 镜开始，必须明确承接上一镜的 state_out/observed_state_out；不要重演上一镜完整 action_desc。若 continuity_mode=action_continuation，state_in 必须等于上一镜实际尾状态；若换场或反应切，写清线索带入、时间跳转或视角切换原因。
4. {final_shot_rule}
5. 如果 is_final=false，本镜结尾要留下清楚的动作/情绪/信息状态，供下一镜继续。
6. 人物入画/出画必须符合导演调度：characters 里新增的人物，必须在 action_desc 或 first_frame_desc 中写清他/她从哪里来、如何进入画面或如何被镜头发现；上一镜在场但本镜不再可见的人物，必须有退开、离开、被遮挡、留在画外或换场的可见原因。禁止“上一镜没有，下一镜突然站在画面里/突然开口”。
7. continuity_mode 必须从 action_continuation / same_scene_cut / reaction_cut / reverse_angle / insert_detail / scene_change 中选择；只在同一人物同一动作跨镜延续时使用 action_continuation，普通同场景切换用 same_scene_cut / reaction_cut / reverse_angle / insert_detail，跨时空用 scene_change。
8. new_information_ids 只能从 current_ids / pending_ids 中选择本镜首次交付的信息，禁止自创英文 snake_case ID；若两栏均为空则输出空数组。do_not_repeat 只能填写 do_not_repeat 栏给出的中文剧情内容，不得填写裸 ID；已交付且不允许强化的信息不得重复讲。
9. 功能性路人合同：{extra_policy}。

拆分原则：
1. 按完整剧本的因果链继续往后拆，不能跳过中间关键事件，也不能重写已通过镜头已经覆盖的内容。
2. 每条 shot 都要推进剧情，且承接上一条的动作、情绪或信息状态。
3. scene_setting 只写时间+地点短标签，characters 只写实际出现在画面中、且入画原因已经交代清楚的角色。
4. 优先用真实台词+画面动作表达信息；narration 必须为空；禁止内心OS/旁白，无法开口的信息用姿态与表情大方向表达。
5. 每条 shot 都必须能追溯到完整剧本与原文依据，不要空泛扩写。
6. 第 1 镜处理：{'【本集是第一集】第 1 镜是全片开场建场镜，主任务是交代故事背景（世界观/主角处境/核心设定）为全片铺底，再自然带出本集 hook。' if int(episode.get('episode_no') or 0) == 1 else first_shot_entry_rule}
7. 最后 1 镜规则：{final_shot_rule}

{output_contract}

{preflight_contract}

本镜相关改编源文本节选（source_excerpt 必须从这里逐字摘录；它是上游改编证据和审计字段，不得写进后续 Seedance 画面提示词，也不得把原文散文当成可直接渲染内容）：
{source_window}

角色圣经：{bible.model_dump_json()}
上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON Schema：
{{"episode_no": {episode['episode_no']}, "is_final": bool, "shot": {{"shot_no": {shot_no}, "duration_s": int, "shot_size": "远景|全景|中景|近景|特写", "camera_move": "固定|推近|拉远|横摇|跟随", "scene_setting": "短时间+地点标签", "characters": ["画面中实际可见的角色圣经姓名或合法功能性路人标签"], "characters_visible": ["本镜画面可见角色，通常等于 characters"], "action_desc": str, "state_in": "本镜开始的精确人物/道具/信息状态", "primary_action": "本镜唯一主动作/主交付", "state_out": "本镜结束后交给下一镜的精确状态", "continuity_mode": "action_continuation|same_scene_cut|reaction_cut|reverse_angle|insert_detail|scene_change", "story_event_id": "对应 screenplay.events[].event_id；没有对应事件时必须输出空字符串，禁止输出 null", "new_information_ids": ["仅填写 information_ledger 中已有的 I1/I2 等内部编号"], "do_not_repeat": ["只能填写已交付信息的中文内容，禁止裸 ID"], "audio_cast": ["本镜发声角色/功能性声音"], "audio_timeline": [{{"start_s": float, "end_s": float, "type": "spoken_dialogue|offscreen_voice|ambient_sound", "speaker_id": "角色名/功能性身份或null", "text": str, "lip_sync": bool, "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定", "voice_canonical": str}}], "required_text": {{"surface": "道具/屏幕/牌匾等承载面", "exact_text": "需要画面准确出现的文字；无则为空", "appear_start_s": 0.0, "stable_until_s": null, "style": "", "allow_other_text": false}}, "first_frame_desc": "本镜开始的静止画面，25~50字，只写看得见的人物姿态/表情/手部/道具/光效", "last_frame_desc": "本镜结束的静止画面，25~50字，与首帧【同机位同场景同构图】，仅人物动作推进后的状态（不要换镜头/景别/场景）", "source_excerpt": "对应本镜头的小说原文逐字摘录，至少 {SOURCE_EXCERPT_MIN_CHARS} 字，仅作审计证据；其中双引号必须按 JSON 规范转义", "narration": "", "dialogues": [{{"speaker": "必须是本镜头 characters 中的可见角色名或功能性路人标签", "line": str, "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定", "delivery": "spoken_dialogue|offscreen_voice"}}], "transition": "{transition_options}"}}}}"""
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    log_provider_call(
        "storyboard_shot_prompt", config.MODEL_TEXT, "PROMPT_READY", None, 0,
        meta={
            "episode_id": episode.get("id"),
            "episode_no": episode.get("episode_no"),
            "shot_no": shot_no,
            "completed_shots": len(completed_shots),
            "expected_total": expected_total,
            "has_outline_brief": brief is not None,
            "source_chapters": episode.get("source_chapters"),
            "source_chars": len(source_text),
            "prompt_chars": len(prompt),
            "source_hash": source_hash,
            "prompt_hash": prompt_hash,
            "contract_version": "renderability_v1",
            "screenplay_mode": "full_script",
        })
    repair_output_contract = f"""只输出一个 JSON 根对象，根字段为 episode_no、is_final、shot。
shot 必须是单数对象，shot.shot_no 必须等于 {shot_no}；禁止输出 shots 数组，禁止附带下一镜。
shot.story_event_id 必须是 JSON 字符串；没有对应事件时输出 ""，禁止输出 null。
source_excerpt 内的双引号必须按 JSON 规范转义，或改用中文引号，不能破坏根对象语法。
如果当前内容仍超过最长 {config.VIDEO_DURATION_MAX_S}s 的容量，只压缩本镜到大纲已分配的内容；后续节拍由系统在下一轮逐镜生成。"""
    repair_context = f"""当前仅修复第 {shot_no} 镜，不得输出其他镜头。
本镜大纲：{brief.beat if brief is not None else '（按完整大纲继续推进）'}
本镜必落内容：{brief.covers if brief is not None else '（无单列项）'}
角色圣经成员：{'/'.join(c.name for c in bible.characters)}
功能性路人：{extra_policy}。
合法时长：{config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S}s 整数，由模型按动作与口播选择最短可用时长；
口播上限随时长变化（5s={config.max_spoken_chars_for_duration(5)}字，10s={config.max_spoken_chars_for_duration(10)}字）。
合法景别：{'|'.join(sorted(SHOT_SIZES))}；合法运镜：{'|'.join(sorted(CAMERA_MOVES))}；合法转场：{transition_options}。
上一镜详细承接：{_render_completed_shots_context(completed_shots[-1:])}
本镜相关剧本：
{screenplay_window}
本镜可逐字摘录原文：
{source_window}
修复时必须保留最近输出中已经正确的字段，只修正错误清单点名的问题。"""
    shot_loop = AgentLoop(
        stage_key=f"storyboard_shot_{shot_no}",
        contract_key="storyboard",
        goal=f"生成第 {shot_no} 镜并通过逐镜合同，保留已通过 checkpoint",
        scope_type="storyboard_checkpoint",
        scope_id=f"{episode.get('id') or episode['episode_no']}:{shot_no}",
        artifact_type="storyboard_shot",
        policy=AgentLoopPolicy(
            max_iterations=4, stall_rounds=2, min_quality_gain=0.03,
            no_gain_rounds=2, allow_warning_candidate=True,
        ),
    )
    draft = await _run_with_agent_loop(
        "分镜脚本", "storyboard", prompt, StoryboardShotDraft,
        lambda d: _validate_storyboard_shot_draft(
            d,
            episode=episode,
            bible=bible,
            screenplay=screenplay,
            completed_shots=completed_shots,
            shot_no=shot_no,
            allow_finish=allow_finish,
            must_finish=must_finish,
            outline_covers=(brief.covers if brief is not None else ""),
            # 向后承接：大纲排给后续镜头的事实留给后面拍，本镜不因此报漏戏。
            later_planned_covers="".join(
                (s.covers or "") for s in (outline.shots[shot_no:] if (outline and outline.shots) else [])
            ),
        ),
        loop=shot_loop,
        temperature=0.7,
        max_tokens=config.STORYBOARD_SHOT_MAX_TOKENS,
        repair_context=repair_context,
        repair_output_contract=repair_output_contract,
        prefill={"episode_no": episode["episode_no"]},
        storyboard_candidate_context={
            "episode_id": episode.get("id"),
            "episode_no": episode["episode_no"],
            "shot_no": shot_no,
            "outline_story_event_id": brief.story_event_id if brief is not None else "",
        },
    )
    sync_shot_continuity_fields(draft.shot, completed_shots[-1] if completed_shots else None)
    ensure_audio_timeline(draft.shot, screenplay.voice_bible)
    # 防重复约束是给后续创作/视频模型理解的中文语义，不持久化裸内部 ID。
    draft.shot.do_not_repeat = list(ledger_context.get("do_not_repeat") or [])
    _normalized_candidate_board(episode["episode_no"], completed_shots, draft.shot, bible, episode["target_duration_s"])
    return draft


# ---------- C2. 单集分镜脚本（基于完整剧本拆分） ----------

def _first_shot_rule(episode: dict) -> str:
    """第 1 镜的写作要求：常规集=直接进 hook；但【第一集第一镜】是全片开场，主要职责是交代故事背景
    （世界观/主角处境/基本设定），为后续剧情铺底，而不是急着推进情节或抛冲突。"""
    hook = (episode.get("hook") or "").strip()
    cliffhanger = (episode.get("cliffhanger") or "").strip()
    ending = (
        f"最后 1 个镜头必须呈现悬念钩：{cliffhanger}"
        if cliffhanger else
        "最后 1 个镜头只收束到剧本/原文已有状态；本集 cliffhanger 为空，禁止发明下一集钩子"
    )
    if int(episode.get("episode_no") or 0) == 1:
        return (
            f"23. 【第一集第一镜=全片开场建场镜，特殊规则，优先级最高】这一镜的主要任务是【交代故事背景】，"
            f"不是推进剧情、不是抛冲突反转：用【画面 + 必要真实台词】把【世界观/时代设定/主角是谁、身处什么处境、基本关系或核心设定】"
            f"讲清楚，让没看过原著的观众迅速进入这个故事。\n"
            f"    - action_desc 写一个能代表本片世界观/主角日常处境的【建立性画面】（establishing shot），"
            f"人物动作克制、信息靠画面与必要对白承载；禁止旁白/内心OS；不要在第一镜就让主角做剧烈动作或触发核心冲突。\n"
            f"    - narration 必须为空字符串；shot_size 优先用远景/全景做开场建场，先把环境和主角位置交代清楚。\n"
            "    - 开场需要更长铺陈时，可为单一连续建场动作选择 5~10 秒；超过 10 秒或进入新节拍时拆成相邻建场镜，逐步完成环境建立与人物入场，"
            f"所以 action_desc/首尾帧请按\"远景缓慢推近、镜头从环境推向主角\"来写：首帧是交代环境的大远景，"
            f"尾帧镜头推近到主角、但仍是同一机位的连续推进，人物动作保持克制连贯。\n"
            + (
                f"    - 仍要包含本集 hook：{hook}，但以\"先立背景、再带出钩子\"的方式呈现，"
                "不要为了 hook 牺牲掉背景交代。\n"
                if hook else
                "    - 本集 hook 为空：开场只按原文和剧本真实情境建立世界与主角处境，禁止额外发明开场钩子。\n"
            )
            + f"    {ending}")
    opening = (
        f"第 1 个镜头必须呈现本集 hook：{hook}"
        if hook else
        "第 1 个镜头按剧本真实开场自然进入，禁止因 hook 为空发明额外钩子"
    )
    return f"23. {opening}\n    {ending}"


def _storyboard_output_contract(episode: dict, bible: Bible, durations: list[int],
                                speech_styles: str) -> str:
    target = episode["target_duration_s"]
    min_shots, max_shots = storyboard_shot_count_range(target)
    character_names = "、".join(c.name for c in bible.characters) or "（角色圣经为空）"
    duration_options = "/".join(str(value) for value in durations)
    speech_budgets = "、".join(
        f"{value}s≤{config.max_spoken_chars_for_duration(value)}字" for value in durations
    )
    extra_policy = functional_extra_policy_text()
    return f"""硬性输出规范（以下规则由代码校验，违反会被退回重写；请首轮直接满足）：
1. episode_no 必须等于 {episode['episode_no']}；shots 按剧情顺序排列，shot_no 必须从 1 开始连续递增，不能跳号、重复或乱序。
2. 整集镜头软预算约 {SHOT_SOFT_MIN}~{SHOT_SOFT_MAX}（硬上限 {SHOT_HARD_MAX}）；覆盖 must_keep spine 与主线台词后即可收束。超预算必须合并。
3. 复杂动作可拆，但优先删减超纲细节而非无限拆镜；禁止为碎镜而合并删主线。
4. duration_s **默认 {PREFERRED_SHOT_DURATION_S}s**；只能取整数 {duration_options} 秒。
   - 【选择原则】绝大多数镜用 {PREFERRED_SHOT_DURATION_S}s。仅当口播超过 {PREFERRED_SHOT_DURATION_S}s 预算、或同一连续动作确需铺陈时，才取 6~10s；超过 5s 的镜会进入 AI 时长审核。禁止无内容拉长。
   - 【硬性·音画同步】口播预算随 duration_s 增长：{speech_budgets}。选择时长后，动作与口播必须都能在该时长内自然完成。
   - 【硬性·拆镜边界】不同时间、地点、主动作必须拆镜；同 spine 事件通常 1~2 镜封顶。
5. 关键：每条 shot 只表现【一个】连贯主动作（大形体可读）。严禁出现"切到/切至/镜头切/镜头转向/闪回/回忆画面/分屏/下一个镜头/→"。禁止微表情/衣角/眼泪/指节等超纲词。
6. 单镜要像一个真实可拍的连续动作（例如"他走向石碑并抬手贴上碑面"是一个动作；"她哭→镜头切到门口→闪回六年前"才是错误快切）。
7. 声轨纪律（重要）：分镜只保留【真实台词】（dialogues）；禁止旁白、内心OS、画外解说。人群/气氛声写进 action_desc。不能把有对白的剧本压成纯画面卡；是否开口由本镜信息交付与口播容量决定。
8. action_desc 目标 {ACTION_DESC_TARGET_MIN}~{ACTION_DESC_TARGET_MAX} 字：写清主体姓名与这一个大形体主动作；不要罗列多个镜头，不要写运镜术语。
8b. 【关键·首尾帧】每条 shot 必须给出 first_frame_desc 与 last_frame_desc：
   - 二者必须是【同一机位、同一场景、同一构图】下，这一个连贯动作的开始与结束瞬间。
   - 正例：首帧「角色A手掌刚贴上石碑，神情平静，碑面无光」；尾帧「同一机位，角色A手掌仍贴在石碑上，碑面亮起，他眉头紧锁」。
   - 各约 20~40 字；不要写超纲微细节、字幕、运镜。
8c. 【导演调度】characters ≤3；入画/出画用走、停、转身等大动作交代；禁止凭空出现/消失。
9. source_excerpt 必填：至少 {SOURCE_EXCERPT_MIN_CHARS} 字，可与相邻镜共享同一主线段落；仅作审计，不得进入 Seedance。
10. 字数只校验必要下限；优先保证主线可看，不要为凑数字堆细节。
11. 信息密度靠"一个清晰动作 + 必要台词"；禁止呆立、氛围空镜、重复上一镜。
12. 【硬性·禁旁白】narration 必须为空字符串 ""；禁止内心OS/画外解说/旁白员。无法开口的信息改用画面姿态表达。
12a. 【口播优先】单镜台词纯文字（不计标点）受第 4 条约束（最长 10s 也不得超过 {config.MAX_SPOKEN_CHARS_PER_SHOT} 字）。环境群像声优先写进 action_desc。
12b. 【声轨时序】同镜多句台词按剧情顺序排列，勿内容重复撞车。
13. 角色名必须准确：characters 不能为空；有姓名角色只能使用角色圣经准确姓名：{character_names}。{extra_policy}。
14. action_desc 必须显式写出本镜头主要角色的准确姓名。
15. dialogues 只写人物实际开口台词，dialogues[*].speaker 必须在本镜头 characters 中。
16. 【单镜】台词纯文字口播必须满足第 4 条上限。emotion 只能取：{'|'.join(sorted(EMOTIONS))}。说话风格：{speech_styles or '（无额外说话风格）'}。
17. scene_setting 建议 {SCENE_SETTING_MAX_CHARS} 字以内，只写"时间，地点"。
18. shot_size 只能取：{'|'.join(sorted(SHOT_SIZES))}；camera_move 只能取：{'|'.join(sorted(CAMERA_MOVES))}；transition 只能取：{'|'.join(sorted(TRANSITIONS))}。
19. 同一 scene_setting 的镜头必须连续排列；同一场景的 scene_setting 必须逐字相同。
20. 连续 3 个镜头不得使用相同 shot_size。
21. 相邻镜头用 continuity_mode 表达承接；action_continuation 仅用于同一人物同一动作跨镜延续。
22. 转场设计：同场景连续镜只能用"硬切"；换场不得硬切。
{_first_shot_rule(episode)}
24. 特效服从剧情，日常对话写实克制。
25. 动作符合物理；复杂手势改写成更稳的简单动作（掌心托物、握拳、伸手按住）。"""


def _storyboard_preflight_contract(episode: dict) -> str:
    target = episode["target_duration_s"]
    min_shots, max_shots = storyboard_shot_count_range(target)
    hints = "、".join(TRANSITION_HINTS[:12])
    return f"""首轮输出前必须逐镜预检（这些就是代码校验器的具体判定条件，不要等返工）：
1. 镜头软预算约 {SHOT_SOFT_MIN}~{SHOT_SOFT_MAX}（硬上限 {SHOT_HARD_MAX}）；每条 duration_s **默认 {PREFERRED_SHOT_DURATION_S}s**，仅必要时取到 {config.VIDEO_DURATION_MAX_S}s（>5 进 AI 审核）。超预算优先合并，禁止无限拆碎。
2. 第 1 镜 continuity_mode 不得为 action_continuation；第 2 镜开始逐条和上一镜比较 state_out、scene_setting 与角色可见状态。
3. 如果本镜 scene_setting 与上一镜完全相同：
   - continuity_mode 必须是 same_scene_cut / reaction_cut / reverse_angle / insert_detail / action_continuation 之一；
   - 只有同一人物同一动作跨镜延续时才能使用 action_continuation，且 state_in 必须承接上一镜 state_out/observed_state_out；
   - transition 必须为"硬切"；
   - characters 至少保留上一镜的 1 个核心人物；
   - action_desc/state_in 必须承接上一镜结尾的人物位置、道具/屏幕内容、动作或情绪，不能重新介绍场景或重复上一镜发现；
   - 如果本镜 characters 比上一镜多了某个角色，必须写清他/她“走进、上前、转身露出、从人群中出来、被带入”等入画过程；如果少了某个角色，必须写清他/她退开、离开、被遮挡、留在画外或换到另一人反应，禁止凭空出现/消失。
4. 如果本镜 scene_setting 与上一镜不同：
   - continuity_mode 必须为 scene_change；
   - transition 必须选择明确的换场方式，绝不能用"硬切"；普通时空跳转优先"淡出淡入"，情绪/回忆延续优先"声音延续+叠化"，悬疑冲击用"闪黑/闪白"，动作追逐用"甩镜/遮挡转场"，有构图呼应时用"匹配剪辑"；
   - action_desc 必须写清承接原因、时间跳跃或线索带入，建议出现：{hints} 等承接词；
   - 上一镜 last_frame_desc 必须带这个转场的结尾视觉，本镜 first_frame_desc 必须是新时间/新地点的建立画面；
   - 如果只是同一段连续动作里从房间走到门口/楼道/桌边/窗前，不要改 scene_setting，继续沿用上一镜主场景标签，把移动写进 action_desc。
5. scene_setting 是稳定短标签，不是镜头内容：同一连续时空统一写同一个"时间，主地点"，例如"当日，场景A"；不要在相邻镜头里改成"当日，场景A楼道外/桌前/门口"导致断链。
6. characters 只写本镜头实际可见/在场且已交代入画原因的人；允许第 13 条定义的功能性路人，具体姓名仍必须来自角色圣经。屏幕发信人、纸条落款、新闻里提到的人、AI 软件名不算 characters。
7. 每条 action_desc 必须显式写出 characters 中角色姓名与这一个大形体主动作（{ACTION_DESC_TARGET_MIN}~{ACTION_DESC_TARGET_MAX} 字）；禁止超纲细节词与切镜词。
8. 每条 shot 的 source_excerpt 必填（≥{SOURCE_EXCERPT_MIN_CHARS} 字），可与相邻镜共享主线段落；仅作审计，不得进入 Seedance。
9. 声轨预检：若完整剧本对应段落有“角色名：台词”且本镜负责交付该信息，必须写 dialogues；内心独白禁止写进 narration（narration 必须为空），改用画面姿态表达；人群嘲讽/恭维写进 action_desc。是否发声服从本镜信息交付与口播容量，禁止为比例凑对白。
10. first_frame_desc 与 last_frame_desc 必须同机位、同场景、同构图，只让人物动作从"开始"推进到"结束"；不要让首尾帧变成两个不同的镜头/景别/场景。
11. 人物调度预检：逐条核对上一镜 last_frame_desc、本镜 first_frame_desc、characters、action_desc。任何角色的入画、出画、开口、转身、靠近、退后都必须有可见动作链；如果一句话解释不清，就拆成相邻两镜，不要让视频模型自行脑补。

常见错误 → 正确写法（以下角色A/场景A仅为占位示例，请替换成本集真实角色与场景）：
- 错：上一镜"当日，场景A"，本镜"当日，场景A楼道外"，transition="硬切"，又没有解释。对：若是角色A从房内走到门口，scene_setting 仍写"当日，场景A"，continuity_mode="action_continuation"，state_in 承接上一镜 state_out，action_desc 写"角色A攥着上一镜的纸页走向门口……"。
- 错：纸条上出现一个落款名就把 characters 写成 ["该落款名"]。对：如果画面只拍到角色A和纸条，characters 写 ["角色A"]，纸条文字放 action_desc。
- 错：下一镜重新说"场景A昏暗、桌上有电脑"。对：下一镜直接从上一镜结尾继续，写"角色A仍盯着刚弹出的新闻推送，手指停在屏幕上，随后抬头望向门口，最后攥紧纸页。"。"""


def _score_or_none(value) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score) or math.isinf(score):
        return None
    if 1 < score <= 100:
        score /= 100
    return max(0.0, min(1.0, score))


def _extract_score_from_text(raw: str, key: str) -> float | None:
    key_pat = re.escape(key)
    number = r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))"
    patterns = (
        rf'["`]?{key_pat}["`]?\s*[:：]\s*{number}',
        rf'\b{key_pat}\b[\s\S]{{0,240}}?(?:score|评分|分数)\s*[:：]?\s*{number}',
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            score = _score_or_none(match.group(1))
            if score is not None:
                return score
    return None


def _issues_from_text(raw: str) -> list[str]:
    lower = raw.lower()
    issues: list[str] = []
    if any(word in lower for word in ("watermark", "ai生成", "text", "logo")) or any(word in raw for word in ("水印", "文字", "字幕", "标识")):
        issues.append("画面可能含文字/水印，请人工确认")
    if any(word in lower for word in ("extra person", "extra character")) or any(word in raw for word in ("多余人物", "额外人物")):
        issues.append("画面可能出现多余人物")
    if any(word in lower for word in ("deform", "distort", "merged joints", "finger")) or any(word in raw for word in ("畸形", "崩坏", "手指")):
        issues.append("画面可能存在肢体或五官异常")
    return issues


def _normalize_issues(value, fallback: list[str] | None = None) -> list[str]:
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(value, str) and value.strip():
        items = [value.strip()]
    else:
        items = []
    if not items and fallback:
        items = fallback
    return items[:8]


def _bool_or_default(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "pass", "passed", "是", "通过"}:
            return True
        if lowered in {"false", "no", "0", "fail", "failed", "否", "不通过"}:
            return False
    return default


def _normalize_qa_object(obj: dict, score_keys: list[str], *, raw: str = "",
                         defaults: dict[str, float] | None = None,
                         recovered: bool = False) -> dict:
    defaults = defaults or {}
    out: dict[str, object] = {}
    known_scores: list[float] = []
    incomplete = False
    for key in score_keys:
        score = _score_or_none(obj.get(key))
        if score is None:
            score = defaults.get(key)
        if score is None and raw:
            score = _extract_score_from_text(raw, key)
        if score is None:
            incomplete = True
            score = 0.0
        out[key] = score
        known_scores.append(score)
    overall = _score_or_none(obj.get("overall"))
    if overall is None:
        overall = defaults.get("overall")
    if overall is None:
        overall = round(sum(known_scores) / len(known_scores), 3) if known_scores else 0.0
    out["overall"] = max(0.0, min(1.0, overall))
    fallback_issues = _issues_from_text(raw) if raw else []
    if recovered and not fallback_issues:
        fallback_issues = ["VLM返回了非标准JSON，已按保守规则恢复评分"]
    out["issues"] = _normalize_issues(obj.get("issues"), fallback_issues)
    out["failure_types"] = _normalize_issues(obj.get("failure_types"))
    out["observed_state_out"] = str(obj.get("observed_state_out") or "").strip()
    for key in ("no_story_repeat", "no_future_leak", "no_character_duplicate", "whole_clip_usable"):
        out[key] = _bool_or_default(obj.get(key), True)
    # 供自动重抽判断“这是资产质量失败，还是 QA 响应格式失败”。
    # 非标准输出可以展示恢复分，但不应据此花钱重生视频。
    out["qa_recovered"] = recovered or incomplete
    return out


def _parse_qa_result(raw: str, score_keys: list[str], *,
                     defaults: dict[str, float] | None = None) -> dict:
    try:
        obj = extract_json(raw)
        return _normalize_qa_object(obj, score_keys, raw=raw, defaults=defaults)
    except ValueError:
        recovered = {key: _extract_score_from_text(raw, key) for key in score_keys}
        recovered = {key: value for key, value in recovered.items() if value is not None}
        return _normalize_qa_object(recovered, score_keys, raw=raw, defaults=defaults, recovered=True)


# ---------- E. VLM 质检 ----------

async def review_scene_image(image_b64: str, frame_desc: str, scene_setting: str,
                             character_anchors: list[str], prev_image_b64: str | None = None,
                             kind: str = "tail") -> dict:
    """场景关键帧评审 agent：只对照【本帧自己的画面描述】（首图描述 / 尾图描述）检查该单张静止帧，
    不要拿整段动作或后续画面来要求它。返回 {expectation_match, continuity, clean_frame, overall, issues}。"""
    anchors = "\n".join(character_anchors) or "（缺少角色锚点）"
    frame_name = "首图（本镜动作开始前的静止画面）" if kind == "head" else "尾图（本镜动作完成后的静止画面）"
    cont = ("\n本关键帧需与第2张参考图在画风、人物形象、光影上自然连贯（第2张可能是本镜首图或上一镜尾图）。"
            if prev_image_b64 else "\n本关键帧是新场景起点，无需对比上一镜。")
    expectation = f"""你是漫剧场景关键帧评审 agent。下面给出本镜{frame_name}{('（第1张）以及参考图（第2张，仅作连贯性对比）' if prev_image_b64 else '')}，对照下面这【单张静止帧】的预期检查，输出 JSON。

重要：只审这一张静止帧是否符合它自己的画面描述；不要因为它没有表现整段动作的过程或后续/结尾画面而扣分（动作的展开由视频负责，关键帧只是这一刻的定格）。但【这一刻的人物姿态、朝向与互动】必须与描述一致——定格不等于可以摆拍。

本帧预期画面：{frame_desc}
预期场景：{scene_setting}
预期角色外观：
{anchors}{cont}

检查项（各 0~1 评分）：
1. expectation_match  画面是否符合【本帧预期画面】，重点核对：人物姿态/表情/手部与道具的接触状态、人物的身体与视线【朝向】、人物与对象（道具或另一人）之间的【空间互动关系】，以及角色外观、场景是否对得上
2. continuity         与参考图的画风、人物形象、光影是否连贯（无参考图则给 1）
3. clean_frame        无文字/水印/多余人物/肢体畸形/五官崩坏

评分硬规则（务必遵守）：
- expectation_match 是本次评审的【主项】。若预期画面里人物在与某对象/另一人互动（触碰/按压/拿取/递出/挥击/指向/注视/搀扶等），而画面中人物只是正面端站、双手垂放、目视镜头，或朝向/接触与描述不符（例如该摸石碑却没碰到石碑、身体正对镜头而非转向石碑），expectation_match 必须 ≤0.4。
- overall 不得高于 expectation_match：动作/朝向/互动不对就是不合格，画面再干净、画风再连贯也不能给高 overall。
- issues 里必须逐条点明具体不符之处（例如"人物未触碰石碑、身体正对镜头而非转向石碑"），供下一版定向改正。

只输出 JSON：{{"expectation_match": float, "continuity": float, "clean_frame": float, "overall": float, "issues": [str]}}"""
    frames = [image_b64] + ([prev_image_b64] if prev_image_b64 else [])
    raw = await hiagent.vlm_check(
        frames, expectation,
        call_meta={
            "initiator_label": "关键帧评审",
            "frame_kind": kind,
            "scene_setting": scene_setting,
            "has_prev_frame": bool(prev_image_b64),
        })
    defaults = {"continuity": 1.0} if not prev_image_b64 else None
    result = _parse_qa_result(raw, ["expectation_match", "continuity", "clean_frame"], defaults=defaults)
    # 动作/朝向/互动是关键帧的主项：把 overall 夹到不超过 expectation_match，避免"画面干净但动作不对"
    # 被 continuity/clean_frame 拉高均值而蒙混过审（VLM 即便没遵守上面的硬规则，这里也强制生效）。
    em = _score_or_none(result.get("expectation_match"))
    if em is not None:
        result["overall"] = round(min(float(result["overall"]), em), 3)
    return result


async def review_portrait_image(image_b64: str, appearance_anchor: str) -> dict:
    """Review a character reference without imposing ordinary-human posing rules.

    Portrait anchors can describe spirits, creatures, floating bodies, props, or
    a non-neutral expression.  Those anchor-specific requirements take priority
    over the conventional front-facing model-sheet pose.
    """
    expectation = f"""你是漫剧角色定妆照评审 agent。请只对照角色锚点检查这张单角色全身设定图，输出 JSON。

角色锚点：{appearance_anchor}

检查项（各 0~1 评分）：
1. expectation_match  年龄、性别、发型、服装、体态、材质/透明度、表情、指定道具及空间关系是否与锚点一致
2. continuity         当前没有历史参考图，固定给 1
3. clean_frame        无文字/水印/多余人物/肢体畸形/五官崩坏，主体与锚点要求的道具完整可见

评分硬规则（务必遵守）：
- 锚点优先于普通定妆照惯例。若锚点要求透明、魂体、悬浮、指定表情或与道具的空间关系，必须按锚点核对；不要反过来要求双脚着地、中性表情或普通实体人站姿。
- 锚点要求的非实体形态、表情或道具空间关系缺失时，expectation_match 必须 ≤0.4。
- 锚点没有要求的火焰、斗气光环、文字或其他主体都属于多余元素，应写入 issues。
- overall 不得高于 expectation_match；issues 必须指出可直接用于下一轮修图的具体差异。

只输出 JSON：{{"expectation_match": float, "continuity": 1.0, "clean_frame": float, "overall": float, "issues": [str]}}"""
    raw = await hiagent.vlm_check(
        [image_b64],
        expectation,
        call_meta={
            "initiator_label": "角色定妆照评审",
            "asset_kind": "portrait",
            "has_prev_frame": False,
        },
    )
    result = _parse_qa_result(
        raw,
        ["expectation_match", "continuity", "clean_frame"],
        defaults={"continuity": 1.0},
    )
    expectation_match = _score_or_none(result.get("expectation_match"))
    if expectation_match is not None:
        result["overall"] = round(min(float(result["overall"]), expectation_match), 3)
    return result


async def qa_shot(frames_b64: list[str], action_desc: str, scene_setting: str,
                  character_anchors: list[str], state_in: str = "", state_out: str = "",
                  required_dialogue: str = "", required_text: str = "",
                  *, duration_s: int | None = None,
                  duration_needs_review: bool = False) -> dict:
    anchors = "\n".join(character_anchors) or "（缺少角色锚点，应回到分镜补角色）"
    duration_block = ""
    if duration_needs_review or (duration_s is not None and int(duration_s) > PREFERRED_SHOT_DURATION_S):
        duration_block = f"""
额外时长审核（本镜标称 {duration_s or '?'}s，超过默认 {PREFERRED_SHOT_DURATION_S}s）：
- duration_justified：若画面动作与口播显然在 {PREFERRED_SHOT_DURATION_S}s 内就能完成，必须为 false，并在 issues 写明「时长过长，建议改回 {PREFERRED_SHOT_DURATION_S}s」；
- 仅当连续动作/口播确实需要更长窗口时才为 true。
"""
    expectation = f"""你是 AI 视频质检员。对照预期检查这几帧画面（同一镜头的首/中/尾），输出 JSON。

预期画面：{action_desc}
预期起始状态：{state_in or '（未单列；按预期画面开头判断）'}
预期结束状态：{state_out or '（未单列；按预期画面结果判断）'}
预期场景：{scene_setting}
预期对白/声轨：{required_dialogue or '（无指定；若画面无法判断则 dialogue_match 给 1）'}
预期画面文字：{required_text or '（无指定；若无文字要求则 text_match 给 1）'}
预期角色外观：
{anchors}
{duration_block}
检查项（各 0~1 评分）：
1. character_match  角色外观与预期相符（发型/服装/年龄感）
2. action_match     画面内容与预期动作相符
3. clean_frame      无文字/水印/多余人物/肢体畸形
4. start_state_match 首帧/开头是否匹配预期起始状态
5. end_state_match   尾帧/结尾是否匹配预期结束状态
6. dialogue_match    可见口型/字幕/声画证据是否没有违背预期对白；无指定对白时给 1
7. text_match        需要画面文字时是否准确；无文字要求时给 1
{"8. duration_justified  超过默认时长是否必要（仅当上方要求审核时填写；不需要时给 true）" if duration_block else ""}

评分硬规则（评分会直接决定是否花费重抽，务必严格且稳定）：
- 只根据所给首/中/尾帧中可见的证据评分，不得因为“可能在未抽到的时刻发生”而臆测通过。
- action_match 是主项：核心动作、人物朝向、道具交互或动作结果未出现时必须 ≤0.4。
- character_match 是主项：主要角色错人、外观明显不符或跨帧换脸/换装时必须 ≤0.4。
- start_state_match / end_state_match 是连续性主项：起始状态或结束状态明显不符时对应项必须 ≤0.4。
- dialogue_match / text_match 是可用性主项：指定台词被改写、字幕乱码、屏幕文字错误时对应项必须 ≤0.4。
- no_story_repeat：若视频重演上一镜已完成内容则 false；no_future_leak：若抢演后续镜头/未来剧情则 false；no_character_duplicate：若同一角色分身/重复出现则 false；whole_clip_usable：若需要裁掉片头片尾或中间无效段才可用则 false。
- failure_types 只能使用 story_repeat、future_leak、wrong_dialogue、text_error、character_duplicate、state_mismatch、needs_crop 等短码。
- observed_state_out 用一句话描述视频实际尾部状态，供下一镜承接。
- overall 不得高于 character_match、action_match、start_state_match、end_state_match、dialogue_match、text_match 中的最低主项；画面干净不能掩盖错人、错动作、状态错、台词/文字错。
- issues 只写画面中可见、可定向修复的具体问题；达标时输出空数组，不要写泛化建议。
{"- 若 duration_justified=false，必须把「时长过长」写入 issues，且 overall 不超过 0.55。" if duration_block else ""}

只输出 JSON：{{"character_match": float, "action_match": float, "clean_frame": float, "start_state_match": float, "end_state_match": float, "dialogue_match": float, "text_match": float, "no_story_repeat": bool, "no_future_leak": bool, "no_character_duplicate": bool, "whole_clip_usable": bool, "failure_types": [str], "observed_state_out": str, "overall": float, "issues": [str]{', "duration_justified": bool' if duration_block else ''}}}"""
    raw = await hiagent.vlm_check(
        frames_b64, expectation,
        call_meta={"initiator_label": "视频自动质检", "scene_setting": scene_setting})
    result = _parse_qa_result(
        raw,
        [
            "character_match", "action_match", "clean_frame",
            "start_state_match", "end_state_match", "dialogue_match", "text_match",
        ],
        defaults={
            "start_state_match": 1.0,
            "end_state_match": 1.0,
            "dialogue_match": 1.0,
            "text_match": 1.0,
        },
    )
    caps = [
        _score_or_none(result.get(key))
        for key in ("character_match", "action_match", "start_state_match",
                    "end_state_match", "dialogue_match", "text_match")
    ]
    caps = [score for score in caps if score is not None]
    if caps:
        result["overall"] = round(min(float(result["overall"]), *caps), 3)
    if duration_block and result.get("duration_justified") is False:
        issues = list(result.get("issues") or [])
        if not any("时长" in str(x) for x in issues):
            issues.append(f"时长过长，建议改回 {PREFERRED_SHOT_DURATION_S}s")
        result["issues"] = issues
        result["overall"] = round(min(float(result.get("overall") or 1), 0.55), 3)
    return result
