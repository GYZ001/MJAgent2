"""付费后写点命中依赖围栏：继续轮询到底、落盘为可人工采纳的 waiting_human，
不再直接判 failed 丢弃已花费的供应商结果（2026-10-01 协调方追加修复）。

根因与修法见 app/media_exec/run_job_steps.py 的
``review_fence_capture``/``downgrade_to_waiting_human_if_stale``/
``fence_or_downgrade`` 三个新函数文档：``provider_poll``/``candidate``/
``candidate_evidence``/``adoption_relation`` 四个写点命中后不再 raise，而是
把已经落盘的 succeeded 结果降级为 waiting_human；``worker_start``/
``provider_input_adoption``/``provider_submit`` 三个预付费写点行为不变——
直接 failed，不调用供应商、不花额度。

夹具复用 ``tests/test_video_provider_content_rejection_skip.py`` 的
``_seed_job``（已接单待轮询的 job/version/shot/episode）。
"""
from __future__ import annotations

import asyncio
import json

from app import worker
from app.compiler import shot_cost_cny
from app.db import get_conn
from app.domain.video_ops.adopt import _assert_version_adoptable
from app.media_exec.fences import ReviewDependencyFence
from tests.conftest import patch_worker_everywhere
from tests.test_video_provider_content_rejection_skip import _seed_job

STALE_DETAIL = json.dumps({
    "code": "REVIEW_DEPENDENCY_STALE", "write_point": "provider_poll",
    "expected_qualification_version": "a:1", "current_qualification_version": "b:2",
    "blockers": [],
}, ensure_ascii=False)


def _wire_success_poll(monkeypatch, *, settled: list) -> None:
    """照抄同目录 ``_wire`` 的桩法，但不吞掉 ``settle_budget``——本文件要核实
    预算按已产生费用结算，不按失败退还。"""
    from app.media_pipeline import concurrency, stage_state

    class Permit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def no_sleep(_delay: float) -> None:
        return None

    async def poll_succeeded(task_id, *, call_meta=None):
        return {
            "status": "succeeded", "video_url": "https://provider.invalid/v.mp4",
            "last_frame_url": "", "error": "", "failure": None,
        }

    async def download(url, dest):
        from pathlib import Path
        Path(dest).write_bytes(b"fake-provider-video")

    def capture_settle(job_id, cost, *, success):
        settled.append((job_id, cost, success))

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(worker.hiagent, "poll_video_task", poll_succeeded)
    monkeypatch.setattr(worker.hiagent, "download", download)
    patch_worker_everywhere(monkeypatch, "_assert_job_lease", lambda *_a, **_k: None)
    monkeypatch.setattr(concurrency, "semaphore_for", lambda _r: Permit())
    monkeypatch.setattr(concurrency, "report_congestion", lambda *_a, **_k: None)
    monkeypatch.setattr(concurrency, "report_healthy", lambda *_a, **_k: None)
    monkeypatch.setattr(stage_state, "set_pipeline_stage", lambda *_a, **_k: None)
    monkeypatch.setattr(worker.media_scheduler, "renew_lease", lambda *_a, **_k: True)
    monkeypatch.setattr(worker.media_scheduler, "settle_budget", capture_settle)
    patch_worker_everywhere(monkeypatch, "mark_media_job_state", lambda *_a, **_k: None)
    patch_worker_everywhere(monkeypatch, "reconcile_episode_generation_status", lambda *_a, **_k: None)


def _fence_trips_only_at(target_write_point: str):
    async def fence(_job, _version_id, write_point):
        if write_point == target_write_point:
            raise ReviewDependencyFence(STALE_DETAIL)
    return fence


