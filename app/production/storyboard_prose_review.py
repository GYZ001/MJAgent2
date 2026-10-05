"""分镜台：分镜正文复核（P0，2026-09-30，真人短剧《顾念长安》第 1 集 31 段多代理
逐段核查驱动）。

背景：既有闸门（``storyboard_action_density``/``storyboard_skin_blush`` 等）要么
只核验模型自报的结构化字段（动作密度只数 ``shot_action_beats`` 条数，不读
``prompt_text`` 正文本身——模型自报 2 条、正文写 9 个动作照样放行），要么只把
规则拼进提示词从不核验（脸红措辞）。独立核验成立的七类真实缺陷——单镜动作
过载、写实画风脸红措辞、无台词的持续说话、跨段左右站位翻转、道具凭空出现、
单镜大跨度时间跳跃、镜头描述用否定句写动作——共同根因是「规则都写进了提示词，
但没有任何一步读最终 prompt_text 正文去核对」。本模块补这一步：每段
``prompt_text`` 定稿后，发一次独立复核调用（模型提名、代码核验），有已核验
违规就带着修改意见原地重写这一段一次，重写稿再复核一次，仍有问题就写进
``degraded_capabilities`` 不阻断（CLAUDE.md「修补器与校验器死锁」：复核同样
不得把整集卡死）。

2026-10-01（边写边审，真实耗时驱动：第 1 集 30 段一轮 118 分钟，逐段写分镜
73 次占 96.8 分钟、正文复核 77 次占 26.3 分钟，全程串行）：复核与定向重写从
「整集写完后单独一次批量后处理」改成「每段生成完立刻复核、立刻重写」，挪进
``storyboard_pack._generate_all_segment_prompts`` 自己的逐段循环（见
``review_segment_inline``）。旧设计下第 N 段重写后，第 N+1 段当初是照着旧的
第 N 段写的，只能靠「复核下一段」补救（``screen_side``/``prop_appearance``/
``repeated_transition_action`` 这三类跨段判据因此需要一张「待处理违规」表和
邻段复核的覆盖规则）；新设计下第 N 段的复核与重写在第 N+1 段开始生成之前就
已经定稿，第 N+1 段天然衔接的是修正后的草稿，不再需要这张表、也不再需要
批量并发（复核与生成现在严格同步、无并发）。

2026-10-01（第 1 集重做第二轮分镜 ``/tmp/mjtest/ep1_redo/segments_r2.json``
独立核查新增第八类）：第 30 段末镜已写温念转身背对镜头望向窗户，硬切到第 31
段开场又写她顺着视线回过头、肩膀侧转——同一个转身在剪辑点两侧被演了两次。
``screen_side`` 只管左右站位，不管「上一段末镜已完成的转身/起身/坐下/回头等
状态转换在本段开场又重新演一遍」，故新增 ``repeated_transition_action``。
同一次核查还发现 ``time_jump`` 原判据的 fix 建议「用硬切或叠化交代时间过去」，
与 ``storyboard_dialects`` 贯穿全片的「镜头之间硬切」全局规则冲突——叠化不是
段内可选项，fix 文案已改为只建议硬切。

2026-10-01（第 1 集重做第三轮分镜 ``/tmp/mjtest/ep1_redo/segments_r3.json``
多代理核查新增第九类）：第 26 段收尾镜写「镜头从餐桌上方缓慢升起并向后拉远，
越过窗台退到窗外巷子上空，透过窗户俯看整间餐厅」——真人实拍摄影机做不到
穿过墙体/玻璃从室内直接运动到室外。新增 ``impossible_camera_move``，只在
写实画风启用（``is_photographic_style_prompt``），判据与 fix 形状照抄
``skin_blush``/``time_jump`` 的「正面陈述 + 必须逐字核验 quote」先例。

2026-10-04（用户反馈《顾念长安》EP1 第 1→2 段：温念拔下插头、插座两孔空着，
下一段又画成插头插着——新增第十一类）：``prop_state_regression``——上一段
``continuity_memo.props`` 记录的道具/衣物位置（location）/状态（state）与默认
或此前样子不同，本段开场却把它写回了那个默认/此前样子，且本段原文没有写出
让它变回去的动作。与现有三类跨段判据不同：``previous_quote`` 不要求逐字
出现在上一段 ``prompt_text``（那类状态信息未必被写进正文，但一定写进备忘），
而是逐字核验到上一段 ``continuity_memo.props`` 对应条目的 ``location``/
``state`` 字段文本本身——见 ``_prop_state_regression_quote_valid``。

2026-10-01（第 1 集第五版 35 段真实成片逐帧复查，``storyboard_skin_blush``
模块 docstring 同批）：``skin_blush`` 判据从"把脸红写成空间蔓延过程才算违规"
扩大为"人脸出现任何局部颜色描述就算违规"——轻量写法（「脸颊泛起淡淡的红晕」）
实测仍有相当比例被画成硬边色块，不只是重写法、空间蔓延式写法会这样；判据
文本随 ``_KIND_RULES["skin_blush"]`` 一起改写，``{skin_blush_rule}`` 占位符
仍单源指向 ``storyboard_skin_blush.SEEDANCE_SKIN_BLUSH_RULE``，不需要在这里
另写一份取值。

2026-10-01（修订本段验收后的第二轮逐帧复查，新增第十类）：第 20 段镜头 4 顾屿
手里凭空多拉了一只与温念那只外观一致的行李箱，第 28 段同一画面里行李箱同时在
床头柜与床尾各出现一份——与 ``storyboard_cast_lock`` 处理的人物分身同源，但发生
在道具上。新增 ``prop_duplication``，与 ``storyboard_prop_count`` 模块 docstring
同批：不像 ``screen_side``/``prop_appearance``/``repeated_transition_action``
需要对照上一段末镜，这里两份重复描述都在本段 ``prompt_text`` 内部，复核模型通读
本段正文本身即可判断，不需要 ``previous_quote``；判据文本单源指向
``storyboard_prop_count.SEEDANCE_PROP_COUNT_RULE``。不按画风分支（道具分身与
写实/非写实画风无关，不进 ``_PHOTOGRAPHIC_ONLY_KINDS``）。

2026-10-05（真人短剧《顾念长安》第 1 集第 8→9 段实测缺陷驱动，新增第十二类）：
第 8 段末镜写温念「右手五指慢慢蜷起，攥住自己的外套袖口收回胸前……眉头拧起……
张口……『还给我！』」；第 9 段镜头 1 写「起幅接上一段末镜：……上身前倾，右手
掌心朝上伸过桌面，停在半空」，没有写过渡动作也没写神情——视频生成模型照着画成
硬切一瞬间她从攥拳怒喊变成面带微笑、手掌伸在半空。``storyboard_narrative_arc``
的 rule_1（人物姿态自然接续）早就写进了生成提示词，但写分镜的模型照样会违反；
既有跨段判据没有一类能抓「本段起幅的人物姿态/手部位置/神情与上一段末镜对不上、
中间又没写过渡动作」，新增 ``opening_pose_break``。与 ``repeated_transition_
action`` 方向相反但容易混淆：那一类是上一段已完成的转换过程在本段又被重新演了
一遍（同一动作演两次）；这一类是本段起幅状态与上一段末镜状态本身对不上、中间
没写过渡动作（状态断档，不是重复演）。不按画风分支（姿态/神情接续与写实/非
写实画风无关）；``previous_quote`` 核验机制与 ``screen_side`` 同一套（逐字核验
到上一段 ``prompt_text``），加入 ``_NEEDS_PREVIOUS_QUOTE``。本次改动同批把
``_KIND_RULES`` 字典搬到 ``storyboard_prose_review_rules`` 模块（纯搬移，字典
内容逐字不变，原名字 ``_KIND_RULES`` 在本模块整体再导出）——本模块当时正好
500 行，Python 文件上限守着不许再加，新判据与搬移腾出的行数一起落地；新模块
docstring 不重复这里的历史，只写拆分本身的理由。

## 判据只认十二类，取值集合单源

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
匹配）；``screen_side``/``prop_appearance``/``repeated_transition_action``/
``opening_pose_break`` 四类额外要求 ``previous_quote`` 逐字出现在上一段
``prompt_text`` 里（本集第一段没有上一段时，这四类违规结构上不可能成立，直接
丢弃）。核验不过的条目单独丢弃，不
拖累同一段其余已核验违规，打印
``[STORYBOARD_PROSE_REVIEW_UNVERIFIED]`` 前缀日志——可见，不静默。

## 边写边审：`review_segment_inline`

调用方（``_generate_all_segment_prompts`` 的逐段循环）每生成一次草稿就调用一次
本函数，传入 ``attempt``（这是本段第几次生成尝试，从 0 开始）。没有已核验违规、
或 ``attempt`` 已到 ``INLINE_MAX_ATTEMPTS - 1``（最后一次尝试）时返回空串，前者
什么都不做，后者把剩余违规写进 ``draft.degraded_capabilities``（``draft`` 是调用
方持有的同一个对象，原地改写、不需要回传）；否则返回修改意见文本，调用方据此
把同一个 ``task_payload`` 构造逻辑用这份意见重新跑一次、拿到重写稿后再调用本
函数一次——``INLINE_MAX_ATTEMPTS = 2`` 即每段最多两次生成尝试（1 次初稿 + 最多
1 次重写），与旧设计「每段最多重写一次」同一上限，只是不再需要单独的重生成
通道：``task_payload`` 本来就支持按 ``revision_notes`` 拼一条正面陈述规则
（``storyboard_revision_notes.segment_rule_text``），边写边审只是把这份能力从
「只服务 storyboard_identity_regenerate 的单段重生成」扩展成「调用方自己的循环
内也能用」，不新造一条模型调用通道。重写调用本身复用生成这一段已有的
format/semantic 重试与校验（不单独处理异常——它们和首次生成是同一段代码）。

## 并发与循环导入

复核与生成现在严格同步：第 N 段生成→复核→（可能）重写→复核，全部完成才轮到
第 N+1 段，不再有批量并发（见上「边写边审」changelog）。

依赖方向：本模块不导入 ``storyboard_pack``，``review_segment_inline`` 只读写
调用方传入的 ``draft``/``outcomes``，不持有、不回调任何 ``storyboard_pack`` 侧
的生成能力——单向依赖 ``storyboard_pack -> 本模块``，不需要任何一侧用函数内
延迟导入把循环藏进运行时（CLAUDE.md「函数内 import app.* 不是解耦手段」）。

## 可观测：`log_review_summary`

每段终态（是否重写、剩余违规数）由调用方累积进一个 ``outcomes`` 列表，整集
``_generate_all_segment_prompts`` 跑完后调一次本函数，打一条
``[STORYBOARD_PROSE_REVIEW_SUMMARY]`` 前缀的汇总日志，便于下次对比耗时与重写率；
``enabled=False``（开关关闭、或调用方是不接复核的 ``storyboard_identity_
regenerate``「修订本段」）时不打印。

## 开关

``storyboard_prose_review_enabled()``：全局设置键 ``storyboard_prose_review_
enabled``，默认开启（``app.config.DEFAULT_SETTINGS`` + ``app.monitoring.
SETTINGS_SCHEMA`` 两处登记，未声明的设置键写接口会拒写）；关闭时
``_generate_all_segment_prompts`` 的 ``enable_prose_review`` 由调用方按这个开关
决定是否传 ``True``（见 ``storyboard_pack.generate_storyboard_pack``），关闭后
不发起任何复核调用，产物与复核上线前逐字一致。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from app import textmatch
from app.db import get_setting
from app.harness import model_gateway
from app.production import storyboard_action_density as _action_density
from app.production import storyboard_prop_count as _prop_count
from app.production import storyboard_skin_blush as _skin_blush
from app.production.storyboard_continuity_memo import continuity_memo_payload
from app.production.storyboard_prose_review_rules import KIND_RULES as _KIND_RULES
from app.production.storyboard_staging_repeat import sub_shots

_LOGGER = logging.getLogger(__name__)

PROSE_REVIEW_SETTING_KEY = "storyboard_prose_review_enabled"
#: 复核调用只需要返回一份不长的违规清单，不是整段 prompt_text。
_REVIEW_ANSWER_TOKENS = 1800
#: 要求 previous_quote 必须逐字核验到的四类——本段开场状态与上一段末镜矛盾。
_NEEDS_PREVIOUS_QUOTE = {"screen_side", "prop_appearance", "repeated_transition_action", "opening_pose_break"}
#: previous_quote 核验对照上一段 continuity_memo.props（不是 prompt_text）的一类，
#: 见模块 docstring 2026-10-04 changelog。
_NEEDS_PREVIOUS_MEMO_QUOTE = {"prop_state_regression"}
#: 只在写实画风项目出现的两类——判据本身预设真人实拍物理约束（脸红的生理蔓延
#: 过程、摄影机的物理可达范围），动画/插画风格不受这两条约束。
_PHOTOGRAPHIC_ONLY_KINDS = {"skin_blush", "impossible_camera_move"}


class ProseViolation(BaseModel):
    """复核模型对一条违规的提名；是否采信由本模块的代码核验决定，见模块 docstring。"""

    kind: str = ""
    shot_label: str = ""
    quote: str = ""
    previous_quote: str = ""
    #: 只在 kind=prop_state_regression 时需要：previous_continuity_memo.props
    #: 里登记的 name，供代码核验定位对应条目——见 _prop_state_regression_quote_valid。
    prop_name: str = ""
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
    没有可比对的镜头文字，不强行比对 screen_side/prop_appearance/
    repeated_transition_action。"""
    if previous_draft is None:
        return ""
    shots = sub_shots(previous_draft.prompt_text)
    return shots[-1] if shots else ""


