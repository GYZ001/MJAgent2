"""死掉的供应商任务号不得被账本找回（2026-09-05 我欲封天第 5 集第 2 镜）。

供应商对一个任务报了终态失败后，``release_provider_poll`` 解绑 job/版本并把那条
create 标成 ``TASK_FAILED``；但重跑时 ``run_job`` 在没有 task id 的情况下会按
operation_id 去账本"找回已付费任务"（``_recover_paid_video_task``），暂停恢复也有
同样的查询（``_recover_paused_provider_handle``）——两处都没看 ``recovery_disposition``，
于是 1 次 create 之后 4 次重试全轮回同一个版权拒绝的任务，永远凑不齐"3 个独立任务
相同拒绝"，最后按技术故障转人工。三处按 operation_id 取回结果的查询必须共用同一份
"死任务"判据；解绑时还要清 ``provider_non_cancellable``，否则
``_assert_provider_create_resolved`` 会把换新任务的重试拒成 CREATE_UNRESOLVED。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from app.db import get_conn
from app.media_exec import enqueue
from app.media_exec.authority import _assert_provider_create_resolved
from app.media_exec.job_state import _recover_paid_video_task, release_provider_poll

OPERATION = "video-create-v1"


def _seeded_database() -> sqlite3.Connection:
    """用测试夹具隔离出来的真实库（含全部迁移列），不用裸 SCHEMA 的内存库。"""
    conn = get_conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES('e1','p1',1,'generating',1)"
    )
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,
                             scene_setting,characters,action_desc,dialogues,transition)
           VALUES('s1','e1',1,5,'中景','固定','室内','[]','人物站定','[]','硬切')"""
    )
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,
                                     provider_task_id,image_inputs,created_at)
           VALUES('v1','s1',1,'prompt','idem','running','task-dead','{}',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,
                            lease_owner,lease_expires_at,provider_operation_id,
                            provider_create_state,provider_non_cancellable,
                            provider_submitted_at,provider_poll_required,created_at,updated_at)
           VALUES('j1','video','s1','v1','e1','p1','running','worker-1',9999999999,?,
                  'accepted',1,1,1,1,1)""",
        (OPERATION,),
    )
    conn.commit()
    return conn


def _insert_create(conn, task_id: str, ts: float, disposition: str | None) -> None:
    conn.execute(
        """INSERT INTO provider_calls(ts,kind,model,status,http_status,latency_ms,
                                      response_json,operation_id,recovery_disposition)
           VALUES(?,'video_create','seedance','OK',200,10,?,?,?)""",
        (ts, json.dumps({"id": task_id}), OPERATION, disposition),
    )
    conn.commit()


@pytest.mark.parametrize("disposition", enqueue.DEAD_PROVIDER_TASK_DISPOSITIONS)
def test_paid_task_recovery_skips_dead_dispositions(disposition: str) -> None:
    conn = _seeded_database()
    _insert_create(conn, "task-dead", 100.0, disposition)
    assert _recover_paid_video_task(conn, OPERATION) is None
    row = {"provider_operation_id": OPERATION}
    assert enqueue._recover_paused_provider_handle(conn, row) is None


def test_paid_task_recovery_returns_live_task_and_ignores_dead_sibling() -> None:
    conn = _seeded_database()
    _insert_create(conn, "task-live", 100.0, None)
    _insert_create(conn, "task-dead", 200.0, "TASK_FAILED")
    assert _recover_paid_video_task(conn, OPERATION) == ("task-live", 100.0)
    assert enqueue._recover_paused_provider_handle(conn, {"provider_operation_id": OPERATION}) == (
        "task-live", 100.0,
    )


def test_release_provider_poll_lets_next_run_create_a_fresh_task() -> None:
    conn = _seeded_database()
    _insert_create(conn, "task-dead", 100.0, None)
    release_provider_poll(conn, "j1", "worker-1", version_id="v1")
    job = conn.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    assert job["provider_poll_required"] == 0
    assert job["provider_create_state"] == "not_started"
    assert job["provider_non_cancellable"] == 0
    assert conn.execute("SELECT provider_task_id FROM shot_versions WHERE id='v1'").fetchone()[0] is None
    # 账本里那条 create 被标死，重跑按 operation_id 找不回旧任务号 → 走 create。
    assert _recover_paid_video_task(conn, OPERATION) is None
    _assert_provider_create_resolved(job, None)  # 不再因"可能已接单"拒绝新建


def test_dead_disposition_contract_shared_with_hiagent_reuse_query() -> None:
    """hiagent 的成功响应复用查询（不在 media_exec 层，无法 import 本常量）必须与
    这里的判据逐字一致：任何一侧漏掉一种 disposition，死任务就会从那一侧漏回来。"""
    source = Path("app/hiagent.py").read_text(encoding="utf-8")
    match = re.search(r"COALESCE\(recovery_disposition,''\) NOT IN \(([^)]*)\)", source)
    assert match, "hiagent 复用查询缺少 recovery_disposition 排除子句"
    listed = {item.strip().strip("'") for item in match.group(1).split(",")}
    assert listed == set(enqueue.DEAD_PROVIDER_TASK_DISPOSITIONS)
    assert enqueue.DEAD_PROVIDER_TASK_SQL == (
        "COALESCE(recovery_disposition,'') NOT IN ('RESET_PURGED','OUTPUT_UNREACHABLE','TASK_FAILED')"
    )
