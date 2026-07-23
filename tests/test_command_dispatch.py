"""Command Bus dispatch 合同：UI 自动批准 vs Agent 等待批准。"""
from __future__ import annotations

import pytest

from app.capabilities import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests
from app.capabilities.dispatch import dispatch
from app.capabilities.policy import reset_approvals_for_tests
from app.capabilities.registry import get_registry
from app.capabilities.schemas import CommandStatus, RiskLevel


@pytest.fixture(autouse=True)
def _ready():
    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()
    yield


@pytest.mark.asyncio
async def test_agent_initiator_waits_for_approval_on_delete():
    result = await dispatch(
        "project.delete",
        {"project_id": "proj_x", "idempotency_key": "a1"},
        initiator="agent",
    )
    assert result.status == CommandStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_ui_initiator_auto_approves_then_hits_handler(monkeypatch):
    calls = {"n": 0}

    async def fake_handler(args):
        calls["n"] += 1
        from app.capabilities.schemas import CommandResult
        return CommandResult(status=CommandStatus.SUCCEEDED, summary="deleted", data={"deleted": args.project_id})

    monkeypatch.setattr(
        "app.capabilities.handlers.domain.project_delete",
        fake_handler,
    )
    from dataclasses import replace
    from app.capabilities.handlers.domain import HANDLER_MAP

    registry = get_registry()
    HANDLER_MAP["project.delete"] = fake_handler
    registry.commands["project.delete"] = replace(
        registry.get_command("project.delete"), handler=fake_handler
    )

    result = await dispatch(
        "project.delete",
        {"project_id": "proj_x", "idempotency_key": "u1"},
        initiator="ui",
    )
    assert result.status == CommandStatus.SUCCEEDED, result.summary
    assert calls["n"] == 1


def test_video_and_delivery_risk_metadata():
    registry = get_registry()
    assert registry.get_command("video.generate_shot").risk == RiskLevel.R2_MATERIAL
    assert registry.get_command("delivery.review").risk == RiskLevel.R3_DESTRUCTIVE
