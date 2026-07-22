from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap


def _run_child(source: str, database: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), str(database)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )


def test_hard_process_exit_is_reconciled_and_media_job_is_requeued(tmp_path) -> None:
    database = tmp_path / "hard-restart.db"
    crashed = _run_child(
        """
        import os
        from pathlib import Path
        import sys

        from app import db
        from app.evidence import repository

        db.DB_PATH = Path(sys.argv[1])
        db._local.conn = None
        db.init_db()
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at) "
            "VALUES('p1','Crash project','created',1)"
        )
        conn.commit()
        run_id = repository.create_run(
            workflow_type='video_generation', scope_type='project', scope_id='p1',
            input_fingerprint='crash-test',
        )
        step_id = repository.create_step(run_id, 'video_generation')
        conn.execute(
            "UPDATE workflow_runs SET status='RUNNING', current_step_key='video_generation' "
            "WHERE id=?", (run_id,),
        )
        conn.execute(
            "UPDATE step_runs SET status='RUNNING', started_at=1 WHERE id=?", (step_id,),
        )
        conn.execute(
            "INSERT INTO jobs(id,kind,project_id,status,created_at,updated_at,run_id,"
            "step_run_id,lease_owner,lease_expires_at) "
            "VALUES('job1','video','p1','running',1,1,?,?,'dead-worker',9999999999)",
            (run_id, step_id),
        )
        conn.commit()
        db.start_provider_call(
            'video_create', 'model', request_json={'prompt': 'same'},
            meta={'operation_id': 'video-create-crash-test'},
        )
        os._exit(23)
        """,
        database,
    )
    assert crashed.returncode == 23, crashed.stderr

    restarted = _run_child(
        """
        import json
        from pathlib import Path
        import sys

        from app import db, worker

        db.DB_PATH = Path(sys.argv[1])
        db._local.conn = None
        db.init_db()
        resumed = worker.recover_media_jobs()
        conn = db.get_conn()
        run = dict(conn.execute('SELECT * FROM workflow_runs').fetchone())
        job = dict(conn.execute("SELECT * FROM jobs WHERE id='job1'").fetchone())
        steps = [dict(row) for row in conn.execute(
            'SELECT id,status,iteration_no,parent_step_run_id FROM step_runs '
            'ORDER BY iteration_no'
        )]
        call = dict(conn.execute('SELECT * FROM provider_calls').fetchone())
        print(json.dumps({
            'resumed': resumed, 'run_status': run['status'], 'failure_code': run['failure_code'],
            'job_status': job['status'], 'lease_owner': job['lease_owner'],
            'job_step_run_id': job['step_run_id'], 'steps': steps,
            'call_status': call['status'],
            'call_recovery_disposition': call['recovery_disposition'],
        }))
        """,
        database,
    )
    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout.strip())

    assert state["resumed"] == 1
    assert state["run_status"] == "WAITING_RETRY"
    assert state["failure_code"] == "SERVICE_RESTART"
    assert state["job_status"] == "queued"
    assert state["lease_owner"] is None
    assert state["steps"][0]["status"] == "FAILED"
    assert state["steps"][1]["status"] == "READY"
    assert state["steps"][1]["parent_step_run_id"] == state["steps"][0]["id"]
    assert state["job_step_run_id"] == state["steps"][1]["id"]
    assert state["call_status"] == "INTERRUPTED"
    assert state["call_recovery_disposition"] == "AWAITING_RETRY"
