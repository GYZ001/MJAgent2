"""分镜台：道具/衣物跨段状态续接——起幅正面陈述 + 备忘道具名核验（P0，
2026-10-04，用户反馈《顾念长安》EP1 第 1→2 段驱动：温念拔下插头、插座两孔
空着，下一段画面里插头又插回了插座）。

## 为什么不新增字段

``continuity_memo.props[].location``/``state``（2026-09-03 扩展，见
``app.production.storyboard_continuity_memo`` 模块 docstring）已经能表达
「插头-已拔下-躺在地上」这类语义，调查（B 生产库 ``ep_a3c61162b4ce`` 段1/2
逐字核对）确认模型在这个真实案例里**两边都写对了**——上一段备忘记对了，
下一段起幅也写对了。缺的不是字段，是两道工序：

1. **起幅正面陈述**：``storyboard_narrative_arc._segment_continuity_rules``
   现有的起幅规则是泛化的（场景/光影/姿态），没有专门点名「道具」——
   ``opening_shot_prop_state_rule`` 补这一条，要求本段第一镜把上一段备忘里
   继续出场的道具/衣物状态逐条抄一遍，与 wardrobe/layout 续接同一套「默认
   沿用、原文驱动改变」纪律。
2. **备忘道具名核验**：``continuity_memo.props[].name`` 此前没有任何校验，
   模型可以写一个与本段任何已登记道具/衣物都对不上的名字，下游没有办法
   确认这到底是哪件道具——``prop_name_advisories`` 补这一条（模型提名、
   代码核验，非阻断：道具命名的自由度本就比外观锚点宽，强行阻断会重演
   layout 判据曾经一刀切打死整集的教训，见
   ``storyboard_continuity_memo.layout_change_advisories`` 文档）。

## 视频侧的已知残余风险

``app.media_exec.enqueue_context.apply_continuity_mode`` 的冻结决策（2.x 段
之间从不传递真实尾帧）意味着无论文字多明确，视频模型仍可能在渲染时回退
默认视觉——这是当前架构下的已知限制，不是本模块能靠改提示词彻底消除的；
对外文案一律使用「降低……概率」而非「保证能续接」（CLAUDE.md「界面承诺
必须与实际行为一致」）。
"""
from __future__ import annotations

from typing import Any

from app.production.storyboard_continuity_memo import _AiContinuityMemo


def _prop_state_fragment(p: Any) -> str:
    """只写这件道具实际记录了的那个字段；location/state 任一留空就不提那个
    字段——不写「未记录」这类说明性文字，避免模型把它当成要逐条照抄的位置/
    状态描述原样写进画面（与 ``storyboard_continuity_memo._WARDROBE_FIELD_RULE``
    「空着才是诚实的，不写说明性文字」同一纪律）。"""
    fields = "、".join(f"{label}={value}" for label, value in (("位置", p.location), ("状态", p.state)) if value)
    return f"『{p.name}』{fields}"


def opening_shot_prop_state_rule(previous_memo: _AiContinuityMemo | None) -> str | None:
    """起幅正面陈述：``previous_memo.props`` 里至少有一件记了 location 或
    state 才生成；全部留空时没有可续接的信息，返回 ``None``（调用方据此不拼
    这条规则，不是生造一句空话）。"""
    if previous_memo is None:
        return None
    recorded = [p for p in previous_memo.props if p.location or p.state]
    if not recorded:
        return None
    lines = "；".join(_prop_state_fragment(p) for p in recorded)
    return (
        f"上一段备忘（previous_continuity_memo.props）记录了以下道具/衣物在上一段结束时的"
        f"位置与状态：{lines}。本段第一个镜头（起幅）如果画面里会出现这些道具/衣物中的任意"
        "一件，必须先把它继续写成与上面记录一致的位置与状态——逐条照抄对应的位置/状态描述，"
        "不要默认回到这件道具/衣物的常见默认样子（例如插座常见默认是插着插头，如果上一段"
        "记录的是『已拔下』，本段起幅也要写成插头已拔下、插座两孔空着，不能因为「插着」更"
        "常见就画成插着）。只有本段原文明确写出了让它变化的具体动作（插回去、重新系上、"
        "再次合上……）时，才允许在动作发生之后呈现变化后的新位置/状态，并且要把这个变化"
        "动作本身写进画面——不能只呈现变化后的结果、略过动作过程。本段原文没有写到这件"
        "道具/衣物时，不必在起幅之外的镜头里继续提它，但只要它出现在画面里，就必须遵守"
        "上面这条续接要求。"
    )


def prop_name_advisories(
    memo: _AiContinuityMemo, resource_prop_labels: set[str], character_wardrobes: list[str],
) -> list[str]:
    """continuity_memo.props[].name 的代码核验（非阻断）：name 必须逐字等于
    某个 resources.props[].label，或逐字作为子串出现在某个人物当前 wardrobe
    描述里——模型自己写 name，代码只核验它能不能对应到本段已登记的真实道具/
    衣物，核验不过不改写、只记可见信号（不兜底猜一个更像的名字）。"""
    return [
        f"[STORYBOARD_PROP_CONTINUITY_NAME_UNKNOWN][未拦截] continuity_memo.props"
        f"『{prop.name}』不是本段 resources.props 的 label，也没有出现在任何人物当前"
        "wardrobe 描述里，无法确认这是本段已登记的哪件道具/衣物"
        for prop in memo.props
        if prop.name.strip()
        and prop.name not in resource_prop_labels
        and not any(prop.name in wardrobe for wardrobe in character_wardrobes if wardrobe)
    ]
