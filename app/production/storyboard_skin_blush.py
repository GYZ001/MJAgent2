"""分镜台阶段二：写实画风下人脸局部颜色的写法（2026-09-30 第 1 集最终成片复查发现问题，
2026-10-01 第五版 35 段逐帧复查推翻初版修法并改写方向）。

2026-09-30 初版证据与修法（已被推翻，原因见下）：第 1 集成片第 5、22 段特写镜头写「一片红
从她的脸颊一路漫到耳根」，视频模型画成边界生硬、高饱和度的红色色块。初版判断根因是肤色
变化被写成了「有起点、有路径、有终点」的空间蔓延过程，修法是要求模型改写成"轻微、自然、
渐变的红晕"（只点出部位 + 用程度轻的词，例如「脸颊泛起淡淡的红晕」）。

2026-10-01 第五版 35 段真实成片逐帧复查：初版修法要求模型写的「脸颊泛起淡淡的红晕」这类
轻量写法，实测仍有相当比例被画成边界生硬的实心色块——第 5 段「她脸上只淡淡扫了一点妆」
画成双颊+下眼睑实心红/粉色块；第 16 段「屋里积水映在她脸颊上的阴绿冷光随之慢慢褪淡」画成
脸颊一块几何形绿松石色块；第 32/34 段「耳根微微泛起淡淡的红晕」画成脸颊硬边红斑。第 7、8、
17 段同样写了「脸颊泛起淡淡的红晕」这次没出色块——同一句写法时而正常时而色块，说明问题
不在「写得有多重」（轻写法已经证伪），而在一个更上游的事实：只要颜色被落在了人脸的某个
部位上——不管是情绪泛红、妆容颜色、还是环境光映在脸上——视频模型就有相当概率把那个部位
画成一块独立于肌肉/五官形态之外的纯色区域。「写轻一点」治标不治本，真正要收紧的是「人脸
该不该承载颜色描述」这件事本身，不是「颜色描述该多轻」。

现行修法（第五版）：人脸不承载任何局部颜色描述——情绪泛红、妆容颜色、彩色光照在脸颊/
半边脸上、脸色变成某种颜色，一律不写；人脸只负责五官形态与肌肉动作（不带颜色的动作，
例如嘴角上扬、眉头拧起、眼睛睁大）。害羞/窘迫这类情绪的表演整体转移到眼神/嘴唇/手部
动作/停顿与呼吸——这几类正是 ``storyboard_dialects.py`` 已有的「情绪一律写成面部肌肉
动作和肢体动作」规则覆盖的表演维度，本规则只是把"脸上的颜色"这件事从"写轻"改成"不写"，
不新造一套表演体系。环境光的颜色与冷暖（暖黄烛光、青灰冷调、晨光暖金色）仍然可以写，
但只写进整段画面/场景的光线描述里（例如「室内光线转为偏冷的青灰色调」），由整个画面统一
承担，不再单独描述这束光落在脸的某个部位、留下一块与环境同色的色调——第 16 段的色块正是
"场景光线颜色" 被错误落到"脸颊"这个局部造成的，不只是情绪泛红一种触发源。

2026-10-01 补丁（「修订本段」验收，用户实测第 35 段）：星盘只写轮廓、未列入
``resources.props``，腮红/唇色被本规则剔除且在复核里留痕——都生效了；但正文仍写了
「近乎素颜的淡妆」，这类不带颜色词的妆容说法（「淡妆」「素颜妆」「化了妆」）本身就会被
视频模型自行配色、画成色块，与第 5 段「她脸上只淡淡扫了一点妆」同一根因（见上「第五版」
一段的原始证据）。说明"不写颜色词"还不够——只要正文点出"化没化妆、妆容程度多重"这件事
本身，模型就会自己决定怎么上色，不需要等正文真的写出颜色词。修法：规则从"不写妆容颜色"
扩大到"连妆容本身都不写"，唇色单独明确禁止；人物的精神状态（疲惫、精心打扮过）改用
发型/衣着/眼神/动作表现，不借道妆容措辞。

不改 ``storyboard_dialects.py``（499/500 行，新增逻辑已没有余量）——接线方式照抄
``storyboard_shot_mandates.py``「静态、无条件、按 render_format 选方言」的先例：两个模型
方言各自的文案在阶段二对每一段都无条件拼进 ``dialect_instructions``，不依赖任何提名。保留
模块/常量/函数名不变（``SEEDANCE_SKIN_BLUSH_RULE``/``MINIMAX_H3_SKIN_BLUSH_RULE``/
``skin_blush_dialect_addendum``）：规则覆盖范围已从"脸红"扩大到"人脸任意局部颜色"，但
改名会牵动 ``storyboard_prose_review``/``storyboard_segment_chains`` 两处单源引用与对应
测试，本次只改规则内容，不做无必要的标识符重命名。

只对写实画风生效：判据不变，仍按 ``app.visual_styles.is_photographic_style_prompt``——
只有当本项目画风解析出的 prompt 串命中某个 ``photographic=True`` 预设（真人摄影风/精修
真人风，含其 ``legacy_prompts``）时才追加这条规则；非写实画风与解析不出画风的项目，
``dialect_instructions`` 保持改动前逐字不变——与
``storyboard_music_bed.music_bed_dialect_addendum`` 开关关闭时的「逐字不变」同一纪律。
国漫电影风/古典水墨风本身就是非写实渲染，局部色块不会被观众看成"上色错误"。
"""
from __future__ import annotations

