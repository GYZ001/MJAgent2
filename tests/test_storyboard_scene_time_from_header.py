"""shots.scene_time 取剧本体段头的时段，不再被映射包的整集锚点（含人物年龄）压掉。

2026-09-15 龙猫出爪：第 1 集 17 镜 scene_time 全是「五十岁上下」、第 2 集 15 镜全是「四十岁上下」——
映射包把人物介绍里的年龄当成 age 锚点、且锚点是整集共享的，age 又排在最高优先级。
"""

from app.production.storyboard_pack import _timeline_anchor_scene_time

ANCHORS = [{"kind": "age", "value": "四十岁上下"}, {"kind": "relative", "value": "三年"}]


def test_screenplay_header_time_wins_over_anchors() -> None:
    excerpt = "【段 12｜人间·老街｜夜】\n人物：周晚\n阿凯：……八个人一个时段？不是吧。"
    assert _timeline_anchor_scene_time(ANCHORS, excerpt) == "夜"


def test_without_header_falls_back_to_most_specific_anchor_or_empty() -> None:
    assert _timeline_anchor_scene_time(ANCHORS, "少年推门而入。") == "四十岁上下"
    assert _timeline_anchor_scene_time([], "少年推门而入。") == ""
