"""整集合成：处理器在线程里跑、浏览器路由后台受理立即返回（2026-09-15 实测合成 122 秒期间整个后端冻结）。"""
from __future__ import annotations

import asyncio
import threading

from app.capabilities import inputs as I
from app.capabilities.handlers import delivery as delivery_handler
from app.capabilities.schemas import CommandStatus
from app.media_exec import concat_state


class _FakeWorker:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.released: list[dict] = []
        self.thread_names: list[str] = []
        self.get_conn = lambda: None

    def _auto_adopt_playable_candidates_before_mix(self, episode_id: str) -> None:
        return None

    def claim_concat_operation(self, **kwargs):
        return "claim-1", None

    def release_concat_operation(self, **kwargs) -> None:
        self.released.append(kwargs)

    def concatenate_episode(self, episode_id: str, **operation) -> dict:
        self.thread_names.append(threading.current_thread().name)
        if self.fail:
            raise ValueError("ffmpeg 合成失败")
        return {"shots": 17, "total_duration_s": 255.8}

    class ConcatOperationConflict(Exception):
        pass

    class ConcatOperationInProgress(Exception):
        pass


def _patch(monkeypatch, fake: _FakeWorker) -> None:
    from app import worker
    from app import downstream_authority

    for name in ("_auto_adopt_playable_candidates_before_mix", "claim_concat_operation", "release_concat_operation",
                 "concatenate_episode", "get_conn", "ConcatOperationConflict", "ConcatOperationInProgress"):
        monkeypatch.setattr(worker, name, getattr(fake, name))
    monkeypatch.setattr(downstream_authority, "verify_current_storyboard_release_authority", lambda episode_id, conn=None: {"r": 1})
    monkeypatch.setattr(downstream_authority, "current_partial_adopted_video_delivery_manifest", lambda episode_id, conn=None: {"items": []})


def test_synchronous_call_runs_concat_off_the_event_loop(monkeypatch) -> None:
    fake = _FakeWorker(); _patch(monkeypatch, fake)
    result = asyncio.run(delivery_handler.concatenate(I.DeliveryConcatenateInput(episode_id="e", idempotency_key="k1")))
    assert result.status == CommandStatus.SUCCEEDED and result.data["shots"] == 17
    assert fake.thread_names and fake.thread_names[0] != threading.main_thread().name


def test_background_mode_returns_accepted_and_finishes_later(monkeypatch) -> None:
    fake = _FakeWorker(); _patch(monkeypatch, fake)

    async def run():
        result = await delivery_handler.concatenate(I.DeliveryConcatenateInput(episode_id="e2", idempotency_key="k2", background=True))
        assert result.status == CommandStatus.ACCEPTED and result.data["concat_in_progress"] is True
        assert concat_state.in_progress("e2") is True
        from app import task_registry
        await task_registry.get(concat_state.TASK_KIND, "e2")
        return result

    asyncio.run(run())
    assert concat_state.in_progress("e2") is False and concat_state.last_error("e2") is None
    assert fake.released == []


def test_background_failure_releases_claim_and_records_error(monkeypatch) -> None:
    fake = _FakeWorker(fail=True); _patch(monkeypatch, fake)

    async def run():
        result = await delivery_handler.concatenate(I.DeliveryConcatenateInput(episode_id="e3", idempotency_key="k3", background=True))
        assert result.status == CommandStatus.ACCEPTED
        from app import task_registry
        await task_registry.get(concat_state.TASK_KIND, "e3")

    asyncio.run(run())
    assert concat_state.in_progress("e3") is False
    assert "ffmpeg 合成失败" in (concat_state.last_error("e3") or "")
    assert fake.released and fake.released[0]["claim_token"] == "claim-1"


def test_low_priority_helper_never_raises() -> None:
    from app.media_pipeline.delivery_encode import ENCODE_THREADS, low_priority

    low_priority()
    assert ENCODE_THREADS >= 2