def _review_rules_text(*, photographic: bool, max_shots: int) -> str:
    """十二类判据的完整正面陈述；``skin_blush``/``impossible_camera_move`` 只在
    写实画风项目出现（见模块 docstring、``_PHOTOGRAPHIC_ONLY_KINDS``），非写实
    项目这两条规则连提示词都不会收到。"""
    kinds = [k for k in _KIND_RULES if k not in _PHOTOGRAPHIC_ONLY_KINDS or photographic]
    numbered = "\n".join(
        f"{i}. " + _KIND_RULES[kind].format(
            max_actions=_action_density.MAX_KEY_ACTIONS_PER_SHOT, skin_blush_rule=_skin_blush.SEEDANCE_SKIN_BLUSH_RULE,
            key_action_definition=_action_density.key_action_definition(),
            over_limit_remedy=_action_density.over_limit_remedy(max_shots=max_shots),
            prop_count_rule=_prop_count.SEEDANCE_PROP_COUNT_RULE,
        )
        for i, kind in enumerate(kinds, start=1)
    )
    return (
        "逐条核对下面这一段分镜正文（prompt_text）有没有出现以下几类问题；每一类都只在你能在正文里"
        "找到逐字证据时才报告，找不到就不要报告这一类，宁可少报不要编造。每条违规给出 kind（取值只能是"
        f"下面编号对应的英文名）、shot_label（这一镜的标签，例如『镜头2』）、quote（出问题的原文片段，"
        "必须逐字照抄 prompt_text 里的文字）、fix（怎么改）；screen_side、prop_appearance、"
        f"repeated_transition_action 与 opening_pose_break 四类还要给 previous_quote（逐字照抄"
        "『上一段末镜文字』里的对应原文）；prop_state_regression 另需给 previous_quote（逐字照抄"
        " previous_continuity_memo.props 里对应条目的 location/state 文本本身，不是『上一段末镜"
        f"文字』）与 prop_name（该道具/衣物在 previous_continuity_memo.props 里登记的 name，逐字照抄）。\n{numbered}"
    )


