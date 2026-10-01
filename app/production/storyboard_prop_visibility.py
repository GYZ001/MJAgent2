"""分镜台阶段二：道具外观/参考图只服务画面里看得见的道具（2026-10-01，第 1 集第五版
35 段真实成片逐帧复查发现，与 ``storyboard_skin_blush``/``storyboard_shot_mandates``
同一次真实回归驱动）。

两类真实缺陷，根因同构：

1. 第 35 段原文「左手抬起，隔着卫衣按住胸前那枚星盘……此刻它被卫衣完全盖住，只在胸前
   正中隔着棉布顶出一个圆形轮廓」——星盘明明被原文写明完全遮住，分镜正文却照抄了它的
   完整标准外观（「主体为做旧黄铜材质星盘，直径约7cm，盘面刻有细密星轨纹路……配套深棕色
   磨旧皮质表袋」），且该段 ``resources.props`` 把「旧星盘」列了进去，于是它的参考图被
   当作本段参考图送给了视频模型（见 ``app.video_modes.prop_references.
   resolve_segment_prop_manifest_entries`` 与 ``prop_library_anchors``：参考图池只认
   ``resources.props`` 列了什么，不知道、也不核验这件道具在画面里实际看不看得见）。成片
   里星盘直接显形，材质、刻度、挂环清晰可见，与原文「完全盖住」矛盾。第 8 段同一件道具
   同样发生。根因是 ``storyboard_dialects.py``（第 111-122 行）「道具标准外观第一次出现
   时必须逐字沿用」与 ``storyboard_prop_appearance_lock.segment_rule_text``（全集外观
   锁定按 beat_id 命中就要求逐字沿用）这两条既有规则都只看"这件道具这一段在不在场"，不看
   "这一段画面里这件道具看不看得见"——道具被遮住、揣进包里、收在口袋，仍然"在场"，但观众
   看不到它的材质颜色形状，模型却被两条规则一起要求把那套看不见的外观写出来、连带参考图
   一起送。

2. 第 28 段画面里（门口、背景）清楚出现第 25/26 段那只「水泡坏的行李箱」（物件库已有卡片
   与参考图），但该段 ``resources.props`` 只报了热牛奶/小木星星/浅灰色卫衣，没有行李箱，
   模型因此没拿到参考图、自己现编外观，画成了另一只竖纹浅橄榄色的箱子，与前两段的箱子
   外观不一致。根因与上面对称：``segment_output_contract`` 对 ``resources`` 字段的说明
   只写「本段实际用到的人物/场景/道具」——"用到"这个措辞偏向"被动作直接涉及"，模型据此漏报
   了只出现在背景、没有人物与它互动的已登记道具，这件道具因此从来没有机会被判定为"看得见"
   并拿到参考图。

共同根因：两条既有规则都没有把"外观该不该写、参考图该不该送"这件事绑定到"这一段画面里
这件道具实际看不看得见"这个唯一判据上，而是分别绑定在"这件道具这一段在不在场"（锁定命中
beat_id）与"模型有没有觉得这件道具这段被用到"（措辞模糊）两个不精确的代理判据上。

修法：新增一条完整正面陈述——视觉可见性是唯一判据，覆盖两个方向：道具在本段某一镜画面里
实际可见（包括只出现在背景、没有人物与它互动）时，必须列进 ``resources.props`` 并写出
标准外观；道具被衣物/容器/包裹完全遮住、收在看不见的地方、或根本不在本段任何画面里时，
不列进 ``resources.props``，也不写它的材质/颜色/形状，只写观众实际能看到的痕迹（例如
「隔着棉布顶出一个圆形轮廓」）。不改 ``storyboard_dialects.py``（499/500 行，新增逻辑
已没有余量）与 ``storyboard_segment_output.py``——接线方式照抄 ``storyboard_shot_mandates.
py``「静态、无条件、按 render_format 选方言」的先例，两个模型方言各自的文案在阶段二对
每一段都无条件拼进 ``dialect_instructions``，不依赖任何提名、不按画风分支（遮挡导致的
误画与画风无关，写实/非写实都会发生，不是 ``skin_blush`` 那种只在写实渲染下才成立的
问题）。``storyboard_prop_appearance_lock.segment_rule_text`` 与
``storyboard_segment_output.segment_output_contract`` 的 ``resources`` 字段说明同批各
加一句指向本规则的可见性限定，三处单源于本模块的两条规则文本，不再各写一份。

不是关键词黑名单：判据是"这一镜画面里看不看得见"这个由模型对画面本身的理解得出的事实，
不是靠匹配"遮住""收起"这类词面——道具可见性与 ``skin_blush``/``impossible_camera_move``
同类，都只能由模型对画面内容的理解产出，不是代码能从 prompt_text 字符串核验的结构事实，
因此本规则和 ``skin_blush`` 一样只进 ``dialect_instructions``，不进
``storyboard_prose_review`` 的代码可核验判据清单。
"""
from __future__ import annotations

