import pytest

from app.schemas import extract_json


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
