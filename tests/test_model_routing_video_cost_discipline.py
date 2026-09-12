"""视频换路费用纪律：EP-05 第三阶段。

客户端超时不等于供应商侧失败——那边可能正在真实出片，自动换路重发就是重复
计费（CLAUDE.md「文本免费但视频有额度」，PRD EP-05 §6 第 3 条硬约束）。
``app.models_registry.video_confirmation.confirm_video_terminal_failure`` 是
``routing.call_with_failover`` 的 ``confirm_terminal_failure`` 回调的真实实现：
按 task_id 向供应商适配器发起一次真实轮询，只有轮询本身成功返回、且状态明确
是 ``"failed"`` 才认定终态失败、允许换路。

拆成独立文件（而不是塞进 ``tests/test_model_routing_failover.py``）：那个文件
只查行数一维的测试文件基线（``app/FILE_CONVENTIONS.toml``），加上这组用例会
把它顶过 500 行的新增文件严格上限——拆分本身就是正确做法，不是绕过基线。
"""
from __future__ import annotations

import json

import pytest

from app.db import get_conn, set_setting
from app.models_registry import bindings, routing


class _FakeProviderError(Exception):
    def __init__(self, failure_kind: str = "", failure_category: str = "", timeout_phase: str | None = None) -> None:
        super().__init__(failure_kind or "fake")
        self.failure_kind = failure_kind
        self.failure_category = failure_category
        self.timeout_phase = timeout_phase


def _audit_failover_rows() -> list:
    return get_conn().execute(
        "SELECT * FROM operation_audit WHERE event='models_registry.route_failover' ORDER BY ts"
    ).fetchall()


class _FakeVideoAdapter:
    def __init__(self, status: str | None, *, raise_on_poll: bool = False) -> None:
        self._status = status
        self._raise_on_poll = raise_on_poll
        self.poll_calls = 0

    async def poll_video_task(self, task_id: str, *, call_meta: dict | None = None) -> dict:
        self.poll_calls += 1
        if self._raise_on_poll:
            raise RuntimeError("轮询本身失败（网络抖动）")
        return {"status": self._status}


async def test_confirm_video_terminal_failure_false_when_supplier_still_running(monkeypatch) -> None:
    from app import video_providers
    from app.models_registry.video_confirmation import confirm_video_terminal_failure

    fake = _FakeVideoAdapter("running")
    monkeypatch.setattr(video_providers, "resolve", lambda provider: fake)

    assert await confirm_video_terminal_failure("custom:vid_a", "task-1") is False
    assert fake.poll_calls == 1


async def test_confirm_video_terminal_failure_true_when_supplier_confirms_failed(monkeypatch) -> None:
    from app import video_providers
    from app.models_registry.video_confirmation import confirm_video_terminal_failure

    fake = _FakeVideoAdapter("failed")
    monkeypatch.setattr(video_providers, "resolve", lambda provider: fake)

    assert await confirm_video_terminal_failure("custom:vid_a", "task-1") is True


async def test_confirm_video_terminal_failure_fail_closed_when_confirmation_poll_itself_errors(monkeypatch) -> None:
    """确认轮询本身也失败（网络抖动）：fail-closed，不允许换路，不是"顺便当失败处理"。"""
    from app import video_providers
    from app.models_registry.video_confirmation import confirm_video_terminal_failure

    fake = _FakeVideoAdapter(None, raise_on_poll=True)
    monkeypatch.setattr(video_providers, "resolve", lambda provider: fake)

    assert await confirm_video_terminal_failure("custom:vid_a", "task-1") is False


async def test_confirm_video_terminal_failure_false_for_empty_task_id() -> None:
    from app.models_registry.video_confirmation import confirm_video_terminal_failure

    assert await confirm_video_terminal_failure("custom:vid_a", "") is False


async def test_video_client_timeout_with_supplier_still_running_does_not_duplicate_provider_call(
    monkeypatch,
) -> None:
    """端到端费用纪律：客户端侧 timeout，但向供应商真实确认发现任务仍在跑
    （非终态）——call_with_failover 必须原样抛出，断言没有产生第二次真实
    供应商调用（不重复计费）。这是本阶段"视频换路费用纪律"的验收用例。"""
    set_setting("custom_models", json.dumps([
        {"id": "vid_a", "provider": "custom:vid_a", "model": "video-a", "kinds": ["video"],
         "builtin": False, "protocol": "seedance", "base_url": "https://va.example.test/v1"},
        {"id": "vid_b", "provider": "custom:vid_b", "model": "video-b", "kinds": ["video"],
         "builtin": False, "protocol": "seedance", "base_url": "https://vb.example.test/v1"},
    ], ensure_ascii=False))
    bindings.upsert_binding(purpose="video:shot", model_id="vid_a", priority=0)
    bindings.upsert_binding(purpose="video:shot", model_id="vid_b", priority=1)

    from app import video_providers
    from app.models_registry.video_confirmation import confirm_video_terminal_failure

    fake_adapter = _FakeVideoAdapter("running")  # 供应商侧仍在跑，非终态失败
    monkeypatch.setattr(video_providers, "resolve", lambda provider: fake_adapter)

    calls: list[str] = []

    async def fn(candidate: routing.ResolvedModel) -> str:
        calls.append(candidate.model_id)
        raise _FakeProviderError(timeout_phase="read")  # 客户端侧 timeout

    async def confirm() -> bool:
        return await confirm_video_terminal_failure("custom:vid_a", "task-xyz")

    with pytest.raises(_FakeProviderError):
        await routing.call_with_failover(
            "video:shot", fn, request_id="req-cost-1", confirm_terminal_failure=confirm,
        )

    assert calls == ["vid_a"]  # 没有第二次真实供应商调用
    assert fake_adapter.poll_calls == 1  # 确认轮询发生过一次，但据此判定不允许换路
    assert _audit_failover_rows() == []


async def test_video_confirmed_terminal_failure_allows_reroute_and_only_then_calls_next_provider(
    monkeypatch,
) -> None:
    """对照组：供应商真实确认任务已终态失败时，换路重发才允许发生。"""
    set_setting("custom_models", json.dumps([
        {"id": "vid_a", "provider": "custom:vid_a", "model": "video-a", "kinds": ["video"],
         "builtin": False, "protocol": "seedance", "base_url": "https://va.example.test/v1"},
        {"id": "vid_b", "provider": "custom:vid_b", "model": "video-b", "kinds": ["video"],
         "builtin": False, "protocol": "seedance", "base_url": "https://vb.example.test/v1"},
    ], ensure_ascii=False))
    bindings.upsert_binding(purpose="video:shot", model_id="vid_a", priority=0)
    bindings.upsert_binding(purpose="video:shot", model_id="vid_b", priority=1)

    from app import video_providers
    from app.models_registry.video_confirmation import confirm_video_terminal_failure

    fake_adapter = _FakeVideoAdapter("failed")  # 供应商侧确已终态失败
    monkeypatch.setattr(video_providers, "resolve", lambda provider: fake_adapter)

    calls: list[str] = []

    async def fn(candidate: routing.ResolvedModel) -> str:
        calls.append(candidate.model_id)
        if candidate.model_id == "vid_a":
            raise _FakeProviderError(timeout_phase="read")
        return "done"

    async def confirm() -> bool:
        return await confirm_video_terminal_failure("custom:vid_a", "task-xyz")

    result = await routing.call_with_failover(
        "video:shot", fn, request_id="req-cost-2", confirm_terminal_failure=confirm,
    )

    assert result == "done"
    assert calls == ["vid_a", "vid_b"]
    assert len(_audit_failover_rows()) == 1
