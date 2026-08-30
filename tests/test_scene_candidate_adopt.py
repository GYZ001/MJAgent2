"""场景库候选图手动采纳与「检查并补齐」超额自动采纳。"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from app import db
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.scenes import (
    SCENE_CANDIDATE_AUTO_ADOPT_THRESHOLD,
    adopt_scene_candidate,
    generate_scene_refs,
    list_scene_reference_candidates,
    pick_best_scene_candidate,
)


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene_adopt.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    return db.get_conn()


def _seed_project(conn, project_id: str, scene_name: str = "宗门广场") -> None:
    bible = {
        "characters": [],
        "world": {"era": "玄幻", "genre": "玄幻", "visual_style_canonical": "国风厚涂"},
        "scenes": [{
            "name": scene_name,
            "scene_canonical": "白日宗门广场，青石铺地，四周高耸石柱与飘扬旗幡，光线明亮，庄严肃穆",
            "location_kind": "室外",
        }],
    }
    conn.execute(
        """INSERT INTO projects(id, name, bible_json, bible_version, created_at)
           VALUES(?,?,?,?,?)""",
        (project_id, "测", json.dumps(bible, ensure_ascii=False), 1, db.now()),
    )
    conn.commit()


def _make_candidate(
    tmp_path: Path,
    project_id: str,
    scene_name: str,
    *,
    attempt: int,
    qa_overall: float,
    status: str = "candidate",
) -> str:
    image = tmp_path / f"cand_{attempt}.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    artifact = repository.create_artifact(EvidenceArtifact(
        type="scene_reference",
        scope_type="reference_asset",
        scope_id=f"{project_id}:{scene_name}:1",
        status=status,
        trust_level="T1",
        file_path=str(image),
        content={
            "scene_name": scene_name,
            "canonical": "白日宗门广场，青石铺地，四周高耸石柱与飘扬旗幡，光线明亮，庄严肃穆",
            "prompt": "国风厚涂，宗门广场定场",
            "attempt": attempt,
        },
        contract_version="reference-1.0.0",
    ))
    repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="model",
            evaluator_name="scene_reference_consistency_qa",
            evaluator_version="1.0.0",
            status="failed" if qa_overall < 0.6 else "passed",
            hard_gate_passed=qa_overall >= 0.6,
            score=qa_overall * 100,
            evidence={"qa": {"overall": qa_overall, "issues": ["测试"]}},
        ),
    )
    return artifact["id"]


def test_pick_best_scene_candidate_by_qa(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn, "p1")
    low = _make_candidate(tmp_path, "p1", "宗门广场", attempt=1, qa_overall=0.41)
    high = _make_candidate(tmp_path, "p1", "宗门广场", attempt=2, qa_overall=0.55)
    _make_candidate(tmp_path, "p1", "宗门广场", attempt=3, qa_overall=0.33)
    best = pick_best_scene_candidate(conn, "p1", "宗门广场")
    assert best is not None
    assert best["artifact_id"] == high
    assert best["qa_score"] == pytest.approx(0.55)
    assert {c["artifact_id"] for c in list_scene_reference_candidates(conn, "p1", "宗门广场")} >= {low, high}


def test_adopt_scene_candidate_registers_main_image(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn, "p1")
    artifact_id = _make_candidate(tmp_path, "p1", "宗门广场", attempt=1, qa_overall=0.42)
    monkeypatch.setattr(
        "app.multiview.scene_multiview_enabled", lambda: False,
    )

    result = asyncio.run(adopt_scene_candidate(
        "p1", "宗门广场", artifact_id, reason="测试人工采纳",
    ))
    assert result["adopted"] is True
    assert result["artifact_id"] == artifact_id

    art = repository.get_artifact(artifact_id)
    assert art["status"] == "approved"
    assert art["trust_level"] == "T4"

    row = conn.execute(
        "SELECT image_path, artifact_id, qa_json FROM scene_references WHERE project_id=? AND scene_name=?",
        ("p1", "宗门广场"),
    ).fetchone()
    assert row is not None
    assert row["artifact_id"] == artifact_id
    assert Path(row["image_path"]).exists()
    qa = json.loads(row["qa_json"])
    assert qa["human_adopted"] is True
    assert qa["adoption_reason"] == "测试人工采纳"

    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id=?", ("p1",),
    ).fetchone()["bible_json"])
    assert bible["scenes"][0]["ref_image_path"] == row["image_path"]


def test_generate_scene_refs_auto_adopts_when_candidates_exceed_threshold(
    tmp_path, monkeypatch,
) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn, "p1")
    scores = [0.21, 0.33, 0.48, 0.39, 0.52]
    assert len(scores) == SCENE_CANDIDATE_AUTO_ADOPT_THRESHOLD + 1
    ids = [
        _make_candidate(tmp_path, "p1", "宗门广场", attempt=i + 1, qa_overall=score)
        for i, score in enumerate(scores)
    ]
    best_id = ids[scores.index(max(scores))]

    called_generate = {"n": 0}

    async def _boom(*_a, **_k):
        called_generate["n"] += 1
        raise AssertionError("超额候选时应直接采纳，不应再出图")

    monkeypatch.setattr("app.scenes._generate_scene_image", _boom)
    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: False)

    asyncio.run(generate_scene_refs("p1"))

    assert called_generate["n"] == 0
    row = conn.execute(
        "SELECT artifact_id FROM scene_references WHERE project_id=? AND scene_name=?",
        ("p1", "宗门广场"),
    ).fetchone()
    assert row is not None
    assert row["artifact_id"] == best_id
    assert repository.get_artifact(best_id)["status"] == "approved"


def test_scene_adopt_route_is_registered() -> None:
    from app.api import router

    methods_by_path = {
        getattr(route, "path", ""): set(getattr(route, "methods", set()))
        for route in router.routes
    }
    path = "/api/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}/adopt"
    assert "POST" in methods_by_path[path]


# ---------------------------------------------------------------------------
# scene.review_candidate 的 REST 路由必须真的走命令总线：不是「能返回 200」，
# 而是「鉴权/校验真的生效」。此前这条路由绕过 dispatch，任何角色都能裸奔执行。
# ---------------------------------------------------------------------------


@contextmanager
def _as_principal(*, role: str | None, is_system_admin: bool = False, workspace_id: str = "ws_test"):
    """临时切换当前 Principal，退出时还原成进入前的身份（同 test_rbac_command_bus.py）。"""
    from app.auth.principal import Principal, get_current_principal, set_current_principal

    previous = get_current_principal()
    workspace_roles = {} if role is None else {workspace_id: role}
    set_current_principal(
        Principal(
            user_id=f"test-{role or 'sysadmin'}",
            username=f"test-{role or 'sysadmin'}",
            is_system_admin=is_system_admin,
            workspace_roles=workspace_roles,
        )
    )
    try:
        yield
    finally:
        set_current_principal(previous)


def _ready_command_bus() -> None:
    from app.capabilities.bus import reset_command_bus_for_tests
    from app.capabilities.loader import ensure_catalog_loaded
    from app.capabilities.policy import reset_approvals_for_tests

    ensure_catalog_loaded()
    reset_approvals_for_tests()
    reset_command_bus_for_tests()

