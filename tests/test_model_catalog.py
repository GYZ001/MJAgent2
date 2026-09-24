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


def test_custom_model_can_be_added_to_catalog() -> None:
    import app.models_registry.store as models_registry_store

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
    saved = models_registry_store.get_model(created["id"])
    assert saved["model_ref"] == "vendor/new-model"
    assert api.get_models()["items"][-1]["label"] == "New Model"
    assert created["context_window_tokens"] == 128 * 1024
    assert created["max_output_tokens"] == 32 * 1024
    assert created["token_limits_source"] == "default_128k_32k"


def test_legacy_catalog_models_receive_128k_32k_compatibility_defaults() -> None:
    """"Legacy" 条目——落表时没有显式能力字段（模拟迁移前保存的旧模型），
    读取时仍要按产品策略补齐 128K/32K 默认值。直接经
    ``models_registry_store.upsert_model`` 落表，不再经 settings blob。"""
    import app.models_registry.store as models_registry_store

    models_registry_store.upsert_model({
        "id": "model_legacy", "provider": "openrouter", "model": "vendor/legacy",
        "label": "Legacy", "kinds": ["text"], "builtin": False,
    }, created_by="tester")

    model = next(item for item in api.get_models()["items"] if item["id"] == "model_legacy")

    assert model["context_window_tokens"] == 131072
    assert model["max_output_tokens"] == 32768
    assert model["token_limits_source"] == "default_128k_32k"


def test_custom_model_rejects_unsupported_capability() -> None:
    with pytest.raises(HTTPException) as exc:
        api.add_model({
            "provider": "deepseek",
            "model": "deepseek-image",
            "label": "Wrong capability",
            "kinds": ["image"],
        })

    assert exc.value.status_code == 422


def test_active_custom_model_cannot_be_deleted(monkeypatch) -> None:
    import app.models_registry.store as models_registry_store

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
    assert models_registry_store.get_model(item["id"]) is not None


def test_public_model_key_configured_falls_back_to_env_by_gateway_family(monkeypatch) -> None:
    """2026-08-30 与 health() 同一类缺陷：``_public_model`` 此前按 provider 字面量
    查环境变量兜底密钥，而 model_migration 产物的 provider 是生成的
    "custom:model_xxx"，字面量永远查不中。此前无害是因为这类条目都自带
    api_key；这里构造一个（假设的）没有自带 api_key、只能靠环境变量兜底的
    custom 条目，证明按 base_url 归族之后真的能查到。"""
    monkeypatch.setattr(config, "HIAGENT_API_KEY", "env-hiagent-key")
    item = {
        "id": "model_no_own_key", "provider": "custom:model_no_own_key",
        "provider_label": "HiAgent 迁移产物", "base_url": config.HIAGENT_BASE_URL,
        "model": "some-model", "label": "L", "kinds": ["text"], "builtin": False,
    }
    monkeypatch.setattr(api, "get_setting", lambda key: "{}")
    assert api._public_model(item)["key_configured"] is True


def test_public_model_key_configured_still_matches_literal_family_without_base_url(monkeypatch) -> None:
    """非 custom 条目（沿用共享网关连接，provider 仍是历史字面量如 "hiagent"）
    本来就不存自己的 base_url/api_key——这条必须继续按字面量 provider 命中环境
    变量，不能被"按 base_url 归族"的新逻辑挤掉（这类条目没有 base_url 可归族，
    归族分支天然查不到，必须退回字面量分支）。"""
    monkeypatch.setattr(config, "HIAGENT_API_KEY", "env-hiagent-key")
    item = {
        "id": "model_shared_gateway", "provider": "hiagent",
        "model": "some-other-model", "label": "L", "kinds": ["text"], "builtin": False,
    }
    monkeypatch.setattr(api, "get_setting", lambda key: "{}")
    assert api._public_model(item)["key_configured"] is True


def test_public_model_key_configured_false_when_no_key_anywhere(monkeypatch) -> None:
    monkeypatch.setattr(config, "HIAGENT_API_KEY", "")
    item = {
        "id": "model_unconfigured", "provider": "custom:model_unconfigured",
        "base_url": "https://unrelated-gateway.example.com/v1",
        "model": "m", "label": "L", "kinds": ["text"], "builtin": False,
    }
    monkeypatch.setattr(api, "get_setting", lambda key: "{}")
    assert api._public_model(item)["key_configured"] is False


