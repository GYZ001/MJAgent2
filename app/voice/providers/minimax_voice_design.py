"""MiniMax 声音设计适配器。没有 MiniMax 密钥，本适配器只跑过单元测试
（``httpx.MockTransport`` 构造的假响应），未做真实接口验证——见交付报告
「已知限制」。

官方页（2026-09-23 逐字核对）：
https://platform.minimax.io/docs/api-reference/voice-design-design

- 端点：``POST {base}/v1/voice_design``。文档只列出 ``https://api.minimax.io``
  一个域名；本适配器不锁死域名，直接用模型库条目里配置的 ``base_url``（国内站
  ``api.minimaxi.com``、国际站 ``api.minimax.io`` 都能填）。
- 请求体：``prompt``（音色描述，必填）、``preview_text``（试听文本，必填，
  官方原文 ``"maxLength: 500"``）、``voice_id``（可选，不填则「a unique
  voice_id will be automatically created」）。公共请求里的 ``preferred_name``
  在这里不使用：官方文档没有给出 ``voice_id`` 的合法字符集，贸然拿人物名当
  ``voice_id`` 有撞非法字符的风险，宁可让 MiniMax 分配随机 ID。
- 响应：``voice_id``、``trial_audio``（官方原文「generated preview audio in
  hex-encoded format」，即十六进制编码，不是 base64）、
  ``base_resp.status_code``/``status_msg``（0 = 成功）。
- 鉴权：``Authorization: Bearer <api_key>``（官方原文 "HTTP: Bearer Auth"）。
- 错误码：官方原文枚举 ``0`` 成功、``1004`` 鉴权失败、``1002``/``1039`` 限流、
  ``1008`` 余额不足、``1027``/``2013`` 参数错误，其余（``1000``/``1001``/
  ``1013``）计入服务端错误。HTTP 层非 200（例如网关直接拒绝请求）另按状态码
  分类，两层独立判断。
- 零成本探测：``POST {base}/v1/get_voice``，body ``{"voice_type":
  "voice_generation"}``（官方页 https://platform.minimax.io/docs/api-reference/
  voice-management-get，`voice_type` 原文枚举含 "voice_generation" = "Voices
  generated via text-to-voice API"）。这是只读查询已有音色列表的接口，语义上
  与生成音频的计费接口明显不同类，但官方页没有逐字标注"免费"——未做真实调用
  验证，若后续接入真实密钥发现探测本身产生费用，需要改用别的判据。
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.voice.providers.base import (
    FailureKind,
    VoiceConnection,
    VoiceDesignRequest,
    VoiceDesignResult,
    VoiceProviderError,
    detect_audio_format,
    ensure_client,
)

_MAX_PREVIEW_TEXT_CHARS = 500  # 官方原文："maxLength: 500"

# status_code -> failure_kind，逐字取自官方错误码列表（见模块文档）。
_STATUS_CODE_FAILURE_KIND: dict[int, FailureKind] = {
    1000: "server_error", 1001: "server_error", 1013: "server_error",
    1002: "rate_limited", 1039: "rate_limited",
    1004: "auth_failed",
    1008: "insufficient_balance",
    1027: "invalid_request", 2013: "invalid_request",
}


def _base(conn: VoiceConnection) -> str:
    """地址填到域名或带 ``/v1`` 都接受；端点路径自带 ``/v1``，这里去掉重复段。"""
    trimmed = conn.base_url.strip().rstrip("/")
    return trimmed[: -len("/v1")] if trimmed.endswith("/v1") else trimmed


def _headers(conn: VoiceConnection) -> dict[str, str]:
    return {"Authorization": f"Bearer {conn.api_key.strip()}", "Content-Type": "application/json"}


def _http_failure_kind(status_code: int) -> FailureKind:
    if status_code in (401, 403):
        return "auth_failed"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    return "invalid_request"


def _raise_for_status(response: httpx.Response) -> dict[str, Any]:
    try:
        data: dict[str, Any] = response.json()
    except ValueError as exc:
        raise VoiceProviderError(
            "MiniMax 声音设计返回的不是合法 JSON", failure_kind="malformed_response",
            http_status=response.status_code,
        ) from exc
    if not response.is_success:
        kind = _http_failure_kind(response.status_code)
        message = str((data.get("base_resp") or {}).get("status_msg") or response.text[:200] or "未知错误")
        raise VoiceProviderError(
            f"MiniMax 声音设计失败（HTTP {response.status_code}）：{message}",
            failure_kind=kind, http_status=response.status_code,
            retryable=kind in {"server_error", "rate_limited"},
        )
    status_code = int((data.get("base_resp") or {}).get("status_code") or 0)
    if status_code != 0:
        message = str((data.get("base_resp") or {}).get("status_msg") or "未知错误")
        kind = _STATUS_CODE_FAILURE_KIND.get(status_code, "server_error")
        raise VoiceProviderError(
            f"MiniMax 声音设计失败（status_code={status_code}）：{message}",
            failure_kind=kind, http_status=response.status_code,
            retryable=kind in {"server_error", "rate_limited"},
        )
    return data


def _parse_design_response(data: dict[str, Any], latency_ms: int) -> VoiceDesignResult:
    voice_id = str(data.get("voice_id") or "")
    hex_audio = str(data.get("trial_audio") or "")
    if not voice_id or not hex_audio:
        raise VoiceProviderError("MiniMax 声音设计返回缺少 voice_id 或 trial_audio", failure_kind="malformed_response")
    try:
        audio_bytes = bytes.fromhex(hex_audio)
    except ValueError as exc:
        raise VoiceProviderError(
            f"MiniMax 声音设计返回的 trial_audio 不是合法十六进制：{exc}", failure_kind="malformed_response",
        ) from exc
    return VoiceDesignResult(
        provider_voice_id=voice_id, audio=audio_bytes, audio_format=detect_audio_format(audio_bytes),
        sample_rate=None, request_id="", latency_ms=latency_ms,
    )


async def design_voice(
    conn: VoiceConnection, req: VoiceDesignRequest, *, client: httpx.AsyncClient | None = None,
) -> VoiceDesignResult:
    """凭文字描述生成一个新音色。不重试：见 dispatch.py 模块文档。"""
    if not conn.api_key.strip():
        raise VoiceProviderError("未配置 MiniMax 声音设计的 API Key", failure_kind="not_configured")
    if len(req.preview_text) > _MAX_PREVIEW_TEXT_CHARS:
        raise VoiceProviderError(
            f"试听文本过长（{len(req.preview_text)} 字符），MiniMax 声音设计上限 {_MAX_PREVIEW_TEXT_CHARS} 字符",
            failure_kind="invalid_request",
        )
    payload = {"prompt": req.voice_prompt, "preview_text": req.preview_text}
    started = time.perf_counter()
    try:
        async with ensure_client(client, timeout_s=60) as active:
            response = await active.post(f"{_base(conn)}/v1/voice_design", headers=_headers(conn), json=payload)
    except httpx.HTTPError as exc:
        raise VoiceProviderError(
            f"连接 MiniMax 声音设计失败：{type(exc).__name__}", failure_kind="connection_failed", retryable=True,
        ) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    data = _raise_for_status(response)
    return _parse_design_response(data, latency_ms)


async def probe(conn: VoiceConnection, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """零成本鉴权探测：查询已有音色列表，不生成任何音频（见模块文档已知限制）。"""
    if not conn.api_key.strip():
        raise VoiceProviderError("未配置 MiniMax 声音设计的 API Key", failure_kind="not_configured")
    started = time.perf_counter()
    try:
        async with ensure_client(client, timeout_s=30) as active:
            response = await active.post(
                f"{_base(conn)}/v1/get_voice", headers=_headers(conn), json={"voice_type": "voice_generation"},
            )
    except httpx.HTTPError as exc:
        raise VoiceProviderError(
            f"连接 MiniMax 声音设计失败：{type(exc).__name__}", failure_kind="connection_failed", retryable=True,
        ) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    _raise_for_status(response)
    return {"ok": True, "latency_ms": latency_ms, "probe": "voice_auth", "preview": "凭证有效；这只是连通性检测，未生成声音"}
