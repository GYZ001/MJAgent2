import json
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import config, hiagent, system_api as api
from app.main import _redact_sensitive


def test_builtin_hiagent_gateway_has_a_working_default() -> None:
    assert config.DEFAULT_HIAGENT_BASE_URL == "https://hia.volcenginepaas.com/api/aigw/v1"
    assert config.HIAGENT_BASE_URL


def _settings_store(monkeypatch):
    store = {"custom_models": "[]"}
    monkeypatch.setattr(api, "get_setting", lambda key: store.get(key, ""))
    monkeypatch.setattr(api, "set_setting", lambda key, value: store.__setitem__(key, value))
    return store


def test_custom_model_can_be_added_to_catalog(monkeypatch) -> None:
    store = _settings_store(monkeypatch)

    created = api.add_model({
        "provider": "custom", "provider_label": "OpenRouter",
        "base_url": "https://openrouter.example.com/api/v1",
        "api_key": "or-key", "protocol": "openrouter",
        "model": "vendor/new-model",
        "label": "New Model",
        "kinds": ["text", "vlm"],
    })

    assert created["builtin"] is False
    assert created["kinds"] == ["text", "vlm"]
    saved = json.loads(store["custom_models"])
    assert saved[0]["model"] == "vendor/new-model"
    assert api.get_models()["items"][-1]["label"] == "New Model"
    assert created["context_window_tokens"] == 128 * 1024
    assert created["max_output_tokens"] == 32 * 1024
    assert created["token_limits_source"] == "default_128k_32k"


def test_legacy_catalog_models_receive_128k_32k_compatibility_defaults(monkeypatch) -> None:
    store = {
        "custom_models": json.dumps([{
            "id": "model_legacy",
            "provider": "openrouter",
            "model": "vendor/legacy",
            "label": "Legacy",
            "kinds": ["text"],
            "builtin": False,
        }]),
    }
    monkeypatch.setattr(api, "get_setting", lambda key: store.get(key, ""))

    model = next(item for item in api.get_models()["items"] if item["id"] == "model_legacy")

    assert model["context_window_tokens"] == 131072
    assert model["max_output_tokens"] == 32768
    assert model["token_limits_source"] == "default_128k_32k"


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
        "provider": "custom", "provider_label": "OpenRouter",
        "base_url": "https://openrouter.example.com/api/v1", "api_key": "k",
        "protocol": "openrouter", "model": "vendor/active",
        "label": "Active", "kinds": ["text"],
    })
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: item["provider"])
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
        "protocol": "openai",
        "model": "team/model-a", "label": "Model A", "kinds": ["text", "vlm"],
    })

    assert created["provider"].startswith("custom:model_")
    assert created["key_configured"] is True
    assert "api_key" not in created
    public = api.get_models()["items"][-1]
    assert "api_key" not in public
    stored = json.loads(store["custom_models"])[0]
    assert stored["api_key"] == "secret-one"


def test_model_credentials_are_saved_by_model_id(monkeypatch) -> None:
    store = _settings_store(monkeypatch)
    created = api.add_model({
        "provider": "custom", "provider_label": "Gateway",
        "base_url": "https://gw.example.com/v1", "api_key": "first",
        "protocol": "openai", "model": "m", "label": "M", "kinds": ["text"],
    })

    api.put_model_credentials(created["id"], {
        "base_url": "https://gateway.example.com/v1",
        "api_key": "model-specific-key",
        "confirm": True,
    })

    credentials = json.loads(store["model_credentials"])
    assert credentials[created["id"]]["api_key"] == "model-specific-key"


def test_catalog_is_the_only_source_of_models(monkeypatch) -> None:
    """代码里不再内嵌任何模型：空模型库就是真的没有模型。

    这是产品要求——页面上每一条都必须能说清"它是通过添加模型进来的"，
    所以不能再有一份代码内嵌清单在旁边并行存在。
    """
    _settings_store(monkeypatch)

    assert api.get_models()["items"] == []


def test_active_selection_resolves_from_catalog(monkeypatch) -> None:
    catalog = [{
        "id": "model_a", "provider": "custom:model_a", "model": "text-1",
        "label": "文本", "kinds": ["text"], "protocol": "openai",
        "base_url": "https://gw.example.com/v1", "api_key": "k",
    }]
    store = {
        "custom_models": json.dumps(catalog),
        "model_text_provider": "custom:model_a",
    }
    monkeypatch.setattr(hiagent, "get_setting", lambda key: store.get(key, ""))
    import app.db as db_module

    monkeypatch.setattr(db_module, "get_setting", lambda key: store.get(key, ""))

    assert hiagent.active_provider("text") == "custom:model_a"
    assert hiagent.active_model("text") == "text-1"

    # 指向一条不存在的条目时不能回落到某个写死的默认，而是退到模型库里
    # 第一条具备该能力的条目；一条都没有就返回空串，由调用方报"未配置"。
    store["model_text_provider"] = "custom:gone"
    assert hiagent.active_provider("text") == "custom:model_a"
    store["custom_models"] = "[]"
    assert hiagent.active_provider("text") == ""
    assert hiagent.active_model("text") == ""


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
    assert result["context_window_tokens"] == 131072
    assert result["max_output_tokens"] == 32768


