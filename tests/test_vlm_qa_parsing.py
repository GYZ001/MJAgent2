from app.stages import _parse_qa_result


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


def test_parse_qa_result_normalizes_complete_json() -> None:
    raw = '{"character_match": 95, "action_match": 0.8, "clean_frame": 1, "issues": "ok"}'

    qa = _parse_qa_result(raw, ["character_match", "action_match", "clean_frame"])

    assert qa["character_match"] == 0.95
    assert qa["action_match"] == 0.8
    assert qa["clean_frame"] == 1.0
    assert qa["overall"] == 0.917
    assert qa["issues"] == ["ok"]
