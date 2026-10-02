"""监制房「模型分配」把默认职责切到一条自定义模型时，``PUT /api/settings`` 的
放行判据必须与页面展示「所选模型连接已配置」用同一个判据——否则加密表里
明明有密钥，保存仍会 422。

回归背景：``app.system_api.put_settings`` 曾自抄一份 ``configured`` 判断，
三个来源全是死源（内联 ``api_key``、``settings.model_credentials``、按
provider 字面量查环境变量的 ``provider_keys``），对任何 ``custom:*`` 条目
恒为假。修复后改成直接调用 ``_public_model(item)["key_configured"]``——与
``/api/models``、``/system/health`` 同一判据，见 app/system_api.py::put_settings
与 app/system_api.py::_public_model。
"""
import json

import pytest
from fastapi import HTTPException

from app import config, hiagent, system_api as api


def _clear_env_keys(monkeypatch):
    """模拟 .env 里没有任何 provider key，排除环境变量兜底命中的干扰。"""
    for name in (
        "HIAGENT_API_KEY", "OPENROUTER_API_KEY", "BAILIAN_API_KEY",
        "DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "MINIMAX_H3_API_KEY",
    ):
        monkeypatch.setattr(config, name, "")


def _add_custom_text_vlm_model_with_credential() -> dict:
    """经公开入口 ``add_model`` 建一条带密钥的自定义模型；密钥落加密表
    （``model_credentials``），不落目录条目本身。"""
    return api.add_model({
        "provider": "custom", "provider_label": "测试网关",
        "base_url": "https://role-switch.example.com/api/v1",
        "api_key": "sk-real-key-for-role-switch", "protocol": "openrouter",
        "model": "vendor/role-switch-model",
        "label": "角色切换测试模型",
        "kinds": ["text", "vlm"],
    })


def _add_custom_model_without_any_credential() -> dict:
    """直接落目录表、不经 ``add_model``（后者强制要求自定义模型必填
    api_key），模拟"模型库里有条目，但从没配过密钥也没有环境变量兜底"的
    自定义模型——复现用户反馈场景的前置状态。"""
    import app.models_registry.store as models_registry_store

    item = {
        "id": "model_role_switch_no_key", "provider": "custom:model_role_switch_no_key",
        "model": "vendor/no-key-model", "label": "无密钥测试模型",
        "kinds": ["text"], "builtin": False,
    }
    models_registry_store.upsert_model(item, created_by="test")
    return item


def test_model_credentials_setting_is_a_dead_source_after_migration():
    """前提核验：迁移后 ``settings.model_credentials`` 恒为 ``"{}"``——这正是
    旧判据三个死源之一，修复必须绕开它而不是继续读它。"""
    assert json.loads(api.get_setting("model_credentials") or "{}") == {}


def test_custom_model_with_credential_can_become_default_text_and_vlm_provider(monkeypatch):
    """正例：加密表里真有密钥的自定义模型，必须能切成默认文本/图像理解职责，
    且绑定同步真的生效（active_provider 反映新选路）。"""
    _clear_env_keys(monkeypatch)
    created = _add_custom_text_vlm_model_with_credential()
    provider = created["provider"]
    assert provider.startswith("custom:")

    result = api.put_settings({
        "model_text_provider": provider, "model_vlm_provider": provider,
    })

    assert result["ok"] is True
    assert hiagent.active_provider("text") == provider
    assert hiagent.active_provider("vlm") == provider


def test_custom_model_without_any_credential_is_rejected_with_422(monkeypatch):
    """反例：模型库里有这条目，但加密表无记录、环境变量也没有兜底
    ——仍要 422，且文案不变。"""
    _clear_env_keys(monkeypatch)
    item = _add_custom_model_without_any_credential()

    with pytest.raises(HTTPException) as exc:
        api.put_settings({"model_text_provider": item["provider"]})

    assert exc.value.status_code == 422
    assert exc.value.detail["message"] == "目标模型连接尚未配置并通过测试"


def test_put_settings_gate_matches_public_model_key_configured(monkeypatch):
    """一致性：对同一条目，put_settings 放行与否必须与 ``_public_model(item)
    ["key_configured"]`` 一致——两处判据不同源正是本次要修的 bug。"""
    _clear_env_keys(monkeypatch)
    configured_item = _add_custom_text_vlm_model_with_credential()
    unconfigured_item = _add_custom_model_without_any_credential()

    configured_catalog_item = next(
        item for item in api._model_catalog() if item["id"] == configured_item["id"]
    )
    unconfigured_catalog_item = next(
        item for item in api._model_catalog() if item["id"] == unconfigured_item["id"]
    )
    assert api._public_model(configured_catalog_item)["key_configured"] is True
    assert api._public_model(unconfigured_catalog_item)["key_configured"] is False

    # 与判据一致：已配置的放行，未配置的仍 422。
    api.put_settings({"model_text_provider": configured_item["provider"]})
    with pytest.raises(HTTPException) as exc:
        api.put_settings({"model_text_provider": unconfigured_item["provider"]})
    assert exc.value.status_code == 422
