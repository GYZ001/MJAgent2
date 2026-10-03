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

修法（第一版，2026-10-01）：新增一条完整正面陈述——视觉可见性是唯一判据，覆盖两个方向：
道具在本段某一镜画面里实际可见（包括只出现在背景、没有人物与它互动）时，必须列进
``resources.props`` 并写出标准外观；道具被衣物/容器/包裹完全遮住、收在看不见的地方、或
根本不在本段任何画面里时，不列进 ``resources.props``，也不写它的材质/颜色/形状，只写
观众实际能看到的痕迹（例如「隔着棉布顶出一个圆形轮廓」）。

3. 第二版（2026-10-01，同一集第 13/17 段真实成片复查，协调方发现）：第一版的「可见/
   不可见」二分法本身制造了新缺陷——第 13 段写「外套5颗扣子全部扣好」，紧接着把被这件
   外套盖住的开衫和碎花长裙的标准外观逐字抄全（「带V型翻领，衣长至腰胯位置」「圆领微收腰
   A字版型，短袖设计，裙长至小腿中部，领口内侧缝有1cm宽白色洗水标」）——开衫/长裙没有
   被"完全遮住"（领口、下摆仍露出一截），按第一版规则只能二选一：判"可见"就逐字抄全部
   标准外观（包括被外套盖住的领口形状、腰身剪裁这类细节），模型据此把外套画成敞开、
   长裙大片外露；判"不可见"又与事实不符（确实露出一截）。两次生成成片都复现了这个
   矛盾（外套整段敞开、碎花裙大片外露；第 17 段外套整段消失）。根因是"可见"这个判据
   本身是连续的（完全可见/只露出一截/完全看不见），第一版把它当成了二元开关。

   修法：把判据从二分改成三态——完全可见（列入 resources.props + 写完整标准外观，与
   第一版「可见」分支相同）；部分可见（列入 resources.props，但只写露出部分的颜色/
   花纹/材质，不写被遮住部分的款式细节，哪怕标准外观文案里有这几句也不抄）；完全不可见
   （与第一版「不可见」分支相同，不列入、不写外观）。``storyboard_prop_appearance_lock.
   segment_rule_text`` 同批把"如果看得见就必须逐字沿用"改成按三态分流，避免"逐字沿用
   锁定外观"这条要求在部分可见时继续诱发同一个问题。

4. 第三版（2026-10-01，第 19/20 段真实成片复查，协调方发现）：第 20 段 resources.props
   列了 8 项道具（其中 5 项是旧版遗留的衣服卡），加上人物/场景参考图合计超过
   ``max_reference_images()``（9 张）上限，``app.multiview.ref_pack_priority`` 对道具档
   超限裁剪按 ``-quality, id`` 排序——quality 对库资产道具恒为 0，实际落到 ``id``（一串
   与本段画面无关的随机串），行李箱参考图被随机丢弃，成片行李箱变成与卡片不符的光面
   香槟金；第 19 段同理。修法分两部分：① 机制修复（见
   ``app.video_modes.prop_references``/``app.multiview.ref_pack_priority`` 模块
   docstring）——``resources.props`` 的列出顺序打成 ``resources_order`` 字段一路带到
   参考图装配，超限裁剪按它优先，不再看随机 id；② 本模块补一句正面陈述，告诉模型
   ``resources.props`` 要按画面显眼程度与跨段一致性重要程度从高到低排列——机制只能
   原样保留模型给出的顺序，顺序本身是否"重要的排前面"仍要靠这句话要求模型自己做到。