def test_connection_probe_reads_provider_token_metadata(monkeypatch) -> None:
    class ChatResponse:
        is_success = True
        status_code = 200
        text = '{"choices": [{"message": {"content": "OK"}}]}'

        def json(self):
            return {"choices": [{"message": {"content": "OK"}}]}

    class ModelsResponse:
        is_success = True

        def json(self):
            return {
                "data": [{
                    "id": "model-a",
                    "context_length": 262144,
                    "top_provider": {"max_completion_tokens": 49152},
                }],
            }

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs): return ChatResponse()
        async def get(self, *args, **kwargs): return ModelsResponse()

    monkeypatch.setattr(api.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        api.socket, "getaddrinfo",
        lambda host, port, **kwargs: [(api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    result = asyncio.run(api._probe_openai_model(
        "https://example.com/v1", "secret", "model-a", "text",
    ))

    assert result["context_window_tokens"] == 262144
    assert result["max_output_tokens"] == 49152
    assert result["token_limits_source"] == "provider_metadata"


def test_custom_model_can_be_edited_then_deleted(monkeypatch) -> None:
    store = _settings_store(monkeypatch)
    item = api.add_model({
        "provider": "custom", "provider_label": "Gateway", "base_url": "https://example.com/v1",
        "api_key": "secret", "protocol": "openai",
        "model": "old-model", "label": "Old", "kinds": ["text"],
    })
    updated = api.update_model(item["id"], {"label": "New", "model": "new-model", "kinds": ["text", "vlm"]})
    assert updated["label"] == "New"
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "")
    api.delete_model(item["id"])
    assert json.loads(store["custom_models"]) == []


def test_error_context_redacts_nested_model_keys() -> None:
    value = _redact_sensitive({"api_key": "secret", "nested": {"token": "also-secret", "model": "ok"}})
    assert value == {"api_key": "***", "nested": {"token": "***", "model": "ok"}}


def test_media_probe_uses_structured_model_catalog(monkeypatch) -> None:
    class Response:
        is_success = True

        def json(self):
            return {"data": [{"id": "video-model"}]}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs): return Response()

    monkeypatch.setattr(api.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        api.socket, "getaddrinfo",
        lambda host, port, **kwargs: [(api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    result = asyncio.run(api._probe_openai_model(
        "https://example.com/v1", "secret", "video-model", "video"))

    assert result["ok"] is True
    assert result["probe"] == "model_catalog"


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


def _capability_settings(custom_models: list[dict], capabilities: dict[str, dict]):
    store = {
        "custom_models": json.dumps(custom_models),
        "model_token_capabilities": json.dumps(capabilities),
    }
    return lambda key: store.get(key, "")


def test_stale_default_probe_cache_cannot_override_configured_capability() -> None:
    """兜底猜测不得盖掉运维显式填写的真实能力。

    生产事故：glm-5.3-flash 的真实输出上限 131072 已在模型编辑里保存下来，
    却被一条标着 default_128k_32k 的旧探测缓存按 32768 用。分镜台阶段二的思
    考预留因此永远挤不进预算、整集截断失败，而 PUT 接口回显的是 131072，
    界面上完全看不出异常。
    """
    from app.model_capabilities import active_model_token_limits

    get_setting = _capability_settings(
        [{
            "id": "model_glm", "provider": "custom:model_glm", "model": "glm-5.3-flash",
            "context_window_tokens": 1048576, "max_output_tokens": 131072,
            "token_limits_source": "configured",
        }],
        {"model_glm": {
            "context_window_tokens": 131072, "max_output_tokens": 32768,
            "token_limits_source": "default_128k_32k",
        }},
    )

    limits = active_model_token_limits("custom:model_glm", "glm-5.3-flash", get_setting)
    assert limits["max_output_tokens"] == 131072
    assert limits["token_limits_source"] == "configured"


def test_fresh_provider_metadata_still_wins_over_saved_configuration() -> None:
    """探测缓存本身有实据时仍然优先——这条规则只挡兜底猜测，不挡真凭据。"""
    from app.model_capabilities import active_model_token_limits

    get_setting = _capability_settings(
        [{
            "id": "model_x", "provider": "custom:model_x", "model": "some-model",
            "context_window_tokens": 131072, "max_output_tokens": 65536,
            "token_limits_source": "configured",
        }],
        {"model_x": {
            "context_window_tokens": 200000, "max_output_tokens": 100000,
            "token_limits_source": "provider_metadata",
        }},
    )

    limits = active_model_token_limits("custom:model_x", "some-model", get_setting)
    assert limits["max_output_tokens"] == 100000
    assert limits["token_limits_source"] == "provider_metadata"


def test_default_probe_cache_still_applies_when_nothing_else_is_evidenced() -> None:
    """两边都没实据时保持原有行为，别把「挡兜底」升级成「丢掉兜底」。"""
    from app.model_capabilities import active_model_token_limits

    get_setting = _capability_settings(
        [{
            "id": "model_y", "provider": "custom:model_y", "model": "plain-model",
        }],
        {"model_y": {
            "context_window_tokens": 131072, "max_output_tokens": 32768,
            "token_limits_source": "default_128k_32k",
        }},
    )

    limits = active_model_token_limits("custom:model_y", "plain-model", get_setting)
    assert limits["max_output_tokens"] == 32768
    assert limits["token_limits_source"] == "default_128k_32k"
