import sqlite3

from app import db
from app.orchestration import media_scheduler


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,created_at) VALUES('e','p',1,0)")
    conn.execute("INSERT INTO jobs(id,kind,episode_id,project_id,status,created_at,updated_at) VALUES('j1','video','e','p','queued',0,0)")
    conn.execute("INSERT INTO jobs(id,kind,episode_id,project_id,status,created_at,updated_at) VALUES('j2','video','e','p','queued',0,0)")
    conn.commit()
    return conn


def test_budget_reservation_is_atomic_and_does_not_overrun(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)
    assert media_scheduler.reserve_budget("j1", "e", 6, 10, conn=conn)
    assert not media_scheduler.reserve_budget("j2", "e", 5, 10, conn=conn)
    assert conn.execute("SELECT status FROM jobs WHERE id='j2'").fetchone()["status"] == "paused_budget"
    assert conn.execute("SELECT SUM(amount_cny) FROM budget_reservations WHERE status='reserved'").fetchone()[0] == 6


def test_lease_claim_is_cas_and_expired_lease_can_be_reclaimed(monkeypatch) -> None:
    conn = _conn()
    clock = {"now": 100.0}
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)
    monkeypatch.setattr(media_scheduler, "now", lambda: clock["now"])
    first = media_scheduler.claim_job("j1", "worker-a", lease_seconds=10)
    assert first and not first.recovered
    assert media_scheduler.claim_job("j1", "worker-b", lease_seconds=10) is None
    clock["now"] = 111.0
    recovered = media_scheduler.claim_job("j1", "worker-b", lease_seconds=10)
    assert recovered and recovered.recovered


def test_recovery_keeps_future_retry_and_cancel_marks_provider_work_abandoned(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(media_scheduler, "get_conn", lambda: conn)
    monkeypatch.setattr(media_scheduler, "now", lambda: 100.0)
    conn.execute("UPDATE jobs SET next_retry_at=105 WHERE id='j1'")
    conn.execute("UPDATE jobs SET status='running', provider_non_cancellable=1, run_id=NULL, step_run_id=NULL WHERE id='j2'")
    conn.commit()
    jobs = dict(media_scheduler.recoverable_jobs())
    assert jobs["j1"] == 5.0
    result = media_scheduler.request_cancel("j2")
    assert result["status"] == "abandoned" and result["provider_may_continue"] is True
    row = conn.execute("SELECT abandoned,cancellation_requested FROM jobs WHERE id='j2'").fetchone()
    assert tuple(row) == (1, 1)
