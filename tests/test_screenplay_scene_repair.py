from __future__ import annotations

import pytest

from app import db
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.production.revision import (
    ensure_production_revision,
    mark_baseline_generated,
    mark_first_evaluation,
    save_checkpoint,
)
from app.production.screenplay_document import (
    apply_field_patch,
    document_to_screenplay,
    screenplay_to_document,
)
from app.production.structured_issues import (
    enrich_issues,
    issues_from_validator_messages,
    structured_issue,
)
from app.schemas import Bible, EpisodeScreenplay, ScriptScene, World


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-repair.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_scene", "场级修复测试", "created", db.now()),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, created_at, screenplay_status) "
        "VALUES(?,?,?,?,?,?)",
        ("ep_scene", "proj_scene", 1, "第一集", db.now(), "pending"),
    )
    conn.commit()
    yield


def _script(story_function: str = "升级") -> EpisodeScreenplay:
    return EpisodeScreenplay(
        episode_no=1,
        title="陨落的天才",
        logline="萧炎在家族测验中面对公开羞辱",
        dramatic_question="萧炎能否守住最后的尊严？",
        protagonist_goal="萧炎要面对测验结果并离开广场",
        obstacle="测验结果与围观者嘲讽构成双重压力",
        stakes="失败将令萧炎失去家族中的最后尊严",
        key_lines=["测验员：斗之力，三段！"],
        key_plot_points=["测验结果公开，萧炎遭到嘲讽"],
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="日 / 萧家测验广场",
            story_function=story_function,
            characters=["萧炎"],
            summary="测验员公开萧炎的三段结果，围观者随即发出嘲讽。",
            conflict="公开羞辱冲击萧炎的尊严",
            turn="萧炎退回队尾",
            source_basis="保留原文测验结果公布与围观者嘲讽",
        )],
        full_script_text="【场1】日 / 萧家测验广场\n测验员：斗之力，三段！",
        emotional_curve="压抑后保持克制",
        ending_hook="无集级钩子",
        source_basis="原文第一章测验广场段落",
    )


def test_scene_validator_message_is_structured_as_scene_field_issue():
    message = (
        "[SCENE_STORY_FUNCTION_TOO_SHORT] "
        "scene_outline 第1场「日 / 萧家测验广场」.story_function "
        "过短；请说明本场戏剧功能"
    )
    issue = issues_from_validator_messages(
        [message], subject="screenplay", stage="screenplay"
    )[0]

    assert issue.code == "SCENE_STORY_FUNCTION_TOO_SHORT"
    assert issue.evidence["path"] == "/scene_blocks/SC01/story_function"
    assert issue.evidence["related_node_ids"] == ["SC01"]
    assert issue.evidence["requires_user_input"] is False


def test_legacy_scene_message_is_not_inferred_as_a_shot():
    legacy = Issue(
        code="DRAMATIC_CONTRACT_INCOMPLETE",
        severity=IssueSeverity.BLOCKER,
        subject="screenplay",
        message=(
            "scene_outline 第1场「日 / 萧家测验广场」.story_function "
            "过短；请说明本场戏剧功能"
        ),
        repairable=True,
    )

    issue = enrich_issues([legacy], stage="screenplay")[0]

    assert issue.evidence["path"] == "/scene_blocks/SC01/story_function"
    assert "shot:1" not in issue.evidence["related_node_ids"]
    assert "SC01" in issue.evidence["related_node_ids"]




