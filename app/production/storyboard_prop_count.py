"""分镜台阶段二：同一件道具不得凭空分身（2026-10-01，第 1 集修订本段验收后的
第二轮逐帧复查——原分辨率帧反向核验，发现与 ``storyboard_cast_lock`` 同源的新
缺陷类别）。

生产证据：第 20 段镜头 4，原文/分镜只写温念拖着行李箱、顾屿空手领路，成片里顾屿
手里却凭空多拉了一只与温念那只外观一模一样的深卡其色行李箱；第 28 段拉远全景时，
床头柜旁多出第二只行李箱，同时床尾原有那只也还在——同一画面里一件道具同时出现了
两份。这两段的行李箱都已经在 ``resources.props`` 排第一位、参考图也送对了（颜色/
材质这次都对，见 ``storyboard_prop_visibility``），问题不是"外观错了"，是"数量
错了"——道具的"分身"问题与人物的"分身"问题（背景冒出撞脸路人，见
``storyboard_cast_lock`` 模块 docstring）同源：模型知道这件道具该出现在这场戏里，
但没有把它当成"全场唯一一件、只能跟着一个持有人/一个位置走"的单一实体，而是在
每一镜需要道具出镜时各自现画一份。

与 ``storyboard_cast_lock`` 的关键差异——为什么这里不生成确定性锁定句：
``storyboard_cast_lock`` 能把"共 N 人"写成一句写死数字的确定性文本，是因为
``resources.characters`` 里每一条都结构化地代表"一个人"（按 identity_id 去重即
人数），这个"一条 = 一个实体"的映射是 schema 本身保证的。``resources.props``
不是这样：``_AiResourceProp`` 只有 ``label``/``description`` 两个自由文本字段，
没有数量字段，一条记录既可能代表"一件道具"，也可能代表模型自己合并描述的"一组
道具"（例如 label 直接写成「两碗馄饨」「两个勺子」）——"这一集 resources.props
列出的道具有几条" 不等于 "画面里应该有几件实物"，数量这件事本身没有任何结构化
字段可读，无法像人数那样从 ``len(resources.props)`` 可靠推出。强行假设"每条一件"
会在遇到本来就该画多件的道具（两碗馄饨、两个勺子）时产生新的误伤——把它们错误
地也锁成一件。因此本模块只提供规则正面陈述（教模型自己在写正文时把数量钉死），
不生成任何写死数字的确定性句子；数量到底是几件，交给模型读原文自己判断，与
``storyboard_cast_lock`` 处理"有几个人"的方式分叉，但判断依据（原文写了几个）
是同一件事。

接线方式照抄 ``storyboard_shot_mandates.py``「静态、无条件、按 render_format 选
方言」的先例：两个模型方言各自的文案在阶段二对每一段都无条件拼进
``dialect_instructions``，不依赖任何提名、不按画风分支（道具分身与写实/非写实
画风无关，``storyboard_skin_blush`` 那种只在写实渲染才成立的限定不适用于这里）。

``storyboard_prose_review`` 同批接入新判据 ``prop_duplication``：真实故障里"第二份
道具"与"第一份道具"的描述都在同一段 ``prompt_text`` 内部（温念那只箱子与顾屿凭空
多出的那只在同一段正文里），复核模型只需要通读这一段正文本身就能判断是否存在"同一
件道具被分别写进了两个人手里/两个位置"这类自相矛盾，不需要比对上一段文字或原文
摘要，因此不需要 ``previous_quote``，与 ``action_density``/``unvoiced_speech`` 同
一套"单段内部自洽性"判据，不是 ``screen_side``/``prop_appearance``/
``repeated_transition_action`` 那套跨段判据。判据文本单源指向本模块的
``SEEDANCE_PROP_COUNT_RULE``。
"""
from __future__ import annotations

SEEDANCE_PROP_COUNT_RULE = (
    "resources.props 里每一件道具，画面里实际出现的件数必须与原文一致：原文只写了一件"
    "（例如「她的行李箱」「那只行李箱」），这一件道具自始至终只有一件，跟着原文（或上一段"
    "末镜）交代的持有人或所在位置走——不能因为另一个人也在场、也可能用得上这类道具，就"
    "顺手让他手里或他那一侧也凭空多出一件外观相同的同类道具，画面里同一时刻只能有这一件"
    "实物，不能在两个人手里或两个位置各出现一份。原文写了几件（例如「两碗馄饨」「两个"
    "勺子」）就照样画几件，不多画也不少画。判断一件道具这一镜该不该出现在某个人手里或"
    "某个位置，依据是原文或上一段末镜交代的持有人/位置，不是"
    "「这个人也在场、这一镜也用得上，所以也该给他配一件」。"
)

MINIMAX_H3_PROP_COUNT_RULE = (
    "For every prop in resources.props, the number of physical copies actually shown on "
    "screen must match what the source text establishes: if the source text names only one "
    "(e.g. \"her suitcase\", \"that suitcase\"), there is exactly one of it throughout, and it "
    "stays with whichever character or location the source text (or the previous segment's "
    "closing shot) assigns it to -- do not let a second, visually identical copy of the same "
    "prop appear in another character's hands or at another spot just because that character "
    "is also present and could plausibly be carrying or using one too; only one physical copy "
    "exists at any given moment, never two in two different hands or two different spots at "
    "once. If the source text establishes multiple copies (e.g. \"two bowls of wontons\", "
    "\"two spoons\"), show exactly that many -- never more, never fewer. Whether a prop should "
    "appear in a given character's hands or at a given spot in this Shot is decided by who the "
    "source text (or the previous segment's closing shot) assigns it to, not by \"this "
    "character is also in the scene so they might as well have one too\"."
)


def prop_count_dialect_rule(render_format: str) -> str:
    """按目标视频模型方言选对应文案；不区分画风、不依赖任何提名，接线方式照抄
    ``storyboard_shot_mandates.shot_mandates_dialect_rule``——两个模型方言各自的文案在
    阶段二对每一段都无条件拼进 ``dialect_instructions``。"""
    if render_format == "minimax_h3_native_fields":
        return MINIMAX_H3_PROP_COUNT_RULE
    return SEEDANCE_PROP_COUNT_RULE


__all__ = [
    "SEEDANCE_PROP_COUNT_RULE",
    "MINIMAX_H3_PROP_COUNT_RULE",
    "prop_count_dialect_rule",
]
