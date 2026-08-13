"""Capability Registry / Command Bus 合同测试。"""
from __future__ import annotations

import pytest

from app.capabilities import ensure_catalog_loaded
from app.capabilities.bus import get_command_bus, reset_command_bus_for_tests
from app.capabilities.coverage import assert_full_coverage, discover_mutating_routes, validate_catalog_integrity
from app.capabilities.policy import consume_approval, issue_approval, reset_approvals_for_tests
from app.capabilities.registry import get_registry
from app.capabilities.schemas import (
    CommandStatus,
    ConfirmationPolicy,
    IdempotencyPolicy,
    PreflightResult,
    RiskLevel,
)


@pytest.fixture(autouse=True)
def _load_catalog(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cap-test.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_x", "测试项目", "created", db.now()),
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_missing_for_test", "待删", "created", db.now()),
    )
    conn.commit()
    yield


def test_catalog_integrity_and_prd_core_tools() -> None:
    assert validate_catalog_integrity() == []


def test_storyboard_exposes_only_the_canonical_generation_route() -> None:
    registry = get_registry()
    assert registry.rest_bindings[
        "POST /api/episodes/{episode_id}/storyboard"
    ] == "storyboard.generate"
    assert "POST /api/episodes/{episode_id}/storyboard/resume" not in registry.rest_bindings
    assert "DELETE /api/episodes/{episode_id}/storyboard" not in registry.rest_bindings
    assert "storyboard.clear" not in registry.commands
    assert "storyboard.set_shot_adoption" not in registry.commands


def test_screenplay_exposes_draft_repair_instead_of_legacy_revise() -> None:
    registry = get_registry()
    assert registry.rest_bindings[
        "POST /api/episodes/{episode_id}/screenplay/repair-draft"
    ] == "screenplay.repair_draft"
    assert "screenplay.revise" not in registry.commands
    assert "POST /api/episodes/{episode_id}/screenplay/revise" not in registry.rest_bindings


def test_mutating_endpoints_fully_classified() -> None:
    report = assert_full_coverage()
    assert report["mutating_routes"] >= 50
    assert report["missing"] == []
    routes = discover_mutating_routes()
    assert "POST /api/projects/import" in routes
    assert "PUT /api/keys" in routes


def test_command_input_models_extend_standard_fields() -> None:
    registry = get_registry()
    for name, spec in registry.commands.items():
        fields = set(spec.input_model.model_fields)
        for required in {
            "request_id", "idempotency_key", "expected_version",
            "dry_run", "approval_token", "reason",
        }:
            assert required in fields, f"{name} missing {required}"


def test_high_risk_commands_require_confirmation_metadata() -> None:
    registry = get_registry()
    for name in ("project.delete", "video.clear_episode", "delivery.review"):
        spec = registry.get_command(name)
        assert spec.risk == RiskLevel.R3_DESTRUCTIVE
        assert spec.confirmation == ConfirmationPolicy.ALWAYS
        assert spec.idempotency == IdempotencyPolicy.REQUIRED


def test_storyboard_confirmation_consumes_domain_preview_without_double_approval() -> None:
    spec = get_registry().get_command("storyboard.confirm")

    assert spec.risk == RiskLevel.R3_DESTRUCTIVE
    assert spec.confirmation == ConfirmationPolicy.NEVER
    assert spec.idempotency == IdempotencyPolicy.REQUIRED


def test_human_only_secrets_never_mcp_exposed() -> None:
    registry = get_registry()
    assert registry.human_only["human.provide_api_key"].mcp_exposed is False
    assert registry.rest_bindings["PUT /api/keys"] == "human.provide_api_key"


def test_bus_dry_run_does_not_require_handler() -> None:
    result = get_command_bus().execute(
        "project.delete",
        {"project_id": "proj_x", "dry_run": True, "idempotency_key": "dry-1"},
    )
    assert result.status == CommandStatus.SUCCEEDED
    assert result.data.get("dry_run") is True


def test_bus_blocks_high_risk_without_approval() -> None:
    result = get_command_bus().execute(
        "project.delete",
        {"project_id": "proj_x", "idempotency_key": "del-1"},
    )
    assert result.status == CommandStatus.WAITING_APPROVAL
    assert "approval_token" in result.data


def test_bus_rejects_mismatched_approval() -> None:
    from app import db

    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_other", "另一项目", "created", db.now()),
    )
    conn.commit()
    bus = get_command_bus()
    first = bus.execute("project.delete", {"project_id": "proj_x", "idempotency_key": "del-2"})
    token = first.data["approval_token"]
    bad = bus.execute(
        "project.delete",
        {"project_id": "proj_other", "idempotency_key": "del-3", "approval_token": token},
    )
    assert bad.status == CommandStatus.REJECTED
    assert bad.error_code == "approval_invalid"


