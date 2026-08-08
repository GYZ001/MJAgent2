"""Project observability must never read or mutate across workspace boundaries."""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, system_api
from app.evidence import repository
from app.observability import api as observability_api
from app.orchestration import api as orchestration_api


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for key, value in db.DEFAULT_SETTINGS.items():
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
    for index, project_id in enumerate(("p1", "p2"), 1):
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at) VALUES(?,?,?,?)",
            (project_id, f"项目{index}", "created", index),
        )
        conn.execute(
            "INSERT INTO episodes(id,project_id,episode_no,title,created_at) VALUES(?,?,?,?,?)",
            (f"e{index}", project_id, 1, f"第{index}集", index),
        )
        conn.execute(
            """INSERT INTO workflow_runs(id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (f"run-{index}", "screenplay", "episode", f"e{index}", "FAILED", f"fp-{index}", index),
        )
        conn.execute(
            "INSERT INTO jobs(id,kind,project_id,episode_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (f"job-{index}", "video", project_id, f"e{index}", "failed", index, index),
        )
        conn.execute(
            """INSERT INTO provider_calls(ts,kind,model,status,latency_ms,request_json,response_json,meta,run_id)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (index, "chat", "model-a", "FAILED", 10, "{}", "{}",
             json.dumps({"project_id": project_id, "episode_id": f"e{index}"}), f"run-{index}"),
        )
        conn.execute(
            """INSERT INTO artifacts(id,type,scope_type,scope_id,version,status,trust_level,content_json,
                                      content_hash,parent_artifact_ids_json,model_snapshot_json,created_at)
               VALUES(?,?,?,?,1,'validated','T3','{}',?,'[]','{}',?)""",
            (f"art-{index}", "character_bible", "project", project_id, f"hash-{index}", index),
        )
    conn.commit()
    return conn


