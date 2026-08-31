"""零产出终态拒绝的放行 + 释放：红绿覆盖真实事故场景（proj_1fce17f77010 镜 5/6）。

判据基于「零产出」（``video_path`` 为空 + 供应商轮询确认终态失败），不基于
「零成本」——``cost_cny``/``actual_cost_cny`` 曾被 ``completion_grant.
reconcile_provider_tasks_for_clear`` 的历史 bug 按预留估算错误结算成非零（见
``app/provider_task_zero_cost.py`` 模块 docstring 与
``tests/test_generation_station_asset_clear.py`` 里那两条 reconcile 红绿用例），
不能作为「真花了钱」的证据。方向都要覆盖（CLAUDE.md「假设要证伪」）：
  1. 供应商确认终态失败 + 从未产生任何产出文件 -> 放行、可释放（不论记录的
     成本是 0 还是被污染成非零）。
  2. 已存在产出文件（下载成功但技术校验不过/QA 事后判不合格）-> 继续阻塞：
     这才是「真花了钱」的唯一可靠信号。
  3. 未提交/未确认终态（没有供应商轮询终态失败记录，局部超时放弃场景）->
     继续阻塞，不能把"不知道供应商结论"当"零产出"。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import completion_grant, db
from app.provider_task_zero_cost import (
    list_zero_cost_terminal_candidates,
    load_zero_cost_evidence,
    release_zero_cost_terminal_jobs,
    zero_cost_terminal_release_eligible,
)


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e','p',1,'done',0)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s','e',5,15)"
    )
    conn.commit()
    return conn


def _seed_rejected_video_job(
    conn: sqlite3.Connection,
    *,
    cost_cny: float = 0.0,
    actual_cost_cny: float | None = None,
    video_path: str | None = None,
    version_status: str = "waiting_human",
    job_status: str = "waiting_human",
    disposition: str = "manual_review",
    retryable: int = 0,
    provider_task_id: str | None = "provider-task-1",
    log_terminal_poll_failure: bool = True,
    reservation_status: str = "reserved",
) -> None:
    completion_grant.ensure_video_budget_authority_tables(conn)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,provider_task_id,
               status,video_path,cost_cny,created_at
           ) VALUES('v1','s',1,'prompt','idem-1',?,?,?,?,1)""",
        (provider_task_id, version_status, video_path, cost_cny),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_non_cancellable,provider_operation_id,
               provider_create_state,provider_poll_required,
               provider_failure_category,provider_failure_kind,
               provider_failure_disposition,provider_failure_retryable,
               reason_text,created_at,updated_at
           ) VALUES(
               'j1','video','s','v1','e','p',?,
               1,'op-1','accepted',1,
               'technical','provider_execution_failed',?,?,
               '视频供应商执行失败，供应商原文：copyright restrictions',1,1
           )""",
        (job_status, disposition or None, retryable),
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at,
               actual_cost_cny
           ) VALUES('b1','j1','episode','e',12.0,?,1,?)""",
        (reservation_status, actual_cost_cny),
    )
    if log_terminal_poll_failure and provider_task_id:
        conn.execute(
            """INSERT INTO provider_calls(
                   ts,kind,status,error,meta,operation_id
               ) VALUES(1,'video_poll','TASK_FAILED','copyright restrictions',?,?)""",
            (
                f'{{"task_id": "{provider_task_id}", "shot_id": "s"}}',
                "op-1",
            ),
        )
    conn.commit()


