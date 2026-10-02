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
    from app import downstream_authority
    from tests.conftest import patch_worker_everywhere

    for name in ("_auto_adopt_playable_candidates_before_mix", "claim_concat_operation", "release_concat_operation",
                 "concatenate_episode", "get_conn", "ConcatOperationConflict", "ConcatOperationInProgress"):
        patch_worker_everywhere(monkeypatch, name, getattr(fake, name))
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
        # 2026-10-01 根因：这个键缺失导致 Command Bus 把"刚受理、还在后台跑"误判
        # 成终态结果缓存 24 小时，用户后续点击全部被缓存拦截重放，处理器再也
        # 不会被调用（见 app/capabilities/bus.py::_is_domain_operation_in_progress）。
        assert result.data["idempotency_in_progress"] is True
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
    assert concat_state.last_error_at("e3") is not None, "mix-status 展示失败时间需要这个时间戳"
    assert fake.released and fake.released[0]["claim_token"] == "claim-1"
    assert fake.released[0]["reason"] == "ffmpeg 合成失败", "release_concat_operation 必须带上失败原因才能落成终态 receipt"


def test_synchronous_runtime_error_releases_claim_before_reraising(monkeypatch) -> None:
    """2026-10-01 生产事故同一类故障：call_guarded 只兜 HTTPException/ArtifactNeedsRebuildError/
    ValueError/KeyError，ffmpeg/ASR 常见的 RuntimeError 会穿透到 concatenate() 里。若不先释放
    claim_concat_operation 已置的 running 租约就重新抛出，这条 receipt 会卡在运行中最长 2 小时。"""
    class _RuntimeFailWorker(_FakeWorker):
        def concatenate_episode(self, episode_id: str, **operation) -> dict:
            raise RuntimeError("ffmpeg 进程被信号杀死")

    fake = _RuntimeFailWorker(); _patch(monkeypatch, fake)

    async def run():
        await delivery_handler.concatenate(I.DeliveryConcatenateInput(episode_id="e5", idempotency_key="k5"))

    try:
        asyncio.run(run())
        raised = None
    except RuntimeError as exc:
        raised = exc

    assert raised is not None and "ffmpeg 进程被信号杀死" in str(raised), "RuntimeError 必须原样重新抛出，不能被吞掉或改写成别的返回"
    assert len(fake.released) == 1, "必须释放 claim_concat_operation 一次，否则 receipt 卡在 running 最长 2 小时租约"
    assert fake.released[0]["claim_token"] == "claim-1"
    assert "RuntimeError" in fake.released[0]["reason"] and "ffmpeg 进程被信号杀死" in fake.released[0]["reason"]


def test_low_priority_helper_never_raises() -> None:
    from app.media_pipeline.delivery_encode import ENCODE_THREADS, low_priority

    low_priority()
    assert ENCODE_THREADS >= 2


def test_repeat_click_after_background_start_reaches_handler_again(monkeypatch) -> None:
    """2026-10-01 生产事故回归：浏览器路由把 background=True 透传给 delivery.concatenate，
    Command Bus 层会把 ACCEPTED 结果缓存 24 小时（``idempotency_key`` 相同即命中）。
    之前 ``_spawn_background``/``in_progress`` 两处 accepted() 都没打
    ``idempotency_in_progress`` 标记，导致缓存把"刚受理"当终态存下——用户同一个
    幂等键的第二次点击被直接拦在 Command Bus，``claim_concat_operation`` 再也不会
    被调用，日志里连一行 CONCAT 记录都没有。这里走真实 Command Bus（含幂等缓存层），
    断言第二次提交仍然会真正重新认领，不是被缓存悄悄吞掉。"""
    from app.capabilities.bus import get_command_bus
    from app.media_exec import concat_state
    from app import task_registry

    fake = _FakeWorker(fail=True)
    _patch(monkeypatch, fake)
    claim_calls: list[dict] = []
    original_claim = fake.claim_concat_operation

    def counting_claim(**kwargs):
        claim_calls.append(kwargs)
        return original_claim(**kwargs)

    fake.claim_concat_operation = counting_claim
    from tests.conftest import patch_worker_everywhere
    patch_worker_everywhere(monkeypatch, "claim_concat_operation", counting_claim)

    bus = get_command_bus()
    body = {"episode_id": "e4", "background": True, "idempotency_key": "concat:e4:same-key"}

    async def run():
        first = await bus.execute_async("delivery.concatenate", dict(body))
        assert first.status == CommandStatus.ACCEPTED
        await task_registry.get(concat_state.TASK_KIND, "e4")
        assert concat_state.in_progress("e4") is False
        second = await bus.execute_async("delivery.concatenate", dict(body))
        return second

    second = asyncio.run(run())
    assert len(claim_calls) == 2, "第二次提交必须重新到达 claim_concat_operation，不能被幂等缓存直接拦截"
    assert second.status == CommandStatus.ACCEPTED
