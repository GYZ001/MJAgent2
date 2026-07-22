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
from typing import Callable

from pydantic import BaseModel

from app import config, hiagent
from app.character_policy import functional_extra_policy_text
from app.db import get_setting, log_provider_call
from app.evaluations.issues import issues_from_messages
from app.harness import model_gateway
from app.loops import AgentLoop, AgentLoopFailure, AgentLoopPolicy
from app.schemas import (Bible, CAMERA_MOVES, EMOTIONS, EpisodeScreenplay,
                         SHOT_SIZES, Scene, Shot, Storyboard, StoryboardOutline,
                         StoryboardOutlineShot, TRANSITIONS,
                         extract_json, schema_errors)
from app.validators import (ACTION_DESC_MIN_CHARS, NARRATION_TARGET_CHARS,
                            SCENE_SETTING_MAX_CHARS,
                            SOURCE_EXCERPT_MIN_CHARS,
                            defer_establishing_covers,
                            downgrade_outline_offbible_spoken,
                            TRANSITION_HINTS, _atomize_claim, _condense, _covers_has_crowd,
                            _covers_has_spoken, _covers_outside_spoken,
                            _too_similar,
                            normalize_action_desc, normalize_continuity,
                            normalize_offbible_characters, normalize_transition_visuals,
                            relieve_spoken_overflow,
                            storyboard_shot_count_range,
                            validate_bible, validate_screenplay,
                            validate_scene_bible,
                            validate_storyboard,
                            validate_storyboard_shot_covers_outline,
                            validate_storyboard_outline,
                            validate_storyboard_preserves_key_content,
                            validate_storyboard_soundtrack)

