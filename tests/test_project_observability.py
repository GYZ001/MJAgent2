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
            """INSERT INTO provider_calls(
                   ts,kind,model,status,latency_ms,request_json,response_json,
                   meta,project_id,run_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (index, "chat", "model-a", "FAILED", 10, "{}", "{}",
             json.dumps({"project_id": project_id, "episode_id": f"e{index}"}),
             project_id, f"run-{index}"),
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
        """INSERT INTO provider_calls(
               ts,kind,status,latency_ms,meta,project_id,run_id
           ) VALUES(3,'chat','OK',1,'{}','p1','run-1')"""
    )
    scoped_db.execute(
        """INSERT INTO provider_calls(
               ts,kind,status,latency_ms,meta,project_id,run_id
           ) VALUES(4,'chat','OK',1,?,'p2','run-1')""",
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
               response_json='{"result":"完成"}',
               meta='{"stage":"discover_character_candidates","discovery_phase":"current"}'
           WHERE id=1"""
    )
    scoped_db.execute(
        """INSERT INTO provider_calls(
               ts,kind,model,status,latency_ms,request_json,response_json,meta,run_id
           ) VALUES(
               1.5,'storyboard_candidate_normalization','local','NORMALIZED',5,
               NULL,NULL,'{"changes":{"count":2},"episode_id":"e1"}','run-1'
           )"""
    )
    scoped_db.commit()

    tree = observability_api._trace_tree("p1", "runs", "run-1")
    by_id = {item["id"]: item for item in tree["nodes"]}
    assert tree["scope"]["project_id"] == "p1"
    assert by_id["step:step-1"]["parent_id"] == "run:run-1"
    assert by_id["step:step-1"]["node_role"] == "business_stage"
    assert by_id["job:job-trace"]["parent_id"] == "step:step-1"
    assert by_id["call:1"]["parent_id"] == "step:step-1"
    assert by_id["call:1"]["node_role"] == "model_processing"
    assert by_id["call:1"]["name"] == "提取本集人物候选"
    assert by_id["call:1"]["subtitle"] == "通过文本生成模型"
    program = next(
        item
        for item in tree["nodes"]
        if item["id"] != "call:1"
        and item["kind"] == "call"
        and item["node_role"] == "program_processing"
    )
    group = by_id[program["parent_id"]]
    assert group["kind"] == "stage"
    assert group["node_role"] == "business_stage"
    assert group["parent_id"] == "run:run-1"
    assert all(
        by_id[child_id]["node_role"] == "business_stage"
        for child_id in [
            item["id"]
            for item in tree["nodes"]
            if item["parent_id"] == "run:run-1"
        ]
    )

    step = observability_api._trace_node_detail(
        "p1", "runs", "run-1", "step:step-1", "auto",
    )
    assert step["input"]["context_manifest"]["instruction"] == "生成剧本"
    assert step["input"]["artifacts"][0]["id"] == "art-1"
    assert step["output"]["artifact"]["id"] == "art-1"

    call = observability_api._trace_node_detail(
        "p1", "calls", "1", "call:1", "auto",
    )
    assert call["input"]["api_key"] == "sk-secret-value"
    assert call["input"]["prompt"] == "原始提示词"
    assert "已隐藏" not in json.dumps(call, ensure_ascii=False)
    assert "***" not in json.dumps(call, ensure_ascii=False)
    assert call["output"]["response"]["result"] == "完成"

    program_detail = observability_api._trace_node_detail(
        "p1", "runs", "run-1", program["id"], "auto",
    )
    assert program_detail["input"] == {
        "execution_context": {
            "changes": {"count": 2},
            "episode_id": "e1",
        },
        "payload_recorded": False,
    }
    assert program_detail["output"]["status"] == "NORMALIZED"
    assert program_detail["output"]["payload_recorded"] is False

    project_detail = observability_api.scoped_call("p1", 1)
    project_download = observability_api.scoped_call_download("p1", 1)
    assert "sk-secret-value" in project_detail["request_json"]
    assert "原始提示词" in project_detail["request_json"]
    assert "sk-secret-value" in project_download
    assert project_detail["raw_access"] is True


