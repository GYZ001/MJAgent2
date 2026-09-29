"""分镜台：两条静态、无条件生效的方言追加规则（2026-09-28，《顾念长安》第 1 集真实回归驱动）。

不改 ``storyboard_dialects.py``（499/500 行，新增逻辑没有余量）；做法照抄
``storyboard_action_beats.decisive_action_dialect_rule``——两个模型方言各自的文案在阶段二
对每一段都无条件拼进 ``dialect_instructions``，不依赖任何提名。

规则一（收尾镜人物可见）：``SEEDANCE_DIALECT_INSTRUCTIONS`` 已有「若这是全片收尾段，最后一
镜必须是大远景或缓慢升起拉远的格局镜」，但没有要求人物必须在画面里——第 30 段（全片收尾段）
最后一镜的大远景里人物完全消失，观众看不到任何人物落点。这条规则是既有收尾镜规则之外的补充
（人物是否可见），不是替换（景别本身不变），因此以追加而不是编辑既有句子的方式接线。

规则二（非生命体拟物材质）：第 25 段「白色纸鹤」被画成了会振翅的真鸟——画面里的拟人/拟物
道具（纸鹤、纸人、布偶、雕像、剪影这类不是活体、却容易被误画成真实生物或真人的物件）此前没
有任何规则要求写清它的材质与制作形态，模型于是按「像什么就画成什么」的默认倾向把折纸画成了
活体。
"""
from __future__ import annotations

SEEDANCE_ENDING_VISIBILITY_RULE = (
    "若这是全片收尾段，最后一镜的大远景/缓慢升起拉远格局镜里，仍在场的人物必须清晰可见：写明"
    "每个人此刻所在的具体位置（画面哪个方位、站着/坐着/倒地/远去背影这类具体状态）与正在做的"
    "动作，并继续用 @正名 点名——不能只写景别与环境，让人物在最后一镜里凭空消失。"
)

SEEDANCE_INANIMATE_MATERIAL_RULE = (
    "画面里出现的非生命体拟人/拟物道具（纸鹤、纸人、布偶、雕像、剪影这类不是活体、却容易被"
    "误画成真实生物或真人的物件）必须在它出现的每一镜写清材质与制作形态（折纸的纸张纹理与"
    "棱角、布料缝制的绒毛质感、石雕/木雕的表面纹理这类），并让这套材质特征保持不变，不能让"
    "画面把它渲染成活体的真实版本（例如白色纸鹤画成振翅的活鸟）。"
)

MINIMAX_H3_ENDING_VISIBILITY_RULE = (
    "If this is the finale segment, the closing extreme-wide/slow-craning-back establishing "
    "Shot must still show every remaining character clearly: state each one's concrete "
    "position in frame (screen-left/right, standing/seated/collapsed/walking away with their "
    "back turned) and what they are doing, keeping the @Name mention -- do not describe only "
    "the scale and environment and let a character vanish from the final Shot."
)

MINIMAX_H3_INANIMATE_MATERIAL_RULE = (
    "A non-living prop shaped like a person or animal (a paper crane, a paper doll, a cloth "
    "puppet, a statue, a silhouette -- anything easily mistaken for a living creature or a "
    "real person) must state its material and how it is made in every Shot it appears in "
    "(folded-paper texture and creases, sewn-fabric plush texture, carved stone/wood grain) "
    "and keep that material unchanged -- do not let the render turn it into a living version "
    "of itself (e.g. a white paper crane flapping its wings like a real bird)."
)


def shot_mandates_dialect_rule(render_format: str) -> str:
    """按目标视频模型方言选对应文案（收尾镜人物可见 + 非生命体拟物材质），拼进
    ``dialect_instructions``；``render_format`` 取值见
    ``app.video_prompt_profiles.VideoPromptProfile.render_format``，选择逻辑与
    ``decisive_action_dialect_rule`` 一致。"""
    if render_format == "minimax_h3_native_fields":
        return f"{MINIMAX_H3_ENDING_VISIBILITY_RULE}\n{MINIMAX_H3_INANIMATE_MATERIAL_RULE}"
    return f"{SEEDANCE_ENDING_VISIBILITY_RULE}\n{SEEDANCE_INANIMATE_MATERIAL_RULE}"


__all__ = [
    "SEEDANCE_ENDING_VISIBILITY_RULE",
    "SEEDANCE_INANIMATE_MATERIAL_RULE",
    "MINIMAX_H3_ENDING_VISIBILITY_RULE",
    "MINIMAX_H3_INANIMATE_MATERIAL_RULE",
    "shot_mandates_dialect_rule",
]
