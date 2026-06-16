"""LLM 流水线阶段：摘要 / 角色圣经 / 剧集规划 / 可拍剧本 / 分镜脚本。
每阶段 = prompt + Schema 校验 + 业务校验 + 修复回路（默认重试到 max_repair_attempts 次，失败抛 StageError——禁止兜底）。
校验类失败一律让模型继续修复；只有模型真正不可用（鉴权失败/参数 400/网关持续故障，
即 hiagent.ProviderError 透传）才立刻失败——重试同一 prompt 对这类错误无意义。
提示词正文与 docs/PROMPT_SPEC.md 保持同步，改动需先跑金样回归。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from typing import Callable

from pydantic import BaseModel

from app import config, hiagent
from app.db import get_setting, log_provider_call
from app.schemas import (Bible, CAMERA_MOVES, EMOTIONS, EpisodePlan, EpisodeScreenplay,
                         SHOT_SIZES, Shot, Storyboard, StoryboardOutline, TRANSITIONS,
                         extract_json, schema_errors)
from app.validators import (ACTION_DESC_MIN_CHARS, NARRATION_TARGET_CHARS,
                            NARRATION_TARGET_MIN_CHARS,
                            ORAL_TARGET_RANGE, SCENE_SETTING_MAX_CHARS,
                            SOURCE_EXCERPT_MIN_CHARS,
                            TRANSITION_HINTS, _too_similar, compact_durations_to_budget, estimate_speech_seconds,
                            normalize_action_desc, normalize_continuity, normalize_durations_for_speech,
                            normalize_episode_opening_shot, normalize_plan_chapters, normalize_transition_visuals,
                            storyboard_duration_limit, storyboard_shot_count_range,
                            validate_bible, validate_plan, validate_screenplay,
                            validate_storyboard,
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


# 连续 STALL_ROUNDS 轮问题集完全相同 = 模型已卡死，再重试只是烧钱/拖时间，提前收手。
STALL_ROUNDS = 3


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


async def _run_with_repair(stage: str, user_prompt: str, model_cls: type[BaseModel],
                           business_validate: Callable[[BaseModel], list[str]],
                           *, temperature: float = 0.7, max_tokens: int = 8192,
                           repair_user_prompt_limit: int | None = 3000,
                           fallback_to_last: bool = False) -> BaseModel:
    # 校验类失败持续让模型修复，直到通过或耗尽 max_repair_attempts。
    # hiagent.ProviderError（模型不可用）不在此捕获，直接透传——对这类错误重试无意义。
    # fallback_to_last=True：次数耗尽后以最后一次结构合法的输出为准（残余校验问题挂在
    # instance.residual_errors 上由调用方展示），而不是整体失败。
    max_attempts = max(int(get_setting("max_repair_attempts") or 8), 1)
    messages = [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": user_prompt}]
    draft = await hiagent.chat(messages, temperature=temperature, max_tokens=max_tokens)
    last_errors: list[str] = []
    error_history: list[list[str]] = []
    last_instance: BaseModel | None = None
    for attempt in range(max_attempts):  # 首次 + (max_attempts-1) 次修复
        try:
            obj = extract_json(draft)
        except ValueError as exc:
            last_errors = [str(exc)]
        else:
            instance, errors = schema_errors(model_cls, obj)
            if instance is not None:
                errors = business_validate(instance)
                if not errors:
                    return instance
                last_instance = instance  # 结构合法但有业务问题——兜底候选
            last_errors = errors
        error_history.append(list(last_errors))
        if attempt >= max_attempts - 1:
            break
        # 提前收手：最近 STALL_ROUNDS 轮问题完全相同 = 模型卡死，继续重试无意义。
        if len(error_history) >= STALL_ROUNDS and all(
                error_history[-1] == error_history[-k] for k in range(2, STALL_ROUNDS + 1)):
            log_provider_call(
                f"{stage}_stall", config.MODEL_TEXT, "REPAIR_STALLED", None, 0,
                meta={"stage": stage, "rounds": len(error_history), "errors": last_errors[:10]})
            break
        # 反复失败说明模型陷在同一处：升高温度跳出定式，并逐次加重措辞。
        repair_temp = 0.2 if attempt < 2 else min(0.2 + 0.15 * (attempt - 1), 0.8)
        emphasis = ("" if attempt < 2 else
                    f"\n\n【第 {attempt + 1} 次修复】历史记录中的问题你已多次未改正。请逐条对照硬性约束逐字修改，"
                    "确保全部满足，且不要引入新的违规。例如信息密度不足就必须补充原文细节、角色反应或关键线索，"
                    "相邻镜头断裂就必须承接上一镜尾状态，角色名错误就必须回到角色圣经和原文专名逐字修正。")
        original_task = user_prompt if repair_user_prompt_limit is None else user_prompt[:repair_user_prompt_limit]
        repair_prompt = (
            "你此前的输出未通过校验。下面是你历次输出的完整问题记录（按时间顺序，最后一轮即最近一次输出）。\n"
            "修复最近一轮的问题时，必须同时对照更早轮次的记录，确保曾犯过的错误不再复发：\n"
            + _render_error_history(error_history)
            + emphasis
            + "\n\n请修复后重新输出完整 JSON（不要解释，不要 Markdown）。"
            + "\n\n原任务要求：\n" + original_task
            + "\n\n你的最近一次输出：\n" + draft[:6000]
        )
        draft = await hiagent.chat(
            [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": repair_prompt}],
            temperature=repair_temp, max_tokens=max_tokens)
    if fallback_to_last and last_instance is not None:
        # 兜底：以最后一次结构合法的输出为准，残余问题透出给调用方/UI，不再整体失败。
        object.__setattr__(last_instance, "residual_errors", list(last_errors))
        log_provider_call(
            f"{stage}_fallback", config.MODEL_TEXT, "FALLBACK_LAST_OUTPUT", None, 0,
            meta={"stage": stage, "attempts": len(error_history), "residual_errors": last_errors[:10]})
        return last_instance
    raise StageError(stage, last_errors + [f"已修复 {len(error_history)} 次仍未通过校验，可点击重试，或在监制房调高「修复重试上限」"])


# ---------- 章节摘要（滚动摘要的原料） ----------

async def summarize_chapter(title: str, content: str) -> str:
    prompt = (
        f"用不超过 200 字概括本章剧情，保留：人物名、关键事件、冲突与悬念。只输出摘要正文。\n\n"
        f"章节《{title}》：\n{content[:8000]}"
    )
    text = await hiagent.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=512)
    return text.strip()[:300]


async def summarize_chapters_concurrent(chapters: list[dict], concurrency: int = 4) -> dict[int, str]:
    """对缺摘要的章节并发生成。返回 {idx: summary}。"""
    sem = asyncio.Semaphore(concurrency)

    async def one(ch: dict) -> tuple[int, str]:
        async with sem:
            return ch["idx"], await summarize_chapter(ch["title"] or f"第{ch['idx']}章", ch["content"])

    pending = [ch for ch in chapters if not ch.get("summary")]
    results = await asyncio.gather(*(one(ch) for ch in pending))
    return dict(results)


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


async def generate_bible(chapters: list[dict], rolling_summary: str = "",
                         feedback: str = "", previous_bible: dict | None = None) -> Bible:
    chapters_text = _render_bible_source(chapters)
    summary_part = f"\n后续章节滚动摘要：\n{rolling_summary}\n" if rolling_summary else ""
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

小说文本：
{chapters_text}{summary_part}{previous_part}{feedback_part}

输出 JSON Schema：
{{"characters": [{{"name": str, "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}]}}], "world": {{"era": str, "genre": str, "visual_style_canonical": str}}}}"""
    return await _run_with_repair("角色圣经", prompt, Bible, validate_bible, temperature=0.5)