def test_model_credentials_are_saved_by_model_id(monkeypatch) -> None:
    """凭据只写不读（EP-05 第一阶段）：新 Key 落进 app.models_registry.store
    的加密表，不再落 settings.model_credentials；接口响应也不回明文。"""
    import app.models_registry.store as models_registry_store
    from app.db import get_setting

    created = api.add_model({
        "provider": "custom", "provider_label": "Gateway",
        "base_url": "https://gw.example.com/v1", "api_key": "first",
        "protocol": "openai", "model": "m", "label": "M", "kinds": ["text"],
    })

    async def _fake_probe(item, base_url, api_key):
        return None

    monkeypatch.setattr(api, "_probe_model_credential", _fake_probe)

    response = asyncio.run(api.put_model_credentials(created["id"], {
        "base_url": "https://gateway.example.com/v1",
        "api_key": "model-specific-key",
        "confirm": True,
    }))

    assert "api_key" not in response and "key_ciphertext" not in response
    assert response["key_fingerprint"]
    assert "model-specific-key" not in json.dumps(response)

    saved = models_registry_store.get_credential(created["id"])
    assert saved["api_key"] == "model-specific-key"
    assert saved["base_url"] == "https://gateway.example.com/v1"
    # settings.model_credentials 不再是凭据来源，本接口也不再写它。
    assert (get_setting("model_credentials") or "") in ("", "{}")


def test_catalog_is_the_only_source_of_models() -> None:
    """代码里不再内嵌任何模型：空模型库就是真的没有模型。

    这是产品要求——页面上每一条都必须能说清"它是通过添加模型进来的"，
    所以不能再有一份代码内嵌清单在旁边并行存在。每个测试独占一份干净数据库
    （见 tests/conftest.py），``models`` 表天然是空的，不需要再靠伪造
    settings 来保证"空模型库"这件事。
    """
    assert api.get_models()["items"] == []


def test_active_selection_resolves_from_catalog() -> None:
    """选路读的是真表（``models``/``model_bindings``），不是伪造的 settings
    blob——``model_text_provider`` 这个旧设置仍然要能通过
    ``routing.binding_migration`` 迁成一条 priority=0 绑定并生效。"""
    import app.models_registry.store as models_registry_store
    from app.db import set_setting

    models_registry_store.upsert_model({
        "id": "model_a", "provider": "custom:model_a", "model": "text-1",
        "label": "文本", "kinds": ["text"], "protocol": "openai",
        "base_url": "https://gw.example.com/v1",
    }, created_by="tester")
    set_setting("model_text_provider", "custom:model_a")

    assert hiagent.active_provider("text") == "custom:model_a"
    assert hiagent.active_model("text") == "text-1"

    # 指向一条不存在的条目时不能回落到某个写死的默认，而是退到模型库里
    # 第一条具备该能力的条目；一条都没有就返回空串，由调用方报"未配置"。
    # 旧设置迁移是一次性、幂等的（已在上面第一次调用时触发并落成绑定），改动
    # legacy 设置本身不会重新触发迁移——这与迁移前的行为逐字段一致。
    set_setting("model_text_provider", "custom:gone")
    assert hiagent.active_provider("text") == "custom:model_a"
    models_registry_store.delete_model("model_a")
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
    import app.models_registry.store as models_registry_store

    item = api.add_model({
        "provider": "custom", "provider_label": "Gateway", "base_url": "https://example.com/v1",
        "api_key": "secret", "protocol": "openai",
        "model": "old-model", "label": "Old", "kinds": ["text"],
    })
    updated = api.update_model(item["id"], {"label": "New", "model": "new-model", "kinds": ["text", "vlm"]})
    assert updated["label"] == "New"
    # 空库里第一条模型会自动成为它各项能力的主用（避免「缺少主用绑定」无路可修）；
    # 被用途绑定引用的模型不许删，所以先解除这两条自动绑定，再验证删除本身。
    from app.models_registry import bindings
    for purpose in ("text:default", "vlm:default"):
        bindings.delete_binding(bindings.get_priority_zero(purpose)["id"])
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "")
    api.delete_model(item["id"])
    assert models_registry_store.get_model(item["id"]) is None
    assert api.get_models()["items"] == []


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


def _capability_settings(capabilities: dict[str, dict]):
    """``model_token_capabilities`` 是唯一还需要伪造的 setting——目录本身
    (``custom_models``) 已退场，测试改为把条目真实落进 ``models`` 表（见各
    用例里的 ``models_registry_store.upsert_model`` 调用）。"""
    store = {"model_token_capabilities": json.dumps(capabilities)}
    return lambda key: store.get(key, "")


