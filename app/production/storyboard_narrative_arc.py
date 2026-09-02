"""分镜台 2.2.0「结构层」三项改造的提示词文案与载荷构造（用户拍板 2026-09-01，
依据专家对真实 EP1 十八段产出的审阅）。

拆出原因：``app.production.storyboard_pack`` 已经在
``app/FILE_CONVENTIONS.toml`` 的 ``line_count`` 棘轮基线上（2085 行，零余量）。
本次三项改造要新增的提示词文案（字符串本身占大量行数）与载荷构造函数放在这里，
不占用主文件的行数预算；``_segment_continuity_rules`` 是一个纯函数（无状态、
不依赖 ``storyboard_pack`` 任何私有对象），同期从那边搬移过来腾出空间——这类
「移出无关但自成一体的纯函数给新增逻辑腾行数」的做法在该文件历史上已有先例
（2.1.0 把两段方言指令搬到 ``storyboard_dialects.py``）。搬移不改变行为，
``storyboard_pack.py`` 通过 ``from app.production.storyboard_narrative_arc
import _segment_continuity_rules`` 继续在同一个名字下调用它，测试的既有导入
路径（``from app.production.storyboard_pack import _segment_continuity_rules``）
不受影响。

三项改造均由真实 EP1 十八段产出的专家审阅驱动：

① 首尾段标记（修 bug，不是新功能）——``storyboard_dialects.
SEEDANCE_DIALECT_INSTRUCTIONS`` 里早就有「若这是全片收尾段，最后一镜必须是
大远景或缓慢升起拉远的格局镜」这条规则，但阶段二 ``task_payload`` 从来没有
告诉模型它是第几段、全片共几段，这条规则因此永远没有触发条件——真实 EP1
段18（全片收尾）实际停在人物全景上，不是大远景。修法是给 payload 补
``is_first_segment``/``is_final_segment`` 两个布尔字段，并在 rules 里补一句
正面陈述指向它们的含义，让已经写好的方言规则真正有信号可读，不改方言规则
本身（``storyboard_dialects.py`` 不在本次改造范围内，另有并行改动）。

② 色温弧线（palette）——EP1 段6-7（孟浩扔葫芦→重新振作）本该有一次冷调
（青灰）转暖调（夕阳暖金）的色温转折，段13（遇仙）本该转成高对比青绿银白，
而实际十八段全程停在同一个色调；同一时间段6/7 的配乐已经换成「舒缓憧憬/
沉稳有力」，画面却没有跟着换，形成音画不同步。做法分两阶段：阶段一（节拍
表）要求模型为每一段写一句色温/色调方向（开放词汇，不设枚举——CLAUDE.md
禁止黑白名单式穷举，色温描述本就是无限开放的自然语言），在情绪或世界观
转折处让相邻段的色温方向明显拉开差异；阶段二把本段与上一段各自的 palette
值一并喂回去，色温不同时要求模型在本段开头约两秒内写出渐变过程本身（光线
变化、色调过渡、环境对光线的反应），不允许从目标色温直接起手——转折必须
发生在本段自己的画面里，因为逐段独立调用下模型看不到下一段会写什么，把
渐变留给下一段等于让它永远不会被画出来。

③ 独白压缩与闪回——EP1 前 5 段（占全片 18 段的 28%）全部是「孟浩坐山顶
皱眉攥葫芦」同一情绪状态的静态独白反复咀嚼，相当篇幅还花在「有钱之后去
东土大唐」这类根本无法视觉化的空想上；与此同时全章真正的钩子——掳人与
飞行——只分到约 30 秒。做法是给阶段一 rules 补两条正面陈述（合并授权 +
高潮展开授权），给阶段二 rules 补一条闪回边界：同一人物同一情绪的连续静态
独白全片最多合并进约 2 个段，「不可视觉化」与「不承载因果/动机/关键设定」
两个条件同时成立才允许不进节拍（不推翻既有的「承载因果的独白改画外音」
规则，是它的一个显式子集）；高潮/转折节拍可以拆成 2-3 个段展开，省下的
时长优先给它们；画外音段落的画面优先用闪回意象承载信息（例如反复翻看的
书页、见底的米缸这类具体物件），闪回内容只能取材本段或前文原文里实际出现
过的意象，不得编造原文没有的场景——这是硬边界，防止模型在「找不到东西可
画」时兜底编一个画面；闪回画面同样受本段色温方向约束，不能靠闪回逃开①②
两项规则。
"""
from __future__ import annotations

