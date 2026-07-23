"""脱敏：任何密钥字段/取值都不得进入 agent_messages / agent_tool_calls / SSE 事件。

PRD §12.1「密钥泄露」与验收 DoD：API Key、token、Authorization 不进入模型
上下文或可见日志。本模块只做防御性剔除，不依赖调用方自律。
"""
from __future__ import annotations

import re
from typing import Any

# 命中即整值替换为 "***"（子串匹配，覆盖 api_key / apiKey / access_token 等变体）
_SENSITIVE_KEY_PATTERNS = ("api_key", "apikey", "authorization", "password", "secret", "token")

# 常见密钥/凭证字面量形态的兜底扫描（即使字段名未命中，也不能把值原样落库/回显）
_SENSITIVE_VALUE_RES = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
)


def _is_sensitive_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(pattern in lowered for pattern in _SENSITIVE_KEY_PATTERNS)


def redact_text(text: str) -> str:
    """扫描并遮蔽自由文本中的常见凭证字面量。"""
    redacted = text
    for pattern in _SENSITIVE_VALUE_RES:
        redacted = pattern.sub("***", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    """递归脱敏：dict 按字段名剔除，list 递归，字符串做兜底扫描。"""
    if isinstance(value, dict):
        return {
            key: ("***" if _is_sensitive_key(key) else redact_value(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
