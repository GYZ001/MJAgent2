"""审计参数脱敏——从 app.main 挪出的 ``_SENSITIVE_KEYS``/``_redact_sensitive``，
加 ``approval_token`` 一起用于 operation_audit.args_json（原版只服务
``_request_context`` 的报错上下文，不含批准令牌本身）。

零 app 内部依赖，只用 stdlib：这是 app.audit 依赖面最窄的一个模块，任何更高
层都能安全引用它做同样的清理，不必各自维护一份敏感键清单。
"""
from __future__ import annotations

import json
from typing import Any

_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "password", "secret", "token",
    "access_token", "approval_token",
}

_MAX_ARGS_JSON_CHARS = 4000


def _redact_sensitive(value: Any) -> Any:
    """递归清理请求/命令参数，任何密钥都不得进入日志或审计行。"""
    if isinstance(value, dict):
        return {
            key: ("***" if str(key).lower() in _SENSITIVE_KEYS else _redact_sensitive(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def redact_and_truncate(args: dict[str, Any]) -> str:
    """脱敏后序列化为 JSON 字符串，截断到 ``_MAX_ARGS_JSON_CHARS`` 字符。"""
    try:
        text = json.dumps(_redact_sensitive(args), ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 序列化失败不能阻断审计主流程，退化成空对象
        return "{}"
    return text if len(text) <= _MAX_ARGS_JSON_CHARS else text[:_MAX_ARGS_JSON_CHARS]
