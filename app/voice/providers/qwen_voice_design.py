"""千问声音设计适配器（阿里百炼 DashScope）。

官方页（2026-09-23 逐字核对）：https://help.aliyun.com/zh/model-studio/qwen-tts-voice-design

- 端点：``POST {base}/services/audio/tts/customization``，``base`` 是
  ``https://dashscope.aliyuncs.com/api/v1``（新加坡地域把域名换成
  ``dashscope-intl.aliyuncs.com``，路径不变）。
- 创建请求体：``{"model": "qwen-voice-design", "input": {"action": "create",
  "target_model": <模型库条目里的模型标识>, "preferred_name": ..., "voice_prompt":
  ..., "preview_text": ...}, "parameters": {"sample_rate": 24000,
  "response_format": "wav"}}``；响应 ``output.voice`` 是音色 ID，
  ``output.preview_audio.data`` 是 base64 编码的 24kHz WAV 试听音频，另有
  ``request_id``、``usage.count``。主会话已用真实密钥实测：每次约 5 秒，
  产出形如 ``qwen-tts-vd-mjzhouwan-voice-20260924110746219-8f6b`` 的音色 ID。
- 零成本探测（已实测 200，不生成任何音频）：``{"model": "qwen-voice-design",
  "input": {"action": "list", "page_size": 1, "page_index": 0}}``。
- 长度限制：``voice_prompt`` 官方原文「不超过 2048 个字符」。
  ``preview_text``/``preferred_name`` 官方页未给出显式上限（2026-09-23 核对
  结果），下面两个常量是防御性保守值，不代表官方限制——真的顶到会被 400
  拒绝，不会静默截断。
- 音色保留规则与账号上限（仅供参考，不在这里校验）：1 年内未用于合成自动
  删除；每账号上限 1000 个。
- 错误体形如 ``{"code", "message", "request_id"}``；官方页未列出完整错误码
  枚举，这里按 HTTP 状态码映射 ``failure_kind``，只对 ``code`` 里出现的
  "欠费"类关键词做一次尽力而为的余额不足识别（未见官方逐字确认，仅作辅助）。
"""
from __future__ import annotations

import base64
import binascii
import time
from typing import Any

import httpx

from app.voice.providers.base import (
    FailureKind,
    VoiceConnection,
    VoiceDesignRequest,
    VoiceDesignResult,
    VoiceProviderError,
    ensure_client,
)

_MAX_VOICE_PROMPT_CHARS = 2048  # 官方原文："Qwen-TTS 不超过 2048 个字符"
_MAX_PREVIEW_TEXT_CHARS = 200  # 保守值，官方页未给出显式上限，见模块文档
_MAX_PREFERRED_NAME_CHARS = 80  # 保守值，同上
_COMPATIBLE_MODE_HINT = "compatible-mode"


def _normalize_base_url(base_url: str) -> str:
    trimmed = (base_url or "").strip().rstrip("/")
    if not trimmed:
        raise VoiceProviderError("未填写千问声音设计的 Base URL", failure_kind="invalid_request")
    if _COMPATIBLE_MODE_HINT in trimmed:
        raise VoiceProviderError(
            "Base URL 不能填 OpenAI 兼容模式路径（compatible-mode）；声音设计走"
            "百炼原生 DashScope 接口，请填 https://dashscope.aliyuncs.com 或"
            " https://dashscope.aliyuncs.com/api/v1（新加坡地域把域名换成"
            " dashscope-intl.aliyuncs.com）",
            failure_kind="invalid_request",
        )
    return trimmed if trimmed.endswith("/api/v1") else f"{trimmed}/api/v1"


def _endpoint(base_url: str) -> str:
    return f"{_normalize_base_url(base_url)}/services/audio/tts/customization"


def _headers(conn: VoiceConnection) -> dict[str, str]:
    return {"Authorization": f"Bearer {conn.api_key.strip()}", "Content-Type": "application/json"}


def _classify_status(status: int, code: str) -> FailureKind:
    lowered = code.lower()
    if "arrearage" in lowered or "insufficientbalance" in lowered or "balance" in lowered:
        return "insufficient_balance"
    if status in (401, 403):
        return "auth_failed"
    if status == 429:
        return "rate_limited"
    if status == 400:
        return "invalid_request"
    if status >= 500:
        return "server_error"
    return "server_error"


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        body = {}
    message = str(body.get("message") or response.text[:200] or "未知错误")
    code = str(body.get("code") or "")
    kind = _classify_status(response.status_code, code)
    suffix = f"，{code}" if code else ""
    raise VoiceProviderError(
        f"千问声音设计失败（HTTP {response.status_code}{suffix}）：{message}",
        failure_kind=kind, http_status=response.status_code,
        retryable=kind in {"server_error", "rate_limited"},
    )


