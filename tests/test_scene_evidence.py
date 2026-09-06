"""场景判定拿到原文依据与结构候选（2026-09-06 场景库近重复：判定只看标签本身，没有材料区分「广场」与「外宗广场」）。"""
from __future__ import annotations

from app.production import scene_evidence as ev
from app.production.scene_granularity import anchor_discovery_sources, scene_granularity_prompt
from app.schemas import Scene
from app.source_excerpt import index_source_segments

_TEXT = (
    "孟浩走出洞府，沿着山路来到外宗广场。\n\n"
    "广场上早已聚满了外宗弟子，人声鼎沸。\n\n"
    "远处的南峰洞府外，两个老者盘膝而坐。"
)


def _scene(name: str, key: str, aliases: list[str] | None = None, excerpt: str = "") -> Scene:
    return Scene(name=name, scene_canonical="x" * 30, aliases=aliases or [], discovery_sources=anchor_discovery_sources(excerpt, key, ""))


def test_location_text_strips_time_and_generic_suffix() -> None:
    assert ev.location_text("夜/外宗广场") == "外宗广场"
    assert ev.location_text("国漫广场场景") == "国漫广场"
    assert ev.location_text("场景") == "场景"  # 只剩后缀时原样保留


def test_evidence_prefers_full_match_then_backs_off_to_tail() -> None:
    segments = index_source_segments(_TEXT)
    assert "来到外宗广场" in ev.scene_label_evidence("外宗广场", segments)
    backed_off = ev.scene_label_evidence("外宗中心广场", segments)  # 整串没有 → 「中心广场」没有 → 「广场」
    assert "外宗广场" in backed_off and "聚满了外宗弟子" in backed_off
    assert ev.scene_label_evidence("宝阁", segments) == ""  # 原文没有就空着，不编


def test_structural_candidates_share_literal_and_come_with_excerpts() -> None:
    scenes = [
        _scene("外宗广场", "外宗广场", ["靠山宗外宗广场"], "来到外宗广场"),
        _scene("南峰洞府外", "靠山宗南峰山脚下洞府外", [], "南峰洞府外"),
        _scene("宝阁内", "宝阁内"),
    ]
    picked = ev.structural_scene_candidates("这片广场场景", scenes)  # 与「外宗广场」共享中心词「广场」
    assert [s.name for s in picked] == ["外宗广场"]
    assert [s.name for s in ev.structural_scene_candidates("东峰洞府外", scenes)] == ["南峰洞府外"]  # 候选只是摆上桌，选不选由判定定
    block = ev.candidate_block(picked)
    assert "外宗广场｜别名：靠山宗外宗广场｜location_key：外宗广场｜原文摘录：来到外宗广场" in block
    assert ev.structural_scene_candidates("山", scenes) == []  # 单字不比
    assert ev.candidate_block([]) == ""


def test_prompt_renders_candidates_section_only_when_present() -> None:
    kwargs = dict(spatial_context="广场上早已聚满了外宗弟子", style="国漫", style_rule="r", known_scenes=[("外宗广场", "c")],
                  ep_label="第 6 集", canonical_min=30, canonical_max=60, same_location_match_rule="rule")
    with_block = scene_granularity_prompt("广场", candidates_block="- 外宗广场｜别名：（无）", **kwargs)
    assert "结构候选" in with_block and "- 外宗广场｜别名：（无）" in with_block
    assert "不加「场景/外景/内景」这类后缀" in with_block
    assert "结构候选" not in scene_granularity_prompt("广场", **kwargs)
