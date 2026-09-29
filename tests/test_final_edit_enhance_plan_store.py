"""``app.final_edit_enhance.plan_store``：编排计划的指纹缓存边车文件——指纹
相等才命中，不看任何"是否已生成过"的状态位；损坏/指纹不符都按未命中处理，
不抛异常中断合成。
"""
from __future__ import annotations

import json

from app.final_edit_enhance import plan_store
from app.final_edit_enhance.plan_generate import EnhancementPlan, ResolvedMusicCue, ResolvedTeaserClip


def _plan() -> EnhancementPlan:
    return EnhancementPlan(
        music_cues=(ResolvedMusicCue(1, "t1"),),
        teaser_clips=(ResolvedTeaserClip(1, 0.0, "开场"),),
        monologue_lines=(),
        dropped=({"feature": "teaser", "item": {"shot_no": 2}, "reason": "越界"},),
        teaser_total_duration_s=2.0,
    )


def test_cache_path_derives_from_final_path(tmp_path) -> None:
    final_path = tmp_path / "episode.mp4"
    assert plan_store.cache_path(final_path) == tmp_path / "episode.enhancement-plan.json"


def test_load_cached_plan_missing_file_returns_none(tmp_path) -> None:
    assert plan_store.load_cached_plan(tmp_path / "no-such-file.json", "fp") is None


def test_save_and_load_round_trip(tmp_path) -> None:
    path = tmp_path / "episode.enhancement-plan.json"
    plan = _plan()
    plan_store.save_plan(path, "fp-1", plan)
    loaded = plan_store.load_cached_plan(path, "fp-1")
    assert loaded == plan


def test_load_cached_plan_fingerprint_mismatch_returns_none(tmp_path) -> None:
    path = tmp_path / "episode.enhancement-plan.json"
    plan_store.save_plan(path, "fp-1", _plan())
    assert plan_store.load_cached_plan(path, "fp-2") is None


def test_load_cached_plan_corrupted_json_returns_none(tmp_path) -> None:
    path = tmp_path / "episode.enhancement-plan.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert plan_store.load_cached_plan(path, "fp-1") is None


def test_load_cached_plan_with_legacy_end_s_field_fails_clean_into_none(tmp_path) -> None:
    """2026-09-29 之前 ``ResolvedTeaserClip`` 还带 ``end_s`` 字段，旧缓存 JSON
    的 ``teaser_clips`` 条目里因此仍有 ``end_s`` 这个键。即便指纹恰好撞车
    （比如 ``_PLAN_RULES_VERSION`` 忘记 bump），``ResolvedTeaserClip(**c)``
    也会因为多出一个未知关键字参数直接 ``TypeError``——不能把半旧的形状半读
    进来，必须干净地退回"没有可用缓存"，交给调用方按新规则重新生成一份。"""
    path = tmp_path / "episode.enhancement-plan.json"
    legacy_payload = {
        "fingerprint": "fp-1",
        "music_cues": [],
        "teaser_clips": [{"shot_no": 1, "start_s": 0.0, "end_s": 3.0, "reason": "开场"}],
        "monologue_lines": [],
        "dropped": [],
        "teaser_total_duration_s": 3.0,
    }
    path.write_text(json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")
    assert plan_store.load_cached_plan(path, "fp-1") is None
