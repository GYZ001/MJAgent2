"""LLM 流水线阶段：摘要 / 角色圣经 / 剧集规划 / 分镜脚本。
每阶段 = prompt + Schema 校验 + 业务校验 + 修复回路（最多 2 次，失败抛 StageError——禁止兜底）。
提示词正文与 docs/PROMPT_SPEC.md 保持同步，改动需先跑金样回归。
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable

from pydantic import BaseModel

from app import hiagent
from app.schemas import (Bible, EpisodePlan, Storyboard, extract_json, schema_errors)
from app.validators import validate_bible, validate_plan, validate_storyboard

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


async def _run_with_repair(stage: str, user_prompt: str, model_cls: type[BaseModel],
                           business_validate: Callable[[BaseModel], list[str]],
                           *, temperature: float = 0.7, max_tokens: int = 8192) -> BaseModel:
    messages = [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": user_prompt}]
    draft = await hiagent.chat(messages, temperature=temperature, max_tokens=max_tokens)
    last_errors: list[str] = []
    for attempt in range(3):  # 首次 + 修复 2 次
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
            last_errors = errors
        if attempt >= 2:
            break
        repair_prompt = (
            "你上一次的输出未通过校验。请修复以下具体问题后重新输出完整 JSON（不要解释，不要 Markdown）：\n"
            + "\n".join(f"- {e}" for e in last_errors[:20])
            + "\n\n原任务要求：\n" + user_prompt[:3000]
            + "\n\n你的原输出：\n" + draft[:6000]
        )
        draft = await hiagent.chat(
            [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": repair_prompt}],
            temperature=0.2, max_tokens=max_tokens)
    raise StageError(stage, last_errors)


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

async def generate_bible(chapters: list[dict], rolling_summary: str = "") -> Bible:
    chapters_text = "\n\n".join(
        f"【{ch['title']}】\n{ch['content'][:6000]}" for ch in chapters[:5]
    )
    summary_part = f"\n后续章节滚动摘要：\n{rolling_summary}\n" if rolling_summary else ""
    prompt = f"""任务：从小说文本中提取角色圣经与世界观，用于后续 AI 视频生成的一致性控制。

要求：
1. 只收录出场 2 次以上或明显重要的角色，最多 8 个。
2. appearance_canonical 是该角色的"固定外观锚点串"：40~60 字，必须包含 性别年龄感/发型发色/服装款式与颜色/1 个标志性特征。只写视觉可见信息，不写性格。原著未描写的部分，按题材合理补全并保持内部一致。
3. visual_style_canonical：25~40 字的全局画风串，包含 美术风格/光线/色调，适配竖屏漫剧，必须依据本书题材定制。
4. speech_style 用于后续台词写作：句长习惯/口头禅/敬语习惯等，15~30 字。

小说文本：
{chapters_text}{summary_part}