# ---------- B. 剧集规划 ----------

PLAN_SOURCE_BUDGET_CHARS = 40000  # 分集 prompt 内原文预算：短篇可全量带，长篇按 start_chapter 起截断


def _render_plan_source(chapter_texts: list[tuple[int, str, str]] | None, start_chapter: int) -> str:
    """从 start_chapter 起，按字数预算拼接原文全文（优先让模型看真实情节，而非摘要）。"""
    if not chapter_texts:
        return ""
    blocks: list[str] = []
    used = 0
    included: list[int] = []
    for idx, title, content in chapter_texts:
        if idx < start_chapter:
            continue
        text = (content or "").strip()
        if not text:
            continue
        remain = PLAN_SOURCE_BUDGET_CHARS - used
        if remain <= 200:
            break
        clipped = text[:remain]
        blocks.append(f"【第{idx}章 {title}】\n{clipped}{'……（原文过长已截断）' if len(text) > remain else ''}")
        used += len(clipped)
        included.append(idx)
    if not blocks:
        return ""
    head = f"本批可用原文（第 {included[0]}~{included[-1]} 章，请优先依据原文真实情节/对白/反转来分集，摘要仅作全书索引）："
    return head + "\n" + "\n\n".join(blocks)


async def generate_plan_batch(chapter_summaries: list[tuple[int, str, str]], bible: Bible,
                              *, start_episode_no: int, start_chapter: int,
                              chapter_count: int, batch_size: int,
                              want_timeline: bool,
                              chapter_texts: list[tuple[int, str, str]] | None = None) -> EpisodePlan:
    """规划一批剧集：从第 start_chapter 章起、至多 batch_size 集。
    全书可能需要多批续写直至覆盖最后一章（避免长篇被截断/丢弃，见 _plan_task 循环）。
    chapter_texts 提供原文全文（按预算注入），分集依据原文而非仅摘要。
    """
    summaries_text = "\n".join(f"第{idx}章《{title}》：{summary}" for idx, title, summary in chapter_summaries)
    source_text = _render_plan_source(chapter_texts, start_chapter)
    timeline_req = (
        "key_timeline：用 10~20 条概括全书关键事件时间线（防伏笔丢失）。"
        if want_timeline else "key_timeline：本批留空数组 []。")
    last_batch_hint = (
        f"若剩余章节（第 {start_chapter}~{chapter_count} 章）能在本批 {batch_size} 集内讲完，"
        f"则最后一集的 source_chapters 必须包含第 {chapter_count} 章（全书收尾）。")
    prompt = f"""任务：将小说规划为竖屏漫剧剧集（每集 {config.EPISODE_TARGET_MIN_S}~{config.EPISODE_TARGET_MAX_S} 秒成片，默认约 {config.EPISODE_TARGET_DEFAULT_S} 秒）。全书共 {chapter_count} 章。

漫剧节奏铁律：
1. 每集开头 3 秒必须是钩子：冲突爆发点/悬念/反转，绝不从平铺直叙开场。
2. 每集只讲一个核心事件，有一个情绪高点。
3. 每集结尾留下一集的悬念钩。
4. 节奏宁快勿慢：删除原著中的过渡性内容，跳跃叙事靠旁白补缝。
5. 成本优先：不要把简单动作或场景交代拉长；一集宁可短而密，不要慢而水。

本批规划要求：
- 从第 {start_chapter} 章开始，规划接下来的【至多 {batch_size} 集】（剩余章节够多就规划满 {batch_size} 集）。
- episode_no 从 {start_episode_no} 开始连续递增。
- 第一集的 source_chapters 必须从第 {start_chapter} 章开始。
- source_chapters 是连续区间，剧情只能向前推进（不倒退、不跳章）。一集可覆盖多章（通常 1~3 章）；
  当某一章内容较多、足够拆成多集时，允许连续 2~3 集共同覆盖同一章（如第 5 章拆成两集：前半事件一集、后续余波一集），章节号可以重复，只要剧情顺序不回放即可。章节数少于想要的集数时，就这样拆章而不是硬凑。
- 不要超出第 {chapter_count} 章。{last_batch_hint}
- {timeline_req}

章节摘要（全书索引，用于把握整体走向）：
{summaries_text}

{source_text}

角色圣经：
{bible.model_dump_json()}

写作要求（提升分集质量，避免空洞重复）：
- 必须依据上方原文的真实情节、对白、动作与反转来切分，不要只复述摘要；synopsis 里要落到具体场景与细节，禁止用“两人发生争执/情感升温”这类空泛概括。
- 相邻集不得讲同一件事或重复同一情绪点；每集必须有独立的核心事件与新进展。
- hook、cliffhanger 取材于原文里最有张力的瞬间（具体动作/台词/反转），不要套模板。

输出 JSON Schema：
{{"key_timeline": [str], "episodes": [{{"episode_no": int, "title": str, "hook": str, "source_chapters": [int], "synopsis": str, "cliffhanger": str, "target_duration_s": int}}]}}
其中 hook=开头3秒画面+一句话；synopsis 80~150字；target_duration_s 只能取 {"/".join(str(x) for x in config.EPISODE_TARGET_CHOICES)}（10 的整数倍），按本集剧情密度与戏剧张力自定：信息密集、转折多或情绪高潮的集可放宽到更长时长（最高 {config.EPISODE_TARGET_MAX_S}s）以保证质量，简单过渡集可取下限 {config.EPISODE_TARGET_MIN_S}s，常规集约 {config.EPISODE_TARGET_DEFAULT_S}s。"""
    def _check(p: EpisodePlan) -> list[str]:
        # 生产级：先用确定性代码修正章节区间/编号/时长（LLM 不擅长记账），再校验残余问题。
        # 这样把“章节重叠/跳章/越界”这类最常见的返工源头直接消灭，几乎不再触发重试。
        normalize_plan_chapters(p.episodes, start_episode_no=start_episode_no,
                                start_chapter=start_chapter, chapter_count=chapter_count)
        return validate_plan(p.episodes, chapter_count,
                             start_episode_no=start_episode_no, start_chapter=start_chapter)

    return await _run_with_repair(
        "剧集规划", prompt, EpisodePlan, _check,
        temperature=0.7, max_tokens=12000, fallback_to_last=True)


