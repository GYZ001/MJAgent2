"""模型库访问层：所有模型（含协议、连接、能力）的唯一事实来源。

设计约束来自一条产品要求：**不再有"代码里内嵌的模型"**。模型库以外不存在
任何模型，页面上看到的每一条都是通过「添加模型」进来的，因此也能对客户解释
清楚它从哪来、连的是哪个服务。

代码里保留的只有**协议实现**——OpenAI 兼容对话、OpenRouter 的 reasoning 参数、
百炼的模型回退、Seedance/MiniMax H3 的异步出片、Seedream 的参考图字段。协议是
需要有人写代码的能力，实例只是"这套协议 + 这个地址 + 这个 Key + 这个模型 ID"，
所以实例全部走模型库，新增实例不必改代码。
"""
from __future__ import annotations

import json
from typing import Any

CATALOG_SETTING = "custom_models"
CREDENTIALS_SETTING = "model_credentials"


def _load(setting: str, fallback: Any) -> Any:
    from app.db import get_setting

    try:
        value = json.loads(get_setting(setting) or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def catalog_items() -> list[dict[str, Any]]:
    """模型库里的全部条目。"""
    return [item for item in _load(CATALOG_SETTING, []) if isinstance(item, dict)]


def _with_credentials(item: dict[str, Any]) -> dict[str, Any]:
    """把单独保存的连接信息合并进条目。

    连接与条目分开存是历史设计（改地址/换 Key 不必动模型定义），这里统一合并，
    调用方不必关心它落在哪张表。
    """
    credentials = _load(CREDENTIALS_SETTING, {})
    saved = credentials.get(str(item.get("id") or "")) if isinstance(credentials, dict) else None
    if not isinstance(saved, dict):
        return dict(item)
    merged = dict(item)
    for key in ("base_url", "api_key"):
        if saved.get(key):
            merged[key] = saved[key]
    return merged


def catalog_item(provider: str) -> dict[str, Any] | None:
    """按 provider 取模型库条目（已合并连接信息）。"""
    name = str(provider or "").strip()
    if not name:
        return None
    item = next(
        (
            entry for entry in catalog_items()
            if str(entry.get("provider") or "") == name
        ),
        None,
    )
    return _with_credentials(item) if item is not None else None


def catalog_item_for_kind(provider: str, kind: str) -> dict[str, Any] | None:
    """按 provider + 能力取条目；provider 下没有该能力时返回 None。"""
    item = catalog_item(provider)
    if item is None or kind not in (item.get("kinds") or []):
        return None
    return item


def items_for_kind(kind: str) -> list[dict[str, Any]]:
    return [
        _with_credentials(item)
        for item in catalog_items()
        if kind in (item.get("kinds") or [])
    ]


def protocol_for_provider(
    provider: str,
    *,
    allowed: dict[str, Any] | set[str],
    default: str,
) -> str:
    """解析某个实例声明的接入协议；未声明或声明了未实现的协议时回落到默认。

    回落而不是抛错：协议缺失是配置问题，应该在「测试连接」和保存时就被拦住，
    真到了出片路径上再抛会把配置问题伪装成生成失败。
    """
    item = catalog_item(provider)
    declared = str((item or {}).get("protocol") or "").strip().lower()
    return declared if declared in allowed else default
