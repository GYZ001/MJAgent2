import json
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import hiagent, system_api as api
from app.main import _redact_sensitive


def _settings_store(monkeypatch):
    store = {"custom_models": "[]"}
    monkeypatch.setattr(api, "get_setting", lambda key: store.get(key, ""))
    monkeypatch.setattr(api, "set_setting", lambda key, value: store.__setitem__(key, value))
    return store


def test_custom_model_can_be_added_to_catalog(monkeypatch) -> None:
    store = _settings_store(monkeypatch)

    created = api.add_model({
        "provider": "openrouter",
        "model": "vendor/new-model",
        "label": "New Model",
        "kinds": ["text", "vlm"],
    })

    assert created["builtin"] is False
    assert created["kinds"] == ["text", "vlm"]
    saved = json.loads(store["custom_models"])
    assert saved[0]["model"] == "vendor/new-model"
    assert api.get_models()["items"][-1]["label"] == "New Model"


def test_custom_model_rejects_unsupported_capability(monkeypatch) -> None:
    _settings_store(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        api.add_model({
            "provider": "deepseek",
            "model": "deepseek-image",
            "label": "Wrong capability",
            "kinds": ["image"],
        })

    assert exc.value.status_code == 422


def test_active_custom_model_cannot_be_deleted(monkeypatch) -> None:
    store = _settings_store(monkeypatch)
    item = api.add_model({
        "provider": "openrouter", "model": "vendor/active",
        "label": "Active", "kinds": ["text"],
    })
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "openrouter")
    monkeypatch.setattr(hiagent, "active_model", lambda kind: "vendor/active")

    with pytest.raises(HTTPException) as exc:
        api.delete_model(item["id"])

    assert exc.value.status_code == 409
    assert json.loads(store["custom_models"])


def test_custom_provider_credentials_are_private_and_model_scoped(monkeypatch) -> None:
    store = _settings_store(monkeypatch)
    created = api.add_model({
        "provider": "custom", "provider_label": "Internal Gateway",
        "base_url": "https://llm.example.com/v1/", "api_key": "secret-one",
        "model": "team/model-a", "label": "Model A", "kinds": ["text", "vlm"],
    })

    assert created["provider"].startswith("custom:model_")
    assert created["key_configured"] is True
    assert "api_key" not in created
    public = api.get_models()["items"][-1]
    assert "api_key" not in public
    stored = json.loads(store["custom_models"])[0]
    assert stored["api_key"] == "secret-one"


def test_builtin_model_credentials_are_saved_by_model_id(monkeypatch) -> None:
    store = _settings_store(monkeypatch)
    model_id = "builtin:openrouter:z-ai/glm-5.2"

    api.put_model_credentials(model_id, {
        "base_url": "https://gateway.example.com/v1", "api_key": "model-specific-key",
    })

    credentials = json.loads(store["model_credentials"])
    assert credentials[model_id]["api_key"] == "model-specific-key"


def test_connection_probe_checks_openai_response(monkeypatch) -> None:
    class Response:
        is_success = True
        status_code = 200
        text = '{"choices": [{"message": {"content": "OK"}}]}'

        def json(self):
            return {"choices": [{"message": {"content": "OK"}}]}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(api.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        api.socket, "getaddrinfo",
        lambda host, port, **kwargs: [(api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    result = asyncio.run(api.test_model_connection({
        "base_url": "https://example.com/v1", "api_key": "secret", "model": "model-a",
    }))

    assert result["ok"] is True
    assert result["preview"] == "OK"


def test_custom_model_can_be_edited_then_deleted(monkeypatch) -> None:
    store = _settings_store(monkeypatch)
    item = api.add_model({
        "provider": "custom", "provider_label": "Gateway", "base_url": "https://example.com/v1",
        "api_key": "secret", "model": "old-model", "label": "Old", "kinds": ["text"],
    })
    updated = api.update_model(item["id"], {"label": "New", "model": "new-model", "kinds": ["text", "vlm"]})
    assert updated["label"] == "New"
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "hiagent")
    api.delete_model(item["id"])
    assert json.loads(store["custom_models"]) == []


def test_error_context_redacts_nested_model_keys() -> None:
    value = _redact_sensitive({"api_key": "secret", "nested": {"token": "also-secret", "model": "ok"}})
    assert value == {"api_key": "***", "nested": {"token": "***", "model": "ok"}}


def test_media_probe_accepts_explicit_endpoint_type_mismatch(monkeypatch) -> None:
    class Response:
        is_success = False
        status_code = 400
        text = "Model 'video-model' is not supported for this endpoint, which expects model type 'text-generation'"

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(api.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        api.socket, "getaddrinfo",
        lambda host, port, **kwargs: [(api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    result = asyncio.run(api._probe_openai_model(
        "https://example.com/v1", "secret", "video-model", "video"))

    assert result["ok"] is True
    assert result["probe"] == "model_recognition"


def test_probe_openai_model_rejects_private_ssrf_targets() -> None:
    for url in (
        "http://127.0.0.1:8080/v1",
        "http://localhost/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.8/v1",
    ):
        try:
            api._assert_public_http_url(url)
            raise AssertionError(f"expected reject for {url}")
        except HTTPException as exc:
            assert exc.status_code == 422


def test_browse_blocks_sensitive_system_paths() -> None:
    sensitive = Path.home() / ".ssh"
    try:
        api.browse_dir(str(sensitive))
        raise AssertionError("expected sensitive directory browse to fail")
    except HTTPException as exc:
        assert exc.status_code == 403


def test_relative_media_url_is_resolved_against_provider_origin() -> None:
    resolved = hiagent._absolute_provider_url(
        "/api/proxy/down?key=abc", "https://hia.example.com/api/aigw/v1")
    assert resolved == "https://hia.example.com/api/proxy/down?key=abc"
    assert hiagent._absolute_provider_url("https://cdn.example.com/image.png", "https://hia.example.com/v1") == "https://cdn.example.com/image.png"
