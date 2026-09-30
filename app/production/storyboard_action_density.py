"""P0：单镜动作密度上限（2026-09-30，真实回归第1集第1/8/12段驱动）。

背景：第1段一个15秒段塞了"睡着→火花→惊醒→冲去拔插头→被烫→看墙角→回床边穿开衫→
穿鞋→拔充电线→拖盆→接水→打电话"十余个动作，视频反复编造家具（床头柜）、人坐在
柜子上、床面积水、插座起火；第12段镜头1塞了"推门→开灯→跟进→看照片→走到沙发→
脱大衣→换卫衣→转身→穿过客厅"十余个动作，视频出现两个不相连的空间；第8段镜头1
"走三步+逐颗扣纽扣+直身+示意"同一根因。人工把动作拆开/精简后错乱消失——一镜（约
3-4秒）被要求实现过多动作时，视频模型会编造画面"跟上"指令，不是提示词写法问题，
是单镜信息密度超过了模型的物理承载上限。

判据不是关键词枚举（CLAUDE.md「判据从数据推导」）：阶段二模型在产出 prompt_text
的同时，为自己写的每一镜结构化申报一份「本镜关键动作」清单
（``_AiStoryboardSegmentDraft.shot_action_beats``），代码只核验这份自报清单每镜
条数是否超过 ``MAX_KEY_ACTIONS_PER_SHOT``——模型提名、代码核验，不猜动词语义。
字段带 ``default_factory=list`` 默认值：``storyboard_identity_regenerate`` 会把
旧 ``shots.shot_contract_json``（落库形态不含这个字段，见下方「不落库」）重建成
``_AiStoryboardSegmentDraft`` 以复用其它段落，没有默认值会在那条路径上直接校验
失败。

不落库：与 ``_AiCameraDigest``（见 ``storyboard_pack`` 模块内该类文档）同一先例，
这份申报只服务于「怎么生成」，不写入 ``StoryboardPackSegment``/``shots`` 表，
分镜产出的持久化形状不变；超限放行后的可见信号走既有 ``degraded_capabilities``
（``segment_advisories``），不是只写后端日志（CLAUDE.md「闸门放行分支必须是
产品里的可见信号」）。

不声明/清空自报同样要有信号：``action_density_errors`` 对空清单天然返回
``[]``（列表推导式在空输入上就是空），如果只有这一条判据，模型可以靠完全
不申报、或在语义重试时把某一镜的 ``key_actions`` 清空来让闸门放行，而不必
真的把画面密度降下来（CLAUDE.md「空集合不等于无需检查」）。``undeclared_
shot_errors`` 是独立的第二条判据：申报的 ``shot_no`` 必须覆盖 ``1..shot_
count`` 且每条 ``key_actions`` 非空，否则视为「没有真的申报」。它接入
``ActionDensitySoftCheck.filter`` 的同一套 ``hard_attempts`` 重试-降级节奏
（给模型真实机会去补申报，不是一上来就判死），最终仍未覆盖时，
``segment_advisories`` 用区别于「超限」的独立标记
``STORYBOARD_PACK_ACTION_DENSITY_UNDECLARED`` 写进 ``degraded_capabilities``
——不与「真的检查过且合规」共用同一个空列表出口。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

#: 15 秒段固定 2-4 镜（MIN/MAX_SHOTS_PER_SEGMENT，见 storyboard_pack），4 镜时
#: 每镜约 3-4 秒：一个动作起止加镜头运动至少要 1.5-2 秒才看得清，一镜超过 2 个
#: 关键动作意味着平均每个动作不到 2 秒——真实回归里十余个动作塞进 4 镜（每镜
#: 3+ 个）正是视频模型编造家具/空间错乱的直接诱因，见模块 docstring。
MAX_KEY_ACTIONS_PER_SHOT = 2


class ShotActionBeats(BaseModel):
    """阶段二模型为 prompt_text 里某一镜自报的关键动作清单，按发生顺序。"""

    shot_no: int = Field(ge=1, description="对应 prompt_text 里第几镜，从 1 开始")
    key_actions: list[str] = Field(
        default_factory=list,
        description="这一镜里的每一个关键动作，按发生顺序各写一个短语；换装/开关灯/走位都算一个动作",
    )


def shot_action_beats_rule(max_per_shot: int = MAX_KEY_ACTIONS_PER_SHOT) -> str:
    """阶段二正面陈述：declare 字段怎么写、超限时怎么正确应对。"""
    return (
        "shot_action_beats：为 prompt_text 里写到的每一镜各申报一条（shot_no 从 1 开始，与镜头顺序"
        "对应），key_actions 按发生顺序列出这一镜里的每一个关键动作（换装、开关灯、走位、拿起/放下"
        f"物品都算一个动作）。一镜最多写 {max_per_shot} 个关键动作——15 秒段每镜只有约 3-4 秒，装不下"
        "更多：动作多的时候，把它们拆成更多镜头，或者把非关键动作交给镜头之间的硬切省略，例如换装"
        "不需要拍换的过程，用硬切直接呈现换好后的样子。"
    )


def action_density_errors(
    shot_action_beats: list[ShotActionBeats], *, max_per_shot: int = MAX_KEY_ACTIONS_PER_SHOT,
) -> list[str]:
    """阻断判据：申报的动作数是否超过上限，只数模型自己报的条数。"""
    return [
        f"镜头 {beat.shot_no} 申报了 {len(beat.key_actions)} 个关键动作"
        f"（{'、'.join(beat.key_actions)}），超过每镜 {max_per_shot} 个的上限：请把这一镜拆成更多"
        "镜头，或把非关键动作交给镜头之间的硬切省略（例如换装用硬切直接呈现换好后的样子）"
        for beat in shot_action_beats
        if len(beat.key_actions) > max_per_shot
    ]


def undeclared_shot_errors(
    shot_action_beats: list[ShotActionBeats], *, shot_count: int,
) -> list[str]:
    """第二条判据：覆盖度而不是条数——申报的 ``shot_no`` 必须覆盖
    ``1..shot_count``，每条 ``key_actions`` 非空，否则视为没有真的申报
    （见模块 docstring「不声明/清空自报同样要有信号」）。``key_actions``
    为空的镜头与完全没出现在清单里的镜头同等对待：两者都是零信息，都不能
    被读成「已核验且合规」。
    """
    declared = {beat.shot_no for beat in shot_action_beats if beat.key_actions}
    missing = sorted(set(range(1, shot_count + 1)) - declared)
    if not missing:
        return []
    return [
        f"镜头 {missing} 没有申报 key_actions（shot_action_beats 里缺这一镜，或申报了空清单）："
        "动作密度闸门依赖每一镜的自报，没有申报就无法核验这一镜是否超限"
    ]


class ActionDensitySoftCheck:
    """前 ``hard_attempts`` 次把超限/未申报当阻断打回模型重试，用尽后放行——
    与 ``StimulusVoiceSoftCheck``/``StagingSoftGate`` 同一套让步策略（见两者
    模块 docstring）。放行分支不是只写日志：``segment_advisories`` 独立重算
    同一份判据、写进 ``degraded_capabilities``。
    """

    def __init__(self, *, hard_attempts: int, segment_no: int) -> None:
        self.hard_attempts = hard_attempts
        self.segment_no = segment_no
        self.calls = 0

    def filter(self, shot_action_beats: list[ShotActionBeats], *, shot_count: int) -> list[str]:
        self.calls += 1
        problems = [
            *action_density_errors(shot_action_beats),
            *undeclared_shot_errors(shot_action_beats, shot_count=shot_count),
        ]
        if not problems or self.calls <= self.hard_attempts:
            return problems
        return []


def segment_advisories(shot_action_beats: list[ShotActionBeats], *, shot_count: int) -> list[str]:
    """非阻断，供 ``storyboard_pack`` 合并进 ``degraded_capabilities``——超限
    判据与 ``ActionDensitySoftCheck`` 同源（``action_density_errors``），独立
    重算；未申报判据同样独立重算，但用区别于「超限」的独立标记
    （``_UNDECLARED``），不与「真的检查过且合规」共用同一个空列表出口。
    """
    return [
        f"[STORYBOARD_PACK_ACTION_DENSITY][未拦截] {error}"
        for error in action_density_errors(shot_action_beats)
    ] + [
        f"[STORYBOARD_PACK_ACTION_DENSITY_UNDECLARED][未拦截] {error}"
        for error in undeclared_shot_errors(shot_action_beats, shot_count=shot_count)
    ]
