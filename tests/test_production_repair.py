"""Production Repair 不变量与局部 Patch 测试。"""
from __future__ import annotations

import pytest

from app import db, errors
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.production.certificate import (
    issue_completion_certificate,
    verify_completion_certificate,
)
from app.production.policy import FullRegenDenied, assert_baseline_allowed, assert_patch_ops_allowed
from app.production.revision import (
    ensure_production_revision,
    mark_baseline_generated,
    mark_first_evaluation,
)
from app.production.screenplay_document import (
    document_to_screenplay,
    screenplay_to_document,
    apply_field_patch,
)
from app.production.structured_issues import enrich_issues, issue_set_hash, structured_issue
from app.repair_router import route_issues, strategy_for_level, normalize_strategy
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    KeyDialogueChain,
    KeyDialogueTurn,
    PlotSpine,
    PlotSpineBeat,
    ScriptScene,
    World,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "prod-repair.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_p", "生产测试", "created", db.now()),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, created_at, screenplay_status) "
        "VALUES(?,?,?,?,?,?)",
        ("ep_p", "proj_p", 1, "第一集", db.now(), "pending"),
    )
    conn.commit()
    yield


def _minimal_script(**overrides) -> EpisodeScreenplay:
    data = dict(
        episode_no=1,
        title="测试",
        logline="主角要赢",
        dramatic_question="他能否赢？",
        protagonist_goal="赢得资格",
        obstacle="对手阻挠",
        stakes="",
        key_lines=["甲：开始"],
        key_plot_points=["对峙升级"],
        plot_spine=PlotSpine(
            episode_premise="主角要赢",
            spine_beats=[PlotSpineBeat(beat_id="S01", who="甲", does="应战", turn="局势紧张")],
            must_keep_ending="对峙未决",
            drop_list=["闲聊", "风景"],
        ),
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】夜 / 场地",
                story_function="开端",
                characters=["甲"],
                summary="对峙开始",
                conflict="敌意",
                turn="升级",
                source_basis="原文开头",
            )
        ],
        full_script_text="【场1】夜 / 场地\n甲站在场地中央。\n甲：开始",
        emotional_curve="紧张",
        ending_hook="无集级钩子",
        source_basis="原文",
    )
    data.update(overrides)
    return EpisodeScreenplay(**data)


def test_baseline_counter_and_policy_denies_second_generate():
    rev = ensure_production_revision(episode_id="ep_p", kind="screenplay", resume=False)
    assert rev.baseline_generation_count == 0
    mark_baseline_generated(rev.id, baseline_artifact_id="art_1")
    rev = mark_first_evaluation(rev.id, "eval_1")
    assert rev.baseline_done
    assert rev.first_evaluation_done
    with pytest.raises(FullRegenDenied) as exc:
        assert_baseline_allowed(rev, command="screenplay.generate", episode_id="ep_p")
    assert "FULL_REGEN_AFTER_QA_DENIED" in str(exc.value)


def test_patch_ops_reject_root_replace_and_delete_all():
    with pytest.raises(FullRegenDenied):
        assert_patch_ops_allowed([{"op": "replace", "path": "/", "value": {}}])
    with pytest.raises(FullRegenDenied):
        assert_patch_ops_allowed([
            {"op": "replace_field", "path": "scene_blocks", "value": [{"x": 1}, {"y": 2}]},
        ])
    with pytest.raises(FullRegenDenied):
        assert_patch_ops_allowed([
            {"op": "delete_node", "target": {"kind": "scene", "id": "*"}},
        ])
    # 合法单字段
    assert_patch_ops_allowed([
        {"op": "replace_field", "path": "stakes", "value": "失败将失去资格"},
    ])