def test_trace_labels_hide_technical_keys_but_keep_them_in_metadata() -> None:
    assert observability_api._trace_step_label("character_discovery") == "识别本集出场人物"
    assert observability_api._trace_step_label("screenplay_blueprint") == "规划全剧剧情结构"
    assert observability_api._trace_step_label("screenplay_identity_freeze") == "统一人物身份与别名"
    assert observability_api._trace_step_label("screenplay_envelope") == "规划全剧叙事框架"
    assert observability_api._trace_step_label("screenplay_scene_shards") == "逐场撰写剧本"
    assert observability_api._trace_step_label("screenplay_merge") == "合并并校验完整剧本"
    assert observability_api._trace_step_label("screenplay_document") == "生成并验收分集映射包"
    assert observability_api._trace_step_label("character_bible_roster.iteration", 2) == "执行第2轮人物名单生成"
    assert (
        observability_api._trace_step_label("storyboard_shot_12.iteration", 2)
        == "生成第12镜分镜"
    )
    assert (
        observability_api._trace_step_label("unknown_internal_step")
        == "业务名称待配置（unknown_internal_step）"
    )

    name, role, method = observability_api._trace_call_semantics("val422_metric")
    assert (name, role, method) == (
        "记录结构校验指标",
        "program_processing",
        "通过本地结构校验",
    )
    name, role, method = observability_api._trace_call_semantics("future_prompt")
    assert (name, role, method) == (
        "生成当前业务环节所需内容",
        "model_processing",
        "通过业务生成模型",
    )

    name, role, method = observability_api._trace_call_semantics(
        "chat",
        {
            "stage": "剧本时空因果蓝图分片",
            "shard_index": 2,
            "shard_count": 5,
            "attempt": 3,
        },
        "生成剧本",
    )
    assert (name, role, method) == (
        "生成剧本时空因果蓝图（第 2/5 片，第 3 次尝试）",
        "model_processing",
        "通过文本生成模型",
    )

    name, role, method = observability_api._trace_call_semantics(
        "chat",
        {
            "stage": "剧本场次分片",
            "shard_id": "SS003",
            "shard_count": 8,
        },
        "逐场撰写剧本",
    )
    assert (name, role, method) == (
        "逐场撰写剧本（场次分片 SS003，共 8 片）",
        "model_processing",
        "通过文本生成模型",
    )

    name, role, method = observability_api._trace_call_semantics(
        "val422_metric",
        {"metric": "repair_activation_total"},
        "生成剧本",
    )
    assert (name, role, method) == (
        "记录剧本修复启动次数",
        "program_processing",
        "通过本地结构校验",
    )


