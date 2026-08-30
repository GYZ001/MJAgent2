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
        # app.db.init_db() looks up its per-table bootstrap steps by name
        # through app.db_schema instead of importing these business modules
        # directly (P0-3 dependency inversion, see
        # docs/coupling_review_2026-08-29.md 第2步). This subprocess is a
        # fresh interpreter that never goes through app.main/conftest.py, so
        # it has to trigger the registration itself or init_db() raises
        # KeyError on the unconditional "video_budget_authority_tables" lookup.
        import app.artifacts, app.completion_grant, app.delivery  # noqa: E401
        import app.model_migration, app.production.certificate  # noqa: E401
        import app.production.grant, app.production.revision  # noqa: E401
        import app.production.shot_uid  # noqa: E401
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
        # app.db.init_db() looks up its per-table bootstrap steps by name
        # through app.db_schema instead of importing these business modules
        # directly (P0-3 dependency inversion, see
        # docs/coupling_review_2026-08-29.md 第2步). This subprocess is a
        # fresh interpreter that never goes through app.main/conftest.py, so
        # it has to trigger the registration itself or init_db() raises
        # KeyError on the unconditional "video_budget_authority_tables" lookup.
        import app.artifacts, app.completion_grant, app.delivery  # noqa: E401
        import app.model_migration, app.production.certificate  # noqa: E401
        import app.production.grant, app.production.revision  # noqa: E401
        import app.production.shot_uid  # noqa: E401
        db.init_db(reconcile_interrupted=True)
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


