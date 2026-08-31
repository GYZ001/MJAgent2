"""零扣费终态释放的路由层：可见性列表 + 本地二段式确认（``?confirm=true``）。

只测路由自身的协议（预览不写库、confirm 才写库、任一不合格整体拒绝），判据
本身的红绿覆盖见 ``tests/test_provider_task_zero_cost.py``。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import HTTPException

from app import completion_grant, db, provider_task_zero_cost_api as api


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


def _seed(
    conn: sqlite3.Connection, *, cost_cny: float = 0.0, video_path: str | None = None,
) -> None:
    completion_grant.ensure_video_budget_authority_tables(conn)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,provider_task_id,
               status,video_path,cost_cny,created_at
           ) VALUES('v1','s',1,'prompt','idem-1','provider-task-1',
                    'waiting_human',?,?,1)""",
        (video_path, cost_cny),
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
               'j1','video','s','v1','e','p','waiting_human',
               1,'op-1','accepted',1,
               'technical','provider_execution_failed','manual_review',0,
               '视频供应商执行失败，供应商原文：copyright restrictions',1,1
           )"""
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at
           ) VALUES('b1','j1','episode','e',12.0,'reserved',1)"""
    )
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,status,error,meta,operation_id
           ) VALUES(1,'video_poll','TASK_FAILED','copyright restrictions',
                    '{"task_id": "provider-task-1"}','op-1')"""
    )
    conn.commit()


def test_candidates_lists_stuck_job_with_cost_and_provider_text(monkeypatch) -> None:
    conn = _database()
    _seed(conn)
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    result = api.zero_cost_candidates(project_id="p")

    assert result["total"] == 1
    item = result["items"][0]
    assert item["job_id"] == "j1"
    assert item["eligible"] is True
    assert item["reserved_amount_cny"] == 12.0
    assert "copyright" in item["reason_text"]


def test_candidate_detail_reports_eligibility_for_job_drawer(monkeypatch) -> None:
    conn = _database()
    _seed(conn)
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    result = api.zero_cost_candidate_detail("j1")

    assert result == {
        "job_id": "j1", "eligible": True,
        "reason": "供应商已确认终态失败（轮询接口返回 failed），且未记录任何已产生费用",
        "reserved_amount_cny": 12.0,
    }


def test_candidate_detail_404_for_missing_job(monkeypatch) -> None:
    conn = _database()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    with pytest.raises(HTTPException) as exc:
        api.zero_cost_candidate_detail("missing")

    assert exc.value.status_code == 404


def test_release_without_confirm_previews_and_does_not_write(monkeypatch) -> None:
    conn = _database()
    _seed(conn)
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    with pytest.raises(HTTPException) as exc:
        api.zero_cost_release({"job_ids": ["j1"]}, confirm=False)

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "confirmation_required"
    assert exc.value.detail["items"][0]["job_id"] == "j1"
    assert conn.execute(
        "SELECT status FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()["status"] == "reserved"


def test_release_with_confirm_settles_to_zero(monkeypatch) -> None:
    conn = _database()
    _seed(conn)
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    result = api.zero_cost_release({"job_ids": ["j1"]}, confirm=True)

    assert result == {"ok": True, "released": [{
        "job_id": "j1", "amount_cny": 0.0,
        "reason": "供应商已确认终态失败（轮询接口返回 failed），且未记录任何已产生费用",
        "reserved_amount_cny": 12.0,
    }]}
    assert conn.execute(
        "SELECT status,actual_cost_cny FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()[:] == ("released", 0.0)


def test_release_rejects_ineligible_job_even_with_confirm(monkeypatch) -> None:
    """已有产出文件的任务（真花了钱）：即便带了 confirm=true 也必须 409，不给
    强制忽略。"""
    conn = _database()
    _seed(conn, video_path="/tmp/shot.mp4")
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    with pytest.raises(HTTPException) as exc:
        api.zero_cost_release({"job_ids": ["j1"]}, confirm=True)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ZERO_COST_RELEASE_NOT_ELIGIBLE"
    assert conn.execute(
        "SELECT status FROM budget_reservations WHERE job_id='j1'"
    ).fetchone()["status"] == "reserved"


def test_release_empty_job_ids_rejected(monkeypatch) -> None:
    conn = _database()
    monkeypatch.setattr(api, "get_conn", lambda: conn)

    with pytest.raises(HTTPException) as exc:
        api.zero_cost_release({"job_ids": []}, confirm=True)

    assert exc.value.status_code == 422
