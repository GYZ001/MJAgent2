"""修订任务（delivery_revision）退场后的回归。

背景：`add_customer_feedback` 曾在 `request_revision=True` 时建一条
`workflow_type="delivery_revision"` 的 workflow_run，但全仓没有任何执行者推进
它、也没有 GET 展示它——它永远停在 CREATED，还会被
`scripts/deploy/in_flight_on_b.py` 当成"在途"拦住生产部署。这里锁住退场后的
行为：不再建 run、REST 层对仍传 `request_revision=true` 的调用方显式 422（不
静默 no-op）、以及新的只读列表（路由 + 查询函数）。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import HTTPException

from app import db, delivery
from app.evaluations.customer_feedback import recent_customer_feedback
from app.evidence import repository
from app.orchestration import api as orchestration_api


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


def _episode_with_delivery(conn, episode_id: str = "ep1", artifact_id: str = "artifact1") -> None:
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, delivery_artifact_id, created_at) "
        "VALUES(?,?,?,?,?)",
        (episode_id, "proj1", 1, artifact_id, time.time()),
    )
    conn.commit()


def test_add_customer_feedback_creates_no_revision_run(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(delivery, "get_conn", lambda: conn)
    # create_evaluation()'s conn=None default falls back to repository.get_conn()
    # separately from delivery.get_conn() -- both must point at this test's
    # isolated conn or the evaluations insert silently hits the real db.
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    _episode_with_delivery(conn)

    feedback = delivery.add_customer_feedback(
        "ep1", message="第二镜节奏可更紧", created_by="customer", rating=3,
    )

    assert "revision_run_id" not in feedback
    row = conn.execute(
        "SELECT revision_run_id FROM customer_feedback WHERE id=?", (feedback["feedback_id"],)
    ).fetchone()
    assert row["revision_run_id"] is None
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM workflow_runs WHERE workflow_type='delivery_revision'"
    ).fetchone()["c"]
    assert count == 0


def test_add_customer_feedback_no_longer_accepts_request_revision_kwarg() -> None:
    with pytest.raises(TypeError):
        delivery.add_customer_feedback(  # type: ignore[call-arg]
            "ep1", message="x", created_by="customer", request_revision=True,
        )


def test_create_customer_feedback_route_rejects_request_revision_true() -> None:
    """REST 层不许静默吞掉旧调用方仍传的 request_revision=true——必须显式 422，
    而不是悄悄不创建任务却回 200。"""
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(orchestration_api.create_customer_feedback(
            "any-episode", body={"message": "x", "request_revision": True},
        ))
    assert exc_info.value.status_code == 422
    assert "修订任务已退场" in str(exc_info.value.detail)


def test_create_customer_feedback_route_stops_forwarding_request_revision(monkeypatch) -> None:
    """正常提交（不带 request_revision）时，转发给命令总线的参数里不应再出现
    这个字段——命令总线 schema 已经不声明它，带上会被 extra=forbid 打 422。"""
    captured: dict = {}

    async def fake_ui_route(name: str, args: dict):
        captured.update({"name": name, "args": args})
        return {"status": "accepted"}

    monkeypatch.setattr("app.capabilities.dispatch.ui_route", fake_ui_route)
    result = asyncio.run(orchestration_api.create_customer_feedback(
        "ep1", body={"message": "反馈内容"},
    ))

    assert result == {"status": "accepted"}
    assert "request_revision" not in captured["args"]


def test_list_customer_feedback_route_returns_recent_rows(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(orchestration_api, "get_conn", lambda: conn)
    monkeypatch.setattr("app.evaluations.customer_feedback.get_conn", lambda: conn)
    _episode_with_delivery(conn)
    conn.execute(
        "INSERT INTO customer_feedback(id, episode_id, artifact_id, rating, message, created_by, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("fb1", "ep1", "artifact1", 4, "节奏可以更紧", "customer", time.time()),
    )
    conn.commit()

    items = orchestration_api.list_customer_feedback("ep1")

    assert len(items) == 1
    assert items[0]["message"] == "节奏可以更紧"
    assert items[0]["rating"] == 4


def test_list_customer_feedback_route_404_for_missing_episode(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(orchestration_api, "get_conn", lambda: conn)

    with pytest.raises(HTTPException) as exc_info:
        orchestration_api.list_customer_feedback("missing")
    assert exc_info.value.status_code == 404


def test_recent_customer_feedback_orders_newest_first(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr("app.evaluations.customer_feedback.get_conn", lambda: conn)
    _episode_with_delivery(conn)
    conn.execute(
        "INSERT INTO customer_feedback(id, episode_id, artifact_id, message, created_by, created_at) "
        "VALUES(?,?,?,?,?,?)", ("fb1", "ep1", "artifact1", "第一条", "customer", 1.0),
    )
    conn.execute(
        "INSERT INTO customer_feedback(id, episode_id, artifact_id, message, created_by, created_at) "
        "VALUES(?,?,?,?,?,?)", ("fb2", "ep1", "artifact1", "第二条", "customer", 2.0),
    )
    conn.commit()

    items = recent_customer_feedback("ep1")

    assert [item["id"] for item in items] == ["fb2", "fb1"]
