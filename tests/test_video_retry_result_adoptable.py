"""可重试失败重排后，同一 job 的最终成功结果必须保有采用资格。

背景（生产 B，2026-10-01，第 1 集第 18/29 段）：首次尝试在供应商已接单前
遇到可重试的技术性失败，``job_state.settle_terminal_poll_failure`` 判定非
重复拒绝、按可重试处理，调用 ``release_provider_poll`` 把
``jobs.provider_result_adoptable`` 清 0（放弃这枚旧任务、交给
``retry_scheduling._schedule_job_retry`` 重新排队、换新任务重试）。但同一
job_id 换到的新供应商任务被 ``checkpoints._commit_provider_acceptance_in_
transaction`` 接单时，从未把这个字段改回 1；重试成功后
``checkpoints._commit_video_result_checkpoint_in_transaction`` 读到的仍是
陈旧的 0，把这个当下唯一、仍握着镜头级独占锁（``video_slot_active``）的
正常结果误判成"历史供应商任务"，整段隔离、永不采纳
（``shots.adopted_version_id`` 永远是 NULL）。

验证点：
1. ``test_retry_after_release_provider_poll_stays_adoptable``——完整复现
   「技术性可重试失败 → release_provider_poll → 同 job_id 换新任务接单 →
   成功」序列，断言最终结果可采用、job/version 落 succeeded 而不是被隔离
   文案污染，且不绕过已有的「真正历史任务必须隔离」语义（见下一个测试）。
2. ``test_disowned_history_job_still_quarantined``——两个不同 job_id 竞争
   同一镜头时，真正被数据库启动态 reconcile 判定为非所有者的历史 job 仍必
   须被隔离；本次修复只重置"同一个仍在 running 的 job 自己签收新任务"这一
   刻的字段，不触碰历史 job 的隔离判据。
"""
from __future__ import annotations

import sqlite3

from app import completion_grant, db, worker
from app.hiagent import ProviderFailure, ProviderFailureKind
from app.media_exec import job_state


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    completion_grant.ensure_video_budget_authority_tables(conn)
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,created_at) VALUES('e','p',1,0)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v1','s1',1,'prompt','idem-1','running',0)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               video_slot_active,provider_result_adoptable,provider_poll_required,
               provider_create_state,provider_non_cancellable,
               lease_owner,lease_expires_at,created_at,updated_at
           ) VALUES(
               'j1','video','s1','v1','e','p','running',
               1,1,1,
               'accepted',1,
               'worker-a',9999999999,0,0
           )"""
    )
    conn.commit()
    return conn


def test_retry_after_release_provider_poll_stays_adoptable() -> None:
    conn = _conn()

    # 1) 首次尝试：供应商任务已接单，轮询返回终态技术性可重试失败（既非重复
    #    拒绝也非上一段锚点帧被拒）。真实调用路径：run_job.py 第 699 行。
    failure = job_state.settle_terminal_poll_failure(
        conn,
        "j1",
        "worker-a",
        shot_id="s1",
        version_id="v1",
        failure=ProviderFailure.technical(
            ProviderFailureKind.EXECUTION_FAILED, retryable=True
        ),
        error_text="网关超时，可重试",
    )
    assert failure.retryable is True
    after_release = dict(
        conn.execute(
            """SELECT provider_poll_required,provider_result_adoptable,
                      video_slot_active,provider_create_state
                 FROM jobs WHERE id='j1'"""
        ).fetchone()
    )
    assert after_release == {
        "provider_poll_required": 0,
        "provider_result_adoptable": 0,  # 修复前后都应清 0：旧任务被放弃
        "video_slot_active": 1,  # 镜头级独占锁从未转手，重试仍是同一所有者
        "provider_create_state": "not_started",
    }

    # 2) retry_scheduling._schedule_job_retry 会把 job 重新排队（status=queued,
    #    retry_count+=1, lease_owner=NULL）；调度器稍后重新 claim 回 running。
    #    这两步只搬状态机，和本次要验证的字段无关，直接落库模拟。
    conn.execute(
        "UPDATE jobs SET status='queued', retry_count=1, lease_owner=NULL WHERE id='j1'"
    )
    conn.execute(
        "UPDATE jobs SET status='running', lease_owner='worker-a' WHERE id='j1'"
    )
    conn.commit()

    # 3) 同一 job_id 换到的新供应商任务被接单。
    worker._commit_provider_acceptance_in_transaction(
        conn,
        job_id="j1",
        version_id="v1",
        owner="worker-a",
        operation_id="video-create-v1-retry2",
        task_id="provider-task-retry2",
        submitted_at=100,
    )
    reaccepted = dict(
        conn.execute(
            "SELECT provider_result_adoptable,video_slot_active FROM jobs WHERE id='j1'"
        ).fetchone()
    )
    assert reaccepted == {"provider_result_adoptable": 1, "video_slot_active": 1}

    # 4) 供应商这次真的出片了。
    adoptable = worker._commit_video_result_checkpoint_in_transaction(
        conn,
        job_id="j1",
        version_id="v1",
        owner="worker-a",
        operation_id="video-create-v1-retry2",
        video_path="/tmp/retry-success.mp4",
        last_frame_url=None,
        cost_cny=12.0,
        latency_s=0.5,
        image_inputs="{}",
    )

    assert adoptable is True, "重试后的真实成功结果必须可采用，不能被当成历史任务隔离"
    job_row = dict(
        conn.execute("SELECT status,error,video_slot_active FROM jobs WHERE id='j1'").fetchone()
    )
    version_row = dict(
        conn.execute(
            "SELECT status,error,video_path FROM shot_versions WHERE id='v1'"
        ).fetchone()
    )
    assert job_row["error"] is None
    assert "隔离" not in (job_row["error"] or "")
    assert version_row["status"] == "succeeded"
    assert version_row["error"] is None
    assert version_row["video_path"] == "/tmp/retry-success.mp4"


def test_disowned_history_job_still_quarantined() -> None:
    """同镜两个不同 job_id 竞争：真正的历史任务依旧必须被隔离，不受本次修复影响。"""
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v2','s1',2,'prompt2','idem-2','running',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               video_slot_active,provider_result_adoptable,provider_poll_required,
               provider_create_state,provider_non_cancellable,
               lease_owner,lease_expires_at,created_at,updated_at
           ) VALUES(
               'j2','video','s1','v2','e','p','running',
               0,0,1,
               'accepted',1,
               'worker-b',9999999999,1,1
           )"""
    )
    conn.commit()

    adoptable = worker._commit_video_result_checkpoint_in_transaction(
        conn,
        job_id="j2",
        version_id="v2",
        owner="worker-b",
        operation_id="video-create-v2",
        video_path="/tmp/history.mp4",
        last_frame_url=None,
        cost_cny=12.0,
        latency_s=0.5,
        image_inputs="{}",
    )

    assert adoptable is False
    version_row = dict(
        conn.execute("SELECT status,error FROM shot_versions WHERE id='v2'").fetchone()
    )
    assert version_row["status"] == "quarantined"
    assert "隔离" in version_row["error"]
