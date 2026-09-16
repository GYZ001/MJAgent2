"""人工更换本段场景绑定（分镜台 components/SegmentSceneEdit.tsx）依赖的字段契约。

背景：段落的 scene_name 由分镜阶段按剧本场次标注归一得到，而一段（shots 一行）只有
一个场景字段，2.x 的一段里却有多个镜头切换——段内跨场景时必然只能取其一。2026-09-16
实测龙猫出爪 ep1 第 17 段：剧本标注「人间·老街」，归一到「人间老街区」没错，但段内
镜 1 站在宠物医院门口，拿不到本该匹配的「晚安宠物医院门口」。结构装不下，所以要留
人工改的口子。

这里锁的是那个口子成立的四个前提。任何一条被改动，前端那个入口会从「能用」变成
「点了报错」或「悄悄改坏别的东西」，而且都不会在别处报红——所以在这里钉死。
"""
from __future__ import annotations

from app.domain.storyboard_ops.mutation_primitives import (
    IDENTITY_BEARING_EDIT_FIELDS,
    RENDER_TIME_ONLY_EDIT_FIELDS,
    _NARRATIVE_PRESENTATION_EDIT_FIELDS,
    edit_touches_identities,
    render_time_only_edit,
)


def test_scene_name_is_an_editable_field_of_shot_update() -> None:
    """前提①：scene_name 必须在 shot.update 的可改白名单里，否则前端改了也不落库。

    取模块用 ``sys.modules`` 按全限定名，不用 ``getattr(storyboard_ops, "edit_shot")``：
    子模块 edit_shot.py 又导出了一个同名函数 ``edit_shot``，包属性被那个函数盖住，
    getattr 会静默返回函数而不是模块（本仓已有同类事故记录）。
    """
    import importlib
    import inspect
    import sys

    importlib.import_module("app.domain.storyboard_ops.edit_shot")
    module = sys.modules["app.domain.storyboard_ops.edit_shot"]
    source = inspect.getsource(module)
    assert '"scene_name"' in source, "shot.update 的 editable_keys 不再包含 scene_name"


def test_scene_name_is_presentation_not_narrative_semantics() -> None:
    """前提②：scene_name 属于呈现类字段，不触发叙事语义修复闸门。

    落在语义侧的字段会被 _raise_narrative_semantic_mutation_required 以 409 挡住，
    要求走「候选 -> 全板叙事验证 -> 冷观众盲审 -> 原子发布」。换一张参考图不该付
    那个代价——它不改变故事，只改变这一段取哪张图。
    """
    assert "scene_name" in _NARRATIVE_PRESENTATION_EDIT_FIELDS
    assert "scene_setting" in _NARRATIVE_PRESENTATION_EDIT_FIELDS
    assert "scene_time" in _NARRATIVE_PRESENTATION_EDIT_FIELDS


def test_scene_rebinding_does_not_trigger_appellation_resolution() -> None:
    """前提③：只改场景不得触发「新增未解析称谓」检查。

    分镜自己登记的群演（``entity:...``）会被那条检查当成新增称谓拒掉——2026-09-15
    实测第 1 集第 17 镜改转场、第 2 集多镜改时段都被 422，正是同一个坑。换场景时
    镜头人物名单原封不动，不该走那条路径。
    """
    assert "scene_name" not in IDENTITY_BEARING_EDIT_FIELDS
    assert edit_touches_identities({"scene_name"}) is False
    assert edit_touches_identities({"scene_name", "scene_time", "scene_setting"}) is False


def test_scene_rebinding_is_not_render_time_only_so_impact_is_shown() -> None:
    """前提④：换场景**不是**成片阶段字段，下游必须照常失效。

    ``transition`` 那类改动只影响最终剪辑，可以不动已生成的视频；换场景改的是这一段
    取哪张参考图，已生成的视频是照旧图出的，必须被判失效——前端的影响预览正是据此
    显示「保存会让 N 项下游产物失效」。如果哪天有人把 scene_name 也划进成片阶段字段，
    界面就会承诺一个不存在的「无损更换」，而用户手上留着一段与新场景对不上的视频。
    """
    assert "scene_name" not in RENDER_TIME_ONLY_EDIT_FIELDS
    assert render_time_only_edit({"scene_name"}) is False
    assert render_time_only_edit({"scene_name", "transition"}) is False