# 剧本台源文预算：覆盖多章的集若超预算会被截断，旧实现固定 16000 字且无标记——
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
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》把小说改写成【完整剧本】。

你现在处于“剧本台”阶段，不是分镜阶段。你的职责是先写出一整集完整、连续、可阅读、可拆镜的【生产级剧本稿】。

剧本层职责：
1. 生成一整集完整故事，而不是拍卡列表或摘要提纲。
2. 保证剧情连贯、人物情绪连贯、因果关系连贯。
3. 输出能直接进入导演/分镜阶段的剧本稿，不要只写成长梗概。
4. 保留原文依据，并明确改编方向。
5. 输出适合后续拆成 4~6 个固定 10 秒视频段分镜的连续剧本。
6. 不在正文里输出“拍01/拍02/拍03”，不写景别、运镜、首尾帧、参考图、提示词。

【最重要·防丢失】本集会被压缩到约 {episode['target_duration_s']} 秒，压缩中最容易丢的就是关键台词和关键剧情。
所以你必须先做"必保留清单"，再写正文，并保证清单里的每一条都真实写进了正文：
- `key_lines`：从原文里挑出本集【绝不能丢】的关键台词 3~8 条——金句、决定性对白、情绪爆点、点破真相或抛出悬念的那几句。尽量保留原著说话风格与冲击力；每条都必须在 `full_script_text` 里实际出现（可口语化微调，但主干不能变）。
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
2. 每场先写动作与场面调度，再写人物对白；动作段和对白段要分行，不要挤成一大段。
3. 对白用“角色名：台词”格式；必要时可写“角色名（情绪/状态）：台词”。
4. 只写戏剧动作、人物反应、对白、必要旁白；不要写镜头语言。
5. 每场都要有明确戏剧任务：进入、升级、冲突、转折、收束中的至少一种。
6. 每场结尾都要把一个新的动作状态、情绪状态或信息状态交给下一场，保证可连续拆镜。
7. 正文必须像真正台本，不得写成“本场讲了什么”的总结句堆叠。

硬性规则（代码校验，违反会被退回）：
1. episode_no 必须等于 {episode['episode_no']}。
2. title / logline / scene_outline / full_script_text / emotional_curve / ending_hook / source_basis 必填；
   dramatic_question / protagonist_goal / obstacle / stakes 必填（单集戏剧契约）；
   key_lines 至少 3 条、key_plot_points 至少 3 条，且 key_lines 每条都必须能在 full_script_text 中找到（主干一致）。