@pytest.mark.asyncio
async def test_waiting_approval_token_retry_reaches_handler() -> None:
    bus = get_command_bus()
    args = {"project_id": "proj_missing_for_test", "idempotency_key": "del-approve"}
    waiting = await bus.execute_async("project.delete", args)
    assert waiting.status == CommandStatus.WAITING_APPROVAL
    approved = await bus.execute_async(
        "project.delete",
        {**args, "approval_token": waiting.data["approval_token"]},
    )
    # 种子项目存在时应真正删除成功；关键是批准后进入了 handler 而非卡在批准。
    assert approved.status in {CommandStatus.SUCCEEDED, CommandStatus.FAILED}
    assert approved.error_code != "approval_invalid"
    if approved.status == CommandStatus.FAILED:
        assert approved.error_code in {"http_404", "not_found", "domain_error", "http_409"}


@pytest.mark.asyncio
async def test_bus_idempotency_suppresses_duplicate_success() -> None:
    """显式 idempotency_key 命中持久化缓存；未传 key 时不按参数自动去重。"""
    bus = get_command_bus()
    args = {"project_id": "proj_x", "idempotency_key": "same-key", "dry_run": True}
    a = await bus.execute_async("project.delete", args)
    b = await bus.execute_async("project.delete", args)
    assert a.status == b.status == CommandStatus.SUCCEEDED

    # 无显式 key：即使参数相同也不应误去重（避免 resume 复用陈旧结果）
    waiting1 = await bus.execute_async("project.delete", {"project_id": "proj_x"})
    waiting2 = await bus.execute_async("project.delete", {"project_id": "proj_x"})
    assert waiting1.status == waiting2.status == CommandStatus.WAITING_APPROVAL
    assert waiting1.data["approval_id"] != waiting2.data["approval_id"]


@pytest.mark.asyncio
async def test_bus_idempotency_rejects_same_key_for_different_payload() -> None:
    bus = get_command_bus()
    first = await bus.execute_async(
        "project.delete",
        {"project_id": "proj_x", "idempotency_key": "scope-bound", "dry_run": True},
    )
    mismatch = await bus.execute_async(
        "project.delete",
        {"project_id": "proj_other", "idempotency_key": "scope-bound", "dry_run": True},
    )

    assert first.status == CommandStatus.SUCCEEDED
    assert mismatch.status == CommandStatus.REJECTED
    assert mismatch.error_code == "idempotency_request_mismatch"


@pytest.mark.asyncio
async def test_paid_video_and_delivery_commands_require_idempotency_key() -> None:
    bus = get_command_bus()

    video = await bus.execute_async("video.complete_episode", {"episode_id": "ep_x"})
    delivery = await bus.execute_async("delivery.create_package", {"episode_id": "ep_x"})

    assert video.status == delivery.status == CommandStatus.REJECTED
    assert video.error_code == delivery.error_code == "idempotency_key_required"


def test_domain_preflight_reads_project_state() -> None:
    from app.capabilities.preflight import project_delete
    from app.capabilities.inputs import ProjectDeleteInput
    from app import db

    conn = db.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_pf", "预检项目", "created", db.now()),
    )
    conn.commit()
    result = project_delete(ProjectDeleteInput(project_id="proj_pf"))
    assert result.allowed is True
    assert result.affected.projects == ["proj_pf"]
    assert result.state_fingerprint.startswith("sha256:")
    # 补跑 Bus 预检应要求确认
    bus_pf = get_command_bus().preflight("project.delete", {"project_id": "proj_pf"})
    assert bus_pf.requires_confirmation is True
    assert "删除" in bus_pf.summary or "永久" in bus_pf.summary