def test_stale_default_probe_cache_cannot_override_configured_capability() -> None:
    """兜底猜测不得盖掉运维显式填写的真实能力。

    生产事故：glm-5.3-flash 的真实输出上限 131072 已在模型编辑里保存下来，
    却被一条标着 default_128k_32k 的旧探测缓存按 32768 用。分镜台阶段二的思
    考预留因此永远挤不进预算、整集截断失败，而 PUT 接口回显的是 131072，
    界面上完全看不出异常。
    """
    import app.models_registry.store as models_registry_store
    from app import model_registry
    from app.model_capabilities import active_model_token_limits

    models_registry_store.upsert_model({
        "id": "model_glm", "provider": "custom:model_glm", "model": "glm-5.3-flash",
        "context_window_tokens": 1048576, "max_output_tokens": 131072,
        "token_limits_source": "configured",
    }, created_by="tester")
    get_setting = _capability_settings({"model_glm": {
        "context_window_tokens": 131072, "max_output_tokens": 32768,
        "token_limits_source": "default_128k_32k",
    }})

    limits = active_model_token_limits(
        "custom:model_glm", "glm-5.3-flash", get_setting, model_registry.catalog_items(),
    )
    assert limits["max_output_tokens"] == 131072
    assert limits["token_limits_source"] == "configured"


def test_fresh_provider_metadata_still_wins_over_saved_configuration() -> None:
    """探测缓存本身有实据时仍然优先——这条规则只挡兜底猜测，不挡真凭据。"""
    import app.models_registry.store as models_registry_store
    from app import model_registry
    from app.model_capabilities import active_model_token_limits

    models_registry_store.upsert_model({
        "id": "model_x", "provider": "custom:model_x", "model": "some-model",
        "context_window_tokens": 131072, "max_output_tokens": 65536,
        "token_limits_source": "configured",
    }, created_by="tester")
    get_setting = _capability_settings({"model_x": {
        "context_window_tokens": 200000, "max_output_tokens": 100000,
        "token_limits_source": "provider_metadata",
    }})

    limits = active_model_token_limits(
        "custom:model_x", "some-model", get_setting, model_registry.catalog_items(),
    )
    assert limits["max_output_tokens"] == 100000
    assert limits["token_limits_source"] == "provider_metadata"


def test_settings_page_reports_the_same_limits_the_run_actually_uses() -> None:
    """设置页显示的能力必须与运行时取到的是同一个数。

    这条判据挂在「两处读数一致」上，而不是各自等于某个字面量：修复前
    active_model_token_limits 已经按证据强度合并、拿到 131072，而 /api/models
    走的是 system_api 里另一份同样形状的合并、仍无条件让探测缓存覆盖，于是
    界面稳定显示 32768。同一个模型两个说法，人照着界面查不出任何问题——上一轮
    排查正是被这一点带偏的。目录条目与探测缓存现在都落真表/真 setting，两个
    读取路径（``api.get_models()`` 与 ``active_model_token_limits``）经的是
    同一个 ``models`` 表，不再需要分别打桩两个模块的 ``get_setting``。
    """
    import app.models_registry.store as models_registry_store
    from app import model_registry
    from app.db import get_setting as real_get_setting, set_setting
    from app.model_capabilities import active_model_token_limits

    models_registry_store.upsert_model({
        "id": "model_glm", "provider": "custom:model_glm", "model": "glm-5.3-flash",
        "label": "GLM", "kinds": ["text"], "protocol": "zhipu",
        "context_window_tokens": 1048576, "max_output_tokens": 131072,
        "token_limits_source": "configured",
    }, created_by="tester")
    capabilities = {"model_glm": {
        "context_window_tokens": 131072, "max_output_tokens": 32768,
        "token_limits_source": "default_128k_32k",
    }}
    set_setting("model_token_capabilities", json.dumps(capabilities))

    shown = next(
        item for item in api.get_models()["items"] if item["id"] == "model_glm"
    )
    used = active_model_token_limits(
        "custom:model_glm", "glm-5.3-flash", real_get_setting, model_registry.catalog_items(),
    )

    for field in ("context_window_tokens", "max_output_tokens", "token_limits_source"):
        assert shown[field] == used[field], field
    assert shown["max_output_tokens"] == 131072


def test_default_probe_cache_still_applies_when_nothing_else_is_evidenced() -> None:
    """两边都没实据时保持原有行为，别把「挡兜底」升级成「丢掉兜底」。"""
    import app.models_registry.store as models_registry_store
    from app import model_registry
    from app.model_capabilities import active_model_token_limits

    models_registry_store.upsert_model({
        "id": "model_y", "provider": "custom:model_y", "model": "plain-model",
    }, created_by="tester")
    get_setting = _capability_settings({"model_y": {
        "context_window_tokens": 131072, "max_output_tokens": 32768,
        "token_limits_source": "default_128k_32k",
    }})

    limits = active_model_token_limits(
        "custom:model_y", "plain-model", get_setting, model_registry.catalog_items(),
    )
    assert limits["max_output_tokens"] == 32768
    assert limits["token_limits_source"] == "default_128k_32k"