from typing import Any


def beat_sheet_narrative_arc_rules() -> list[str]:
    """阶段一（节拍表）rules[] 新增的三类正面陈述：色温弧线的起点、独白/
    情绪节拍的合并授权、高潮节拍的展开授权。三者都是「怎么归组、怎么分配
    时长」的判断，天然属于决定段数与节拍归组的阶段一，不是阶段二逐段渲染
    时才决定的事——完整真实案例见本模块 docstring。
    """
    return [
        (
            "为每一个段构思一句色温/色调方向（例如「冷调青灰」「夕阳暖金」"
            "「高对比青绿银白」，不必套用这三个例子，按剧情自行拟定）；在"
            "情绪或世界观出现转折的地方，让转折前后相邻两段的色温方向明显"
            "拉开差异，不要让全片停留在同一个色调——这句色温方向就是"
            "segments[].palette 字段的内容，供下一阶段写进画面。"
        ),
        (
            "同一人物在同一情绪状态下的连续静态独白（没有新信息、没有场景"
            "或动作变化，只是同一个念头反复咀嚼）：全片范围内最多合并进约 "
            "2 个节拍/段承载，不必让情绪停留的每一处都单独占一个段。"
            "不可视觉化、且同时不承载因果关系/人物动机/关键设定的独白内容"
            "（例如空想式的碎碎念），可以不进入任何节拍——判据是两个条件"
            "同时成立：既无法转成画面，也不影响读者理解因果/动机/设定；"
            "这不推翻上面「保留进节拍、改写成画外音」那条规则，只要其中"
            "任意一个条件不成立（能视觉化，或者承载了因果/动机/设定），"
            "仍然要保留进节拍。"
        ),
        (
            "冲突密集或世界观发生转折的高潮节拍，可以拆成 2-3 个段展开"
            "承载，不必和其它节拍平分时长——上面合并静态独白省下的段数/"
            "时长，优先分配给这类节拍，让真正的钩子（冲突、转折、悬念"
            "揭晓）获得与其重要性匹配的篇幅，不要被压缩进和一句平淡独白"
            "同样长的一个段里。"
        ),
    ]


def segment_narrative_arc_payload_fields(
    *, segment_no: int, total_segments: int, palette_current: str, palette_previous: str,
) -> dict[str, Any]:
    """阶段二 task_payload 里叙事弧线相关的四个字段，一次性构造、一处调用，
    不把判断逻辑摊在调用方（``_generate_all_segment_prompts``，已在
    function_lines 棘轮基线上，不应继续变长）。

    ``is_first_segment``/``is_final_segment`` 修的是一个真实 bug：方言约束
    里已有的「全片收尾段」镜头要求此前没有任何信号可读；``palette_current``/
    ``palette_previous`` 是色温弧线，供 ``segment_narrative_arc_rules``
    判断本段要不要写渐变过程——完整推导见本模块 docstring。
    """
    return {
        "is_first_segment": segment_no == 1,
        "is_final_segment": segment_no == total_segments,
        "palette_current": palette_current,
        "palette_previous": palette_previous,
    }


