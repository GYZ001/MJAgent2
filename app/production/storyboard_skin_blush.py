"""分镜台阶段二：写实画风下情绪引起的肤色变化写法（2026-09-30，第 1 集最终成片复查发现）。

生产证据：第 1 集最终成片第 5 段、第 22 段特写镜头（第 22 段还被剪进了片头预告），分镜
正文写「一片红从她的脸颊一路漫到耳根」「脸颊的红一下子漫到耳根」，视频模型画成一整块
边界生硬、高饱和度的红色色块，观众第一反应是「上色错误」或「被扇了巴掌」，不是「害羞
脸红」。人物外观锚点里本来就有「淡淡的蜜桃色腮红」这类基底肤色描述——正文再叠一句
「一片红……漫到耳根」，等于同时给了模型两套肤色线索，且后一句把肤色变化写成了一个有
起点（脸颊）、有路径（一路漫）、有终点（耳根）的空间蔓延过程。

根因不是某个具体词（不是关键词黑名单能堵住的问题——CLAUDE.md「判据从数据推导，禁止
黑白名单/关键词枚举」），是「情绪引起的肤色变化」这个生理反应被写成了和运镜、位移同一种
「有明确路径的空间过程」描述；视频模型对这类描述的默认处理方式是画一块能对应这条路径的
色块，这与分镜正文里其它「受力特征」「行进方向」类空间过程描述本该被精确执行、模型确实
会精确执行是同一种倾向，只是用在了肤色这种不该有清晰边界的生理反应上。

修法：在 dialect_instructions 里追加一条正面陈述——只描述"怎么写"（轻微、自然、渐变的
红晕，只点出发红的具体部位，用程度轻的词），并把害羞/窘迫这类情绪的主要表演转移到眼神/
嘴唇/手部动作/停顿；这几类正是 ``storyboard_dialects.py`` 已有的「情绪一律写成面部肌肉
动作和肢体动作」规则覆盖的表演维度，本规则只是把"脸红"这个特例从"空间蔓延"改写成同一套
表演语言里的一处轻量细节，不新造一套表演体系。不改 ``storyboard_dialects.py``
（499/500 行，新增逻辑已没有余量）——接线方式照抄 ``storyboard_shot_mandates.py``「静态、
无条件、按 render_format 选方言」的先例：两个模型方言各自的文案在阶段二对每一段都无条件
拼进 ``dialect_instructions``，不依赖任何提名。

只对写实画风生效：这个问题的根因是"照片级写实渲染"对肤色边界的要求比动画/水墨风更苛刻——
国漫电影风/古典水墨风本身就是非写实渲染，情绪红晕被画成色块在这两种画风里不会被观众看成
"上色错误"（动画/水墨风格本身允许平涂色块与程式化的表情符号），且本次生产证据（第 1 集）
用的正是真人摄影风。因此按 ``app.visual_styles.is_photographic_style_prompt`` 判断：只有
当本项目画风解析出的 prompt 串命中某个 ``photographic=True`` 预设（真人摄影风/精修真人风，
含其 ``legacy_prompts``）时才追加这条规则；非写实画风与解析不出画风的项目，
``dialect_instructions`` 保持改动前逐字不变——与
``storyboard_music_bed.music_bed_dialect_addendum`` 开关关闭时的「逐字不变」同一纪律。
"""
from __future__ import annotations

SEEDANCE_SKIN_BLUSH_RULE = (
    "写实画风下，情绪引起的脸颊泛红要写成轻微、自然、渐变的红晕：只点出发红的具体部位"
    "（例如「脸颊泛起淡淡的红晕」「耳尖微微发红」「脸颊浅浅地红了」），用「淡淡」「微微」"
    "「浅浅」这类程度轻的词。害羞、窘迫这类情绪主要靠眼神（低头躲闪、眼神游移不敢直视）、"
    "嘴唇（抿嘴、轻咬下唇）、手部动作（绞衣角、摸后颈、攥紧衣袖）和停顿这几类表演来演，"
    "脸颊/耳尖的泛红只是这套表演之外的一处轻量细节，不是这一镜的表演重点。"
)

MINIMAX_H3_SKIN_BLUSH_RULE = (
    "In a photographic (live-action-style) visual style, an emotion-driven flush on the face "
    "should read as a slight, natural, gradual blush: name only the specific spot that flushes "
    "(e.g. \"a faint blush rises on her cheeks\", \"the tips of her ears turn slightly pink\", "
    "\"a light flush touches her cheeks\"), using mild-degree words such as \"faint\", \"slight\", "
    "or \"light\". Play shyness or embarrassment mainly through the eyes (looking down, gaze "
    "darting away, avoiding eye contact), the lips (pressing them together, a light bite on the "
    "lower lip), hand gestures (twisting the hem of a sleeve, touching the back of the neck, "
    "gripping a cuff), and a held pause -- the cheek/ear flush stays a minor supporting detail, "
    "not this Shot's main performance."
)


def skin_blush_dialect_addendum(render_format: str, *, photographic: bool) -> str:
    """非写实画风（或画风未解析出命中预设）返回空串——拼进 ``dialect_instructions`` 的
    f-string 后逐字不变；写实画风按 ``render_format`` 选对应文案，带前导换行拼进
    ``dialect_instructions``，接线方式与 ``storyboard_music_bed.music_bed_dialect_addendum``
    同源。"""
    if not photographic:
        return ""
    rule = MINIMAX_H3_SKIN_BLUSH_RULE if render_format == "minimax_h3_native_fields" else SEEDANCE_SKIN_BLUSH_RULE
    return "\n" + rule


__all__ = [
    "SEEDANCE_SKIN_BLUSH_RULE",
    "MINIMAX_H3_SKIN_BLUSH_RULE",
    "skin_blush_dialect_addendum",
]