5. 第四版（2026-10-02，《顾念长安》EP1 第 14/15 段真实成片复查，经「修订本段」重生成
   后发现）：第二版把判据从二分改成三态后，"部分可见只写露出部分"这句话被模型理解成
   "把可见范围写进 label 本身"——``resources.props`` 出现了「浅蓝色碎花长裙（外套下摆
   露出的一截）」「米白色针织开衫（外套领口露出的领口）」这类 label，括号里的说明本该
   写进 ``description``。``app.video_modes.prop_references.resolve_segment_prop_
   manifest_entries`` 按 label 逐字查道具卡，带括号的 label 查不到卡，参考图没有送给
   视频模型，第 15 段温念的浅蓝碎花长裙被画成了另一条裤子。修法：两个方言补一句正面
   陈述——label 逐字取自道具卡名称或其登记别名，可见范围/部位/归属一律写进
   description，不写进 label、不加括号；新增结构校验 ``storyboard_prop_label_
   validation.prop_label_bracket_note_errors``（label 是"已知道具名+紧跟括号"的形状
   就报错，交给既有生成重试/人工修订拒存机制，不静默剥括号替模型"修好"），接入逐段
   生成（``storyboard_identity_generation.generated_identity_errors``）与「修订本段」
   （``app.domain.storyboard_ops.identity_workspace.prepare_identity_candidate``）两条
   路径。

不改 ``storyboard_dialects.py``（499/500 行，新增逻辑已没有余量）——接线方式照抄
``storyboard_shot_mandates.py``「静态、无条件、按 render_format 选方言」的先例，两个
模型方言各自的文案在阶段二对每一段都无条件拼进 ``dialect_instructions``，不依赖任何
提名、不按画风分支（遮挡导致的误画与画风无关，写实/非写实都会发生，不是 ``skin_blush``
那种只在写实渲染下才成立的问题）。``storyboard_prop_appearance_lock.segment_rule_text``
与 ``storyboard_segment_output.segment_output_contract`` 的 ``resources`` 字段说明同批
各加一句指向本规则的可见性/排序限定，三处单源于本模块的规则文本，不再各写一份。

