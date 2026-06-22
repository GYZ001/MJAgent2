import asyncio

from app import hiagent
from app.stages import _parse_qa_result, qa_shot


def test_parse_qa_result_recovers_scores_from_truncated_json() -> None:
    raw = '''```json
{
  "expectation_match": 0.95,
  "continuity": 1.0,
  "clean_frame": 0.8,
'''

    qa = _parse_qa_result(raw, ["expectation_match", "continuity", "clean_frame"])

    assert qa["expectation_match"] == 0.95
    assert qa["continuity"] == 1.0
    assert qa["clean_frame"] == 0.8
    assert qa["overall"] == 0.917
    assert qa["issues"]
    assert qa["qa_recovered"] is True


def test_parse_qa_result_recovers_scores_from_markdown_explanation() -> None:
    raw = """
No reference image provided. *Score:* 1.0.
**clean_frame**: There is a small AI watermark. Score: 0.75.
"expectation_match": 0.9
"""

    qa = _parse_qa_result(
        raw,
        ["expectation_match", "continuity", "clean_frame"],
        defaults={"continuity": 1.0},
    )

    assert qa["expectation_match"] == 0.9
    assert qa["continuity"] == 1.0
    assert qa["clean_frame"] == 0.75
    assert qa["overall"] == 0.883
    assert any("文字" in issue or "水印" in issue for issue in qa["issues"])
    assert qa["qa_recovered"] is True


def test_parse_qa_result_normalizes_complete_json() -> None:
    raw = '{"character_match": 95, "action_match": 0.8, "clean_frame": 1, "issues": "ok"}'

    qa = _parse_qa_result(raw, ["character_match", "action_match", "clean_frame"])

    assert qa["character_match"] == 0.95
    assert qa["action_match"] == 0.8
    assert qa["clean_frame"] == 1.0
    assert qa["overall"] == 0.917
    assert qa["issues"] == ["ok"]
    assert qa["qa_recovered"] is False


def test_parse_qa_result_marks_missing_required_score_untrusted() -> None:
    qa = _parse_qa_result(
        '{"character_match": 0.9, "clean_frame": 0.9, "issues": []}',
        ["character_match", "action_match", "clean_frame"],
    )

    assert qa["action_match"] == 0.0
    assert qa["qa_recovered"] is True


def test_video_qa_caps_overall_at_character_and_action_main_scores(monkeypatch) -> None:
    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "overall 不得高于" in expectation
        return (
            '{"character_match": 0.9, "action_match": 0.35, '
            '"clean_frame": 1.0, "overall": 0.95, "issues": ["核心动作未出现"]}'
        )

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    qa = asyncio.run(qa_shot(["frame"], "角色拿起钥匙", "夜，咖啡厅", ["黑发灰衣"]))

    assert qa["overall"] == 0.35
    assert qa["qa_recovered"] is False
