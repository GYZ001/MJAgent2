"""监制房整改 PRD 的高风险契约回归。"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, monitoring, system_api
from app.evidence import repository
from app.orchestration import api as orchestration_api
from app.local_session import public_session_payload


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for key, value in db.DEFAULT_SETTINGS.items():
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
    conn.commit()
    return conn


def _patch_conn(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)
    monkeypatch.setattr(monitoring, "get_conn", lambda: conn)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(orchestration_api, "get_conn", lambda: conn)


def test_settings_schema_rejects_illegal_values_and_dependency_conflicts(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)

    for patch in (
        {"video_submit_concurrency": "abc"},
        {"video_submit_concurrency": "Infinity"},
        {"video_submit_concurrency": 0},
        {"text_generation_concurrency": 17},
        {"auto_qa": "yes"},
        {"media_scheduler_policy": "random"},
        {"video_ready_low_watermark": 9, "video_ready_high_watermark": 3},
        {"episode_video_inflight_limit": 20, "project_video_inflight_limit": 10},
        {"undeclared_setting": "1"},
    ):
        with pytest.raises(HTTPException) as exc:
            system_api.put_settings({"version": 0, "patch": patch})
        assert exc.value.status_code == 422

    assert conn.execute(
        "SELECT value FROM settings WHERE key='video_submit_concurrency'"
    ).fetchone()["value"] == db.DEFAULT_SETTINGS["video_submit_concurrency"]


def test_settings_save_is_versioned_authoritative_and_atomic(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    from app import worker
    from app.media_pipeline import concurrency

    monkeypatch.setattr(concurrency, "reload_limits_from_settings", lambda: None)
    monkeypatch.setattr(worker, "ensure_workers", lambda: None)
    result = system_api.put_settings({
        "version": 0,
        "patch": {"video_submit_concurrency": "020", "provider_call_retention_days": 60},
    })
    assert result["version"] == 1
    assert {item["key"]: item for item in result["items"]}["video_submit_concurrency"] == {
        "key": "video_submit_concurrency", "requested": "20", "effective": "20", "apply_mode": "immediate",
    }
    retention = {item["key"]: item for item in result["items"]}["provider_call_retention_days"]
    assert retention["apply_mode"] == "restart"
    assert retention["effective"] == "30"
    settings_view = system_api.get_settings(include_schema=True)
    assert settings_view["values"]["provider_call_retention_days"] == "60"
    assert settings_view["effective"]["provider_call_retention_days"] == "30"

    with pytest.raises(HTTPException) as conflict:
        system_api.put_settings({"version": 0, "patch": {"video_submit_concurrency": 21}})
    assert conflict.value.status_code == 409
    assert conn.execute("SELECT value FROM settings WHERE key='video_submit_concurrency'").fetchone()[0] == "20"

    monkeypatch.setattr(concurrency, "reload_limits_from_settings", lambda: (_ for _ in ()).throw(RuntimeError("apply failed")))
    with pytest.raises(HTTPException) as failed:
        system_api.put_settings({"version": 1, "patch": {"video_submit_concurrency": 22}})
    assert failed.value.status_code == 503
    assert conn.execute("SELECT value FROM settings WHERE key='video_submit_concurrency'").fetchone()[0] == "20"
    assert conn.execute("SELECT value FROM settings WHERE key='_monitor_config_version'").fetchone()[0] == "1"


def test_text_generation_concurrency_hot_resizes_existing_queue(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    conn.execute(
        "UPDATE settings SET value='2' WHERE key='text_generation_concurrency'"
    )
    conn.commit()
    from app import generation_concurrency, worker
    from app.media_pipeline import concurrency

    reloads: list[str] = []
    monkeypatch.setattr(concurrency, "reload_limits_from_settings", lambda: None)
    monkeypatch.setattr(worker, "ensure_workers", lambda: None)
    monkeypatch.setattr(
        generation_concurrency,
        "reload_generation_limits",
        lambda: reloads.append("text") or 1,
    )

    result = system_api.put_settings({
        "version": 0,
        "patch": {"text_generation_concurrency": 10},
    })

    assert reloads == ["text"]
    assert result["items"] == [{
        "key": "text_generation_concurrency",
        "requested": "10",
        "effective": "10",
        "apply_mode": "immediate",
    }]
    assert conn.execute(
        "SELECT value FROM settings WHERE key='text_generation_concurrency'"
    ).fetchone()[0] == "10"


def test_independent_release_switches_fail_safe(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    monkeypatch.setenv("MONITOR_OVERVIEW_V2_ENABLED", "false")
    monkeypatch.setenv("MONITOR_CALL_DETAIL_V2_ENABLED", "off")
    monkeypatch.setenv("MONITOR_SETTINGS_EDIT_V2_ENABLED", "0")

    flags = system_api.get_settings(include_schema=True)["features"]
    assert flags["overview_state_v2"] is False
    assert flags["jobs_query_v2"] is True
    assert flags["call_detail_v2"] is False
    with pytest.raises(HTTPException) as readonly:
        system_api.put_settings({
            "version": 0, "patch": {"video_submit_concurrency": 5},
        })
    assert readonly.value.status_code == 503
    with pytest.raises(HTTPException) as detail_disabled:
        system_api.call_detail(1)
    assert detail_disabled.value.status_code == 503


def test_calls_query_is_full_count_summary_only_and_detail_is_redacted(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    for index in range(225):
        conn.execute(
            """INSERT INTO provider_calls(
                   ts,kind,model,status,http_status,latency_ms,error,
                   request_json,response_json,meta,project_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                float(index), "chat", "model-a", "FAILED" if index == 224 else "OK", 500 if index == 224 else 200,
                10, "boom" if index == 224 else None,
                json.dumps({"api_key": "sk-supersecret", "path": "/Users/alice/project/input.txt", "prompt": "hello"}),
                json.dumps({"authorization": "Bearer abcdefghijklmnop", "result": "ok"}),
                json.dumps({"project_id": "p1", "episode_no": 1}),
                "p1",
            ),
        )
    conn.commit()

    first_page = system_api.query_calls(
        page=1, page_size=20, search="", status=None, category="business", project_id=None,
        function=None, model=None, from_ts=None, to_ts=None, sort="desc",
    )
    deep_page = system_api.query_calls(
        page=12, page_size=20, search="", status=None, category="business", project_id=None,
        function=None, model=None, from_ts=None, to_ts=None, sort="desc",
    )
    assert first_page["total"] == 225
    assert deep_page["items"][-1]["id"] == 1
    assert "request_json" not in first_page["items"][0]
    assert first_page["aggregates"][0]["count"] == 1
    assert first_page["failed_total"] == 1
    project_page = system_api.query_calls(
        page=1, page_size=20, search="", status=None, category="business", project_id="p1",
        function=None, model=None, from_ts=None, to_ts=None, sort="desc",
    )
    assert project_page["total"] == 225

    detail = system_api.call_detail(225)
    combined = " ".join(str(detail[field]) for field in ("request_json", "response_json", "meta"))
    assert "sk-supersecret" not in combined
    assert "/Users/alice" not in combined
    assert "abcdefghijklmnop" not in combined
    assert "***" in combined and "[本机路径已隐藏]" in combined
    assert detail["raw_access"] is False

    protected_app = FastAPI()
    protected_app.include_router(system_api.router)
    client = TestClient(protected_app)
    assert client.get("/api/system/calls/225").status_code == 401
    headers = {"X-Manju-Session": public_session_payload()["session_token"]}
    assert client.get("/api/system/calls/225", headers=headers).status_code == 200
    download = client.get("/api/system/calls/225/download", headers=headers)
    assert download.status_code == 200
    assert "sk-supersecret" not in download.text