def test_screenplay_document_patch_stakes_only():
    script = _minimal_script()
    doc = screenplay_to_document(script)
    patched, touched = apply_field_patch(
        doc, path="stakes", value="失败将失去资格与尊严",
        target={"kind": "metadata", "id": "stakes"},
    )
    out = document_to_screenplay(patched)
    assert out.stakes == "失败将失去资格与尊严"
    assert "meta:stakes" in touched
    # 无关场次标题保持
    assert out.scene_outline[0].scene_heading == script.scene_outline[0].scene_heading


def test_dialogue_chain_turn_patch_changes_only_opening_source_anchor():
    script = _minimal_script(dialogue_chains=[KeyDialogueChain(
        chain_id="DC1",
        topic="测验结果公开",
        turns=[KeyDialogueTurn(
            speaker="测验员",
            line="萧炎，斗之力，三段！级别：低级！",
            function="announcement",
            source_text="萧炎，斗之力，三段！级别：低级！",
        )],
    )])
    doc = screenplay_to_document(script)

    patched, touched = apply_field_patch(
        doc,
        path="source_text",
        value="斗之力，三段！",
        target={
            "kind": "dialogue_chain_turn",
            "chain_id": "DC1",
            "turn_index": 0,
        },
    )
    out = document_to_screenplay(patched)

    assert out.dialogue_chains[0].turns[0].source_text == "斗之力，三段！"
    assert out.dialogue_chains[0].turns[0].line == "萧炎，斗之力，三段！级别：低级！"
    assert out.full_script_text == script.full_script_text
    assert touched == ["DC1-T1", "DC1"]


def test_patch_planner_recognizes_legacy_rederive_and_repairs_opening_anchor():
    from app.production.screenplay_repair import (
        _patch_strategy_key,
        _strategy_was_tried,
        plan_screenplay_patch,
    )

    script = _minimal_script(dialogue_chains=[KeyDialogueChain(
        chain_id="DC1",
        topic="测验结果公开",
        turns=[KeyDialogueTurn(
            speaker="测验员",
            line="萧炎，斗之力，三段！级别：低级！",
            function="announcement",
            source_text="萧炎，斗之力，三段！级别：低级！",
        )],
    )])
    issue = structured_issue(
        code="SOURCE_FIDELITY",
        message=(
            "原文开场第一句对白未作为 dialogue_chains[0].turns[0]：斗之力，三段！；"
            "开场对白不能丢失"
        ),
        subject="screenplay",
        path="/dialogue_chains",
        rule_id="opening_anchor",
        stage="screenplay",
    )

    assert _strategy_was_tried(["rederive:"], "rederive")
    ops = plan_screenplay_patch(
        issue,
        script,
        strategy_history={issue.fingerprint: ["rederive:"]},
    )

    assert len(ops) == 1
    assert _patch_strategy_key(ops) == "fix_opening_source_anchor"
    assert ops[0].target["kind"] == "dialogue_chain_turn"
    assert ops[0].value == "斗之力，三段！"


def test_full_regen_denied_is_a_policy_conflict_not_a_media_error():
    assert errors.classify(FullRegenDenied("denied")) == (
        "conflict",
        "FULL-REGEN-DENIED",
    )


