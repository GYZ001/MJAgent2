"""把历史上内嵌在代码里的模型搬进模型库。

背景：早期模型分两条路进系统——代码里写死的 ``BUILTIN_MODELS`` 和页面上「添加
模型」加进来的。同一家服务因此会在下拉里出现两次（一条来自代码、一条来自页面），
既没法对客户解释，也让"改一个模型要改代码"变成常态。现在模型库是唯一来源，
这份迁移负责把老的内嵌模型连同它们已经配好的连接一起搬过去，用户不必重配。

迁移自带一份旧世界的快照（``LEGACY_BUILTINS``），不去引用现在的代码——迁移的
职责是解释历史数据，不能随着当前代码一起漂移。
"""
from __future__ import annotations

import json
from typing import Any

MIGRATION_FLAG = "builtin_models_migrated_v1"

# 旧的内嵌模型清单：(provider, model, label, kinds, protocol, provider_label)
LEGACY_BUILTINS: tuple[tuple[str, str, str, tuple[str, ...], str, str], ...] = (
    ("hiagent", "d2a5n9rnvvm49eucvnvg", "文本推理模型", ("text",), "openai", "火山"),
    ("hiagent", "d71l5c8nfdb167kligqg", "Text 模型", ("text",), "openai", "火山"),
    ("hiagent", "d7ev7il5boeaebtf4sgg", "视觉质检模型", ("vlm",), "openai", "火山"),
    ("hiagent", "d7jf6nd5boeaebtfbdqg", "Seedance 视频生成", ("video",), "seedance", "火山"),
    ("hiagent", "d7ute7ppcc7n89uuqqp0", "Seedream 图像生成", ("image",), "seedream", "火山"),
    ("minimax_h3", "minimax-h3", "MiniMaxH3", ("video",), "minimax_h3", "MiniMax H3"),
    ("openrouter", "z-ai/glm-5.2", "GLM 5.2", ("text",), "openrouter", "OpenRouter"),
    ("openrouter", "anthropic/claude-opus-4.8", "Claude Opus 4.8", ("text",), "openrouter", "OpenRouter"),
    ("openrouter", "google/gemini-3.5-flash", "Gemini 3.5 Flash", ("vlm",), "openrouter", "OpenRouter"),
    ("bailian", "qwen3.7-max", "Qwen3.7-Max", ("text",), "bailian", "百炼"),
    ("bailian", "qwen3.7-plus", "Qwen3.7-Plus", ("text", "vlm"), "bailian", "百炼"),
    ("deepseek", "deepseek-v4-pro", "DeepSeek V4 Pro", ("text",), "deepseek", "DeepSeek"),
    ("zhipu", "glm-5.2", "GLM 5.2", ("text",), "zhipu", "智谱"),
)

# 旧的"某 provider 当前选中哪个模型"设置键。
LEGACY_MODEL_SETTING = {
    ("hiagent", "text"): "hiagent_model_text",
    ("hiagent", "vlm"): "hiagent_model_vlm",
    ("hiagent", "video"): "hiagent_model_video",
    ("hiagent", "image"): "hiagent_model_image",
    ("minimax_h3", "video"): "minimax_h3_model_video",
    ("openrouter", "text"): "openrouter_model_text",
    ("openrouter", "vlm"): "openrouter_model_vlm",
    ("bailian", "text"): "bailian_model_text",
    ("bailian", "vlm"): "bailian_model_vlm",
    ("deepseek", "text"): "deepseek_model_text",
    ("zhipu", "text"): "zhipu_model_text",
}


