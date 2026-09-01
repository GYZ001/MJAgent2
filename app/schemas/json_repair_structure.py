"""JSON 修复：容器/分隔符结构相关的窄修复（不处理字符串内容，见 .json_repair_strings）。

覆盖漏收尾括号、错误收尾符、缺逗号/冒号、对象键被合并进上一个字符串值、
数组元素漏开括号等具体、可机械判定的结构腐化形态。
"""
from __future__ import annotations

import json
import re

def _close_missing_trailing_containers(text: str) -> str:
    """Close every still-open container at end-of-output, innermost first.

    Providers occasionally stop generating right after a complete, well-formed
    child value while one or more ancestor containers remain open -- e.g. an
    object nested inside an array nested inside the root, with only the
    deepest value fully written and none of the closing brackets above it ever
    emitted (real incident ERR-20260826-93c8e3: one ``}`` and one ``]`` both
    missing, response ended exactly at ``len(content)`` with
    ``finish_reason=stop`` -- not a token-budget truncation).

    This repair only ever sees ``finish_reason=stop`` content: a
    ``finish_reason=='length'`` response is converted into a ``ProviderError``
    by ``app.hiagent._reject_truncated_chat_response`` before ``chat()``
    returns anything, at every one of its call sites (streaming and
    non-streaming) -- so truncated output never reaches ``extract_json`` in
    the first place, and this function does not need to (and must not try to)
    re-derive that distinction from the text alone.

    It is still a pure syntax completion, not a guess: the whole text is
    walked once, string contents are tracked so brackets inside them are
    ignored, and the fix is applied only when the walk finishes cleanly
    outside any string with a non-empty stack of still-open containers -- any
    closer encountered along the way that does not match the top of the stack
    proves the document is corrupt beyond "trailing closers omitted", and this
    refuses to guess (returns the text unchanged). Ending the walk still
    inside an open string (or mid-escape) is refused for the same reason: that
    is a value cut off mid-write, not a value that is complete except for its
    ancestors' closers, and closing containers over it would paper over
    missing content instead of completing it.
    """
    expected_closers: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            expected_closers.append("}")
        elif char == "[":
            expected_closers.append("]")
        elif char in "}]":
            if not expected_closers or expected_closers[-1] != char:
                return text
            expected_closers.pop()

    if not in_string and not escaped and expected_closers:
        return text + "".join(reversed(expected_closers))
    return text


def _repair_trailing_container_closure(text: str) -> str:
    """Replace one wrong EOF closer with the uniquely required close sequence."""
    expected_closers: list[str] = []
    in_string = False
    escaped = False
    last_nonspace = len(text.rstrip()) - 1
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            expected_closers.append("}")
        elif char == "[":
            expected_closers.append("]")
        elif char in "}]":
            if not expected_closers:
                return text
            if expected_closers[-1] != char:
                if index != last_nonspace or in_string or escaped:
                    return text
                return (
                    text[:index]
                    + "".join(reversed(expected_closers))
                    + text[index + 1:]
                )
            expected_closers.pop()
    return text


def _repair_singleton_string_object_fields(
    text: str,
    field_names: tuple[str, ...],
) -> str:
    json_string = r'"(?:\\.|[^"\\])*"'
    for field_name in field_names:
        pattern = re.compile(
            rf'"{re.escape(field_name)}"\s*:\s*\{{\s*({json_string})\s*\}}'
        )
        text = pattern.sub(
            rf'"{field_name}":{{"description":\1}}',
            text,
        )
    return text


def _repair_structural_json_delimiters(text: str) -> str:
    """Repair delimiter omissions that are uniquely implied by JSON nesting."""
    repaired: list[str] = []
    expected_closers: list[str] = []
    in_string = False
    escaped = False
    previous_significant = ""

    def next_token_is_object_key(start: int) -> bool:
        index = start
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != '"':
            return False
        index += 1
        local_escaped = False
        while index < len(text):
            char = text[index]
            if local_escaped:
                local_escaped = False
            elif char == "\\":
                local_escaped = True
            elif char == '"':
                index += 1
                break
            index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        return index < len(text) and text[index] == ":"

    for index, char in enumerate(text):
        if in_string:
            repaired.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                previous_significant = '"'
            continue
        if char == '"':
            in_string = True
            repaired.append(char)
            continue
        if (
            char in "[{"
            and previous_significant in "}]"
            and expected_closers
            and expected_closers[-1] == "]"
        ):
            repaired.append(",")
        if char == "{":
            expected_closers.append("}")
        elif char == "[":
            expected_closers.append("]")
        elif char in "}]":
            if expected_closers and expected_closers[-1] == char:
                expected_closers.pop()
        elif (
            char == ","
            and expected_closers
            and expected_closers[-1] == "]"
            and next_token_is_object_key(index + 1)
        ):
            repaired.append("]")
            expected_closers.pop()
        repaired.append(char)
        if not char.isspace():
            previous_significant = char
    return "".join(repaired)


