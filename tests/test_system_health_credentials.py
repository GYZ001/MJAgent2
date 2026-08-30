"""`/api/system/health` 的凭据/模型状态必须挂实际生效的配置（settings 表的
模型库 + 每条目凭据，或环境变量），不能只看导入期环境变量、也不能硬编码已经
被 app.model_migration 淘汰的字面量 provider id（"hiagent"/"minimax_h3" 等，
迁移后真实条目是动态生成的 "custom:model_xxx"）。

回归背景：迁移前 health() 直接用字面量 provider 去查模型库，迁移后永远查不到，
于是对着已经配好、能实际调通的部署照样报"未配置"——见 app/system_health.py
模块 docstring 与 app/system_api.py::health()。
"""
import json

from app import config, hiagent, system_api as api, system_health


def _settings_store(monkeypatch, *, custom_models=None, model_route="hiagent"):
    store = {
        "custom_models": json.dumps(custom_models or []),
        "model_route": model_route,
    }
    monkeypatch.setattr(api, "get_setting", lambda key: store.get(key, ""))
    monkeypatch.setattr(api, "set_setting", lambda key, value: store.__setitem__(key, value))
    return store


def _clear_env_keys(monkeypatch):
    """模拟 .env 里没有任何 provider key（本仓库当前 .env 的真实状态）。"""
    for name in (
        "HIAGENT_API_KEY", "OPENROUTER_API_KEY", "BAILIAN_API_KEY",
        "DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "MINIMAX_H3_API_KEY",
    ):
        monkeypatch.setattr(config, name, "")


def _hiagent_text_item(provider: str = "custom:model_abc123") -> dict:
    return {
        "id": "model_abc123",
        "provider": provider,
        "model": "d71l5c8nfdb167kligqg",
        "label": "seed2.0pro",
        "kinds": ["text"],
        "builtin": False,
        "protocol": "openai",
        "provider_label": "火山",
        "base_url": config.HIAGENT_BASE_URL,
        "api_key": "sk-real-key-in-settings-table",
    }


# ---------------------------------------------------------------------------
# 直接单测 app.system_health：不经过 HTTP/DB，纯函数、快、可精确断言。
# ---------------------------------------------------------------------------

def test_family_model_ignores_stale_literal_provider_id():
    """迁移后条目的 provider 是 "custom:model_xxx"，不是字面量 "hiagent"——
    按网关地址（base_url）归类，而不是按 provider 字符串是否等于历史字面量。"""
    catalog = [_hiagent_text_item()]

    model = system_health.family_model(catalog, "hiagent", "text", "custom:model_abc123")

    assert model == "d71l5c8nfdb167kligqg"


def test_family_model_prefers_active_selection_over_first_match():
    catalog = [
        {**_hiagent_text_item(provider="custom:model_a"), "id": "model_a", "model": "model-a"},
        {**_hiagent_text_item(provider="custom:model_b"), "id": "model_b", "model": "model-b"},
    ]

    model = system_health.family_model(catalog, "hiagent", "text", "custom:model_b")

    assert model == "model-b"


def test_family_model_empty_when_family_has_no_catalog_entry():
    assert system_health.family_model([], "hiagent", "text", "") == ""


def test_family_key_configured_true_from_item_credentials_even_without_env_key():
    catalog = [_hiagent_text_item()]

    configured = system_health.family_key_configured(
        catalog, "hiagent", lambda item: bool(item.get("api_key")),
    )

    assert configured is True


def test_family_key_configured_false_when_no_env_and_no_item_credentials():
    configured = system_health.family_key_configured([], "hiagent", lambda item: False)

    assert configured is False


def test_credential_report_gives_a_hint_for_where_to_configure():
    report = system_health.credential_report(
        [], item_key_configured=lambda item: False, active_provider=lambda kind: "",
    )

    assert report["config_hint"]
    assert "模型中心" in report["config_hint"] or ".env" in report["config_hint"]


# ---------------------------------------------------------------------------
# 端到端：经 app.system_api.health()，settings 表里配了/都没配两种场景。
# ---------------------------------------------------------------------------

def test_health_reports_configured_when_settings_table_has_credentials(monkeypatch):
    _clear_env_keys(monkeypatch)
    item = _hiagent_text_item()
    _settings_store(monkeypatch, custom_models=[item])
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: item["provider"] if kind == "text" else "")

    result = api.health()

    assert result["key_configured"] is True
    assert result["hiagent_model_text"] == "d71l5c8nfdb167kligqg"
    # 没配的能力仍如实报空，不是被这一条命中污染。
    assert result["hiagent_model_vlm"] == ""


def test_health_reports_unconfigured_with_hint_when_neither_source_has_a_key(monkeypatch):
    _clear_env_keys(monkeypatch)
    _settings_store(monkeypatch, custom_models=[])
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "")

    result = api.health()

    assert result["key_configured"] is False
    assert result["hiagent_model_text"] == ""
    assert result["openrouter_key_configured"] is False
    assert result["config_hint"]
    # 公开端点：绝不能把密钥本身吐出来。
    assert "sk-" not in json.dumps(result, ensure_ascii=False)