@pytest.mark.asyncio
async def test_existing_baseline_resumes_qa_without_calling_full_generation(monkeypatch):
    from app import stages
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_repair

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        resume=False,
    )
    script = _minimal_script(stakes="失败将失去资格")
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
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

    async def forbidden_baseline(*_args, **_kwargs):
        raise AssertionError("resume must not call full screenplay generation")

    monkeypatch.setattr(stages, "generate_screenplay_baseline", forbidden_baseline)
    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_qa",
        lambda *_args, **_kwargs: (
            [],
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="test",
                evaluator_version="1",
                status="passed",
                hard_gate_passed=True,
                score=100,
            ),
        ),
    )
    published: list[str] = []

    def fake_publish(**kwargs):
        published.append(kwargs["artifact_id"])
        return {"status": "ready", "artifact_id": kwargs["artifact_id"]}

    monkeypatch.setattr(screenplay_repair, "publish_screenplay", fake_publish)

    result = await screenplay_repair.run_screenplay_production(
        episode_id="ep_p",
        episode={
            "id": "ep_p",
            "project_id": "proj_p",
            "episode_no": 1,
            "target_duration_s": 50,
        },
        source_text="原文",
        bible=Bible(
            characters=[],
            world=World(visual_style_canonical="测试画风"),
        ),
        resume=True,
    )

    assert result.title == script.title
    assert published == [artifact["id"]]
    resumed = screenplay_repair.get_production_revision(revision.id)
    assert resumed is not None
    assert resumed.baseline_generation_count == 1
    assert resumed.checkpoint_json["phase"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_noop_patch_attempts_consume_activation_budget(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_repair
    from app.production.patch import PatchOperation, PatchResult

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        resume=False,
    )
    script = _minimal_script(stakes="失败将失去资格")
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
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
        message="测试同一问题没有进展",
        subject="screenplay",
        path="/test",
        rule_id="stalled",
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
    planned = 0

    def fake_plan(*_args, **_kwargs):
        nonlocal planned
        planned += 1
        return [PatchOperation(
            op="replace_field",
            path=f"attempt_{planned}",
            value="same",
            target={"kind": "test", "id": str(planned)},
        )]

    attempts = 0

    def fake_apply(request, *, episode_id):
        nonlocal attempts
        attempts += 1
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            error="no-op Patch 已拒绝",
        )

    monkeypatch.setattr(screenplay_repair, "plan_screenplay_patch", fake_plan)
    monkeypatch.setattr(screenplay_repair, "apply_screenplay_patch", fake_apply)
    monkeypatch.setattr(screenplay_repair, "MAX_REPAIR_ACTIVATION_PATCHES", 2)

    await screenplay_repair.run_screenplay_production(
        episode_id="ep_p",
        episode={
            "id": "ep_p",
            "project_id": "proj_p",
            "episode_no": 1,
            "target_duration_s": 50,
        },
        source_text="原文",
        bible=Bible(
            characters=[],
            world=World(visual_style_canonical="测试画风"),
        ),
        resume=True,
    )

    assert attempts == 2
    stalled = screenplay_repair.get_production_revision(revision.id)
    assert stalled is not None
    assert stalled.checkpoint_json["phase"] == "WAITING_RETRY"
    assert stalled.checkpoint_json["yield_reason"] == "activation_budget"


@pytest.mark.asyncio
async def test_recorded_repair_resume_skips_character_discovery_model_call(monkeypatch):
    from app.domain import screenplay_ops

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id="artifact-baseline",
        working_artifact_id="artifact-working",
    )

    async def forbidden_discovery(*_args, **_kwargs):
        raise AssertionError("repair resume must skip character discovery")

    captured_preflight: list[dict] = []

    async def fake_screenplay_task(_episode_id, *, preflight_result=None):
        captured_preflight.append(preflight_result)
        return _minimal_script(stakes="失败将失去资格")

    monkeypatch.setattr(
        screenplay_ops,
        "_screenplay_character_discovery",
        forbidden_discovery,
    )
    monkeypatch.setattr(screenplay_ops, "_screenplay_task", fake_screenplay_task)
    recorder = screenplay_ops._new_screenplay_recorder(
        "ep_p",
        trigger_type="resume",
    )

    result = await screenplay_ops._recorded_screenplay_task("ep_p", recorder)

    assert result is not None
    assert captured_preflight == [{
        "added": [],
        "skipped": "baseline_already_exists",
    }]


def test_structured_issue_has_stable_path():
    issues = enrich_issues([
        Issue(
            code="DRAMATIC_CONTRACT_INCOMPLETE",
            severity=IssueSeverity.BLOCKER,
            subject="screenplay",
            message="stakes 不能为空",
            repairable=True,
        )
    ], stage="screenplay")
    assert issues[0].evidence.get("path")
    assert issues[0].evidence.get("must_fix") is True
    assert issue_set_hash(issues)


