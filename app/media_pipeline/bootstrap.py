"""启动 / 停止媒体流水线附属协程（poller、设置热更新）。"""
from __future__ import annotations

import asyncio

from app.media_pipeline.concurrency import migrate_legacy_settings, reload_limits_from_settings

_hot_reload_task: asyncio.Task | None = None


async def _settings_hot_reload_loop(interval: float = 5.0) -> None:
    while True:
        try:
            reload_limits_from_settings()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(interval)


def start_media_pipeline() -> None:
    global _hot_reload_task
    migrate_legacy_settings()
    reload_limits_from_settings()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _hot_reload_task is None or _hot_reload_task.done():
        _hot_reload_task = loop.create_task(_settings_hot_reload_loop(), name="media-settings-hot-reload")


async def stop_media_pipeline() -> None:
    global _hot_reload_task
    if _hot_reload_task is not None:
        _hot_reload_task.cancel()
        try:
            await _hot_reload_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _hot_reload_task = None
