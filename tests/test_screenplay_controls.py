"""剧本台按钮必须映射到真实的 Baseline / Patch 后端阶段。"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app import api, db, task_registry
from app.capabilities import ensure_catalog_loaded
from app.capabilities.direct import enter_handler
from app.capabilities.registry import get_registry
from app.evidence import repository
from app.harness.types import EvidenceArtifact
from app.production.revision import (
    ensure_production_revision,
    mark_baseline_generated,
    save_checkpoint,
    screenplay_production_state,
    set_published_artifact,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-controls.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',?)",
        (db.now(),),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, screenplay_status, status, created_at) "
        "VALUES('e1','p1',1,'第一集','pending','planned',?)",
        (db.now(),),
    )
    conn.commit()
    yield


def test_production_state_resumes_post_baseline_stages() -> None:
    initial = screenplay_production_state("e1")
    assert initial["operation"] == "baseline"
    assert initial["baseline_done"] is False
    assert initial["can_resume_repair"] is False

    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    save_checkpoint(revision.id, {"phase": "GENERATING_BASELINE"})
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id="artifact-baseline",
        working_artifact_id="artifact-working",
    )

    finalize = screenplay_production_state("e1")
    assert finalize["operation"] == "finalize"
    assert finalize["phase"] == "STRUCTURE_VALIDATION"
    assert finalize["baseline_done"] is True
    assert finalize["can_resume_repair"] is True
    assert [item["label"] for item in finalize["stages"]] == [
        "人物识别", "叙事蓝图", "身份冻结", "全局包络", "场次写作",
        "全局编译", "结构校验", "质量评分", "原子发布", "已完成",
    ]

    save_checkpoint(revision.id, {
        "phase": "SUCCEEDED",
        "quality_score": 42.0,
        "quality_issue_count": 3,
        "gate_retry_exhausted": True,
    })
    set_published_artifact(revision.id, "artifact-working")
    completed = screenplay_production_state("e1")
    assert completed["operation"] == "complete"
    assert completed["phase"] == "SUCCEEDED"
    assert all(item["status"] == "completed" for item in completed["stages"])
    assert completed["quality_score"] == 42.0
    assert completed["quality_issue_count"] == 3


def test_production_state_exposes_resumable_scene_shard_checkpoint() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    save_checkpoint(revision.id, {
        "phase": "SCENE_SHARD_GENERATION",
        "blueprint_artifact_id": "art-blueprint",
        "identity_artifact_id": "art-identity",
        "envelope_artifact_id": "art-envelope",
        "yield_reason": "user_cancelled",
        "shards": [
            {"shard_id": "SS001", "status": "validated"},
            {"shard_id": "SS002", "status": "failed"},
            {"shard_id": "SS003", "status": "pending"},
        ],
    })
    state = screenplay_production_state("e1")
    assert state["operation"] == "baseline"
    assert state["baseline_done"] is False
    assert state["can_resume_baseline"] is True
    assert state["can_resume_repair"] is False
    assert state["shard_progress"] == {
        "total": 3,
        "validated": 1,
        "running": 0,
        "failed": 1,
    }
    assert state["yield_reason"] == "user_cancelled"


def test_resume_route_has_a_distinct_capability() -> None:
    ensure_catalog_loaded()
    registry = get_registry()
    assert registry.rest_bindings[
        "POST /api/episodes/{episode_id}/screenplay/resume"
    ] == "screenplay.resume"
    assert registry.commands["screenplay.resume"].title == "继续剧本流程"


def test_screenplay_generation_preflight_sizes_source_without_side_effects() -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','林舟推门。\\n他说：别走。',12)"
    )
    conn.execute("UPDATE episodes SET source_chapters='[1]' WHERE id='e1'")
    conn.commit()

    result = api._screenplay_generation_preflight("e1")

    assert result["action"] == "generate_screenplay"
    assert result["input"]["source_segment_count"] >= 1
    assert result["input"]["estimated_blueprint_shards"] >= 1
    assert result["input"]["estimated_scene_writing_shards"] >= 1
    assert conn.execute("SELECT COUNT(*) AS c FROM workflow_runs").fetchone()["c"] == 0


def test_screenplay_generate_preflight_allows_terminal_run_takeover() -> None:
    from app.capabilities.inputs import ScreenplayGenerateInput
    from app.capabilities.preflight import screenplay_generate

    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','林舟推门。',5)"
    )
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="failed-run",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='FAILED',failure_code='TEST' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET source_chapters='[1]',active_screenplay_run_id=? WHERE id='e1'",
        (run_id,),
    )
    conn.commit()

    terminal = screenplay_generate(ScreenplayGenerateInput(episode_id="e1"))

    assert terminal.allowed is True
    assert terminal.denial_code is None

    conn.execute("UPDATE workflow_runs SET status='RUNNING' WHERE id=?", (run_id,))
    conn.commit()
    live = screenplay_generate(ScreenplayGenerateInput(episode_id="e1"))

    assert live.allowed is False
    assert live.denial_code == "SCREENPLAY_ALREADY_RUNNING"
    assert live.state_fingerprint != terminal.state_fingerprint


@pytest.mark.asyncio
async def test_start_screenplay_replaces_terminal_run_owner(monkeypatch) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','林舟推门。',5)"
    )
    failed_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="failed-run",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='FAILED',failure_code='TEST' WHERE id=?",
        (failed_run_id,),
    )
    conn.execute(
        "UPDATE episodes SET source_chapters='[1]',screenplay_status='failed',"
        "active_screenplay_run_id=? WHERE id='e1'",
        (failed_run_id,),
    )
    conn.commit()

    class Recorder:
        run_id = "run_replacement"

        def cancel(self, _message: str) -> None:
            raise AssertionError("successful takeover must not cancel the new run")

    spawned: list[tuple[str, str]] = []

    def capture_spawn(kind, key, coro, *, project_id=None):
        spawned.append((kind, key))
        coro.close()

    monkeypatch.setattr(api, "_new_screenplay_recorder", lambda *args, **kwargs: Recorder())
    monkeypatch.setattr(task_registry, "spawn", capture_spawn)

    with enter_handler():
        result = await api.start_screenplay("e1", body={})

    episode = conn.execute(
        "SELECT screenplay_status,active_screenplay_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert result["run_id"] == "run_replacement"
    assert dict(episode) == {
        "screenplay_status": "queued",
        "active_screenplay_run_id": "run_replacement",
    }
    assert spawned == [("screenplay", "e1")]


def test_clear_unpublished_ir_preserves_published_lineage() -> None:
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
    )
    step_id = repository.create_step(
        run_id,
        "screenplay.iteration",
    )
    unpublished = repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id="e1",
            status="approved",
            trust_level="T2",
            content={"candidate": "retry-only"},
        ),
        step_run_id=step_id,
    )
    published_ir = repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id="e1",
            status="approved",
            trust_level="T2",
            content={"candidate": "published-source"},
        ),
        step_run_id=step_id,
    )
    repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="e1",
        status="approved",
        trust_level="T4",
        content={"published": True},
        parent_artifact_ids=[published_ir["id"]],
    ))
    conn = db.get_conn()
    conn.execute(
        "UPDATE step_runs SET output_artifact_id=? WHERE id=?",
        (unpublished["id"], step_id),
    )
    conn.commit()

    assert api._clear_unpublished_screenplay_ir("e1") == 1
    assert repository.get_artifact(unpublished["id"]) is None
    assert repository.get_artifact(published_ir["id"]) is not None
    assert conn.execute(
        "SELECT output_artifact_id FROM step_runs WHERE id=?",
        (step_id,),
    ).fetchone()["output_artifact_id"] is None


def test_failed_recovery_run_clears_only_its_ir_lineage() -> None:
    parent_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
    )
    child_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
        parent_run_id=parent_run_id,
    )
    unrelated_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="other-input",
    )
    artifacts = []
    for run_id, label in (
        (parent_run_id, "parent"),
        (child_run_id, "child"),
        (unrelated_run_id, "unrelated"),
    ):
        step_id = repository.create_step(run_id, "screenplay.iteration")
        artifacts.append(repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_generation_ir",
                scope_type="episode",
                scope_id="e1",
                status="approved",
                trust_level="T2",
                content={"candidate": label},
            ),
            step_run_id=step_id,
        ))

    assert api._clear_unpublished_screenplay_ir(
        "e1",
        run_id=child_run_id,
    ) == 2
    assert repository.get_artifact(artifacts[0]["id"]) is None
    assert repository.get_artifact(artifacts[1]["id"]) is None
    assert repository.get_artifact(artifacts[2]["id"]) is not None


def test_exhausted_repair_discards_ir_and_active_revision() -> None:
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
    )
    step_id = repository.create_step(run_id, "screenplay.iteration")
    ir = repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id="e1",
            status="approved",
            trust_level="T2",
            content={"candidate": "repair-working"},
        ),
        step_run_id=step_id,
    )
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=ir["id"],
        working_artifact_id=ir["id"],
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing', "
        "active_screenplay_run_id=?, working_screenplay_artifact_id=?, "
        "screenplay_production_revision_id=? WHERE id='e1'",
        (run_id, ir["id"], revision.id),
    )
    conn.commit()

    deleted = api._discard_exhausted_screenplay_working_state(
        "e1",
        run_id=run_id,
        message="修复轮次耗尽",
    )

    assert deleted == 1
    assert repository.get_artifact(ir["id"]) is None
    row = conn.execute(
        "SELECT screenplay_status,active_screenplay_run_id,"
        "working_screenplay_artifact_id,screenplay_production_revision_id "
        "FROM episodes WHERE id='e1'",
    ).fetchone()
    assert dict(row) == {
        "screenplay_status": "failed",
        "active_screenplay_run_id": None,
        "working_screenplay_artifact_id": None,
        "screenplay_production_revision_id": None,
    }
    assert conn.execute(
        "SELECT status FROM production_revisions WHERE id=?",
        (revision.id,),
    ).fetchone()["status"] == "superseded"


def test_atomic_claim_does_not_clear_current_owner_ir() -> None:
    owner_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="owner",
    )
    step_id = repository.create_step(
        owner_run_id,
        "screenplay.iteration",
    )
    ir = repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id="e1",
            status="approved",
            trust_level="T2",
            content={"candidate": "current-owner"},
        ),
        step_run_id=step_id,
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_status='queued', "
        "active_screenplay_run_id=? WHERE id='e1'",
        (owner_run_id,),
    )
    conn.commit()

    class Recorder:
        run_id = "late-run"
        cancelled = False

        def cancel(self, _message: str) -> None:
            self.cancelled = True

    recorder = Recorder()
    with pytest.raises(api.StateConflict):
        api._spawn_screenplay_activation(
            "e1",
            recorder,
            project_id="p1",
            status="queued",
            message="late",
            clear_unpublished_ir=True,
        )

    assert recorder.cancelled is True
    assert repository.get_artifact(ir["id"]) is not None
    assert conn.execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id='e1'",
    ).fetchone()["active_screenplay_run_id"] == owner_run_id


@pytest.mark.asyncio
async def test_first_screenplay_spawn_failure_restores_state_and_legacy_columns(
    monkeypatch,
) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','第一章\\n林舟说：别走。',12)"
    )
    conn.execute(
        "UPDATE episodes SET source_chapters='[1]', screenplay_error='上次提示', "
        "screenplay_started_at=10, screenplay_updated_at=11, "
        "screenplay_character_resolutions=?, screenplay_required_dialogues=?, "
        "screenplay_required_dialogue_occurrences=? WHERE id='e1'",
        (
            json.dumps([{
                "source_label": "旧称谓",
                "canonical_name": "路人甲",
                "resolution": "functional_extra",
            }], ensure_ascii=False),
            json.dumps(["旧台词"], ensure_ascii=False),
            json.dumps(["legacy-occurrence"], ensure_ascii=False),
        ),
    )
    conn.commit()
    unpublished_ir = repository.create_artifact(EvidenceArtifact(
        type="screenplay_generation_ir",
        scope_type="episode",
        scope_id="e1",
        status="candidate",
        trust_level="T1",
        content={"candidate": "must-survive-spawn-failure"},
    ))

    class Recorder:
        run_id = "run_not_started"
        cancelled = False

        def cancel(self, _message: str) -> None:
            self.cancelled = True

    recorder = Recorder()

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(api, "_new_screenplay_recorder", lambda *args, **kwargs: recorder)
    monkeypatch.setattr(task_registry, "spawn", fail_spawn)

    with enter_handler(), pytest.raises(HTTPException) as exc_info:
        await api.start_screenplay("e1", body={})

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["action"] == "retry_generate"
    row = conn.execute(
        "SELECT screenplay_status,screenplay_error,screenplay_started_at,"
        "screenplay_updated_at,active_screenplay_run_id,screenplay_required_dialogues,"
        "screenplay_required_dialogue_occurrences,screenplay_character_resolutions "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert row["screenplay_status"] == "pending"
    assert row["screenplay_error"] == "上次提示"
    assert row["screenplay_started_at"] == 10
    assert row["screenplay_updated_at"] == 11
    assert row["active_screenplay_run_id"] is None
    assert json.loads(row["screenplay_required_dialogues"]) == ["旧台词"]
    assert json.loads(row["screenplay_required_dialogue_occurrences"]) == ["legacy-occurrence"]
    assert json.loads(row["screenplay_character_resolutions"])[0][
        "source_label"
    ] == "旧称谓"
    assert recorder.cancelled is True
    assert repository.get_artifact(unpublished_ir["id"]) is not None


@pytest.mark.asyncio
async def test_fresh_screenplay_clears_stale_identity_and_legacy_dialogue_selection(
    monkeypatch,
) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','第一章\\n小胖子站在门口。',12)"
    )
    conn.execute(
        "UPDATE episodes SET source_chapters='[1]',screenplay_status='failed',"
        "screenplay_character_resolutions=?,screenplay_required_dialogues=?,"
        "screenplay_required_dialogue_occurrences=? WHERE id='e1'",
        (
            json.dumps([{
                "source_label": "小胖子",
                "canonical_name": "路人甲",
                "resolution": "functional_extra",
            }], ensure_ascii=False),
            json.dumps(["旧台词"], ensure_ascii=False),
            json.dumps(["legacy-occurrence"], ensure_ascii=False),
        ),
    )
    conn.commit()

    class Recorder:
        run_id = "run_fresh"

    seen: dict[str, object] = {}

    def fake_spawn(_kind, _key, coro, *, project_id=None):
        row = conn.execute(
            "SELECT screenplay_character_resolutions,screenplay_required_dialogues,"
            "screenplay_required_dialogue_occurrences FROM episodes WHERE id='e1'"
        ).fetchone()
        seen["resolutions"] = json.loads(row["screenplay_character_resolutions"])
        seen["legacy_dialogues"] = json.loads(row["screenplay_required_dialogues"])
        seen["legacy_occurrences"] = json.loads(
            row["screenplay_required_dialogue_occurrences"]
        )
        coro.close()

    monkeypatch.setattr(
        api,
        "_new_screenplay_recorder",
        lambda *_args, **_kwargs: Recorder(),
    )
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    with enter_handler():
        result = await api.start_screenplay("e1", body={})

    assert result["mode"] == "baseline"
    assert seen["resolutions"] == []
    assert seen["legacy_dialogues"] == []
    assert seen["legacy_occurrences"] == []


def test_recovery_resumes_repair_interrupted_by_service_restart(monkeypatch) -> None:
    conn = db.get_conn()
    parent_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="repair",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='PAUSED_EXTERNAL', failure_code='SERVICE_RESTART' "
        "WHERE id=?",
        (parent_run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing', screenplay_error='修复到第 2 步', "
        "active_screenplay_run_id=? WHERE id='e1'",
        (parent_run_id,),
    )
    conn.commit()
    seen: dict[str, object] = {}

    class Recorder:
        run_id = "run_recovered"

    def fake_recorder(*_args, **kwargs):
        seen["parent_run_id"] = kwargs.get("parent_run_id")
        return Recorder()

    def fake_spawn(kind, key, coro, *, project_id=None):
        seen["spawn"] = (kind, key, project_id)
        coro.close()
        return None

    monkeypatch.setattr(api, "_new_screenplay_recorder", fake_recorder)
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    assert api.recover_screenplay_tasks() == 1
    row = conn.execute(
        "SELECT screenplay_status,screenplay_error,active_screenplay_run_id "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(row) == {
        "screenplay_status": "queued",
        "screenplay_error": "恢复任务已排队，等待文本生成槽位",
        "active_screenplay_run_id": "run_recovered",
    }
    assert seen == {
        "parent_run_id": parent_run_id,
        "spawn": ("screenplay", "e1", "p1"),
    }


def test_recovery_does_not_resume_obsolete_contract_revision(monkeypatch) -> None:
    conn = db.get_conn()
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        input_fingerprint="old-input",
        contract_version="2.0.0",
        qa_profile_version="screenplay-qa-gate-2",
        resume=False,
    )
    parent_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="repair",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='PAUSED_EXTERNAL',failure_code='SERVICE_RESTART' "
        "WHERE id=?",
        (parent_run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing',active_screenplay_run_id=?,"
        "screenplay_production_revision_id=? WHERE id='e1'",
        (parent_run_id, revision.id),
    )
    conn.commit()
    monkeypatch.setattr(
        api,
        "_new_screenplay_recorder",
        lambda *_args, **_kwargs: pytest.fail("旧合同禁止自动恢复"),
    )

    assert api.recover_screenplay_tasks() == 0
    episode = conn.execute(
        "SELECT screenplay_status,screenplay_error,active_screenplay_run_id "
        "FROM episodes WHERE id='e1'",
    ).fetchone()
    assert episode["screenplay_status"] == "repairing"
    assert "旧合同 2.0.0" in episode["screenplay_error"]
    assert "当前合同为 4.0.0" in episode["screenplay_error"]
    assert episode["active_screenplay_run_id"] is None


def test_recovery_does_not_restart_intentionally_paused_repair(monkeypatch) -> None:
    conn = db.get_conn()
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="repair",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='PARTIAL', failure_code='PARTIAL_RESULT' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing', "
        "screenplay_error='恢复点已保存，可继续', active_screenplay_run_id=? WHERE id='e1'",
        (run_id,),
    )
    conn.commit()
    monkeypatch.setattr(
        api,
        "_new_screenplay_recorder",
        lambda *_args, **_kwargs: pytest.fail("不应自动重启主动暂停的修复"),
    )

    assert api.recover_screenplay_tasks() == 0


def test_recovery_does_not_restart_persisted_cancellation(monkeypatch) -> None:
    conn = db.get_conn()
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="cancelled",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='CANCELLED' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='running', "
        "screenplay_error='CANCELLING: 正在取消运行', active_screenplay_run_id=? "
        "WHERE id='e1'",
        (run_id,),
    )
    conn.commit()
    monkeypatch.setattr(
        api,
        "_new_screenplay_recorder",
        lambda *_args, **_kwargs: pytest.fail("用户取消的任务不应在重启后恢复"),
    )

    assert api.recover_screenplay_tasks() == 0


@pytest.mark.asyncio
async def test_batch_start_reports_partial_failure_without_stranding_episode(
    monkeypatch,
) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,screenplay_status,status,created_at) "
        "VALUES('e2','p1',2,'第二集','running','planned',?)",
        (db.now(),),
    )
    conn.commit()

    class Recorder:
        def __init__(self, run_id: str):
            self.run_id = run_id
            self.cancelled = False

        def cancel(self, _message: str) -> None:
            self.cancelled = True

    recorders: dict[str, Recorder] = {}

    def fake_recorder(episode_id: str, **_kwargs):
        recorder = Recorder(f"run_{episode_id}")
        recorders[episode_id] = recorder
        return recorder

    def fake_spawn(_kind, key, coro, *, project_id=None):
        coro.close()
        if key == "e2":
            raise RuntimeError("queue unavailable")
        return None

    monkeypatch.setattr(api, "_new_screenplay_recorder", fake_recorder)
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    cleared: list[str] = []
    monkeypatch.setattr(
        api,
        "_clear_unpublished_screenplay_ir",
        lambda episode_id, **_kwargs: cleared.append(episode_id) or 0,
    )
    with enter_handler():
        result = await api.start_screenplay_all("p1")

    assert result["started"] == 1
    assert result["batch_run_id"].startswith("run_")
    assert result["retryable_failures"] == 1
    assert result["failed_to_start"][0]["episode_id"] == "e2"
    rows = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT id,screenplay_status,active_screenplay_run_id FROM episodes ORDER BY id"
        ).fetchall()
    }
    assert rows["e1"]["screenplay_status"] == "queued"
    assert rows["e1"]["active_screenplay_run_id"] == "run_e1"
    assert rows["e2"]["screenplay_status"] == "failed"
    assert rows["e2"]["active_screenplay_run_id"] is None
    assert recorders["e2"].cancelled is True
    assert cleared == ["e1", "e2"]
    batch = conn.execute(
        "SELECT workflow_type,scope_id,status FROM workflow_runs WHERE id=?",
        (result["batch_run_id"],),
    ).fetchone()
    assert tuple(batch) == ("screenplay_batch", "p1", "RUNNING")
