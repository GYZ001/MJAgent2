"""WS2-2c：场景来源证明缺 anchor_phrase 的确定性修补
（app.production.prep_pack.provenance_repair）。

真实故障形态：B 库 7 天内多次撞见「资产来源证明自校验失败：场景「X」的
provenance.method='resolution' 缺少 anchor_phrase」。这里验证：本集原文
里确实逐字出现该场景名称时，修补能补齐 anchor_phrase/anchor_segments 让
原有自校验通过；原文里根本没出现时不编造锚点，原校验仍然拦截；method
不要求锚点、或 anchor_phrase 本来就非空的场景，修补是空操作。
"""
from __future__ import annotations

from app.production.prep_pack.provenance_repair import (
    repair_scene_anchor_phrases,
    verify_manifest_provenance_with_repair,
)
from app.source_excerpt import SourceSegment


def _segment(index: int, text: str) -> SourceSegment:
    return SourceSegment(segment_id=f"s{index}", text=text, start_offset=0, end_offset=len(text))


def _scene(method: str, anchor_phrase: str = "", display_name: str = "青石坪") -> dict:
    return {
        "scene_id": "scene:1",
        "display_name": display_name,
        "provenance": {"method": method, "anchor_segments": [], "anchor_phrase": anchor_phrase},
    }


def test_missing_anchor_is_repaired_from_source_text_and_passes_verification():
    """真实故障形态：method=resolution 缺 anchor_phrase，但原文里确实逐字
    出现该场景名称——修补后补齐锚点，原有自校验（verify_manifest_
    provenance_with_repair）不再报错。"""
    segments = [
        _segment(1, "云雾缭绕的靠山宗山腰青石坪，能看到一些精美的阁楼环绕山峦八方。"),
        _segment(2, "少年缓缓走上前去。"),
    ]
    asset_manifest = {"scenes": [_scene("resolution")]}
    errors = verify_manifest_provenance_with_repair(segments, asset_manifest)
    assert errors == []
    provenance = asset_manifest["scenes"][0]["provenance"]
    assert provenance["anchor_phrase"]
    assert "青石坪" in provenance["anchor_phrase"]
    assert provenance["anchor_segments"] == [1]


def test_repair_notes_report_what_was_fixed():
    segments = [_segment(1, "青石坪上云雾缭绕。")]
    asset_manifest = {"scenes": [_scene("discovery")]}
    notes = repair_scene_anchor_phrases(segments, asset_manifest)
    assert len(notes) == 1
    assert "青石坪" in notes[0]
    assert "provenance.method" in notes[0]


def test_name_never_appearing_in_source_is_not_fabricated_and_still_fails():
    """修不了的形态：场景名称在本集原文里根本没有逐字出现——不编造锚点，
    原有自校验仍然报错（走既有的降级/拦截路径，不被这里悄悄放过）。"""
    segments = [_segment(1, "少年缓缓走上前去，四周一片寂静。")]
    asset_manifest = {"scenes": [_scene("resolution", display_name="从未出现过的场景名")]}
    errors = verify_manifest_provenance_with_repair(segments, asset_manifest)
    assert len(errors) == 1
    assert "缺少 anchor_phrase" in errors[0]
    provenance = asset_manifest["scenes"][0]["provenance"]
    assert provenance["anchor_phrase"] == ""


def test_method_not_requiring_anchor_is_left_untouched():
    """method 不在 resolution/discovery 范围内（例如 alias_inherited）时，
    自校验本来就不要求非空锚点——修补应当是空操作，不主动去补一个不需要的锚点。"""
    segments = [_segment(1, "青石坪上云雾缭绕。")]
    asset_manifest = {"scenes": [_scene("alias_inherited")]}
    notes = repair_scene_anchor_phrases(segments, asset_manifest)
    assert notes == []
    assert asset_manifest["scenes"][0]["provenance"]["anchor_phrase"] == ""


def test_already_populated_anchor_phrase_is_left_untouched():
    """合法输入：anchor_phrase 已经非空——修补是空操作，不覆盖模型/上游已经
    产出的正确锚点。"""
    segments = [_segment(1, "青石坪上云雾缭绕。")]
    asset_manifest = {"scenes": [_scene("resolution", anchor_phrase="已有的合法锚点")]}
    notes = repair_scene_anchor_phrases(segments, asset_manifest)
    assert notes == []
    assert asset_manifest["scenes"][0]["provenance"]["anchor_phrase"] == "已有的合法锚点"
