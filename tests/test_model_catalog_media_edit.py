"""模型库媒体模型编辑回归测试。"""
import json

import pytest
from fastapi import HTTPException

from app import system_api as api


def _settings_store(monkeypatch):
    store = {"custom_models": "[]"}
    monkeypatch.setattr(api, "get_setting", lambda key: store.get(key, ""))
    monkeypatch.setattr(api, "set_setting", lambda key, value: store.__setitem__(key, value))
    return store


def test_custom_seedance_video_model_can_be_edited(monkeypatch) -> None:
    """视频模型通过连接测试后，编辑保存不能再套用旧 Text/VLM 限制。"""
    store = _settings_store(monkeypatch)
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
    saved = json.loads(store["custom_models"])[0]
    assert saved["provider_label"] == "火山引擎"
    assert saved["api_key"] == "secret"


def test_edit_model_rejects_protocol_that_does_not_support_capability(monkeypatch) -> None:
    store = _settings_store(monkeypatch)
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
    assert json.loads(store["custom_models"])[0]["kinds"] == ["text"]