输出 JSON Schema：
{{"characters": [{{"name": str, "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}]}}], "world": {{"era": str, "genre": str, "visual_style_canonical": str}}}}"""
    return await _run_with_repair("角色圣经", prompt, Bible, validate_bible, temperature=0.5)


# ---------- B. 剧集规划 ----------

async def generate_plan_batch(chapter_summaries: list[tuple[int, str, str]], bible: Bible,
                              *, start_episode_no: int, start_chapter: int,
                              chapter_count: int, batch_size: int,
                              want_timeline: bool) -> EpisodePlan:
    """规划一批剧集：从第 start_chapter 章起、至多 batch_size 集。
    全书可能需要多批续写直至覆盖最后一章（避免长篇被截断/丢弃，见 _plan_task 循环）。
    """
    summaries_text = "\n".join(f"第{idx}章《{title}》：{summary}" for idx, title, summary in chapter_summaries)
    timeline_req = (
        "key_timeline：用 10~20 条概括全书关键事件时间线（防伏笔丢失）。"
        if want_timeline else "key_timeline：本批留空数组 []。")
    last_batch_hint = (
        f"若剩余章节（第 {start_chapter}~{chapter_count} 章）能在本批 {batch_size} 集内讲完，"
        f"则最后一集的 source_chapters 必须包含第 {chapter_count} 章（全书收尾）。")
    prompt = f"""任务：将小说规划为竖屏漫剧剧集（每集 60~90 秒成片）。全书共 {chapter_count} 章。

漫剧节奏铁律：
1. 每集开头 3 秒必须是钩子：冲突爆发点/悬念/反转，绝不从平铺直叙开场。
2. 每集只讲一个核心事件，有一个情绪高点。
3. 每集结尾留下一集的悬念钩。
4. 节奏宁快勿慢：删除原著中的过渡性内容，跳跃叙事靠旁白补缝。

本批规划要求：
- 从第 {start_chapter} 章开始，规划接下来的【至多 {batch_size} 集】（剩余章节够多就规划满 {batch_size} 集）。
- episode_no 从 {start_episode_no} 开始连续递增。
- 第一集的 source_chapters 必须从第 {start_chapter} 章开始。
- 每集 source_chapters 连续、集间不重叠不跳章；一集可覆盖多章（通常 1~3 章），剧情紧凑。
- 不要超出第 {chapter_count} 章。{last_batch_hint}
- {timeline_req}

章节摘要：
{summaries_text}

角色圣经：
{bible.model_dump_json()}

输出 JSON Schema：
{{"key_timeline": [str], "episodes": [{{"episode_no": int, "title": str, "hook": str, "source_chapters": [int], "synopsis": str, "cliffhanger": str, "target_duration_s": int}}]}}
其中 hook=开头3秒画面+一句话；synopsis 80~150字；target_duration_s 取 60~90。"""
    return await _run_with_repair(
        "剧集规划", prompt, EpisodePlan,
        lambda p: validate_plan(p.episodes, chapter_count,
                                start_episode_no=start_episode_no, start_chapter=start_chapter),
        temperature=0.7, max_tokens=12000)


# ---------- C. 单集分镜脚本 ----------

async def generate_storyboard(episode: dict, source_text: str, bible: Bible,
                              prev_ending: str = "") -> Storyboard:
    from app import config
    speech_styles = "；".join(f"{c.name}：{c.speech_style}" for c in bible.characters if c.speech_style)
    durations = sorted(config.ALLOWED_DURATIONS)
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》编写分镜脚本。

硬性约束（违反将被退回）：
1. 总时长 = 各镜头时长之和，必须在 {episode['target_duration_s']}±10% 秒内。
2. 镜头时长只能取整数秒：{durations}。
3. 语速预算：每镜头旁白字数+台词字数 ≤ 时长×5，≥ 时长×3。纯画面镜头（无旁白无台词）需在 action_desc 中体现完整叙事动作。
4. characters 只能使用角色圣经中的角色名。台词 speaker 必须在该镜头 characters 中，或为"旁白"。
5. 每个镜头的 action_desc 只描述一个主要动作（一个主语+一个动词短语+情绪/神态），禁止"边A边B然后C"式复合动作。15~50 字。
6. 同一场景的镜头必须连续排列；scene_setting 同场景逐字相同，格式："时间，地点，氛围"。
7. 第 1 个镜头必须呈现本集 hook：{episode['hook']}
   最后 1 个镜头必须呈现悬念钩：{episode['cliffhanger']}

创作要求：
- 景别交替：连续 3 个镜头不得使用相同景别；情绪高点用特写。
- 台词从原著提炼改写为口语化短句（单句 ≤20 字），保留人物说话风格：{speech_styles}
- 旁白负责时间跳跃与心理描写，台词负责冲突，不要用旁白复述画面内容。

镜头连贯铁律（成片是否连贯取决于此，逐条遵守）：
- 同一场景内，除第一个镜头外，所有镜头 continuity_from_prev=true（生成时会用上一镜头的收尾画面作为本镜头的起始画面）。
- 相邻 continuity_from_prev=true 的镜头，动作必须严格承接：上一镜头 action_desc 的结束状态，就是本镜头动作的起始状态（如上一镜"拔剑指向黑影"，本镜应从持剑指向的姿态继续，而不是另起炉灶）。
- 每个场景的第一个镜头（continuity_from_prev=false）优先用远景或全景交代环境，再切近。
- 切换场景时 transition 用"叠化"或"黑场"，同场景内一律"硬切"。
- 角色不得凭空出现：某角色若在场景中段才登场，须在 action_desc 中写明入场方式（推门而入/从暗处走出）。

本集改编源文本：
{source_text[:12000]}

本集概要：{episode['synopsis']}
角色圣经：{bible.model_dump_json()}
上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON Schema：
{{"episode_no": {episode['episode_no']}, "shots": [{{"shot_no": int, "duration_s": int, "shot_size": "远景|全景|中景|近景|特写", "camera_move": "固定|推近|拉远|横摇|跟随", "scene_setting": str, "characters": [str], "action_desc": str, "narration": str|null, "dialogues": [{{"speaker": str, "line": str, "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定"}}], "transition": "硬切|叠化|黑场", "continuity_from_prev": bool}}]}}"""
    return await _run_with_repair(
        "分镜脚本", prompt, Storyboard,
        lambda b: validate_storyboard(b, bible, episode["target_duration_s"]),
        temperature=0.7, max_tokens=16384)


# ---------- E. VLM 质检 ----------

async def qa_shot(frames_b64: list[str], action_desc: str, scene_setting: str,
                  character_anchors: list[str]) -> dict:
    anchors = "\n".join(character_anchors) or "（本镜头无角色）"
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
    try:
        return extract_json(raw)
    except ValueError:
        return {"overall": -1, "issues": [f"质检输出不可解析：{raw[:200]}"]}
