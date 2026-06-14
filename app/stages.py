"""LLM 流水线阶段：摘要 / 角色圣经 / 剧集规划 / 分镜脚本。
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
from app.schemas import (BeatChain, Bible, CAMERA_MOVES, EMOTIONS, EpisodePlan, SHOT_SIZES,
                         Storyboard, TIME_OF_DAY_ORDER, TRANSITIONS, extract_json, schema_errors)
from app.validators import (ACTION_DESC_MIN_CHARS, NARRATION_TARGET_CHARS,
                            NARRATION_TARGET_MIN_CHARS,
                            ORAL_TARGET_RANGE, SCENE_SETTING_MAX_CHARS,
                            SOURCE_EXCERPT_MIN_CHARS,
                            TRANSITION_HINTS, beat_scene_label, normalize_plan_chapters,
                            validate_beat_chain, validate_bible, validate_plan,
                            validate_storyboard, validate_storyboard_against_beats)

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


def _render_bible_source(chapters: list[dict], budget: int = BIBLE_SOURCE_BUDGET_CHARS) -> str:
    """Render as much source text as possible for the character bible.

    The old prompt only read the first 5 chapters, which made late-arriving
    important characters easy to miss in 6+ chapter imports.
    """
    blocks: list[str] = []
    used = 0
    for ch in chapters:
        title = ch.get("title") or f"第{ch.get('idx', '?')}章"
        content = (ch.get("content") or "").strip()
        if not content:
            continue
        remain = budget - used
        if remain <= 200:
            break
        clipped = content[:remain]
        suffix = "……（原文过长已截断）" if len(content) > remain else ""
        blocks.append(f"【{title}】\n{clipped}{suffix}")
        used += len(clipped)
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
3. visual_style_canonical：25~40 字的全局画风串，包含 美术风格/光线/色调，适配竖屏漫剧，必须依据本书题材定制。
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
其中 hook=开头3秒画面+一句话；synopsis 80~150字；target_duration_s 只能取 40/50/60，常规集优先取 {config.EPISODE_TARGET_DEFAULT_S}。"""
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


# ---------- C1. 节拍链（分镜的戏剧骨架） ----------