def _verbatim_in(quote: str, text: str) -> bool:
    condensed = textmatch.condense(quote or "")
    return bool(condensed) and condensed in textmatch.condense(text or "")


def _prop_state_regression_quote_valid(v: ProseViolation, previous_draft: Any | None) -> bool:
    """prop_state_regression 的 previous_quote 核验：逐字出自上一段
    continuity_memo.props 里 name 归一化匹配（``textmatch.condense``，容忍两次独立模型调用之间的空白/标点差异）
    的那一条 location/state 文本本身（不是上一段 prompt_text）。"""
    if previous_draft is None or not v.prop_name.strip():
        return False
    for prop in getattr(previous_draft.continuity_memo, "props", None) or []:
        if textmatch.condense(prop.name) == textmatch.condense(v.prop_name):
            return _verbatim_in(v.previous_quote, prop.location) or _verbatim_in(v.previous_quote, prop.state)
    return False


def _verified_violations(
    raw: list[ProseViolation], *, segment_no: int, draft: Any, previous_draft: Any | None,
) -> list[ProseViolation]:
    """模型提名、代码核验：kind 必须在 ``_KIND_RULES`` 里，quote 必须逐字核验到
    本段 prompt_text；screen_side/prop_appearance/repeated_transition_action/
    opening_pose_break 额外要求 previous_quote 逐字核验到上一段 prompt_text
    （没有上一段时这四类结构上不可能成立）；prop_state_regression 额外要求
    previous_quote 逐字核验到上一段 continuity_memo.props 对应条目（见
    _prop_state_regression_quote_valid）。
    核验不过的条目单独丢弃，不拖累同段其余已核验违规，可见日志见模块 docstring。"""
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
        if v.kind in _NEEDS_PREVIOUS_MEMO_QUOTE and not _prop_state_regression_quote_valid(v, previous_draft):
            _LOGGER.warning("[STORYBOARD_PROSE_REVIEW_UNVERIFIED] 第 %s 段 kind=%s 的 previous_quote 在上一段 continuity_memo.props 里找不到逐字证据，丢弃：%r", segment_no, v.kind, v.previous_quote[:200])
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