SYSTEM_PREFIX = (
    "你是专业的竖屏漫剧（动态漫画短剧）编剧与分镜师。\n"
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
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》把小说改写成【完整剧本】。

你现在处于“剧本台”阶段，不是分镜阶段。你的职责是先写出一整集完整、连续、可阅读、可拆镜的【生产级剧本稿】。

剧本层职责：
1. 生成一整集完整故事，而不是拍卡列表或摘要提纲。
2. 保证剧情连贯、人物情绪连贯、因果关系连贯。
3. 输出能直接进入导演/分镜阶段的剧本稿，不要只写成长梗概。
4. 保留原文依据，并明确改编方向。
5. 输出适合后续拆成若干个 5~10 秒视频分镜的连续剧本；镜头数和整集时长不设上限，以完整演绎剧情为准；
   分镜模型会按单镜动作与口播判断具体时长；动作或口播超过 10 秒仍无法完整表达时，必须拆成连续相邻镜头。
6. 不在正文里输出“拍01/拍02/拍03”，不写景别、运镜、首尾帧、参考图、提示词。

【导演级连续性要求】你要像现场导演一样先把戏调顺，而不是只改写对白：
- 每场开头必须交代本场已在场人物、他们的大致位置/姿态/注意力，不要让人物在正文中突然出现。
- 任何人物进入、离开、走近、退后、转身、拿起/放下道具，都必须有可见动作承接；不能上一段不在场、下一句突然开口。
- 每场结尾必须留下清楚的“人物位置 + 情绪状态 + 关键道具状态”，供下一场/下一镜自然承接。
- 人物行为要符合动机和情绪递进：先看到/听到/意识到，再反应、开口或行动；禁止无因果地突然爆发、突然换态度。

【最重要·防丢失】{episode['target_duration_s']} 秒只作为初始节拍参考；不得为了追目标时长删除关键台词、关键剧情或结尾钩子。
所以你必须先做"必保留清单"，再写正文，并保证清单里的每一条都真实写进了正文：
- `key_lines`：只保留本集源文中【已在人物谱中的角色】（{bible_names_inline}）说出口的台词，不允许只挑金句/关键句；若同一角色台词很多，也要逐条列入并在正文中写回。可在不改变主干信息的前提下轻微口语化、压缩赘字，但人物态度、信息点和情绪锋芒不能丢。【硬性】key_lines 不得包含测验员/围观者/旁白等非人物谱角色的台词——这些台词可以写进 full_script_text 的对白行，但绝不能进入 key_lines，否则后续分镜会因 characters 字段无法承载这些角色而陷入死循环。没有人物谱角色台词时，才从原文里挑出 3~8 条绝不能丢的关键台词。
- `key_plot_points`：列出本集【绝不能丢】的关键剧情点 3~8 条——核心事件、关键反转、信息揭示、关系变化。每条都必须在 `full_script_text` 里真的发生。
- 这两个清单是后续分镜台的"必须保留项"，分镜会逐条校验它们是否仍在镜头里。清单越准，成片越不会丢戏。

【单集戏剧契约】（先想清楚再落笔，避免压缩后只剩事件、没有方向）：
- `dramatic_question`：用一句话写出本集观众心里追问的那个问题（例：他能否在不暴露底牌的情况下赢得资格？）。
- `protagonist_goal`：主角本集看得见、可完成的外在目标。
- `obstacle`：阻力 = 外部对手/规则 + 内部恐惧/执念。
- `stakes`：失败代价——输了会失去什么关系、尊严、目标或机会。

你必须同时输出两层内容：
A. `scene_outline`：场次级结构表，是制作层用来审戏和拆镜的骨架。
B. `full_script_text`：真正的剧本正文，必须是带场标、动作段、对白段的台本稿，而不是一大段总结。

`full_script_text` 必须采用以下剧本写法：
1. 使用场次标题，例如：`【场1】夜 / 旧仓库内`
2. 每场先写动作与场面调度，再写人物对白；动作段和对白段要分行，不要挤成一大段。场面调度必须包含本场人物初始站位/姿态，以及后续入场、离场、靠近、退开、转身等可见动作。
3. 对白用“角色名：台词”格式；必要时可写“角色名（情绪/状态）：台词”。
4. 只写戏剧动作、人物反应、对白、必要旁白；不要写镜头语言。
5. 每场都要有明确戏剧任务：进入、升级、冲突、转折、收束中的至少一种。
6. 每场结尾都要把一个新的动作状态、情绪状态、人物位置或信息状态交给下一场，保证可连续拆镜。
7. 正文必须像真正台本，不得写成“本场讲了什么”的总结句堆叠。

硬性规则（代码校验，违反会被退回）：
1. episode_no 必须作为顶层字段出现且等于 {episode['episode_no']}（不可省略，也不可写进任何嵌套对象里）。
2. title / logline / scene_outline / full_script_text / emotional_curve / ending_hook / source_basis 必填；
   dramatic_question / protagonist_goal / obstacle / stakes 必填（单集戏剧契约）；
   key_lines 至少 3 条、key_plot_points 至少 3 条；若源文里出现“人物谱角色名：台词”，这些台词必须全部进入 key_lines 且都必须能在 full_script_text 中找到（主干一致）；且 key_lines 中每条台词标注的说话人，必须与 full_script_text 中该句对白行的说话人一致（不能清单写某角色说、正文却由另一角色说）。
3. `scene_outline` 必须是 3~6 场的连续场次结构，scene_no 从 1 连续递增。
   【硬性·角色圣经】scene_outline[*].characters 只能填角色圣经里的角色名（{bible_names_inline}），不能填测验员/围观者/旁白等非人物谱角色；这些非主角在场人物请写进 summary 描述，不要放进 characters 数组。
4. full_script_text 必须是一篇连续故事正文，且必须带场次标题、动作段、对白段，不能写成 beat 列表、卡片列表、分镜表或镜头说明。正文里的「【场N】」场次标题数量必须与 scene_outline 的场次数完全一致（scene_outline 有几场，正文就写几个【场N】，按 scene_no 顺序一一对应），不要把多场并成一段，也不要额外多拆场。
5. full_script_text 不能是一大段梗概；必须像台本，至少拆成多场、多段、多行。
6. full_script_text 中禁止出现：拍01、拍1、拍 01、镜头、景别、运镜、首帧、尾帧、参考图、提示词、prompt。
7. 剧本开头必须尽快进入本集 hook：{episode['hook']}
8. 剧本结尾必须落到本集尾钩：{episode['cliffhanger']}
9. 人物姓名、关系、说话风格必须遵守角色圣经；台词要自然口语化，优先保留原著冲击力。
10. 信息密度服从目标时长 {episode['target_duration_s']}s：正文不能过度注水，但必须讲清因果链、情绪推进和关键转折。
11. source_basis 必须概括本集改编依据的原文信息，保留真实事件、对白、冲突或线索；不要空泛。

本集规划信息：
- 概要（只用于理解，不可替代原文）：{episode.get('synopsis') or ''}
- 上一集结尾：{prev_ending or '（本集为第一集）'}
- 本集目标时长：{episode['target_duration_s']} 秒

角色圣经（姓名、关系、说话风格必须遵守）：
{bible.model_dump_json()}

角色说话风格：
{speech_styles or '（无额外说话风格）'}

本集改编源文本：
{_render_screenplay_source(source_text)}

输出 JSON Schema：
{{"episode_no": {episode['episode_no']}, "mode": "full_script", "title": str, "logline": str, "script_format_note": "一句话说明正文采用的台本格式", "dramatic_question": "本集戏剧问题（一句话）", "protagonist_goal": "主角外在目标", "obstacle": "外部+内部阻力", "stakes": "失败代价", "key_lines": ["本集所有人物谱角色台词；无此类台词时列3~8条关键台词；均需在正文出现"], "key_plot_points": ["本集绝不能丢的关键剧情点/反转，3~8条"], "scene_outline": [{{"scene_no": int, "scene_heading": "场次标题，如「日 / 萧家测验广场」，不少于4字", "story_function": "本场戏剧功能：一句话说明这场在整集里推进/升级/转折/收束了什么，不少于6字，不要只写「高潮转折」这类四字标签", "characters": [str], "summary": "本场具体戏剧内容概括，不少于16字", "conflict": str, "turn": "本场交给下一场的状态变化（人物位置/情绪/信息），不少于4字", "source_basis": "本场改编依据的原文信息，不少于8字"}}], "full_script_text": str, "character_state_changes": [str], "emotional_curve": str, "ending_hook": str, "source_basis": str, "adaptation_direction": str, "opening": str, "development": str, "conflict": str, "climax": str}}"""
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
    script = await _run_with_agent_loop(
        "可拍剧本", "screenplay", prompt, EpisodeScreenplay,
        lambda s: validate_screenplay(s, bible, max(1, episode["target_duration_s"] // config.VIDEO_DURATION_MIN_S),
                                      episode_no=episode["episode_no"], source_text=source_text),
        loop=loop, temperature=0.7, max_tokens=65535,
        # episode_no/mode 是后端权威值（validator 也要求 episode_no 必须等于本集号），模型给的值不可信，
        # 直接确定性回填，避免再为这两个已知字段空转修复轮。
        prefill={"episode_no": episode["episode_no"], "mode": "full_script"})
    return script


def _storyboard_key_content_block(screenplay: EpisodeScreenplay) -> str:
    """把剧本台标记的"必保留清单 + 单集戏剧契约"渲染成分镜 prompt 区块。
    分镜台据此逐条落实关键台词/剧情点，validate_storyboard_preserves_key_content 再逐条校验是否仍在。"""
    key_lines = [ln.strip() for ln in (screenplay.key_lines or []) if ln and ln.strip()]
    key_points = [pt.strip() for pt in (screenplay.key_plot_points or []) if pt and pt.strip()]
    contract = [
        f"- 本集戏剧问题：{screenplay.dramatic_question}" if screenplay.dramatic_question else "",
        f"- 主角目标：{screenplay.protagonist_goal}" if screenplay.protagonist_goal else "",
        f"- 阻力：{screenplay.obstacle}" if screenplay.obstacle else "",
        f"- 失败代价：{screenplay.stakes}" if screenplay.stakes else "",
    ]
    contract_text = "\n".join(c for c in contract if c)
    lines_text = "\n".join(f"- {ln}" for ln in key_lines) or "（剧本未单列，请从完整剧本文本中提取关键对白）"
    points_text = "\n".join(f"- {pt}" for pt in key_points) or "（剧本未单列，请从完整剧本文本中提取关键剧情）"
    blocks = ["【本集必保留关键台词】（每条必须写进某镜的 dialogues 或 narration，代码逐条校验）：", lines_text,
              "", "【本集必保留关键剧情点】（每条必须在某镜的 action_desc 或声轨中体现，代码逐条校验）：", points_text]
    if contract_text:
        blocks = ["【单集戏剧契约】（指导取舍：服务它们的内容优先保留）：", contract_text, ""] + blocks
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
    detail_from = max(0, len(shots) - 2)
    for index, shot in enumerate(shots):
        # 只有最近两镜需要完整动作/尾帧来做物理承接；更早镜头仅保留防重复摘要。
        # 否则第 N 镜会重发 1..N-1 镜的全文，输入 token 呈二次增长。
        action = (shot.action_desc or "").strip()
        narration = (shot.narration or "").strip()
        dialogue_text = "｜".join(d.line for d in shot.dialogues if (d.line or "").strip())
        if index < detail_from:
            rows.append({
                "shot_no": shot.shot_no,
                "duration_s": shot.duration_s,
                "scene_setting": shot.scene_setting,
                "characters": shot.characters,
                "progress": action[:90],
                "soundtrack": (narration + ("｜" if narration and dialogue_text else "") + dialogue_text)[:90],
            })
            continue
        rows.append({
            "shot_no": shot.shot_no,
            "duration_s": shot.duration_s,
            "scene_setting": shot.scene_setting,
            "characters": shot.characters,
            "action_desc": shot.action_desc,
            "last_frame_desc": shot.last_frame_desc,
            "narration": shot.narration,
            "dialogues": [d.model_dump() for d in shot.dialogues],
            "transition": shot.transition,
            "continuity_from_prev": shot.continuity_from_prev,
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
        f"\n【分镜进度】已通过 {len(completed_shots)} 镜、累计 {used}s；整集不设时长上限。\n"
        f"- 本镜 duration_s 由你按内容判断为 {config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S}s 整数；"
        "选择能自然完成单一动作和口播的最短时长，超过 10 秒仍放不下才拆到后续镜头。\n"
        "- 继续按剧本推进，完整覆盖全部剧情和尾钩后才能设置 is_final=true。\n"
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
    # 须在 relieve_spoken_overflow 之前——未知角色剥离可能把人群台词转写进 narration。
    normalize_offbible_characters(board, bible)
    # ② 确定性卸载：把人群议论/哄笑类旁白降级为画面，先把单镜口播压回上限内，
    # 再按模型已选择的时长校验——避免"角色台词+人群旁白共 67 字超限"这类报错耗尽逐镜重试。
    relieve_spoken_overflow(board)
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
        errors.append(f"当前已到本集最大镜头数，第 {shot_no} 镜必须收束到尾钩并设置 is_final=true")

    # 反停留：本镜原文摘录与上一镜几乎逐字相同 = 停在同一段原文空耗（典型的"多镜演同一句话"）。
    if completed_shots:
        prev_src = (completed_shots[-1].source_excerpt or "").strip()
        cur_src = (draft.shot.source_excerpt or "").strip()
        if prev_src and cur_src and _too_similar(prev_src, cur_src):
            errors.append(
                f"第 {shot_no} 镜 source_excerpt 与上一镜几乎相同，说明本镜停留在同一段原文未推进剧情；"
                "请推进到完整剧本/原文的下一段，覆盖新的剧情进展，不要把同一情绪拆成多镜")

    target = episode["target_duration_s"]
    board = _normalized_candidate_board(episode["episode_no"], completed_shots, draft.shot, bible, target)
    current = board.shots[-1]
    partial_errors = validate_storyboard(board, bible, target)
    errors.extend(_filter_partial_storyboard_errors(partial_errors, current_index=len(completed_shots)))
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
    # 只有撞到最大镜头数（must_finish）、再无后续镜头可分担时才硬失败。
    episode_errors = (
        validate_storyboard_soundtrack(board, screenplay, target)
        + validate_storyboard_preserves_key_content(board, screenplay)
    )
    if episode_errors:
        if must_finish:
            errors.extend(episode_errors)
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
    first_rule = ("【本集是第一集】第 1 镜是全片开场建场镜：先交代世界观/主角处境/核心设定，再带出本集 hook。"
                  if is_first else f"第 1 镜要尽快进入本集 hook：{episode['hook']}。")
    scene_block = (chr(10).join(
        f"场{sc.scene_no}｜{sc.scene_heading}｜功能：{sc.story_function}｜摘要：{sc.summary}｜"
        f"冲突：{sc.conflict or '（无）'}｜转折：{sc.turn or '（无）'}"
        for sc in screenplay.scene_outline) if screenplay.scene_outline else "（未提供场次结构）")
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》规划【分镜大纲】。

你现在做的是全局节奏规划：把下方【完整剧本】一次性铺成有序的 N 条镜头节拍，确保整集剧情【从头到尾】被均匀覆盖。这一步只写"每镜推进什么剧情"，不写景别/运镜/首尾帧/台词原文——那些留给逐镜填充。

最重要的目标是节奏：后续会严格按这份大纲逐镜填充，所以——
- 每一条镜头都必须把剧情向前推进一步，禁止两条镜头停留在同一情绪、同一个动作或同一句原文上空耗时长（这是当前最大的问题：多镜挤在开场情绪上，后段剧情进不来）。
- N 条镜头必须覆盖整集的开端→发展→冲突→高潮→尾钩，篇幅按剧情权重分配，不要把大半镜头耗在开场。
- 最后一镜落到本集尾钩：{episode['cliffhanger']}。

【导演调度总则】大纲阶段就要规划“谁在画面里、谁如何进入/离开、人物位置如何变化”：
- 同一场景连续镜头之间，人物不能无动机地从无到有或从有到无；新人物出现必须有入场/上前/转身露出/从人群中走出等动作原因。
- 相邻 beat 要能形成动作接力：上一镜的尾部状态，必须能自然成为下一镜的起点；不要把每镜写成互不相干的剧情摘要。
- 如果某个角色只在台词里被提到但画面没拍到本人，不要把他规划成出场人物；让这类信息由旁白、屏幕/纸条或在场人物反应承载。

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
1. 镜头数 N 不设上限，按完整剧本逐个拆出所有必要镜头；shot_no 从 1 起连续递增。大纲阶段不输出 duration_s，逐镜阶段由模型按每镜动作与口播在 5~10 秒内判断具体时长。复杂动作、超长台词或不同节拍必须继续拆成相邻镜头，绝不能为了控制总时长合并或删减剧情；只有完整覆盖剧本并落到结尾钩子才可收束。
2. 每条只写一行 beat：本镜推进的剧情（谁做了什么 / 局势如何变化 / 与上一镜的区别），不少于 6 字。
3. 相邻两镜剧情必须不同、持续前进，严禁停留或复述同一节拍。
4. 上方"必保留关键台词/关键剧情点"清单里的每一条，都必须分配到某一镜的 covers，全集覆盖、不得遗漏（代码逐条校验）。同一时刻同一动作里同时发生的多件事（如"碑亮+测出三段+宣告低级+全场哄笑"本就是宣判这一拍），放进同一镜 covers 即可，不要硬拆成多镜空耗时长；但发生在不同时刻/不同人物/不同场景的事（如萧媚七段、萧薰儿九段、树荫对话）必须分到不同镜，每镜 covers 只写该镜能实际拍出来、说出来的部分；不要把"萧媚七段+萧薰儿九段+反衬萧炎失格"整段塞给只拍萧媚的一镜。
5. {first_rule}
6. 每条 scene_setting 写时间+地点短标签；同一连续空间必须保持同一个标签，不要因为人物走到门口/桌边/人群前就改标签。
7. beat 必须写清人物调度：若本镜新增人物，beat 中必须出现“走进/上前/转身露出/从人群中出来/被带入”等入画原因；若人物离开画面，必须写“退下/离开/走出/被遮挡/镜头留在另一人身上”等出画原因。

本集目标时长 {target}s。上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON（不要解释、不要 Markdown）：
{{"episode_no": {episode['episode_no']}, "shots": [{{"shot_no": int, "scene_setting": "时间+地点短标签", "beat": "本镜推进的剧情一句话", "covers": "本镜落实的关键台词/剧情点，可空"}}]}}"""
    log_provider_call(
        "storyboard_outline_prompt", config.MODEL_TEXT, "PROMPT_READY", None, 0,
        meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
              "target_duration_s": target, "shot_range": [min_shots, max_shots],
              "prompt_chars": len(prompt), "contract_version": "storyboard_outline_v1"})
    logged_downgrades: set[tuple[int, str]] = set()

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