@pytest.mark.asyncio
async def test_old_exhausted_checkpoint_resumes_without_second_baseline(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_authority, screenplay_repair

    revision = ensure_production_revision(
        episode_id="ep_scene",
        kind="screenplay",
        resume=False,
    )
    script = _script()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_scene",
        status="candidate",
        trust_level="T1",
        content=screenplay_repair.screenplay_artifact_payload(script),
    ))
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    mark_first_evaluation(revision.id, "eval_old")
    old_issue = structured_issue(
        code="DRAMATIC_CONTRACT_INCOMPLETE",
        message="scene_outline 第1场.story_function 过短；请说明本场戏剧功能",
        subject="screenplay",
        path="/shots/1",
        related_node_ids=["shot:1"],
        stage="screenplay",
    )
    save_checkpoint(revision.id, {
        "phase": "WAITING_INPUT",
        "issue_strategy_history": {
            old_issue.fingerprint: ["rederive", "exhausted"],
        },
        "yield_reason": "strategies_exhausted",
    })

    def fake_qa(current, **_kwargs):
        issues = []
        if len(current.scene_outline[0].story_function.strip()) < 6:
            issues = issues_from_validator_messages(
                ["scene_outline 第1场.story_function 过短；请说明本场戏剧功能"],
                subject="screenplay",
                stage="screenplay",
            )
        return issues, Evaluation(
            evaluator_type="deterministic",
            evaluator_name="test",
            evaluator_version="1",
            status="failed" if issues else "passed",
            hard_gate_passed=not issues,
            score=90 if issues else 100,
            issues=issues,
            evidence={
                "authority_input_fingerprint": "authority-test",
            },
        )

    async def forbidden_baseline(*_args, **_kwargs):
        raise AssertionError("repair resume must not generate a second baseline")

    monkeypatch.setattr(
        screenplay_authority,
        "screenplay_authority_fingerprint",
        lambda *_args, **_kwargs: "authority-test",
    )
    monkeypatch.setattr(screenplay_repair, "run_screenplay_qa", fake_qa)
    monkeypatch.setattr("app.stages.generate_screenplay_baseline", forbidden_baseline)
    monkeypatch.setattr(
        screenplay_repair,
        "publish_screenplay",
        lambda **kwargs: {"status": "ready", "artifact_id": kwargs["artifact_id"]},
    )

    result = await screenplay_repair.run_screenplay_production(
        episode_id="ep_scene",
        episode={
            "id": "ep_scene",
            "project_id": "proj_scene",
            "episode_no": 1,
            "target_duration_s": 60,
        },
        source_text="原文",
        bible=Bible(
            characters=[],
            world=World(visual_style_canonical="测试画风"),
        ),
        resume=True,
    )

    resumed = screenplay_repair.get_production_revision(revision.id)
    assert resumed is not None
    assert resumed.baseline_generation_count == 1
    assert resumed.checkpoint_json["phase"] == "SUCCEEDED"
    assert resumed.checkpoint_json["planner_version"] == screenplay_repair.SCREENPLAY_REPAIR_PLANNER_VERSION
    assert resumed.checkpoint_json["yield_reason"] is None
    assert resumed.checkpoint_json.get("quality_issue_count", 0) == 0
    assert result.scene_outline[0].story_function != script.scene_outline[0].story_function
    assert len(result.scene_outline[0].story_function) >= 6


@pytest.mark.asyncio
async def test_business_qa_issue_blocks_when_no_repair_strategy_exists(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_repair

    revision = ensure_production_revision(
        episode_id="ep_scene",
        kind="screenplay",
        resume=False,
    )
    script = _script(story_function="建立公开测验冲突并推动萧炎退场")
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_scene",
        status="candidate",
        trust_level="T1",
        content=screenplay_repair.screenplay_artifact_payload(script),
    ))
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    mark_first_evaluation(revision.id, "eval_existing")
    issue = structured_issue(
        code="BUSINESS_RULE_FAILED",
        message="测试内部未知修复缺口",
        subject="screenplay",
        path="/unknown",
        repairable=True,
        requires_user_input=False,
        stage="screenplay",
    )
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="test",
        evaluator_version="1",
        status="failed",
        hard_gate_passed=False,
        score=90,
        issues=[issue],
    )
    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_qa",
        lambda *_args, **_kwargs: ([issue], evaluation),
    )
    published: list[str] = []
    monkeypatch.setattr(
        screenplay_repair,
        "publish_screenplay",
        lambda **kwargs: (
            published.append(kwargs["artifact_id"])
            or {"status": "ready", "artifact_id": kwargs["artifact_id"]}
        ),
    )

    with pytest.raises(screenplay_repair.ScreenplayNarrativeGateError):
        await screenplay_repair.run_screenplay_production(
            episode_id="ep_scene",
            episode={
                "id": "ep_scene",
                "project_id": "proj_scene",
                "episode_no": 1,
                "target_duration_s": 60,
            },
            source_text="原文",
            bible=Bible(
                characters=[],
                world=World(visual_style_canonical="测试画风"),
            ),
            resume=True,
        )

    resumed = screenplay_repair.get_production_revision(revision.id)
    assert resumed is not None
    assert resumed.working_artifact_id == artifact["id"]
    assert resumed.checkpoint_json["phase"] == "WAITING_HUMAN"
    assert published == []
