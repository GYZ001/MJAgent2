"""EP-05 第一阶段：add_model/update_model 的自定义服务商凭据不得落明文。

现状事实（迁移前）：``add_model``/``update_model`` 把自定义服务商的 api_key 直接
内联写进 ``settings.custom_models`` 的目录条目里，库文件或备份一拿到手就是明文
Key。本文件证明这条路径已经堵上：两个写入口都改成把 api_key 写进
``app.models_registry.store`` 的加密表。

2026-09-13 起 ``settings.custom_models`` 本身已完全退场（``add_model``/
``update_model`` 直接写 ``models`` 表，见 ``app/models_registry/store.py``
模块文档）——本文件"库里 grep 不到明文 Key"这条判据因此直接对 ``models`` 表
的行做字段级断言，而不是像迁移前那样拿 settings blob 的字符串代表落点。
"""
from app import system_api as api


def test_custom_provider_credentials_are_private_and_model_scoped() -> None:
    """add_model 收到 api_key 后写进加密表；``models`` 表那一行连 api_key
    字段都不留——不是靠 grep 一个 JSON blob 字符串，是直接查真表没有这一列。"""
    import app.models_registry.store as models_registry_store

    created = api.add_model({
        "provider": "custom", "provider_label": "Internal Gateway",
        "base_url": "https://llm.example.com/v1/", "api_key": "secret-one",
        "protocol": "openai",
        "model": "team/model-a", "label": "Model A", "kinds": ["text", "vlm"],
    })

    assert created["provider"].startswith("custom:model_")
    assert created["key_configured"] is True
    assert "api_key" not in created
    public = api.get_models()["items"][-1]
    assert "api_key" not in public
    stored = models_registry_store.get_model(created["id"])
    assert "api_key" not in stored
    assert models_registry_store.get_credential(created["id"])["api_key"] == "secret-one"


def test_update_model_writes_new_key_to_store_not_settings_blob() -> None:
    """update_model 收到新 api_key 时同样只写加密表；换 Key 后旧 Key 不残留在
    ``models`` 表或加密表里（原子替换），新 Key 也不落进 ``models`` 表那一行。"""
    import app.models_registry.store as models_registry_store

    created = api.add_model({
        "provider": "custom", "provider_label": "Gateway",
        "base_url": "https://gw.example.com/v1", "api_key": "first-key",
        "protocol": "openai", "model": "m", "label": "M", "kinds": ["text"],
    })

    api.update_model(created["id"], {
        "provider_label": "Gateway", "base_url": "https://gw.example.com/v1",
        "api_key": "second-key", "label": "M", "model": "m", "kinds": ["text"],
    })

    row = models_registry_store.get_model(created["id"])
    assert "api_key" not in row
    assert models_registry_store.get_credential(created["id"])["api_key"] == "second-key"


def test_update_model_editing_base_url_keeps_existing_key_working() -> None:
    """只改 base_url、不带新 Key 时，加密表里的旧 Key 必须继续可用（且 base_url
    同步更新到同一行）——不能因为换了地址就把已有的 Key 冲掉或让两者脱节。"""
    import app.models_registry.store as models_registry_store

    created = api.add_model({
        "provider": "custom", "provider_label": "Gateway",
        "base_url": "https://old.example.com/v1", "api_key": "stays-the-same",
        "protocol": "openai", "model": "m", "label": "M", "kinds": ["text"],
    })

    api.update_model(created["id"], {
        "provider_label": "Gateway", "base_url": "https://new.example.com/v1",
        "label": "M", "model": "m", "kinds": ["text"],
    })

    saved = models_registry_store.get_credential(created["id"])
    assert saved["api_key"] == "stays-the-same"
    assert saved["base_url"] == "https://new.example.com/v1"
    row = models_registry_store.get_model(created["id"])
    assert row["base_url"] == "https://new.example.com/v1"
    assert "api_key" not in row