def _render_storyboard_outline(outline: StoryboardOutline | None, current_shot_no: int) -> str:
    """把整集大纲渲染进逐镜 prompt，并标出"本镜"在大纲里的位置，让模型按计划推进、不越位也不停留。"""
    if not outline or not outline.shots:
        return ""
    total = len(outline.shots)
    rows = []
    for s in outline.shots:
        scene = f"｜{s.scene_setting}" if (s.scene_setting or "").strip() else ""
        covers = f"｜落实：{s.covers}" if (s.covers or "").strip() else ""
        mark = "  ← 本镜" if s.shot_no == current_shot_no else ""
        rows.append(f"第{s.shot_no}/{total}镜{scene}：{s.beat}{covers}{mark}")
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
    """把单镜不可完成的 covers 预拆成可由逐镜合同逐个生成的相邻节拍。

    口播超预算时会一次拆成足够多段，并保证每段都不超过最长 10 秒的口播上限；
    这样模型无需用非法的 ``shots`` 数组自行加镜。拆分后会重排 shot_no。
    """
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
    if not outside and not both_tracks and not over_budget:
        return False
    atoms = _atomize_claim(covers)
    if not atoms:
        return False
    if over_budget:
        chunks = _split_atoms_to_content_budget(
            atoms, config.MAX_SPOKEN_CHARS_PER_SHOT
        )
    else:
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
                beat=f"（自动拆分自第{shot_no}镜第{offset + 1}段）{chunk}",
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
    must_finish = False
    # 方案 C：当前镜大纲 covers 若"不可单镜完成"（依赖圣经外角色开口 或 同时要求角色开口+人群声），
    # 在调用 LLM 前自动拆成足够多段并插入相邻节拍，让本镜只落实当前段，避免逐镜修复打转。
    # 拆分后 outline.shots 变长，下方 expected_total / allow_finish 自动按新长度计算。
    _maybe_split_outline_covers(outline, shot_no, bible, max_shots)
    # 有大纲时由计划的镜头数决定收尾时机（执行完整份大纲，避免提前收尾把后段剧情挤掉）；
    # 无大纲时回退到基础镜头数下限。
    expected_total = len(outline.shots) if (outline and outline.shots) else min_shots
    allow_finish = shot_no >= max(min_shots if not (outline and outline.shots) else expected_total, 1)
    budget_block = _storyboard_progress_block(completed_shots)
    outline_block = _render_storyboard_outline(outline, shot_no)
    brief = _outline_brief(outline, shot_no)
    brief_block = ""
    if brief is not None:
        brief_block = (
            f"\n【本镜大纲任务】（第 {shot_no}/{expected_total} 镜，必须落实这一条、不要停留在前面已覆盖的剧情）：\n"
            f"- 推进：{brief.beat}\n"
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
        screenplay.full_script_text, context_hints, max_chars=6000)
    source_window = _relevant_text_windows(source_text, context_hints, max_chars=5000)
    feedback_block = ""
    if final_feedback:
        # ④ 软剧情点保护：临近收尾时把"未落实必保留内容"升级为最高优先级，并明确剧情点可用
        # action_desc 直接拍出来（不必逐字台词）——避免"萧薰儿上前安慰"这类无台词软剧情被整段略过。
        head = ("\n【收尾前必须补齐·最高优先级】本集即将收尾，但以下剧本必保留内容仍未落实；"
                "请【本镜或紧接的下一镜】优先把它们拍出来/念出来，未全部落实前不得设 is_final=true：\n"
                if allow_finish else
                "\n【本集仍有未落实的必保留内容】（整集校验：以下内容尚未出现在已通过镜头中）：\n")
        tail = ("\n落实方式：关键台词写进 dialogues 或 narration（人物开口 / 内心OS / 旁白）；"
                "关键剧情点【不必逐字台词】，可直接用 action_desc 把这件事拍出来"
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

与本镜任务相关的完整剧本节选（其余剧情已由上方场次结构和分镜大纲覆盖）：
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
{_render_completed_shots_context(completed_shots)}
{brief_block}{budget_block}{feedback_block}
当前镜头约束：
1. 只输出第 {shot_no} 镜，shot.shot_no 必须等于 {shot_no}。
2. 本集不设镜头数和总时长上限；当前按完整大纲推进到第 {shot_no}/{expected_total} 镜。本镜必须落实大纲第 {shot_no} 条、推进到新剧情，不得停留或复述已覆盖内容。{"只有剧情已完整落到尾钩时才可设置 is_final=true，否则必须继续生成" if allow_finish else "剧情尚未铺到计划收尾，is_final 必须为 false"}。
3. 从第 2 镜开始，必须明确承接上一镜的 last_frame_desc、动作结果、道具状态、人物位置、情绪或声轨信息；如果换场，要写清线索带入或时间跳转。
4. 如果 is_final=true，本镜必须落到本集尾钩：{episode['cliffhanger']}，并且整集必保留关键台词/剧情点都已经在已通过镜头或本镜中体现。
5. 如果 is_final=false，本镜结尾要留下清楚的动作/情绪/信息状态，供下一镜继续。
6. 人物入画/出画必须符合导演调度：characters 里新增的人物，必须在 action_desc、first_frame_desc 或 narration 中写清他/她从哪里来、如何进入画面或如何被镜头发现；上一镜在场但本镜不再可见的人物，必须有退开、离开、被遮挡、留在画外或换场的可见原因。禁止“上一镜没有，下一镜突然站在画面里/突然开口”。
7. 功能性路人合同：{extra_policy}。

拆分原则：
1. 按完整剧本的因果链继续往后拆，不能跳过中间关键事件，也不能重写已通过镜头已经覆盖的内容。
2. 每条 shot 都要推进剧情，且承接上一条的动作、情绪或信息状态。
3. scene_setting 只写时间+地点短标签，characters 只写实际出现在画面中、且入画原因已经交代清楚的角色。
4. 优先用台词+画面动作表达信息；必要内心OS放入 narration，并以“内心OS：……”或“内心：……”开头。
5. 每条 shot 都必须能追溯到完整剧本与原文依据，不要空泛扩写。
6. 第 1 镜处理：{'【本集是第一集】第 1 镜是全片开场建场镜，主任务是交代故事背景（世界观/主角处境/核心设定）为全片铺底，再自然带出本集 hook。' if int(episode.get('episode_no') or 0) == 1 else f"第 1 镜要尽快进入本集 hook：{episode['hook']}。"}
7. 最后 1 镜必须落到本集尾钩：{episode['cliffhanger']}。

{output_contract}

{preflight_contract}

本镜相关改编源文本节选（source_excerpt 必须从这里逐字摘录）：
{source_window}

角色圣经：{bible.model_dump_json()}
上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON Schema：
{{"episode_no": {episode['episode_no']}, "is_final": bool, "shot": {{"shot_no": {shot_no}, "duration_s": int, "shot_size": "远景|全景|中景|近景|特写", "camera_move": "固定|推近|拉远|横摇|跟随", "scene_setting": "短时间+地点标签", "characters": ["画面中实际可见的角色圣经姓名或合法功能性路人标签"], "action_desc": str, "first_frame_desc": "本镜开始的静止画面，25~50字，只写看得见的人物姿态/表情/手部/道具/光效", "last_frame_desc": "本镜结束的静止画面，25~50字，与首帧【同机位同场景同构图】，仅人物动作推进后的状态（不要换镜头/景别/场景）", "source_excerpt": "对应本镜头的小说原文逐字摘录，至少 {SOURCE_EXCERPT_MIN_CHARS} 字", "narration": "可空；用于保留内心OS、结尾悬念旁白、非角色圣经人物的人群声/议论声，建议≤{NARRATION_TARGET_CHARS} 字", "dialogues": [{{"speaker": "必须是本镜头 characters 中的可见角色名或功能性路人标签", "line": str, "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定"}}], "transition": "{transition_options}", "continuity_from_prev": bool}}}}"""
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
            "contract_version": "storyboard_sequential_shot_v5_functional_extras",
            "screenplay_mode": "full_script",
        })
    repair_output_contract = f"""只输出一个 JSON 根对象，根字段为 episode_no、is_final、shot。
shot 必须是单数对象，shot.shot_no 必须等于 {shot_no}；禁止输出 shots 数组，禁止附带下一镜。
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
    )
    _normalized_candidate_board(episode["episode_no"], completed_shots, draft.shot, bible, episode["target_duration_s"])
    return draft