不是关键词黑名单：判据是"这一镜画面里是哪种可见状态"这个由模型对画面本身的理解得出的
事实，不是靠匹配"遮住""收起""露出"这类词面——道具可见性与 ``skin_blush``/
``impossible_camera_move`` 同类，都只能由模型对画面内容的理解产出，不是代码能从
prompt_text 字符串核验的结构事实，因此本规则和 ``skin_blush`` 一样只进
``dialect_instructions``，不进 ``storyboard_prose_review`` 的代码可核验判据清单。
"""
from __future__ import annotations

SEEDANCE_PROP_VISIBILITY_RULE = (
    "道具/衣物要不要写出外观、要不要列进本段 resources.props，只看一个判据：这件道具/衣物在"
    "本段某一镜画面里实际是哪种可见状态，与它是否被原文提到、是否在人物身上/手里、是否有"
    "素材库标准外观或全集外观锁定都无关，按下面三种状态分别处理——"
    "完全可见（画面里能看到它的全貌，没有被其它衣物/容器/道具挡住任何部分，包括只出现在"
    "背景、没有人物与它互动的情形，例如门口放着的行李箱、桌上摆着的水杯）：把它列进本段"
    "resources.props，并写出完整的标准外观（有素材库标准外观或全集外观锁定的逐字沿用，"
    "没有的自定至少三项可视觉验证特征）。"
    "部分可见（只露出一部分，其余被外层衣物/容器/道具盖住——例如扣好的外套下摆以下露出的"
    "一截裙摆、外套领口露出的一圈毛衣领、包口露出的一角）：把它列进本段 resources.props，"
    "但只写露出来的那部分看得见的特征（颜色、花纹、材质），不写被遮住部分的款式与细节"
    "（领口形状、袖子长短、腰身剪裁、内侧标签这类）——哪怕素材库/全集外观锁定给了这件衣物"
    "完整的标准外观文案，这一段画面里只露出一截，就只能抄这一截对应的颜色/花纹/材质用词，"
    "不逐字整段抄。"
    "完全不可见（被衣物/容器/包裹完全遮住、揣进口袋、收进包里、锁在抽屉里，或者根本不在"
    "本段任何一镜的画面里）：不列进本段 resources.props，也不写它的材质、颜色、形状、磨损"
    "细节这类外观信息——哪怕素材库给它建了标准外观卡片或全集已经锁定过它的外观，这一段画面"
    "里看不见就不写；只写观众在画面里实际能看到的痕迹（例如「隔着卫衣的棉布顶出一个圆形"
    "轮廓」「手提着一个有分量的鼓包，看不出里面是什么」），不写轮廓之下那件东西本身长什么样。"
    "resources.props 的列出顺序也有意义：按本段画面里的显眼程度、以及这件道具/衣物跨段"
    "外观一致性的重要程度，从高到低排列——画面里越显眼、在别的段也会出现的道具排得越靠前；"
    "参考图张数有上限，排在后面的道具会最先被舍弃，只按这两项重要性判断，不按道具名字或"
    "类型排序。"
    "resources.props 每一条的 label 只能是这件道具/衣物本身的名称：有道具卡的，逐字使用"
    "relevant_assets.props 里给出的这件道具的名称或其登记的别名，一个字都不能改，也不能在"
    "后面加括号或任何其它说明；没有道具卡的，写它的通用名称（例如「帆布包」「保温杯」）。"
    "这件道具这一段是完全可见、只露出一截、露出的是哪个部位、此刻挂在谁身上或放在什么"
    "位置，这类说明一律写进 description，不写进 label，也不得在 label 后面用括号补充——"
    "label 只负责标识这是同一件道具，可见范围/部位/归属这些描述交给 description。"
)

MINIMAX_H3_PROP_VISIBILITY_RULE = (
    "Whether a prop or garment gets its appearance written out, and whether it goes into this "
    "segment's resources.props, is decided by exactly one thing: which visibility state it is in "
    "in some Shot of this segment's picture -- not whether the source text mentions it, not "
    "whether a character is holding or wearing it, and not whether it has a standard-library "
    "appearance or a series-wide appearance lock. Handle it per these three states -- "
    "Fully visible (the whole thing is visible in frame, with no part covered by another "
    "garment/container/prop, including when it only appears in the background with no character "
    "interacting with it, e.g. a suitcase sitting by the doorway, a cup left on the table): put it "
    "into this segment's resources.props and write out its complete appearance (copy a "
    "locked/known appearance verbatim; invent at least three verifiable features if there is "
    "none). "
    "Partially visible (only part of it shows, the rest covered by an outer garment, container, "
    "or prop -- e.g. a dress hem peeking out below a buttoned coat, a sweater collar peeking out "
    "above a coat's collar, one corner of a bag showing through its opening): put it into this "
    "segment's resources.props, but write only the visible portion's color, pattern, and material "
    "-- do not write the covered portion's cut or detail (collar shape, sleeve length, waist "
    "tailoring, an inside label and the like), even if the standard/locked appearance text "
    "describes the whole garment -- copy only the color/pattern/material words matching the "
    "visible sliver, never the full paragraph verbatim. "
    "Not visible at all (fully covered by clothing, inside a bag or container, in a pocket, locked "
    "in a drawer, or simply not in any Shot's picture this segment): do not put it into this "
    "segment's resources.props, and do not write its material, color, shape, or wear-and-tear -- "
    "even if the asset library has a standard-appearance card or a series-wide lock for it, an "
    "invisible prop stays invisible in the prose. Write only what the audience can actually see "
    "(e.g. \"a round shape presses out through the hoodie's cotton fabric\", \"she carries a "
    "heavy-looking bulging bag, its contents unseen\"), never what the hidden object itself looks "
    "like. "
    "The order items appear in resources.props matters too: rank them from most to least "
    "important by how prominent they are in this segment's picture and how much cross-segment "
    "appearance consistency they need -- a more prominent prop, or one that also appears in other "
    "segments, goes first. Reference images have a hard cap, and items listed later are the first "
    "to be dropped -- order by these two factors, never by name or type. "
    "Each resources.props entry's label must be nothing but the prop/garment's own name: if it has "
    "a prop card, use verbatim the name given for it in relevant_assets.props, or one of its "
    "registered aliases -- not a single character changed, and never append a parenthetical or any "
    "other note after it; if it has no prop card, write its generic name (e.g. \"canvas bag\", "
    "\"thermos\"). Whether it is fully visible, only a sliver shows, which part is showing, or whose "
    "hands or which spot it is at right now -- all of that belongs in description, never in label "
    "and never appended to label in parentheses -- label only exists to identify this as the same "
    "prop; the visible-portion/location/ownership description belongs in description."
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
