"""模型中心接线：新增「声音生成」能力（``kinds=["voice"]``），只允许自建服务商
声明；``GET /models`` 暴露协议清单与中文说明；三处连接测试对 voice 协议走
适配器探测而不是 OpenAI 探测；写 ``model_voice_provider`` 触发
``voice:default`` 绑定同步；内置服务商（hiagent 等）能力集不受影响。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app import system_api as api
from app.voice.providers.base import VoiceProviderError


def _add_voice_model(**overrides) -> dict:
    body = {
        "provider": "custom", "provider_label": "百炼", "base_url": "https://dashscope.aliyuncs.com",
        "api_key": "sk-test", "protocol": "qwen_voice_design", "model": "qwen3-tts-vd-2026-01-26",
        "label": "千问声音设计", "kinds": ["voice"],
    }
    body.update(overrides)
    return api.add_model(body)


def test_add_model_with_voice_kind_and_qwen_protocol_succeeds() -> None:
    item = _add_voice_model()
    assert item["kinds"] == ["voice"]
    assert item["protocol"] == "qwen_voice_design"
    assert "api_key" not in item  # 凭据落加密表，不回落目录条目


def test_add_model_voice_kind_rejects_unknown_protocol() -> None:
    with pytest.raises(HTTPException) as exc:
        _add_voice_model(protocol="not_a_real_protocol")
    assert exc.value.status_code == 422
    assert exc.value.detail["field"] == "protocol"


def test_builtin_provider_cannot_declare_voice_kind() -> None:
    """内置服务商（hiagent 等）能力集不含 voice：只有自建服务商能声明它。"""
    with pytest.raises(HTTPException) as exc:
        api.add_model({
            "provider": "hiagent", "model": "some-model", "label": "L", "kinds": ["voice"],
        })
    assert exc.value.status_code == 422
    assert "不支持该模型能力" in str(exc.value.detail)


def test_hiagent_capability_set_unchanged_by_voice_addition() -> None:
    """写死四种能力的地方改成显式集合后，hiagent 的能力集本身不能变。"""
    assert api.MODEL_PROVIDER_KINDS["hiagent"] == {"text", "vlm", "video", "image"}
    assert "voice" not in api.MODEL_PROVIDER_KINDS["hiagent"]
    assert api.CUSTOM_PROVIDER_KINDS == {"text", "vlm", "video", "image", "voice"}


def test_get_models_includes_voice_media_protocols_and_hints() -> None:
    result = api.get_models()
    assert result["media_protocols"]["voice"] == ["minimax_voice_design", "qwen_voice_design"]
    assert result["protocol_hints"]["qwen_voice_design"]["label"] == "千问声音设计（阿里百炼）"
    assert result["protocol_hints"]["minimax_voice_design"]["label"] == "MiniMax 声音设计"
    for hint in result["protocol_hints"].values():
        assert set(hint) >= {"label", "base_url_example", "model_example", "note"}


def test_probe_kind_recognizes_voice() -> None:
    assert api.probe_kind(["voice"]) == "voice"


def test_media_protocol_options_voice() -> None:
    assert api.media_protocol_options(["voice"]) == {"qwen_voice_design", "minimax_voice_design"}


def test_draft_voice_model_test_routes_to_voice_probe_not_openai(monkeypatch) -> None:
    """voice 协议不是 OpenAI 兼容接口：草稿态 /models/test 必须走适配器探测。"""
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("voice 协议不应该走 _probe_openai_model")

    monkeypatch.setattr(api, "_probe_openai_model", fail_if_called)

    async def fake_probe(protocol: str, base_url: str, api_key: str, model_ref: str) -> dict:
        assert protocol == "qwen_voice_design"
        assert base_url == "https://dashscope.aliyuncs.com"
        assert api_key == "sk-test"
        return {"ok": True, "latency_ms": 12, "probe": "voice_auth", "preview": "凭证有效；这只是连通性检测，未生成声音"}

    monkeypatch.setattr(api, "probe_voice_model", fake_probe)
    monkeypatch.setattr(
        api.socket, "getaddrinfo",
        lambda host, port, **kwargs: [(api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    result = asyncio.run(api.test_model_connection({
        "protocol": "qwen_voice_design", "base_url": "https://dashscope.aliyuncs.com",
        "api_key": "sk-test", "model": "qwen3-tts-vd-2026-01-26", "kinds": ["voice"],
    }))

    assert result["ok"] is True
    assert result["probe"] == "voice_auth"


def test_draft_voice_model_test_converts_provider_error_to_422(monkeypatch) -> None:
    async def fake_probe(protocol: str, base_url: str, api_key: str, model_ref: str) -> dict:
        raise VoiceProviderError("凭证无效", failure_kind="auth_failed", http_status=401)

    monkeypatch.setattr(api, "probe_voice_model", fake_probe)
    monkeypatch.setattr(
        api.socket, "getaddrinfo",
        lambda host, port, **kwargs: [(api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.test_model_connection({
            "protocol": "qwen_voice_design", "base_url": "https://dashscope.aliyuncs.com",
            "api_key": "bad-key", "model": "qwen3-tts-vd-2026-01-26", "kinds": ["voice"],
        }))
    assert exc.value.status_code == 422
    assert "凭证无效" in exc.value.detail
    assert "bad-key" not in exc.value.detail  # 报错不带密钥


def test_saved_voice_model_test_routes_to_voice_probe(monkeypatch) -> None:
    item = _add_voice_model()

    async def fake_probe(protocol: str, base_url: str, api_key: str, model_ref: str) -> dict:
        assert protocol == "qwen_voice_design"
        return {"ok": True, "latency_ms": 8, "probe": "voice_auth", "preview": "凭证有效；这只是连通性检测，未生成声音"}

    monkeypatch.setattr(api, "probe_voice_model", fake_probe)
    monkeypatch.setattr(
        api.socket, "getaddrinfo",
        lambda host, port, **kwargs: [(api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    result = asyncio.run(api.test_saved_model(item["id"], {}))
    assert result["ok"] is True
    assert result["probe"] == "voice_auth"


def test_credential_rotation_for_voice_model_probes_before_saving(monkeypatch) -> None:
    """凭据轮换前必须真实探活；探活失败不落新密文，探活成功才写入。"""
    import app.models_registry.store as models_registry_store

    item = _add_voice_model()
    probe_calls: list[str] = []

    async def failing_probe(protocol: str, base_url: str, api_key: str, model_ref: str) -> dict:
        probe_calls.append(api_key)
        raise VoiceProviderError("凭证无效", failure_kind="auth_failed", http_status=401)

    monkeypatch.setattr(api, "probe_voice_model", failing_probe)
    monkeypatch.setattr(
        api.socket, "getaddrinfo",
        lambda host, port, **kwargs: [(api.socket.AF_INET, api.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.put_model_credentials(item["id"], {
            "base_url": "https://dashscope.aliyuncs.com", "api_key": "new-bad-key", "confirm": True,
        }))
    assert exc.value.status_code == 422
    assert probe_calls == ["new-bad-key"]
    assert models_registry_store.get_credential(item["id"])["api_key"] == "sk-test"  # 旧密文未被覆盖

    async def succeeding_probe(protocol: str, base_url: str, api_key: str, model_ref: str) -> dict:
        return {"ok": True, "latency_ms": 5, "probe": "voice_auth", "preview": "凭证有效；这只是连通性检测，未生成声音"}

    monkeypatch.setattr(api, "probe_voice_model", succeeding_probe)
    result = asyncio.run(api.put_model_credentials(item["id"], {
        "base_url": "https://dashscope.aliyuncs.com", "api_key": "new-good-key", "confirm": True,
    }))
    assert result["ok"] is True
    assert models_registry_store.get_credential(item["id"])["api_key"] == "new-good-key"


def test_sync_legacy_bindings_voice_provider_key_updates_voice_default_binding() -> None:
    """写 model_voice_provider（旧版下拉）必须同步一条 voice:default 绑定，
    否则就是 CLAUDE.md 禁止的"界面撒谎"（保存成功但选路无变化）。"""
    from app.models_registry import routing

    first = _add_voice_model()  # 第一条声音模型自动成为主用
    item = _add_voice_model(label="千问声音设计备用", model="qwen3-tts-vd-2026-01-26-b")
    assert routing.resolve("voice:default").provider == first["provider"]

    routing.sync_legacy_bindings({"model_voice_provider": item["provider"]})

    resolved = routing.resolve("voice:default")
    assert resolved is not None
    assert resolved.provider == item["provider"]
    assert resolved.protocol == "qwen_voice_design"


def test_model_voice_provider_key_is_in_legacy_setting_keys() -> None:
    from app.models_registry import routing

    assert ("voice", "model_voice_provider") in routing._LEGACY_PROVIDER_SETTING_KEYS


def test_model_voice_provider_is_a_declared_setting() -> None:
    from app.monitoring import SETTINGS_SCHEMA

    assert "model_voice_provider" in SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["model_voice_provider"]["type"] == "provider_ref"


def test_model_migration_protocol_backfill_skips_voice_entries() -> None:
    """非 video/image 的条目会被协议回填成 openai；voice 条目不能被误填。"""
    from app import model_migration
    from app.db import get_setting, set_setting
    import json as _json

    set_setting("custom_models", _json.dumps([{
        "id": "model_voice_legacy", "provider": "custom:model_voice_legacy",
        "model": "some-voice-model", "label": "旧声音条目", "kinds": ["voice"],
        "builtin": False, "base_url": "https://dashscope.aliyuncs.com", "api_key": "k",
    }], ensure_ascii=False))

    result = model_migration.migrate_builtin_models(force=True)

    assert "旧声音条目 → openai" not in result["backfilled_protocol"]
    catalog = _json.loads(get_setting("custom_models") or "[]")
    voice_entry = next(item for item in catalog if item["id"] == "model_voice_legacy")
    assert voice_entry.get("protocol", "") == ""


def test_first_voice_model_becomes_default_binding_and_second_does_not_override() -> None:
    """新能力的第一条模型自动设为主用（否则「保存模型分配」无改动不可点、主用永远建不起来）；
    已有主用时再加模型不改动它。"""
    from app.models_registry import bindings

    first = _add_voice_model()
    assert bindings.get_priority_zero("voice:default")["model_id"] == first["id"]
    _add_voice_model(label="千问声音设计备用", model="qwen3-tts-vd-2026-01-26-b")
    assert bindings.get_priority_zero("voice:default")["model_id"] == first["id"]


def test_saving_an_unbound_model_restores_missing_default_binding() -> None:
    """存量「有模型、无主用绑定」：编辑该模型并保存即补上主用绑定，界面红框随之消失。"""
    from app.models_registry import bindings

    item = _add_voice_model()
    bindings.delete_binding(bindings.get_priority_zero("voice:default")["id"])
    assert bindings.get_priority_zero("voice:default") is None
    api.update_model(item["id"], {"label": "千问声音设计", "model": "qwen3-tts-vd-2026-01-26"})
    assert bindings.get_priority_zero("voice:default")["model_id"] == item["id"]