# ---------- C2. 单集分镜脚本（基于完整剧本拆分） ----------

def _first_shot_rule(episode: dict) -> str:
    """第 1 镜的写作要求：常规集=直接进 hook；但【第一集第一镜】是全片开场，主要职责是交代故事背景
    （世界观/主角处境/基本设定），为后续剧情铺底，而不是急着推进情节或抛冲突。"""
    if int(episode.get("episode_no") or 0) == 1:
        return (
            f"23. 【第一集第一镜=全片开场建场镜，特殊规则，优先级最高】这一镜的主要任务是【交代故事背景】，"
            f"不是推进剧情、不是抛冲突反转：用画面+旁白把【世界观/时代设定/主角是谁、身处什么处境、基本关系或核心设定】"
            f"讲清楚，让没看过原著的观众迅速进入这个故事。\n"
            f"    - action_desc 写一个能代表本片世界观/主角日常处境的【建立性画面】（establishing shot），"
            f"人物动作克制、信息靠画面与旁白承载；不要在第一镜就让主角做剧烈动作或触发核心冲突。\n"
            f"    - 必须配 narration 旁白做背景交代（世界观/设定/主角身份处境），旁白先于任何台词；"
            f"shot_size 优先用远景/全景做开场建场，先把环境和主角位置交代清楚。\n"
            "    - 开场需要更长铺陈时，可为单一连续建场动作选择 5~10 秒；超过 10 秒或进入新节拍时拆成相邻建场镜，逐步完成环境建立与人物入场，"
            f"所以 action_desc/首尾帧请按\"远景缓慢推近、镜头从环境推向主角\"来写：首帧是交代环境的大远景，"
            f"尾帧镜头推近到主角、但仍是同一机位的连续推进，人物动作保持克制连贯。\n"
            f"    - 仍要包含本集 hook：{episode['hook']}，但以\"先立背景、再带出钩子\"的方式呈现，"
            f"不要为了 hook 牺牲掉背景交代。\n"
            f"    最后 1 个镜头必须呈现悬念钩：{episode['cliffhanger']}")
    return (f"23. 第 1 个镜头必须呈现本集 hook：{episode['hook']}\n"
            f"    最后 1 个镜头必须呈现悬念钩：{episode['cliffhanger']}")


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
2. 整集不设总时长上限，完整演绎剧本、因果链、情绪推进、关键台词和结尾钩子后才能结束。
3. 本集镜头数不设上限；复杂动作、长台词、不同时间或不同人物节拍必须拆成更多相邻镜头，不得合并删戏。
4. duration_s 必须由你根据【本镜单一连续动作的完成时间 + 台词/旁白的自然语速】自主判断，只能取整数 {duration_options} 秒。
   - 【选择原则】选择能完整、自然呈现本镜内容的最短时长：5~6s 用于简单动作/无口播或极短口播；7~8s 用于常规对话、情绪反应或有起承转合的连续动作；9~10s 只用于确实需要铺陈的连续动作或较长口播。禁止所有镜头机械取同一时长，也禁止无内容地拉长到 10s。
   - 【硬性·音画同步】口播预算随 duration_s 增长：{speech_budgets}。选择时长后，动作与口播必须都能在该时长内自然完成。
   - 【硬性·拆镜边界】不同时间、地点、主动作或剧情节拍必须拆镜；单一内容超过 10s 仍演不完/说不完，也必须拆成连续相邻镜头，不得用 10s 强塞多个节拍。