def test_certificate_binds_hash_and_rejects_mismatch(monkeypatch):
    from app.evidence import repository as evidence_repository

    art = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_document",
            scope_type="episode",
            scope_id="ep_p",
            status="validated",
            trust_level="T2",
            content={"ok": True, "stakes": "代价"},
        )
    )
    h = art["content_hash"] or evidence_repository.content_hash(art["content"])
    cert = issue_completion_certificate(
        kind="screenplay",
        scope_id="ep_p",
        artifact_id=art["id"],
        artifact_hash=h,
        contract_version="1",
        qa_profile_version="screenplay-qa-1",
    )
    verify_completion_certificate(cert, expected_artifact_hash=h)
    with pytest.raises(ValueError):
        verify_completion_certificate(cert, expected_artifact_hash="deadbeef")


def test_repair_router_no_longer_emits_redo_or_replan():
    assert strategy_for_level("L3") == "insert_shot"
    assert strategy_for_level("L4") == "split_shot"
    assert normalize_strategy("redo_suffix") == "repair_window"
    assert normalize_strategy("replan_outline") == "insert_shot"

    plan = route_issues([
        structured_issue(
            code="SPINE_MISSING",
            message="第 5 镜缺少 must_keep spine",
            subject="shot:5",
            path="/shots/5",
            related_node_ids=["shot:5"],
            stage="storyboard",
        )
    ], validated_prefix_end=4, next_shot_no=5)
    assert plan.strategy == "insert_shot"
    assert plan.strategy not in {"redo_suffix", "replan_outline"}

    capacity = route_issues([
        structured_issue(
            code="SPOKEN_CAPACITY_EXCEEDED",
            message="第 9 镜必保留台词超过 10 秒容量，请拆镜",
            subject="shot:9",
            path="/shots/9",
            stage="storyboard",
        )
    ], validated_prefix_end=8, next_shot_no=9, current_level="L4")
    assert capacity.strategy in {"split_shot", "split_adjacent_shot"}
    assert capacity.strategy != "replan_outline"


def test_apply_screenplay_patch_cas_and_noop(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production.patch import PatchOperation, PatchRequest, apply_screenplay_patch
    from app.production.patch import screenplay_artifact_payload

    rev = ensure_production_revision(episode_id="ep_p", kind="screenplay", resume=False)
    script = _minimal_script(stakes="")
    payload = screenplay_artifact_payload(script)
    art = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_document",
            scope_type="episode",
            scope_id="ep_p",
            status="candidate",
            trust_level="T1",
            content=payload,
        )
    )
    mark_baseline_generated(rev.id, baseline_artifact_id=art["id"], working_artifact_id=art["id"])
    before_hash = art["content_hash"] or evidence_repository.content_hash(art["content"])

    ok = apply_screenplay_patch(
        PatchRequest(
            production_revision_id=rev.id,
            expected_artifact_id=art["id"],
            expected_hash=before_hash,
            operations=[PatchOperation(op="replace_field", path="stakes", value="失败失去资格")],
            idempotency_key="k1",
        ),
        episode_id="ep_p",
    )
    assert ok.ok
    assert ok.after_artifact_id
    assert ok.after_hash != before_hash

    # no-op
    art2 = evidence_repository.get_artifact(ok.after_artifact_id)
    h2 = art2["content_hash"] or evidence_repository.content_hash(art2["content"])
    noop = apply_screenplay_patch(
        PatchRequest(
            production_revision_id=rev.id,
            expected_artifact_id=ok.after_artifact_id,
            expected_hash=h2,
            operations=[PatchOperation(op="replace_field", path="stakes", value="失败失去资格")],
            idempotency_key="k2",
        ),
        episode_id="ep_p",
    )
    assert not noop.ok
    assert "no-op" in (noop.error or "")
