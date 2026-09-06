"""结构化输出的键名形态归一：模型偶发把 ``raw_label`` 写成 ``rawLabel``。

2026-09-05 验收轮实测：映射台称谓归属一次响应 20 条里恰有 1 条用了驼峰键
（``appellations.19.rawLabel``），schema 是 ``extra="forbid"``，整条响应被拒，修复重试
后另一条又换了一个位置驼峰，第 1 集与另一集的映射台都因此失败。

判据是 schema 自身：只有当这个键不是模型字段、而它的下划线形态**恰好是**模型字段时才改名；
其它多余键原样保留，让 ``extra="forbid"`` 照常拒绝真正的越界字段。按字段注解递归进入嵌套
模型与其列表；不认识的注解不进去。纯函数、不依赖 app.hiagent，供 model_gateway 在
``model_validate`` 之前调用。
"""
from __future__ import annotations

import re
import typing
from typing import Any

from pydantic import BaseModel

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])([A-Z])")


def to_snake_case(key: str) -> str:
    return _CAMEL_BOUNDARY.sub(lambda m: "_" + m.group(1).lower(), key).lower()


def _model_classes(annotation: Any) -> list[type[BaseModel]]:
    """注解里能找到的 BaseModel 子类：直接类型、Optional/Union 成员、list[...] 元素。"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        found.extend(_model_classes(arg))
    return found


def snake_case_keys_for_model(model_type: Any, payload: Any) -> Any:
    """返回按 ``model_type`` 字段表把驼峰键改成下划线键后的副本；无事可做时原样返回。"""
    if not (isinstance(model_type, type) and issubclass(model_type, BaseModel)):
        return payload
    fields = model_type.model_fields
    if isinstance(payload, list):
        return [snake_case_keys_for_model(model_type, item) for item in payload]
    if not isinstance(payload, dict):
        return payload
    out: dict[str, Any] = {}
    for key, value in payload.items():
        name = key
        if isinstance(key, str) and key not in fields:
            snake = to_snake_case(key)
            if snake != key and snake in fields:
                if snake not in payload:
                    name = snake
                elif payload[snake] == value:
                    continue  # 同一字段驼峰/下划线各写了一遍且值相同：多余的那份丢掉（2026-09-06 第 1 集 appellations.22）
        field = fields.get(name)
        nested = _model_classes(field.annotation) if field is not None else []
        if len(nested) == 1 and isinstance(value, (dict, list)):
            value = snake_case_keys_for_model(nested[0], value)
        out[name] = value
    return out