5. 关键：每条 shot 只表现【一个】连贯流畅的主动作（视频模型一镜到底拍这一件事），用一句话把它的"起势→过程→收势"和人物表情/反应写清楚（逗号分句多少不限，写细更好）。判定"多镜头快切"看的不是逗号数量，而是有没有出现切镜：严禁出现"切到/切至/镜头切/镜头转向/闪回/回忆画面/分屏/下一个镜头/→"这类词。
6. 单镜要像一个真实可拍的连续动作（例如"她攥紧衣角，肩膀微颤，眼泪无声砸落，嘴角弧度僵在半空"是一个动作，没问题；"她哭→镜头切到门口→闪回六年前"才是错误的多段快切）。画面负责动作和表情，声轨负责冲突、态度、内心和悬念，二者必须共同推进剧情。
7. 声轨纪律（重要）：分镜必须从【已确认完整剧本】保留角色对白、内心OS、旁白、人群嘲讽/恭维等可听见信息，不能把有声剧本压成纯画面卡。全集至少约 75% 镜头应有 dialogues 或 narration；对白冲突镜优先写 dialogues，内心OS和非角色圣经人物的人群声写入 narration。禁止空泛情绪词注水，每一句声轨都要提供新信息。
8. action_desc 目标 ≥{ACTION_DESC_MIN_CHARS} 字（不设上限）：写清这一个动作的主体姓名、动作起止、力度/速度、表情与道具反应；不要罗列多个镜头，不要写运镜术语（景别/运镜由独立字段给出）。
8b. 【关键·首尾帧=同一镜头的起止，决定 5~10s 视频是否自然】每条 shot 必须给出 first_frame_desc（本镜开始的静止画面）与 last_frame_desc（本镜结束的静止画面），它们是本镜视频的起点帧和终点帧：
   - 二者必须是【同一机位、同一场景、同一构图】下，这一个连贯动作的开始瞬间与结束瞬间：背景、镜头框取、人物在画面中的位置与形象保持一致，只有人物的姿态/表情/手部/道具状态随这一个动作自然推进。
   - 要能看出动作发生了变化（首尾不能写成完全相同的一句），但【绝不是换机位、换构图、换场景、换人物形象】——否则视频会在两帧之间出现不合常理的跳变/形变/瞬移（这是当前成片最严重的问题，务必避免）。
   - 正例（同机位、仅动作推进）：首帧「角色A手掌刚贴上石碑，神情平静，碑面无光」；尾帧「同一机位，角色A手掌仍贴在石碑上，碑面微微亮起，他眉头骤紧、掌心收力」。反例（错误，等于换了镜头）：首帧拍人脸特写、尾帧却拍远处大厅全景。
   - 各 25~50 字，只写画面里看得见的东西（人物姿态/表情/手部/关键道具/光效），同一场景、同一人物形象；不要写出旁白/字幕文字、不要写运镜。