3. `scene_outline` 必须是 3~6 场的连续场次结构，scene_no 从 1 连续递增。
4. full_script_text 必须是一篇连续故事正文，且必须带场次标题、动作段、对白段，不能写成 beat 列表、卡片列表、分镜表或镜头说明。
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
{{"episode_no": {episode['episode_no']}, "mode": "full_script", "title": str, "logline": str, "script_format_note": "一句话说明正文采用的台本格式", "dramatic_question": "本集戏剧问题（一句话）", "protagonist_goal": "主角外在目标", "obstacle": "外部+内部阻力", "stakes": "失败代价", "key_lines": ["本集绝不能丢的关键台词，3~8条，需在正文出现"], "key_plot_points": ["本集绝不能丢的关键剧情点/反转，3~8条"], "scene_outline": [{{"scene_no": int, "scene_heading": str, "story_function": str, "characters": [str], "summary": str, "conflict": str, "turn": str, "source_basis": str}}], "full_script_text": str, "character_state_changes": [str], "emotional_curve": str, "ending_hook": str, "source_basis": str, "adaptation_direction": str, "opening": str, "development": str, "conflict": str, "climax": str}}"""
    script = await _run_with_repair(
        "可拍剧本", prompt, EpisodeScreenplay,
        lambda s: validate_screenplay(s, bible, max(1, episode["target_duration_s"] // config.FIXED_VIDEO_DURATION_S),
                                      episode_no=episode["episode_no"]),
        temperature=0.7, max_tokens=10000, fallback_to_last=True)
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


def _render_completed_shots_context(shots: list[Shot]) -> str:
    if not shots:
        return "（尚无已通过镜头，本次是第 1 镜）"
    rows: list[dict] = []
    for shot in shots:
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


def _remaining_storyboard_seconds(target_duration_s: int, completed_shots: list[Shot]) -> int:
    """距离规划目标还剩多少秒。该值用于节奏提示，不是硬上限。"""
    used = sum(int(getattr(s, "duration_s", 0) or 0) for s in completed_shots)
    return int(target_duration_s) - used


def _next_shot_duration_budget(target_duration_s: int, completed_shots: list[Shot], *,
                               allow_finish: bool) -> int:
    """逐镜生成的当前镜建议上限。

    校验器允许整集适度超目标，但如果已进入可收尾区间，下一镜还继续长时长/长口播，
    就会出现"上一批镜头都过了，追加镜反复把总时长顶爆"。这里给模型和校验器一个当前镜闸门。
    """
    used = sum(int(getattr(s, "duration_s", 0) or 0) for s in completed_shots)
    limit = storyboard_duration_limit(target_duration_s)
    if not allow_finish and used < limit:
        return config.MAX_VIDEO_DURATION_S
    # 如果前序镜头已经超过硬上限，当前镜仍保底允许最短镜头收尾，但必须压到最短。
    remaining = max(config.MIN_VIDEO_DURATION_S, limit - used)
    return min(config.MAX_VIDEO_DURATION_S, remaining)


def _storyboard_budget_block(target_duration_s: int, completed_shots: list[Shot], *,
                             allow_finish: bool) -> str:
    used = sum(int(getattr(s, "duration_s", 0) or 0) for s in completed_shots)
    target_remaining = int(target_duration_s) - used
    limit = storyboard_duration_limit(target_duration_s)
    limit_remaining = limit - used
    max_this_shot = _next_shot_duration_budget(target_duration_s, completed_shots, allow_finish=allow_finish)
    pacing = (
        "- 剩余时长已不多，请尽快把剧情收束到尾钩并设置 is_final=true，不要为凑时长注水。\n"
        if allow_finish and limit_remaining <= config.MAX_VIDEO_DURATION_S * 2
        else "- 剩余时长仍较充裕，应继续推进剧情、把戏做足，不要过早收尾。\n"
    )
    return (
        f"\n【本集时长预算】规划目标 {int(target_duration_s)}s；允许硬上限 {limit}s；"
        f"已通过 {len(completed_shots)} 镜累计 {used}s；距离规划目标剩余 {target_remaining}s，距离硬上限剩余 {limit_remaining}s。\n"
        f"- 本镜 duration_s 建议不超过 {max_this_shot}s（单镜合法范围 {config.MIN_VIDEO_DURATION_S}~{config.MAX_VIDEO_DURATION_S}s）；"
        "若硬上限剩余很小，请把本镜压短、压缩台词或直接收尾，避免整集超时。\n"
        f"- 若本镜是收尾镜，台词+旁白必须能在 {max_this_shot}s 内念完；不要把解释、抒情和尾钩全塞进最后一镜。\n"
        f"{pacing}"
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
                                target_duration_s: int | None = None) -> Storyboard:
    board = Storyboard(episode_no=episode_no, shots=[*completed_shots, shot])
    normalize_continuity(board)
    normalize_durations_for_speech(board)
    normalize_episode_opening_shot(board)
    if target_duration_s is not None:
        compact_durations_to_budget(board, target_duration_s)
    normalize_transition_visuals(board)
    for s in board.shots:
        s.action_desc = normalize_action_desc(s.action_desc)
    return board


def _validate_storyboard_shot_draft(draft: StoryboardShotDraft, *, episode: dict, bible: Bible,
                                    screenplay: EpisodeScreenplay, completed_shots: list[Shot],
                                    shot_no: int, allow_finish: bool, must_finish: bool) -> list[str]:
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
    board = _normalized_candidate_board(episode["episode_no"], completed_shots, draft.shot, target)
    current = board.shots[-1]
    duration_limit = storyboard_duration_limit(target, board)
    compacted_prev_used = sum(int(s.duration_s or 0) for s in board.shots[:-1])
    max_this_shot = min(config.MAX_VIDEO_DURATION_S, max(config.MIN_VIDEO_DURATION_S, duration_limit - compacted_prev_used))
    if allow_finish and int(current.duration_s or 0) > max_this_shot:
        errors.append(
            f"第 {shot_no} 镜 duration_s={current.duration_s}s 超出当前剩余预算建议上限 {max_this_shot}s；"
            "请压短动作、精简台词/旁白，或只保留尾钩所需信息")
    speech_need = math.ceil(estimate_speech_seconds(current))
    if allow_finish and speech_need > max_this_shot:
        errors.append(
            f"第 {shot_no} 镜台词+旁白约需 {speech_need}s 才能念完，但当前剩余预算建议上限只有 {max_this_shot}s；"
            "请删掉解释性旁白、拆短长台词，只保留推动剧情和尾钩的核心句")
    partial_errors = validate_storyboard(board, bible, target, enforce_total_duration=False)
    errors.extend(_filter_partial_storyboard_errors(partial_errors, current_index=len(completed_shots)))

    if not (draft.is_final or must_finish):
        return errors

    # 收尾镜才跑整集兜底校验。必保留台词/剧情点、声轨这类"靠后续镜头分担"的缺口，
    # 在自愿收尾时不硬塞进单镜（那会让修复回路卡死），而是要求改判 is_final=false 继续补镜；
    # 只有撞到最大镜头数（must_finish）、再无后续镜头可分担时才硬失败。
    episode_errors = (
        validate_storyboard_soundtrack(board, screenplay, target)
        + validate_storyboard_preserves_key_content(board, screenplay)
    )
    # 整集时长上限：收尾镜必须让全集落在预算内（可由缩短本镜修复，故始终硬校验）。
    errors.extend(e for e in validate_storyboard(board, bible, target) if e.startswith("总时长"))
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
硬性约束：
1. 镜头数 N 取 {min_shots}~{max_shots}（按剧情密度自定）；shot_no 从 1 起连续递增。
2. 每条只写一行 beat：本镜推进的剧情（谁做了什么 / 局势如何变化 / 与上一镜的区别），不少于 6 字。
3. 相邻两镜剧情必须不同、持续前进，严禁停留或复述同一节拍。
4. 上方"必保留关键台词/关键剧情点"清单里的每一条，都必须分配到某一镜的 covers，全集覆盖、不得遗漏（代码逐条校验）。
5. {first_rule}
6. 每条 scene_setting 写时间+地点短标签。

本集目标时长 {target}s。上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON（不要解释、不要 Markdown）：
{{"episode_no": {episode['episode_no']}, "shots": [{{"shot_no": int, "scene_setting": "时间+地点短标签", "beat": "本镜推进的剧情一句话", "covers": "本镜落实的关键台词/剧情点，可空"}}]}}"""
    log_provider_call(
        "storyboard_outline_prompt", config.MODEL_TEXT, "PROMPT_READY", None, 0,
        meta={"episode_id": episode.get("id"), "episode_no": episode.get("episode_no"),
              "target_duration_s": target, "shot_range": [min_shots, max_shots],
              "prompt_chars": len(prompt), "contract_version": "storyboard_outline_v1"})
    outline = await _run_with_repair(
        "分镜大纲", prompt, StoryboardOutline,
        lambda o: validate_storyboard_outline(o, screenplay, target),
        temperature=0.6, max_tokens=4096, repair_user_prompt_limit=None, fallback_to_last=True)
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


