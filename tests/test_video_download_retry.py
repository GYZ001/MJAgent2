"""视频结果下载失败的重试契约（2026-09-05）。"""

from __future__ import annotations

import pytest

class _FakeResp:
    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        self.content = b"\x00"


class _FakeClient:
    def __init__(self, status_code: int) -> None:
        self._status = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url: str):
        return _FakeResp(self._status, url)


@pytest.mark.parametrize("status_code,expected", [(500, True), (502, True), (404, False), (403, False)])
def test_video_download_5xx_is_retryable_but_4xx_is_terminal(monkeypatch, tmp_path, status_code, expected):
    """2026-09-05 我欲封天第 2/23 集：供应商任务已成功，下载对象存储时 HTTP 500 被判成不可
    重试的技术故障转人工（ERR-20260905-639104 等 3 例）。5xx 是瞬时故障，应重新轮询同一任务
    再下载；4xx 才是 URL 失效。"""
    import asyncio
    import socket

    from app import hiagent

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kw: _FakeClient(status_code))
    with pytest.raises(hiagent.ProviderError) as info:
        asyncio.run(hiagent._download_public_url("https://cdn.example.com/v.mp4", str(tmp_path / "v.mp4")))
    assert info.value.retryable is expected
    assert f"HTTP {status_code}" in str(info.value)
