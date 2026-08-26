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


def _has_credentials(item: dict[str, Any]) -> bool:
    """条目是否真的能打通连接；口径与 app/system_api.py::_public_model 的
    key_configured 一致（多数条目要求 api_key 非空；条目显式声明
    requires_api_key=False 时只要求 base_url，用于极少数不需要 Key 的自建服务）。"""
    if item.get("requires_api_key") is False:
        return bool(str(item.get("base_url") or "").strip())
    return bool(str(item.get("api_key") or "").strip())


def text_model_choices() -> list[dict[str, str]]:
    """世界书/映射台/分镜台的分环节文本模型下拉可选清单：kind=text 且已配凭据的
    条目。不返回没配凭据的条目——不能让用户选一个必然失败的模型。"""
    return [
        {
            "provider": str(item.get("provider") or ""),
            "label": str(item.get("label") or item.get("model") or ""),
            "model": str(item.get("model") or ""),
        }
        for item in items_for_kind("text")
        if _has_credentials(item)
    ]


def resolve_stage_text_provider(value: str | None) -> str | None:
    """校验某环节保存的 provider 选择当前是否仍然可用。

    返回 None 时调用方按"未设置"处理，回落到全局默认文本 provider——不让一条
    后来被删除或掉了凭据的陈旧选择打断生成；这不是给模型库开回落口子（模型库
    本身该报错的地方仍然报错），只是分环节覆盖这一层的失败姿态选择可用性优先。
    """
    provider = str(value or "").strip()
    if not provider:
        return None
    valid = {choice["provider"] for choice in text_model_choices()}
    return provider if provider in valid else None


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