async def generate_storyboard_next_shot(episode: dict, source_text: str, bible: Bible,
                                        prev_ending: str, screenplay: EpisodeScreenplay,
                                        completed_shots: list[Shot],
                                        final_feedback: list[str] | None = None,
                                        outline: StoryboardOutline | None = None) -> StoryboardShotDraft:
    """基于已通过镜头生成下一个镜头；业务校验通过才返回，调用方可立即落库给前端增量展示。"""
    if not (screenplay.full_script_text or "").strip():
        raise StageError("分镜脚本", ["旧版拍卡剧本已下线，请先重新生成完整剧本，再进入分镜台"])

    speech_styles = "；".join(f"{c.name}：{c.speech_style}" for c in bible.characters if c.speech_style)
    durations = sorted(config.ALLOWED_DURATIONS)
    output_contract = _storyboard_output_contract(episode, bible, durations, speech_styles)
    preflight_contract = _storyboard_preflight_contract(episode)
    transition_options = "|".join(sorted(TRANSITIONS))
    key_content_block = _storyboard_key_content_block(screenplay)
    min_shots, max_shots = storyboard_shot_count_range(episode["target_duration_s"])
    shot_no = len(completed_shots) + 1
    must_finish = shot_no >= max_shots
    # 有大纲时由计划的镜头数决定收尾时机（执行完整份大纲，避免提前收尾把后段剧情挤掉）；
    # 无大纲时回退到基础镜头数下限。
    expected_total = len(outline.shots) if (outline and outline.shots) else min_shots
    allow_finish = shot_no >= max(min_shots if not (outline and outline.shots) else expected_total, 1)
    remaining_duration = _remaining_storyboard_seconds(episode["target_duration_s"], completed_shots)
    duration_limit = storyboard_duration_limit(episode["target_duration_s"])
    used_duration = sum(int(getattr(s, "duration_s", 0) or 0) for s in completed_shots)
    limit_remaining = duration_limit - used_duration
    budget_block = _storyboard_budget_block(episode["target_duration_s"], completed_shots, allow_finish=allow_finish)
    outline_block = _render_storyboard_outline(outline, shot_no)
    brief = _outline_brief(outline, shot_no)
    brief_block = ""
    if brief is not None:
        brief_block = (
            f"\n【本镜大纲任务】（第 {shot_no}/{expected_total} 镜，必须落实这一条、不要停留在前面已覆盖的剧情）：\n"
            f"- 推进：{brief.beat}\n"
            + (f"- 落实关键内容：{brief.covers}\n" if (brief.covers or '').strip() else "")
            + (f"- 计划场景：{brief.scene_setting}\n" if (brief.scene_setting or '').strip() else "")
        )
    feedback_block = ""
    if final_feedback:
        feedback_block = (
            "\n【本集仍有未落实的必保留内容】（整集校验：以下内容尚未出现在已通过镜头中）：\n"
            + "\n".join(f"- {e}" for e in final_feedback[:10])
            + "\n请在从本镜开始的后续镜头里逐步覆盖它们，并确保收尾（is_final=true）前全部落实。\n"
        )

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

完整剧本文本：
{screenplay.full_script_text}

人物状态变化：
{chr(10).join(screenplay.character_state_changes) if screenplay.character_state_changes else '（无单列项）'}

情绪曲线：
{screenplay.emotional_curve}

结尾钩子：
{screenplay.ending_hook}

原文依据：
{screenplay.source_basis}

{key_content_block}
辅助结构：
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
2. 本集计划共 {expected_total} 镜，最多 {max_shots}；规划目标剩余 {remaining_duration}s，硬上限剩余 {limit_remaining}s（硬上限 {duration_limit}s）。本镜必须落实大纲第 {shot_no} 条、推进到新剧情，不得停留或复述已通过镜头已覆盖的内容。当前{"必须收束为最后一镜" if must_finish else ("可以在剧情已完整落到尾钩时设置 is_final=true，否则继续保持 false" if allow_finish else "不能设置为最后一镜，is_final 必须为 false（剧情尚未铺到计划的收尾镜）")}。
3. 从第 2 镜开始，必须明确承接上一镜的 last_frame_desc、动作结果、道具状态、情绪或声轨信息；如果换场，要写清线索带入或时间跳转。
4. 如果 is_final=true，本镜必须落到本集尾钩：{episode['cliffhanger']}，并且整集必保留关键台词/剧情点都已经在已通过镜头或本镜中体现。
5. 如果 is_final=false，本镜结尾要留下清楚的动作/情绪/信息状态，供下一镜继续。

