"""剧本台按钮必须映射到真实的 Baseline / Patch 后端阶段。"""
from __future__ import annotations

import pytest

from app import db
from app.capabilities import ensure_catalog_loaded
from app.capabilities.registry import get_registry
from app.production.revision import (
    ensure_production_revision,
    mark_baseline_generated,
    screenplay_production_state,
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


def test_production_state_only_allows_patch_resume_after_baseline() -> None:
    initial = screenplay_production_state("e1")
    assert initial["operation"] == "baseline"
    assert initial["baseline_done"] is False
    assert initial["can_resume_repair"] is False

    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id="artifact-baseline",
        working_artifact_id="artifact-working",
    )

    repair = screenplay_production_state("e1")
    assert repair["operation"] == "repair"
    assert repair["baseline_done"] is True
    assert repair["can_resume_repair"] is True


def test_resume_route_has_a_distinct_capability() -> None:
    ensure_catalog_loaded()
    registry = get_registry()
    assert registry.rest_bindings[
        "POST /api/episodes/{episode_id}/screenplay/resume"
    ] == "screenplay.resume"
    assert registry.commands["screenplay.resume"].title == "继续剧本局部修复"
