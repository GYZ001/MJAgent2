from __future__ import annotations

import asyncio
import json

import pytest

from app import db
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.scene_policy import normalize_scene_image_qa


def test_environment_only_prompt_removes_conflicting_human_clauses() -> None:
    from app.scenes import environment_only_scene_canonical, scene_ref_prompt

    canonical = "热闹露天坊市，摊位林立，人流穿梭，周围有萧家护卫巡视，色彩明快"
    cleaned = environment_only_scene_canonical(canonical)
    assert "摊位林立" in cleaned
    assert "人流" not in cleaned
    assert "护卫" not in cleaned
    prompt = scene_ref_prompt("国风厚涂", canonical)
    assert "人流穿梭" not in prompt
    assert "护卫巡视" not in prompt
    assert "无人物" in prompt


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-candidate-recovery.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    return db.get_conn()


def _seed_project(conn) -> None:
    bible = {
        "characters": [],
        "world": {"era": "玄幻", "genre": "玄幻", "visual_style_canonical": "国风厚涂"},
        "scenes": [{
            "name": "萧家坊市",
            "scene_canonical": "白日萧家坊市，古风木制商铺与青石长街，阳光明亮，空间开阔，纯环境无人物",
            "location_kind": "室外",
        }],
    }
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,bible_version,created_at) VALUES(?,?,?,?,?)",
        ("p", "测试", json.dumps(bible, ensure_ascii=False), 1, db.now()),
    )
    conn.commit()


def _candidate(tmp_path) -> dict:
    path = tmp_path / "candidate.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return repository.create_artifact(EvidenceArtifact(
        type="scene_reference",
        scope_type="reference_asset",
        scope_id="p:萧家坊市:1",
        status="candidate",
        trust_level="T1",
        file_path=str(path),
        content={"scene_name": "萧家坊市", "prompt": "国风厚涂纯环境坊市", "attempt": 1},
    ))


def _evaluation(qa: dict, *, status: str | None = None) -> Evaluation:
    hard = bool(qa.get("hard_failures"))
    return Evaluation(
        evaluator_type="model",
        evaluator_name="scene_reference_consistency_qa",
        evaluator_version=str(qa.get("policy_version") or "legacy"),
        status=status or ("failed" if hard else "passed"),
        hard_gate_passed=bool(qa.get("hard_gate_passed")) and not hard,
        score=float(qa.get("overall") or 0) * 100,
        evidence={"qa": qa},
        recovered=bool(qa.get("qa_recovered")),
    )