拆分原则：
1. 按完整剧本的因果链继续往后拆，不能跳过中间关键事件，也不能重写已通过镜头已经覆盖的内容。
2. 每条 shot 都要推进剧情，且承接上一条的动作、情绪或信息状态。
3. scene_setting 只写时间+地点短标签，characters 只写实际出现在画面中的角色。
4. 优先用台词+画面动作表达信息；必要内心OS放入 narration，并以“内心OS：……”或“内心：……”开头。
5. 每条 shot 都必须能追溯到完整剧本与原文依据，不要空泛扩写。
6. 第 1 镜处理：{'【本集是第一集】第 1 镜是全片开场建场镜，主任务是交代故事背景（世界观/主角处境/核心设定）为全片铺底，再自然带出本集 hook。' if int(episode.get('episode_no') or 0) == 1 else f"第 1 镜要尽快进入本集 hook：{episode['hook']}。"}
7. 最后 1 镜必须落到本集尾钩：{episode['cliffhanger']}。

{output_contract}

{preflight_contract}

本集改编源文本：
{source_text}

角色圣经：{bible.model_dump_json()}
上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON Schema：
{{"episode_no": {episode['episode_no']}, "is_final": bool, "shot": {{"shot_no": {shot_no}, "duration_s": int, "shot_size": "远景|全景|中景|近景|特写", "camera_move": "固定|推近|拉远|横摇|跟随", "scene_setting": "短时间+地点标签", "characters": ["画面中实际可见/在场且属于角色圣经的准确姓名"], "action_desc": str, "first_frame_desc": "本镜开始的静止画面，25~50字，只写看得见的人物姿态/表情/手部/道具/光效", "last_frame_desc": "本镜结束的静止画面，25~50字，与首帧【同机位同场景同构图】，仅人物动作推进后的状态（不要换镜头/景别/场景）", "source_excerpt": "对应本镜头的小说原文逐字摘录，至少 {SOURCE_EXCERPT_MIN_CHARS} 字", "narration": "可空；用于保留内心OS、结尾悬念旁白、非角色圣经人物的人群声/议论声，≤{NARRATION_TARGET_CHARS} 字", "dialogues": [{{"speaker": "必须是本镜头 characters 中的角色名", "line": str, "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定"}}], "transition": "{transition_options}", "continuity_from_prev": bool}}}}"""
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    log_provider_call(
        "storyboard_shot_prompt", config.MODEL_TEXT, "PROMPT_READY", None, 0,
        meta={
            "episode_id": episode.get("id"),
            "episode_no": episode.get("episode_no"),
            "shot_no": shot_no,
            "completed_shots": len(completed_shots),
            "remaining_duration_s": remaining_duration,
            "expected_total": expected_total,
            "has_outline_brief": brief is not None,
            "source_chapters": episode.get("source_chapters"),
            "source_chars": len(source_text),
            "prompt_chars": len(prompt),
            "source_hash": source_hash,
            "prompt_hash": prompt_hash,
            "contract_version": "storyboard_sequential_shot_v2_outline",
            "screenplay_mode": "full_script",
        })
    draft = await _run_with_repair(
        "分镜脚本",
        prompt,
        StoryboardShotDraft,
        lambda d: _validate_storyboard_shot_draft(
            d,
            episode=episode,
            bible=bible,
            screenplay=screenplay,
            completed_shots=completed_shots,
            shot_no=shot_no,
            allow_finish=allow_finish,
            must_finish=must_finish,
        ),
        temperature=0.7,
        max_tokens=6000,
        repair_user_prompt_limit=None,
        fallback_to_last=True,
    )
    _normalized_candidate_board(episode["episode_no"], completed_shots, draft.shot, episode["target_duration_s"])
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
            f"    - 出片侧会把本镜强制为【远景 + 缓慢推近 + 较长时长（{config.ESTABLISHING_SHOT_DURATION_S}s）】，"
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
    return f"""硬性输出规范（以下规则由代码校验，违反会被退回重写；请首轮直接满足）：
1. episode_no 必须等于 {episode['episode_no']}；shots 按剧情顺序排列，shot_no 必须从 1 开始连续递增，不能跳号、重复或乱序。
2. 总时长 = 所有 duration_s 之和，目标 ≈ {target} 秒；自动校验允许到约 {int(target * 1.2)} 秒或口播刚需时长。不要为凑数把简单镜头硬撑长；台词较多的镜头必须给足念白时间，不要为压总时长而截短台词镜。
3. 本集基础镜头数为 {min_shots} 条 shot；如果必保留关键台词/内心OS/关键剧情点导致单镜口播超限，必须新增相邻镜头拆分承接，最多 {max_shots} 条。不要把多句长对白硬塞进一镜，也不要无理由少于基础镜头数。
4. duration_s 取 {config.MIN_VIDEO_DURATION_S}~{config.MAX_VIDEO_DURATION_S} 的整数（这是 Seedance 的 --dur 参数）。先按动作密度定一个基准，再按台词长度抬高，取较大者：
   - 动作密度基准：{config.MIN_VIDEO_DURATION_S}~6s 静态/简单动作（凝视、僵住、低头看、对话特写、单一表情变化，给长会让人物停滞干等）；7~9s 中等动作（走动、转身、拿放道具、一来一回对话）；10~{config.MAX_VIDEO_DURATION_S}s 复杂/强运动/连续多步动作或情绪爆发（打斗、奔跑、跌倒、剧烈挣扎）。
   - 【节奏硬约束】不要习惯性给 12~15s：无声轨或短旁白镜默认 5~8s；只有台词+旁白确实需要、或画面有强运动/高潮爆发，才允许 10~15s。前中段镜头若都给 13~15s，会挤掉尾钩镜头并导致整集超时。
   - 【硬性·音画同步】duration_s 必须 ≥ 本镜台词+旁白念完所需时间（中文约每 {config.SPEECH_CHARS_PER_SECOND} 字 1 秒，另加约 {int(config.SPEECH_LEAD_IN_S + config.SPEECH_TAIL_BUFFER_S)}s 开场留白与收势）。动作再简单，只要台词较长就要给足时长，否则动作演完了台词还没说完（如"趴下睡觉"只演 5s 但台词要 8s）会严重音画不同步。
   - 【硬性·口播上限】单镜台词+旁白总字数不得超过 {config.MAX_SPOKEN_CHARS_PER_SHOT} 字（{config.MAX_VIDEO_DURATION_S}s 也念不完）；超了就优先拆到新增/相邻镜头分担，其次再精简非关键口水话，绝不能一镜塞下念不完的台词。
