"""文本/VLM 模型的 token 能力归一化。

供应商的 OpenAI 兼容 ``/models`` 元数据没有统一字段名。本模块只读取常见的
声明字段，不通过发送超长 prompt 暴力试探上限。探测不到时采用产品策略默认值：
128K 上下文、32K 输出，并把来源标为 default，避免把默认值伪装成供应商实测值。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable


DEFAULT_CONTEXT_WINDOW_TOKENS = 128 * 1024
DEFAULT_MAX_OUTPUT_TOKENS = 32 * 1024
MIN_OUTPUT_TOKENS = 256
MAX_REASONABLE_CONTEXT_TOKENS = 16 * 1024 * 1024
MAX_REASONABLE_OUTPUT_TOKENS = 1024 * 1024

_CONTEXT_KEYS = (
    "context_window_tokens",
    "context_window",
    "context_length",
    "max_context_length",
    "max_model_len",
    "n_ctx",
)
_OUTPUT_KEYS = (
    "max_output_tokens",
    "max_completion_tokens",
    "output_token_limit",
    "max_tokens",
)
_NESTED_LIMIT_KEYS = ("top_provider", "limits", "capabilities", "token_limits")
_LIMIT_SOURCES = {"provider_metadata", "configured", "default_128k_32k"}


def parse_token_count(value: Any) -> int | None:
    """解析 ``131072``、``128k``、``32 K tokens`` 等供应商元数据。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = int(value)
        return parsed if parsed > 0 else None
    text = str(value).strip().lower().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)\s*(?:tokens?)?", text)
    if not match:
        return None
    multiplier = {"": 1, "k": 1024, "m": 1024 * 1024}[match.group(2)]
    parsed = int(float(match.group(1)) * multiplier)
    return parsed if parsed > 0 else None


def _first_limit(mapping: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = parse_token_count(mapping.get(key))
        if value is not None:
            return value
    for nested_key in _NESTED_LIMIT_KEYS:
        nested = mapping.get(nested_key)
        if isinstance(nested, dict):
            value = _first_limit(nested, keys)
            if value is not None:
                return value
    return None


def _model_metadata(payload: Any, model: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    candidates = payload.get("data") or payload.get("models")
    if isinstance(candidates, list):
        wanted = model.strip()
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
            if item_id == wanted:
                return item
    return payload


def extract_provider_token_limits(payload: Any, model: str) -> dict[str, Any]:
    """从供应商模型元数据提取 token 限制；完全缺失时返回空字典。"""
    metadata = _model_metadata(payload, model)
    if not metadata:
        return {}
    context = _first_limit(metadata, _CONTEXT_KEYS)
    output = _first_limit(metadata, _OUTPUT_KEYS)
    if context is None and output is None:
        return {}
    result: dict[str, Any] = {"token_limits_source": "provider_metadata"}
    if context is not None:
        result["context_window_tokens"] = context
    if output is not None:
        result["max_output_tokens"] = output
    return result


def normalize_token_limits(item: dict[str, Any] | None) -> dict[str, Any]:
    """补齐并校正模型能力，保证未知模型稳定落到 128K/32K 策略。"""
    value = item or {}
    context = parse_token_count(value.get("context_window_tokens"))
    output = parse_token_count(value.get("max_output_tokens"))
    detected = context is not None or output is not None
    context = context or DEFAULT_CONTEXT_WINDOW_TOKENS
    output = output or DEFAULT_MAX_OUTPUT_TOKENS
    context = min(max(MIN_OUTPUT_TOKENS, context), MAX_REASONABLE_CONTEXT_TOKENS)
    output = min(max(MIN_OUTPUT_TOKENS, output), MAX_REASONABLE_OUTPUT_TOKENS, context)
    source = str(value.get("token_limits_source") or "").strip()
    if source not in _LIMIT_SOURCES:
        source = "configured" if detected else "default_128k_32k"
    return {
        "context_window_tokens": context,
        "max_output_tokens": output,
        "token_limits_source": source,
    }


def apply_token_limit_defaults(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    normalized.update(normalize_token_limits(normalized))
    return normalized


#: 有实据的来源：供应商元数据实测，或运维在模型编辑里显式填写。
#: ``default_128k_32k`` 不在其列——它的语义就是「没探到，先按产品策略兜底」。
_EVIDENCED_LIMIT_SOURCES = frozenset({"provider_metadata", "configured"})

#: 受证据强度规则约束的字段。规则只管这三个，不扩大成「整条记录谁覆盖谁」。
_LIMIT_FIELDS = ("context_window_tokens", "max_output_tokens", "token_limits_source")


def _limits_are_evidenced(entry: Any) -> bool:
    """这份能力记录是真凭据，还是探测不到时写下的兜底猜测。"""
    if not isinstance(entry, dict):
        return False
    return str(entry.get("token_limits_source") or "") in _EVIDENCED_LIMIT_SOURCES


def merge_token_capability_override(
    item: dict[str, Any], override: Any
) -> dict[str, Any]:
    """把 ``model_token_capabilities`` 的探测缓存合进模型记录。

    能力字段按**证据强度**决定谁说了算，而不是按谁在哪张表：一条标着
    ``default_128k_32k``（探测失败时写下的兜底猜测）的陈旧缓存，不得盖掉运维
    在模型编辑里显式填写的真实能力。

    这份判据必须只有一处实现。它原本在 ``active_model_token_limits`` 里就地
    写着，于是运行时读到了正确的 131072，而设置页走的是 ``system_api``
    另一条同样形状的合并、仍然拿到 32768——同一个模型，界面和实际行为对不上，
    人照着界面根本查不出问题。
    """
    if not isinstance(override, dict):
        override = {}
    if _limits_are_evidenced(item) and not _limits_are_evidenced(override):
        override = {
            key: value for key, value in override.items() if key not in _LIMIT_FIELDS
        }
    return {**item, **override}


def active_model_token_limits(
    provider: str,
    model: str,
    get_setting: Callable[[str], str],
) -> dict[str, Any]:
    """读取当前模型能力；兼容尚未写入能力字段的既有模型。

    ``model_token_capabilities`` 是添加模型时的探测缓存，``custom_models``
    里的则是模型编辑里保存下来的值。两者的合并规则见
    ``merge_token_capability_override``：按证据强度定胜负，判据从数据本身推导
    （``token_limits_source`` 是这两条记录各自对自己成色的声明），不给哪个
    模型或哪张表开白名单。
    """
    selected: dict[str, Any] = {}
    try:
        custom = json.loads(get_setting("custom_models") or "[]")
    except (TypeError, json.JSONDecodeError):
        custom = []
    if isinstance(custom, list):
        selected = next(
            (
                item for item in custom
                if isinstance(item, dict)
                and item.get("provider") == provider
                and item.get("model") == model
            ),
            {},
        )
    item_id = str(selected.get("id") or f"builtin:{provider}:{model}")
    try:
        saved = json.loads(get_setting("model_token_capabilities") or "{}")
    except (TypeError, json.JSONDecodeError):
        saved = {}
    override = saved.get(item_id, {}) if isinstance(saved, dict) else {}
    return normalize_token_limits(
        merge_token_capability_override(selected, override)
    )