8c. 【导演调度·人物不能凭空出现】每条 shot 都要像现场调度表一样写清人物在画面中的合理存在：
   - characters 里的每个角色，都必须在 action_desc 或 first_frame_desc 中有明确可见位置/姿态/动作；不能只出现在 dialogues 里。
   - 同场景连续镜头若新增角色，action_desc 必须写明“从门口走进/从人群中上前/被旁人推入/转身露出/镜头跟随他进入”等入画过程；不要让角色突然站在画面中央或突然开口。
   - 同场景连续镜头若某角色不再出现在 characters，上一镜 last_frame_desc 或本镜 action_desc 必须交代他退开、离开、被遮挡、留在画外，或本镜明确换到另一个人物反应；不要让角色凭空消失。
   - 台词发生前必须有可见听闻与反应：人物先看见/听见/意识到，再转身、表情变化、开口或行动；禁止无因果的突然动作。
9. source_excerpt 必填：每条 shot 必须带对应小说原文摘录，至少 {SOURCE_EXCERPT_MIN_CHARS} 字、不设上限，必须从下方"本集改编源文本"逐字摘录；可以截取最相关的连续段落，不要改写成摘要，不要写分镜解释。它会作为 Seedance prompt 的兜底参考。
10. 字数只校验下限，不校验上限；目标值仅作写作引导。优先保证戏剧质量与因果连贯，不要为凑数字牺牲剧情。
11. 信息密度靠"画面一个清晰动作 + 台词/内心OS承担冲突与信息（必要时一句短旁白补缝）"配合，而不是把多件事塞进同一个画面，也不是靠旁白硬讲剧情。禁止单纯场景氛围、人物呆立、重复上一镜内容。
12. narration 可为空，但以下内容必须优先保留在 narration：必要内心独白、结尾悬念旁白、非角色圣经人物的人群嘲讽/恭维/议论声、画面与角色开口都无法表达的隐藏因果。若写则务必简短（一句话、建议≤{NARRATION_TARGET_CHARS} 字），内心独白请以“内心OS：……”或“内心：……”开头。
12a. 【口播优先·重要】单镜台词+旁白总字数受第 4 条按时长预算约束（最长 10s 也不得超过 {config.MAX_SPOKEN_CHARS_PER_SHOT} 字）。若本镜已有接近对应时长上限的角色台词或关键长台词，就【不要】再把人群嘲讽/议论/哄笑/惊呼这类环境群像声塞进 narration 占用口播——请改写进 action_desc 当画面群像（如“周围人群哄笑、交头接耳地议论”）。环境群像声写进画面同样能被观众看到，不必占用念白时间；本镜旁白可留空或只留一句最关键的内心OS/尾钩。
12b. 【声轨时序·重要】成片配音按“先旁白/内心、人物再开口”的听感顺序念：所以同一镜里 narration 是【铺垫情境/画外音/内心活动】，台词是人物【听到/看到后的反应】，二者必须前后承接、各讲各的信息，绝不能内容重复或自相矛盾（错例：narration 写“敌暗我明，谁在操控这一切”，台词又说“敌暗我明，这家伙是谁”——重复撞车）。只有全知视角的结尾悬念钩旁白（“可他不知道……/殊不知……/然而……”）才是念在台词之后的收尾。若本镜逻辑是“人物先反应、再补一句旁白”，就把旁白写成这种结尾钩句式，否则默认旁白先于台词。
13. 角色名必须准确：characters 不能为空；有姓名、重要或跨镜持续的角色只能使用角色圣经准确姓名：{character_names}。{extra_policy}。characters 只写本镜头实际可见/在场的人物；幕后发消息者、纸条落款、屏幕昵称、AI 软件名不算出场角色，除非镜头真的拍到他本人。不要创造具体姓名，不要把姓名改成外号/称谓，不要用"无角色"。
14. action_desc 必须显式写出本镜头主要角色的准确姓名，不能只写"他/她/男人/女人/镜头/纸张"；每个动作节点都优先围绕人物表情、动作、道具反应和剧情后果展开。
15. dialogues 只写人物实际开口台词，dialogues[*].speaker 必须在本镜头 characters 中；不要把纸条文字、屏幕文字、手机通知、内心独白或旁白写成 speaker="旁白"，这些内容放到 narration 或 action_desc。
16. 单句台词可按人物语气灵活长短，但【单镜】台词+旁白总口播必须满足第 4 条上限；关键长台词请拆成连续相邻镜头分段说。emotion 只能取：{'|'.join(sorted(EMOTIONS))}。台词从原著提炼为口语化短句，但优先保留关键细节和人物说话风格：{speech_styles or '（无额外说话风格）'}。
17. scene_setting 只是连续性标签，不是渲染重点，建议 {SCENE_SETTING_MAX_CHARS} 字以内（不强制），只写"时间，地点"；能不写氛围就不写，禁止堆砌薄雾、灯光、杂物、墙面、天气等环境描写。镜头主要渲染故事情节和人物。
18. shot_size 只能取：{'|'.join(sorted(SHOT_SIZES))}；camera_move 只能取：{'|'.join(sorted(CAMERA_MOVES))}；transition 只能取：{'|'.join(sorted(TRANSITIONS))}。
19. 同一 scene_setting 的镜头必须连续排列，不能被其他场景打断；同一场景的 scene_setting 必须逐字相同，格式建议："时间，地点"。
20. 连续 3 个镜头不得使用相同 shot_size；情绪高点优先用特写。
21. 相邻镜头必须有明确上下文接力：同场景连续镜头 continuity_from_prev=true，下一镜 action_desc 的开头必须承接上一镜结尾的动作、道具、屏幕内容或情绪；换时间/地点时 continuity_from_prev=false，且 narration 或 action_desc 必须写清转场原因/时间跳跃。
22. 转场设计：同场景连续镜只能用"硬切"；只要 scene_setting 与上一镜不同，就必须选择一个明确转场，禁止硬切。普通时空跳转优先"淡出淡入"；情绪/回忆延续优先"声音延续+叠化"；悬疑冲击用"闪黑/闪白"；动作追逐用"甩镜/遮挡转场"；有构图呼应时用"匹配剪辑"。换场前一镜的 last_frame_desc 必须带转场结尾（画面渐暗、闪白、遮挡、甩镜、叠化余韵等），换场镜的 first_frame_desc 必须是新时间/新地点的建立画面。
{_first_shot_rule(episode)}
24. 特效/光效服从剧情，不要每个镜头都堆特效：日常对话与一般场景写实克制（不要满屏光效、能量、粒子、光环）；只有情绪高潮或力量爆发的镜头才用强特效，且特效不得遮挡人物面部表情。把"发生了什么/人物什么反应"写清楚，而不是靠光效撑场面。
25. 动作必须符合现实物理与人体运动规律：一个镜头里人物的位置、姿态、所持道具是连续变化的，不要瞬移、不要凭空出现/消失人物或道具、不要让手与道具脱节或穿模。复杂手势（如结印、捏取小物）改写成更稳的简单动作（掌心托物、握拳、伸手按住）。"""


def _storyboard_preflight_contract(episode: dict) -> str:
    target = episode["target_duration_s"]
    min_shots, max_shots = storyboard_shot_count_range(target)
    hints = "、".join(TRANSITION_HINTS[:12])
    return f"""首轮输出前必须逐镜预检（这些就是代码校验器的具体判定条件，不要等返工）：
