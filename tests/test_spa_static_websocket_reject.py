"""SPA 静态挂载对 WebSocket 握手要干净拒绝，而不是撞上 StaticFiles 的 assert 变 500。

本服务没有 ws 路由；外部扫描器发到站点根的 ws 握手曾在 B 的日志里积下 17 段
``AssertionError`` traceback。ASGI 约定：accept 之前发 ``websocket.close`` 即拒绝握手。
"""
from __future__ import annotations

import asyncio

from app.main import SpaStaticFiles


def _run(scope: dict, tmp_path) -> list[dict]:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    asyncio.run(SpaStaticFiles(directory=tmp_path, html=True)(scope, receive, send))
    return sent


def test_websocket_scope_is_closed_before_accept(tmp_path) -> None:
    sent = _run({"type": "websocket", "path": "/ws", "headers": []}, tmp_path)
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_http_scope_still_served(tmp_path) -> None:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"",
             "scheme": "http", "server": ("test", 80), "client": ("test", 1)}
    sent = _run(scope, tmp_path)
    assert sent and sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