def test_video_completion_trace_explains_business_stages_and_shot_work(
    scoped_db,
) -> None:
    scoped_db.execute(
        """UPDATE workflow_runs
           SET workflow_type='episode_video_completion',status='RUNNING',
               requested_by='user',trigger_type='manual',
               policy_snapshot_json='{"budget_cap_cny":150}',
               budget_limit_cny=150,started_at=10,updated_at=20
           WHERE id='run-1'"""
    )
    scoped_db.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s)
           VALUES('shot-1','e1',1,5)"""
    )
    scoped_db.execute(
        """INSERT INTO provider_video_capability_snapshots(
               id,provider,model,capabilities_json,probe_time,probe_result,
               technical_success,created_at
           ) VALUES('cap-1','provider','model','{}',10,'succeeded',1,10)"""
    )
    scoped_db.execute(
        """INSERT INTO episode_video_generation_plans(
               id,episode_id,plan_revision,source_storyboard_revision_id,
               capability_snapshot_id,status,estimated_cost,
               estimated_latency_ms,created_at
           ) VALUES(
               'plan-1','e1',3,'board-1','cap-1','valid',3.5,9000,12
           )"""
    )
    scoped_db.execute(
        """INSERT INTO shot_video_generation_plans(
               id,episode_video_plan_id,shot_id,shot_no,planned_mode,
               capability_snapshot_id,status,created_at,updated_at
           ) VALUES(
               'shot-plan-1','plan-1','shot-1',1,'FIRST_LAST_FRAME_MODE',
               'cap-1','planned',12,12
           )"""
    )
    scoped_db.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,
               started_at,updated_at
           ) VALUES(
               'run-video-1','video_generation','shot','shot-1','RUNNING',
               'video-fp',13,13
           )"""
    )
    scoped_db.execute(
        """INSERT INTO jobs(
               id,kind,project_id,episode_id,shot_id,status,pipeline_stage,
               stage_status,run_id,owner_run_id,created_at,updated_at
           ) VALUES(
               'job-video-1','video','p1','e1','shot-1','running',
               'video_generating','active','run-video-1','run-1',13,14
           )"""
    )
    scoped_db.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,
               content_json,content_hash,parent_artifact_ids_json,
               model_snapshot_json,created_at
           ) VALUES(
               'checkpoint-1','video_supervisor_checkpoint','episode','e1',1,
               'validated','T2',?,'checkpoint-hash','[]','{}',14
           )""",
        (json.dumps({
            "run_id": "run-1",
            "phase": "PLANNING_COVERAGE",
            "tick_no": 1,
            "repair_epoch": 0,
            "episode_video_plan_id": "plan-1",
            "grant_id": "grant-1",
            "budget": {"cap_cny": 150, "spent_cny": 0},
            "coverage": {
                "total": 1, "adopted": 0, "A": 0, "B": 0, "C": 1,
            },
        }, ensure_ascii=False),),
    )
    scoped_db.commit()

    tree = observability_api._trace_tree("p1", "runs", "run-1")
    by_id = {item["id"]: item for item in tree["nodes"]}
    stage_nodes = [
        item for item in tree["nodes"]
        if item["parent_id"] == "run:run-1"
    ]
    assert [item["name"] for item in sorted(
        stage_nodes, key=lambda item: item["sequence"],
    )] == [
        "确认补齐范围与生成授权",
        "制定全片视频生成方案",
        "核对人物、场景与连续性素材",
        "盘点全片缺口与生成顺序",
        "逐镜生成视频",
        "检查质量并自动修复",
        "验收全片覆盖并收口",
    ]
    assert "run:run-video-1" not in by_id
    video_job = by_id["job:job-video-1"]
    assert video_job["parent_id"] == "stage:run-1:video-shots"
    assert video_job["name"] == "第 1 镜 · 视频模型生成中"
    assert video_job["subtitle"] == "首尾帧控制 · 第 1 次执行"
    assert by_id["stage:run-1:video-shots"]["subtitle"] == (
        "已派发 1/1 镜 · 当前处理中 1 镜"
    )

    plan_detail = observability_api._trace_node_detail(
        "p1", "runs", "run-1", "stage:run-1:video-plan", "auto",
    )
    assert plan_detail["output"]["plan_revision"] == 3
    assert plan_detail["output"]["mode_distribution"] == {"首尾帧控制": 1}
    coverage_detail = observability_api._trace_node_detail(
        "p1", "runs", "run-1", "stage:run-1:video-coverage", "auto",
    )
    assert coverage_detail["output"]["phase_name"] == "盘点待补镜头"
    assert coverage_detail["output"]["coverage"]["total"] == 1
    job_detail = observability_api._trace_node_detail(
        "p1", "runs", "run-1", "job:job-video-1", "auto",
    )
    assert job_detail["input"]["shot_no"] == 1
    assert job_detail["input"]["generation_plan"]["generation_mode"] == "首尾帧控制"
    assert job_detail["output"]["current_action"] == "视频模型生成中"


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


def test_legacy_run_link_resolves_to_task_queue(scoped_db) -> None:
    resolved = observability_api.resolve_legacy_observability(run_id="run-1")

    assert resolved == {
        "project_id": "p1",
        "section": "jobs",
        "object_id": "run-1",
    }


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