def segment_narrative_arc_rules(*, palette_current: str, palette_previous: str) -> list[str]:
    """阶段二 rules[] 新增的正面陈述：首尾段含义（恒定出现）、色温渐变
    （只在本段色温与上一段不同时出现）、闪回边界（恒定出现）。
    """
    rules = [
        "task_payload 里的 is_first_segment 为 true 表示本段是全片开场段，"
        "is_final_segment 为 true 表示本段是全片收尾段：上面方言约束里任何"
        "专门针对「全片收尾段」的镜头要求（例如收尾镜别、构图），在 "
        "is_final_segment 为 true 时必须满足，不必也不应该由你自己再判断"
        "这是不是最后一段。",
    ]
    if palette_current and palette_current != palette_previous:
        rules.append(
            f"本段的色温/色调方向是「{palette_current}」，与上一段"
            f"「{palette_previous or '（本集第一段，没有上一段可比）'}」不同："
            "不要让画面在本段一开始就直接呈现目标色温，而要在本段开头约两秒"
            "内写出色温渐变的过程本身（光线变化、色调过渡、环境对光线的"
            "反应），转折必须发生在本段画面里——你看不到下一段会写什么，把"
            "渐变留给下一段等于让它永远不会被画出来。"
        )
    rules.append(
        "如果本段有台词是画外音（dialogue[].delivery=offscreen_voice）："
        "画面优先用闪回意象承载画外音陈述的信息（例如灯下反复翻看的书页、"
        "见底的米缸这类具体物件或场景），闪回画面里出现的意象只能取材本段"
        "或前文原文里实际出现过的场景/物件，不得编造原文没有写过的场景——"
        "这是硬边界，查不到原文依据的闪回画面不允许出现；闪回画面同样必须"
        "遵守本段的色温方向，不能借闪回逃开上面两条约束。"
    )
    return rules


def _segment_continuity_rules(
    *,
    previous_segment_no: int | None,
    camera_history: list[dict[str, Any]],
) -> list[str]:
    """一镜参考的第一、二层文案（第三层——世界书外观锚点——在
    ``_generate_all_segment_prompts`` 的 shared_rules 里，逐段调用同样适用）。

    按 CLAUDE.md「Prompts」一节的要求写：正面陈述而非禁令，说清参考素材从
    哪来，以及确实没有时该怎么写——本集第一段没有上一段、没有镜头语言历史，
    两种情况都直接说清楚，不假装存在一个不存在的参照。

    2.2.0：从 ``storyboard_pack.py`` 搬移到本模块（纯搬移，行为不变），
    为该文件新增的三项结构层改造腾出行数——见本模块 docstring 开头的说明。
    """
    if previous_segment_no is not None:
        rule_1 = (
            f"previous_segment_prompt 是上一段（第 {previous_segment_no} 段）"
            "已经生成、定稿的提示词全文，不会再被改写。本段与它的关系由你自己"
            "判断：如果本段发生在与上一段相同的空间、紧接着的时间点，本段的"
            "起幅要能承接上一段结尾的画面（同一场景、同一光影，人物姿态自然"
            "接续，不要凭空跳到一个新姿势或新机位）；如果本段换了空间或跳过"
            "了一段时间，本段的起幅要让观众能明确感知到这次切换（用新的场景"
            "描述、光影变化，或者一个专门的转场镜头交代），不能让两段读起来"
            "像是从互不相干的素材里各剪一段拼起来的。"
        )
    else:
        rule_1 = "本段是本集第一段，没有上一段可参考，起幅由你自行判断，不必与任何前情衔接。"
    if camera_history:
        history_nos = [item["segment_no"] for item in camera_history]
        rule_2 = (
            f"recent_camera_language 列出了最近 {len(camera_history)} 段"
            f"（第 {history_nos} 段）各自的开场景别与运镜。本段的开场景别、"
            "运镜请从这份清单以外挑一个组合，让画面持续推进；如果这一段的"
            "剧情确实需要沿用清单里出现过的某个机位（例如同一场戏的正反打、"
            "同一场追逐的连续对切），把理由写进 camera_repetition_rationale"
            "（例如「与第 X 段是同一场对话的正反打，沿用同一组机位是刻意"
            "的」）；如果这一段没有这种必要，camera_repetition_rationale "
            "留空即可，不必强行解释。"
        )
    else:
        rule_2 = (
            "本集到本段为止还没有可参考的镜头语言历史，camera_digest 与 "
            "camera_repetition_rationale 按本段实际情况据实填写、留空即可，"
            "不必刻意呼应任何东西。"
        )
    return [rule_1, rule_2]