def test_jobs_and_runs_are_queryable_past_legacy_caps(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','项目一','created',1)")
    for index in range(230):
        conn.execute(
            "INSERT INTO jobs(id,kind,project_id,status,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (f"job-{index:03d}", "video", "p1", "failed" if index == 0 else "succeeded", None, index, index),
        )
    for index in range(70):
        conn.execute(
            """INSERT INTO workflow_runs(id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (f"run-{index:03d}", "character_bible", "project", "p1", "SUCCEEDED", f"fp-{index}", index),
        )
    conn.commit()

    jobs = system_api.query_jobs(
        page=15, page_size=20, search="", status=None, project_id=None, workflow=None,
        from_ts=None, to_ts=None, sort="desc",
    )
    assert jobs["total"] == 300
    assert len(jobs["items"]) == 20
    assert any(item["id"] == "job-000" for item in jobs["items"])
    grouped = system_api.query_jobs(
        page=1, page_size=100, search="", status="failed,partial", project_id=None,
        workflow=None, from_ts=None, to_ts=None, sort="desc",
    )
    assert grouped["total"] == 1
    deep_linked_job = system_api.job_detail("job-000", source="auto")
    assert deep_linked_job["id"] == "job-000"
    assert deep_linked_job["source"] == "job"

    runs = orchestration_api.query_runs(
        page=4, page_size=20, search="", status=None, project_id=None, workflow=None,
        episode_no=None, from_ts=None, to_ts=None, include_history=True, sort="desc",
    )
    assert runs["total"] == 70
    assert runs["items"][-1]["id"] == "run-000"
    deep_linked_run = system_api.job_detail("run-000", source="auto")
    assert deep_linked_run["id"] == "run-000"
    assert deep_linked_run["source"] == "run"
    assert deep_linked_run["status"] == "succeeded"


def test_gate_decision_is_versioned_and_idempotent(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p1','项目一','created',1)")
    conn.execute(
        """INSERT INTO artifacts(id,type,scope_type,scope_id,version,status,trust_level,content_json,
                                  content_hash,parent_artifact_ids_json,model_snapshot_json,created_at)
           VALUES('art-1','character_bible','project','p1',1,'validated','T3','{}','hash','[]','{}',1)"""
    )
    conn.commit()

    result = orchestration_api.decide_gate("art-1", {
        "decision": "approve", "reason": "内容符合制作目标", "expected_version": 1,
        "idempotency_key": "gate-art-1-v1",
    })
    assert result["ok"] is True
    assert conn.execute("SELECT status FROM artifacts WHERE id='art-1'").fetchone()[0] == "approved"
    assert conn.execute("SELECT COUNT(*) FROM gate_decisions WHERE artifact_id='art-1'").fetchone()[0] == 1

    repeated = orchestration_api.decide_gate("art-1", {
        "decision": "approve", "reason": "重复提交", "expected_version": 1,
        "idempotency_key": "gate-art-1-v1",
    })
    assert repeated["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM gate_decisions WHERE artifact_id='art-1'").fetchone()[0] == 1