def test_latest_scene_qa_replaces_old_unverified_decision(tmp_path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    _seed_project(db.get_conn())
    artifact = _candidate(tmp_path)
    old = normalize_scene_image_qa({"overall": 1.0, "issues": [], "qa_recovered": True})
    repository.create_evaluation(artifact["id"], _evaluation(old, status="error"))
    fresh = normalize_scene_image_qa({
        "overall": 0.86,
        "person_count": 0,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "space_type_matches": True,
    })
    repository.create_evaluation(artifact["id"], _evaluation(fresh))

    from app.scenes import _scene_candidate_qa_score, scene_candidate_gate
    gate = scene_candidate_gate(artifact["id"])
    assert gate["verified"] is True
    assert gate["state"] == "passed"
    assert _scene_candidate_qa_score(artifact["id"]) == pytest.approx(0.86)


def test_review_existing_candidate_adds_new_qa_without_regenerating(tmp_path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    _seed_project(db.get_conn())
    artifact = _candidate(tmp_path)
    qa = normalize_scene_image_qa({
        "overall": 0.91,
        "person_count": 0,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "space_type_matches": True,
    })

    async def reviewed(*_args, **_kwargs):
        return qa

    async def must_not_generate(*_args, **_kwargs):
        raise AssertionError("重验 QA 不得重新生图")

    monkeypatch.setattr("app.scenes._review_scene_ref", reviewed)
    monkeypatch.setattr("app.scenes._generate_scene_image", must_not_generate)
    from app.scenes import review_scene_candidate
    result = asyncio.run(review_scene_candidate("p", "萧家坊市", artifact["id"]))
    assert result["reviewed"] is True
    assert result["image_regenerated"] is False
    assert result["gate"]["verified"] is True
    assert len(repository.get_evaluations(artifact["id"])) == 1


def test_batch_completion_rechecks_best_old_candidate_before_generating(tmp_path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    _seed_project(db.get_conn())
    artifacts = []
    for _ in range(5):
        artifact = _candidate(tmp_path)
        old = normalize_scene_image_qa({"overall": 0.9, "issues": [], "qa_recovered": True})
        repository.create_evaluation(artifact["id"], _evaluation(old, status="error"))
        artifacts.append(artifact)

    reviewed_ids = []
    adopted_ids = []

    async def review_existing(project_id, scene_name, artifact_id):
        reviewed_ids.append(artifact_id)
        fresh = normalize_scene_image_qa({
            "overall": 0.88,
            "person_count": 0,
            "watermark_detected": False,
            "forbidden_text_detected": False,
            "space_type_matches": True,
        })
        repository.create_evaluation(artifact_id, _evaluation(fresh))
        from app.scenes import scene_candidate_gate
        return {"gate": scene_candidate_gate(artifact_id)}

    async def adopt_existing(project_id, scene_name, artifact_id, **_kwargs):
        adopted_ids.append(artifact_id)
        artifact = repository.get_artifact(artifact_id)
        return {"image_path": artifact["file_path"]}

    async def must_not_generate(*_args, **_kwargs):
        raise AssertionError("已有候选可复验时不得继续付费生图")

    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: True)
    monkeypatch.setattr("app.scenes.review_scene_candidate", review_existing)
    monkeypatch.setattr("app.scenes.adopt_scene_candidate", adopt_existing)
    monkeypatch.setattr("app.scenes._generate_scene_image", must_not_generate)

    from app.scenes import generate_scene_refs
    asyncio.run(generate_scene_refs("p", ["萧家坊市"]))

    assert len(reviewed_ids) == 1
    assert adopted_ids == reviewed_ids


def test_manual_review_adopts_only_unverified_candidate_with_audit(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn)
    artifact = _candidate(tmp_path)
    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: False)
    from app.scenes import manually_review_and_adopt_scene_candidate
    result = asyncio.run(manually_review_and_adopt_scene_candidate(
        "p",
        "萧家坊市",
        artifact["id"],
        confirmations={
            "person_free": True,
            "watermark_free": True,
            "forbidden_text_free": True,
            "space_type_matches": True,
        },
        reason="已放大检查四项硬门禁",
    ))
    assert result["adopted"] is True
    assert result["manual_reviewed"] is True
    evaluations = repository.get_evaluations(artifact["id"])
    review = next(row for row in evaluations if row["evaluator_name"] == "scene_candidate_human_hard_gate_review")
    assert review["evidence"]["reason"] == "已放大检查四项硬门禁"
    assert review["evidence"]["confirmations"] == {
        "person_free": True,
        "watermark_free": True,
        "forbidden_text_free": True,
        "space_type_matches": True,
    }


def test_manual_review_never_overrides_historical_explicit_hard_failure(tmp_path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    _seed_project(db.get_conn())
    artifact = _candidate(tmp_path)
    failed = normalize_scene_image_qa({
        "overall": 0.95,
        "person_count": 0,
        "watermark_detected": True,
        "forbidden_text_detected": False,
        "space_type_matches": True,
    })
    repository.create_evaluation(artifact["id"], _evaluation(failed))
    incomplete = normalize_scene_image_qa({"overall": 0.0, "qa_recovered": True})
    repository.create_evaluation(artifact["id"], _evaluation(incomplete, status="error"))
    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: False)
    from app.scenes import manually_review_and_adopt_scene_candidate
    with pytest.raises(ValueError, match="历史上存在明确硬失败"):
        asyncio.run(manually_review_and_adopt_scene_candidate(
            "p", "萧家坊市", artifact["id"],
            confirmations={
                "person_free": True, "watermark_free": True,
                "forbidden_text_free": True, "space_type_matches": True,
            },
            reason="尝试覆盖历史失败",
        ))


def test_candidate_recovery_routes_are_registered() -> None:
    from app.api import router
    methods = {
        (route.path, method)
        for route in router.routes
        for method in (route.methods or set())
    }
    base = "/api/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}"
    assert (f"{base}/review", "POST") in methods
    assert (f"{base}/manual-review", "POST") in methods


def test_scene_candidate_review_required_finishes_task_as_partial_not_provider_failure(
    tmp_path, monkeypatch,
) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn)
    from app.scenes import SceneCandidateReviewRequired

    async def pending_review(*_args, **_kwargs):
        raise SceneCandidateReviewRequired("候选已生成，2 张待复核")

    monkeypatch.setattr("app.scenes.generate_scene_refs", pending_review)
    from app.domain.bible_ops import _scene_refs_task
    asyncio.run(_scene_refs_task("p", None))

    project = conn.execute(
        "SELECT scene_refs_status,scene_refs_error FROM projects WHERE id='p'",
    ).fetchone()
    assert project["scene_refs_status"] == "warning"
    assert "候选已生成" in project["scene_refs_error"]
    run = conn.execute(
        "SELECT status,failure_code FROM workflow_runs "
        "WHERE workflow_type='scene_references' AND scope_id='p' ORDER BY updated_at DESC LIMIT 1",
    ).fetchone()
    assert run["status"] == "PARTIAL"
    assert run["failure_code"] == "PARTIAL_RESULT"


def test_mixed_candidate_and_pack_quality_failures_are_not_provider_outages() -> None:
    from app import hiagent
    from app.errors import ContentGenerationError
    from app.scenes import SceneCandidateReviewRequired, _scene_failures_are_quality_only

    assert _scene_failures_are_quality_only([
        SceneCandidateReviewRequired("高分候选待复核"),
        hiagent.ProviderError("多视角资产包未通过：status=failed"),
        ContentGenerationError("场景一致性检查未通过"),
    ]) is True
    assert _scene_failures_are_quality_only([
        SceneCandidateReviewRequired("高分候选待复核"),
        hiagent.ProviderError("上游图像服务 503"),
    ]) is False
