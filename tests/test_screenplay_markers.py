"""剧本体原文结构标记解析（2026-09-15《龙猫出爪》第 1 集：13→14、16→17 衔接生硬）。"""
from __future__ import annotations

from app.production.screenplay_markers import (
    SAME_SCENE_TRANSITION,
    SCENE_CHANGE_TRANSITION,
    explicit_transition_marker,
    map_transition,
    parse_scene_header,
    required_beats,
    scene_changed,
    scene_changed_by_resource_scenes,
    transition_between,
    transition_with_resource_bypass,
)

SEG_05 = "【段 05｜人间·诊室｜夜】\n人物：周晚、龙猫\n龙猫：记住了。\n【转场：爪印光圈】"
SEG_06 = "【段 06｜爪间·掌心接线台｜夜】\n人物：龙猫、散散\n（俯瞰：一座爪印形状的村子。）"
SEG_10 = "【段 10｜爪间·记线阁｜夜】\n人物：龙猫\n龙猫：三十天。"
SEG_11 = "【段 11｜人间·医院·前台｜清晨】\n人物：周晚、小李、龙猫\n周晚：……这什么？"
SEG_12 = ("【段 12｜人间·老街｜清晨】\n人物：周晚、小李\n（周晚站在门口，看向街对面。）\n"
          "（格局镜：从门口升起，老街清晨，两家门面隔街相对，一家亮着招牌，一家挂着横幅。）\n"
          "（钩子：切爪间，接线台上，红线卷里的那根灰线，比昨晚粗了一圈。）")


def test_scene_header_and_change_detection() -> None:
    assert parse_scene_header(SEG_11) == ("人间·医院·前台", "清晨")
    assert parse_scene_header("（没有段头的普通段落。）") is None
    assert scene_changed(SEG_10, SEG_11) is True      # 爪间·夜 → 人间·清晨
    assert scene_changed(SEG_11, SEG_11) is False     # 同一场戏拆成两段
    assert scene_changed("普通段落", SEG_11) is True   # 上一段无段头、本段有
    assert scene_changed(SEG_11, "普通段落") is False  # 本段无段头：不判换场


def test_explicit_transition_marker_wins_and_maps_to_supported_names() -> None:
    assert explicit_transition_marker(SEG_05) == "爪印光圈"
    assert transition_between(SEG_05, SEG_06) == "遮挡转场"
    assert map_transition("叠化") == "叠化" and map_transition("黑场") == "淡出淡入" and map_transition("闪白") == "闪白"
    assert map_transition("甩镜") == "甩镜" and map_transition("匹配剪辑") == "匹配剪辑" and map_transition("未知手法") == "遮挡转场"


def test_implicit_scene_change_dissolves_and_same_scene_cuts() -> None:
    assert transition_between(SEG_10, SEG_11) == "叠化"
    assert transition_between(SEG_11, SEG_12) == "叠化"   # 同为清晨但地点变了
    assert transition_between(SEG_11, SEG_11) == "硬切"


def test_required_beats_keep_author_labels_in_order() -> None:
    assert required_beats(SEG_12) == [
        "格局镜：从门口升起，老街清晨，两家门面隔街相对，一家亮着招牌，一家挂着横幅。",
        "钩子：切爪间，接线台上，红线卷里的那根灰线，比昨晚粗了一圈。",
    ]
    assert required_beats(SEG_06) == []


def test_segment_structure_and_rules_and_beat_check() -> None:
    from types import SimpleNamespace

    from app.production.screenplay_markers import required_beats_errors, segment_structure, structure_rules

    structure = segment_structure(SEG_11, SEG_12)
    assert structure["scene_change"] is True and structure["transition_from_previous"] == "叠化"
    assert len(structure["required_beats"]) == 2
    rules = structure_rules(structure)
    assert any("定场镜" in r and "不写「残留上一段色调」" in r for r in rules)
    assert any("必拍镜头" in r and "钩子镜排最后" in r for r in rules)
    assert any("同一场戏" in r for r in structure_rules(segment_structure(SEG_11, SEG_11)))
    assert segment_structure("", SEG_11)["transition_from_previous"] == "硬切"  # 本集第一段
    prompt_missing_hook = SimpleNamespace(prompt_text="镜头1：周晚站在门口看向街对面。镜头2：缓慢升起拉远的大远景，老街清晨两家门面隔街相对，一家亮着招牌一家挂着横幅。")
    errors = required_beats_errors(prompt_missing_hook, structure["required_beats"])
    assert len(errors) == 1 and "钩子" in errors[0] and "格局镜" not in errors[0]
    prompt_full = SimpleNamespace(prompt_text=prompt_missing_hook.prompt_text + "镜头3：切爪间，接线台上，红线卷里的那根灰线比昨晚粗了一圈。")
    assert required_beats_errors(prompt_full, structure["required_beats"]) == []


