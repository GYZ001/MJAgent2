"""分镜台：分镜正文复核（P0，2026-09-30，真人短剧《顾念长安》第 1 集 31 段多代理
逐段核查驱动）。

背景：既有闸门（``storyboard_action_density``/``storyboard_skin_blush`` 等）要么
只核验模型自报的结构化字段（动作密度只数 ``shot_action_beats`` 条数，不读
``prompt_text`` 正文本身——模型自报 2 条、正文写 9 个动作照样放行），要么只把
规则拼进提示词从不核验（脸红措辞）。独立核验成立的七类真实缺陷——单镜动作
过载、写实画风脸红措辞、无台词的持续说话、跨段左右站位翻转、道具凭空出现、
单镜大跨度时间跳跃、镜头描述用否定句写动作——共同根因是「规则都写进了提示词，
但没有任何一步读最终 prompt_text 正文去核对」。本模块补这一步：整集逐段
``prompt_text`` 定稿后，发一次独立复核调用（模型提名、代码核验），有已核验
违规的段按升序走既有单段重生成通道定向重写一次，重写后再复核该段与紧邻下一段，
仍有问题就写进 ``degraded_capabilities`` 不阻断整集（CLAUDE.md「修补器与校验器
死锁」：后处理同样不得把整集卡死）。

## 判据只认七类，取值集合单源

``_KIND_RULES`` 既是喂给复核模型的判据正面陈述，也是代码核验 ``kind`` 合法性的
唯一依据（``v.kind in _KIND_RULES``）——CLAUDE.md「模型契约两侧必须对齐」：schema
允许的取值和校验接受的取值必须来自同一份数据，不得两处各写一份、宽的一侧成为
故障。``kind`` 在 ``ProseViolation`` 里是普通 ``str`` 而不是 Literal/Enum：
Literal 会让 ``chat_structured`` 在格式层整体拒绝回答（一条 kind 写错，整段
复核全部作废、触发格式重试），而本模块要的是「逐条丢弃不合法的那一条，其余
继续用」，判断必须留在我们自己的代码核验这一步，不能上交给 pydantic 的格式
校验（同一取舍另见 ``_resolve_review_verdicts`` 的 ``_review_response_model``
对照：那边用 Literal 是因为 item_id 集合当次固定且必须整体对齐，这里不是）。

``action_density`` 的上限复用 ``storyboard_action_density.MAX_KEY_ACTIONS_PER_
SHOT``，不另立常量；``skin_blush`` 的判据文本直接引用
``storyboard_skin_blush.SEEDANCE_SKIN_BLUSH_RULE`` 原文，规则改了判据自动跟着
变，且只在写实画风（``app.visual_styles.is_photographic_style_prompt``）生效，
非写实项目连这条规则都不会出现在提示词里——与 ``skin_blush_dialect_addendum``
开关关闭时的「逐字不变」同一纪律。

## 代码核验：模型提名、代码核验

每条 violation 的 ``quote`` 必须能在本段 ``prompt_text`` 里逐字核验到
（``textmatch.condense`` 容忍标点/空白差异，与 ``storyboard_beat_causality``/
``storyboard_beat_foreshadowing`` 的 ``evidence_quote`` 核验同一口径，不是语义
匹配）；``screen_side``/``prop_appearance`` 两类额外要求 ``previous_quote`` 逐字
出现在上一段 ``prompt_text`` 里（本集第一段没有上一段时，这两类违规结构上不可能
成立，直接丢弃）。核验不过的条目单独丢弃，不拖累同一段其余已核验违规，打印
``[STORYBOARD_PROSE_REVIEW_UNVERIFIED]`` 前缀日志——可见，不静默。

## 定向重写与死锁规避

有已核验违规的段按段号升序逐段调用既有单段重生成通道
（``storyboard_pack._generate_all_segment_prompts`` 的 ``reuse_segments``/
``revision_notes`` 参数，与 ``storyboard_identity_regenerate.regenerate_identity_
candidate`` 用户点「修订本段」同一条通道，但不走那个路由——避免嵌套循环）。
每段最多重写一次；重写成功后立即再复核该段与紧邻下一段（下一段的跨段判据
``screen_side``/``prop_appearance`` 依赖它），结果覆盖式更新「待处理违规」表：
如果那个邻段本来就排在后面等自己的处理轮次，就把结果留给它自己那一轮（可能
因此被跳过重写，也可能带着刷新后的违规正常重写）；如果邻段不在待处理表里
（首轮复核没发现问题，是这次重写才牵连出来的），就没有下一轮会处理它了，
当场把剩余违规记进它的 ``degraded_capabilities``。重写调用抛异常（含重写稿
未通过 ``_generate_all_segment_prompts`` 自身既有校验耗尽重试后向上抛出，两者
对调用方是同一种表现）时保留原稿，``exc_info=True`` 记录完整异常
（``[STORYBOARD_PROSE_REVIEW_FAILED]``，不吞异常信息），不让整集失败。

## 并发与循环导入

所有段先并发复核，上限 ``_REVIEW_CONCURRENCY``（opus 有限流）；复核通过后的
定向重写必须串行（同一份 ``reuse_segments`` 快照不能被并发重写互相踩踏）。

依赖方向：本模块不导入 ``storyboard_pack``。定向重写所需的「按修订意见重写一段」
由调用方（``storyboard_pack.generate_storyboard_pack``）以 ``regenerate(reuse_segments,
revision_notes)`` 回调注入——单向依赖 ``storyboard_pack -> 本模块``，不需要任何一侧
用函数内延迟导入把循环藏进运行时（CLAUDE.md「函数内 import app.* 不是解耦手段」）。

## 开关

``storyboard_prose_review_enabled()``：全局设置键 ``storyboard_prose_review_
enabled``，默认开启（``app.config.DEFAULT_SETTINGS`` + ``app.monitoring.
SETTINGS_SCHEMA`` 两处登记，未声明的设置键写接口会拒写）；关闭时
``review_and_revise_segments`` 原样返回入参，不发起任何复核调用，产物与今天
逐字一致。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from app import textmatch
from app.db import get_setting
from app.harness import model_gateway
from app.production import storyboard_action_density as _action_density
from app.production import storyboard_skin_blush as _skin_blush
from app.production.storyboard_continuity_memo import continuity_memo_payload
from app.production.storyboard_staging_repeat import sub_shots
from app.visual_styles import current_visual_style_prompt, is_photographic_style_prompt

_LOGGER = logging.getLogger(__name__)

PROSE_REVIEW_SETTING_KEY = "storyboard_prose_review_enabled"
#: 并发复核上限：opus 有限流，见模块 docstring「并发与循环导入」。
_REVIEW_CONCURRENCY = 3
#: 复核调用只需要返回一份不长的违规清单，不是整段 prompt_text。
_REVIEW_ANSWER_TOKENS = 1800
#: 要求 previous_quote 必须逐字核验到的两类——本段开场状态与上一段末镜矛盾。
_NEEDS_PREVIOUS_QUOTE = {"screen_side", "prop_appearance"}

#: 七类判据的正面陈述，单源用于「喂给模型的提示词」与「核验 kind 合法性」两处
#: （见模块 docstring「判据只认七类，取值集合单源」）。``{max_actions}`` 由
#: ``_review_rules_text`` 用 ``storyboard_action_density.MAX_KEY_ACTIONS_PER_
#: SHOT`` 填入。
_KIND_RULES: dict[str, str] = {
    "action_density": (
        "action_density（单镜动作过载）：按发生顺序列出这一镜里出现的每一个关键动作。{key_action_definition}"
        "一镜超过 {max_actions} 个关键动作即违规——15 秒段每镜只有约 3-4 秒，装不下更多，视频生成模型"
        "会编造画面去'跟上'过多的指令。quote 填这一镜在 prompt_text 里的原文片段；fix 按下面的应对写清楚"
        "怎么改（本段当前镜头数见输入里的 shot_count）：{over_limit_remedy}"
    ),
    "skin_blush": (
        "skin_blush（写实画风下脸红写法不对）：判据是——{skin_blush_rule} 如果镜头描述没有按这个写法"
        "来写——比如把脸红写成一个有起点（某个部位）、有路径（一路漫/漫到）、有终点（另一个部位）的"
        "空间蔓延过程，或用了高饱和度/大面积的措辞——即违规。quote 填原文里描述脸红的那句话；fix 按"
        "上面的写法改写成轻微、自然、渐变的局部细节。"
    ),
    "unvoiced_speech": (
        "unvoiced_speech（无台词的持续说话）：镜头描述里某个人物处于持续说话的状态（嘴唇/嘴部开合、"
        "说着说着如何如何），但这一镜既没有写 {{speech:Uxx}} 占位符，也对不上下面『本段台词占位符"
        "清单』里的任何一条——即违规。quote 填这段描写说话状态的原文；fix 给出两种正面写法之一：要么"
        "把这段表演改写成不说话的短促动作（张嘴又闭上、摇头、停顿、欲言又止），要么——仅当原文确实有"
        "这句台词时——建议走台词合同补上对应的 {{speech:Uxx}} 占位符，不要自己编一句新台词。"
    ),
    "screen_side": (
        "screen_side（跨段左右站位翻转）：本段延续的是上一段的同一场景，本段开场人物的画面左右站位与"
        "上一段末镜相反，但本段和上一段末镜都没有写任何一方的换位走动作为依据——即违规。quote 填本段"
        "开场描述站位的原文；previous_quote 必须填上一段末镜里对应描述站位的原文（上面已给出『上一段"
        "末镜文字』，从那句话里逐字摘取）；fix 给出两种正面写法之一：改回与上一段一致的站位，或者补写"
        "一个明确的换位走动作。"
    ),
    "prop_appearance": (
        "prop_appearance（道具凭空出现）：本段开场某件道具已经在人物手里或桌上，但上一段末镜和本段都"
        "没有交代它是怎么被拿出来、带过来、放上去的——即违规。quote 填本段里道具凭空出现的原文；"
        "previous_quote 必须填上一段末镜文字中与这件道具/这个位置相关的原文（如果上一段末镜完全没提"
        "这件道具，就填上一段末镜那句话本身，作为『确实没交代』的证据——必须逐字摘自上面给出的『上一段"
        "末镜文字』，不能自己编）；fix 给出两种正面写法之一：补一个明确的拿取/放置动作，或改成与上一段"
        "末镜一致的初始状态。"
    ),
    "time_jump": (
        "time_jump（单镜大跨度时间跳跃）：一个不间断的镜头运动里跨越了明显的一段时间（例如从深夜写到"
        "天亮、从白天写到夜晚），镜头描述里却没有任何硬切或叠化把这段时间过去交代清楚——即违规。quote"
        "填这一镜里描述大跨度时间推移的原文；fix 建议把它拆成两个镜头，之间用硬切或叠化交代时间过去。"
    ),
    "negated_action": (
        "negated_action（用否定句写人物动作）：镜头描述里用否定句描写人物正在做的动作或穿着状态（例如"
        "『没有……』『不再……』『没有再……』），即违规——视频生成模型会忽略否定句，画出来的反而是被"
        "否定的那个动作。系统统一追加的全局约束行（prompt_text 末尾以『约束——』开头的那一行）、人数"
        "锁定句（『画面中只有……不出现其他人物或路人。』）以及台词占位符或台词后面括号里的口型说明"
        "（例如『画面人物嘴唇闭合无张合动作』『发声者开口，其他可见人物不跟随口型』）都不算，它们是"
        "系统统一写入的固定说明，不是对人物动作的描写。quote 填这句否定句原文；fix 给出对应的正面写法：直接描述人物实际在做什么/"
        "穿什么，不提被否定的那个动作。"
    ),
}


class ProseViolation(BaseModel):
    """复核模型对一条违规的提名；是否采信由本模块的代码核验决定，见模块 docstring。"""

    kind: str = ""
    shot_label: str = ""
    quote: str = ""
    previous_quote: str = ""
    fix: str = ""


class _ProseReviewResponse(BaseModel):
    violations: list[ProseViolation] = Field(default_factory=list)


def storyboard_prose_review_enabled() -> bool:
    """默认开启；settings 表里 ``storyboard_prose_review_enabled`` 显式写
    0/false/off/no 才关闭——与 ``app.video_plan.prev_frame_reference.prev_frame_
    reference_enabled`` 同一读法，只是默认方向相反（该开关默认关闭，本开关默认
    开启，两者都以『未显式写否定值』为真/假的判定起点）。"""
    raw = str(get_setting(PROSE_REVIEW_SETTING_KEY) or "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _is_photographic(bible: Any) -> bool:
    """与 ``_generate_all_segment_prompts`` 推导 ``visual_style_is_photographic``
    同一表达式（见 ``storyboard_pack`` 模块内该局部变量），这里独立重算一遍：
    本模块只拿到调用方传入的 ``bible``，不持有那次调用内部的局部变量。"""
    if bible is None or bible.world is None:
        return False
    return is_photographic_style_prompt(current_visual_style_prompt(bible.world.visual_style_canonical))


def _dialogue_placeholders(dialogue: list[Any]) -> list[dict[str, str]]:
    """本段台词合同展开的占位符清单，token 格式与分镜正文里实际写入的占位符同形
    （``"{{speech:" + utterance_id + "}}"``），供复核模型核对『说话但没占位符』时
    有一份权威清单可比对，不必自己猜占位符长什么样。"""
    return [
        {
            "utterance_id": str(getattr(line, "utterance_id", "")),
            "placeholder": "{{speech:" + str(getattr(line, "utterance_id", "")) + "}}",
            "speaker_identity_id": str(getattr(line, "speaker_identity_id", "")),
            "line": str(getattr(line, "line", "")),
        }
        for line in dialogue
    ]


def _previous_shot_text(previous_draft: Any | None) -> str:
    """上一段末镜文字：``sub_shots`` 按『镜头N：』切出子镜列表，取最后一条；没有
    上一段或解析不出子镜时返回空串——复核模型据此知道这是本集第一段或上一段
    没有可比对的镜头文字，不强行比对 screen_side/prop_appearance。"""
    if previous_draft is None:
        return ""
    shots = sub_shots(previous_draft.prompt_text)
    return shots[-1] if shots else ""


def _review_rules_text(*, photographic: bool, max_shots: int) -> str:
    """七类判据的完整正面陈述；``skin_blush`` 只在写实画风项目出现（见模块
    docstring），非写实项目这条规则连提示词都不会收到。"""
    kinds = [k for k in _KIND_RULES if k != "skin_blush" or photographic]
    numbered = "\n".join(
        f"{i}. " + _KIND_RULES[kind].format(
            max_actions=_action_density.MAX_KEY_ACTIONS_PER_SHOT, skin_blush_rule=_skin_blush.SEEDANCE_SKIN_BLUSH_RULE,
            key_action_definition=_action_density.key_action_definition(),
            over_limit_remedy=_action_density.over_limit_remedy(max_shots=max_shots),
        )
        for i, kind in enumerate(kinds, start=1)
    )
    return (
        "逐条核对下面这一段分镜正文（prompt_text）有没有出现以下几类问题；每一类都只在你能在正文里"
        "找到逐字证据时才报告，找不到就不要报告这一类，宁可少报不要编造。每条违规给出 kind（取值只能是"
        f"下面编号对应的英文名）、shot_label（这一镜的标签，例如『镜头2』）、quote（出问题的原文片段，"
        "必须逐字照抄 prompt_text 里的文字）、fix（怎么改）；screen_side 与 prop_appearance 两类还要给"
        f"previous_quote（逐字照抄『上一段末镜文字』里的对应原文）。\n{numbered}"
    )


def _verbatim_in(quote: str, text: str) -> bool:
    condensed = textmatch.condense(quote or "")
    return bool(condensed) and condensed in textmatch.condense(text or "")


def _verified_violations(
    raw: list[ProseViolation], *, segment_no: int, draft: Any, previous_draft: Any | None,
) -> list[ProseViolation]:
    """模型提名、代码核验：kind 必须在 ``_KIND_RULES`` 里，quote 必须逐字核验到
    本段 prompt_text；screen_side/prop_appearance 额外要求 previous_quote 逐字
    核验到上一段 prompt_text（没有上一段时这两类结构上不可能成立）。核验不过的
    条目单独丢弃，不拖累同段其余已核验违规，可见日志见模块 docstring。"""
    verified: list[ProseViolation] = []
    for v in raw:
        if v.kind not in _KIND_RULES:
            _LOGGER.warning("[STORYBOARD_PROSE_REVIEW_UNVERIFIED] 第 %s 段 kind=%r 不在合法取值内，丢弃", segment_no, v.kind)
            continue
        if not _verbatim_in(v.quote, draft.prompt_text):
            _LOGGER.warning("[STORYBOARD_PROSE_REVIEW_UNVERIFIED] 第 %s 段 kind=%s 的 quote 在 prompt_text 里找不到逐字证据，丢弃：%r", segment_no, v.kind, v.quote[:200])
            continue
        if v.kind in _NEEDS_PREVIOUS_QUOTE and not (previous_draft is not None and _verbatim_in(v.previous_quote, previous_draft.prompt_text)):
            _LOGGER.warning("[STORYBOARD_PROSE_REVIEW_UNVERIFIED] 第 %s 段 kind=%s 的 previous_quote 在上一段 prompt_text 里找不到逐字证据，丢弃：%r", segment_no, v.kind, v.previous_quote[:200])
            continue
        verified.append(v)
    return verified


async def _review_segment(
    *, episode_id: str, segment_no: int, draft: Any, previous_draft: Any | None, photographic: bool, max_shots: int,
) -> list[ProseViolation]:
    """一次独立复核调用，失败（供应商错误/格式修复耗尽）返回空列表，不让整集
    失败——与 ``storyboard_short_drama_review._run_drop_review`` 同一取舍。"""
    payload: dict[str, Any] = {
        "rules": [_review_rules_text(photographic=photographic, max_shots=max_shots)],
        "segment_no": segment_no,
        "shot_count": draft.shot_count,
        "prompt_text": draft.prompt_text,
        "dialogue_placeholders": _dialogue_placeholders(draft.dialogue),
        "previous_segment_last_shot": _previous_shot_text(previous_draft),
        "previous_continuity_memo": continuity_memo_payload(previous_draft.continuity_memo if previous_draft is not None else None),
        "output_schema": _ProseReviewResponse.model_json_schema(),
    }
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    try:
        response = await model_gateway.chat_structured(
            [
                {"role": "system", "content": "你是短剧分镜正文复核员，只核对画面正文本身的问题，不改动剧情。只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_type=_ProseReviewResponse,
            validate=None,
            operation_id=f"storyboard_prose_review_{episode_id}_{segment_no}_{fingerprint}",
            max_tokens=_REVIEW_ANSWER_TOKENS,
            format_retry_limit=1,
            semantic_retry_limit=0,
            temperature=0.2,
            call_meta={
                "stage_key": "storyboard_prose_review",
                "call_role": "storyboard_prose_review_segment",
                "initiator_label": "分镜台正文复核",
                "episode_id": episode_id,
                "segment_no": segment_no,
            },
        )
    except Exception:  # noqa: BLE001 -- 复核失败不许让整集失败，见模块 docstring
        _LOGGER.warning("[STORYBOARD_PROSE_REVIEW_FAILED] 第 %s 段复核调用失败，跳过本段复核", segment_no, exc_info=True)
        return []
    return response.violations


async def _run_batch_review(
    segment_drafts: dict[int, Any], *, episode_id: str, photographic: bool, max_shots: int,
) -> dict[int, list[ProseViolation]]:
    """全部段落并发复核（上限 ``_REVIEW_CONCURRENCY``），只返回有已核验违规的段。"""
    semaphore = asyncio.Semaphore(_REVIEW_CONCURRENCY)

    async def _one(no: int) -> tuple[int, list[ProseViolation]]:
        previous = segment_drafts.get(no - 1)
        async with semaphore:
            raw = await _review_segment(episode_id=episode_id, segment_no=no, draft=segment_drafts[no], previous_draft=previous, photographic=photographic, max_shots=max_shots)
        return no, _verified_violations(raw, segment_no=no, draft=segment_drafts[no], previous_draft=previous)

    results = await asyncio.gather(*[_one(no) for no in sorted(segment_drafts)])
    return {no: violations for no, violations in results if violations}


def _revision_notes_text(violations: list[ProseViolation]) -> str:
    """把已核验违规整理成正面修改意见，交给 ``_revision_notes.segment_rule_
    text`` 包装（该函数由 ``_generate_all_segment_prompts`` 内部调用，本模块不
    重复包装）。"""
    lines = [f"{i}. [{v.kind}] {v.shot_label or '对应镜头'}：{v.fix}（原文问题片段：{v.quote[:80]}）" for i, v in enumerate(violations, start=1)]
    return "分镜正文复核发现以下问题，请逐条修正，其余镜头与画面保持不变：\n" + "\n".join(lines)


def _remaining_advisory_texts(violations: list[ProseViolation]) -> list[str]:
    return [f"[STORYBOARD_PROSE_REVIEW_REMAINING][未拦截] [{v.kind}] {v.shot_label or '对应镜头'}：{v.fix}（原文：{v.quote[:80]}）" for v in violations]


Regenerate = Callable[[dict[int, Any], str], Awaitable[dict[int, Any]]]
"""``regenerate(reuse_segments, revision_notes)``：调用方注入的单段重写回调，语义同
``storyboard_pack._generate_all_segment_prompts`` 的同名两个参数（见模块 docstring
「依赖方向」）。"""


async def _regenerate_segment(
    no: int, segment_drafts: dict[int, Any], violations: list[ProseViolation], *, regenerate: Regenerate,
) -> dict[int, Any] | None:
    """按既有单段重生成通道重写第 ``no`` 段；失败（含重写稿未通过既有校验耗尽
    重试后向上抛出）返回 None，调用方保留原稿——不让整集失败，见模块 docstring
    「定向重写与死锁规避」。"""
    reuse = {segno: d for segno, d in segment_drafts.items() if segno != no}
    try:
        return await regenerate(reuse, _revision_notes_text(violations))
    except Exception:  # noqa: BLE001 -- 不吞异常信息：exc_info=True 保留完整堆栈
        _LOGGER.warning("[STORYBOARD_PROSE_REVIEW_FAILED] 第 %s 段定向重写调用失败，保留原稿", no, exc_info=True)
        return None


async def review_and_revise_segments(
    segment_drafts: dict[int, Any], *, episode_id: str, bible: Any, max_shots: int, regenerate: Regenerate,
) -> dict[int, Any]:
    """整集复核 → 定向重写一次 → 再复核；开关关闭时原样返回，逐字不变。

    ``outstanding`` 是「待处理违规」表：初始来自首轮批量复核，处理某段时若它的
    紧邻下一段也被重新核验过，按该段是否还会轮到自己的处理轮次决定是现在就
    收尾（写 degraded_capabilities）还是把结果留给它自己那一轮（见模块
    docstring「定向重写与死锁规避」）。
    """
    if not storyboard_prose_review_enabled():
        return segment_drafts
    photographic = _is_photographic(bible)
    outstanding = await _run_batch_review(segment_drafts, episode_id=episode_id, photographic=photographic, max_shots=max_shots)
    for no in sorted(outstanding):
        violations = outstanding.get(no) or []
        if not violations:
            continue  # 已被更早一段重写后的邻段复核清空
        rewritten = await _regenerate_segment(no, segment_drafts, violations, regenerate=regenerate)
        if rewritten is None:
            segment_drafts[no].degraded_capabilities = [*segment_drafts[no].degraded_capabilities, *_remaining_advisory_texts(violations)]
            continue
        segment_drafts = rewritten
        for check_no in (no, no + 1):
            if check_no not in segment_drafts:
                continue
            draft, previous = segment_drafts[check_no], segment_drafts.get(check_no - 1)
            raw = await _review_segment(episode_id=episode_id, segment_no=check_no, draft=draft, previous_draft=previous, photographic=photographic, max_shots=max_shots)
            verified = _verified_violations(raw, segment_no=check_no, draft=draft, previous_draft=previous)
            if check_no != no and check_no in outstanding:
                outstanding[check_no] = verified
            elif verified:
                draft.degraded_capabilities = [*draft.degraded_capabilities, *_remaining_advisory_texts(verified)]
    return segment_drafts
