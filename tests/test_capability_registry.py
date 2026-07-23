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
def _load_catalog():
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()
    yield


def test_catalog_integrity_and_prd_core_tools() -> None:
    assert validate_catalog_integrity() == []


def test_mutating_endpoints_fully_classified() -> None:
    report = assert_full_coverage()
    assert report["mutating_routes"] >= 50
    assert report["missing"] == []
    routes = discover_mutating_routes()
    assert "POST /api/projects" in routes
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
    for name in ("project.delete", "video.clear_episode", "delivery.review", "storyboard.confirm"):
        spec = registry.get_command(name)
        assert spec.risk == RiskLevel.R3_DESTRUCTIVE
        assert spec.confirmation == ConfirmationPolicy.ALWAYS
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
async def test_bus_idempotency_suppresses_duplicate_dry_run() -> None:
    bus = get_command_bus()
    args = {"project_id": "proj_x", "idempotency_key": "same-key", "dry_run": True}
    a = await bus.execute_async("project.delete", args)
    b = await bus.execute_async("project.delete", args)
    assert a.status == b.status == CommandStatus.SUCCEEDED


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
    assert approved.status == CommandStatus.FAILED
    assert approved.error_code in {"http_404", "not_found", "domain_error", "http_409"}


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


def test_core_commands_have_handlers_bound() -> None:
    registry = get_registry()
    for name in ("bible.generate", "project.delete", "video.generate_shot", "delivery.review", "run.control"):
        assert registry.get_command(name).handler is not None