def _repair_merged_object_string_entry(
    text: str,
    error: json.JSONDecodeError,
) -> str:
    """Split one object key accidentally merged into the preceding string value."""
    if error.msg != "Expecting ',' delimiter" or error.pos >= len(text):
        return text
    if text[error.pos] != ":":
        return text

    prefix = text[:error.pos]
    match = re.search(
        r':\s*"(?P<value>(?:\\.|[^"\\])*)"(?P<space>\s*)$',
        prefix,
    )
    if match is None:
        return text

    value = match.group("value")
    delimiter_index = max(
        value.rfind(","),
        value.rfind("，"),
        value.rfind(";"),
        value.rfind("；"),
    )
    if delimiter_index <= 0 or delimiter_index >= len(value) - 1:
        return text

    previous_value = value[:delimiter_index].strip()
    merged_key = value[delimiter_index + 1:].strip()
    if not previous_value or not merged_key:
        return text

    replacement = (
        f':"{previous_value}","{merged_key}"'
        f'{match.group("space")}'
    )
    return text[:match.start()] + replacement + text[error.pos:]


def _remove_unmatched_root_level_closer(
    text: str,
    error: json.JSONDecodeError,
) -> str:
    """Remove one closer that cannot belong to any open nested container."""
    if error.pos >= len(text) or text[error.pos] not in "]}":
        return text
    expected_closers: list[str] = []
    in_string = False
    escaped = False
    for char in text[:error.pos]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            expected_closers.append("}")
        elif char == "[":
            expected_closers.append("]")
        elif char in "]}":
            if not expected_closers or expected_closers[-1] != char:
                return text
            expected_closers.pop()
    if (
        not in_string
        and expected_closers == ["}"]
        and text[error.pos] != expected_closers[-1]
    ):
        return text[:error.pos] + text[error.pos + 1:]
    return text


_JSON_KEY_AFTER_COLON_RE = re.compile(
    r',(\s*)"((?:\\[nrt]|\s)*):((?:\\[nrt]|\s)*)"([A-Za-z_][A-Za-z0-9_]*)"(\s*),(\s*)"',
)


def _repair_json_key_after_colon(text: str) -> str:
    """Repair `, "\\n    :"field_name", "value"` where the key was split around the colon.

    Production (provider_calls 14348 / character_bible_detail 孟浩 attempt 1): the
    model closed appearance_canonical, opened the next key, then wrote
    `:"period_costume_canonical",` instead of `period_costume_canonical":`.
    """
    return _JSON_KEY_AFTER_COLON_RE.sub(r',\n    "\4": "', text)


def _repair_array_missing_object_brace(text: str) -> str:
    """Repair an array element whose opening ``{`` was omitted.

    Providers sometimes emit ``},\n    ,"field": ...`` after an object in an
    array: the first comma closes the previous element, the second comma is a
    doubled separator, and the following object keys are missing the opening
    brace.  This turns that pattern into a proper next object and, when the
    inserted object consumes the only trailing root closer, appends the
    deterministic ``]}`` closers.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "{":
            stack.append("{")
            index += 1
            continue
        if char == "[":
            stack.append("[")
            index += 1
            continue
        if char == "}":
            if stack and stack[-1] == "{":
                stack.pop()
            index += 1
            continue
        if char == "]":
            if stack and stack[-1] == "[":
                stack.pop()
            index += 1
            continue
        if char == "," and stack and stack[-1] == "[":
            second = index + 1
            while second < length and text[second].isspace():
                second += 1
            if second < length and text[second] == ",":
                key_start = second + 1
                while key_start < length and text[key_start].isspace():
                    key_start += 1
                if key_start < length and text[key_start] == '"':
                    cursor = key_start + 1
                    local_escaped = False
                    while cursor < length:
                        current = text[cursor]
                        if local_escaped:
                            local_escaped = False
                        elif current == "\\":
                            local_escaped = True
                        elif current == '"':
                            cursor += 1
                            break
                        cursor += 1
                    while cursor < length and text[cursor].isspace():
                        cursor += 1
                    if cursor < length and text[cursor] == ":":
                        repaired = text[: index + 1] + "{" + text[second + 1 :]
                        try:
                            json.loads(repaired)
                        except (TypeError, ValueError):
                            if (
                                repaired.rstrip().endswith("}")
                                and not repaired.rstrip().endswith("]}")
                            ):
                                candidate = repaired + "]}"
                                try:
                                    json.loads(candidate)
                                except (TypeError, ValueError):
                                    return text
                                return candidate
                            return text
                        return repaired
        index += 1
    return text