1. 镜头数和整集总时长不设上限；每条 duration_s 必须按内容判断为 {config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S} 的整数，并选择能完成单一动作与口播的最短时长。口播预算按 `duration_s × {config.SPOKEN_CHARS_PER_5_SECONDS}/5` 向下取整；超过 10s 容量或包含不同节拍时必须新增相邻镜头拆分，直到完整演绎剧本。
2. 第 1 镜 continuity_from_prev 必须为 false；第 2 镜开始逐条和上一镜比较 scene_setting。
3. 如果本镜 scene_setting 与上一镜完全相同：
   - continuity_from_prev 必须为 true；
   - transition 必须为"硬切"；
   - characters 至少保留上一镜的 1 个核心人物；
   - action_desc 开头必须承接上一镜结尾的人物位置、道具/屏幕内容、动作或情绪，不能重新介绍场景或重复上一镜发现；
   - 如果本镜 characters 比上一镜多了某个角色，必须写清他/她“走进、上前、转身露出、从人群中出来、被带入”等入画过程；如果少了某个角色，必须写清他/她退开、离开、被遮挡、留在画外或换到另一人反应，禁止凭空出现/消失。
4. 如果本镜 scene_setting 与上一镜不同：
   - continuity_from_prev 必须为 false；
   - transition 必须选择明确的换场方式，绝不能用"硬切"；普通时空跳转优先"淡出淡入"，情绪/回忆延续优先"声音延续+叠化"，悬疑冲击用"闪黑/闪白"，动作追逐用"甩镜/遮挡转场"，有构图呼应时用"匹配剪辑"；
   - narration 或 action_desc 必须写清承接原因、时间跳跃或线索带入，建议出现：{hints} 等承接词；
   - 上一镜 last_frame_desc 必须带这个转场的结尾视觉，本镜 first_frame_desc 必须是新时间/新地点的建立画面；
   - 如果只是同一段连续动作里从房间走到门口/楼道/桌边/窗前，不要改 scene_setting，继续沿用上一镜主场景标签，把移动写进 action_desc。
