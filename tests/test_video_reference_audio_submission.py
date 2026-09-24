"""角色固定音色 U3：视频请求提交侧对参考音频的处理。

覆盖：① Seedance 能力快照声明 ``supports_reference_audio``/段数/总时长上限
（官方页 https://docs.volcengine.com/docs/82379/1520757：最多 3 段、总时长
不超过 15 秒）；H3 未声明的三个新字段按 pydantic 默认值 False/0/0.0，不需要
改 H3 探测代码。② ``SeedanceAdapter.create_video_task`` 在 content 末尾追加
``audio_url`` 项，且没有音频时请求体与本次改动之前逐字相同（不出现任何
``audio_url`` 项）。③ ``hiagent.create_video_task`` 的音频本地校验（角色名/
段数/必须有图或视频）在真实调用链路上生效，合法音频被原样转发给适配器。
"""
from __future__ import annotations

import asyncio

import pytest

from app import hiagent, seedance
from app.video_plan.models import ProviderVideoCapabilitySnapshot


def test_seedance_capability_snapshot_declares_reference_audio_limits() -> None:
    snapshot = seedance.SeedanceAdapter().capability_snapshot(provider="hiagent", model="any-model")
    assert snapshot.max_reference_audios == 3
    assert snapshot.max_reference_audio_total_s == 15.0


def test_minimax_h3_capability_defaults_do_not_declare_reference_audio_support() -> None:
    default = ProviderVideoCapabilitySnapshot(id="cap1", provider="minimax_h3", model="m", probe_time=0.0)
    assert default.supports_reference_audio is False
    assert default.max_reference_audios == 0
    assert default.max_reference_audio_total_s == 0.0


def _stub_seedance_post(monkeypatch, captured: dict) -> None:
    async def fake_post_json(_client, _url, payload, **_kwargs):
        captured["payload"] = payload
        return {"id": "task-1"}

    monkeypatch.setattr(hiagent, "active_model", lambda *_a, **_k: "test-video-model")
    monkeypatch.setattr(hiagent, "_model_connection", lambda *_a, **_k: ("https://example.test", {}))
    monkeypatch.setattr(hiagent, "_latest_provider_operation_request", lambda *_a, **_k: None)
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)


def test_seedance_create_video_task_appends_audio_url_content_items(monkeypatch) -> None:
    captured: dict = {}
    _stub_seedance_post(monkeypatch, captured)

    asyncio.run(
        seedance.SeedanceAdapter().create_video_task(
            "镜头1：@张三 说话。 --ratio 9:16 --dur 15",
            image_urls=[("https://img.example.test/a.jpg", "reference_image")],
            audio_urls=[("data:audio/wav;base64,AAAA", "reference_audio")],
        )
    )

    content = captured["payload"]["content"]
    roles = [item["role"] for item in content[1:]]
    assert roles == ["reference_image", "reference_audio"]
    assert content[-1]["type"] == "audio_url"
    assert content[-1]["audio_url"]["url"] == "data:audio/wav;base64,AAAA"


def test_seedance_create_video_task_without_audio_urls_has_no_audio_content(monkeypatch) -> None:
    """开关关闭/没有音频清单时请求体与本次改动之前逐字相同：content 里不
    出现任何 audio_url 项。"""
    captured: dict = {}
    _stub_seedance_post(monkeypatch, captured)

    asyncio.run(
        seedance.SeedanceAdapter().create_video_task(
            "镜头1：固定远景。 --ratio 9:16 --dur 15",
            image_urls=[("https://img.example.test/a.jpg", "reference_image")],
        )
    )

    assert all(item["type"] != "audio_url" for item in captured["payload"]["content"])


def test_hiagent_create_video_task_rejects_illegal_audio_role(monkeypatch) -> None:
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "hiagent")

    with pytest.raises(hiagent.ProviderError, match="非法视频音频输入角色"):
        asyncio.run(
            hiagent.create_video_task(
                "镜头1：固定远景。",
                image_urls=[("https://img.example.test/a.jpg", "reference_image")],
                audio_urls=[("data:audio/wav;base64,AAAA", "not_reference_audio")],
            )
        )


def test_hiagent_create_video_task_rejects_audio_without_image_or_video(monkeypatch) -> None:
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "hiagent")

    with pytest.raises(hiagent.ProviderError, match="不能单独提交"):
        asyncio.run(
            hiagent.create_video_task(
                "镜头1：固定远景。",
                audio_urls=[("data:audio/wav;base64,AAAA", "reference_audio")],
            )
        )


def test_hiagent_create_video_task_rejects_audio_over_count_cap(monkeypatch) -> None:
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "hiagent")
    four_clips = [(f"data:audio/wav;base64,{'A' * 200}", "reference_audio") for _ in range(4)]

    with pytest.raises(hiagent.ProviderError, match="超过供应商上限 3"):
        asyncio.run(
            hiagent.create_video_task(
                "镜头1：固定远景。",
                image_urls=[("https://img.example.test/a.jpg", "reference_image")],
                audio_urls=four_clips,
            )
        )


def test_hiagent_create_video_task_forwards_legal_audio_to_adapter(monkeypatch) -> None:
    captured: dict = {}
    _stub_seedance_post(monkeypatch, captured)
    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "hiagent")

    task_id = asyncio.run(
        hiagent.create_video_task(
            "镜头1：固定远景。",
            image_urls=[("https://img.example.test/a.jpg", "reference_image")],
            audio_urls=[("data:audio/wav;base64,AAAA", "reference_audio")],
        )
    )

    assert task_id == "task-1"
    assert any(item.get("type") == "audio_url" for item in captured["payload"]["content"])