def _parse_design_response(response: httpx.Response, latency_ms: int) -> VoiceDesignResult:
    try:
        data = response.json()
    except ValueError as exc:
        raise VoiceProviderError(
            "千问声音设计返回的不是合法 JSON", failure_kind="malformed_response",
            http_status=response.status_code,
        ) from exc
    output = data.get("output") if isinstance(data, dict) else None
    voice_id = str((output or {}).get("voice") or "")
    preview = (output or {}).get("preview_audio") if isinstance(output, dict) else None
    audio_b64 = str((preview or {}).get("data") or "")
    if not voice_id or not audio_b64:
        raise VoiceProviderError(
            "千问声音设计返回缺少音色 ID 或试听音频", failure_kind="malformed_response",
            http_status=response.status_code,
        )
    try:
        audio_bytes = base64.b64decode(audio_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VoiceProviderError(
            f"千问声音设计返回的试听音频不是合法 base64：{exc}", failure_kind="malformed_response",
        ) from exc
    return VoiceDesignResult(
        provider_voice_id=voice_id, audio=audio_bytes, audio_format="wav",
        sample_rate=24000, request_id=str(data.get("request_id") or ""), latency_ms=latency_ms,
    )


def _validate_lengths(req: VoiceDesignRequest) -> None:
    if len(req.voice_prompt) > _MAX_VOICE_PROMPT_CHARS:
        raise VoiceProviderError(
            f"音色描述过长（{len(req.voice_prompt)} 字符），千问声音设计上限 {_MAX_VOICE_PROMPT_CHARS} 字符",
            failure_kind="invalid_request",
        )
    if len(req.preview_text) > _MAX_PREVIEW_TEXT_CHARS:
        raise VoiceProviderError(
            f"试听文本过长（{len(req.preview_text)} 字符），建议不超过 {_MAX_PREVIEW_TEXT_CHARS} 字符",
            failure_kind="invalid_request",
        )
    if len(req.preferred_name) > _MAX_PREFERRED_NAME_CHARS:
        raise VoiceProviderError(
            f"音色名称过长（{len(req.preferred_name)} 字符），建议不超过 {_MAX_PREFERRED_NAME_CHARS} 字符",
            failure_kind="invalid_request",
        )


async def design_voice(
    conn: VoiceConnection, req: VoiceDesignRequest, *, client: httpx.AsyncClient | None = None,
) -> VoiceDesignResult:
    """凭文字描述生成一个新音色（约 0.2 元/次）。不重试：见 dispatch.py 模块文档。"""
    if not conn.api_key.strip():
        raise VoiceProviderError("未配置千问声音设计的 API Key", failure_kind="not_configured")
    _validate_lengths(req)
    payload: dict[str, Any] = {
        "model": "qwen-voice-design",
        "input": {
            "action": "create", "target_model": conn.model_ref,
            "voice_prompt": req.voice_prompt, "preview_text": req.preview_text,
        },
        "parameters": {"sample_rate": 24000, "response_format": "wav"},
    }
    if req.preferred_name:
        payload["input"]["preferred_name"] = req.preferred_name
    started = time.perf_counter()
    try:
        async with ensure_client(client, timeout_s=60) as active:
            response = await active.post(_endpoint(conn.base_url), headers=_headers(conn), json=payload)
    except httpx.HTTPError as exc:
        raise VoiceProviderError(
            f"连接千问声音设计失败：{type(exc).__name__}", failure_kind="connection_failed", retryable=True,
        ) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    _raise_for_status(response)
    return _parse_design_response(response, latency_ms)


async def probe(conn: VoiceConnection, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """零成本鉴权探测：查询已有音色列表（``action: "list"``），不生成任何音频。"""
    if not conn.api_key.strip():
        raise VoiceProviderError("未配置千问声音设计的 API Key", failure_kind="not_configured")
    payload = {"model": "qwen-voice-design", "input": {"action": "list", "page_size": 1, "page_index": 0}}
    started = time.perf_counter()
    try:
        async with ensure_client(client, timeout_s=30) as active:
            response = await active.post(_endpoint(conn.base_url), headers=_headers(conn), json=payload)
    except httpx.HTTPError as exc:
        raise VoiceProviderError(
            f"连接千问声音设计失败：{type(exc).__name__}", failure_kind="connection_failed", retryable=True,
        ) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    _raise_for_status(response)
    return {"ok": True, "latency_ms": latency_ms, "probe": "voice_auth", "preview": "凭证有效；这只是连通性检测，未生成声音"}
