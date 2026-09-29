"""千问「用已有音色合成任意文本」能力（2026-09-28 成片增强·主角内心独白新增）：
``app.voice.providers.qwen_voice_design.synthesize_speech`` 的请求形状/响应
解析，以及 ``app.voice.providers.dispatch.synthesize_speech_for_voice`` 按
``model_id`` 重新解析模型条目（而不是走 purpose 选路）这条一致性约束。全部
走 ``httpx.MockTransport``，不打真实网络。
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from app import system_api
from app.voice.providers import dispatch
from app.voice.providers import qwen_voice_design as qwen
from app.voice.providers.base import VoiceConnection, VoiceProviderError


def _conn() -> VoiceConnection:
    return VoiceConnection(
        base_url="https://dashscope.aliyuncs.com", api_key="sk-test",
        model_ref="qwen3-tts-vd-2026-01-26", params={},
    )


async def test_synthesize_speech_request_shape_and_base64_response() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "output": {"audio": {"data": base64.b64encode(b"RIFFfakewav").decode("ascii")}},
            "request_id": "req-mono-1",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await qwen.synthesize_speech(_conn(), "我到底该不该相信他", "voice-abc", client=client)

    assert captured["url"] == "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    assert captured["body"] == {"model": "qwen3-tts-vd-2026-01-26", "input": {"text": "我到底该不该相信他", "voice": "voice-abc"}}
    assert result.audio == b"RIFFfakewav"
    assert result.audio_format == "wav"
    assert result.request_id == "req-mono-1"


async def test_synthesize_speech_url_shape_raises_not_implemented() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"audio": {"url": "https://example.com/a.wav"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VoiceProviderError) as exc:
            await qwen.synthesize_speech(_conn(), "text", "voice-abc", client=client)
    assert exc.value.failure_kind == "malformed_response"


async def test_synthesize_speech_unknown_shape_raises_malformed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VoiceProviderError) as exc:
            await qwen.synthesize_speech(_conn(), "text", "voice-abc", client=client)
    assert exc.value.failure_kind == "malformed_response"


async def test_synthesize_speech_missing_api_key_raises_not_configured() -> None:
    conn = VoiceConnection(base_url="https://dashscope.aliyuncs.com", api_key="", model_ref="m", params={})
    with pytest.raises(VoiceProviderError) as exc:
        await qwen.synthesize_speech(conn, "text", "voice-abc")
    assert exc.value.failure_kind == "not_configured"


async def test_synthesize_speech_text_too_long_rejected() -> None:
    with pytest.raises(VoiceProviderError) as exc:
        await qwen.synthesize_speech(_conn(), "字" * 2001, "voice-abc")
    assert exc.value.failure_kind == "invalid_request"


# ---------- dispatch.synthesize_speech_for_voice：按 model_id 重新解析，不走 purpose 选路 ----------

async def test_synthesize_speech_for_voice_uses_stored_model_id_not_current_binding(monkeypatch) -> None:
    """即使 voice:default 当前绑定了别的模型，也必须用落库时记录的 model_id
    重新解析出与设计音色时一致的 ``target_model``（见
    ``app.voice.providers.qwen_voice_design.synthesize_speech`` 模块文档）。"""
    designed_item = system_api.add_model({
        "provider": "custom", "provider_label": "设计时的千问条目", "base_url": "https://dashscope.aliyuncs.com",
        "api_key": "sk-designed", "protocol": "qwen_voice_design", "model": "qwen3-tts-vd-2026-01-26",
        "label": "设计时的千问条目", "kinds": ["voice"],
    })
    captured: dict = {}

    async def fake_synthesize(conn: VoiceConnection, text: str, voice_id: str, *, client=None):
        captured["model_ref"] = conn.model_ref
        captured["api_key"] = conn.api_key
        from app.voice.providers.base import SpeechSynthesisResult
        return SpeechSynthesisResult(audio=b"RIFFx", audio_format="wav", sample_rate=24000, request_id="r1", latency_ms=1)

    monkeypatch.setattr(qwen, "synthesize_speech", fake_synthesize)

    result = await dispatch.synthesize_speech_for_voice(designed_item["id"], "我好害怕", "voice-xyz")
    assert captured["model_ref"] == "qwen3-tts-vd-2026-01-26"
    assert captured["api_key"] == "sk-designed"
    assert result.audio == b"RIFFx"


async def test_synthesize_speech_for_voice_unsupported_protocol_raises_invalid_request() -> None:
    minimax_item = system_api.add_model({
        "provider": "custom", "provider_label": "MiniMax 声音设计", "base_url": "https://api.minimax.io",
        "api_key": "mm-k", "protocol": "minimax_voice_design", "model": "minimax-voice",
        "label": "MiniMax 声音设计", "kinds": ["voice"],
    })
    with pytest.raises(VoiceProviderError) as exc:
        await dispatch.synthesize_speech_for_voice(minimax_item["id"], "text", "voice-1")
    assert exc.value.failure_kind == "invalid_request"
    assert "不支持任意文本合成" in str(exc.value)


async def test_synthesize_speech_for_voice_missing_model_id_raises_not_configured() -> None:
    with pytest.raises(VoiceProviderError) as exc:
        await dispatch.synthesize_speech_for_voice("model-does-not-exist", "text", "voice-1")
    assert exc.value.failure_kind == "not_configured"