def test_zero_output_terminal_rejection_is_eligible_and_releasable() -> None:
    """真实事故场景：单次轮询终态失败，无产出、无成本记录 -> 应当放行。"""
    conn = _database()
    _seed_rejected_video_job(conn)

    evidence = load_zero_cost_evidence(conn, "j1")
    eligible, reason = zero_cost_terminal_release_eligible(conn, evidence)
    assert eligible is True, reason

    clearance = completion_grant.provider_task_clearance_snapshot(
        project_id="p", conn=conn,
    )
    assert clearance["safe_to_clear"] is True
    assert clearance["blockers"] == []

    receipts = release_zero_cost_terminal_jobs(conn, ["j1"])
    assert receipts == [{
        "job_id": "j1", "amount_cny": 0.0,
        "reason": "供应商已确认终态失败（轮询接口返回 failed），且未记录任何已产生费用",
        "reserved_amount_cny": 12.0,
    }]
    reservation = conn.execute(
        "SELECT status,actual_cost_cny FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()
    assert dict(reservation) == {"status": "released", "actual_cost_cny": 0.0}
    assert conn.execute(
        "SELECT provider_poll_required,reserved_cost_cny FROM jobs WHERE id='j1'"
    ).fetchone()[:] == (0, 0.0)


def test_polluted_nonzero_cost_still_releases_when_output_is_provably_absent() -> None:
    """真实事故的当前实况：``reconcile_provider_tasks_for_clear`` 的历史 bug 已经
    把这两笔按 ¥12 记成非零成本（``video_path`` 全程为空）——这不是"真花了钱"
    的证据，只是一条已知 bug 的污染。零产出证据（供应商确认失败 + 从未下载）
    才是权威来源，释放时必须能把这个虚构数字纠正为 0。"""
    conn = _database()
    _seed_rejected_video_job(
        conn, cost_cny=12.0, actual_cost_cny=12.0,
        reservation_status="settled",
        job_status="failed",
    )

    evidence = load_zero_cost_evidence(conn, "j1")
    eligible, reason = zero_cost_terminal_release_eligible(conn, evidence)
    assert eligible is True, reason
    assert "矛盾" in reason

    receipts = release_zero_cost_terminal_jobs(conn, ["j1"])

    assert receipts[0]["job_id"] == "j1"
    reservation = conn.execute(
        "SELECT status,actual_cost_cny FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()
    assert dict(reservation) == {"status": "released", "actual_cost_cny": 0.0}
    assert conn.execute(
        "SELECT cost_cny FROM shot_versions WHERE id='v1'"
    ).fetchone()["cost_cny"] == 0.0


def test_unsettled_null_cost_without_terminal_poll_evidence_still_blocks() -> None:
    """局部超时放弃：从未拿到供应商的终态失败应答——不能把"不知道供应商结论"
    当"零产出"，必须继续阻塞。"""
    conn = _database()
    _seed_rejected_video_job(
        conn,
        cost_cny=0.0,
        actual_cost_cny=None,
        log_terminal_poll_failure=False,
    )

    evidence = load_zero_cost_evidence(conn, "j1")
    eligible, reason = zero_cost_terminal_release_eligible(conn, evidence)
    assert eligible is False
    assert "轮询" in reason

    clearance = completion_grant.provider_task_clearance_snapshot(
        project_id="p", conn=conn,
    )
    assert clearance["safe_to_clear"] is False

    with pytest.raises(ValueError):
        release_zero_cost_terminal_jobs(conn, ["j1"])


def test_external_terminal_zero_output_is_also_releasable() -> None:
    """生产库存量的真实形状：``external_terminal``（模型明确拒绝，视频模型判定
    走 ``checkpoints.py::_commit_provider_terminal_failure_in_transaction``）此前
    同样被那条 bug 按预留全价结算过——生产库有 30 条同类记录，合计 ¥360，
    全部 ``video_path`` 为空。这条判据必须能覆盖它们，而不是把 external_
    terminal 一律拒之门外（那样这 30 条永远没有入口能纠正）。"""
    conn = _database()
    _seed_rejected_video_job(
        conn, disposition="external_terminal", retryable=0,
        cost_cny=12.0, actual_cost_cny=12.0,
        reservation_status="settled", job_status="failed",
    )

    evidence = load_zero_cost_evidence(conn, "j1")
    eligible, reason = zero_cost_terminal_release_eligible(conn, evidence)
    assert eligible is True, reason

    receipts = release_zero_cost_terminal_jobs(conn, ["j1"])
    assert receipts[0]["job_id"] == "j1"
    assert conn.execute(
        "SELECT actual_cost_cny FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()["actual_cost_cny"] == 0.0


def test_no_disposition_at_all_still_blocks() -> None:
    """完全没有结构化失败分类（例如非 ProviderError 的普通异常）时不能放行——
    没有任何终态分类信号，判据必须 fail closed。"""
    conn = _database()
    _seed_rejected_video_job(conn, disposition="")

    evidence = load_zero_cost_evidence(conn, "j1")
    eligible, reason = zero_cost_terminal_release_eligible(conn, evidence)
    assert eligible is False
    assert "无匹配的技术失败终态分类" in reason


def test_retryable_failure_is_not_terminal_yet() -> None:
    conn = _database()
    _seed_rejected_video_job(conn, retryable=1)

    evidence = load_zero_cost_evidence(conn, "j1")
    eligible, _reason = zero_cost_terminal_release_eligible(conn, evidence)
    assert eligible is False


def test_adopted_output_with_video_path_blocks_even_with_zero_recorded_cost() -> None:
    """真花了钱的唯一可靠信号：已存在产出文件（下载成功但技术校验不过/QA 事后
    判不合格）。即便记录的成本是 0，也绝不能被当零产出放行——这条防护比"成本
    数字"更重要，是本次两次订正后仍然守住的安全网。"""
    conn = _database()
    _seed_rejected_video_job(conn, video_path="/tmp/shot.mp4")

    evidence = load_zero_cost_evidence(conn, "j1")
    eligible, reason = zero_cost_terminal_release_eligible(conn, evidence)
    assert eligible is False
    assert "产出文件" in reason

    with pytest.raises(ValueError, match="不满足零产出终态释放条件"):
        release_zero_cost_terminal_jobs(conn, ["j1"])


def test_list_candidates_reports_both_eligible_and_blocked_with_reasons() -> None:
    conn = _database()
    _seed_rejected_video_job(conn)

    candidates = list_zero_cost_terminal_candidates(conn, project_id="p")

    assert len(candidates) == 1
    item = candidates[0]
    assert item["job_id"] == "j1"
    assert item["eligible"] is True
    assert item["reserved_amount_cny"] == 12.0
    assert "copyright" in item["reason_text"]


def test_list_candidates_also_surfaces_already_settled_polluted_entries() -> None:
    """真实事故的当前实况：预留已经是 status='settled'（不是 'reserved'）——
    这类"已经挂着历史 bug 污染费用"的记录也必须出现在列表里，否则用户找不到
    入口去纠正。"""
    conn = _database()
    _seed_rejected_video_job(
        conn, cost_cny=12.0, actual_cost_cny=12.0,
        reservation_status="settled",
        job_status="failed",
    )

    candidates = list_zero_cost_terminal_candidates(conn, project_id="p")

    assert len(candidates) == 1
    assert candidates[0]["job_id"] == "j1"
    assert candidates[0]["eligible"] is True


def test_list_candidates_surfaces_external_terminal_pollution_too() -> None:
    """生产库存量：``external_terminal`` 分类的 30 条污染记录也必须出现在
    列表里——之前的判据把 external_terminal 整体排除在候选之外，那 30 条永远
    没有入口可见、可纠正。"""
    conn = _database()
    _seed_rejected_video_job(
        conn, disposition="external_terminal",
        cost_cny=12.0, actual_cost_cny=12.0,
        reservation_status="settled", job_status="failed",
    )

    candidates = list_zero_cost_terminal_candidates(conn, project_id="p")

    assert len(candidates) == 1
    assert candidates[0]["job_id"] == "j1"
    assert candidates[0]["eligible"] is True


def test_release_rejects_unknown_job_id() -> None:
    conn = _database()
    with pytest.raises(ValueError, match="任务不存在"):
        release_zero_cost_terminal_jobs(conn, ["missing-job"])
