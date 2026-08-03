import pytest

from app import errors
from app.schemas import extract_json, normalize_screenplay_json_shape
from app.stages import StageError


def test_extract_json_skips_non_json_braces_before_payload() -> None:
    text = """说明：下面这个 {不是 JSON，只是普通说明}

```json
{
  "characters": [],
  "world": {"visual_style_canonical": "竖屏漫画，清晰线稿，柔和光影"}
}
```
"""

    obj = extract_json(text)

    assert obj["characters"] == []
    assert obj["world"]["visual_style_canonical"] == "竖屏漫画，清晰线稿，柔和光影"


def test_extract_json_reports_missing_object() -> None:
    with pytest.raises(ValueError, match="找不到 JSON"):
        extract_json("没有对象")


def test_extract_json_does_not_accept_nested_object_from_broken_root() -> None:
    text = '''{
  "episode_no": 1,
  "shot": {
    "source_excerpt": "少女轻声唤他"萧炎哥哥。"随后弯腰",
    "dialogues": [{"speaker": "萧薰儿", "line": "萧炎哥哥。"}]
  }
}'''

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)


def test_extract_json_can_repair_unescaped_quotes_for_screenplay_only() -> None:
    text = '''{
  "episode_no": 1,
  "turn": {"speaker": "围观者", "line": "这个"天才"仍在原地", "source_text": "这个“天才”仍在原地"}
}'''

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)

    obj = extract_json(text, repair_unescaped_inner_quotes=True)
    assert obj["turn"]["line"] == '这个"天才"仍在原地'
    assert obj["turn"]["source_text"] == "这个“天才”仍在原地"


def test_inner_quote_repair_does_not_hide_json_structure_errors() -> None:
    text = '{"episode_no": 1, "title": "第一集" "logline": "缺少逗号"}'
    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text, repair_unescaped_inner_quotes=True)


def test_extract_json_closes_only_missing_root_brace_at_eof() -> None:
    text = '{"episode_no": 9, "forbidden_additions": ["禁止发明新角色"]'

    assert extract_json(text) == {
        "episode_no": 9,
        "forbidden_additions": ["禁止发明新角色"],
    }


def test_extract_json_combines_inner_quote_and_missing_root_repairs() -> None:
    text = '{"episode_no": 9, "line": "这个"天才"仍在原地"'

    assert extract_json(text, repair_unescaped_inner_quotes=True) == {
        "episode_no": 9,
        "line": '这个"天才"仍在原地',
    }


def test_extract_json_does_not_guess_multiple_missing_closers() -> None:
    text = '{"episode_no": 9, "events": [{"event_id": "E1"'

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)


def test_screenplay_shape_hoists_fields_misnested_under_plot_spine() -> None:
    # One brace closes plot_spine; the missing final brace closes the root.
    # This is the shape observed in ERR-20260803-df0cee.
    text = '''{"episode_no": 9, "plot_spine": {
        "episode_premise": "处理赵武刚",
        "spine_beats": [],
        "must_keep_ending": "孟浩突破",
        "drop_list": [],
        "scene_outline": [{"scene_no": 1}],
        "full_script_text": "【场1】孟浩举起铜镜。",
        "events": [{"event_id": "E1"}],
        "forbidden_additions": ["禁止发明新角色"]}'''

    parsed = extract_json(text)
    normalized, moved = normalize_screenplay_json_shape(parsed)

    assert normalized["plot_spine"] == {
        "episode_premise": "处理赵武刚",
        "spine_beats": [],
        "must_keep_ending": "孟浩突破",
        "drop_list": [],
    }
    assert normalized["scene_outline"] == [{"scene_no": 1}]
    assert normalized["events"] == [{"event_id": "E1"}]
    assert set(moved) == {
        "scene_outline", "full_script_text", "events", "forbidden_additions",
    }


def test_json_stage_failure_has_specific_error_code_and_non_blind_hint() -> None:
    exc = StageError("剧本首次整版 Baseline", ["JSON 解析失败（line 39）"])
    assert errors.classify(exc) == ("generation", "JSON")
    hint = errors.CATEGORIES["generation"]["hint"]
    assert "先按错误码检查具体原因" in hint
