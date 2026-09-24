"""声音生成协议的共享类型：连接信息、请求/结果形状、统一错误。

与 ``app.hiagent.ProviderError`` 是两套独立的异常体系，故意不复用：声音生成
只有「设计一个新音色」这一种付费调用，从不自动重试（发出后超时就是「不知道
有没有扣费」，交给调用方决定，见 ``app.voice.providers.dispatch`` 模块文档），
不需要 ``ProviderFailure``/``ProviderFailureKind`` 那整套为可重试技术性失败
设计的分类基础设施。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import httpx

FailureKind = Literal[
    "not_configured", "auth_failed", "rate_limited", "invalid_request",
    "insufficient_balance", "server_error", "connection_failed", "malformed_response",
]


@dataclass(frozen=True)
class VoiceConnection:
    """一次声音生成请求要用的连接信息（选路结果 ``ResolvedModel`` 的最小切片）。"""

    base_url: str
    api_key: str
    model_ref: str
    params: dict[str, Any]


@dataclass(frozen=True)
class VoiceDesignRequest:
    """凭文字描述设计一个新音色。"""

    voice_prompt: str
    preview_text: str
    preferred_name: str = ""
    language: str = "zh"


@dataclass(frozen=True)
class VoiceDesignResult:
    """设计结果：音色 ID + 一段可播放的试听音频。"""

    provider_voice_id: str
    audio: bytes
    audio_format: str  # "wav" | "mp3" | ...
    sample_rate: int | None
    request_id: str
    latency_ms: int


class VoiceProviderError(Exception):
    """声音生成失败：``message`` 面向界面，``failure_kind`` 供上层归类。"""

    def __init__(
        self,
        message: str,
        *,
        failure_kind: FailureKind = "server_error",
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind
        self.http_status = http_status
        self.retryable = retryable


def detect_audio_format(data: bytes) -> str:
    """按文件头判断音频容器格式；两者都不像时如实标"bin"，不猜一个好看的默认值。"""
    if data[:4] == b"RIFF":
        return "wav"
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    return "bin"


@asynccontextmanager
async def ensure_client(
    client: httpx.AsyncClient | None, *, timeout_s: float,
) -> AsyncIterator[httpx.AsyncClient]:
    """测试可注入 ``client``（配 ``httpx.MockTransport``）；调用方不传时自建自关。"""
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10)) as owned:
        yield owned