5. 关键：每条 shot 只表现【一个】连贯流畅的主动作（视频模型一镜到底拍这一件事），用一句话把它的"起势→过程→收势"和人物表情/反应写清楚（逗号分句多少不限，写细更好）。判定"多镜头快切"看的不是逗号数量，而是有没有出现切镜：严禁出现"切到/切至/镜头切/镜头转向/闪回/回忆画面/分屏/下一个镜头/→"这类词。
6. 单镜要像一个真实可拍的连续动作（例如"她攥紧衣角，肩膀微颤，眼泪无声砸落，嘴角弧度僵在半空"是一个动作，没问题；"她哭→镜头切到门口→闪回六年前"才是错误的多段快切）。画面负责动作和表情，声轨负责冲突、态度、内心和悬念，二者必须共同推进剧情。
7. 声轨纪律（重要）：分镜必须从【已确认完整剧本】保留角色对白、内心OS、旁白、人群嘲讽/恭维等可听见信息，不能把有声剧本压成纯画面卡。全集至少约 75% 镜头应有 dialogues 或 narration；对白冲突镜优先写 dialogues，内心OS和非角色圣经人物的人群声写入 narration。禁止空泛情绪词注水，每一句声轨都要提供新信息。
8. action_desc 目标 ≥{ACTION_DESC_MIN_CHARS} 字（不设上限）：写清这一个动作的主体姓名、动作起止、力度/速度、表情与道具反应；不要罗列多个镜头，不要写运镜术语（景别/运镜由独立字段给出）。
8b. 【关键·首尾帧=同一镜头的起止，决定 10s 视频是否自然】每条 shot 必须给出 first_frame_desc（本镜开始的静止画面）与 last_frame_desc（本镜结束的静止画面），它们是这 10s 视频的起点帧和终点帧：
   - 二者必须是【同一机位、同一场景、同一构图】下，这一个连贯动作的开始瞬间与结束瞬间：背景、镜头框取、人物在画面中的位置与形象保持一致，只有人物的姿态/表情/手部/道具状态随这一个动作自然推进。
   - 要能看出动作发生了变化（首尾不能写成完全相同的一句），但【绝不是换机位、换构图、换场景、换人物形象】——否则 10s 视频会在两帧之间出现不合常理的跳变/形变/瞬移（这是当前成片最严重的问题，务必避免）。
   - 正例（同机位、仅动作推进）：首帧「角色A手掌刚贴上石碑，神情平静，碑面无光」；尾帧「同一机位，角色A手掌仍贴在石碑上，碑面微微亮起，他眉头骤紧、掌心收力」。反例（错误，等于换了镜头）：首帧拍人脸特写、尾帧却拍远处大厅全景。
   - 各 25~50 字，只写画面里看得见的东西（人物姿态/表情/手部/关键道具/光效），同一场景、同一人物形象；不要写出旁白/字幕文字、不要写运镜。
9. source_excerpt 必填：每条 shot 必须带对应小说原文摘录，至少 {SOURCE_EXCERPT_MIN_CHARS} 字、不设上限，必须从下方"本集改编源文本"逐字摘录；可以截取最相关的连续段落，不要改写成摘要，不要写分镜解释。它会作为 Seedance prompt 的兜底参考。
10. 字数只校验下限，不校验上限；目标值仅作写作引导。优先保证戏剧质量与因果连贯，不要为凑数字牺牲剧情。
11. 信息密度靠"画面一个清晰动作 + 台词/内心OS承担冲突与信息（必要时一句短旁白补缝）"配合，而不是把多件事塞进同一个画面，也不是靠旁白硬讲剧情。禁止单纯场景氛围、人物呆立、重复上一镜内容。
12. narration 可为空，但以下内容必须优先保留在 narration：必要内心独白、结尾悬念旁白、非角色圣经人物的人群嘲讽/恭维/议论声、画面与角色开口都无法表达的隐藏因果。若写则务必简短（一句话、≤{NARRATION_TARGET_CHARS} 字，10s 念得完），内心独白请以“内心OS：……”或“内心：……”开头。
12b. 【声轨时序·重要】成片配音按“先旁白/内心、人物再开口”的听感顺序念：所以同一镜里 narration 是【铺垫情境/画外音/内心活动】，台词是人物【听到/看到后的反应】，二者必须前后承接、各讲各的信息，绝不能内容重复或自相矛盾（错例：narration 写“敌暗我明，谁在操控这一切”，台词又说“敌暗我明，这家伙是谁”——重复撞车）。只有全知视角的结尾悬念钩旁白（“可他不知道……/殊不知……/然而……”）才是念在台词之后的收尾。若本镜逻辑是“人物先反应、再补一句旁白”，就把旁白写成这种结尾钩句式，否则默认旁白先于台词。
13. 角色名必须准确：characters 不能为空，只能使用角色圣经里的准确姓名：{character_names}。characters 只写本镜头画面中实际可见/实际在场的人物；幕后发消息者、纸条落款、屏幕昵称、AI 软件名不算出场角色，除非镜头真的拍到他本人。不要创造新名字，不要把姓名改成外号/称谓，不要用"无角色"。如果原文出现角色姓名，必须照抄原文和角色圣经中的姓名。
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
25. 动作必须符合现实物理与人体运动规律：一个镜头里人物的位置、姿态、所持道具是连续变化的，不要瞬移、不要凭空出现/消失道具、不要让手与道具脱节或穿模。复杂手势（如结印、捏取小物）改写成更稳的简单动作（掌心托物、握拳、伸手按住）。"""


def _storyboard_preflight_contract(episode: dict) -> str:
    target = episode["target_duration_s"]
    min_shots, max_shots = storyboard_shot_count_range(target)
    hints = "、".join(TRANSITION_HINTS[:12])
    return f"""首轮输出前必须逐镜预检（这些就是代码校验器的具体判定条件，不要等返工）：
