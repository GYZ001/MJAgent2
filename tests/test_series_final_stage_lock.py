"""连播台成片步骤：同一集在进程内串行，后到者不重复渲染。

补跑任务与主任务同时覆盖一集时（第 24 集实测），两个任务各自等到该集空闲后
几乎同时进入成片台，各自渲染一遍、再争同一个发布租约。锁内串行后，后到者
拿到锁时先看判据：前者已发布就直接返回。
"""
from __future__ import annotations

import asyncio
import time

from app.domain.series_ops import stages
from tests.conftest import patch_worker_everywhere


def test_concurrent_final_runs_for_same_episode_render_once(monkeypatch) -> None:
    calls: list[str] = []
    published = {"done": False}

    def fake_concat(episode_id: str) -> dict:
        calls.append(episode_id)
        time.sleep(0.05)
        published["done"] = True
        return {}

    patch_worker_everywhere(monkeypatch, "concatenate_episode", fake_concat)
    monkeypatch.setattr(stages, "get_conn", lambda: None)
    monkeypatch.setattr(stages, "final_complete", lambda conn, episode_id: published["done"])

    async def run() -> None:
        await asyncio.gather(stages._run_final("ep_lock_once"), stages._run_final("ep_lock_once"))

    asyncio.run(run())
    assert calls == ["ep_lock_once"]


def test_final_runs_for_different_episodes_do_not_serialize(monkeypatch) -> None:
    calls: list[str] = []

    def fake_concat(episode_id: str) -> dict:
        calls.append(episode_id)
        time.sleep(0.05)
        return {}

    patch_worker_everywhere(monkeypatch, "concatenate_episode", fake_concat)
    monkeypatch.setattr(stages, "get_conn", lambda: None)
    monkeypatch.setattr(stages, "final_complete", lambda conn, episode_id: False)

    async def run() -> None:
        await asyncio.gather(stages._run_final("ep_lock_a"), stages._run_final("ep_lock_b"))

    started = time.perf_counter()
    asyncio.run(run())
    assert sorted(calls) == ["ep_lock_a", "ep_lock_b"]
    assert time.perf_counter() - started < 0.09, "不同集不该互相串行"
