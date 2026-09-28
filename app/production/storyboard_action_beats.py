"""P0-B：决定性动作方言规则（2026-09-27）。

这条规则**通用生效**（不依赖任何提名——是独立于 P0-A 的静态摄影语法规则，
两个模型方言各自的文案在阶段二对每一段都无条件拼进
``dialect_instructions``，与本段是否命中 P0-A 的情绪转折提名无关）：改变
人物处境走向的决定性动作要单独占一镜、紧跟在触发它的刺激画面之后，不要
把刺激和决定动作揉进同一镜头描述，也不要让镜头停在旁人反应上就切走、把
决定动作本身留白。

不改 ``storyboard_dialects.py``（494/500 零余量）；不改
``_dialect_for_target_video_model`` 返回值本身，两个模型方言各自的既有
``MINIMAX_H3_DIALECT_INSTRUCTIONS``/``SEEDANCE_DIALECT_INSTRUCTIONS`` 字面
量不变，``tests/test_storyboard_pack.py`` 里对它们的 ``is`` 恒等断言零改动。
"""
from __future__ import annotations

SEEDANCE_DECISIVE_ACTION_RULE = (
    "改变人物处境走向的决定性动作（点头、握紧、转身离开这类）单独占一镜"
    "（镜头N：……），紧跟在触发它的刺激画面所在的镜头之后：先是刺激本身"
    "单独一镜，再是这个决定性动作单独一镜，不要把刺激和决定动作揉进同一镜头"
    "描述，也不要让镜头停在旁人反应（例如对方的笑脸）上就切走、把决定动作"
    "本身留白。人物对刺激产生的情绪反应（表情、身体僵住这类还没转化为动作的"
    "反应）同样要紧跟在刺激镜头之后成镜。决定性动作镜头要写出动作本身"
    "（手部/身体动作、停顿、呼吸），不写抽象心理描述。"
)

MINIMAX_H3_DECISIVE_ACTION_RULE = (
    "A decisive action that changes where a character stands or what they "
    "choose -- nodding, gripping something tighter, turning to leave -- gets "
    "its own [Shot N], immediately following the [Shot N] showing the "
    "stimulus that triggers it: the stimulus is one shot, the decisive "
    "action is the next shot -- do not fold them into a single [Shot N], and "
    "do not cut away on someone else's reaction while leaving the decisive "
    "action itself unshown. A character's emotional reaction to a stimulus "
    "(expression, body freezing) must likewise directly follow the stimulus "
    "shot. Write the action itself (the hand/body movement, the pause, the "
    "breath), not an abstract description of feelings."
)


def decisive_action_dialect_rule(render_format: str) -> str:
    """按目标视频模型方言选对应文案；``render_format`` 取值见
    ``app.video_prompt_profiles.VideoPromptProfile.render_format``。"""
    if render_format == "minimax_h3_native_fields":
        return MINIMAX_H3_DECISIVE_ACTION_RULE
    return SEEDANCE_DECISIVE_ACTION_RULE
