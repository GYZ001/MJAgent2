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
from typing import Callable

from pydantic import BaseModel

from app import config, hiagent
from app.db import get_setting, log_provider_call
from app.schemas import (BeatChain, Bible, CAMERA_MOVES, EMOTIONS, EpisodePlan, SHOT_SIZES,
                         Storyboard, TIME_OF_DAY_ORDER, TRANSITIONS, extract_json, schema_errors)
from app.validators import (ACTION_DESC_MIN_CHARS, NARRATION_TARGET_CHARS,
                            ORAL_TARGET_RANGE, SCENE_SETTING_MAX_CHARS,
                            TRANSITION_HINTS, beat_scene_label,
                            validate_beat_chain, validate_bible, validate_plan,
                            validate_storyboard, validate_storyboard_against_beats,
                            VIDEO_SEGMENT_MIN_BEATS)

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
                           *, temperature: float = 0.7, max_tokens: int = 8192,
                           repair_user_prompt_limit: int | None = 3000) -> BaseModel:
    # 校验类失败持续让模型修复，直到通过或耗尽 max_repair_attempts。
    # hiagent.ProviderError（模型不可用）不在此捕获，直接透传——对这类错误重试无意义。
    max_attempts = max(int(get_setting("max_repair_attempts") or 8), 1)
    messages = [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": user_prompt}]
    draft = await hiagent.chat(messages, temperature=temperature, max_tokens=max_tokens)
    last_errors: list[str] = []
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
            last_errors = errors
        if attempt >= max_attempts - 1:
            break
        # 反复失败说明模型陷在同一处：升高温度跳出定式，并逐次加重措辞。
        repair_temp = 0.2 if attempt < 2 else min(0.2 + 0.15 * (attempt - 1), 0.8)
        emphasis = ("" if attempt < 2 else
                    f"\n\n【第 {attempt + 1} 次修复】以下问题你已多次未改正。请逐条对照硬性约束逐字修改，"
                    "确保全部满足，且不要引入新的违规。例如信息密度不足就必须补充原文细节、角色反应或关键线索，"
                    "相邻镜头断裂就必须承接上一镜尾状态，角色名错误就必须回到角色圣经和原文专名逐字修正。")
        original_task = user_prompt if repair_user_prompt_limit is None else user_prompt[:repair_user_prompt_limit]
        repair_prompt = (
            "你上一次的输出未通过校验。请修复以下具体问题后重新输出完整 JSON（不要解释，不要 Markdown）：\n"
            + "\n".join(f"- {e}" for e in last_errors[:20])
            + emphasis
            + "\n\n原任务要求：\n" + original_task
            + "\n\n你的原输出：\n" + draft[:6000]
        )
        draft = await hiagent.chat(
            [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": repair_prompt}],
            temperature=repair_temp, max_tokens=max_tokens)
    raise StageError(stage, last_errors + [f"已连续修复 {max_attempts} 次仍未通过校验，可点击重试，或在监制房调高「修复重试上限」"])


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
- 每集 source_chapters 连续、集间不重叠不跳章；一集可覆盖多章（通常 1~3 章），剧情紧凑。
- 不要超出第 {chapter_count} 章。{last_batch_hint}
- {timeline_req}

章节摘要：
{summaries_text}

角色圣经：
{bible.model_dump_json()}

输出 JSON Schema：
{{"key_timeline": [str], "episodes": [{{"episode_no": int, "title": str, "hook": str, "source_chapters": [int], "synopsis": str, "cliffhanger": str, "target_duration_s": int}}]}}
其中 hook=开头3秒画面+一句话；synopsis 80~150字；target_duration_s 只能取 40/50/60，常规集优先取 {config.EPISODE_TARGET_DEFAULT_S}。"""
    return await _run_with_repair(
        "剧集规划", prompt, EpisodePlan,
        lambda p: validate_plan(p.episodes, chapter_count,
                                start_episode_no=start_episode_no, start_chapter=start_chapter),
        temperature=0.7, max_tokens=12000)


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
   - event：谁做了什么（8~50 字，一句话，必须用角色圣经准确姓名：{names}）
   - turn：这件事改变了什么局势/揭示了什么新信息（4~40 字）。两拍的 turn 不得是同一信息的重复表述。
   - carry：留给下一拍的悬念或未完成动作（4~30 字）
3. 因果链：第 i+1 拍的 event 必须由第 i 拍的 carry 直接触发。禁止平行罗列事件，禁止把同一事件拆成两拍重复讲。
4. beat_type 分配：第 1 拍必须「钩子」，呈现：{episode['hook']}
   最后一拍必须「尾钩」，呈现：{episode['cliffhanger']}
   中间至少 1 拍「反转」或「高潮」；不允许连续两拍「铺垫」。
5. 时间线性（代码校验单调）：day_offset 从 0 开始（0=本集第一天），time_of_day 只能取 {list(TIME_OF_DAY_ORDER)}；时间只能向前，禁止闪回——前史/背景用第 1 拍的 event 或 turn 一句话带过，绝不回放过去场景。
6. location：2~10 字主地点标签（如"出租屋"），同一连续时空逐字同一写法；characters 只写该拍画面中实际在场的人（屏幕消息发送者/纸条落款不算在场）。
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
        temperature=0.7, max_tokens=6000)


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
5. 10s 内必须尽可能多塞入镜头和情节：每条 shot 的 action_desc 至少写 {VIDEO_SEGMENT_MIN_BEATS} 个连续小镜头/动作节点，建议 3~5 个，用"先……，随即……，转而……，最后……"写清顺序。
6. 每个 10s 视频段要像一段紧凑短片：允许快速切景、推近、特写、道具插入和角色反应连续发生；不要只写一个简单动作、凝视、走路、推门或氛围交代。
7. 口播带宽：每镜头 narration 字数 + 全部台词字数之和，目标 {ORAL_TARGET_RANGE[0]}~{ORAL_TARGET_RANGE[1]} 字——这是 10 秒配音念得完的量，超出太多会被退回（物理上念不完）。不设下限：信息密度由节拍表的局势变化保证，禁止用空泛情绪词注水凑字。
8. action_desc 目标 ≥{ACTION_DESC_MIN_CHARS} 字，不设上限；要写清"触发事件 + 连续动作 + 可见反应 + 新信息/后果"，让视频模型在 10s 内尽力实现多镜头推进。
9. 所有字数要求按目标值写作即可，校验器留有合理容差；优先保证戏剧质量与因果连贯，不要为凑数字牺牲剧情。
10. 每个 10s 视频段至少推进三个具体信息点：例如"发现线索 + 角色反应 + 冲突升级"、"动作结果 + 新目标 + 悬念加深"。禁止单纯场景氛围、人物姿态、重复上一镜内容。
11. 禁止纯画面空镜；固定 10s 视频段必须有旁白或台词承载剧情信息，但这些文字不是字幕上限，视频模型只需把信息视觉化。
12. 角色名必须准确：characters 不能为空，只能使用角色圣经里的准确姓名：{character_names}。characters 只写本镜头画面中实际可见/实际在场的人物；幕后发消息者、纸条落款、屏幕昵称、AI 软件名不算出场角色，除非镜头真的拍到他本人。不要创造新名字，不要把姓名改成外号/称谓，不要用"无角色"。如果原文出现角色姓名，必须照抄原文和角色圣经中的姓名。
13. action_desc 必须显式写出本镜头主要角色的准确姓名，不能只写"他/她/男人/女人/镜头/纸张"；每个动作节点都优先围绕人物表情、动作、道具反应和剧情后果展开。
14. dialogues 只写人物实际开口台词，dialogues[*].speaker 必须在本镜头 characters 中；不要把纸条文字、屏幕文字、手机通知、内心独白或旁白写成 speaker="旁白"，这些内容放到 narration 或 action_desc。
15. 台词 line 不设字数上限；emotion 只能取：{'|'.join(sorted(EMOTIONS))}。台词从原著提炼为口语化短句，但优先保留关键细节和人物说话风格：{speech_styles or '（无额外说话风格）'}。
16. scene_setting 只是连续性标签，不是渲染重点，最多 {SCENE_SETTING_MAX_CHARS} 字，只写"时间，地点"；能不写氛围就不写，禁止堆砌薄雾、灯光、杂物、墙面、天气等环境描写。镜头主要渲染故事情节和人物。
17. shot_size 只能取：{'|'.join(sorted(SHOT_SIZES))}；camera_move 只能取：{'|'.join(sorted(CAMERA_MOVES))}；transition 只能取：{'|'.join(sorted(TRANSITIONS))}。
18. 同一 scene_setting 的镜头必须连续排列，不能被其他场景打断；同一场景的 scene_setting 必须逐字相同，格式建议："时间，地点"。
19. 连续 3 个镜头不得使用相同 shot_size；情绪高点优先用特写。
20. 相邻镜头必须有明确上下文接力：同场景连续镜头 continuity_from_prev=true，下一镜 action_desc 的开头必须承接上一镜结尾的动作、道具、屏幕内容或情绪；换时间/地点时 continuity_from_prev=false，且 narration 或 action_desc 必须写清转场原因/时间跳跃。
21. 第 1 个镜头必须呈现本集 hook：{episode['hook']}
    最后 1 个镜头必须呈现悬念钩：{episode['cliffhanger']}"""


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
   - transition 只能用"叠化"或"黑场"，绝不能用"硬切"；
   - narration 或 action_desc 必须写清承接原因、时间跳跃或线索带入，建议出现：{hints} 等承接词；
   - 如果只是同一段连续动作里从房间走到门口/楼道/桌边/窗前，不要改 scene_setting，继续沿用上一镜主场景标签，把移动写进 action_desc。
5. scene_setting 是稳定短标签，不是镜头内容：同一连续时空统一写同一个"时间，主地点"，例如"当日，出租屋"；不要在相邻镜头里改成"当日，出租屋楼道外/桌前/门口"导致断链。
6. characters 只写本镜头实际可见/在场的人；屏幕发信人、纸条落款、新闻里提到的人、AI 软件名不算 characters。它们只能写在 action_desc 或 narration。
7. 每条 action_desc 必须显式写出 characters 中的准确角色名，并至少包含 {VIDEO_SEGMENT_MIN_BEATS} 个连续动作/信息节拍；不要只写纸张、屏幕、镜头、场景自己在动。
8. 每条 shot 都必须有 narration 或真实人物台词；dialogues[*].speaker 必须是本镜 characters 里的角色名，不能写"旁白"。

常见错误 → 正确写法：
- 错：上一镜"当日，出租屋"，本镜"当日，出租屋楼道外"，transition="硬切"，又没有解释。对：若是王浩从房内走到门口，scene_setting 仍写"当日，出租屋"，continuity_from_prev=true，action_desc 写"王浩攥着上一镜的纸页走向门口……"。
- 错：纸条上出现"未署名作者"就把 characters 写成 ["未署名作者"]。对：如果画面只拍到王浩和纸条，characters 写 ["王浩"]，纸条文字放 action_desc/narration。
- 错：下一镜重新说"出租屋昏暗、桌上有电脑"。对：下一镜直接从上一镜结尾继续，写"王浩仍盯着刚弹出的新闻推送，先……随即……最后……"。"""


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
    prompt = f"""任务：为漫剧第 {episode['episode_no']} 集《{episode['title']}》编写分镜脚本——按下方节拍表逐拍展开，第 i 镜实现第 i 拍。

本集节拍表（戏剧骨架，已通过结构校验，禁止增删拍、合并拍或调整顺序）：
{beat_table}

对拍展开规则（代码校验）：
A. 第 i 镜必须完整实现第 i 拍：画面呈现"事件"，让观众看懂"局势变化"，镜头结尾落在"结尾钩子"上——下一镜从这个钩子直接继续。
B. scene_setting 必须逐字使用节拍表给定的场景标签，不要自己改写时间地点。
C. characters 必须包含该拍的全部在场角色（可按画面需要增补圣经内角色，不可遗漏）。
D. 局势变化优先用台词和画面动作表达；旁白目标 ≤{NARRATION_TARGET_CHARS} 字，只写画面拍不出的信息（时间跳跃/内心活动），禁止复述画面、禁止堆人物前史（前史只允许第 1 镜旁白一句话带过）。

{output_contract}

{preflight_contract}

专名锁定（只在脑内执行，不要输出到 JSON）：
- 角色圣经姓名只能逐字使用 {', '.join(c.name for c in bible.characters) or '（角色圣经为空）'}；原文中的地名、书名、软件名、屏幕/纸条文字、人名必须逐字照抄，不要猜新名字、改字、换同音字或把普通称谓升级成新角色。
- 专名出现在纸条、屏幕、新闻或旁白里，不等于它就是本镜头 characters；characters 只放实际可见/在场的人。
- 如果原文用"我/他/她"，必须结合角色圣经和上下文还原为准确角色名；还原不了就用动作主体的普通称谓，不要编姓名。

创作要求：
- 旁白负责时间跳跃与心理描写（目标 ≤{NARRATION_TARGET_CHARS} 字），台词负责冲突与信息，不要用旁白复述画面内容。
- 场景描述能忽略就忽略：只保留最短时间地点标签；不要让薄雾、灯光、街道、杂物成为镜头主角。每个视频段的主角必须是人物、人物动作、人物反应和故事线索；场景只能服务于人物正在做什么、发现什么、失去什么、决定什么。
- 每个镜头输出前先自检：shot_no 连续、duration_s 全部为 10、characters 非空且姓名准确、action_desc 出现准确角色名、scene_setting 足够短、文案满足信息密度下限且不检查上限、剧情载荷足够、action_desc 至少 3 个连续小镜头/动作节点、台词 speaker 在本镜头 characters 中且不能是旁白、与上一镜有动作/道具/情绪/信息承接。

镜头连贯铁律（成片是否连贯取决于此，逐条遵守）：
- 同一场景内，除第一个镜头外，所有镜头 continuity_from_prev=true（生成时会用上一镜头的收尾画面作为本镜头的起始画面）。
- 相邻 continuity_from_prev=true 的镜头，动作必须严格承接：上一镜头 action_desc 的结束状态，就是本镜头动作的起始状态（如上一镜"拔剑指向黑影"，本镜应从持剑指向的姿态继续，而不是另起炉灶）。
- 下一镜不要重新介绍同一场景，不要把上一镜已经完成的发现/动作重新讲一遍；必须推进到"因此发生了什么"。
- 如果必须跨时间或跨地点，transition 用"叠化"或"黑场"，continuity_from_prev=false，并在 narration 或 action_desc 写清"次日/几小时后/与此同时/他带着某线索来到某处"这类承接语。
- 每个场景的第一个镜头（continuity_from_prev=false）优先用远景或全景交代环境，再切近。
- 切换场景时 transition 用"叠化"或"黑场"，同场景内一律"硬切"。
- 角色不得凭空出现：某角色若在场景中段才登场，须在 action_desc 中写明入场方式（推门而入/从暗处走出）。

本集改编源文本：
{source_text}

分镜改编依据：只以以上原文全文、hook、悬念钩、角色圣经和上一集结尾为准；episode.synopsis 仅用于前端展示，禁止作为分镜剧情依据。
角色圣经：{bible.model_dump_json()}
上一集结尾：{prev_ending or "（本集为第一集）"}

输出 JSON Schema：
{{"episode_no": {episode['episode_no']}, "shots": [{{"shot_no": int, "duration_s": int, "shot_size": "远景|全景|中景|近景|特写", "camera_move": "固定|推近|拉远|横摇|跟随", "scene_setting": "短时间+地点标签", "characters": ["画面中实际可见/在场且属于角色圣经的准确姓名"], "action_desc": str, "narration": str|null, "dialogues": [{{"speaker": "必须是本镜头 characters 中的角色名", "line": str, "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定"}}], "transition": "硬切|叠化|黑场", "continuity_from_prev": bool}}]}}"""
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
            "contract_version": "storyboard_beat_chain_v8",
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
    return await _run_with_repair(
        "分镜脚本", prompt, Storyboard,
        lambda b: validate_storyboard_against_beats(b, bible, episode["target_duration_s"], chain),
        temperature=0.7, max_tokens=16384, repair_user_prompt_limit=None)


# ---------- E. VLM 质检 ----------

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
    try:
        return extract_json(raw)
    except ValueError:
        return {"overall": -1, "issues": [f"质检输出不可解析：{raw[:200]}"]}