def test_project_delete_preflight_returns_project_scoped_provider_blocker() -> None:
    from app import db
    from app.capabilities.inputs import ProjectDeleteInput
    from app.capabilities.preflight import project_delete
    from app.completion_grant import ensure_video_budget_authority_tables

    conn = db.get_conn()
    ensure_video_budget_authority_tables(conn)
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('proj-pf-block','P','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('ep-pf-block','proj-pf-block',1,'confirmed',1)"""
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('shot-pf-block','ep-pf-block',1,5)"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,provider_task_id,status,created_at
           ) VALUES(
               'ver-pf-block','shot-pf-block',1,'p','i','provider-task','running',1
           )"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               provider_non_cancellable,provider_operation_id,
               provider_create_state,created_at,updated_at
           ) VALUES(
               'job-pf-block','video','shot-pf-block','ver-pf-block',
               'ep-pf-block','proj-pf-block','waiting_provider',
               1,'op-pf-block','accepted',1,1
           )"""
    )
    conn.execute(
        """INSERT INTO provider_video_budget_claims(
               operation_id,project_id,episode_id,shot_id,job_id,version_id,
               origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
               amount_cny,status,created_at,updated_at,accepted_at
           ) VALUES(
               'op-pf-block','proj-pf-block','ep-pf-block','shot-pf-block',
               'job-pf-block','ver-pf-block','ep-pf-block','shot-pf-block',
               'job-pf-block','ver-pf-block',4,'accepted',1,1,1
           )"""
    )
    conn.commit()

    result = project_delete(ProjectDeleteInput(project_id="proj-pf-block"))

    assert result.allowed is True
    assert result.requires_confirmation is True
    blocker = result.affected.extra["blockers"][0]
    assert blocker["project_id"] == "proj-pf-block"
    assert blocker["episode_id"] == "ep-pf-block"
    assert blocker["shot_id"] == "shot-pf-block"
    assert blocker["recovery_action"] == "continue_provider_poll"
    assert "不会创建新任务" in result.warnings[-1]


def test_video_batch_preflight_quotes_exact_pending_shot_cost() -> None:
    from app import db

    conn = db.get_conn()
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('video-quote-ep','proj_x',1,'confirmed',1)"""
    )
    for shot_no, duration in ((1, 10), (2, 6), (3, 5)):
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,shot_no,duration_s,characters,dialogues
               ) VALUES(?,?,?,?, '[]','[]')""",
            (f"video-quote-s{shot_no}", "video-quote-ep", shot_no, duration),
        )
    conn.commit()

    result = get_command_bus().preflight(
        "video.generate_episode",
        {"episode_id": "video-quote-ep"},
    )

    # 参考图只复用人物谱/场景库，预检不再预留额外关键帧生成费用。
    assert result.estimated_cost_cny == pytest.approx(16.8)
    assert "¥16.8" in result.summary


def test_video_completion_preflight_does_not_hide_cost_above_requested_cap() -> None:
    from app import db

    conn = db.get_conn()
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('video-complete-quote-ep','proj_x',2,'confirmed',1)"""
    )
    for shot_no in (1, 2):
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,shot_no,duration_s,characters,dialogues
               ) VALUES(?,?,?,?, '[]','[]')""",
            (f"video-complete-quote-s{shot_no}", "video-complete-quote-ep", shot_no, 5),
        )
    conn.commit()

    result = get_command_bus().preflight(
        "video.complete_episode",
        {
            "episode_id": "video-complete-quote-ep",
            "budget_cap_cny": 1,
        },
    )

    assert result.estimated_cost_cny is not None
    assert result.estimated_cost_cny > 1


def test_approval_token_single_use() -> None:
    preflight = PreflightResult(
        command="project.delete",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary="删除项目",
        state_fingerprint="sha256:abc",
        requires_confirmation=True,
    )
    args = {"project_id": "proj_x"}
    token, _ = issue_approval(command="project.delete", args=args, preflight=preflight)
    consume_approval(token, command="project.delete", args=args, state_fingerprint_now="sha256:abc")
    with pytest.raises(PermissionError, match="already used"):
        consume_approval(token, command="project.delete", args=args, state_fingerprint_now="sha256:abc")


def test_approval_token_concurrent_consume_is_exactly_once() -> None:
    import concurrent.futures
    import threading

    preflight = PreflightResult(
        command="project.delete",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary="删除项目",
        state_fingerprint="sha256:concurrent",
        requires_confirmation=True,
    )
    args = {"project_id": "proj_x"}
    token, approval = issue_approval(
        command="project.delete",
        args=args,
        preflight=preflight,
    )
    barrier = threading.Barrier(2)

    def consume_once() -> tuple[str, str]:
        barrier.wait(timeout=5)
        try:
            value = consume_approval(
                token,
                command="project.delete",
                args=args,
                state_fingerprint_now="sha256:concurrent",
            )
            return "ok", value.approval_id
        except PermissionError as exc:
            return "error", str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: consume_once(), range(2)))

    assert sorted(item[0] for item in results) == ["error", "ok"]
    assert [item[1] for item in results if item[0] == "ok"] == [
        approval.approval_id
    ]
    assert "already used" in next(
        item[1] for item in results if item[0] == "error"
    )


def test_approval_token_requires_bound_session() -> None:
    preflight = PreflightResult(
        command="project.delete",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary="删除项目",
        state_fingerprint="sha256:abc",
        requires_confirmation=True,
    )
    args = {"project_id": "proj_x"}
    token, _ = issue_approval(
        command="project.delete", args=args, preflight=preflight, session_id="sess-1",
    )
    with pytest.raises(PermissionError, match="session mismatch"):
        consume_approval(
            token, command="project.delete", args=args,
            state_fingerprint_now="sha256:abc", session_id=None,
        )


def test_bus_dry_run_rejects_when_preflight_denies() -> None:
    bus = get_command_bus()
    args = bus.registry.get_command("project.delete").input_model.model_validate(
        {"project_id": "proj_x", "dry_run": True, "idempotency_key": "dry-deny"}
    )
    denied = PreflightResult(
        command="project.delete",
        allowed=False,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary="禁止删除",
        state_fingerprint="sha256:deny",
        denial_code="policy_denied",
        denial_message="策略拒绝",
        requires_confirmation=False,
    )
    result = bus._gate(
        "project.delete", args, args.model_dump(mode="json"), denied, session_id=None,
    )
    assert result is not None
    assert result.status == CommandStatus.REJECTED
    assert result.error_code == "policy_denied"


def test_core_commands_have_handlers_bound() -> None:
    registry = get_registry()
    for name in ("bible.generate", "project.delete", "video.generate_shot", "delivery.review", "run.control"):
        assert registry.get_command(name).handler is not None
