"""带意见重拍（09-23 方案 P0-4）：锁住 critique 从命令输入到 worker.enqueue_shot 的转发。

`critique` 是「追加在段落末尾、不改台词、不回写分镜」的一次性通道，已在
`app/media_exec/enqueue.py`/`enqueue_prompt.py` 落地并有专门测试覆盖（见
`tests/test_subtitle_directed_retake.py`）。本文件只锁上游两层此前缺失的转发：
`app.capabilities.inputs.VideoGenerateShotInput` -> `app.capabilities.handlers.video`
-> `app.domain.video_ops.generate._generate_shot_core` -> `worker.enqueue_shot`，
防止将来有人加字段却漏接下游某一层（这类漏接此前在同一份代码上真实发生过：
`with_critique` 参数在前端存在多年但后端从未读取）。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.capabilities import inputs as I
from app.capabilities.handlers import video as video_handlers
from app.domain.video_ops import generate as generate_mod


class _FakeShotPlanItem:
    def __init__(self, shot_id: str) -> None:
        self.shot_id = shot_id
        self.depends_on_shot_id = None


class _FakePlan:
    def __init__(self, shot_id: str) -> None:
        self.shots = [_FakeShotPlanItem(shot_id)]


class _FakeConn:
    """只回答 `_generate_shot_core` 真正会问的那一条 SELECT，其余协作对象另外打桩。"""

    def __init__(self, shot_row: dict) -> None:
        self._shot_row = shot_row

    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return self._shot_row


def _wire_generate_shot_core_stubs(monkeypatch, shot_id: str, episode_id: str, captured: dict) -> None:
    """把生成计划/校验/入队四层协作对象换成最小可控替身，只留 critique 的转发路径可观察。"""
    monkeypatch.setattr(generate_mod, "get_conn", lambda: _FakeConn({"id": shot_id, "episode_id": episode_id}))
    monkeypatch.setattr(generate_mod, "_assert_shot_generation_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(generate_mod, "_review_assert_shot_positive", lambda *_a, **_k: {"qualified": True})

    import app.video_plan as video_plan_mod

    async def _fake_generate_episode_plan(*_a, **_k):
        return _FakePlan(shot_id)

    monkeypatch.setattr(video_plan_mod, "generate_episode_plan", _fake_generate_episode_plan)
    monkeypatch.setattr(
        video_plan_mod, "create_local_replan_revision", lambda *_a, **_k: _FakePlan(shot_id),
    )

    def _capture_enqueue_shot(shot_id_arg, **kwargs):
        captured["shot_id"] = shot_id_arg
        captured.update(kwargs)
        return {"reused": False, "job_id": "job_1", "task_accepted": True}

    monkeypatch.setattr(generate_mod.worker, "enqueue_shot", _capture_enqueue_shot)


@pytest.mark.asyncio
async def test_generate_shot_core_forwards_critique_to_enqueue_shot(monkeypatch) -> None:
    shot_id, episode_id = "shot_1", "ep_1"
    captured: dict = {}
    _wire_generate_shot_core_stubs(monkeypatch, shot_id, episode_id, captured)

    result = await generate_mod._generate_shot_core(shot_id, {"critique": ["把光线调暗一点"]})

    assert result == {"reused": False, "job_id": "job_1", "task_accepted": True}
    assert captured["critique"] == ["把光线调暗一点"]
    assert captured["prompt_override"] is None
    assert captured["reroll"] is False


@pytest.mark.asyncio
async def test_generate_shot_core_without_critique_is_unchanged(monkeypatch) -> None:
    """回归：不带意见的既有请求（无 reroll/override/critique）不触发本地重排计划、critique 转发为 None。"""
    shot_id, episode_id = "shot_2", "ep_1"
    captured: dict = {}
    _wire_generate_shot_core_stubs(monkeypatch, shot_id, episode_id, captured)

    def _fail_if_called(*_a, **_k):
        raise AssertionError("未带意见/重抽/覆盖时不应触发本地重排计划")

    import app.video_plan as video_plan_mod
    monkeypatch.setattr(video_plan_mod, "create_local_replan_revision", _fail_if_called)

    result = await generate_mod._generate_shot_core(shot_id, {})

    assert result == {"reused": False, "job_id": "job_1", "task_accepted": True}
    assert captured["critique"] is None
    assert captured["reroll"] is False
    assert captured["prompt_override"] is None


@pytest.mark.asyncio
async def test_generate_shot_handler_forwards_critique_end_to_end(monkeypatch) -> None:
    """Command Handler 层（`video.generate_shot`）把 `args.critique` 一路转发到 `worker.enqueue_shot`。"""
    shot_id, episode_id = "shot_3", "ep_1"
    captured: dict = {}
    _wire_generate_shot_core_stubs(monkeypatch, shot_id, episode_id, captured)

    import app.video_command_operations as operations
    op_conn = sqlite3.connect(":memory:")
    op_conn.row_factory = sqlite3.Row
    monkeypatch.setattr(operations, "get_conn", lambda: op_conn)

    args = I.VideoGenerateShotInput(
        shot_id=shot_id,
        critique=["把光线调暗一点", "  ", "手指五根"],
        idempotency_key="idem-critique-1",
    )
    result = await video_handlers.generate_shot(args)

    assert result.status == "succeeded"
    assert captured["critique"] == ["把光线调暗一点", "  ", "手指五根"]
