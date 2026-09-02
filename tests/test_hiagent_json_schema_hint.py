"""json_schema 请求同样要带「json」字样（ERR-20260902-3d19ef）。

换到 DeepSeek V4 Pro（HiAgent 网关）后，映射台事件链抽取被 HTTP 400 拒绝：
「'messages' must contain the word 'json' in some form, to use 'response_format' of type
'json_schema'」。既有补丁只给 json_object 加协议提示，json_schema 请求原样发出。
"""
from __future__ import annotations

from app import hiagent


def test_json_schema_adds_json_instruction_without_mutating_messages() -> None:
    messages = [{"role": "user", "content": "按 Schema 输出事件链"}]
    response_format = {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": {}}}

    normalized = hiagent._messages_for_response_format(messages, response_format)

    assert messages == [{"role": "user", "content": "按 Schema 输出事件链"}]
    assert normalized is not messages
    assert normalized[0]["role"] == "system"
    assert "json" in normalized[0]["content"].lower()
    assert normalized[1:] == messages


def test_json_schema_keeps_existing_json_mention_unchanged() -> None:
    messages = [{"role": "system", "content": "只输出一个 JSON 对象"}, {"role": "user", "content": "生成"}]

    normalized = hiagent._messages_for_response_format(messages, {"type": "json_schema", "json_schema": {}})

    assert normalized == messages
    assert sum("json" in str(m.get("content") or "").lower() for m in normalized) == 1


def test_text_response_format_still_untouched() -> None:
    messages = [{"role": "user", "content": "写一段自由文本"}]
    assert hiagent._messages_for_response_format(messages, {"type": "text"}) is messages
