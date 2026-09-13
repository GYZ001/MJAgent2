"""模型库媒体模型编辑回归测试。"""
import pytest
from fastapi import HTTPException

from app import system_api as api


def test_custom_seedance_video_model_can_be_edited() -> None:
    """视频模型通过连接测试后，编辑保存不能再套用旧 Text/VLM 限制。"""
    import app.models_registry.store as models_registry_store

    item = api.add_model({
        "provider": "custom", "provider_label": "火山",
        "base_url": "https://hia.volcenginepaas.com/api/aigw/v1",
        "api_key": "secret", "protocol": "seedance",
        "model": "d7jf6nd5boeaebtfbdqg", "label": "Seedance 视频生成",
        "kinds": ["video"],
    })

    updated = api.update_model(item["id"], {
        "label": "Seedance 视频生成（生产）",
        "provider_label": "火山引擎",
        "base_url": "https://hia.volcenginepaas.com/api/aigw/v1",
        "model": "d7jf6nd5boeaebtfbdqg",
        "kinds": ["video"],
        "protocol": "seedance",
    })

    assert updated["kinds"] == ["video"]
    assert updated["protocol"] == "seedance"
    saved = models_registry_store.get_model(item["id"])
    assert saved["provider_label"] == "火山引擎"
    # EP-05 第一阶段：api_key 不再落目录（现在是 models 表），改走加密表。
    assert "api_key" not in saved
    assert models_registry_store.get_credential(item["id"])["api_key"] == "secret"


def test_edit_model_rejects_protocol_that_does_not_support_capability() -> None:
    import app.models_registry.store as models_registry_store

    item = api.add_model({
        "provider": "custom", "provider_label": "Gateway",
        "base_url": "https://example.com/v1", "api_key": "secret",
        "protocol": "openai", "model": "text-model", "label": "Text",
        "kinds": ["text"],
    })

    with pytest.raises(HTTPException) as exc:
        api.update_model(item["id"], {
            "kinds": ["video"],
            "protocol": "openai",
        })

    assert exc.value.status_code == 422
    assert exc.value.detail["field"] == "protocol"
    assert models_registry_store.get_model(item["id"])["kinds"] == ["text"]