1. 本集基础 {min_shots} 条 shot；若某镜单镜台词+旁白会超过 {config.MAX_SPOKEN_CHARS_PER_SHOT} 字，或必保留关键台词/剧情点塞不下，必须新增相邻镜头拆分承接，最多 {max_shots} 条。每条 duration_s 取 {config.MIN_VIDEO_DURATION_S}~{config.MAX_VIDEO_DURATION_S} 的整数：先按动作密度定基准（静态/简单→短，复杂/强运动→长），再按台词长度抬高（约每 {config.SPEECH_CHARS_PER_SECOND} 字 1 秒），取较大者。简单又没台词的镜头别给长时长；台词较多就给足时长。
2. 第 1 镜 continuity_from_prev 必须为 false；第 2 镜开始逐条和上一镜比较 scene_setting。
3. 如果本镜 scene_setting 与上一镜完全相同：
   - continuity_from_prev 必须为 true；
   - transition 必须为"硬切"；
   - characters 至少保留上一镜的 1 个核心人物；
   - action_desc 开头必须承接上一镜结尾的道具/屏幕内容/动作/情绪，不能重新介绍场景或重复上一镜发现。
4. 如果本镜 scene_setting 与上一镜不同：
   - continuity_from_prev 必须为 false；
   - transition 必须选择明确的换场方式，绝不能用"硬切"；普通时空跳转优先"淡出淡入"，情绪/回忆延续优先"声音延续+叠化"，悬疑冲击用"闪黑/闪白"，动作追逐用"甩镜/遮挡转场"，有构图呼应时用"匹配剪辑"；
   - narration 或 action_desc 必须写清承接原因、时间跳跃或线索带入，建议出现：{hints} 等承接词；
   - 上一镜 last_frame_desc 必须带这个转场的结尾视觉，本镜 first_frame_desc 必须是新时间/新地点的建立画面；
   - 如果只是同一段连续动作里从房间走到门口/楼道/桌边/窗前，不要改 scene_setting，继续沿用上一镜主场景标签，把移动写进 action_desc。
5. scene_setting 是稳定短标签，不是镜头内容：同一连续时空统一写同一个"时间，主地点"，例如"当日，场景A"；不要在相邻镜头里改成"当日，场景A楼道外/桌前/门口"导致断链。
6. characters 只写本镜头实际可见/在场的人；屏幕发信人、纸条落款、新闻里提到的人、AI 软件名不算 characters。它们只能写在 action_desc 或 narration。
7. 每条 action_desc 必须显式写出 characters 中的准确角色名，把这【一个】连贯动作写清（写细无妨，但不要出现切到/闪回/镜头转向/分屏等切镜词）；不要只写纸张、屏幕、镜头、场景自己在动。
8. 每条 shot 的 source_excerpt 必填，必须从本集原文逐字摘录至少 {SOURCE_EXCERPT_MIN_CHARS} 字（不设上限），作为 Seedance 生成兜底参考。
9. 声轨预检：若完整剧本对应段落有“角色名：台词”，本镜必须写 dialogues；若有“角色名（内心/OS）：台词”，本镜必须写 narration 并以“内心OS：……”或“内心：……”开头；若有人群嘲讽/恭维/旁白但说话者不在角色圣经，写入 narration 或 action_desc，不能丢掉。整集至少约 75% 镜头要有 dialogues 或 narration，避免纯画面哑剧。
10. first_frame_desc 与 last_frame_desc 必须同机位、同场景、同构图，只让人物动作从"开始"推进到"结束"；不要让首尾帧变成两个不同的镜头/景别/场景。

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
    for key in score_keys:
        score = _score_or_none(obj.get(key))
        if score is None:
            score = defaults.get(key)
        if score is None and raw:
            score = _extract_score_from_text(raw, key)
        if score is None:
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

重要：只审这一张静止帧是否符合它自己的画面描述；不要因为它没有表现整段动作的过程或后续/结尾画面而扣分（动作的展开由视频负责，关键帧只是这一刻的定格）。

本帧预期画面：{frame_desc}
预期场景：{scene_setting}
预期角色外观：
{anchors}{cont}

检查项（各 0~1 评分）：
1. expectation_match  画面是否符合【本帧预期画面】（人物姿态/表情/手部/道具状态、角色外观、场景对得上）
2. continuity         与参考图的画风、人物形象、光影是否连贯（无参考图则给 1）
3. clean_frame        无文字/水印/多余人物/肢体畸形/五官崩坏

只输出 JSON：{{"expectation_match": float, "continuity": float, "clean_frame": float, "overall": float, "issues": [str]}}"""
    frames = [image_b64] + ([prev_image_b64] if prev_image_b64 else [])
    raw = await hiagent.vlm_check(frames, expectation)
    defaults = {"continuity": 1.0} if not prev_image_b64 else None
    return _parse_qa_result(raw, ["expectation_match", "continuity", "clean_frame"], defaults=defaults)


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

只输出 JSON：{{"character_match": float, "action_match": float, "clean_frame": float, "overall": float, "issues": [str]}}"""
    raw = await hiagent.vlm_check(frames_b64, expectation)
    return _parse_qa_result(raw, ["character_match", "action_match", "clean_frame"])