def test_header_wording_variants_and_capacity_splits_are_same_scene() -> None:
    seg03 = "【段 03｜人间·医院·诊室｜夜】\n周晚：我是兽医，不是会计。"
    seg04 = "【段 04｜人间·诊室｜夜】\n阿凯：姐，你这样不行。"
    assert scene_changed(seg03, seg04) is False and transition_between(seg03, seg04) == "硬切"
    seg06 = "【段 06｜爪间·掌心接线台｜夜】\n龙猫：先理线。"
    seg07 = "【段 07｜爪间·接线台｜夜】\n算算：成了。"
    assert scene_changed(seg06, seg07) is False
    # 同一原文段拆成两段（容量拆分）：段尾的【转场】不在两半之间
    assert transition_between(SEG_05, SEG_05) == "硬切" and scene_changed(SEG_05, SEG_05) is False
    assert transition_between(SEG_05, SEG_06) == "遮挡转场"


def test_scene_changed_by_resource_scenes_requires_both_sides_nonempty_and_different() -> None:
    """小说体原文没有段头，判据只能靠相邻两段各自登记的 resources.scenes；
    任一侧为空都不下判断（不得拿缺失数据伪造"同场"），两边都非空且不同才算换场。"""
    assert scene_changed_by_resource_scenes({"scene_a"}, {"scene_b"}) is True
    assert scene_changed_by_resource_scenes({"scene_a"}, {"scene_a"}) is False
    assert scene_changed_by_resource_scenes({"scene_a", "scene_b"}, {"scene_b"}) is True  # 部分重叠也算变了
    assert scene_changed_by_resource_scenes(set(), {"scene_a"}) is False
    assert scene_changed_by_resource_scenes({"scene_a"}, set()) is False
    assert scene_changed_by_resource_scenes(set(), set()) is False


def test_scene_changed_by_resource_scenes_keeps_same_scene_when_previous_is_subset() -> None:
    """本段计划场景是上一段场景的超集（真实情形：本段开头仍在上一场景，段尾一条
    「（钩子：切某地……）」把目的地场景也登记进了本段 asset_manifest）不算换场——钩子切到
    哪已经由 required_beats 单独告诉模型，镜头1 仍应延续上一段的同场站位起幅。
    与"部分重叠也算变了"（上一条用例，current 缩小为 previous 的子集）方向相反，不冲突：
    比较集合用包含，不用相等，只有"previous 不再被 current 完整包含"才算换场。"""
    assert scene_changed_by_resource_scenes({"scene_a"}, {"scene_a", "scene_b"}) is False


def test_transition_with_resource_bypass_upgrades_same_scene_only() -> None:
    """只在文本判据判定为同场时才补台升级；已经是显式/结构化换场转场的不覆盖，
    这样才是与既有判据取「或」而不是替代它。"""
    # 文本判据同场 + 资源判据换场 -> 升级为换场默认转场
    assert transition_with_resource_bypass(SAME_SCENE_TRANSITION, {"a"}, {"b"}) == SCENE_CHANGE_TRANSITION
    # 文本判据同场 + 资源判据同场 -> 维持同场
    assert transition_with_resource_bypass(SAME_SCENE_TRANSITION, {"a"}, {"a"}) == SAME_SCENE_TRANSITION
    # 文本判据同场 + 资源信息缺失（任一侧为空）-> 不升级
    assert transition_with_resource_bypass(SAME_SCENE_TRANSITION, set(), {"b"}) == SAME_SCENE_TRANSITION
    # 文本判据已经是显式转场（非同场默认值）-> 原样返回，不被资源判据覆盖
    assert transition_with_resource_bypass("遮挡转场", {"a"}, {"b"}) == "遮挡转场"
    assert transition_with_resource_bypass(SCENE_CHANGE_TRANSITION, {"a"}, {"a"}) == SCENE_CHANGE_TRANSITION


def test_beat_paraphrased_by_model_counts_as_shot_but_dropped_beat_does_not() -> None:
    from types import SimpleNamespace

    from app.production.screenplay_markers import beat_is_shot, required_beats_errors

    beat = "格局镜：从门口升起，老街夜景，两家门面。"
    # 2026-09-15 第 2 集第 15 段模型原话：整句二元组覆盖率 5/12，旧判据误拦
    shot = "镜头2：全景升起，拍摄机位从周晚站立的门口向上升起，扫过整条夜晚的老街，街对面两家相邻的门面清晰可见。"
    assert beat_is_shot(beat, shot)
    assert not beat_is_shot(beat, "镜头1：周晚锁好店门低头掏出手机，屏幕亮起。")
    hook = "钩子：切阿凯出租屋。屏幕上八张一模一样的卡片叠在一起。"
    assert required_beats_errors(SimpleNamespace(prompt_text=shot), [beat, hook]) and "钩子" in required_beats_errors(SimpleNamespace(prompt_text=shot), [beat, hook])[0]
    assert required_beats_errors(SimpleNamespace(prompt_text=shot + "镜头3：切到阿凯出租屋，屏幕上八张一模一样的卡片叠在一起。"), [beat, hook]) == []
