"""`/api/system/health` 的凭据/模型状态计算——从 app/system_api.py 拆出来，
避免把新逻辑堆进已经顶到行数基线的 system_api.py（见 app/FILE_CONVENTIONS.toml）。

背景：早期 provider 是字面量（"hiagent"/"openrouter"/...），app.model_migration
把内嵌模型搬进模型库后，真实条目的 provider id 变成动态生成的
"custom:model_xxx"。旧版 health() 直接拿字面量去查模型库，永远查不到，于是对着
已经配好、能实际调通的部署照样报"未配置密钥/模型"——判据挂在了单一来源（导入期
环境变量）上，而权威来源其实是 settings 表（模型库 + 每条目的凭据）。

这里改成按网关地址把模型库条目归类回历史家族——base_url 是本仓库一直以来判定
"这是哪家服务"的信号（同样用法见 app/model_migration.py::_legacy_connection 的
defaults 字典），不依赖 provider 字符串是否还等于历史上的字面量。密钥判据取
"环境变量或模型库任一条目已配凭据"——任一来源有值就算配好，不挂单一来源。
"""
from __future__ import annotations

from typing import Any, Callable

CONFIG_HINT = (
    "密钥与模型分配在「监制房 → 模型中心」配置；"
    "HiAgent 网关密钥也可在项目根目录 .env 设置 HIAGENT_API_KEY 等变量。"
)


def _family_base_url(family: str) -> str:
    from app import config

    return {
        "hiagent": config.HIAGENT_BASE_URL,
        "minimax_h3": config.MINIMAX_H3_BASE_URL,
        "openrouter": config.OPENROUTER_BASE_URL,
        "bailian": config.BAILIAN_BASE_URL,
        "deepseek": config.DEEPSEEK_BASE_URL,
        "zhipu": config.ZHIPU_BASE_URL,
    }.get(family, "").rstrip("/")


def _family_env_key(family: str) -> str:
    from app import config

    return {
        "hiagent": config.HIAGENT_API_KEY, "openrouter": config.OPENROUTER_API_KEY,
        "bailian": config.BAILIAN_API_KEY, "deepseek": config.DEEPSEEK_API_KEY,
        "zhipu": config.ZHIPU_API_KEY, "minimax_h3": config.MINIMAX_H3_API_KEY,
    }.get(family, "")


def _family_items(
    catalog: list[dict[str, Any]], family: str, kind: str | None = None,
) -> list[dict[str, Any]]:
    """模型库里属于某历史家族（按网关地址判定）、且支持某能力的条目。"""
    base = _family_base_url(family)
    if not base:
        return []
    return [
        item for item in catalog
        if str(item.get("base_url") or "").rstrip("/") == base
        and (kind is None or kind in (item.get("kinds") or []))
    ]


def family_key_configured(
    catalog: list[dict[str, Any]],
    family: str,
    item_key_configured: Callable[[dict[str, Any]], bool],
) -> bool:
    """家族级密钥是否配好：env 变量或模型库里该家族任一条目已配凭据，任一有值即算配好。"""
    if _family_env_key(family):
        return True
    return any(item_key_configured(item) for item in _family_items(catalog, family))


def family_model(
    catalog: list[dict[str, Any]], family: str, kind: str, active_provider: str,
) -> str:
    """家族在某能力上生效的模型 ID：优先取当前选中项，否则退回模型库里该家族
    第一条同能力条目；都没有则真的没配，返回空串由调用方报"未配置"。"""
    candidates = _family_items(catalog, family, kind)
    if not candidates:
        return ""
    active = next(
        (item for item in candidates if item.get("provider") == active_provider),
        candidates[0],
    )
    return str(active.get("model") or "")


def credential_report(
    catalog: list[dict[str, Any]],
    *,
    item_key_configured: Callable[[dict[str, Any]], bool],
    active_provider: Callable[[str], str],
) -> dict[str, Any]:
    """health() 里 key_configured / *_model_* 那组字段的完整取值。"""

    def key_ok(family: str) -> bool:
        return family_key_configured(catalog, family, item_key_configured)

    def model(family: str, kind: str) -> str:
        return family_model(catalog, family, kind, active_provider(kind))

    return {
        "key_configured": key_ok("hiagent"),
        "openrouter_key_configured": key_ok("openrouter"),
        "bailian_key_configured": key_ok("bailian"),
        "deepseek_key_configured": key_ok("deepseek"),
        "zhipu_key_configured": key_ok("zhipu"),
        "hiagent_model_text": model("hiagent", "text"),
        "hiagent_model_vlm": model("hiagent", "vlm"),
        "hiagent_model_video": model("hiagent", "video"),
        "minimax_h3_model_video": model("minimax_h3", "video"),
        "hiagent_model_image": model("hiagent", "image"),
        "openrouter_model_text": model("openrouter", "text"),
        "openrouter_model_vlm": model("openrouter", "vlm"),
        "bailian_model_text": model("bailian", "text"),
        "bailian_model_vlm": model("bailian", "vlm"),
        "deepseek_model_text": model("deepseek", "text"),
        "zhipu_model_text": model("zhipu", "text"),
        "config_hint": CONFIG_HINT,
    }
