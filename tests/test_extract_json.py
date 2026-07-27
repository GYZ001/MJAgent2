import pytest

from app import errors
from app.schemas import extract_json
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


def test_json_stage_failure_has_specific_error_code_and_non_blind_hint() -> None:
    exc = StageError("剧本首次整版 Baseline", ["JSON 解析失败（line 39）"])
    assert errors.classify(exc) == ("generation", "JSON")
    hint = errors.CATEGORIES["generation"]["hint"]
    assert "先按错误码检查具体原因" in hint
