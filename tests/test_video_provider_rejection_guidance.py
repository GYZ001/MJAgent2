"""供应商拒绝/失败落地文案 + 重复失败行为判据回归。

背景（真实案例 ERR-20260828-ca89d8 / ERR-20260828-cfab88，"我欲封天" EP1
两镜被 Seedance 判定版权风险拒绝）：

- 供应商返回没有任何结构化信号（``error.code`` 为空、无 ``failure`` 子
  对象），落到 ``ProviderFailure.technical(EXECUTION_FAILED)`` 默认分类，
  disposition=manual_review。``route_video_repair()`` 在第一次失败就已经
  正确判 ``is_paid=False, strategy=handoff_human``（见
  app/video_repair_router.py 的 ``non_repairable`` 分支）。
- 但 ``_video_model_rejection_guidance()`` 之前只在
  ``failure.category is MODEL_REJECTION`` 时接管文案，这条路径落到
  app/errors.py 的 provider 分类兜底提示——``is_technical=True`` 时无条件
  丢弃供应商原文，只吐"可稍后重试"。对已经判定不会再自动付费重试的镜头，
  这句话是假话。
- 供应商这类失败没有任何结构化信号可用，``if "copyright" in message`` 这种
  关键词黑名单被明令禁止。本文件验证改用的纯行为判据（同一供应商任务标识
  连续 ≥2 次返回终态失败且 error.message 字节级相同）只看结构、不看内容。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app import db as db_mod
from app import hiagent, worker
from app.errors import CATEGORIES
from app.hiagent import ProviderError, ProviderFailure, ProviderFailureKind

REAL_COPYRIGHT_MESSAGE = (
    "The request failed because the output video may be related to copyright "
    "restrictions. Request id: 02178796468022100000000000000000000ffffac141a61e355b4"
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db_mod.SCHEMA)
    for stmt in db_mod.MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


# ---------------------------------------------------------------------------
# _video_model_rejection_guidance
# ---------------------------------------------------------------------------


def test_generic_provider_hint_says_retry_later():
    """先证明兜底提示确实会说"可稍后重试"——这是本次修复要绕开的假话来源，
    app/errors.py 不属于本次改动范围，这里只做只读核对。"""
    assert "可稍后重试" in CATEGORIES["provider"]["hint"]


def test_guidance_covers_execution_failed_technical_disposition_without_lying():
    """P0 fix #3：TECHNICAL/EXECUTION_FAILED + exc.raw 非空必须被接管，不能
    再落到"可稍后重试"兜底；必须逐字带出供应商原文，并声明已停止自动重试。"""
    exc = ProviderError(
        f"视频模型 任务失败：{REAL_COPYRIGHT_MESSAGE}",
        raw=REAL_COPYRIGHT_MESSAGE,
        failure=ProviderFailure.technical(ProviderFailureKind.EXECUTION_FAILED),
    )
    guidance = worker._video_model_rejection_guidance({}, exc)
    assert guidance is not None
    code, message = guidance
    assert code == "VIDEO_PROVIDER_EXECUTION_FAILED"
    assert "可稍后重试" not in message
    assert REAL_COPYRIGHT_MESSAGE in message
    assert "已停止" in message and "自动付费重试" in message


def test_guidance_covers_model_rejection_with_verbatim_provider_text():
    exc = ProviderError(
        f"视频模型 任务失败：{REAL_COPYRIGHT_MESSAGE}",
        raw=REAL_COPYRIGHT_MESSAGE,
        failure=ProviderFailure.model_rejection(ProviderFailureKind.EXECUTION_FAILED),
    )
    guidance = worker._video_model_rejection_guidance({"mode": "reference_image"}, exc)
    assert guidance is not None
    code, message = guidance
    assert code == "VIDEO_PROVIDER_MODEL_REJECTED"
    assert "可稍后重试" not in message
    assert REAL_COPYRIGHT_MESSAGE in message
    assert "已停止" in message and "自动付费重试" in message


def test_guidance_returns_none_without_provider_raw_text():
    """没有供应商原文时不接管——避免产出"引用了空字符串"的假原文。"""
    exc = ProviderError(
        "视频模型 任务失败：",
        raw="",
        failure=ProviderFailure.technical(ProviderFailureKind.EXECUTION_FAILED),
    )
    assert worker._video_model_rejection_guidance({}, exc) is None


def test_guidance_leaves_prompt_provider_rejected_branch_intact():
    exc = ProviderError(
        "AI 视频提示词服务拒绝当前内容",
        raw="provider content policy hit",
        failure=ProviderFailure.model_rejection(ProviderFailureKind.PROMPT_PROVIDER_REJECTED),
    )
    guidance = worker._video_model_rejection_guidance({}, exc)
    assert guidance is not None
    code, message = guidance
    assert code == "VIDEO_PROMPT_PROVIDER_REJECTED"
    assert "可稍后重试" not in message


def test_guidance_returns_none_for_unrelated_technical_kind():
    """只扩展 EXECUTION_FAILED，不擅自扩大到其它 technical kind——避免超出
    已定稿方案的范围。"""
    exc = ProviderError(
        "视频任务状态响应不是合法 JSON 对象",
        raw="not-json-body",
        failure=ProviderFailure.technical(ProviderFailureKind.MALFORMED_RESPONSE),
    )
    assert worker._video_model_rejection_guidance({}, exc) is None


# ---------------------------------------------------------------------------
# P1：hiagent.has_repeated_terminal_poll_failure —— 纯行为判据，不看内容
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("messages", "min_repeats", "expected"),
    [
        ([], 2, False),
        ([REAL_COPYRIGHT_MESSAGE], 2, False),
        ([REAL_COPYRIGHT_MESSAGE, REAL_COPYRIGHT_MESSAGE], 2, True),
        (["msg-a", "msg-b"], 2, False),
        (["unrelated-earlier", REAL_COPYRIGHT_MESSAGE, REAL_COPYRIGHT_MESSAGE], 2, True),
        ([REAL_COPYRIGHT_MESSAGE, "different-msg"], 2, False),
        (["", ""], 2, False),
        ([REAL_COPYRIGHT_MESSAGE, REAL_COPYRIGHT_MESSAGE], 3, False),
        ([REAL_COPYRIGHT_MESSAGE] * 3, 3, True),
        # 字节级严格：多一个空格就不算相同，不允许模糊/相似度匹配。
        ([REAL_COPYRIGHT_MESSAGE + " ", REAL_COPYRIGHT_MESSAGE], 2, False),
    ],
)
def test_has_repeated_terminal_poll_failure_is_structural(messages, min_repeats, expected):
    assert (
        hiagent.has_repeated_terminal_poll_failure(messages, min_repeats=min_repeats)
        is expected
    )


def test_has_repeated_terminal_poll_failure_ignores_message_content():
    """判据是结构（同一任务连续给出相同结果），不是内容（不查 message 里
    有没有 "copyright" 这类词）：完全不含该词的技术性文案同样应该被判定为
    重复终态，禁止关键词黑名单式实现。"""
    generic = "internal provider timeout while rendering frame batch 4/8"
    assert hiagent.has_repeated_terminal_poll_failure([generic, generic]) is True


def test_prior_task_poll_failure_messages_reads_chronological_history():
    """DB 胶水函数只做查询拼装，不做判断；用真实 provider_calls 形状核对
    时间顺序与 task_id 过滤都对。"""
    conn = _conn()
    conn.executemany(
        """INSERT INTO provider_calls(ts, kind, status, error, meta)
           VALUES(?, 'video_poll', 'TASK_FAILED', ?, ?)""",
        [
            (1.0, REAL_COPYRIGHT_MESSAGE, json.dumps({"task_id": "task-a"})),
            (2.0, "unrelated other task failure", json.dumps({"task_id": "task-b"})),
            (3.0, REAL_COPYRIGHT_MESSAGE, json.dumps({"task_id": "task-a"})),
        ],
    )
    conn.commit()
    history = worker._prior_task_poll_failure_messages(conn, "task-a")
    assert history == [REAL_COPYRIGHT_MESSAGE, REAL_COPYRIGHT_MESSAGE]


# ---------------------------------------------------------------------------
# P1 端到端：worker._run_job 里的行为判据升级
# ---------------------------------------------------------------------------


def _seed_repeat_poll_job(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e1','p1',1,'generating',1)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,transition
           ) VALUES(
               's1','e1',1,5,'中景','固定','室内','[]','人物站定','[]','硬切'
           )"""
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               provider_task_id,image_inputs,created_at
           ) VALUES(
               'v1','s1',1,'prompt','idem','running',
               'task-repeat-1','{}',1
           )"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               lease_owner,lease_expires_at,provider_operation_id,
               provider_create_state,provider_non_cancellable,
               provider_submitted_at,created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e1','p1','running',
               'worker-1',9999999999,'video-create-v1',
               'accepted',1,1,1,1
           )"""
    )
    conn.commit()


def _wire_run_job_common_mocks(monkeypatch, conn: sqlite3.Connection, poll_fn) -> None:
    from app.media_pipeline import concurrency, stage_state

    class Permit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def no_sleep(_delay: float) -> None:
        return None

    async def no_fence(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(worker, "_assert_review_dependency_fence_async", no_fence)
    monkeypatch.setattr(worker, "_assert_job_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.hiagent, "poll_video_task", poll_fn)
    monkeypatch.setattr(concurrency, "semaphore_for", lambda _resource: Permit())
    monkeypatch.setattr(concurrency, "report_congestion", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(concurrency, "report_healthy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stage_state, "set_pipeline_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.media_scheduler, "renew_lease", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker.media_scheduler, "settle_budget", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker, "reconcile_episode_generation_status", lambda *_args, **_kwargs: None,
    )


def _wire_run_job_common_mocks_real_db(monkeypatch, poll_fn) -> None:
    """跟 ``_wire_run_job_common_mocks`` 一样，但不 monkeypatch ``get_conn``：
    留给真实的 ``app.db.get_conn``/``DB_PATH`` 机制，让内部按线程重新打开
    连接的写路径落在同一个磁盘文件上。"""
    from app.media_pipeline import concurrency, stage_state

    class Permit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def no_sleep(_delay: float) -> None:
        return None

    async def no_fence(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(worker, "_assert_review_dependency_fence_async", no_fence)
    monkeypatch.setattr(worker, "_assert_job_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.hiagent, "poll_video_task", poll_fn)
    monkeypatch.setattr(concurrency, "semaphore_for", lambda _resource: Permit())
    monkeypatch.setattr(concurrency, "report_congestion", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(concurrency, "report_healthy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stage_state, "set_pipeline_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.media_scheduler, "renew_lease", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker.media_scheduler, "settle_budget", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "mark_media_job_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker, "reconcile_episode_generation_status", lambda *_args, **_kwargs: None,
    )


def test_repeated_identical_poll_failure_escalates_to_model_rejected(monkeypatch):
    """P1 端到端：同一 task_id 连续两次轮询给出字节级相同的终态失败，第二次
    必须把 provider_create_state 升级为 'model_rejected'（外部终态），
    即使供应商本身从没给过任何结构化 category=model_rejection 信号。

    刻意不用手工 :memory: 连接：``_commit_provider_acceptance`` 等写路径会
    按 ``_authority_checks_can_use_worker_thread`` 探测到"可重开"的落盘库
    就切到后台线程，经由 app.db.run_write_transaction 重新打开一条指向
    ``app.db.DB_PATH`` 的连接——那条路径不认从 ``worker.get_conn`` monkeypatch
    出去的内存连接对象，会在真实落盘文件与内存库之间读写错位。这里改用
    pytest 每个用例自动准备好的隔离落盘库（conftest.py 的
    ``_reset_capability_runtime``，通过 ``app.db.get_conn()``/``DB_PATH``
    透明生效，不需要手工建库），最后另开一条独立只读连接读盘验证——不是
    同一个写连接读自己写的内容，证明这次升级真的提交了。
    """
    import asyncio

    from app.db import get_conn

    conn = get_conn()
    _seed_repeat_poll_job(conn)

    poll_calls = {"n": 0}

    async def poll_task_failed(task_id, *, call_meta=None):
        poll_calls["n"] += 1
        # 模拟真实 SeedanceAdapter.poll_video_task：TASK_FAILED 轮询在
        # 适配器内部同步落 provider_calls（见 app/seedance.py + app/db.py
        # log_provider_call），这里手写等价的落库动作，用当前任务的连接
        # （与 _run_job 本体用的是同一条，按 asyncio task 缓存）。
        c = get_conn()
        c.execute(
            """INSERT INTO provider_calls(ts, kind, model, status, http_status,
                   latency_ms, error, meta)
               VALUES(?, 'video_poll', 'seedance', 'TASK_FAILED', 200, 10, ?, ?)""",
            (
                float(poll_calls["n"]),
                REAL_COPYRIGHT_MESSAGE,
                json.dumps({"task_id": task_id}),
            ),
        )
        c.commit()
        return {
            "status": "failed",
            "video_url": "",
            "last_frame_url": "",
            "error": REAL_COPYRIGHT_MESSAGE,
            "failure": {
                "category": "technical",
                "kind": "provider_execution_failed",
                "disposition": "manual_review",
                "retryable": False,
            },
        }

    _wire_run_job_common_mocks_real_db(monkeypatch, poll_task_failed)

    # 第一次失败：还没有"连续重复"的历史，维持 manual_review/waiting_human，
    # 不得升级——这条断言同时锁定"不误伤真正只失败一次的瞬时故障"。
    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))
    first = conn.execute(
        """SELECT status,provider_create_state,provider_failure_category,
                  provider_failure_disposition
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    assert dict(first) == {
        "status": "waiting_human",
        "provider_create_state": "accepted",
        "provider_failure_category": "technical",
        "provider_failure_disposition": "manual_review",
    }
    assert poll_calls["n"] == 1

    # 模拟对同一任务的再次轮询（真实场景：还没确认终态前，调度器又发现这个
    # "看起来还没完全终结"的任务，重新拉了一次同一 task_id 的状态）。
    conn.execute(
        """UPDATE jobs
              SET status='running',lease_owner='worker-1',lease_expires_at=9999999999,
                  provider_create_state='accepted',error=NULL
            WHERE id='j1'"""
    )
    conn.execute("UPDATE shot_versions SET status='running' WHERE id='v1'")
    conn.commit()

    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))
    second = conn.execute(
        """SELECT status,provider_create_state,provider_failure_category,
                  provider_failure_disposition
             FROM jobs WHERE id='j1'"""
    ).fetchone()
    assert dict(second) == {
        "status": "failed",
        "provider_create_state": "model_rejected",
        "provider_failure_category": "model_rejection",
        "provider_failure_disposition": "external_terminal",
    }
    assert poll_calls["n"] == 2

    # 独立只读连接，证明写入真的提交到了磁盘上的文件，不是同一连接读自己写。
    verify_conn = sqlite3.connect(str(db_mod.DB_PATH))
    verify_conn.row_factory = sqlite3.Row
    try:
        persisted = verify_conn.execute(
            "SELECT provider_create_state, status FROM jobs WHERE id='j1'"
        ).fetchone()
    finally:
        verify_conn.close()
    assert persisted["provider_create_state"] == "model_rejected"
    assert persisted["status"] == "failed"


