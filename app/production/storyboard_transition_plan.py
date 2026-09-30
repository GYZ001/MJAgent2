"""转场先定后写：段生成前就把『这段开头是什么转场』算成既定事实写进任务，不是等模型写完提示词、
resources.scenes 也报出来之后，再用同一套推导覆盖 camera_digest.transition_from_previous。

真实故障（2026-09-30《顾念长安》EP1 第19段）：旧流程里 ``storyboard_pack.py`` 只把纯文本判据
（``screenplay_markers.transition_between``）算出的转场猜测喂进 task_payload，且
``structure_rules`` 只在换场（``scene_change=true``）时才把转场名文本告诉模型；
``scene_changed_by_resource_scenes`` 这个旁路信号要等模型自己写完 ``resources.scenes``
才可信，于是 ``draft.camera_digest.transition_from_previous`` 在模型写完提示词之后才被
``transition_with_resource_bypass`` 升级成「叠化」——但模型写镜头1正文时被告知的是「本段与
上一段是同一场戏」，按同场硬切的写法起幅，终剪却按叠化渲染，画面和转场对不上。

修法：生成前用同一套推导函数（不重新发明判据），把『本段实际生成的场景』换成『映射台已经为
本段原文范围登记好的计划场景』（``relevant_assets.scenes``——这是产出侧数据，生成前就有，不是
模型这次生成才决定的东西）代入，得到的结果连同 ``scene_change`` 一起写回 ``structure``，正常
流入既有的 ``screenplay_markers.structure_rules``/``storyboard_narrative_arc.segment_narrative_arc_rules``
——这两个函数一行都不用改，它们本来就认 ``structure["transition_from_previous"]``/
``structure["scene_change"]``。生成后模型实际登记的 ``resources.scenes`` 出来了，如果和计划
场景不同、导致再推导结果变了，以再推导结果为准（不静默改写模型已经写好的正文——那是生成时的
既定事实，事后不能回头篡改），但要留一条可见告警：这段的场景映射可能本身就不准，值得人工核查。
"""
from __future__ import annotations

import logging

from app.production.screenplay_markers import transition_with_resource_bypass

log = logging.getLogger(__name__)


def resolve_transition_before_generation(
    structure: dict, previous_scene_ids: set[str], planned_scene_ids: set[str],
) -> tuple[dict, str]:
    """生成前调用。返回 (写回后的 structure, 纯文本判据值)——后者是生成后核对漂移的基准，
    不能拿这次已经升级过的结果去做基准：``transition_with_resource_bypass`` 只在传入值等于
    ``SAME_SCENE_TRANSITION`` 时才会升级，拿升级后的「叠化」再传一次，条件恒假，升级永远只做
    这一次就再也测不出后续漂移。"""
    text_transition = structure["transition_from_previous"]
    resolved = transition_with_resource_bypass(text_transition, previous_scene_ids, planned_scene_ids)
    if resolved != text_transition:
        # 推导升级正是因为两段 resources.scenes 不同、场景确实变了：transition_from_previous
        # 与 scene_change 是同一个结论的两种表达，不能只改一个——structure_rules 的「同场」
        # 分支会告诉模型『从上一段末镜状态接续，不重开机位』，与「叠化」矛盾。
        structure = {**structure, "transition_from_previous": resolved, "scene_change": True}
    return structure, text_transition


def finalize_transition_after_generation(
    text_transition: str, previous_scene_ids: set[str], actual_scene_ids: set[str],
    *, told_transition: str, segment_no: int,
) -> str:
    """生成后调用：本段模型实际登记的 resources.scenes 出来了，用同一套推导（同一个
    text_transition 基准）重算一次；与生成前告诉模型的 told_transition 不一致，说明这段的
    实际场景登记偏离了计划——以重算结果为准写回结构字段（贴合真实产出），但记一条可见告警，
    不静默：镜头正文是按 told_transition 写的，人工需要核查两者是否真的对不上。"""
    resolved = transition_with_resource_bypass(text_transition, previous_scene_ids, actual_scene_ids)
    if resolved != told_transition:
        log.warning(
            "[STORYBOARD_TRANSITION_DRIFT][未拦截] 第%s段生成前推导的转场是「%s」并已写进生成任务，"
            "生成后按本段实际 resources.scenes 重新推导得到「%s」：camera_digest 改用后者，但镜头"
            "正文是按前者写的，两者可能不一致，请人工核查本段场景映射是否准确",
            segment_no, told_transition, resolved,
        )
    return resolved


__all__ = ["resolve_transition_before_generation", "finalize_transition_after_generation"]