SEEDANCE_SKIN_BLUSH_RULE = (
    "写实画风下，人物脸上不写任何局部颜色，也不写妆容本身：情绪引起的脸颊/耳根泛红、妆容"
    "呈现的颜色（腮红、唇彩这类）、彩色光线照在脸颊或半边脸上形成的色调、脸色因情绪或体感"
    "变成某种颜色，这几类统统不写；化没化妆、化了什么程度的妆这类不带颜色词的说法（「淡妆」"
    "「素颜妆」「化了妆」「近乎素颜的淡妆」）同样不写——视频模型会自己给这些说法配色，照样"
    "画成色块；唇色也不写，不管是用颜色词写的还是用「涂了口红」这类说法写的。脸只负责五官"
    "形态与肌肉动作本身（嘴角上扬、眉头拧起、眼睛睁大这类不带颜色的动作可以写）。害羞、"
    "窘迫这类情绪改由眼神（低头躲闪、眼神游移不敢直视）、嘴唇（抿嘴、轻咬下唇）、手部动作"
    "（绞衣角、摸后颈、攥紧衣袖）、停顿与呼吸（屏住呼吸、呼吸变急促）来演；人物的精神状态"
    "（疲惫、精心打扮过）改用发型（刻意盘起的发髻、凌乱的碎发）、衣着（熨帖的正装、皱巴巴的"
    "居家服）、眼神（眼神疲惫涣散、眼神明亮有神）和动作（放松地靠着、强打精神站直）来表现，"
    "这几项足够传达情绪与状态，不需要再靠脸上的颜色变化或妆容措辞。场景光线本身的颜色与"
    "冷暖（暖黄烛光、偏冷的青灰色调、晨光的暖金色）只写进整段画面/场景的光线描述里（例如"
    "「整间屋子被暖黄的灯光笼罩」「室内光线转为偏冷的青灰色调」），由整个画面统一承担，不要"
    "再单独写这束光落在脸上、让脸的某个部位出现一块与环境同色的色调。"
)

MINIMAX_H3_SKIN_BLUSH_RULE = (
    "In a photographic (live-action-style) visual style, do not write any localized color on a "
    "character's face, and do not write makeup itself: an emotion-driven flush on the cheeks or "
    "ears, a color applied by makeup (blush, lip color), a colored light tint landing on a cheek "
    "or one side of the face, or skin turning some color from emotion or physical strain -- none "
    "of these get written; phrases about whether makeup was applied or how much, even without "
    "naming a color (\"light makeup\", \"a bare-faced, no-makeup look\", \"wearing makeup\", "
    "\"near-bare, barely-there makeup\"), also do not get written -- the video model invents its "
    "own color for these phrases too and produces the same color-block artifact; lip color is "
    "never written either, whether named directly or implied (\"wearing lipstick\"). The face "
    "only carries shape and muscle movement (a corner of the mouth lifting, brows knitting, eyes "
    "widening -- movements with no color attached are fine). Play shyness or embarrassment "
    "through the eyes (looking down, gaze darting away, avoiding eye contact), the lips "
    "(pressing them together, a light bite on the lower lip), hand gestures (twisting the hem of "
    "a sleeve, touching the back of the neck, gripping a cuff), a held pause, or breath (holding "
    "a breath, breathing quickening). A character's state of mind or body (exhausted, carefully "
    "dressed up) is conveyed instead through hairstyle (a deliberately pinned-up bun, loose stray "
    "strands), clothing (a crisp, pressed outfit vs. rumpled loungewear), the eyes (a tired, "
    "unfocused gaze vs. bright and alert), and action (slumping against something vs. forcing an "
    "upright posture) -- these are enough to carry the emotion and state without any color on the "
    "face or any mention of makeup. A scene's own light color and warmth (warm candlelight, a "
    "cool blue-grey cast, warm golden morning light) belongs only in that shot's overall "
    "picture/scene lighting description (e.g. \"the whole room is bathed in warm amber light\", "
    "\"the room's light shifts to a cool blue-grey cast\"), carried by the whole frame -- do not "
    "also describe that light landing on the face and leaving a patch of matching color on some "
    "part of it."
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
