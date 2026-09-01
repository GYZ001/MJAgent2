"""JSON 修复：字符串/引号相关的窄修复（不处理容器结构问题，见 .json_repair_structure）。

每个函数只处理一种能被机械判定、无需语义猜测的具体腐化形态（模型偶尔把弯
引号写成 ASCII 引号、控制字符未转义、模型使用全角右引号收尾等）。
"""
from __future__ import annotations

import json

def _embedded_string_array_end(text: str, start: int) -> int | None:
    """Return the end of an exact ``list[str]`` fragment inside prose."""
    previous_index = start - 1
    while previous_index >= 0 and text[previous_index].isspace():
        previous_index -= 1
    if previous_index < 0 or text[previous_index] == '"':
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    if not value or not isinstance(value, list):
        return None
    if any(not isinstance(item, str) for item in value):
        return None
    return start + end


def _escape_unescaped_inner_quotes(text: str) -> str:
    """只修复能明确判定为 JSON 字符串内容的裸双引号。

    模型偶尔把原文弯引号改成 ASCII 双引号，例如
    ``"这个"天才"仍在原地"``。字符串真正的结束引号后只能跟
    ``:``, ``,``、``]``、``}`` 或文本结束；其他位置的裸引号可安全
    视为字符串内容并转义。缺逗号、括号错误等结构问题不会被放行。
    """
    repaired: list[str] = []
    in_string = False
    escaped = False
    embedded_string_array_end: int | None = None
    length = len(text)
    for index, char in enumerate(text):
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            continue
        if (
            embedded_string_array_end is not None
            and index < embedded_string_array_end
        ):
            if char in {'"', "\\"}:
                repaired.append("\\")
            repaired.append(char)
            continue
        if escaped:
            repaired.append(char)
            escaped = False
            continue
        if char == "\\":
            repaired.append(char)
            escaped = True
            continue
        if char != '"':
            repaired.append(char)
            if char == "[":
                embedded_string_array_end = _embedded_string_array_end(
                    text,
                    index,
                )
            elif (
                embedded_string_array_end is not None
                and index + 1 >= embedded_string_array_end
            ):
                embedded_string_array_end = None
            continue

        next_index = index + 1
        while next_index < length and text[next_index].isspace():
            next_index += 1
        next_char = text[next_index] if next_index < length else ""
        if next_char in {":", ",", "]", "}"} or not next_char:
            repaired.append(char)
            in_string = False
        else:
            repaired.extend(("\\", char))
    return "".join(repaired)


def _escape_json_control_chars_in_strings(text: str) -> str:
    """Escape raw control characters only while inside JSON strings."""
    replacements = {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    repaired: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
                repaired.append(char)
                continue
            if char == "\\":
                escaped = True
                repaired.append(char)
                continue
            if char == '"':
                in_string = False
                repaired.append(char)
                continue
            if ord(char) < 0x20:
                repaired.append(
                    replacements.get(char, f"\\u{ord(char):04x}")
                )
                continue
            repaired.append(char)
            continue
        if char == '"':
            in_string = True
        repaired.append(char)
    return "".join(repaired)


def _has_unclosed_json_string_prefix(text: str) -> bool:
    """Return whether the prefix ends inside an ASCII-quoted JSON string."""
    in_string = False
    escaped = False
    for char in text:
        if not in_string:
            if char == '"':
                in_string = True
            continue
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_string = False
    return in_string


def _repair_fullwidth_closing_quote(text: str) -> str:
    """Close a JSON string when the model used ``”`` as the closing quote.

    The model frequently writes Chinese text containing full-width quotes and
    occasionally replaces the required ASCII closing quote with ``”``.  When
    that happens the JSON string is left open until the next structural token.
    This narrow repair only converts ``”`` to ``"`` if it is immediately
    followed (ignoring whitespace) by ``}`` or ``]`` and no ASCII quote appears
    before that closer.
    """
    repaired: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            if escaped:
                repaired.append(char)
                escaped = False
                index += 1
                continue
            if char == "\\":
                repaired.append(char)
                escaped = True
                index += 1
                continue
            if char == '"':
                repaired.append(char)
                in_string = False
                index += 1
                continue
            if char == "”":
                look = index + 1
                while look < length and text[look].isspace():
                    look += 1
                if look < length and text[look] in "}]":
                    after = look + 1
                    while after < length and text[after].isspace():
                        after += 1
                    if after >= length or text[after] in ",}]":
                        probe = index + 1
                        has_ascii_quote = False
                        while probe < look:
                            if text[probe] == "\\":
                                probe += 2
                                continue
                            if text[probe] == '"':
                                has_ascii_quote = True
                                break
                            probe += 1
                        if not has_ascii_quote:
                            repaired.append('"')
                            in_string = False
                            index += 1
                            continue
            repaired.append(char)
            index += 1
            continue
        if char == '"':
            in_string = True
        repaired.append(char)
        index += 1
    return "".join(repaired)