def test_waiting_provider_restart_polls_existing_task_with_new_step(tmp_path) -> None:
    database = tmp_path / "waiting-provider-restart.db"
    crashed = _run_child(
        """
        import os
        from pathlib import Path
        import sys

        from app import db
        from app.evidence import repository

        db.DB_PATH = Path(sys.argv[1])
        db._local.conn = None
        # app.db.init_db() looks up its per-table bootstrap steps by name
        # through app.db_schema instead of importing these business modules
        # directly (P0-3 dependency inversion, see
        # docs/coupling_review_2026-08-29.md 第2步). This subprocess is a
        # fresh interpreter that never goes through app.main/conftest.py, so
        # it has to trigger the registration itself or init_db() raises
        # KeyError on the unconditional "video_budget_authority_tables" lookup.
        import app.artifacts, app.completion_grant, app.delivery  # noqa: E401
        import app.model_migration, app.production.certificate  # noqa: E401
        import app.production.grant, app.production.revision  # noqa: E401
        import app.production.shot_uid  # noqa: E401
        db.init_db()
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at) "
            "VALUES('p1','Crash project','created',1)"
        )
        conn.execute(
            "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
            "VALUES('e1','p1',1,'generating',1)"
        )
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s) "
            "VALUES('s1','e1',1,5)"
        )
        conn.execute(
            "INSERT INTO shot_versions("
            "id,shot_id,version_no,prompt_text,idem_key,provider_task_id,status,"
            "image_inputs,created_at"
            ") VALUES("
            "'v1','s1',1,'prompt','idem','provider-task-existing','running','{}',1"
            ")"
        )
        conn.commit()
        run_id = repository.create_run(
            workflow_type='video_generation', scope_type='shot', scope_id='s1',
            input_fingerprint='waiting-provider-crash',
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
            "INSERT INTO jobs("
            "id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at,"
            "run_id,step_run_id,provider_non_cancellable,provider_operation_id,"
            "provider_create_state,provider_submitted_at"
            ") VALUES("
            "'job1','video','s1','v1','e1','p1','waiting_provider',1,1,?,?,1,"
            "'video-create-v1','accepted',1"
            ")",
            (run_id, step_id),
        )
        conn.commit()
        os._exit(23)
        """,
        database,
    )
    assert crashed.returncode == 23, crashed.stderr

    restarted = _run_child(
        """
        import asyncio
        import json
        from pathlib import Path
        import sys

        from app import db, worker
        from app.orchestration.media_runs import mark_media_job_state
        import app.media_pipeline.concurrency as concurrency

        db.DB_PATH = Path(sys.argv[1])
        db._local.conn = None
        # app.db.init_db() looks up its per-table bootstrap steps by name
        # through app.db_schema instead of importing these business modules
        # directly (P0-3 dependency inversion, see
        # docs/coupling_review_2026-08-29.md 第2步). This subprocess is a
        # fresh interpreter that never goes through app.main/conftest.py, so
        # it has to trigger the registration itself or init_db() raises
        # KeyError on the unconditional "video_budget_authority_tables" lookup.
        import app.artifacts, app.completion_grant, app.delivery  # noqa: E401
        import app.model_migration, app.production.certificate  # noqa: E401
        import app.production.grant, app.production.revision  # noqa: E401
        import app.production.shot_uid  # noqa: E401
        db.init_db(reconcile_interrupted=True)
        conn = db.get_conn()
        interrupted_run = conn.execute(
            "SELECT status FROM workflow_runs"
        ).fetchone()["status"]
        interrupted_step = conn.execute(
            "SELECT status FROM step_runs"
        ).fetchone()["status"]

        resumed = worker.recover_media_jobs()
        recovered_job = conn.execute(
            "SELECT run_id,step_run_id,status FROM jobs WHERE id='job1'"
        ).fetchone()

        create_calls = []
        poll_calls = []

        async def no_sleep(_delay):
            return None

        async def no_fence(*_args, **_kwargs):
            return None

        async def create_task(*_args, **_kwargs):
            create_calls.append(True)
            return "provider-task-duplicate"

        async def poll_task(task_id, **_kwargs):
            poll_calls.append(task_id)
            return {"status": "processing"}

        class Permit:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        # This is a bare subprocess script (fresh interpreter, no pytest test
        # session), so the monkeypatch fixture is not injected -- but
        # pytest.MonkeyPatch is the same class the fixture hands out,
        # constructible directly (public API since pytest 6.2). That lets
        # this call the exact tests.conftest.patch_worker_everywhere() every
        # other test in the suite uses instead of hand-listing which
        # app.media_exec submodules to patch. A hand-rolled list (this test
        # used to patch only "worker and app.media_exec.run_job", plus
        # "app.media_exec.enqueue" for _load_shot_model) silently stops
        # covering a name the moment its call site moves to a different
        # app.media_exec submodule -- app.media_exec is a real package, each
        # submodule binds its own copy of anything it imports with `from .x
        # import y`, and there is no error when a stale target list misses
        # one: the real, unstubbed function just runs against the fake
        # provider client instead of the test's stub (verified 2026-08-30 by
        # relocating _provider_wait_policy's call site into a scratch
        # submodule: the old hand-rolled list left it unpatched and the real
        # policy raised a provider timeout error the test doesn't expect;
        # patch_worker_everywhere still found and patched it because it
        # walks every app.media_exec submodule already in sys.modules rather
        # than a fixed list -- see its docstring in tests/conftest.py).
        # Importing `worker` above already imports every app.media_exec
        # submodule transitively (app/media_exec/__init__.py re-exports from
        # all of them and app/worker.py imports from that package __init__),
        # so they are all already in sys.modules by the time this runs.
        import pytest
        from tests.conftest import patch_worker_everywhere

        no_fence_provider_wait_policy = lambda *_args, **_kwargs: {
            "meta_changed": False,
            "stage_progress": None,
            "elapsed_s": 1.0,
            "timeout_s": 60.0,
            "poll_delay_s": 0.0,
            "scope": "视频任务",
        }
        no_source_excerpt = lambda prompt, _shot: prompt
        no_shot_model = lambda _shot: object()
        _mp = pytest.MonkeyPatch()
        patch_worker_everywhere(_mp, "_assert_review_dependency_fence_async", no_fence)
        patch_worker_everywhere(_mp, "ensure_source_excerpt_in_prompt", no_source_excerpt)
        patch_worker_everywhere(_mp, "_provider_wait_policy", no_fence_provider_wait_policy)
        patch_worker_everywhere(_mp, "_load_shot_model", no_shot_model)
        worker.asyncio.sleep = no_sleep
        worker.hiagent.create_video_task = create_task
        worker.hiagent.poll_video_task = poll_task
        concurrency.semaphore_for = lambda _resource: Permit()
        concurrency.report_healthy = lambda *_args, **_kwargs: None
        concurrency.report_congestion = lambda *_args, **_kwargs: None

        first_claim = worker.media_scheduler.claim_job(
            "job1", "restart-poll", lease_seconds=180
        )
        mark_media_job_state(
            recovered_job["run_id"], recovered_job["step_run_id"], "running"
        )
        asyncio.run(worker._run_job("job1", lease_owner="restart-poll"))

        second_claim = worker.media_scheduler.claim_job(
            "job1", "restart-finish", lease_seconds=180
        )
        mark_media_job_state(
            recovered_job["run_id"], recovered_job["step_run_id"], "running"
        )
        worker._set_job("job1", "succeeded", lease_owner="restart-finish")

        run = dict(conn.execute("SELECT * FROM workflow_runs").fetchone())
        job = dict(conn.execute(
            "SELECT * FROM jobs WHERE id='job1'"
        ).fetchone())
        version = dict(conn.execute(
            "SELECT * FROM shot_versions WHERE id='v1'"
        ).fetchone())
        steps = [dict(row) for row in conn.execute(
            "SELECT id,status,iteration_no,parent_step_run_id FROM step_runs "
            "ORDER BY iteration_no"
        )]
        print(json.dumps({
            "interrupted_run": interrupted_run,
            "interrupted_step": interrupted_step,
            "resumed": resumed,
            "recovered_job_status": recovered_job["status"],
            "first_claim": first_claim is not None,
            "second_claim": second_claim is not None,
            "create_calls": len(create_calls),
            "poll_calls": poll_calls,
            "provider_task_id": version["provider_task_id"],
            "run_status": run["status"],
            "job_status": job["status"],
            "job_step_run_id": job["step_run_id"],
            "steps": steps,
        }))
        """,
        database,
    )
    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout.strip())

    assert state["interrupted_run"] == "PAUSED_EXTERNAL"
    assert state["interrupted_step"] == "FAILED"
    assert state["resumed"] == 1
    assert state["recovered_job_status"] == "waiting_provider"
    assert state["first_claim"] is True
    assert state["second_claim"] is True
    assert state["create_calls"] == 0
    assert state["poll_calls"] == ["provider-task-existing"]
    assert state["provider_task_id"] == "provider-task-existing"
    assert state["run_status"] == "SUCCEEDED"
    assert state["job_status"] == "succeeded"
    assert state["steps"][0]["status"] == "FAILED"
    assert state["steps"][1]["status"] == "SUCCEEDED"
    assert state["steps"][1]["iteration_no"] == 2
    assert state["steps"][1]["parent_step_run_id"] == state["steps"][0]["id"]
    assert state["job_step_run_id"] == state["steps"][1]["id"]
