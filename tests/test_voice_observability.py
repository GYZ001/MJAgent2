"""角色声音生成接入「观测」（工作流运行/步骤）的契约测试。

用户报告点击批量生成配音后观测台什么都看不到——根因是声音生成只是
``task_registry.spawn`` 起的裸后台协程，从未登记成 ``workflow_runs``。本文件
钉住三条入口登记成 ``workflow_type="character_voices"`` 运行后的可观测性契约：

- 批量补齐（``generate_missing_for_project``）：一次运行，每个角色一个
  ``voice_generate`` 步骤，终态按成功/部分失败/全部失败三档落定（对齐
  ``_refs_task`` 的处理方式，见 app.voice.service._bulk_result_message）。
- 定妆后自动生成（``trigger_auto_generate_after_portrait``）：同样一次运行，
  ``trigger_type="auto_after_portrait"``，``parent_run_id`` 指向触发它的定妆
  运行。
- 单角色生成（``generate_voice_for_character_run``，REST/命令入口用）：一次
  运行一个步骤。
- 服务重启/任务取消不留 RUNNING 僵尸运行；REST 响应带 run_id；
  provider_calls 经 ``recorder.step`` 的 ``bind_trace`` 自动挂上 run_id/
  step_run_id（不需要额外接线，本文件用真实查询验证这一点，不只是读代码）。

打桩纪律同 ``tests/test_voice_service.py``：``dispatch.design_voice``/
``dispatch.routing.resolve`` 打模块属性，``check_preview_match``/
``generate_voice_description`` 打在 ``voice_service`` 命名空间上。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db, task_registry
from app.domain.bible_ops import voice_routes
from app.evidence import repository as evidence_repository
from app.orchestration.engine import WorkflowRecorder
from app.voice import service as voice_service
from app.voice.providers import dispatch
from app.voice.providers.base import VoiceDesignResult, VoiceProviderError

from tests.test_voice_service import (
    _character,
    _check_passed,
    _design_voice_ok,
    _fake_description,
    _resolved_model,
    _seed_project,
    _wav_bytes,
)


def _steps_for_run(run_id: str) -> list[dict]:
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT step_key, status, context_manifest_json FROM step_runs "
        "WHERE run_id=? ORDER BY started_at", (run_id,),
    ).fetchall()
    return [
        {
            "step_key": row["step_key"], "status": row["status"],
            "context_manifest": json.loads(row["context_manifest_json"] or "{}"),
        }
        for row in rows
    ]


async def _run_and_drain(coro, project_id: str):
    result = await coro
    task = task_registry.get(voice_service._VOICE_BULK_TASK_KIND, project_id)
    if task is not None:
        await task
    return result


# ---------------------------------------------------------------------------
# 批量补齐：一次运行，每角色一个步骤
# ---------------------------------------------------------------------------


def test_generate_missing_creates_one_run_with_one_step_per_character(monkeypatch) -> None:
    _seed_project([_character("甲"), _character("乙")], project_id="p_obs_bulk")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    _fake_description(monkeypatch)

    accepted, names, run_id = asyncio.run(_run_and_drain(
        voice_service.generate_missing_for_project("p_obs_bulk", triggered_by="tester"), "p_obs_bulk",
    ))

    assert accepted == 2
    assert run_id
    run = evidence_repository.get_run(run_id)
    assert run["workflow_type"] == "character_voices"
    assert run["scope_type"] == "project"
    assert run["scope_id"] == "p_obs_bulk"
    assert run["trigger_type"] == "manual"
    assert run["status"] == "SUCCEEDED"
    steps = _steps_for_run(run_id)
    assert len(steps) == 2
    assert {s["step_key"] for s in steps} == {"voice_generate"}
    assert {s["status"] for s in steps} == {"SUCCEEDED"}
    assert {s["context_manifest"]["character_name"] for s in steps} == set(names)


def test_generate_missing_run_row_exists_before_task_finishes(monkeypatch) -> None:
    """运行记录必须在受理返回时已经落库——观测里立刻可见，不用等第一个角色跑完。"""
    _seed_project([_character("甲")], project_id="p_obs_visible")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    _fake_description(monkeypatch)

    async def _drive():
        accepted, names, run_id = await voice_service.generate_missing_for_project(
            "p_obs_visible", triggered_by="tester",
        )
        # 此刻事件循环还没来得及运行后台任务的第一行（没有任何 await 让出过控制权）。
        run = evidence_repository.get_run(run_id)
        assert run is not None
        assert run["workflow_type"] == "character_voices"
        task = task_registry.get(voice_service._VOICE_BULK_TASK_KIND, "p_obs_visible")
        assert task is not None
        await task
        return run_id

    asyncio.run(_drive())


def test_generate_missing_partial_failure_marks_run_partial_with_message(monkeypatch) -> None:
    _seed_project([_character("甲"), _character("乙")], project_id="p_obs_partial")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _fake_description(monkeypatch)
    _check_passed(monkeypatch)

    async def flaky_design_voice(req, *, purpose, call_meta=None):
        if (call_meta or {}).get("character_name") == "乙":
            raise VoiceProviderError("供应商额度不足", failure_kind="insufficient_balance")
        return VoiceDesignResult(
            provider_voice_id="prov_ok", audio=_wav_bytes(), audio_format="wav",
            sample_rate=24000, request_id="req_ok", latency_ms=100,
        )

    monkeypatch.setattr(dispatch, "design_voice", flaky_design_voice)

    accepted, names, run_id = asyncio.run(_run_and_drain(
        voice_service.generate_missing_for_project("p_obs_partial", triggered_by="tester"), "p_obs_partial",
    ))

    assert accepted == 2
    run = evidence_repository.get_run(run_id)
    assert run["status"] == "PARTIAL"
    assert "成功 1 个、失败 1 个" in run["failure_message"]
    assert "乙" in run["failure_message"]
    steps = _steps_for_run(run_id)
    assert {s["status"] for s in steps} == {"SUCCEEDED", "FAILED"}


def test_generate_missing_all_failed_marks_run_failed_with_message(monkeypatch) -> None:
    """全部角色都失败时（例如供应商密钥失效）运行必须落 FAILED，不能假装成功——
    本仓库已因「批量结果全绿掩盖真实故障」吃过亏（图像密钥失效一周无人察觉）。"""
    _seed_project([_character("丙")], project_id="p_obs_allfail")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _fake_description(monkeypatch)

    async def always_fail(req, *, purpose, call_meta=None):
        raise VoiceProviderError("供应商拒绝了本次请求", failure_kind="content_rejected")

    monkeypatch.setattr(dispatch, "design_voice", always_fail)

    accepted, names, run_id = asyncio.run(_run_and_drain(
        voice_service.generate_missing_for_project("p_obs_allfail", triggered_by="tester"), "p_obs_allfail",
    ))

    assert accepted == 1
    run = evidence_repository.get_run(run_id)
    assert run["status"] == "FAILED"
    assert run["failure_code"] == "ALL_CHARACTERS_FAILED"
    assert "失败 1 个" in run["failure_message"]


def test_generate_missing_task_cancelled_mid_flight_does_not_leave_running(monkeypatch) -> None:
    """服务重启/任务取消时不能留下永远 RUNNING 的僵尸运行——角色固定音色没有
    自动续跑，必须直接终态化为 CANCELLED（不是挂在 PAUSED_EXTERNAL 里等一个
    永远不会发生的自动恢复）。"""
    _seed_project([_character("甲")], project_id="p_obs_cancel")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _fake_description(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_design_voice(req, *, purpose, call_meta=None):
        started.set()
        await release.wait()
        raise AssertionError("不应该跑到这里——测试在 release 之前就取消了任务")

    monkeypatch.setattr(dispatch, "design_voice", blocking_design_voice)

    async def _drive():
        recorder = WorkflowRecorder.create(
            workflow_type="character_voices", scope_type="project", scope_id="p_obs_cancel",
            input_fingerprint="fp-test-cancel-mid-flight", requested_by="tester", trigger_type="manual",
        )
        task = asyncio.ensure_future(
            voice_service._generate_missing_task(
                "p_obs_cancel", ["甲"], triggered_by="tester", recorder=recorder,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return recorder.run_id

    run_id = asyncio.run(_drive())
    run = evidence_repository.get_run(run_id)
    assert run["status"] == "CANCELLED"
    assert run["status"] != "RUNNING"


# ---------------------------------------------------------------------------
# 定妆后自动生成：parent_run_id 与 trigger_type
# ---------------------------------------------------------------------------


def test_trigger_auto_generate_after_portrait_sets_parent_run_id_and_trigger_type(monkeypatch) -> None:
    _seed_project([_character("辛"), _character("壬")], project_id="p_obs_hook")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    monkeypatch.setattr(voice_service, "voice_auto_generate_enabled", lambda: True)
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    _fake_description(monkeypatch)
    parent = WorkflowRecorder.create(
        workflow_type="character_references", scope_type="project", scope_id="p_obs_hook",
        input_fingerprint="fp-parent-for-voice-hook-test", requested_by="user", trigger_type="manual",
    )

    async def _drive():
        voice_service.trigger_auto_generate_after_portrait(
            "p_obs_hook", ["辛", "壬"], parent_run_id=parent.run_id,
        )
        task = task_registry.get(voice_service._VOICE_BULK_TASK_KIND, "p_obs_hook")
        assert task is not None
        await task

    asyncio.run(_drive())

    row = db.get_conn().execute(
        "SELECT id FROM workflow_runs WHERE workflow_type='character_voices' AND scope_id=?",
        ("p_obs_hook",),
    ).fetchone()
    assert row is not None
    run = evidence_repository.get_run(row["id"])
    assert run["parent_run_id"] == parent.run_id
    assert run["trigger_type"] == "auto_after_portrait"
    assert run["requested_by"] == "system"


# ---------------------------------------------------------------------------
# 单角色生成：一次运行一个步骤
# ---------------------------------------------------------------------------


def test_generate_voice_for_character_run_creates_one_run_one_step(monkeypatch) -> None:
    _seed_project([_character("单角色")], project_id="p_obs_single")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)

    voice, run_id = asyncio.run(voice_service.generate_voice_for_character_run(
        "p_obs_single", "单角色", voice_prompt="a", preview_text="试听台词", created_by="tester",
    ))

    assert voice["status"] == "current"
    run = evidence_repository.get_run(run_id)
    assert run["workflow_type"] == "character_voices"
    assert run["status"] == "SUCCEEDED"
    steps = _steps_for_run(run_id)
    assert len(steps) == 1
    assert steps[0]["step_key"] == "voice_generate"
    assert steps[0]["status"] == "SUCCEEDED"
    assert steps[0]["context_manifest"]["character_name"] == "单角色"


def test_generate_voice_for_character_run_marks_run_failed_on_lookup_error(monkeypatch) -> None:
    _seed_project([_character("存在的")], project_id="p_obs_single_404")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())

    with pytest.raises(voice_service.VoiceLookupError):
        asyncio.run(voice_service.generate_voice_for_character_run(
            "p_obs_single_404", "不存在的角色", voice_prompt="a", preview_text="b", created_by="tester",
        ))

    row = db.get_conn().execute(
        "SELECT id FROM workflow_runs WHERE workflow_type='character_voices' AND scope_id=?",
        ("p_obs_single_404",),
    ).fetchone()
    assert row is not None
    run = evidence_repository.get_run(row["id"])
    assert run["status"] == "FAILED"


# ---------------------------------------------------------------------------
# REST 响应带 run_id
# ---------------------------------------------------------------------------


def test_generate_character_voice_route_response_includes_run_id(monkeypatch) -> None:
    _seed_project([_character("路由角色")], project_id="p_obs_route1")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)

    result = asyncio.run(voice_routes.generate_character_voice(
        "p_obs_route1", "路由角色",
        {"voice_prompt": "a", "preview_text": "试听台词", "idempotency_key": "k1"},
    ))

    assert result["run_id"]
    assert evidence_repository.get_run(result["run_id"])["workflow_type"] == "character_voices"


def test_generate_missing_voices_route_response_includes_run_id(monkeypatch) -> None:
    _seed_project([_character("批量路由甲")], project_id="p_obs_route2")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    _fake_description(monkeypatch)

    async def _drive():
        result = await voice_routes.generate_missing_voices("p_obs_route2")
        task = task_registry.get(voice_service._VOICE_BULK_TASK_KIND, "p_obs_route2")
        if task is not None:
            await task
        return result

    result = asyncio.run(_drive())
    assert result["run_id"]
    assert evidence_repository.get_run(result["run_id"])["workflow_type"] == "character_voices"


def test_generate_missing_voices_route_run_id_null_when_nothing_to_do(monkeypatch) -> None:
    _seed_project([], project_id="p_obs_route3")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())

    result = asyncio.run(voice_routes.generate_missing_voices("p_obs_route3"))

    # 直接调用路由函数会真的走一遍 Command Bus，响应是领域字段的超集（见
    # tests/test_voice_routes.py 模块 docstring）；按包含断言，不按整体相等。
    assert result["accepted"] == 0
    assert result["characters"] == []
    assert result["run_id"] is None


# ---------------------------------------------------------------------------
# provider_calls 挂 run_id（经 recorder.step 的 bind_trace）
# ---------------------------------------------------------------------------


def test_provider_calls_carry_run_id_for_voice_design(monkeypatch) -> None:
    """打桩要打在适配器层（``qwen_voice_design.design_voice``），不能像
    ``_design_voice_ok`` 那样直接替换 ``dispatch.design_voice`` 本身——那个函数
    才是真正调用 ``hiagent.log_provider_call`` 记 ``provider_calls`` 行的地方，
    换掉它整个函数会连带把记账逻辑一起换掉，测不出 bind_trace 是否生效。"""
    from app.voice.providers import qwen_voice_design

    _seed_project([_character("追踪角色")], project_id="p_obs_trace")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())

    async def fake_adapter_design_voice(conn, req, *, client=None):
        return VoiceDesignResult(
            provider_voice_id="prov_trace", audio=_wav_bytes(), audio_format="wav",
            sample_rate=24000, request_id="req_trace", latency_ms=80,
        )

    monkeypatch.setattr(qwen_voice_design, "design_voice", fake_adapter_design_voice)
    _check_passed(monkeypatch)

    voice, run_id = asyncio.run(voice_service.generate_voice_for_character_run(
        "p_obs_trace", "追踪角色", voice_prompt="a", preview_text="试听台词", created_by="tester",
    ))

    rows = db.get_conn().execute(
        "SELECT run_id, step_run_id FROM provider_calls WHERE kind='voice_design' AND run_id=?",
        (run_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["step_run_id"]
