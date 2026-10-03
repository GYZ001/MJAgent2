"""场景状态图分组的纯函数判据：``app.video_modes.scene_state_views.
group_scene_state_runs`` 是生成（整集扫描缺口）与装配（本段该发哪张状态图）
共用的唯一分组函数（见该函数所在模块文档）。本文件只测分组本身，不碰数据库。
"""
from __future__ import annotations

import json

from app.video_modes.scene_state_views import (
    find_run_for_shot,
    group_scene_state_runs,
    normalize_state_description,
    state_key_for,
)

_SCENE = "scene_ref_a"


def _row(shot_no: int, scenes: list[dict]) -> dict:
    """照 ``shots`` 表一行的最小形状：``group_scene_state_runs`` 只读
    ``shot_no``/``shot_contract_json`` 两个键。"""
    payload = {"storyboard_pack_segment": {"resources": {"scenes": scenes}}}
    return {"shot_no": shot_no, "shot_contract_json": json.dumps(payload, ensure_ascii=False)}


def _scene_entry(matches: str, description: str = "", scene_reference_id: str = _SCENE) -> dict:
    return {
        "scene_id": "scene:温念的出租屋", "scene_reference_id": scene_reference_id,
        "scene_state_matches_card": matches, "description": description,
    }


def _group(rows: list[dict]) -> list[dict]:
    return group_scene_state_runs(rows, ready_scene_reference_ids={_SCENE})


def test_consecutive_no_segments_form_one_run():
    rows = [
        _row(15, [_scene_entry("no", "满地积水")]),
        _row(16, [_scene_entry("no", "满地积水")]),
        _row(17, [_scene_entry("unsure", "")]),
    ]
    runs = _group(rows)
    assert len(runs) == 1
    assert runs[0]["scene_reference_id"] == _SCENE
    assert runs[0]["start_shot_no"] == 15
    assert runs[0]["shot_nos"] == [15, 16, 17]
    assert runs[0]["description"] == "满地积水"


def test_yes_ends_the_run_and_a_later_no_starts_a_new_one():
    rows = [
        _row(15, [_scene_entry("no", "满地积水")]),
        _row(16, [_scene_entry("yes", "")]),
        _row(17, [_scene_entry("no", "水退了但家具还歪着")]),
    ]
    runs = _group(rows)
    assert len(runs) == 2
    assert runs[0]["shot_nos"] == [15]
    assert runs[1]["start_shot_no"] == 17
    assert runs[1]["shot_nos"] == [17]
    assert runs[1]["description"] == "水退了但家具还歪着"


def test_empty_string_is_not_evaluated_yet_and_also_ends_the_run():
    """空字符串是"从未被问过"而不是"确认一致"，但同样不是新证据支持"仍然
    不一致"——与 yes 一样结束当前串，不得被并入 unsure 分支。"""
    rows = [
        _row(15, [_scene_entry("no", "满地积水")]),
        _row(16, [_scene_entry("", "")]),
        _row(17, [_scene_entry("unsure", "水还没退")]),
    ]
    runs = _group(rows)
    assert len(runs) == 2
    assert runs[0]["shot_nos"] == [15]
    assert runs[1]["shot_nos"] == [17]


def test_shots_not_mentioning_the_scene_do_not_break_the_run():
    rows = [
        _row(15, [_scene_entry("no", "满地积水")]),
        _row(16, [_scene_entry("no", "", scene_reference_id="scene_ref_other")]),
        _row(17, [_scene_entry("no", "")]),
    ]
    runs = _group(rows)
    assert len(runs) == 1
    assert runs[0]["shot_nos"] == [15, 17]


def test_description_falls_back_to_first_non_empty_entry_in_the_run():
    rows = [
        _row(15, [_scene_entry("no", "")]),
        _row(16, [_scene_entry("no", "")]),
        _row(17, [_scene_entry("no", "满地积水，鞋柜歪倒")]),
        _row(18, [_scene_entry("unsure", "另一句描述不会覆盖已经取到的")]),
    ]
    runs = _group(rows)
    assert len(runs) == 1
    assert runs[0]["description"] == "满地积水，鞋柜歪倒"


def test_scene_without_ready_card_image_does_not_participate():
    rows = [_row(15, [_scene_entry("no", "满地积水")])]
    runs = group_scene_state_runs(rows, ready_scene_reference_ids=set())
    assert runs == []


def test_run_still_open_at_episode_end_is_flushed():
    rows = [_row(15, [_scene_entry("no", "满地积水")]), _row(16, [_scene_entry("unsure", "")])]
    runs = _group(rows)
    assert len(runs) == 1
    assert runs[0]["shot_nos"] == [15, 16]


def test_find_run_for_shot_matches_by_scene_and_membership():
    rows = [_row(15, [_scene_entry("no", "满地积水")]), _row(16, [_scene_entry("no", "")])]
    runs = _group(rows)
    assert find_run_for_shot(runs, _SCENE, 16) is runs[0]
    assert find_run_for_shot(runs, _SCENE, 99) is None
    assert find_run_for_shot(runs, "other-scene", 15) is None


def test_state_key_is_stable_for_same_inputs_and_changes_with_description():
    key_a = state_key_for(_SCENE, 15, "满地积水")
    key_b = state_key_for(_SCENE, 15, "满地积水。")  # 只差结尾标点，结构归一化后应相同
    key_c = state_key_for(_SCENE, 15, "水退了")
    assert key_a == key_b
    assert key_a != key_c
    assert len(key_a) == 16


def test_state_key_changes_with_start_shot_no():
    """同一场景在同一集里出现两段文字相同的状态串（例如"水又涨回来了"与
    第一次涨水描述恰好相同）必须判成不同状态，不能因描述偶然相同而共享
    一张状态图——串首 shot_no 入指纹正是为了区分这种情况。"""
    assert state_key_for(_SCENE, 15, "满地积水") != state_key_for(_SCENE, 40, "满地积水")


def test_normalize_state_description_collapses_whitespace_and_trailing_punctuation():
    assert normalize_state_description("满地  积水，\n鞋柜歪倒。") == "满地 积水， 鞋柜歪倒"