def _json_setting(get_setting, key: str, fallback: Any) -> Any:
    try:
        value = json.loads(get_setting(key) or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _legacy_connection(
    provider: str,
    model: str,
    credentials: dict[str, Any],
) -> tuple[str, str]:
    """还原一条内嵌模型当时实际在用的连接。

    优先取页面上配过的 per-model 连接（``model_credentials``），否则回落到
    provider 级的环境变量——两者都空就说明这条从来没被配通过，不迁。
    """
    from app import config

    saved = credentials.get(f"builtin:{provider}:{model}")
    if isinstance(saved, dict) and saved.get("base_url") and saved.get("api_key"):
        return str(saved["base_url"]).rstrip("/"), str(saved["api_key"])
    if provider == "minimax_h3":
        from app.db import get_setting

        base = str(
            get_setting("minimax_h3_base_url") or config.MINIMAX_H3_BASE_URL or ""
        ).rstrip("/")
        return (base, config.MINIMAX_H3_API_KEY) if base and config.MINIMAX_H3_API_KEY else ("", "")
    defaults = {
        "hiagent": (config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY),
        "openrouter": (config.OPENROUTER_BASE_URL, config.OPENROUTER_API_KEY),
        "bailian": (config.BAILIAN_BASE_URL, config.BAILIAN_API_KEY),
        "deepseek": (config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY),
        "zhipu": (config.ZHIPU_BASE_URL, config.ZHIPU_API_KEY),
    }
    base_url, api_key = defaults.get(provider, ("", ""))
    return (str(base_url).rstrip("/"), str(api_key)) if base_url and api_key else ("", "")


def _legacy_active(get_setting, kind: str) -> tuple[str, str]:
    """按旧规则还原某个职责当前实际选中的 (provider, model)。"""
    configured = str(get_setting(f"model_{kind}_provider") or "").strip()
    if configured.startswith("custom:"):
        return configured, ""
    valid = {
        "text": {"hiagent", "openrouter", "bailian", "deepseek", "zhipu"},
        "vlm": {"hiagent", "openrouter", "bailian"},
        "video": {"hiagent", "minimax_h3"},
        "image": {"hiagent"},
    }[kind]
    provider = configured if configured in valid else ""
    if not provider and kind in {"text", "vlm"}:
        route = str(get_setting("model_route") or "hiagent").strip()
        provider = route if route in valid else "hiagent"
    provider = provider or "hiagent"
    setting_key = LEGACY_MODEL_SETTING.get((provider, kind))
    model = str(get_setting(setting_key) or "").strip() if setting_key else ""
    return provider, model


def migrate_builtin_models(*, force: bool = False) -> dict[str, Any]:
    """把内嵌模型搬进模型库，并把职责分配重指到新条目。

    幂等：按 (model, base_url) 去重，已经迁过或用户手工加过的同一条不会重复出现。
    返回一份可打印的摘要，供启动日志与手工执行时确认。
    """
    from app.db import get_setting, new_id, set_setting

    if not force and str(get_setting(MIGRATION_FLAG) or "").strip() == "done":
        return {"skipped": True, "reason": "already_migrated"}

    catalog = _json_setting(get_setting, "custom_models", [])
    credentials = _json_setting(get_setting, "model_credentials", {})
    capabilities = _json_setting(get_setting, "model_token_capabilities", {})

    existing = {
        (str(item.get("model") or ""), str(item.get("base_url") or "").rstrip("/"))
        for item in catalog
        if isinstance(item, dict)
    }
    # 旧 (provider, model) → 新 provider，供后面重指职责分配。
    migrated_provider: dict[tuple[str, str], str] = {}
    added: list[str] = []
    skipped: list[str] = []

    for provider, model, label, kinds, protocol, provider_label in LEGACY_BUILTINS:
        base_url, api_key = _legacy_connection(provider, model, credentials)
        if not base_url or not api_key:
            skipped.append(f"{provider}:{model}（未配置过连接）")
            continue
        key = (model, base_url)
        if key in existing:
            reused = next(
                item for item in catalog
                if isinstance(item, dict)
                and str(item.get("model") or "") == model
                and str(item.get("base_url") or "").rstrip("/") == base_url
            )
            migrated_provider[(provider, model)] = str(reused.get("provider") or "")
            skipped.append(f"{provider}:{model}（模型库里已有同一条）")
            continue
        item_id = new_id("model")
        entry: dict[str, Any] = {
            "id": item_id,
            "provider": f"custom:{item_id}",
            "model": model,
            "label": label,
            "kinds": list(kinds),
            "builtin": False,
            "protocol": protocol,
            "provider_label": provider_label,
            "base_url": base_url,
            "api_key": api_key,
        }
        legacy_caps = capabilities.get(f"builtin:{provider}:{model}")
        if isinstance(legacy_caps, dict):
            entry.update(legacy_caps)
            capabilities[item_id] = dict(legacy_caps)
        catalog.append(entry)
        existing.add(key)
        migrated_provider[(provider, model)] = entry["provider"]
        added.append(f"{label}（{provider}:{model}）→ {entry['provider']}")

    # 页面在"协议"这个概念出现之前加的条目没有 protocol 字段。那时只能加文本/视觉
    # 理解模型，它们一律走 OpenAI 兼容协议，回填是安全的。
    # 视频/图像不回填：它们从一开始就必须显式声明协议，缺失说明数据异常，
    # 这时猜一个协议会拿错误的实现去打真实服务（比如用 H3 协议打 Seedance 端点），
    # 宁可让它在页面上暴露成"待补协议"，也不要在出片路径上静默走错。
    backfilled: list[str] = []
    for item in catalog:
        if not isinstance(item, dict) or str(item.get("protocol") or "").strip():
            continue
        kinds = item.get("kinds") or []
        if "video" in kinds or "image" in kinds:
            continue
        item["protocol"] = "openai"
        backfilled.append(f"{item.get('label')} → openai")

    reassigned: dict[str, str] = {}
    for kind in ("text", "vlm", "video", "image"):
        provider, model = _legacy_active(get_setting, kind)
        if provider.startswith("custom:"):
            # 用户已经自己加过并选中了，保持原样。
            continue
        target = migrated_provider.get((provider, model))
        if not target:
            # 该职责当前指向一条没迁成的内嵌模型：不猜，留空由用户在页面上选。
            continue
        set_setting(f"model_{kind}_provider", target)
        reassigned[kind] = target

    set_setting("custom_models", json.dumps(catalog, ensure_ascii=False))
    set_setting("model_token_capabilities", json.dumps(capabilities, ensure_ascii=False))
    set_setting(MIGRATION_FLAG, "done")
    return {
        "skipped": False,
        "added": added,
        "not_migrated": skipped,
        "backfilled_protocol": backfilled,
        "reassigned": reassigned,
    }


# app.db.init_db() no longer imports this module directly (P0-3 dependency
# inversion, docs/coupling_review_2026-08-29.md 第2步) — it looks this up by
# name through app.db_schema instead. migrate_builtin_models() takes no conn
# argument (it resolves its own connection via app.db.get_setting/set_setting,
# matching how init_db() called it before this change); the registered
# wrapper discards the conn the registry passes in to keep that behavior
# identical.
from app.db_schema import register_table as _register_table  # noqa: E402

_register_table("builtin_models_migration", lambda conn: migrate_builtin_models())
