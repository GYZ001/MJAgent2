"""声音生成模型层：千问/MiniMax 适配器请求形状、错误映射、记账脱敏，以及
「换绑定即换供应商」的切换验收点。不打真实网络，全部走 ``httpx.MockTransport``
或直接монkeypatch 适配器模块函数（``app.voice.providers.dispatch`` 按属性访问
``app.hiagent.log_provider_call``/适配器模块的 ``design_voice``，不是
``from x import y`` 的名字拷贝陷阱，monkeypatch 对它们天然有效，不需要
``patch_<pkg>_everywhere`` 这类跨子模块桩）。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import hiagent, system_api
from app.models_registry import bindings
from app.voice.providers import qwen_voice_design as qwen
from app.voice.providers import minimax_voice_design as minimax
from app.voice.providers import dispatch
from app.voice.providers.base import (
    VoiceConnection,
    VoiceDesignRequest,
    VoiceDesignResult,
    VoiceProviderError,
    detect_audio_format,
)


def _wav_bytes() -> bytes:
    return b"RIFF" + b"\x24\x00\x00\x00" + b"WAVEfmt test-body"


def _add_voice_model(protocol: str, *, base_url: str, api_key: str, model: str, label: str) -> dict:
    return system_api.add_model({
        "provider": "custom", "provider_label": label, "base_url": base_url,
        "api_key": api_key, "protocol": protocol, "model": model, "label": label,
        "kinds": ["voice"],
    })


# ---------- detect_audio_format ----------

def test_detect_audio_format_by_header() -> None:
    assert detect_audio_format(_wav_bytes()) == "wav"
    assert detect_audio_format(b"ID3\x03\x00\x00\x00rest") == "mp3"
    assert detect_audio_format(bytes([0xFF, 0xFB, 0x90, 0x00])) == "mp3"
    assert detect_audio_format(b"not-an-audio-header") == "bin"


# ---------- 千问适配器 ----------

async def test_qwen_design_voice_request_shape_and_response_parsing() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        import base64
        return httpx.Response(200, json={
            "output": {
                "voice": "qwen-tts-vd-test-0001",
                "preview_audio": {"data": base64.b64encode(_wav_bytes()).decode("ascii")},
            },
            "request_id": "req-abc", "usage": {"count": 1},
        })

    conn = VoiceConnection(
        base_url="https://dashscope.aliyuncs.com", api_key="sk-test",
        model_ref="qwen3-tts-vd-2026-01-26", params={},
    )
    req = VoiceDesignRequest(voice_prompt="温柔知性的青年女声", preview_text="你好，很高兴认识你", preferred_name="小美")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await qwen.design_voice(conn, req, client=client)

    assert captured["url"] == "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "qwen-voice-design"
    assert captured["body"]["input"]["action"] == "create"
    assert captured["body"]["input"]["target_model"] == "qwen3-tts-vd-2026-01-26"
    assert captured["body"]["input"]["preferred_name"] == "小美"
    assert captured["body"]["parameters"] == {"sample_rate": 24000, "response_format": "wav"}
    assert result.provider_voice_id == "qwen-tts-vd-test-0001"
    assert result.audio == _wav_bytes()
    assert result.audio_format == "wav"
    assert result.sample_rate == 24000
    assert result.request_id == "req-abc"
    assert result.latency_ms >= 0


@pytest.mark.parametrize("base_in,base_out", [
    ("https://dashscope.aliyuncs.com", "https://dashscope.aliyuncs.com/api/v1"),
    ("https://dashscope.aliyuncs.com/api/v1", "https://dashscope.aliyuncs.com/api/v1"),
    ("https://dashscope.aliyuncs.com/api/v1/", "https://dashscope.aliyuncs.com/api/v1"),
    ("https://dashscope-intl.aliyuncs.com", "https://dashscope-intl.aliyuncs.com/api/v1"),
])
def test_qwen_base_url_normalization(base_in: str, base_out: str) -> None:
    assert qwen._normalize_base_url(base_in) == base_out


def test_qwen_base_url_rejects_compatible_mode_path() -> None:
    with pytest.raises(VoiceProviderError) as exc:
        qwen._normalize_base_url("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert exc.value.failure_kind == "invalid_request"


async def test_qwen_probe_sends_list_action_not_generate() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": {"voices": []}, "request_id": "r1"})

    conn = VoiceConnection(base_url="https://dashscope.aliyuncs.com/api/v1", api_key="sk-test", model_ref="m", params={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await qwen.probe(conn, client=client)

    assert captured["body"] == {"model": "qwen-voice-design", "input": {"action": "list", "page_size": 1, "page_index": 0}}
    assert result["ok"] is True
    assert result["probe"] == "voice_auth"


@pytest.mark.parametrize("status,expected_kind", [
    (401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited"),
    (400, "invalid_request"), (500, "server_error"), (503, "server_error"),
])
async def test_qwen_design_voice_error_status_mapping(status: int, expected_kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"code": "SomeError", "message": "boom", "request_id": "r"})

    conn = VoiceConnection(base_url="https://dashscope.aliyuncs.com/api/v1", api_key="sk-test", model_ref="m", params={})
    req = VoiceDesignRequest(voice_prompt="x", preview_text="y")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VoiceProviderError) as exc:
            await qwen.design_voice(conn, req, client=client)
    assert exc.value.failure_kind == expected_kind
    assert exc.value.http_status == status


async def test_qwen_design_voice_malformed_response_missing_voice_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {}, "request_id": "r"})

    conn = VoiceConnection(base_url="https://dashscope.aliyuncs.com/api/v1", api_key="sk-test", model_ref="m", params={})
    req = VoiceDesignRequest(voice_prompt="x", preview_text="y")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VoiceProviderError) as exc:
            await qwen.design_voice(conn, req, client=client)
    assert exc.value.failure_kind == "malformed_response"


async def test_qwen_missing_api_key_raises_not_configured() -> None:
    conn = VoiceConnection(base_url="https://dashscope.aliyuncs.com", api_key="", model_ref="m", params={})
    req = VoiceDesignRequest(voice_prompt="x", preview_text="y")
    with pytest.raises(VoiceProviderError) as exc:
        await qwen.design_voice(conn, req)
    assert exc.value.failure_kind == "not_configured"


def test_qwen_voice_prompt_over_2048_chars_rejected() -> None:
    conn = VoiceConnection(base_url="https://dashscope.aliyuncs.com", api_key="sk-test", model_ref="m", params={})
    req = VoiceDesignRequest(voice_prompt="x" * 2049, preview_text="y")
    import asyncio
    with pytest.raises(VoiceProviderError) as exc:
        asyncio.run(qwen.design_voice(conn, req))
    assert exc.value.failure_kind == "invalid_request"


# ---------- MiniMax 适配器（无密钥，只单测；未做真实接口验证） ----------

async def test_minimax_design_voice_request_shape_and_hex_decoding() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "voice_id": "mm-voice-001", "trial_audio": _wav_bytes().hex(),
            "base_resp": {"status_code": 0, "status_msg": "success"},
        })

    conn = VoiceConnection(base_url="https://api.minimaxi.com", api_key="mm-key", model_ref="", params={})
    req = VoiceDesignRequest(voice_prompt="沉稳的中年男声", preview_text="早上好，今天天气不错")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await minimax.design_voice(conn, req, client=client)

    assert captured["url"] == "https://api.minimaxi.com/v1/voice_design"
    assert captured["auth"] == "Bearer mm-key"
    assert captured["body"] == {"prompt": "沉稳的中年男声", "preview_text": "早上好，今天天气不错"}
    assert result.provider_voice_id == "mm-voice-001"
    assert result.audio == _wav_bytes()
    assert result.audio_format == "wav"
    assert result.sample_rate is None


@pytest.mark.parametrize("status_code,expected_kind", [
    (1004, "auth_failed"), (1002, "rate_limited"), (1039, "rate_limited"),
    (1008, "insufficient_balance"), (1027, "invalid_request"), (2013, "invalid_request"),
    (1013, "server_error"), (1000, "server_error"), (1001, "server_error"),
    (9999, "server_error"),
])
async def test_minimax_business_status_code_mapping(status_code: int, expected_kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"base_resp": {"status_code": status_code, "status_msg": "x"}})

    conn = VoiceConnection(base_url="https://api.minimax.io", api_key="k", model_ref="", params={})
    req = VoiceDesignRequest(voice_prompt="x", preview_text="y")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VoiceProviderError) as exc:
            await minimax.design_voice(conn, req, client=client)
    assert exc.value.failure_kind == expected_kind


async def test_minimax_http_level_failure_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"base_resp": {"status_code": 0, "status_msg": ""}})

    conn = VoiceConnection(base_url="https://api.minimax.io", api_key="k", model_ref="", params={})
    req = VoiceDesignRequest(voice_prompt="x", preview_text="y")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VoiceProviderError) as exc:
            await minimax.design_voice(conn, req, client=client)
    assert exc.value.failure_kind == "server_error"
    assert exc.value.http_status == 500


async def test_minimax_malformed_hex_audio_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "voice_id": "v1", "trial_audio": "not-hex!!", "base_resp": {"status_code": 0},
        })

    conn = VoiceConnection(base_url="https://api.minimax.io", api_key="k", model_ref="", params={})
    req = VoiceDesignRequest(voice_prompt="x", preview_text="y")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VoiceProviderError) as exc:
            await minimax.design_voice(conn, req, client=client)
    assert exc.value.failure_kind == "malformed_response"


async def test_minimax_probe_queries_get_voice_endpoint_readonly() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"base_resp": {"status_code": 0}})

    conn = VoiceConnection(base_url="https://api.minimaxi.com", api_key="k", model_ref="", params={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await minimax.probe(conn, client=client)

    assert captured["url"] == "https://api.minimaxi.com/v1/get_voice"
    assert captured["body"] == {"voice_type": "voice_generation"}
    assert result["ok"] is True


def test_minimax_preview_text_over_500_chars_rejected() -> None:
    conn = VoiceConnection(base_url="https://api.minimax.io", api_key="k", model_ref="", params={})
    req = VoiceDesignRequest(voice_prompt="x", preview_text="y" * 501)
    import asyncio
    with pytest.raises(VoiceProviderError) as exc:
        asyncio.run(minimax.design_voice(conn, req))
    assert exc.value.failure_kind == "invalid_request"


async def test_minimax_missing_api_key_raises_not_configured() -> None:
    conn = VoiceConnection(base_url="https://api.minimax.io", api_key="", model_ref="", params={})
    with pytest.raises(VoiceProviderError) as exc:
        await minimax.probe(conn)
    assert exc.value.failure_kind == "not_configured"


# ---------- dispatch：切换验收点 + 记账脱敏 ----------

async def test_switching_voice_default_binding_switches_adapter(monkeypatch) -> None:
    """同一个 design_voice() 调用：voice:default 绑到千问条目走千问适配器、
    改绑到 MiniMax 条目走 MiniMax 适配器——这是「以后可以随意切换音频模型，
    换绑定即换供应商，业务代码不改」的验收点。"""
    calls: list[str] = []

    async def fake_qwen(conn: VoiceConnection, req: VoiceDesignRequest, *, client=None) -> VoiceDesignResult:
        calls.append("qwen")
        return VoiceDesignResult(
            provider_voice_id="qwen-v1", audio=_wav_bytes(), audio_format="wav",
            sample_rate=24000, request_id="q1", latency_ms=10,
        )

    async def fake_minimax(conn: VoiceConnection, req: VoiceDesignRequest, *, client=None) -> VoiceDesignResult:
        calls.append("minimax")
        return VoiceDesignResult(
            provider_voice_id="mm-v1", audio=bytes.fromhex("deadbeef"), audio_format="bin",
            sample_rate=None, request_id="", latency_ms=5,
        )

    monkeypatch.setattr(qwen, "design_voice", fake_qwen)
    monkeypatch.setattr(minimax, "design_voice", fake_minimax)

    qwen_item = _add_voice_model(
        "qwen_voice_design", base_url="https://dashscope.aliyuncs.com", api_key="sk-q",
        model="qwen3-tts-vd-2026-01-26", label="千问声音设计",
    )
    minimax_item = _add_voice_model(
        "minimax_voice_design", base_url="https://api.minimax.io", api_key="mm-k",
        model="minimax-voice", label="MiniMax 声音设计",
    )
    req = VoiceDesignRequest(voice_prompt="温柔知性的青年女声", preview_text="你好，很高兴认识你")

    bindings.upsert_binding(purpose="voice:default", model_id=qwen_item["id"], priority=0, created_by="test")
    result_qwen = await dispatch.design_voice(req)
    assert calls == ["qwen"]
    assert result_qwen.provider_voice_id == "qwen-v1"

    bindings.upsert_binding(purpose="voice:default", model_id=minimax_item["id"], priority=0, created_by="test")
    result_minimax = await dispatch.design_voice(req)
    assert calls == ["qwen", "minimax"]
    assert result_minimax.provider_voice_id == "mm-v1"


async def test_design_voice_falls_back_to_catalog_voice_model_when_unbound(monkeypatch) -> None:
    """模型已在模型库、但 voice:default 没有主用绑定（2026-09-24 B 上实测状态）：
    按模型中心「当前运行」同一口径回落到第一条声音模型，不再报「未配置」。"""
    calls: list[str] = []

    async def fake_qwen(conn: VoiceConnection, req: VoiceDesignRequest, *, client=None) -> VoiceDesignResult:
        calls.append(conn.model_ref)
        return VoiceDesignResult(
            provider_voice_id="qwen-v1", audio=_wav_bytes(), audio_format="wav",
            sample_rate=24000, request_id="q1", latency_ms=10,
        )

    monkeypatch.setattr(qwen, "design_voice", fake_qwen)
    _add_voice_model(
        "qwen_voice_design", base_url="https://dashscope.aliyuncs.com", api_key="sk-q",
        model="qwen3-tts-vd-2026-01-26", label="千问声音设计",
    )
    binding = bindings.get_priority_zero("voice:default")
    assert binding is not None  # 添加第一条声音模型时已自动设为主用
    bindings.delete_binding(binding["id"])  # 复现「有模型、无绑定」的存量状态

    result = await dispatch.design_voice(VoiceDesignRequest(voice_prompt="x", preview_text="y"))
    assert calls == ["qwen3-tts-vd-2026-01-26"]
    assert result.provider_voice_id == "qwen-v1"


async def test_design_voice_not_configured_when_no_binding() -> None:
    req = VoiceDesignRequest(voice_prompt="x", preview_text="y")
    with pytest.raises(VoiceProviderError) as exc:
        await dispatch.design_voice(req)
    assert exc.value.failure_kind == "not_configured"
    assert "模型中心" in str(exc.value)


async def test_design_voice_logs_provider_call_without_key_or_raw_audio(monkeypatch) -> None:
    logged: dict = {}

    def fake_log(kind, model, status, http_status, latency_ms, *, error=None, meta=None,
                 request_json=None, response_json=None, operation_id=None):
        logged.update(
            kind=kind, model=model, status=status, meta=meta,
            request_json=request_json, response_json=response_json,
        )

    monkeypatch.setattr(hiagent, "log_provider_call", fake_log)

    async def fake_design(conn: VoiceConnection, req: VoiceDesignRequest, *, client=None) -> VoiceDesignResult:
        return VoiceDesignResult(
            provider_voice_id="v1", audio=_wav_bytes() + b"\x00" * 500, audio_format="wav",
            sample_rate=24000, request_id="req-1", latency_ms=123,
        )

    monkeypatch.setattr(qwen, "design_voice", fake_design)

    item = _add_voice_model(
        "qwen_voice_design", base_url="https://dashscope.aliyuncs.com", api_key="super-secret-key",
        model="qwen3-tts-vd-2026-01-26", label="千问声音设计",
    )
    bindings.upsert_binding(purpose="voice:default", model_id=item["id"], priority=0, created_by="test")

    result = await dispatch.design_voice(VoiceDesignRequest(voice_prompt="温柔女声", preview_text="你好，很高兴认识你"))

    assert result.provider_voice_id == "v1"
    assert logged["kind"] == "voice_design"
    assert logged["status"] == "OK"
    dumped_request = json.dumps(logged["request_json"], ensure_ascii=False)
    dumped_response = json.dumps(logged["response_json"], ensure_ascii=False)
    assert "super-secret-key" not in dumped_request
    assert "super-secret-key" not in dumped_response
    assert "RIFF" not in dumped_response  # 原始音频字节不落库
    assert logged["response_json"]["audio"].startswith("[omitted ")
    assert "sha256:" in logged["response_json"]["audio"]


async def test_design_voice_failure_logs_failed_status_without_raising_extra(monkeypatch) -> None:
    logged: dict = {}

    def fake_log(kind, model, status, http_status, latency_ms, *, error=None, meta=None,
                 request_json=None, response_json=None, operation_id=None):
        logged.update(kind=kind, status=status, http_status=http_status, error=error)

    monkeypatch.setattr(hiagent, "log_provider_call", fake_log)

    async def fake_design(conn: VoiceConnection, req: VoiceDesignRequest, *, client=None) -> VoiceDesignResult:
        raise VoiceProviderError("上游拒绝", failure_kind="invalid_request", http_status=400)

    monkeypatch.setattr(qwen, "design_voice", fake_design)

    item = _add_voice_model(
        "qwen_voice_design", base_url="https://dashscope.aliyuncs.com", api_key="sk-test",
        model="qwen3-tts-vd-2026-01-26", label="千问声音设计",
    )
    bindings.upsert_binding(purpose="voice:default", model_id=item["id"], priority=0, created_by="test")

    with pytest.raises(VoiceProviderError):
        await dispatch.design_voice(VoiceDesignRequest(voice_prompt="x", preview_text="y"))

    assert logged["kind"] == "voice_design"
    assert logged["status"] == "FAILED"
    assert logged["http_status"] == 400
    assert "上游拒绝" in logged["error"]


async def test_probe_voice_model_unknown_protocol_raises() -> None:
    with pytest.raises(VoiceProviderError) as exc:
        await dispatch.probe_voice_model("not_a_real_protocol", "https://example.com", "k", "m")
    assert exc.value.failure_kind == "invalid_request"
