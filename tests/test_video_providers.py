"""视频/图像供应商接入层：注册表路由、自建实例与协议声明。"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app import (
    hiagent,
    image_providers,
    minimax_h3,
    monitoring,
    seedance,
    system_api,
    video_prompt_profiles,
    video_providers,
)


def _register_custom(monkeypatch, item: dict, credentials: dict | None = None) -> None:
    """把一条自建实例塞进模型库，同时覆盖所有读取 settings 的入口。"""
    settings = {
        "custom_models": json.dumps([item], ensure_ascii=False),
        "model_credentials": json.dumps(credentials or {}, ensure_ascii=False),
    }
    reader = lambda key: settings.get(key, "")  # noqa: E731
    monkeypatch.setattr(video_providers, "_CACHE", {})
    for module in (video_providers, image_providers, system_api, hiagent):
        monkeypatch.setattr(module, "get_setting", reader, raising=False)
    import app.db as db_module

    monkeypatch.setattr(db_module, "get_setting", reader)


def test_registry_resolves_builtin_providers_and_falls_back() -> None:
    assert video_providers.resolve("minimax_h3").provider == "minimax_h3"
    assert video_providers.resolve("hiagent").provider == "hiagent"
    # 未知 provider 不能抛错：那会把一次配置问题伪装成生成失败。
    assert video_providers.resolve("no-such-vendor").provider == "hiagent"
    assert video_providers.resolve("").provider == "hiagent"


def test_每个内置适配器都实现了完整协议面() -> None:
    required = (
        "create_video_task", "poll_video_task", "owns_task_id", "owns_output_url",
        "download_output", "capability_snapshot", "capability_snapshot_is_current",
        "prompt_profile", "apply_wait_policy",
    )
    for adapter in video_providers.all_adapters():
        for name in required:
            assert callable(getattr(adapter, name, None)), (adapter.provider, name)
        assert isinstance(adapter.wait_meta_keys, tuple)


def test_task_id_and_output_url_route_to_the_owning_adapter(monkeypatch) -> None:
    monkeypatch.setattr(minimax_h3, "base_url", lambda: "https://h3.example.test")
    assert video_providers.adapter_for_task_id(
        "minimax_h3:abc"
    ).provider == "minimax_h3"
    # Seedance 的 task_id 没有前缀，不能被任何适配器抢走，否则会打到错误的网关。
    assert video_providers.adapter_for_task_id("plain-task-id") is None
    assert video_providers.adapter_for_output_url(
        "https://h3.example.test/v1/outputs?filename=a.mp4"
    ).provider == "minimax_h3"
    assert video_providers.adapter_for_output_url(
        "https://cdn.example.test/a.mp4"
    ) is None


def test_prompt_profile_follows_the_registry() -> None:
    assert video_prompt_profiles.resolve_video_prompt_profile(
        provider="minimax_h3"
    ).render_format == "minimax_h3_native_fields"
    assert video_prompt_profiles.resolve_video_prompt_profile(
        provider="hiagent"
    ).render_format == "seedance_compact_director_brief"


def test_custom_h3_instance_binds_its_own_connection(monkeypatch) -> None:
    _register_custom(monkeypatch, {
        "id": "model_x", "provider": "custom:model_x", "model": "minimax-h3",
        "label": "备用 H3", "kinds": ["video"], "builtin": False,
        "protocol": "minimax_h3", "base_url": "https://spare.example.test",
        "api_key": "spare-key",
        "params": {"acceleration": "standard", "steps": 24},
    })

    adapter = video_providers.resolve("custom:model_x")

    assert adapter.provider == "custom:model_x"
    assert adapter.connection.base_url == "https://spare.example.test"
    assert adapter.connection.api_key == "spare-key"
    assert adapter.connection.acceleration == "standard"
    assert adapter.connection.steps == 24
    # 自建实例的产物只认自己的服务地址，不会被内置实例的地址误认。
    assert adapter.owns_output_url(
        "https://spare.example.test/v1/outputs?filename=a.mp4"
    )
    assert not adapter.owns_output_url(
        "https://other.example.test/v1/outputs?filename=a.mp4"
    )


def test_custom_instance_without_video_kind_is_not_a_video_provider(monkeypatch) -> None:
    _register_custom(monkeypatch, {
        "id": "model_t", "provider": "custom:model_t", "model": "gpt",
        "label": "纯文本", "kinds": ["text"], "builtin": False,
        "base_url": "https://text.example.test", "api_key": "k",
    })

    assert video_providers.resolve("custom:model_t").provider == "hiagent"


def test_media_models_must_declare_a_protocol(monkeypatch) -> None:
    monkeypatch.setattr(system_api, "_custom_models", lambda: [])
    saved: dict = {}
    monkeypatch.setattr(
        system_api, "set_setting", lambda key, value: saved.update({key: value}),
    )
    draft = {
        "provider": "custom", "provider_label": "自建 H3", "label": "备用 H3",
        "model": "minimax-h3", "kinds": ["video"],
        "base_url": "https://spare.example.test", "api_key": "spare-key",
    }

    with pytest.raises(HTTPException) as missing:
        system_api.add_model(dict(draft))
    assert missing.value.status_code == 422

    with pytest.raises(HTTPException):
        system_api.add_model({**draft, "protocol": "not-a-protocol"})

    created = system_api.add_model({**draft, "protocol": "minimax_h3"})
    assert created["protocol"] == "minimax_h3"
    assert created["kinds"] == ["video"]

    # 文本模型声明协议是配置错误，不能静默接受。
    with pytest.raises(HTTPException):
        system_api.add_model({
            **draft, "kinds": ["text"], "protocol": "minimax_h3", "model": "gpt",
        })


def test_video_and_image_providers_accept_custom_selection() -> None:
    # _custom_provider_exists 直接查库，这里就用真库落一条，验证的是真实校验路径。
    from app.db import set_setting

    set_setting("custom_models", json.dumps([{
        "id": "model_x", "provider": "custom:model_x", "model": "minimax-h3",
        "label": "备用 H3", "kinds": ["video"], "builtin": False,
        "protocol": "minimax_h3", "base_url": "https://spare.example.test",
    }], ensure_ascii=False))
    try:
        for key in ("model_video_provider", "model_image_provider"):
            assert monitoring.normalize_setting(
                key, "custom:model_x",
            ) == "custom:model_x"
            with pytest.raises(HTTPException):
                monitoring.normalize_setting(key, "custom:missing")
    finally:
        set_setting("custom_models", "[]")


def test_image_reference_dialect_is_protocol_driven() -> None:
    payload: dict = {}
    image_providers.apply_reference_images(
        payload, ["data:image/png;base64,AA"], protocol="seedream",
    )
    assert payload["image"] == "data:image/png;base64,AA"

    payload = {}
    image_providers.apply_reference_images(
        payload,
        ["data:image/png;base64,AA", "data:image/png;base64,BB"],
        protocol="seedream",
    )
    assert isinstance(payload["image"], list) and len(payload["image"]) == 2

    # 不支持参考图的协议必须报错，不能悄悄丢掉一致性约束。
    with pytest.raises(hiagent.ProviderError):
        image_providers.apply_reference_images(
            {}, ["data:image/png;base64,AA"], protocol="openai",
        )

    # 没有参考图时任何协议都不该被拦。
    payload = {}
    image_providers.apply_reference_images(payload, [], protocol="openai")
    assert payload == {}


def test_seedance_adapter_defers_ownership_to_prefixed_adapters() -> None:
    adapter = seedance.SeedanceAdapter()
    assert adapter.owns_task_id("anything") is False
    assert adapter.owns_output_url("https://cdn.example.test/a.mp4") is False
    assert adapter.serial_generation is False
    assert adapter.capability_snapshot_is_current(object()) is True


def test_seedance_wait_policy_leaves_the_generic_budget_untouched() -> None:
    policy = {"elapsed_s": 3.0, "timeout_s": 900.0, "poll_delay_s": None,
              "scope": "供应商任务", "meta_changed": False, "stage_progress": None}
    meta: dict = {}

    result = seedance.SeedanceAdapter().apply_wait_policy(
        "task", {"status": "running"}, meta, dict(policy),
        duration_s=5.0, current=100.0,
    )

    assert result == policy
    assert meta == {}


def test_seedance_create_video_task_pins_explicit_1080p_resolution(monkeypatch) -> None:
    """真实链路审阅发现提交报文尾部只有 --ratio 9:16 --dur 15，没有 resolution；

    实测（docs/PROVIDER_CAPABILITY_NOTES.md）不传该字段时网关落到 720×1280，
    竖屏分辨率是生成时唯一后期救不回的参数，必须显式钉在提交报文里。
    """
    captured: dict = {}

    async def fake_post_json(_client, _url, payload, **_kwargs):
        captured["payload"] = payload
        return {"id": "task-1080p"}

    monkeypatch.setattr(hiagent, "active_model", lambda *_a, **_k: "test-video-model")
    monkeypatch.setattr(
        hiagent, "_model_connection", lambda *_a, **_k: ("https://example.test", {})
    )
    monkeypatch.setattr(
        hiagent, "_latest_provider_operation_request", lambda *_a, **_k: None
    )
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    task_id = asyncio.run(
        seedance.SeedanceAdapter().create_video_task(
            "身体力学：向前一步 --ratio 9:16 --dur 15",
            image_urls=[("https://img.example.test/a.jpg", "first_frame")],
        )
    )

    assert task_id == "task-1080p"
    assert captured["payload"]["resolution"] == seedance.SEEDANCE_VIDEO_RESOLUTION


def test_seedance_create_video_task_writes_duration_and_ratio_fields(monkeypatch) -> None:
    """2026-09-03 在 B 上实测：网关认顶层 duration/ratio 字段——带字段出片
    1080x1920/5.08s，不带字段落到网关自己的默认值 1920x1080（横屏）。字段与
    prompt 尾部的 --ratio/--dur 文本后缀双写，文本后缀继续保留在正文里。
    """
    captured: dict = {}

    async def fake_post_json(_client, _url, payload, **_kwargs):
        captured["payload"] = payload
        return {"id": "task-fields"}

    monkeypatch.setattr(hiagent, "active_model", lambda *_a, **_k: "test-video-model")
    monkeypatch.setattr(
        hiagent, "_model_connection", lambda *_a, **_k: ("https://example.test", {})
    )
    monkeypatch.setattr(
        hiagent, "_latest_provider_operation_request", lambda *_a, **_k: None
    )
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    asyncio.run(
        seedance.SeedanceAdapter().create_video_task(
            "固定远景 --ratio 9:16 --dur 15",
        )
    )

    assert captured["payload"]["duration"] == 15
    assert captured["payload"]["ratio"] == "9:16"
    assert captured["payload"]["content"][0]["text"] == "固定远景 --ratio 9:16 --dur 15"


def test_seedance_create_video_task_prefers_call_meta_duration_when_prompt_has_no_dur(
    monkeypatch,
) -> None:
    """分镜台 2.0.0 段落式 prompt_text 从不内嵌 --dur；duration 字段必须落到
    call_meta['duration_s']（shot.duration_s），不能静默回落到 5 秒默认值。
    """
    captured: dict = {}

    async def fake_post_json(_client, _url, payload, **_kwargs):
        captured["payload"] = payload
        return {"id": "task-meta-duration"}

    monkeypatch.setattr(hiagent, "active_model", lambda *_a, **_k: "test-video-model")
    monkeypatch.setattr(
        hiagent, "_model_connection", lambda *_a, **_k: ("https://example.test", {})
    )
    monkeypatch.setattr(
        hiagent, "_latest_provider_operation_request", lambda *_a, **_k: None
    )
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)

    asyncio.run(
        seedance.SeedanceAdapter().create_video_task(
            "镜头1：固定远景，无对白。",
            call_meta={"duration_s": 15},
        )
    )

    assert captured["payload"]["duration"] == 15
    assert captured["payload"]["ratio"] == "9:16"
    assert seedance.SEEDANCE_VIDEO_RESOLUTION == "1080p"


def test_h3_wait_policy_separates_queueing_from_generation() -> None:
    adapter = minimax_h3.MiniMaxH3Adapter()
    policy = {"elapsed_s": 500.0, "timeout_s": 9000.0, "poll_delay_s": None,
              "scope": "供应商任务", "meta_changed": False, "stage_progress": None}
    meta = {"mode": "REFERENCE_IMAGE_MODE"}

    queued = adapter.apply_wait_policy(
        "minimax_h3:t", {"status": "queued", "stage": "queued", "queue_position": 2},
        meta, dict(policy), duration_s=5.0, current=1000.0,
    )
    # 还在排队时不能开始计生成超时，否则长队列会被误判成生成卡死。
    assert "minimax_h3_generation_started_at" not in meta
    assert queued["scope"] == "供应商任务"

    generating = adapter.apply_wait_policy(
        "minimax_h3:t", {"status": "running", "stage": "sampling"},
        meta, dict(policy), duration_s=5.0, current=1000.0,
    )
    assert meta["minimax_h3_generation_started_at"] == 1000.0
    assert generating["scope"] == "MiniMaxH3 生成阶段"
    assert generating["elapsed_s"] == 0.0
    assert generating["timeout_s"] < 9000.0


def test_generate_image_uses_the_selected_provider(monkeypatch) -> None:
    seen: dict = {}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "custom:model_i")
    monkeypatch.setattr(
        hiagent, "active_model", lambda kind, provider=None: "my-image-model",
    )
    def fake_connection(provider, model, *_args):
        seen["provider"] = provider
        seen["model"] = model
        return "https://img.example.test", {"Authorization": "Bearer x"}

    monkeypatch.setattr(hiagent, "_model_connection", fake_connection)
    monkeypatch.setattr(
        image_providers, "protocol_for_provider", lambda _provider: "seedream",
    )
    monkeypatch.setattr(
        hiagent, "_cached_successful_provider_response",
        lambda *_args, **_kwargs: {"data": [{"url": "/img/a.png"}]},
    )

    result = asyncio.run(hiagent.generate_image("画一张图"))

    assert seen["provider"] == "custom:model_i"
    assert seen["model"] == "my-image-model"
    assert result["url"] == "https://img.example.test/img/a.png"


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (["image"], "image"),
        (["video"], "video"),
        (["vlm"], "vlm"),
        (["text", "vlm"], "text"),
        (["text"], "text"),
        (None, "text"),
        # 同时勾多种能力时按最"重"的一种探测：媒体模型不能被当成文本模型
        # 去打 /chat/completions。
        (["text", "image"], "image"),
        (["image", "video"], "video"),
    ],
)
def test_probe_kind_follows_declared_capabilities(kinds, expected) -> None:
    assert system_api.probe_kind(kinds) == expected


def test_draft_model_test_probes_media_models_as_media(monkeypatch) -> None:
    """草稿态「测试连接」必须按勾选的能力探测。

    回归：图像模型曾因为草稿 body 只带 kinds（复数）、后端读的是 kind（单数）
    而回落成 text，跑去 POST /chat/completions，被上游以
    "expects model type 'text-generation'" 拒掉。
    """
    seen: dict = {}

    async def fake_probe(base_url, api_key, model, kind="text"):
        seen.update({"base_url": base_url, "model": model, "kind": kind})
        return {"ok": True}

    monkeypatch.setattr(system_api, "_probe_openai_model", fake_probe)

    asyncio.run(system_api.test_model_connection({
        "provider": "custom", "provider_label": "Seedream",
        "label": "Seedream", "model": "img-model",
        "kinds": ["image"], "protocol": "seedream",
        "base_url": "https://gw.example.test/v1", "api_key": "k",
    }))

    assert seen["kind"] == "image"
    assert seen["model"] == "img-model"