def _assert_landed_waiting_human(conn, settled: list) -> None:
    """四个付费后写点命中围栏，最终必须落到同一个可核验状态——版本/任务都是
    waiting_human、视频文件保留、详情留痕、预算按已产生费用结算、人工采纳
    入口接受。调用方已经跑过 ``worker._run_job``。"""
    job = conn.execute("SELECT status, error FROM jobs WHERE id='j1'").fetchone()
    version = conn.execute(
        "SELECT status, error, video_path FROM shot_versions WHERE id='v1'"
    ).fetchone()
    assert job["status"] == "waiting_human", "命中围栏的付费后写点不得直接判 failed"
    assert version["status"] == "waiting_human"
    assert version["video_path"], "供应商任务已接单，必须照常轮询到底、把视频落盘"
    assert "REVIEW_DEPENDENCY_STALE_AFTER_SUBMIT" in version["error"]
    assert "人工核对后采纳" in version["error"]
    assert "REVIEW_DEPENDENCY_STALE" in version["error"], "依赖变化详情必须留痕，不能只给一句空话"
    # 预算按已产生费用结算，不按失败退还。
    assert settled == [("j1", shot_cost_cny(5), True)]
    # 人工采纳入口必须接受这个版本——界面给出的出路必须是真的。
    version_row = dict(conn.execute("SELECT * FROM shot_versions WHERE id='v1'").fetchone())
    assert _assert_version_adoptable(version_row, {"human_override": True}) is True


def _seed_adoptable_job(conn) -> None:
    _seed_job(conn)
    # _seed_job 不摆 video_slot_active/provider_result_adoptable（默认 0/1，
    # 代表"槽位已被兄弟任务顶替"场景）：这组测试要验证的是槽位本来就是本任务
    # 的、若无依赖过期就会正常采用的情形，必须显式置 1。
    conn.execute("UPDATE jobs SET video_slot_active=1, provider_result_adoptable=1 WHERE id='j1'")
    conn.commit()


def _wire_qa_pass(monkeypatch) -> None:
    """candidate_evidence/adoption_relation 两个写点要先跑完 QA 闸门（及更晚的
    技术校验）才会被检查到，照抄 ``tests/test_video_subtitle_gate.py`` 的桩法：
    两个窄问题闸门本身不是本测试要验证的东西，桩成无副作用的直接通过。"""
    from app.media_exec import character_count_gate, subtitle_gate

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(subtitle_gate, "evaluate_version", noop)
    monkeypatch.setattr(character_count_gate, "evaluate_version", noop)


def _wire_technical_pass(monkeypatch, calls: list) -> None:
    """``record_video_candidate`` 真实实现会读本地视频文件算容器/时长；测试用
    的是假字节文件，直接桩成"技术校验通过"，只验证围栏接线，不重复测技术校验
    本身（那是 record_video_candidate 自己模块的职责）。"""
    from app.evidence import media as media_evidence

    def fake_record(version_id, **_kwargs):
        calls.append(version_id)
        get_conn().execute(
            "UPDATE shot_versions SET technical_validation_json=? WHERE id=?",
            (json.dumps({"passed": True, "issues": []}), version_id),
        )
        get_conn().commit()
        return {"id": "art-v1"}

    monkeypatch.setattr(media_evidence, "record_video_candidate", fake_record)


def test_provider_poll_fence_polls_to_completion_and_lands_waiting_human(monkeypatch) -> None:
    conn = get_conn()
    _seed_adoptable_job(conn)
    settled: list = []
    _wire_success_poll(monkeypatch, settled=settled)
    patch_worker_everywhere(
        monkeypatch, "_assert_review_dependency_fence_async", _fence_trips_only_at("provider_poll"),
    )

    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))

    _assert_landed_waiting_human(conn, settled)


def test_candidate_fence_after_download_lands_waiting_human(monkeypatch) -> None:
    """``candidate`` 写点在下载之后、checkpoint 落盘之前；命中同样不得丢弃已经
    下载好的视频文件。"""
    conn = get_conn()
    _seed_adoptable_job(conn)
    settled: list = []
    _wire_success_poll(monkeypatch, settled=settled)
    patch_worker_everywhere(
        monkeypatch, "_assert_review_dependency_fence_async", _fence_trips_only_at("candidate"),
    )

    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))

    _assert_landed_waiting_human(conn, settled)