SEEDANCE_PROP_VISIBILITY_RULE = (
    "道具要不要写出标准外观、要不要列进本段 resources.props，只看一个判据：这件道具在本段"
    "某一镜画面里实际看不看得见，与它是否被原文提到、是否在人物身上/手里、是否有素材库标准"
    "外观或全集外观锁定都无关。看得见时（包括只出现在背景、没有人物与它互动，例如门口放着"
    "的行李箱、桌上摆着的水杯）：把它列进本段 resources.props，并按标准外观锚点规则写出它"
    "的可视特征（有素材库标准外观或全集外观锁定的逐字沿用，没有的自定至少三项可视觉验证"
    "特征）。看不见时（被衣物/容器/包裹完全遮住、揣进口袋、收进包里、锁在抽屉里，或者根本"
    "不在本段任何一镜的画面里）：不列进本段 resources.props，也不写它的材质、颜色、形状、"
    "磨损细节这类外观信息——哪怕素材库给它建了标准外观卡片或全集已经锁定过它的外观，这一段"
    "画面里看不见就不写；只写观众在画面里实际能看到的痕迹（例如「隔着卫衣的棉布顶出一个圆形"
    "轮廓」「手提着一个有分量的鼓包，看不出里面是什么」），不写轮廓之下那件东西本身长什么样。"
)

MINIMAX_H3_PROP_VISIBILITY_RULE = (
    "Whether a prop gets its standard appearance written out, and whether it goes into this "
    "segment's resources.props, is decided by exactly one thing: whether that prop is actually "
    "visible in some Shot of this segment's picture -- not whether the source text mentions it, "
    "not whether a character is holding or wearing it, and not whether it has a standard-library "
    "appearance or a series-wide appearance lock. When it is visible (including when it only "
    "appears in the background with no character interacting with it, e.g. a suitcase sitting by "
    "the doorway, a cup left on the table): put it into this segment's resources.props and write "
    "out its visual features per the standard-appearance rule (copy a locked/known appearance "
    "verbatim; invent at least three verifiable features if there is none). When it is not "
    "visible (fully covered by clothing, inside a bag or container, in a pocket, locked in a "
    "drawer, or simply not in any Shot's picture this segment): do not put it into this "
    "segment's resources.props, and do not write its material, color, shape, or wear-and-tear -- "
    "even if the asset library has a standard-appearance card or a series-wide lock for it, an "
    "invisible prop stays invisible in the prose. Write only what the audience can actually see "
    "(e.g. \"a round shape presses out through the hoodie's cotton fabric\", \"she carries a "
    "heavy-looking bulging bag, its contents unseen\"), never what the hidden object itself looks "
    "like."
)


def prop_visibility_dialect_rule(render_format: str) -> str:
    """按目标视频模型方言选对应文案；不区分画风、不依赖任何提名，接线方式照抄
    ``storyboard_shot_mandates.shot_mandates_dialect_rule``——两个模型方言各自的文案在
    阶段二对每一段都无条件拼进 ``dialect_instructions``。"""
    if render_format == "minimax_h3_native_fields":
        return MINIMAX_H3_PROP_VISIBILITY_RULE
    return SEEDANCE_PROP_VISIBILITY_RULE


__all__ = [
    "SEEDANCE_PROP_VISIBILITY_RULE",
    "MINIMAX_H3_PROP_VISIBILITY_RULE",
    "prop_visibility_dialect_rule",
]