@pytest.fixture()
def scoped_db(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    conn = _database()
    monkeypatch.setattr(observability_api, "get_conn", lambda: conn)
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(orchestration_api, "get_conn", lambda: conn)
    return conn


def test_lists_and_counts_are_project_scoped(scoped_db) -> None:
    runs = observability_api.scoped_runs(
        "p1", page=1, page_size=20, search="", status=None, workflow=None,
        episode_no=None, from_ts=None, to_ts=None, include_history=True, sort="desc",
    )
    jobs = observability_api.scoped_jobs(
        "p1", page=1, page_size=20, search="", status=None, workflow=None,
        from_ts=None, to_ts=None, sort="desc",
    )
    calls = observability_api.scoped_calls(
        "p1", page=1, page_size=20, search="", status=None, category="business",
        function=None, model=None, from_ts=None, to_ts=None, sort="desc", ids=None,
    )

    assert [item["id"] for item in runs["items"]] == ["run-1"]
    assert [item["id"] for item in jobs["items"]] == ["run-1", "job-1"]
    assert jobs["counts"] == {"failed": 2}
    assert [item["id"] for item in calls["items"]] == [1]
    assert all(payload["scope"]["project_id"] == "p1" for payload in (runs, jobs, calls))


def test_scope_resolver_uses_run_links_and_rejects_conflicting_metadata(scoped_db) -> None:
    scoped_db.execute(
        """INSERT INTO provider_calls(ts,kind,status,latency_ms,meta,run_id)
           VALUES(3,'chat','OK',1,'{}','run-1')"""
    )
    scoped_db.execute(
        """INSERT INTO provider_calls(ts,kind,status,latency_ms,meta,run_id)
           VALUES(4,'chat','OK',1,?,'run-1')""",
        (json.dumps({"project_id": "p2"}),),
    )
    scoped_db.execute(
        "INSERT INTO jobs(id,kind,project_id,episode_id,status,created_at,updated_at) VALUES('job-conflict','video','p1','e2','failed',3,3)"
    )
    scoped_db.commit()

    calls = observability_api.scoped_calls(
        "p1", page=1, page_size=20, search="", status=None, category="business",
        function=None, model=None, from_ts=None, to_ts=None, sort="desc", ids=None,
    )
    jobs = observability_api.scoped_jobs(
        "p1", page=1, page_size=20, search="", status=None, workflow=None,
        from_ts=None, to_ts=None, sort="desc",
    )
    assert [item["id"] for item in calls["items"]] == [3, 1]
    assert 4 not in [item["id"] for item in calls["items"]]
    assert "job-conflict" not in [item["id"] for item in jobs["items"]]


@pytest.mark.parametrize(
    ("reader", "args"),
    [
        (observability_api.scoped_run, ("p1", "run-2")),
        (observability_api.scoped_job, ("p1", "job-2")),
        (observability_api.scoped_call, ("p1", 2)),
        (observability_api.scoped_artifact, ("p1", "art-2")),
    ],
)
def test_cross_project_details_are_indistinguishable_from_missing(scoped_db, reader, args) -> None:
    with pytest.raises(HTTPException) as exc:
        reader(*args)
    assert exc.value.status_code == 404
    assert "p2" not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_cross_project_action_is_blocked_before_dispatch(scoped_db, monkeypatch) -> None:
    dispatched = False

    async def fake_cancel(_run_id: str):
        nonlocal dispatched
        dispatched = True
        return {"ok": True}

    monkeypatch.setattr(orchestration_api, "cancel_run_route", fake_cancel)
    with pytest.raises(HTTPException) as exc:
        await observability_api.scoped_run_action("p1", "run-2", "cancel")
    assert exc.value.status_code == 404
    assert dispatched is False

    assert await observability_api.scoped_run_action("p1", "run-1", "cancel") == {"ok": True}
    assert dispatched is True


def test_cross_project_gate_decision_is_blocked(scoped_db, monkeypatch) -> None:
    called = False

    def fake_decision(_artifact_id: str, _body: dict):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(orchestration_api, "decide_gate", fake_decision)
    with pytest.raises(HTTPException) as exc:
        observability_api.scoped_gate_decision("p1", "art-2", {
            "decision": "approve", "reason": "不应执行",
        })
    assert exc.value.status_code == 404
    assert called is False


def test_system_overview_contains_aggregates_not_raw_records(scoped_db) -> None:
    payload = observability_api.system_overview()
    assert payload["totals"] == {
        "projects": 2, "jobs": 4, "calls": 2,
        "unattributed_jobs": 0, "unattributed_calls": 0,
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "request_json" not in serialized
    assert "response_json" not in serialized
    assert "run-1" not in serialized


def test_http_contract_keeps_scope_on_list_and_hides_foreign_detail(scoped_db) -> None:
    app = FastAPI()
    app.include_router(observability_api.router)
    client = TestClient(app)

    listing = client.get("/api/projects/p1/observability/runs?include_history=true")
    assert listing.status_code == 200
    assert listing.json()["scope"]["project_id"] == "p1"
    assert [item["id"] for item in listing.json()["items"]] == ["run-1"]
    assert client.get("/api/projects/p1/observability/runs/run-2").status_code == 404


def test_trace_tree_and_node_io_follow_persisted_links(scoped_db) -> None:
    scoped_db.execute(
        """INSERT INTO step_runs(
               id,run_id,step_key,status,input_artifact_ids_json,context_manifest_json,
               output_artifact_id,started_at,finished_at,latency_ms
           ) VALUES('step-1','run-1','generate','SUCCEEDED','["art-1"]',
                    '{"instruction":"生成剧本"}','art-1',1,2,1000)"""
    )
    scoped_db.execute(
        """INSERT INTO jobs(
               id,kind,project_id,episode_id,status,created_at,updated_at,run_id,step_run_id
           ) VALUES('job-trace','video','p1','e1','succeeded',1,2,'run-1','step-1')"""
    )
    scoped_db.execute(
        """UPDATE provider_calls
           SET step_run_id='step-1',
               request_json='{"api_key":"sk-secret-value","prompt":"原始提示词"}',
               response_json='{"result":"完成"}'
           WHERE id=1"""
    )
    scoped_db.commit()

    tree = observability_api._trace_tree("p1", "runs", "run-1")
    by_id = {item["id"]: item for item in tree["nodes"]}
    assert tree["scope"]["project_id"] == "p1"
    assert by_id["step:step-1"]["parent_id"] == "run:run-1"
    assert by_id["job:job-trace"]["parent_id"] == "step:step-1"
    assert by_id["call:1"]["parent_id"] == "step:step-1"

    step = observability_api._trace_node_detail(
        "p1", "runs", "run-1", "step:step-1", "auto",
    )
    assert step["input"]["context_manifest"]["instruction"] == "生成剧本"
    assert step["input"]["artifacts"][0]["id"] == "art-1"
    assert step["output"]["artifact"]["id"] == "art-1"

    call = observability_api._trace_node_detail(
        "p1", "calls", "1", "call:1", "auto",
    )
    assert call["input"]["api_key"] == "***"
    assert "sk-secret-value" not in json.dumps(call, ensure_ascii=False)
    assert call["output"]["response"]["result"] == "完成"


def test_trace_routes_reject_foreign_project_before_returning_tree(scoped_db) -> None:
    app = FastAPI()
    app.include_router(observability_api.router)
    client = TestClient(app)

    own = client.get("/api/projects/p1/observability/traces/runs/run-1")
    node = client.get(
        "/api/projects/p1/observability/traces/runs/run-1/nodes/run%3Arun-1"
    )
    foreign = client.get("/api/projects/p1/observability/traces/runs/run-2")
    assert own.status_code == 200
    assert own.json()["selected_node_id"] == "run:run-1"
    assert node.status_code == 200
    assert node.json()["id"] == "run:run-1"
    assert "input" in node.json() and "output" in node.json()
    assert foreign.status_code == 404
    assert "p2" not in foreign.text


def test_legacy_screenplay_trace_keeps_source_specific_node_io(scoped_db) -> None:
    scoped_db.execute(
        """UPDATE episodes
           SET screenplay_status='failed',screenplay_error='旧任务失败',
               screenplay_started_at=1,screenplay_updated_at=2
           WHERE id='e1'"""
    )
    scoped_db.commit()

    tree = observability_api._trace_tree(
        "p1", "jobs", "screenplay_e1", "screenplay",
    )
    detail = observability_api._trace_node_detail(
        "p1", "jobs", "screenplay_e1", "job:screenplay_e1", "screenplay",
    )

    assert tree["run_id"] is None
    assert tree["selected_node_id"] == "job:screenplay_e1"
    assert detail["input"]["episode_id"] == "e1"
    assert detail["output"]["status"] == "failed"
    assert detail["output"]["error"] == "旧任务失败"
