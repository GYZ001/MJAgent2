"""Command Bus dispatch 合同：UI / Agent 均不可自动批准高风险操作。"""
from __future__ import annotations

import pytest

from app.capabilities import ensure_catalog_loaded
from app.capabilities.bus import reset_command_bus_for_tests
from app.capabilities.dispatch import dispatch, waiting_approval_payload
from app.capabilities.policy import reset_approvals_for_tests
from app.capabilities.registry import get_registry
from app.capabilities.schemas import CommandStatus, RiskLevel


@pytest.fixture(autouse=True)
def _ready(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dispatch-test.db")
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
    conn.commit()
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
async def test_ui_initiator_does_not_auto_approve_high_risk():
    """P0：页面 initiator 也不能同请求内自动签发并消费批准令牌。"""
    result = await dispatch(
        "project.delete",
        {"project_id": "proj_x", "idempotency_key": "u1"},
        initiator="ui",
    )
    assert result.status == CommandStatus.WAITING_APPROVAL
    assert result.data.get("approval_token")
    payload = waiting_approval_payload(result)
    assert payload["status"] == "waiting_approval"
    assert payload["approval_token"]


@pytest.mark.asyncio
async def test_ui_can_execute_after_explicit_approval_token(monkeypatch):
    calls = {"n": 0}

    async def fake_handler(args):
        calls["n"] += 1
        from app.capabilities.schemas import CommandResult
        return CommandResult(status=CommandStatus.SUCCEEDED, summary="deleted", data={"deleted": args.project_id})

    from dataclasses import replace

    registry = get_registry()
    registry.commands["project.delete"] = replace(
        registry.get_command("project.delete"), handler=fake_handler
    )

    waiting = await dispatch(
        "project.delete",
        {"project_id": "proj_x", "idempotency_key": "u2"},
        initiator="ui",
    )
    assert waiting.status == CommandStatus.WAITING_APPROVAL
    approved = await dispatch(
        "project.delete",
        {
            "project_id": "proj_x",
            "idempotency_key": "u2-exec",
            "approval_token": waiting.data["approval_token"],
        },
        initiator="ui",
    )
    assert approved.status == CommandStatus.SUCCEEDED, approved.summary
    assert calls["n"] == 1


def test_video_and_delivery_risk_metadata():
    registry = get_registry()
    assert registry.get_command("video.generate_shot").risk == RiskLevel.R2_MATERIAL
    assert registry.get_command("delivery.review").risk == RiskLevel.R3_DESTRUCTIVE