async def generate_beat_chain(episode: dict, source_text: str, bible: Bible) -> BeatChain:
    """把本集剧情压缩成 N 拍因果链（N=集时长÷10，与固定 10s 视频段一一对应）。
    紧凑的本质：每 10 秒都有一次局势变化（turn），而不是每 10 秒塞满字。"""
    expected_beats = max(1, episode["target_duration_s"] // config.FIXED_VIDEO_DURATION_S)
    names = "、".join(c.name for c in bible.characters) or "（角色圣经为空）"
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》设计节拍链——把本集剧情压缩成正好 {expected_beats} 个 10 秒节拍。这是分镜的戏剧骨架，只写戏剧结构，不写镜头语言。

节拍链规则（代码校验，违反将被退回）：
1. 正好 {expected_beats} 拍，beat_no 从 1 连续递增。
2. 每拍 = 一次局势变化：
   - event：谁做了什么（至少 8 字，一句话，必须用角色圣经准确姓名：{names}）
   - turn：这件事改变了什么局势/揭示了什么新信息（至少 4 字）。两拍的 turn 不得是同一信息的重复表述。
   - carry：留给下一拍的悬念或未完成动作（至少 4 字）
3. 因果链：第 i+1 拍的 event 必须由第 i 拍的 carry 直接触发。禁止平行罗列事件，禁止把同一事件拆成两拍重复讲。
4. beat_type 分配：第 1 拍必须「钩子」，呈现：{episode['hook']}
   最后一拍必须「尾钩」，呈现：{episode['cliffhanger']}
   中间至少 1 拍「反转」或「高潮」；不允许连续两拍「铺垫」。
5. 时间线性（代码校验单调）：day_offset 从 0 开始（0=本集第一天），time_of_day 只能取 {list(TIME_OF_DAY_ORDER)}；时间只能向前，禁止闪回——前史/背景用第 1 拍的 event 或 turn 一句话带过，绝不回放过去场景。
6. location：至少 2 字的主地点标签，尽量简短（如"出租屋"），同一连续时空逐字同一写法；characters 只写该拍画面中实际在场的人（屏幕消息发送者/纸条落款不算在场）。
7. 砍掉原著一切过渡内容：节拍间允许跳时间，跳跃由 day_offset/time_of_day 体现，宁快勿慢。

本集概要：{episode['synopsis']}

本集改编源文本：
{source_text[:12000]}

角色圣经（只用于姓名与关系，不要复述）：
{bible.model_dump_json()}

输出 JSON Schema：
{{"beats": [{{"beat_no": int, "day_offset": int, "time_of_day": str, "location": str, "characters": [str], "event": str, "turn": str, "carry": str, "beat_type": "钩子|铺垫|升级|反转|高潮|尾钩"}}]}}"""
    return await _run_with_repair(
        "节拍链", prompt, BeatChain,
        lambda c: validate_beat_chain(c, bible, expected_beats),
        temperature=0.7, max_tokens=6000, fallback_to_last=True)


def _render_beat_table(chain: BeatChain) -> str:
    rows = []
    for b in chain.beats:
        rows.append(
            f"第{b.beat_no}拍 ｜ 场景标签（scene_setting 必须逐字使用）：{beat_scene_label(b)} ｜ "
            f"在场角色：{'、'.join(b.characters)} ｜ 类型：{b.beat_type}\n"
            f"  事件：{b.event}\n  局势变化：{b.turn}\n  结尾钩子：{b.carry}")
    return "\n".join(rows)


# ---------- C2. 单集分镜脚本（对拍展开） ----------

def _storyboard_output_contract(episode: dict, bible: Bible, durations: list[int],
                                speech_styles: str) -> str:
    target = episode["target_duration_s"]
    expected_shots = max(1, math.ceil(target / config.FIXED_VIDEO_DURATION_S))
    character_names = "、".join(c.name for c in bible.characters) or "（角色圣经为空）"
    return f"""硬性输出规范（以下规则由代码校验，违反会被退回重写；请首轮直接满足）：
1. episode_no 必须等于 {episode['episode_no']}；shots 按剧情顺序排列，shot_no 必须从 1 开始连续递增，不能跳号、重复或乱序。
2. 总时长 = 所有 duration_s 之和，必须在 {target}±10% 秒内；不要只接近镜头数量，要实际相加检查。
3. 每条 shot 是一个固定 10s 的视频段，本集必须正好 {expected_shots} 条 shot；不要输出 5/6/7/8/9s。
4. duration_s 必须等于 {config.FIXED_VIDEO_DURATION_S}，这是最终 Seedance 视频生成参数 --dur 10。
5. 关键：每条 shot 只表现【一个】连贯流畅的主动作（视频模型一镜到底拍这一件事），用一句话把它的"起势→过程→收势"和人物表情/反应写清楚（逗号分句多少不限，写细更好）。判定"多镜头快切"看的不是逗号数量，而是有没有出现切镜：严禁出现"切到/切至/镜头切/镜头转向/闪回/回忆画面/分屏/下一个镜头/→"这类词。
6. 单镜要像一个真实可拍的连续动作（例如"她攥紧衣角，肩膀微颤，眼泪无声砸落，嘴角弧度僵在半空"是一个动作，没问题；"她哭→镜头切到门口→闪回六年前"才是错误的多段快切）。剧情推进主要靠 narration 旁白承担，画面只需把这一个动作演好。
7. 叙事方式（重要·本片以台词+画面为主，少用旁白）：剧情优先用【人物台词】和【可见的画面动作/表情】来表达。narration 旁白【默认留空】，绝大多数镜头不要写旁白；只有当某条关键信息既无法用画面表现、又无法让人物说出口时（如较大时间跳跃、必要的内心独白、画面与台词都带不出的隐藏因果），才写【一句尽量短】的旁白，可不写就不写——预计全集只有 0~2 个镜头需要旁白。台词与旁白都要句句提供新信息，禁止空泛情绪词注水。
8. action_desc 目标 ≥{ACTION_DESC_MIN_CHARS} 字（不设上限）：写清这一个动作的主体姓名、动作起止、力度/速度、表情与道具反应；不要罗列多个镜头，不要写运镜术语（景别/运镜由独立字段给出）。
8b. 【关键·首尾帧=同一镜头的起止，决定 10s 视频是否自然】每条 shot 必须给出 first_frame_desc（本镜开始的静止画面）与 last_frame_desc（本镜结束的静止画面），它们是这 10s 视频的起点帧和终点帧：
   - 二者必须是【同一机位、同一场景、同一构图】下，这一个连贯动作的开始瞬间与结束瞬间：背景、镜头框取、人物在画面中的位置与形象保持一致，只有人物的姿态/表情/手部/道具状态随这一个动作自然推进。
   - 要能看出动作发生了变化（首尾不能写成完全相同的一句），但【绝不是换机位、换构图、换场景、换人物形象】——否则 10s 视频会在两帧之间出现不合常理的跳变/形变/瞬移（这是当前成片最严重的问题，务必避免）。
   - 正例（同机位、仅动作推进）：首帧「萧炎手掌刚贴上测验石碑，神情平静，碑面无光」；尾帧「同一机位，萧炎手掌仍贴在石碑上，碑面微微亮起，他眉头骤紧、掌心收力」。反例（错误，等于换了镜头）：首帧拍人脸特写、尾帧却拍远处大殿全景。
   - 各 25~50 字，只写画面里看得见的东西（人物姿态/表情/手部/关键道具/光效），同一场景、同一人物形象；不要写出旁白/字幕文字、不要写运镜。
9. source_excerpt 必填：每条 shot 必须带对应小说原文摘录，至少 {SOURCE_EXCERPT_MIN_CHARS} 字、不设上限，必须从下方"本集改编源文本"逐字摘录；可以截取最相关的连续段落，不要改写成摘要，不要写分镜解释。它会作为 Seedance prompt 的兜底参考。
10. 字数只校验下限，不校验上限；目标值仅作写作引导。优先保证戏剧质量与因果连贯，不要为凑数字牺牲剧情。
11. 信息密度靠"画面一个清晰动作 + 台词承担冲突与信息（必要时一句短旁白补缝）"配合，而不是把多件事塞进同一个画面，也不是靠旁白硬讲剧情。禁止单纯场景氛围、人物呆立、重复上一镜内容。
12. narration 旁白为【选填】，可以是空字符串 ""。若写则务必简短（一句话、≤{NARRATION_TARGET_CHARS} 字，10s 念得完，超了会被退回）：只承担画面与台词都无法表达的关键信息（因果/时间跳跃/内心动机/隐藏信息），不要复述画面、不要每镜都写。镜头不靠旁白时，必须靠台词或清晰的画面动作把剧情带出来，不要纯氛围空镜、人物呆立。
13. 角色名必须准确：characters 不能为空，只能使用角色圣经里的准确姓名：{character_names}。characters 只写本镜头画面中实际可见/实际在场的人物；幕后发消息者、纸条落款、屏幕昵称、AI 软件名不算出场角色，除非镜头真的拍到他本人。不要创造新名字，不要把姓名改成外号/称谓，不要用"无角色"。如果原文出现角色姓名，必须照抄原文和角色圣经中的姓名。
14. action_desc 必须显式写出本镜头主要角色的准确姓名，不能只写"他/她/男人/女人/镜头/纸张"；每个动作节点都优先围绕人物表情、动作、道具反应和剧情后果展开。
15. dialogues 只写人物实际开口台词，dialogues[*].speaker 必须在本镜头 characters 中；不要把纸条文字、屏幕文字、手机通知、内心独白或旁白写成 speaker="旁白"，这些内容放到 narration 或 action_desc。
16. 台词 line 不设字数上限；emotion 只能取：{'|'.join(sorted(EMOTIONS))}。台词从原著提炼为口语化短句，但优先保留关键细节和人物说话风格：{speech_styles or '（无额外说话风格）'}。
17. scene_setting 只是连续性标签，不是渲染重点，建议 {SCENE_SETTING_MAX_CHARS} 字以内（不强制），只写"时间，地点"；能不写氛围就不写，禁止堆砌薄雾、灯光、杂物、墙面、天气等环境描写。镜头主要渲染故事情节和人物。
18. shot_size 只能取：{'|'.join(sorted(SHOT_SIZES))}；camera_move 只能取：{'|'.join(sorted(CAMERA_MOVES))}；transition 只能取：{'|'.join(sorted(TRANSITIONS))}。
19. 同一 scene_setting 的镜头必须连续排列，不能被其他场景打断；同一场景的 scene_setting 必须逐字相同，格式建议："时间，地点"。
20. 连续 3 个镜头不得使用相同 shot_size；情绪高点优先用特写。
21. 相邻镜头必须有明确上下文接力：同场景连续镜头 continuity_from_prev=true，下一镜 action_desc 的开头必须承接上一镜结尾的动作、道具、屏幕内容或情绪；换时间/地点时 continuity_from_prev=false，且 narration 或 action_desc 必须写清转场原因/时间跳跃。
22. 转场设计：同场景连续镜只能用"硬切"；只要 scene_setting 与上一镜不同，就必须选择一个明确转场，禁止硬切。普通时空跳转优先"淡出淡入"；情绪/回忆延续优先"声音延续+叠化"；悬疑冲击用"闪黑/闪白"；动作追逐用"甩镜/遮挡转场"；有构图呼应时用"匹配剪辑"。换场前一镜的 last_frame_desc 必须带转场结尾（画面渐暗、闪白、遮挡、甩镜、叠化余韵等），换场镜的 first_frame_desc 必须是新时间/新地点的建立画面。
23. 第 1 个镜头必须呈现本集 hook：{episode['hook']}
    最后 1 个镜头必须呈现悬念钩：{episode['cliffhanger']}
24. 特效/光效服从剧情，不要每个镜头都堆特效：日常对话与一般场景写实克制（不要满屏光效、能量、粒子、光环）；只有情绪高潮或力量爆发的镜头才用强特效，且特效不得遮挡人物面部表情。把"发生了什么/人物什么反应"写清楚，而不是靠光效撑场面。
25. 动作必须符合现实物理与人体运动规律：一个镜头里人物的位置、姿态、所持道具是连续变化的，不要瞬移、不要凭空出现/消失道具、不要让手与道具脱节或穿模。复杂手势（如结印、捏取小物）改写成更稳的简单动作（掌心托物、握拳、伸手按住）。"""


def _storyboard_preflight_contract(episode: dict) -> str:
    target = episode["target_duration_s"]
    expected_shots = max(1, math.ceil(target / config.FIXED_VIDEO_DURATION_S))
    hints = "、".join(TRANSITION_HINTS[:12])
    return f"""首轮输出前必须逐镜预检（这些就是代码校验器的具体判定条件，不要等返工）：
1. 本集必须正好 {expected_shots} 条 shot，每条 duration_s=10，总时长={target}s；不要输出 5/6/7/8/9s，也不要多/少镜头。
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
5. scene_setting 是稳定短标签，不是镜头内容：同一连续时空统一写同一个"时间，主地点"，例如"当日，出租屋"；不要在相邻镜头里改成"当日，出租屋楼道外/桌前/门口"导致断链。
6. characters 只写本镜头实际可见/在场的人；屏幕发信人、纸条落款、新闻里提到的人、AI 软件名不算 characters。它们只能写在 action_desc 或 narration。
7. 每条 action_desc 必须显式写出 characters 中的准确角色名，把这【一个】连贯动作写清（写细无妨，但不要出现切到/闪回/镜头转向/分屏等切镜词）；不要只写纸张、屏幕、镜头、场景自己在动。
8. 每条 shot 的 source_excerpt 必填，必须从本集原文逐字摘录至少 {SOURCE_EXCERPT_MIN_CHARS} 字（不设上限），作为 Seedance 生成兜底参考。
9. narration 旁白【选填、默认留空】：本片以台词和画面动作叙事，旁白只在画面与台词都无法表达关键信息时才写一句短旁白（≤{NARRATION_TARGET_CHARS} 字）。有两人及以上在场、或有对话/质问/冲突的镜头，必须写出 dialogues 台词；dialogues[*].speaker 必须是本镜 characters 里的角色名，不能写"旁白"。
10. first_frame_desc 与 last_frame_desc 必须同机位、同场景、同构图，只让人物动作从"开始"推进到"结束"；不要让首尾帧变成两个不同的镜头/景别/场景。

常见错误 → 正确写法：
- 错：上一镜"当日，出租屋"，本镜"当日，出租屋楼道外"，transition="硬切"，又没有解释。对：若是王浩从房内走到门口，scene_setting 仍写"当日，出租屋"，continuity_from_prev=true，action_desc 写"王浩攥着上一镜的纸页走向门口……"。
- 错：纸条上出现"未署名作者"就把 characters 写成 ["未署名作者"]。对：如果画面只拍到王浩和纸条，characters 写 ["王浩"]，纸条文字放 action_desc/narration。
- 错：下一镜重新说"出租屋昏暗、桌上有电脑"。对：下一镜直接从上一镜结尾继续，写"王浩仍盯着刚弹出的新闻推送，手指停在屏幕上，随后抬头望向门口，最后攥紧纸页。"。"""


async def generate_storyboard(episode: dict, source_text: str, bible: Bible,
                              prev_ending: str = "") -> Storyboard:
    # 两段式：C1 先把剧情压成节拍链（戏剧骨架），C2 逐拍展开成 10s 视频段。
    # 紧凑与连贯由 C1 保证（因果链+线性时间+反转覆盖），C2 只做"视觉翻译"，不再让模型平铺切原文。
    chain = await generate_beat_chain(episode, source_text, bible)
    beat_table = _render_beat_table(chain)
    speech_styles = "；".join(f"{c.name}：{c.speech_style}" for c in bible.characters if c.speech_style)
    durations = sorted(config.ALLOWED_DURATIONS)
    output_contract = _storyboard_output_contract(episode, bible, durations, speech_styles)
    preflight_contract = _storyboard_preflight_contract(episode)
    transition_options = "|".join(sorted(TRANSITIONS))
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》编写分镜脚本——按下方节拍表逐拍展开，第 i 镜实现第 i 拍。

本集节拍表（戏剧骨架，已通过结构校验，禁止增删拍、合并拍或调整顺序）：
{beat_table}

对拍展开规则（代码校验）：
A. 第 i 镜必须完整实现第 i 拍：画面呈现"事件"，让观众看懂"局势变化"，镜头结尾落在"结尾钩子"上——下一镜从这个钩子直接继续。
B. scene_setting 必须逐字使用节拍表给定的场景标签，不要自己改写时间地点。
C. characters 必须包含该拍的全部在场角色（可按画面需要增补圣经内角色，不可遗漏）。
D. 局势变化优先用【台词 + 可见画面动作/表情】表达，把本拍"局势变化(turn)+结尾钩子(carry)"靠人物说的话和做的事带出来。narration 旁白默认留空，只在画面与台词都无法传达关键信息时（较大时间跳跃、必要内心独白、隐藏因果）才写一句短旁白；禁止复述画面、禁止堆人物前史（前史只允许必要时一句话带过）。

{output_contract}

{preflight_contract}

专名锁定（只在脑内执行，不要输出到 JSON）：
- 角色圣经姓名只能逐字使用 {', '.join(c.name for c in bible.characters) or '（角色圣经为空）'}；原文中的地名、书名、软件名、屏幕/纸条文字、人名必须逐字照抄，不要猜新名字、改字、换同音字或把普通称谓升级成新角色。
- 专名出现在纸条、屏幕、新闻或旁白里，不等于它就是本镜头 characters；characters 只放实际可见/在场的人。
- 如果原文用"我/他/她"，必须结合角色圣经和上下文还原为准确角色名；还原不了就用动作主体的普通称谓，不要编姓名。

创作要求：
- 【叙事主力=台词+画面，少用旁白】能用人物台词说清、用画面动作/表情演清的，就不要写旁白。narration 默认留空，仅在万不得已（画面+台词都带不出的关键因果/时间跳跃/内心独白）时写一句短旁白，可不写就不写；预计全集只有 0~2 个镜头需要旁白。
- 【代入感·关键】把原文里最有冲击力的【关键台词/对白】尽量写进 dialogues，保留原著原话与语感；让观众靠人物对话和画面就能入戏，而不是听旁白干讲剧情梗概。不要把一切抽象成“他很愤怒/局势升级”这类干巴巴的总结。
- 台词要多、要密：凡是有两个及以上角色在场、或有对话/质问/告白/冲突的镜头，必须写出人物台词（dialogues），用口语化短句承担冲突与信息。全集台词整体偏少时优先补台词，而不是补旁白。
- 特效/光效服从剧情：日常对话和一般场景克制写实，不要每个镜头都堆光效/能量/光环；只有情绪高潮或力量爆发的镜头才用强特效，且不得遮挡人物面部表情。
- 动作要符合现实物理与人体常识：单镜只演一个连续动作，人物位置/姿态/道具连续变化，不要瞬移、穿模、道具凭空出现或消失；复杂手势改成简单稳定的动作。
- 场景描述能忽略就忽略：只保留最短时间地点标签；不要让薄雾、灯光、街道、杂物成为镜头主角。每个视频段的主角必须是人物、人物动作、人物反应和故事线索；场景只能服务于人物正在做什么、发现什么、失去什么、决定什么。
- 每个镜头输出前完成自检：shot_no 连续、duration_s 全部为 10、characters 非空且姓名准确、action_desc 出现准确角色名、source_excerpt 已从原文逐字摘录、scene_setting 足够短、narration 留空或一句短旁白（不超 {NARRATION_TARGET_CHARS} 字，多数镜头应留空）、首尾帧同机位同场景仅动作推进、action_desc 是一个连贯主动作（无切到/闪回/分屏等切镜词）、台词 speaker 在本镜头 characters 中且不能是旁白、与上一镜有动作/道具/情绪/信息承接。

镜头连贯铁律（成片是否连贯取决于此，逐条遵守）：
- 整集成片由各镜拼接而成，上下镜必须自然衔接：每镜的开头要承接上一镜的结尾状态（人物位置、动作、情绪、道具）；上下镜的因果优先靠台词与画面动作衔接，必要时才用一句短旁白补缝，避免观感上句不接下句。
- 同一场景内，continuity_from_prev=true，动作必须严格承接：上一镜头 action_desc 的结束状态，就是本镜头动作的起始状态（如上一镜"拔剑指向黑影"，本镜应从持剑指向的姿态继续，而不是另起炉灶）。
- 视频生成会为每个场景起始镜（含第1镜、换场镜）预生成首图+尾图；同场景连续镜只预生成尾图，并用上一镜尾图作为本镜首帧。
- 下一镜不要重新介绍同一场景，不要把上一镜已经完成的发现/动作重新讲一遍；必须推进到"因此发生了什么"。
- 如果必须跨时间或跨地点，transition 必须选择一个明确换场，禁止"硬切"；普通时空跳转用"淡出淡入"，情绪/回忆延续用"声音延续+叠化"，悬疑冲击用"闪黑/闪白"，动作追逐用"甩镜/遮挡转场"，构图呼应用"匹配剪辑"。continuity_from_prev=false，并在 narration 或 action_desc 写清"次日/几小时后/与此同时/他带着某线索来到某处"这类承接语；换场镜会使用自己的首图开启新场景。
- 每个场景的第一个镜头（continuity_from_prev=false）优先用远景或全景交代环境，再切近。
- 切换场景时 transition 表示"从上一镜进入本镜"的方式；同场景内一律"硬切"。换场前一镜的 last_frame_desc 要写出对应结尾视觉（渐暗、闪白、遮挡、甩镜模糊、叠化余韵等），换场镜的 first_frame_desc 要落到新时间/新地点的建立画面。
- 角色不得凭空出现：某角色若在场景中段才登场，须在 action_desc 中写明入场方式（推门而入/从暗处走出）。

本集改编源文本：
{source_text}

分镜改编依据：只以以上原文全文、hook、悬念钩、角色圣经和上一集结尾为准；episode.synopsis 仅用于前端展示，禁止作为分镜剧情依据。
角色圣经：{bible.model_dump_json()}
上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON Schema：
{{"episode_no": {episode['episode_no']}, "shots": [{{"shot_no": int, "duration_s": int, "shot_size": "远景|全景|中景|近景|特写", "camera_move": "固定|推近|拉远|横摇|跟随", "scene_setting": "短时间+地点标签", "characters": ["画面中实际可见/在场且属于角色圣经的准确姓名"], "action_desc": str, "first_frame_desc": "本镜开始的静止画面，25~50字，只写看得见的人物姿态/表情/手部/道具/光效", "last_frame_desc": "本镜结束的静止画面，25~50字，与首帧【同机位同场景同构图】，仅人物动作推进后的状态（不要换镜头/景别/场景）", "source_excerpt": "对应本镜头的小说原文逐字摘录，至少 {SOURCE_EXCERPT_MIN_CHARS} 字", "narration": "选填，默认空字符串\\"\\"；如写则一句短旁白（≤{NARRATION_TARGET_CHARS} 字，仅在画面与台词都无法表达关键信息时才写）", "dialogues": [{{"speaker": "必须是本镜头 characters 中的角色名", "line": str, "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定"}}], "transition": "{transition_options}", "continuity_from_prev": bool}}]}}"""
    synopsis = (episode.get("synopsis") or "").strip()
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    log_provider_call(
        "storyboard_prompt", config.MODEL_TEXT, "PROMPT_READY", None, 0,
        meta={
            "episode_id": episode.get("id"),
            "episode_no": episode.get("episode_no"),
            "source_chapters": episode.get("source_chapters"),
            "source_chars": len(source_text),
            "prompt_chars": len(prompt),
            "source_hash": source_hash,
            "prompt_hash": prompt_hash,
            "contract_version": "storyboard_beat_chain_v12_dialogue_first_physics",
            "narration_required": False,
            "narration_policy": "optional_rare",
            "beat_count": len(chain.beats),
            "fixed_video_duration_s": config.FIXED_VIDEO_DURATION_S,
            "text_upper_bound_enforced": False,
            "oral_target_range": list(ORAL_TARGET_RANGE),
            "scene_setting_max_chars": SCENE_SETTING_MAX_CHARS,
            "characters_required": True,
            "dialogue_speaker_narrator_allowed": False,
            "shot_to_shot_handoff_required": True,
            "first_prompt_preflight_rules": True,
            "source_exact_in_prompt": source_text in prompt,
            "source_tail_in_prompt": source_text[-200:] in prompt if source_text else False,
            "synopsis_content_in_prompt": bool(synopsis and synopsis in prompt),
        })
    board = await _run_with_repair(
        "分镜脚本", prompt, Storyboard,
        lambda b: validate_storyboard_against_beats(b, bible, episode["target_duration_s"], chain),
        temperature=0.7, max_tokens=16384, repair_user_prompt_limit=None, fallback_to_last=True)
    # 兜底残余问题（节拍链 + 分镜脚本）合并后挂在 board 上，由 _storyboard_task 透出到 UI
    residual = [f"节拍链：{e}" for e in getattr(chain, "residual_errors", []) or []]
    residual += list(getattr(board, "residual_errors", []) or [])
    if residual:
        object.__setattr__(board, "residual_errors", residual)
    return board


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
