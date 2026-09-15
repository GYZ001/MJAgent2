"""字幕只用说出口的话：括号舞台提示不进对齐也不进字幕（2026-09-15《龙猫出爪》「（自言自语）都说我好……」）。"""
from __future__ import annotations

import json

from app.subtitles.episode import shot_line_specs, spoken_text


def test_spoken_text_strips_stage_directions() -> None:
    assert spoken_text("（自言自语）都说我好……") == "都说我好……"
    assert spoken_text("（接过猫，听诊器贴上去）你又喂它火腿肠了？") == "你又喂它火腿肠了？"
    assert spoken_text("别告诉我姐。（顿了顿）她会骂我。") == "别告诉我姐。她会骂我。"
    assert spoken_text("知道了。") == "知道了。"
    assert spoken_text(None) == ""


def test_shot_line_specs_use_spoken_text_for_pack_and_legacy_rows() -> None:
    pack_row = {
        "shot_contract_json": json.dumps({"storyboard_pack_segment": {
            "resources": {"characters": []},
            "dialogue": [{"utterance_id": "U01", "speaker_identity_id": "bible:周晚", "line": "（没抬头）知道了。", "delivery_kind": "spoken_dialogue"}],
        }}, ensure_ascii=False),
        "dialogues": "[]",
    }
    assert [spec.text for spec in shot_line_specs(pack_row)] == ["知道了。"]
    legacy_row = {"shot_contract_json": None, "dialogues": json.dumps([{"speaker": "bible:周晚", "line": "（自言自语）都说我好……", "delivery": "spoken_dialogue"}], ensure_ascii=False)}
    assert [spec.text for spec in shot_line_specs(legacy_row)] == ["都说我好……"]


def test_cached_alignment_with_stale_line_text_is_not_reused() -> None:
    """重新合成仍是「（没接）刘姐」：缓存键不含台词文本，旧对齐结果照常命中。"""
    from app.subtitles.align import LineSpec
    from app.subtitles.episode import cached_alignment_matches

    cached = {"asr_text": "x", "lines": [{"utterance_id": "U01", "text": "（没接）刘姐，我这六年……"}], "extra_speech": []}
    fresh = [LineSpec(utterance_id="U01", text="刘姐，我这六年……", speaker="周晚", delivery_kind="spoken_dialogue")]
    assert cached_alignment_matches(cached, fresh) is False
    same = [LineSpec(utterance_id="U01", text="（没接）刘姐，我这六年……", speaker="周晚", delivery_kind="spoken_dialogue")]
    assert cached_alignment_matches(cached, same) is True
    assert cached_alignment_matches({"lines": []}, fresh) is False