def _revision_notes_text(violations: list[ProseViolation]) -> str:
    """把已核验违规整理成正面修改意见，交给 ``_revision_notes.segment_rule_
    text`` 包装（该函数由 ``_generate_all_segment_prompts`` 内部调用，本模块不
    重复包装）。"""
    lines = [f"{i}. [{v.kind}] {v.shot_label or '对应镜头'}：{v.fix}（原文问题片段：{v.quote[:80]}）" for i, v in enumerate(violations, start=1)]
    return "分镜正文复核发现以下问题，请逐条修正，其余镜头与画面保持不变：\n" + "\n".join(lines)


def _remaining_advisory_texts(violations: list[ProseViolation]) -> list[str]:
    return [f"[STORYBOARD_PROSE_REVIEW_REMAINING][未拦截] [{v.kind}] {v.shot_label or '对应镜头'}：{v.fix}（原文：{v.quote[:80]}）" for v in violations]


INLINE_MAX_ATTEMPTS = 2
"""每段最多两次生成尝试：第一次 + 复核违规触发的一次重写，见模块 docstring
「边写边审」。"""


async def review_segment_inline(
    draft: Any, *, previous_draft: Any | None, episode_id: str, segment_no: int,
    photographic: bool, max_shots: int, attempt: int, enabled: bool,
    outcomes: list[dict[str, Any]] | None = None,
) -> str:
    """调用方每生成一次草稿后调用一次，见模块 docstring「边写边审」。没有已核验
    违规、或已到 ``INLINE_MAX_ATTEMPTS - 1``（最后一次尝试）时返回空串收尾——
    后者先把剩余违规写进 ``draft.degraded_capabilities``（原地改写调用方持有的
    同一个对象，不需要回传）；否则返回修改意见文本，调用方据此重新生成一次再
    调用本函数一次。``enabled=False``（开关关闭，或调用方是不接复核的
    ``storyboard_identity_regenerate``「修订本段」）原样返回空串，不发起任何
    复核调用。复核调用本身失败时 ``_review_segment`` 已吞成空列表并记
    ``[STORYBOARD_PROSE_REVIEW_FAILED]``，这里按「没有违规」处理，不重写不
    阻断。``outcomes`` 非空时追加一条本段终态记录，供 ``log_review_summary``
    打汇总日志。"""
    if not enabled:
        return ""
    raw = await _review_segment(
        episode_id=episode_id, segment_no=segment_no, draft=draft, previous_draft=previous_draft,
        photographic=photographic, max_shots=max_shots,
    )
    violations = _verified_violations(raw, segment_no=segment_no, draft=draft, previous_draft=previous_draft)
    if violations and attempt < INLINE_MAX_ATTEMPTS - 1:
        return _revision_notes_text(violations)
    if violations:
        draft.degraded_capabilities = [*draft.degraded_capabilities, *_remaining_advisory_texts(violations)]
    if outcomes is not None:
        outcomes.append({"segment_no": segment_no, "rewritten": attempt > 0, "remaining": len(violations)})
    return ""


def log_review_summary(
    outcomes: list[dict[str, Any]], *, episode_id: str, enabled: bool,
    seam_rewritten: int = 0, seam_remaining: int = 0,
) -> None:
    """整集逐段生成结束后打一条汇总日志，便于下次对比耗时与重写率（见模块
    docstring「可观测」）；``enabled=False`` 或 ``outcomes`` 为空时不打印。
    ``seam_rewritten``/``seam_remaining``（2026-10-01 换场并行链）：接缝复核
    触发的链首段重写数 / 复核后仍剩余的违规数，默认 0——只有
    ``app.production.storyboard_segment_chains`` 真正并行出多条链时才非零，
    1 条链（没有换场）时与本次改动前逐字一致。"""
    if not enabled or not outcomes:
        return
    rewritten = sum(1 for o in outcomes if o["rewritten"])
    remaining = sum(o["remaining"] for o in outcomes)
    _LOGGER.info(
        "[STORYBOARD_PROSE_REVIEW_SUMMARY] episode=%s segments=%s rewritten=%s remaining_violations=%s "
        "seam_rewritten=%s seam_remaining=%s",
        episode_id, len(outcomes), rewritten, remaining, seam_rewritten, seam_remaining,
    )
