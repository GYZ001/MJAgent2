"""接线守卫：定妆照双视角改造（2026-10-01）的 costume_mode 管线确实接进了
2.x 分镜包主通路，不是写好了字段却没传。

只读复核在落地前已经实测复现过一次"接了字段却没接进主通路"：``costume_mode``
此前只接到了 ``app.video_modes.asset_lookup.character_reference_assets`` 的
单图回退分支（极少命中），``app.video_modes.reference_assemble._build_library_
reference_assets``（2.x 段真正命中的路径）从未把它传给 ``_asset_from_path``，
"只锁长相"文案在真实生成请求里形同虚设。本文件用源码级断言把这条线钉住，防止
将来重构又把它静默焊断（CLAUDE.md「拆包/新增判据必须配套守卫测试」同一精神）。
"""
from __future__ import annotations

import inspect

from app import multiview
from app.video_modes import reference_assemble


def test_build_library_reference_assets_forwards_costume_mode_to_asset_from_path():
    source = inspect.getsource(reference_assemble._build_library_reference_assets)
    assert "costume_mode=anchor.get(\"costume_mode\")" in source


def test_library_anchor_assets_from_manifest_carries_costume_mode():
    source = inspect.getsource(multiview.library_anchor_assets_from_manifest)
    assert '"costume_mode": view.get("costume_mode")' in source


def test_storyboard_pack_asset_dependencies_calls_pick_character_reference_view():
    source = inspect.getsource(multiview._storyboard_pack_asset_dependencies)
    assert "pick_character_reference_view(" in source
    assert "wardrobe_matches_default=str(entry.get(\"wardrobe_matches_default\")" in source


def test_wardrobe_plan_segment_rule_text_still_wired_into_task_payload_rules():
    """按段选图依赖 wardrobe_matches_default 规则真的进了提示词，不只是加了
    字段没接线——见 tests/test_storyboard_wardrobe_plan.py 的端到端接线测试。"""
    from app.production import storyboard_segment_chains as chains_module

    source = inspect.getsource(chains_module._task_payload_rules)
    assert "_wardrobe_plan.segment_rule_text(*moments.wardrobe_advance" in source
