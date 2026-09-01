"""模型输出的 JSON 提取公开入口：extract_json / schema_errors。

extract_json 从模型输出中定位第一个完整 JSON 根对象，按需叠加
.json_repair_strings / .json_repair_structure 里的窄修复，全部修复失败则
抛 ValueError（带原文摘要）供上层重试/回喂。schema_errors 把 Pydantic 校验
错误转成"字段路径: 原因"的可回喂文案。
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, ValidationError

from .json_repair_strings import (
    _escape_json_control_chars_in_strings,
    _escape_unescaped_inner_quotes,
    _has_unclosed_json_string_prefix,
    _repair_fullwidth_closing_quote,
)
from .json_repair_structure import (
    _close_missing_trailing_containers,
    _remove_unmatched_root_level_closer,
    _repair_array_missing_object_brace,
    _repair_json_key_after_colon,
    _repair_merged_object_string_entry,
    _repair_singleton_string_object_fields,
    _repair_structural_json_delimiters,
    _repair_trailing_container_closure,
)

def extract_json(
    text: str,
    *,
    repair_unescaped_inner_quotes: bool = False,
    repair_singleton_string_object_fields: tuple[str, ...] = (),
) -> dict:
    """从模型输出中提取第一个完整 JSON 对象。失败抛 ValueError（含原文摘要）。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    think_markers = list(
        re.finditer(r"</think[^>]*>", cleaned, flags=re.IGNORECASE)
    )
    if think_markers:
        formal_payload = cleaned[think_markers[-1].end():].strip()
        if "{" in formal_payload:
            cleaned = formal_payload
    first_start = cleaned.find("{")
    if first_start == -1:
        raise ValueError(f"输出中找不到 JSON 对象。原文开头：{text[:200]}")

    first_error: json.JSONDecodeError | None = None
    for match in re.finditer(r"{", cleaned):
        start = match.start()
        # 只把形如 JSON 对象开头的花括号当候选；这样仍能跳过说明文字里的
        # “{不是 JSON}”。一旦遇到第一个真正的 JSON 根对象候选，就必须以它
        # 为准：若它因字符串内双引号未转义等原因损坏，应把解析错误回喂模型，
        # 不能继续向内扫描并误把 dialogues 中的小对象当成整份输出。
        remainder = cleaned[start + 1:].lstrip()
        if remainder and not (remainder.startswith('"') or remainder.startswith("}")):
            continue
        if _has_unclosed_json_string_prefix(cleaned[:start]):
            continue
        candidate = _repair_singleton_string_object_fields(
            cleaned[start:],
            repair_singleton_string_object_fields,
        )
        candidate = _repair_json_key_after_colon(candidate)
        if repair_unescaped_inner_quotes:
            candidate = _repair_fullwidth_closing_quote(candidate)
        candidate = _escape_json_control_chars_in_strings(candidate)
        candidate = _repair_array_missing_object_brace(candidate)
        try:
            obj, _ = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError as exc:
            candidate_error = exc
            if repair_unescaped_inner_quotes:
                repaired = _escape_unescaped_inner_quotes(candidate)
                if repaired != candidate:
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(repaired)
                    except json.JSONDecodeError as repaired_exc:
                        candidate_error = repaired_exc
                    else:
                        if isinstance(obj, dict):
                            return obj
                    candidate = repaired
                    if candidate_error.pos >= len(candidate):
                        repaired = _close_missing_trailing_containers(candidate)
                        if repaired != candidate:
                            try:
                                obj, _ = json.JSONDecoder().raw_decode(repaired)
                            except json.JSONDecodeError:
                                pass
                            else:
                                if isinstance(obj, dict):
                                    return obj
                    # A quote repair is semantic rewriting. If it is not
                    # sufficient, do not stack delimiter or field guesses.
                    first_error = candidate_error
                    break
            repaired = _repair_structural_json_delimiters(candidate)
            if repaired != candidate:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(repaired)
                except json.JSONDecodeError as repaired_exc:
                    candidate_error = repaired_exc
                else:
                    if isinstance(obj, dict):
                        return obj
                candidate = repaired
            repaired = _repair_json_key_after_colon(candidate)
            if repaired != candidate:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(repaired)
                except json.JSONDecodeError as repaired_exc:
                    candidate_error = repaired_exc
                else:
                    if isinstance(obj, dict):
                        return obj
                candidate = repaired
            repaired = _remove_unmatched_root_level_closer(
                candidate,
                candidate_error,
            )
            if repaired != candidate:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(repaired)
                except json.JSONDecodeError as repaired_exc:
                    candidate_error = repaired_exc
                else:
                    if isinstance(obj, dict):
                        return obj
                candidate = repaired
            repaired = _repair_merged_object_string_entry(
                candidate,
                candidate_error,
            )
            if repaired != candidate:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(repaired)
                except json.JSONDecodeError as repaired_exc:
                    candidate_error = repaired_exc
                else:
                    if isinstance(obj, dict):
                        return obj
                candidate = repaired
            if candidate_error.pos >= len(candidate.rstrip()) - 1:
                repaired = _repair_trailing_container_closure(candidate)
                if repaired != candidate:
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(repaired)
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(obj, dict):
                            return obj
            # Only an EOF failure may be eligible.  Missing commas and damaged
            # inner structure fail before EOF and must still enter the repair
            # loop instead of being silently guessed here.
            if candidate_error.pos >= len(candidate):
                repaired = _close_missing_trailing_containers(candidate)
                if repaired != candidate:
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(repaired)
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(obj, dict):
                            return obj
            first_error = exc
            break
        if isinstance(obj, dict):
            return obj
        raise ValueError(f"JSON 根节点不是对象。片段：{cleaned[start:start + 200]}")

    detail = f"（{first_error}）" if first_error else ""
    raise ValueError(f"JSON 解析失败{detail}。片段：{cleaned[first_start:first_start + 200]}")


def schema_errors(model_cls: type[BaseModel], obj: dict) -> tuple[BaseModel | None, list[str]]:
    """返回 (实例, 错误列表)。错误消息具体到字段路径，供修复回路回喂。"""
    try:
        return model_cls.model_validate(obj), []
    except ValidationError as exc:
        errors = []
        for e in exc.errors():
            path = ".".join(str(p) for p in e["loc"])
            errors.append(f"字段 {path}：{e['msg']}")
        return None, errors
