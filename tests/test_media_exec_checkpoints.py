"""``_commit_provider_terminal_failure_in_transaction``：真实事故复盘。

真实事故（proj_1fce17f77010 镜 5/6，及生产库另外 30 条同类记录，合计 ¥360）
的真凶：这个函数此前对任何显式失败终态（``external_terminal`` 分类，例如
视频模型明确拒绝）一律按预留/claim 金额全价结算，完全不看有没有产出——供应商
拒绝往往发生在生成之前，根本没有算力消耗，``video_path`` 全程为空却被记了
全价。修复后：零产出（``video_path`` 为空）结算为 0；有产出（技术校验不过、
QA 事后判不合格等场景，确已下载视频）仍按估算保守计费，不受影响。

红绿验证按独立观察点做（不回退线上代码）：``_pre_fix_settled_cost`` 是修复前
公式的手写副本，用来证明"如果沿用旧公式，这里会算出 12.0"，而不是靠临时改回
生产代码。
"""
from __future__ import annotations

import sqlite3

from app import completion_grant, db, hiagent
from app.media_exec.checkpoints import _commit_provider_terminal_failure_in_transaction


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


def _seed_running_provider_job(conn: sqlite3.Connection, *, video_path: str | None) -> None:
    completion_grant.ensure_video_budget_authority_tables(conn)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,provider_task_id,
               status,video_path,created_at
           ) VALUES('v1','s',1,'prompt','idem-1','provider-task-1','running',?,1)""",
        (video_path,),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_non_cancellable,provider_operation_id,
               provider_create_state,provider_poll_required,
               lease_owner,cancellation_requested,created_at,updated_at
           ) VALUES(
               'j1','video','s','v1','e','p','running',
               1,'op-1','accepted',1,'owner-1',0,1,1
           )"""
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at
           ) VALUES('b1','j1','episode','e',12.0,'running',1)"""
    )
    conn.commit()


def _pre_fix_settled_cost(conn: sqlite3.Connection, *, operation_id: str, job_id: str) -> float:
    """修复前公式的手写副本（不是修复后代码的引用）：只看 claim/reservation
    金额，完全不看有没有产出。用来独立证明旧公式在这组数据上会算出 12.0。"""
    claim = conn.execute(
        "SELECT amount_cny FROM provider_video_budget_claims WHERE operation_id=? AND job_id=?",
        (operation_id, job_id),
    ).fetchone()
    reservation = conn.execute(
        "SELECT amount_cny FROM budget_reservations WHERE job_id=?", (job_id,),
    ).fetchone()
    return max(0.0, float(
        claim["amount_cny"] if claim is not None
        else (reservation["amount_cny"] if reservation is not None else 0)
    ))


def test_pre_fix_formula_would_have_charged_full_price_for_zero_output() -> None:
    """独立观察点：证明修复前的公式在真实事故的数据形状上确实算出 12.0。"""
    conn = _database()
    _seed_running_provider_job(conn, video_path=None)

    assert _pre_fix_settled_cost(conn, operation_id="op-1", job_id="j1") == 12.0


def test_terminal_failure_settles_zero_cost_when_no_output_downloaded() -> None:
    """修复后：供应商终态失败 + 从未下载 -> 结算为 0，不再按预留全价计费。"""
    conn = _database()
    _seed_running_provider_job(conn, video_path=None)
    failure = hiagent.ProviderFailure.model_rejection(
        hiagent.ProviderFailureKind.PROVIDER_REJECTED
    )

    settled_cost = _commit_provider_terminal_failure_in_transaction(
        conn,
        job_id="j1",
        version_id="v1",
        owner="owner-1",
        operation_id="op-1",
        message="视频模型明确拒绝了本次输入",
        reason_code="VIDEO_PROVIDER_MODEL_REJECTED",
        failure=failure,
    )

    assert settled_cost == 0.0
    assert conn.execute(
        "SELECT cost_cny FROM shot_versions WHERE id='v1'"
    ).fetchone()["cost_cny"] == 0.0
    reservation = conn.execute(
        "SELECT status,actual_cost_cny FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()
    assert dict(reservation) == {"status": "settled", "actual_cost_cny": 0.0}


def test_terminal_failure_still_charges_estimate_when_output_was_downloaded() -> None:
    """安全网：确有下载产出（真花了钱）时仍按预留估算保守计费，不受这次修复
    影响——不能因为修了零产出分支就把这个也一起改没了。"""
    conn = _database()
    _seed_running_provider_job(conn, video_path="/tmp/shot.mp4")
    failure = hiagent.ProviderFailure.model_rejection(
        hiagent.ProviderFailureKind.PROVIDER_REJECTED
    )

    settled_cost = _commit_provider_terminal_failure_in_transaction(
        conn,
        job_id="j1",
        version_id="v1",
        owner="owner-1",
        operation_id="op-1",
        message="视频模型明确拒绝了本次输入",
        reason_code="VIDEO_PROVIDER_MODEL_REJECTED",
        failure=failure,
    )

    assert settled_cost == 12.0
    assert conn.execute(
        "SELECT actual_cost_cny FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()["actual_cost_cny"] == 12.0