def test_single_poll_failure_does_not_escalate(monkeypatch):
    """反例：只失败一次（没有"连续重复"）不得升级——防止把纯行为判据实现
    成"只要失败就当作终态"的懒惰版本。"""
    import asyncio

    conn = _conn()
    _seed_repeat_poll_job(conn)

    poll_calls = {"n": 0}

    async def poll_task_failed_once(task_id, *, call_meta=None):
        poll_calls["n"] += 1
        conn.execute(
            """INSERT INTO provider_calls(ts, kind, model, status, http_status,
                   latency_ms, error, meta)
               VALUES(?, 'video_poll', 'seedance', 'TASK_FAILED', 200, 10, ?, ?)""",
            (1.0, REAL_COPYRIGHT_MESSAGE, json.dumps({"task_id": task_id})),
        )
        conn.commit()
        return {
            "status": "failed",
            "video_url": "",
            "last_frame_url": "",
            "error": REAL_COPYRIGHT_MESSAGE,
            "failure": {
                "category": "technical",
                "kind": "provider_execution_failed",
                "disposition": "manual_review",
                "retryable": False,
            },
        }

    _wire_run_job_common_mocks(monkeypatch, conn, poll_task_failed_once)

    asyncio.run(worker._run_job("j1", lease_owner="worker-1"))
    job = conn.execute(
        "SELECT status,provider_create_state FROM jobs WHERE id='j1'"
    ).fetchone()
    assert job["status"] == "waiting_human"
    assert job["provider_create_state"] == "accepted"