5. scene_setting 是稳定短标签，不是镜头内容：同一连续时空统一写同一个"时间，主地点"，例如"当日，场景A"；不要在相邻镜头里改成"当日，场景A楼道外/桌前/门口"导致断链。
6. characters 只写本镜头实际可见/在场且已交代入画原因的人；允许第 13 条定义的功能性路人，具体姓名仍必须来自角色圣经。屏幕发信人、纸条落款、新闻里提到的人、AI 软件名不算 characters。
7. 每条 action_desc 必须显式写出 characters 中每个角色的角色圣经姓名或功能性路人标签、画面位置和动作反应，把这【一个】连贯动作写清（写细无妨，但不要出现切到/闪回/镜头转向/分屏等切镜词）；不要只写纸张、屏幕、镜头、场景自己在动。
8. 每条 shot 的 source_excerpt 必填，必须从本集原文逐字摘录至少 {SOURCE_EXCERPT_MIN_CHARS} 字（不设上限），作为 Seedance 生成兜底参考。
9. 声轨预检：若完整剧本对应段落有“角色名：台词”，本镜必须写 dialogues；若有“角色名（内心/OS）：台词”，本镜必须写 narration 并以“内心OS：……”或“内心：……”开头；若有人群嘲讽/恭维/旁白但说话者不在角色圣经，写入 narration 或 action_desc，不能丢掉。整集至少约 75% 镜头要有 dialogues 或 narration，避免纯画面哑剧。
10. first_frame_desc 与 last_frame_desc 必须同机位、同场景、同构图，只让人物动作从"开始"推进到"结束"；不要让首尾帧变成两个不同的镜头/景别/场景。
11. 人物调度预检：逐条核对上一镜 last_frame_desc、本镜 first_frame_desc、characters、action_desc。任何角色的入画、出画、开口、转身、靠近、退后都必须有可见动作链；如果一句话解释不清，就拆成相邻两镜，不要让视频模型自行脑补。

常见错误 → 正确写法（以下角色A/场景A仅为占位示例，请替换成本集真实角色与场景）：
- 错：上一镜"当日，场景A"，本镜"当日，场景A楼道外"，transition="硬切"，又没有解释。对：若是角色A从房内走到门口，scene_setting 仍写"当日，场景A"，continuity_from_prev=true，action_desc 写"角色A攥着上一镜的纸页走向门口……"。
- 错：纸条上出现一个落款名就把 characters 写成 ["该落款名"]。对：如果画面只拍到角色A和纸条，characters 写 ["角色A"]，纸条文字放 action_desc/narration。
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
                  character_anchors: list[str]) -> dict:
    anchors = "\n".join(character_anchors) or "（缺少角色锚点，应回到分镜补角色）"
    expectation = f"""你是 AI 视频质检员。对照预期检查这几帧画面（同一镜头的首/中/尾），输出 JSON。

预期画面：{action_desc}
预期场景：{scene_setting}
预期角色外观：
{anchors}

检查项（各 0~1 评分）：
1. character_match  角色外观与预期相符（发型/服装/年龄感）
2. action_match     画面内容与预期动作相符
3. clean_frame      无文字/水印/多余人物/肢体畸形

评分硬规则（评分会直接决定是否花费重抽，务必严格且稳定）：
- 只根据所给首/中/尾帧中可见的证据评分，不得因为“可能在未抽到的时刻发生”而臆测通过。
- action_match 是主项：核心动作、人物朝向、道具交互或动作结果未出现时必须 ≤0.4。
- character_match 是主项：主要角色错人、外观明显不符或跨帧换脸/换装时必须 ≤0.4。
- overall 不得高于 character_match 和 action_match 中的较低值；画面干净不能掩盖错人或错动作。
- issues 只写画面中可见、可定向修复的具体问题；达标时输出空数组，不要写泛化建议。

只输出 JSON：{{"character_match": float, "action_match": float, "clean_frame": float, "overall": float, "issues": [str]}}"""
    raw = await hiagent.vlm_check(
        frames_b64, expectation,
        call_meta={"initiator_label": "视频自动质检", "scene_setting": scene_setting})
    result = _parse_qa_result(raw, ["character_match", "action_match", "clean_frame"])
    character = _score_or_none(result.get("character_match"))
    action = _score_or_none(result.get("action_match"))
    if character is not None and action is not None:
        result["overall"] = round(min(float(result["overall"]), character, action), 3)
    return result