def test_candidate_evidence_fence_after_qa_lands_waiting_human(monkeypatch) -> None:
    """``candidate_evidence`` 写点在 QA 闸门之后：checkpoint 已经把版本落成
    succeeded，命中围栏必须把它降级为 waiting_human，不是维持 succeeded 也不是
    直接判 failed。"""
    conn = get_conn()
    _seed_adoptable_job(conn)
    settled: list = []
    _wire_success_poll(monkeypatch, settled=settled)
    _wire_qa_pass(monkeypatch)
    record_calls: list = []
    from app.evidence import media as media_evidence

    def forbid_record(*_a, **_k):
        raise AssertionError("candidate_evidence 命中围栏时不得继续走到 record_video_candidate")

    monkeypatch.setattr(media_evidence, "record_video_candidate", forbid_record)
    patch_worker_everywhere(
        monkeypatch, "_assert_review_dependency_fence_async", _fence_trips_only_at("candidate_evidence"),
    )

    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))

    assert record_calls == []
    _assert_landed_waiting_human(conn, settled)


def test_adoption_relation_fence_after_technical_pass_lands_waiting_human(monkeypatch) -> None:
    """``adoption_relation`` 写点在技术校验通过之后、真正建立采纳关系之前——
    这是最晚的付费后写点，命中时版本已经通过了全部质检，仍必须降级而不是
    丢弃或悄悄放行采纳。"""
    conn = get_conn()
    _seed_adoptable_job(conn)
    settled: list = []
    _wire_success_poll(monkeypatch, settled=settled)
    _wire_qa_pass(monkeypatch)
    record_calls: list = []
    _wire_technical_pass(monkeypatch, record_calls)
    patch_worker_everywhere(
        monkeypatch, "_assert_review_dependency_fence_async", _fence_trips_only_at("adoption_relation"),
    )

    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))

    assert record_calls == ["v1"], "必须真的跑到技术校验这一步，不是提前被别的分支拦住"
    _assert_landed_waiting_human(conn, settled)


def test_worker_start_fence_still_fails_closed_without_calling_provider(monkeypatch) -> None:
    conn = get_conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES('e1','p1',1,'generating',1)"
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,transition
           ) VALUES('s1','e1',1,5,'中景','固定','室内','[]','人物站定','[]','硬切')"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('v1','s1',1,'prompt','idem','running','{}',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               lease_owner,lease_expires_at,provider_create_state,
               provider_non_cancellable,provider_poll_required,created_at,updated_at
           ) VALUES('j1','video','s1','v1','e1','p1','running','worker-1',9999999999,'not_started',0,0,1,1)"""
    )
    conn.commit()
    settled: list = []

    async def fence_always_trips(_job, _version_id, write_point):
        raise ReviewDependencyFence(STALE_DETAIL)

    def forbid_create(*_a, **_k):
        raise AssertionError("worker_start 命中围栏不得调用供应商 create")

    def forbid_poll(*_a, **_k):
        raise AssertionError("worker_start 命中围栏不得调用供应商 poll")

    patch_worker_everywhere(monkeypatch, "_assert_review_dependency_fence_async", fence_always_trips)
    patch_worker_everywhere(monkeypatch, "_assert_job_lease", lambda *_a, **_k: None)
    monkeypatch.setattr(worker.hiagent, "create_video_task", forbid_create)
    monkeypatch.setattr(worker.hiagent, "poll_video_task", forbid_poll)
    monkeypatch.setattr(
        worker.media_scheduler, "settle_budget",
        lambda job_id, cost, *, success: settled.append((job_id, cost, success)),
    )
    patch_worker_everywhere(monkeypatch, "reconcile_episode_generation_status", lambda *_a, **_k: None)

    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))

    job = conn.execute("SELECT status, error FROM jobs WHERE id='j1'").fetchone()
    version = conn.execute("SELECT status FROM shot_versions WHERE id='v1'").fetchone()
    assert job["status"] == "failed"
    assert version["status"] == "failed"
    assert "REVIEW_DEPENDENCY_STALE" in job["error"]
    assert settled == [("j1", 0.0, False)], "尚未花钱，预算必须原样释放，不得按已产生费用结算"
