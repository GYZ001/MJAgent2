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
    bind_unpublished_revision_metadata,
    ensure_production_revision,
    mark_baseline_generated,
    mark_first_evaluation,
    rebind_input_fingerprint,
)
from app.production.screenplay_document import (
    document_to_screenplay,
    normalize_overdetail_text_fields,
    screenplay_to_document,
    apply_field_patch,
)
from app.production.structured_issues import enrich_issues, issue_set_hash, structured_issue
from app.repair_router import route_issues, strategy_for_level, normalize_strategy
from app.schemas import (
    Bible,
    Character,
    EpisodeScreenplay,
    KeyDialogueChain,
    KeyDialogueTurn,
    NarrativeContinuityPlan,
    NarrativeEvent,
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


def test_revision_authority_rebind_requires_current_working_artifact() -> None:
    from app.evidence import repository as evidence_repository

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        input_fingerprint="before",
        resume=False,
    )
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
        status="candidate",
        trust_level="T1",
        content={"title": "测试"},
    ))
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )

    rebound = rebind_input_fingerprint(
        revision.id,
        input_fingerprint="after",
        expected_working_artifact_id=artifact["id"],
    )

    assert rebound.input_fingerprint == "after"
    with pytest.raises(ValueError, match="CAS 冲突"):
        rebind_input_fingerprint(
            revision.id,
            input_fingerprint="stale",
            expected_working_artifact_id="art_stale",
        )


def test_unpublished_revision_metadata_only_fills_blank_values() -> None:
    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="storyboard",
        resume=False,
    )

    bound = bind_unpublished_revision_metadata(
        revision.id,
        input_fingerprint="board-v1",
        contract_version="3.0.0",
        qa_profile_version="storyboard-full-gate-2",
    )

    assert bound.input_fingerprint == "board-v1"
    assert bound.contract_version == "3.0.0"
    assert bound.qa_profile_version == "storyboard-full-gate-2"
    assert bind_unpublished_revision_metadata(
        revision.id,
        input_fingerprint="board-v1",
        contract_version="3.0.0",
        qa_profile_version="storyboard-full-gate-2",
    ).id == revision.id
    with pytest.raises(ValueError, match="已绑定其他版本"):
        bind_unpublished_revision_metadata(
            revision.id,
            input_fingerprint="board-v2",
            contract_version="3.0.0",
            qa_profile_version="storyboard-full-gate-2",
        )


def test_screenplay_qa_is_read_only_and_nonblocking() -> None:
    from app.production.screenplay_repair import run_screenplay_qa

    script = _minimal_script()
    before = script.model_dump_json()

    issues, evaluation = run_screenplay_qa(
        script,
        bible=Bible(characters=[], world=World(visual_style_canonical="测试画风")),
        source_text="甲：开始",
        episode={
            "episode_no": 1,
            "target_duration_s": 50,
            "required_dialogue_lines": [],
        },
    )

    assert script.model_dump_json() == before
    assert issues
    assert evaluation.status == "warning"
    assert evaluation.evaluation_role == "score_only"
    assert evaluation.runtime_blocking is False
    assert evaluation.retry_eligible is False


def test_unresolved_character_identity_is_reported_without_qa_blocking() -> None:
    from app.production.screenplay_repair import (
        non_waivable_screenplay_issues,
        run_screenplay_qa,
    )

    script = _minimal_script()
    issues, evaluation = run_screenplay_qa(
        script,
        bible=Bible(
            characters=[],
            world=World(visual_style_canonical="测试画风"),
        ),
        source_text="甲：开始",
        episode={
            "episode_no": 1,
            "target_duration_s": 50,
            "required_dialogue_lines": [],
        },
    )
    # 无真实 Bible 的历史占位流程不开身份门禁。
    assert non_waivable_screenplay_issues(issues) == []
    assert evaluation.runtime_blocking is False

    issues, evaluation = run_screenplay_qa(
        script,
        bible=Bible(
            characters=[Character(
                name="萧炎",
                role="主角",
                appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
            )],
            world=World(visual_style_canonical="测试画风"),
        ),
        source_text="甲：开始",
        episode={
            "episode_no": 1,
            "target_duration_s": 50,
            "required_dialogue_lines": [],
        },
    )
    hard = non_waivable_screenplay_issues(issues)
    assert hard and all(issue.code == "CHARACTER_IDENTITY_UNRESOLVED" for issue in hard)
    assert evaluation.evaluation_role == "score_only"
    assert evaluation.runtime_blocking is False
    assert evaluation.retry_eligible is False


@pytest.mark.asyncio
async def test_retry_exhaustion_never_publishes_unresolved_character_identity(monkeypatch):
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
    issue = structured_issue(
        code="CHARACTER_IDENTITY_UNRESOLVED",
        message="剧本人物身份未解决：「青衣人」",
        subject="screenplay",
        path="/character_identities",
        rule_id="character_identity_must_resolve_before_publish",
        stage="screenplay",
    )
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="test",
        evaluator_version="1",
        status="failed",
        hard_gate_passed=False,
        evaluation_role="business_safety",
        runtime_blocking=True,
        score=0,
        issues=[issue],
    )
    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_qa",
        lambda *_args, **_kwargs: ([issue], evaluation),
    )
    monkeypatch.setattr(screenplay_repair, "plan_screenplay_patch", lambda *_args, **_kwargs: [])

    async def no_llm_patch(*_args, **_kwargs):
        return []

    monkeypatch.setattr(screenplay_repair, "_llm_field_patch", no_llm_patch)

    def forbidden_publish(**_kwargs):
        raise AssertionError("人物身份 blocker 不得发布")

    monkeypatch.setattr(screenplay_repair, "publish_screenplay", forbidden_publish)

    with pytest.raises(
        screenplay_repair.ScreenplayIdentityGateError,
        match="缺少可确定的人物身份上下文",
    ):
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
                characters=[Character(
                    name="萧炎",
                    role="主角",
                    appearance_canonical="黑发少年，玄色劲装，目光坚定",
                )],
                world=World(visual_style_canonical="测试画风"),
            ),
            resume=True,
        )

    row = db.get_conn().execute(
        "SELECT screenplay_status, screenplay_error FROM episodes WHERE id='ep_p'"
    ).fetchone()
    assert row["screenplay_status"] == "failed"
    assert "人物身份" in row["screenplay_error"]


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


def test_create_narrative_node_does_not_treat_event_reference_as_node_identity():
    from app.production.patch import PatchOperation, _create_node

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan(
            scope_id="ep_p",
            events=[NarrativeEvent(event_id="E-5")],
        ),
    )
    operation = PatchOperation(
        op="create_node",
        target={
            "kind": "narrative_node",
            "collection": "setup_payoff_contracts",
            "id": "SP-2",
        },
        value={
            "setup_payoff_id": "SP-2",
            "setup_proposition_ids": ["P-1"],
            "setup_event_ids": ["E-1"],
            "payoff_event_ids": ["E-5"],
            "retention_deadline_event_id": "E-5",
        },
    )

    patched, touched = _create_node(screenplay_to_document(script), operation)

    assert patched.narrative_plan.setup_payoff_contracts[0].setup_payoff_id == "SP-2"
    assert patched.narrative_plan.setup_payoff_contracts[0].retention_deadline_event_id == "E-5"
    assert touched == ["narrative:setup_payoff_contracts:SP-2"]


def test_narrative_graph_normalizer_repairs_unique_source_span_and_event_aliases():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    chapter = "前文。又落榜了……后文。"
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-1",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": "又落榜了……",
            }],
            "adaptation_decisions": [{
                "adaptation_decision_id": "AD-1",
                "affected_event_ids": ["E1"],
            }],
            "dramatic_questions": [{
                "dramatic_question_id": "DQ-1",
                "question_text": "他会如何选择？",
                "open_anchor": {"type": "event", "id": "E1"},
                "resolution_anchor": {"type": "event", "id": "E2"},
            }],
            "evidence": [{
                "evidence_id": "EV-1",
                "anchor": {"type": "event", "id": "E1"},
                "observable_claim": "他再次落榜。",
            }],
            "state_facts": [{
                "fact_id": "F-1",
                "proposition_id": "P-1",
                "subject_id": "甲",
                "predicate_id": "state",
            }],
            "events": [
                {"event_id": "E-1", "precondition_fact_ids": ["F-1"]},
                {"event_id": "E-2"},
            ],
            "character_beliefs": [{
                "character_belief_id": "CB-1",
                "character_id": "甲",
                "anchor": {"type": "event", "id": "E1"},
                "beliefs": [{
                    "proposition_id": "P-1",
                    "stance": "disbelieved",
                }],
            }],
            "setup_payoff_contracts": [{
                "setup_payoff_id": "SP-1",
                "setup_event_ids": ["E1"],
                "payoff_event_ids": ["E2"],
                "retention_deadline_event_id": "E2",
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    source = script.narrative_plan.source_evidence[0]
    assert source.source_span.start == chapter.index(source.verbatim_excerpt)
    assert source.source_span.end == source.source_span.start + len(
        source.verbatim_excerpt
    )
    assert script.narrative_plan.adaptation_decisions[0].affected_event_ids == [
        "E-1"
    ]
    assert script.narrative_plan.dramatic_questions[0].open_anchor.id == "E-1"
    assert script.narrative_plan.dramatic_questions[0].resolution_anchor.id == "E-2"
    assert script.narrative_plan.evidence[0].anchor.id == "E-1"
    payoff = script.narrative_plan.setup_payoff_contracts[0]
    assert payoff.setup_event_ids == ["E-1"]
    assert payoff.payoff_event_ids == ["E-2"]
    assert payoff.retention_deadline_event_id == "E-2"
    assert script.narrative_plan.events[0].proposition_ids == ["P-1"]
    assert script.narrative_plan.character_beliefs[0].beliefs[0].stance == "rejected"
    assert {item["kind"] for item in changes} == {
        "source_span",
        "event_ref",
        "event_refs",
        "event_proposition_refs",
        "belief_stance",
    }


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


def test_screenplay_projection_separates_action_labels_and_deduplicates_dialogue():
    script = _minimal_script(
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="任务",
            turns=[KeyDialogueTurn(
                speaker="甲",
                line="今晚七点以前完成。",
                function="announcement",
                source_text="今晚七点以前完成。",
            )],
        )],
        full_script_text=(
            "【场1】夜 / 场地\n"
            "甲侧身让出门缝，看向乙：今晚七点以前完成。\n"
            "甲：今晚七点以前完成。\n"
            "银幕上出现画面：旧钟楼在雨中亮起。\n"
            "陌生杀手：你们来晚了。"
        ),
    )

    result = document_to_screenplay(screenplay_to_document(script))

    assert result.full_script_text.count("甲：今晚七点以前完成。") == 1
    assert "甲侧身让出门缝，看向乙：" not in result.full_script_text
    assert "银幕上出现画面，旧钟楼在雨中亮起。" in result.full_script_text
    assert "银幕上出现画面：" not in result.full_script_text
    assert "陌生杀手：你们来晚了。" in result.full_script_text


def test_overdetail_normalizer_only_changes_visual_description() -> None:
    script = _minimal_script(
        full_script_text=(
            "【场1】夜 / 场地\n"
            "甲攥紧衣角并站定。\n"
            "甲：我只是说了‘衣角’两个字。"
        ),
    )
    doc = screenplay_to_document(script)

    patched, touched = normalize_overdetail_text_fields(doc, terms=["衣角"])
    out = document_to_screenplay(patched)

    assert "甲攥紧并站定。" in out.full_script_text
    assert "甲：我只是说了‘衣角’两个字。" in out.full_script_text
    assert touched


def test_patch_planner_normalizes_overdetail_without_model_call() -> None:
    from app.production.screenplay_repair import _patch_strategy_key, plan_screenplay_patch

    issue = structured_issue(
        code="OVERDETAIL",
        message="full_script_text 含超纲细节词：衣角；请删除服饰细节",
        subject="screenplay",
        path="/full_script_text",
        rule_id="renderability_overdetail",
        stage="screenplay",
    )

    ops = plan_screenplay_patch(issue, _minimal_script())

    assert len(ops) == 1
    assert ops[0].op == "normalize_overdetail"
    assert ops[0].value == {"terms": ["衣角"]}
    assert _patch_strategy_key(ops) == "normalize_overdetail"


def test_dialogue_chain_turn_patch_changes_only_opening_source_anchor():
    script = _minimal_script(
        full_script_text=(
            "【场1】夜 / 场地\n"
            "甲站在场地中央。\n"
            "测验员：萧炎，斗之力，三段！级别：低级！"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="测验结果公开",
            turns=[KeyDialogueTurn(
                speaker="测验员",
                line="萧炎，斗之力，三段！级别：低级！",
                function="announcement",
                source_text="萧炎，斗之力，三段！级别：低级！",
            )],
        )],
    )
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


def test_dialogue_chain_replacement_only_selects_existing_body_turns() -> None:
    from app.production.screenplay_repair import (
        _dialogue_chain_replacement_is_local,
        _normalize_dialogue_source_references,
    )

    script = _minimal_script(
        full_script_text=(
            "【场1】夜 / 场地\n"
            "甲：救命。\n"
            "乙：你怎么在这里？\n"
            "丙（大声），飞！\n"
            "甲：别听他说，我是被抓来的。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="获救说明",
                turns=[
                    KeyDialogueTurn(
                        speaker="甲",
                        line="救命。",
                        function="trigger",
                        source_text="救命。",
                    ),
                    KeyDialogueTurn(
                        speaker="甲",
                        line="别听他说，我是被抓来的。",
                        function="response",
                        source_text="别听他说，我是被抓来的。",
                    ),
                ],
            ),
        ],
    )
    document = screenplay_to_document(script)
    repaired_turns = [
        {
            "speaker": "甲",
            "line": "救命。",
            "function": "trigger",
            "source_text": "救命。",
        },
        {
            "speaker": "乙",
            "line": "你怎么在这里？",
            "function": "question",
            "source_text": "你怎么在这里？",
        },
        {
            "speaker": "丙",
            "line": "飞！",
            "function": "statement",
            "source_text": "飞！",
        },
        {
            "speaker": "甲",
            "line": "别听他说，我是被抓来的。",
            "function": "response",
            "source_text": "别听他说，我是被抓来的。",
        },
    ]

    assert _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC1",
        turns=repaired_turns,
    )
    assert not _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC1",
        turns=repaired_turns + [{
            "speaker": "乙",
            "line": "正文中不存在的新增对白。",
            "function": "statement",
            "source_text": "原文叙述",
        }],
    )
    assert not _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC1",
        turns=repaired_turns[1:],
    )
    patched, _ = apply_field_patch(
        document,
        path="turns",
        value=repaired_turns,
        target={"kind": "future_dialogue_container", "id": "DC1"},
    )
    projected = document_to_screenplay(patched)
    assert "丙：飞！" in projected.full_script_text
    assert "丙（大声），飞！" not in projected.full_script_text

    source = (
        "“不止是我，还有附近县其他几人，我们都在这里，"
        "孟兄先别说了，快救我们出去。”"
    )
    normalized = _normalize_dialogue_source_references(
        {
            "speaker": "甲",
            "line": "不止是我，还有附近县其他几人，孟兄快救我们出去。",
            "source_text": "不止是我，还有附近县其他几人，孟兄快救我们出去。",
        },
        source,
    )
    assert normalized["source_text"] in source
    assert "我们都在这里" in normalized["source_text"]


def test_document_projection_inserts_missing_chain_turn_next_to_sibling() -> None:
    script = _minimal_script(
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】夜 / 放映室",
                story_function="更换保险管并恢复供电",
                characters=["林澈", "苏禾"],
                summary="林澈确认安全后示意苏禾合闸。",
                conflict="设备能否安全通电",
                turn="放映机恢复供电",
                source_basis="原文合闸段落",
            ),
        ],
        full_script_text=(
            "【场1】夜 / 放映室\n"
            "林澈盖好保险仓，退到安全线外。\n"
            "林澈：保险仓盖好了，退到安全线外。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC2",
                topic="确认合闸时机",
                turns=[
                    KeyDialogueTurn(
                        speaker="林澈",
                        line="保险仓盖好了，退到安全线外。",
                        function="statement",
                        source_text="林澈盖好保险仓、退到安全线外。",
                    ),
                    KeyDialogueTurn(
                        speaker="苏禾",
                        line="合闸。",
                        function="decision",
                        source_text="苏禾合上电闸。",
                    ),
                ],
            ),
        ],
    )

    projected = document_to_screenplay(screenplay_to_document(script))
    projected_twice = document_to_screenplay(screenplay_to_document(projected))

    assert "林澈：保险仓盖好了，退到安全线外。\n苏禾：合闸。" in projected.full_script_text
    assert projected.full_script_text.count("苏禾：合闸。") == 1
    assert projected_twice.full_script_text.count("苏禾：合闸。") == 1


def test_document_projection_removes_legacy_cross_scene_prefixed_duplicate() -> None:
    script = _minimal_script(
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】夜 / 山顶",
                story_function="建立等待状态",
                characters=["甲"],
                summary="甲在山顶等待消息并观察远处动静。",
                conflict="消息迟迟没有出现",
                turn="远处传来呼喊",
                source_basis="甲在山顶等待消息。",
            ),
            ScriptScene(
                scene_no=2,
                scene_heading="【场2】夜 / 山腰",
                story_function="完成问答并交付真相",
                characters=["甲", "乙"],
                summary="乙追问真相，甲说明自己被人抓来。",
                conflict="乙不确定甲是否可信",
                turn="甲交代被抓经过",
                source_basis="甲说明自己被人抓来。",
            ),
        ],
        full_script_text=(
            "【场1】夜 / 山顶\n"
            "甲：甲：别听他说，我是被抓来的。\n"
            "【场2】夜 / 山腰\n"
            "乙：你怎么在这里？\n"
            "甲：别听他说，我是被抓来的。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="被抓经过",
                turns=[
                    KeyDialogueTurn(
                        speaker="乙",
                        line="你怎么在这里？",
                        function="question",
                        source_text="你怎么在这里？",
                    ),
                    KeyDialogueTurn(
                        speaker="甲",
                        line="别听他说，我是被抓来的。",
                        function="response",
                        source_text="别听他说，我是被抓来的。",
                    ),
                ],
            ),
        ],
    )

    projected = document_to_screenplay(screenplay_to_document(script))

    assert "甲：甲：别听他说" not in projected.full_script_text
    assert projected.full_script_text.count("甲：别听他说，我是被抓来的。") == 1
    assert projected.scene_outline[1].scene_no == 2


def test_context_gap_skips_unsafe_rederive_and_trigger_insertion() -> None:
    from app.production.screenplay_repair import plan_screenplay_patch

    issue = structured_issue(
        code="KEY_LINE_MISSING",
        message=(
            "主线对白上下文断裂：甲：别听他说，我是被抓来的。；"
            "必须把同一场前两轮内另一角色的触发台词也列入 key_lines"
        ),
        subject="screenplay",
        path="/dialogue_chains",
        rule_id="key_line_context",
        stage="screenplay",
    )

    assert plan_screenplay_patch(issue, _minimal_script()) == []


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


def test_patch_planner_repairs_semantically_unrelated_first_turn_source():
    from app.production.screenplay_repair import (
        _patch_strategy_key,
        plan_screenplay_patch,
    )

    script = _minimal_script(dialogue_chains=[KeyDialogueChain(
        chain_id="DC1",
        topic="校长召见",
        turns=[KeyDialogueTurn(
            speaker="白洁",
            line="校长，您找我？",
            function="question",
            source_text="砰、砰",
        )],
    )])
    source = "门外传来“砰、砰”的敲击声。白洁问：“校长，您找我？”"
    issue = structured_issue(
        code="SOURCE_FIDELITY",
        message=(
            "dialogue_chains[0].turns[0].source_text 与改编台词语义不匹配："
            "原文证据「砰、砰」→台词「校长，您找我？」"
        ),
        subject="screenplay",
        path="/dialogue_chains",
        rule_id="opening_anchor",
        stage="screenplay",
    )

    ops = plan_screenplay_patch(issue, script, source_text=source)

    assert len(ops) == 1
    assert ops[0].target["chain_id"] == "DC1"
    assert ops[0].target["turn_index"] == 0
    assert ops[0].value == "校长，您找我？"
    assert _patch_strategy_key(ops) == "fix_dialogue_source_DC1_0"


def test_patch_planner_delivers_missing_spine_with_one_action_node():
    from app.production.patch import apply_patch_operation_to_document
    from app.production.screenplay_repair import (
        _patch_strategy_key,
        plan_screenplay_patch,
    )
    from app.validators import validate_screenplay_spine_delivery

    script = _minimal_script()
    issue = structured_issue(
        code="SPINE_MISSING",
        message=(
            "full_script_text 未交付 1 条 must_keep 主线节拍："
            "S01/甲:应战；必须在对应场次的动作段或角色对白中完整演出"
        ),
        subject="screenplay",
        path="/nodes/S01",
        related_node_ids=["S01"],
        rule_id="spine_delivery",
        stage="screenplay",
    )

    ops = plan_screenplay_patch(issue, script)

    assert len(ops) == 1
    assert ops[0].op == "create_node"
    assert ops[0].target["kind"] == "action_block"
    assert ops[0].target["scene_id"] == "SC01"
    assert ops[0].value == {
        "action_id": "AC-SPINE-S01",
        "text": "甲应战。",
    }
    assert _patch_strategy_key(ops) == "deliver_spine_S01"

    patched, touched = apply_patch_operation_to_document(
        screenplay_to_document(script),
        ops[0],
    )
    projected = document_to_screenplay(patched)

    assert touched == ["AC-SPINE-S01", "SC01"]
    assert "甲应战。" in projected.full_script_text
    assert validate_screenplay_spine_delivery(
        projected,
        action_text=projected.full_script_text,
    ) == []


def test_patch_planner_repairs_indexed_source_placeholder_with_exact_source() -> None:
    from app.production.screenplay_repair import _patch_strategy_key, plan_screenplay_patch

    source = (
        "铁柜没有上锁。苏禾拉开柜门，里面没有胶片，只有一只蓝色铁盒。"
        "林澈接过铁盒，翻到背面，看见父亲留下的手写标签。"
    )
    script = _minimal_script(
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="任务开始",
                turns=[
                    KeyDialogueTurn(
                        speaker="苏禾",
                        line="先检查铁柜。",
                        function="announcement",
                        source_text="铁柜没有上锁。",
                    ),
                ],
            ),
            KeyDialogueChain(
                chain_id="DC2",
                topic="发现蓝色铁盒",
                turns=[
                    KeyDialogueTurn(
                        speaker="林澈",
                        line="柜里没有胶片。只有这个。",
                        function="statement",
                        source_text="（原文叙述转为对白）",
                    ),
                ],
            ),
        ],
        full_script_text=(
            "【场1】夜 / 放映室\n"
            "苏禾：先检查铁柜。\n"
            "林澈：柜里没有胶片。只有这个。"
        ),
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】夜 / 放映室",
                story_function="检查铁柜并发现蓝色铁盒",
                characters=["林澈", "苏禾"],
                summary="苏禾打开铁柜，林澈发现里面没有胶片。",
                conflict="胶片缺失",
                turn="转而检查蓝色铁盒",
                source_basis="铁柜里没有胶片，只有蓝色铁盒。",
            ),
        ],
    )
    issue = structured_issue(
        code="SOURCE_FIDELITY",
        message=(
            "dialogue_chains[1].turns[0].source_text 未在本集原文中找到："
            "（原文叙述转为对白）"
        ),
        subject="screenplay",
        path="/dialogue_chains",
        rule_id="source_fidelity",
        stage="screenplay",
    )

    ops = plan_screenplay_patch(issue, script, source_text=source)

    assert len(ops) == 1
    assert ops[0].target["chain_id"] == "DC2"
    assert ops[0].target["turn_index"] == 0
    assert ops[0].value == "苏禾拉开柜门，里面没有胶片，只有一只蓝色铁盒。"
    assert ops[0].value in source
    assert _patch_strategy_key(ops) == "fix_dialogue_source_DC2_0"


def test_patch_planner_does_not_treat_generic_source_mismatch_as_opening_anchor() -> None:
    from app.production.screenplay_repair import plan_screenplay_patch

    script = _minimal_script(dialogue_chains=[KeyDialogueChain(
        chain_id="DC1",
        topic="任务开始",
        turns=[KeyDialogueTurn(
            speaker="甲",
            line="开始。",
            function="announcement",
            source_text="原文开场。",
        )],
    )])
    issue = structured_issue(
        code="SOURCE_FIDELITY",
        message=(
            "dialogue_chains[9].turns[9].source_text 未在本集原文中找到："
            "（原文叙述转为对白）"
        ),
        subject="screenplay",
        path="/dialogue_chains",
        stage="screenplay",
    )

    assert plan_screenplay_patch(issue, script, source_text="原文开场。") == []


def test_patch_planner_relabels_invalid_same_speaker_response() -> None:
    from app.production.screenplay_repair import _patch_strategy_key, plan_screenplay_patch

    script = _minimal_script(dialogue_chains=[KeyDialogueChain(
        chain_id="DC1",
        topic="萧炎连续自语",
        turns=[
            KeyDialogueTurn(
                speaker="萧炎", line="十五年了。", function="statement", source_text="十五年了。",
            ),
            KeyDialogueTurn(
                speaker="萧炎", line="为什么偏偏是我？", function="response", source_text="为什么偏偏是我？",
            ),
        ],
    )])
    issue = structured_issue(
        code="KEY_LINE_MISSING",
        message="dialogue_chains[0].turns[1] 是 response，但前一话轮没有另一角色的触发台词",
        subject="screenplay",
        path="/dialogue_chains",
        rule_id="response_requires_trigger",
        stage="screenplay",
    )

    ops = plan_screenplay_patch(issue, script)

    assert len(ops) == 1
    assert ops[0].path == "function"
    assert ops[0].value == "statement"
    assert ops[0].target["chain_id"] == "DC1"
    assert _patch_strategy_key(ops) == "fix_dialogue_function_DC1_1"


def test_cross_scene_dialogue_chain_is_split_without_rewriting_body() -> None:
    from app.production.screenplay_document import (
        document_to_screenplay,
        screenplay_to_document,
        split_dialogue_chain_by_scene,
    )
    from app.production.screenplay_repair import _patch_strategy_key, plan_screenplay_patch

    script = _minimal_script(dialogue_chains=[KeyDialogueChain(
        chain_id="DC1",
        topic="宣布与回应",
        turns=[
            KeyDialogueTurn(speaker="甲", line="结果公布。", function="announcement", source_text="结果公布。"),
            KeyDialogueTurn(speaker="乙", line="我不接受。", function="response", source_text="我不接受。"),
            KeyDialogueTurn(speaker="乙", line="你们若是当事人呢？", function="question", source_text="你们若是当事人呢？"),
            KeyDialogueTurn(speaker="丙", line="他有权回答。", function="statement", source_text="他有权回答。"),
        ],
    )])
    script.scene_outline = [
        ScriptScene(scene_no=1, scene_heading="【场1】日 / 大厅", story_function="公布结果并引发拒绝",
                    characters=["甲", "乙"], summary="公布结果", conflict="乙拒绝", turn="乙开始反问", source_basis="原文"),
        ScriptScene(scene_no=2, scene_heading="【场2】日 / 大厅", story_function="反问促使立场改变",
                    characters=["乙", "丙"], summary="乙反问", conflict="立场冲突", turn="丙支持乙", source_basis="原文"),
        ScriptScene(scene_no=3, scene_heading="【场3】日 / 大厅", story_function="收束对峙并交付结果",
                    characters=["乙"], summary="对峙收束", conflict="余波", turn="局势定型", source_basis="原文"),
    ]
    script.full_script_text = (
        "【场1】日 / 大厅\n甲（宣布）：结果公布。\n乙（拒绝）：我不接受。\n\n"
        "【场2】日 / 大厅\n乙（追问）：你们若是当事人呢？\n丙（支持）：他有权回答。\n\n"
        "【场3】日 / 大厅\n乙转身离开。"
    )
    issue = structured_issue(
        code="KEY_LINE_MISSING",
        message="dialogue_chains[0] 被拆到多个场次；同一触发→回应链必须在同一场完成",
        subject="screenplay", path="/dialogue_chains", rule_id="chain_scene", stage="screenplay",
    )

    ops = plan_screenplay_patch(issue, script)
    assert len(ops) == 1
    assert ops[0].op == "split_dialogue_chain_by_scene"
    assert _patch_strategy_key(ops) == "split_dialogue_chain_DC1"

    document = screenplay_to_document(script)
    before_body = document_to_screenplay(document).full_script_text
    patched, touched = split_dialogue_chain_by_scene(document, chain_id="DC1")
    result = document_to_screenplay(patched)
    assert [len(chain.turns) for chain in result.dialogue_chains] == [2, 2]
    assert result.dialogue_chains[0].chain_id == "DC1"
    assert result.dialogue_chains[1].chain_id == "DC2"
    assert result.full_script_text == before_body
    assert touched == ["dialogue_chains", "DC1", "DC2"]


@pytest.mark.asyncio
async def test_narrative_screenplay_uses_deterministic_document_patch_first(
    monkeypatch,
) -> None:
    from app.production import screenplay_repair

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan(scope_id="ep_p"),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="宣布与回应",
            turns=[
                KeyDialogueTurn(
                    speaker="甲",
                    line="结果公布。",
                    function="announcement",
                    source_text="结果公布。",
                ),
                KeyDialogueTurn(
                    speaker="乙",
                    line="我不接受。",
                    function="response",
                    source_text="我不接受。",
                ),
            ],
        )],
    )
    issue = structured_issue(
        code="KEY_LINE_MISSING",
        message="dialogue_chains[0] 被拆到多个场次；同一触发→回应链必须在同一场完成",
        subject="screenplay",
        path="/dialogue_chains",
        stage="screenplay",
    )

    async def forbidden_semantic_planner(*_args, **_kwargs):
        raise AssertionError("文档结构问题不应调用叙事图语义规划器")

    monkeypatch.setattr(
        screenplay_repair,
        "_llm_field_patch",
        forbidden_semantic_planner,
    )

    operations = await screenplay_repair._plan_screenplay_repair_operations(
        issue,
        script,
        source_text="原文",
        strategy_history={},
    )

    assert len(operations) == 1
    assert operations[0].op == "split_dialogue_chain_by_scene"


def test_decision_chain_patch_is_derived_from_unique_perceivable_evidence():
    from app.production.patch import _create_node
    from app.production.screenplay_document import screenplay_to_document
    from app.production.screenplay_repair import plan_screenplay_patch

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "events": [{
                "event_id": "E4",
                "proposition_ids": ["P-5"],
                "action_ids": ["A2"],
            }],
            "atomic_actions": [{
                "action_id": "A2",
                "actor_ids": ["char-a"],
                "semantic_intent": "角色执行动作",
                "completion_condition": "动作完成",
                "decision_requirement": "applies",
                "temporal_phases": [{
                    "phase_id": "A2/P1",
                    "start_condition": "收到请求",
                    "end_condition": "完成动作",
                    "estimated_min_s": 1.0,
                }],
            }],
            "evidence": [{
                "evidence_id": "EV-4",
                "anchor": {"type": "event", "id": "E4"},
                "observable_claim": "角色收到请求并执行动作",
                "perceivable_by": ["char-a", "audience"],
                "supports_proposition_ids": ["P-5"],
            }],
        }),
    )
    issue = structured_issue(
        code="CHARACTER_DECISION_CHAIN_MISSING",
        message=(
            "[CHARACTER_DECISION_CHAIN_MISSING] "
            "E4/A2 的执行者 char-a 缺少感知→判断→选择依据"
        ),
        subject="screenplay",
        path="/nodes/E4",
        stage="screenplay",
    )

    operations = plan_screenplay_patch(issue, script)

    assert len(operations) == 1
    operation = operations[0]
    assert operation.op == "create_node"
    assert operation.target["collection"] == "character_beliefs"
    assert operation.value["decision_action_ids"] == ["A2"]
    assert operation.value["decision_basis_ids"] == ["EV-4"]
    patched, _ = _create_node(screenplay_to_document(script), operation)
    belief = patched.narrative_plan.character_beliefs[0]
    assert belief.character_id == "char-a"
    assert belief.anchor.id == "E4"


def test_belief_delta_patch_binds_exact_state_and_fills_assumed_unknowns():
    from app.production.screenplay_repair import (
        _patch_strategy_key,
        plan_screenplay_patch,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "audience_priors": [{
                "audience_prior_id": "AP-1",
                "scope_id": "ep_p",
                "audience_description": "首次观看",
                "assumed_unknown_proposition_ids": ["P-1", "P-2"],
            }],
            "audience_states": [
                {
                    "audience_state_id": "AS-IN",
                    "audience_prior_id": "AP-1",
                    "anchor": {"type": "event", "id": "E1"},
                    "beliefs": [{
                        "proposition_id": "P-1",
                        "stance": "unknown",
                        "confidence": 0.0,
                    }],
                },
                {
                    "audience_state_id": "AS-OUT",
                    "audience_prior_id": "AP-1",
                    "anchor": {"type": "event", "id": "E2"},
                    "beliefs": [
                        {
                            "proposition_id": "P-1",
                            "stance": "believed",
                            "confidence": 1.0,
                        },
                        {
                            "proposition_id": "P-2",
                            "stance": "believed",
                            "confidence": 1.0,
                        },
                    ],
                },
            ],
            "experience_intents": [{
                "experience_intent_id": "XI-1",
                "scope_id": "ep_p",
                "director_objective": "建立认知",
                "audience_paths": [{
                    "audience_path_id": "XP-1",
                    "audience_prior_id": "AP-1",
                    "audience_state_in_id": "AS-IN",
                    "audience_state_out_target_id": "AS-OUT",
                    "target_deltas": [{
                        "target_delta_id": "XD-1",
                        "dimension": "belief",
                        "proposition_ids": ["P-1", "P-2"],
                        "description": "建立两个信念",
                        "from_state": {"stance": "unknown"},
                        "to_state": {"stance": "believed"},
                        "deadline_event_id": "E2",
                    }],
                }],
            }],
        }),
    )
    issue = structured_issue(
        code="TARGET_DELTA_FROM_STATE_MISMATCH",
        message=(
            "[TARGET_DELTA_FROM_STATE_MISMATCH] XD-1.from_state "
            "不是该观众路径入场状态的真实结构片段"
        ),
        subject="screenplay",
        path="/target_delta_from_state_mismatch",
        stage="screenplay",
    )

    operations = plan_screenplay_patch(issue, script)

    assert len(operations) == 2
    assert operations[0].target["id"] == "XD-1"
    assert operations[0].value["beliefs"][1] == {
        "proposition_id": "P-2",
        "stance": "unknown",
        "confidence": 0.0,
        "evidence_ids": [],
    }
    assert operations[1].target["id"] == "AS-IN"
    assert _patch_strategy_key(operations) == "replace_field:XD-1:from_state"


def test_unassigned_audience_state_fields_align_to_prior_contract():
    from app.production.screenplay_repair import plan_screenplay_patch

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "audience_priors": [
                {
                    "audience_prior_id": "AP-1",
                    "scope_id": "ep_p",
                    "audience_description": "首次观看",
                },
                {
                    "audience_prior_id": "AP-2",
                    "scope_id": "ep_p",
                    "audience_description": "已知剧情",
                    "assumed_known_proposition_ids": ["P-1", "P-2"],
                },
            ],
            "audience_states": [
                {
                    "audience_state_id": "AS-1-IN",
                    "audience_prior_id": "AP-1",
                    "anchor": {"type": "event", "id": "E1"},
                },
                {
                    "audience_state_id": "AS-1-OUT",
                    "audience_prior_id": "AP-1",
                    "anchor": {"type": "event", "id": "E2"},
                    "working_memory": [{
                        "proposition_id": "P-1",
                        "retention_confidence": 0.9,
                    }],
                },
                {
                    "audience_state_id": "AS-2-IN",
                    "audience_prior_id": "AP-2",
                    "anchor": {"type": "event", "id": "E1"},
                    "beliefs": [{
                        "proposition_id": "P-1",
                        "stance": "believed",
                        "confidence": 1.0,
                    }],
                },
                {
                    "audience_state_id": "AS-2-OUT",
                    "audience_prior_id": "AP-2",
                    "anchor": {"type": "event", "id": "E2"},
                    "beliefs": [{
                        "proposition_id": "P-2",
                        "stance": "believed",
                        "confidence": 1.0,
                        "evidence_ids": ["EV-2"],
                    }],
                },
            ],
            "experience_intents": [{
                "experience_intent_id": "XI-1",
                "scope_id": "ep_p",
                "director_objective": "观众状态",
                "audience_paths": [
                    {
                        "audience_path_id": "XP-1",
                        "audience_prior_id": "AP-1",
                        "audience_state_in_id": "AS-1-IN",
                        "audience_state_out_target_id": "AS-1-OUT",
                    },
                    {
                        "audience_path_id": "XP-2",
                        "audience_prior_id": "AP-2",
                        "audience_state_in_id": "AS-2-IN",
                        "audience_state_out_target_id": "AS-2-OUT",
                    },
                ],
            }],
        }),
    )
    memory_issue = structured_issue(
        code="AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED",
        message=(
            "[AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED] XP-1 "
            "入/出状态的结构变化没有 target_delta 负责：['working_memory']"
        ),
        subject="screenplay",
        stage="screenplay",
    )
    belief_issue = structured_issue(
        code="AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED",
        message=(
            "[AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED] XP-2 "
            "入/出状态的结构变化没有 target_delta 负责：['beliefs']"
        ),
        subject="screenplay",
        stage="screenplay",
    )

    memory_ops = plan_screenplay_patch(memory_issue, script)
    belief_ops = plan_screenplay_patch(belief_issue, script)

    assert len(memory_ops) == 1
    assert memory_ops[0].target["id"] == "AS-1-OUT"
    assert memory_ops[0].value == []
    assert len(belief_ops) == 2
    assert {operation.target["id"] for operation in belief_ops} == {
        "AS-2-IN",
        "AS-2-OUT",
    }
    assert all(
        operation.value == [
            {
                "proposition_id": "P-1",
                "stance": "believed",
                "confidence": 1.0,
                "evidence_ids": [],
            },
            {
                "proposition_id": "P-2",
                "stance": "believed",
                "confidence": 1.0,
                "evidence_ids": [],
            },
        ]
        for operation in belief_ops
    )


def test_unassigned_affective_state_creates_nested_target_delta():
    from app.narrative import validate_screenplay_narrative
    from app.production.patch import apply_patch_operation_to_document
    from app.production.screenplay_repair import (
        _patch_strategy_key,
        plan_screenplay_patch,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "events": [
                {"event_id": "E-1"},
                {"event_id": "E-2"},
            ],
            "audience_priors": [{
                "audience_prior_id": "AP-1",
                "scope_id": "ep_p",
                "audience_description": "首次观看",
            }],
            "audience_states": [
                {
                    "audience_state_id": "AS-IN",
                    "audience_prior_id": "AP-1",
                    "anchor": {"type": "event", "id": "E-1"},
                    "affective_state": {},
                },
                {
                    "audience_state_id": "AS-OUT",
                    "audience_prior_id": "AP-1",
                    "anchor": {"type": "event", "id": "E-2"},
                    "affective_state": {"tension": 0.9},
                },
            ],
            "experience_intents": [{
                "experience_intent_id": "XI-1",
                "scope_id": "ep_p",
                "anchor_event_ids": ["E-2"],
                "director_objective": "提高紧张感",
                "audience_paths": [{
                    "audience_path_id": "XP-AFFECT",
                    "audience_prior_id": "AP-1",
                    "audience_state_in_id": "AS-IN",
                    "audience_state_out_target_id": "AS-OUT",
                }],
            }],
        }),
    )
    issue = structured_issue(
        code="AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED",
        message=(
            "[AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED] XP-AFFECT "
            "入/出状态的结构变化没有 target_delta 负责：['affective_state']"
        ),
        subject="screenplay",
        stage="screenplay",
    )

    operations = plan_screenplay_patch(issue, script)

    assert len(operations) == 1
    assert operations[0].op == "create_node"
    assert operations[0].target["parent_id"] == "XP-AFFECT"
    assert operations[0].target["parent_field"] == "target_deltas"
    assert operations[0].value["dimension"] == "affective"
    assert operations[0].value["from_state"] == {
        "affective_state": {},
    }
    assert operations[0].value["to_state"] == {
        "affective_state": {"tension": 0.9},
    }
    assert _patch_strategy_key(operations) == (
        "create_node:XD-XP-AFFECT-affective:node"
    )

    patched, _touched = apply_patch_operation_to_document(
        screenplay_to_document(script),
        operations[0],
    )
    projected = document_to_screenplay(patched)
    validation_errors = validate_screenplay_narrative(
        projected,
        require=True,
        expected_scope_id="ep_p",
    )

    assert not any(
        "AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED] XP-AFFECT" in error
        for error in validation_errors
    )


def test_full_regen_denied_is_a_policy_conflict_not_a_media_error():
    assert errors.classify(FullRegenDenied("denied")) == (
        "conflict",
        "FULL-REGEN-DENIED",
    )


@pytest.mark.asyncio
async def test_existing_baseline_resumes_qa_without_calling_full_generation(monkeypatch):
    from app import stages
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_authority, screenplay_repair

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
        screenplay_authority,
        "screenplay_authority_fingerprint",
        lambda *_args, **_kwargs: "authority-test",
    )
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
                    evidence={
                        "authority_input_fingerprint": "authority-test",
                    },
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
async def test_score_only_qa_does_not_plan_or_apply_patch(monkeypatch):
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

    def fake_apply(request, *, episode_id, character_resolutions=None):
        del character_resolutions
        nonlocal attempts
        attempts += 1
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            error="no-op Patch 已拒绝",
        )

    monkeypatch.setattr(screenplay_repair, "plan_screenplay_patch", fake_plan)
    monkeypatch.setattr(screenplay_repair, "apply_screenplay_patch", fake_apply)
    monkeypatch.setattr(
        screenplay_repair,
        "publish_screenplay",
        lambda **_kwargs: {"artifact_id": artifact["id"], "status": "ready"},
    )

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

    assert result is not None
    assert attempts == 0
    assert planned == 0
    completed = screenplay_repair.get_production_revision(revision.id)
    assert completed is not None
    assert completed.working_artifact_id == artifact["id"]
    assert completed.checkpoint_json["phase"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_invalid_modern_narrative_graph_enters_patch_loop(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_repair

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        resume=False,
    )
    script = _minimal_script(
        stakes="失败将失去资格",
        narrative_plan=NarrativeContinuityPlan(scope_id="ep_p"),
    )
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

    def reached_patch_loop(*_args, **_kwargs):
        raise RuntimeError("entered narrative patch loop")

    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_qa",
        reached_patch_loop,
    )

    with pytest.raises(RuntimeError, match="entered narrative patch loop"):
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


@pytest.mark.asyncio
async def test_resume_replays_persisted_identity_before_first_qa(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_repair
    from app.portraits import apply_screenplay_character_resolutions

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        resume=False,
    )
    script = _minimal_script(stakes="失败将失去资格")
    script.scene_outline[0].characters.append("青衣人")
    script.full_script_text += "\n青衣人：此路不通。"
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

    identity_replay_count = 0

    def apply_identity_once(candidate, resolutions):
        nonlocal identity_replay_count
        identity_replay_count += 1
        return apply_screenplay_character_resolutions(candidate, resolutions)

    def stop_after_identity(candidate, **_kwargs):
        assert "青衣人" not in candidate.scene_outline[0].characters
        assert "路人甲" in candidate.scene_outline[0].characters
        assert "路人甲：此路不通。" in candidate.full_script_text
        raise RuntimeError("identity replay verified")

    monkeypatch.setattr(
        "app.portraits.apply_screenplay_character_resolutions",
        apply_identity_once,
    )
    monkeypatch.setattr(screenplay_repair, "run_screenplay_qa", stop_after_identity)

    with pytest.raises(RuntimeError, match="identity replay verified"):
        await screenplay_repair.run_screenplay_production(
            episode_id="ep_p",
            episode={
                "id": "ep_p",
                "project_id": "proj_p",
                "episode_no": 1,
                "target_duration_s": 50,
                "character_resolutions": [{
                    "source_label": "青衣人",
                    "canonical_name": "路人甲",
                    "resolution": "functional_extra",
                }],
            },
            source_text="青衣人拦路。",
            bible=Bible(characters=[], world=World(visual_style_canonical="测试画风")),
            resume=True,
        )

    updated = screenplay_repair.get_production_revision(revision.id)
    assert updated is not None
    assert updated.working_artifact_id != artifact["id"]
    assert identity_replay_count == 1


@pytest.mark.asyncio
async def test_identity_replay_with_unchanged_payload_reaches_qa(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_repair
    from app.portraits import apply_screenplay_character_resolutions

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

    def report_non_material_change(candidate, _resolutions):
        before = screenplay_repair.screenplay_artifact_payload(candidate)
        changes = apply_screenplay_character_resolutions(candidate, [{
            "source_label": "许师姐",
            "canonical_name": "许清",
            "resolution": "future_identity",
        }])
        assert screenplay_repair.screenplay_artifact_payload(candidate) == before
        return changes or [{
            "source_label": "许师姐",
            "canonical_name": "许清",
            "resolution": "future_identity",
        }]

    def stop_at_qa(*_args, **_kwargs):
        raise RuntimeError("qa reached")

    monkeypatch.setattr(
        "app.portraits.apply_screenplay_character_resolutions",
        report_non_material_change,
    )
    monkeypatch.setattr(screenplay_repair, "run_screenplay_qa", stop_at_qa)

    with pytest.raises(RuntimeError, match="qa reached"):
        await screenplay_repair.run_screenplay_production(
            episode_id="ep_p",
            episode={
                "id": "ep_p",
                "project_id": "proj_p",
                "episode_no": 1,
                "target_duration_s": 50,
                "character_resolutions": [{
                    "source_label": "许师姐",
                    "canonical_name": "许清",
                    "resolution": "future_identity",
                }],
            },
            source_text="原文",
            bible=Bible(characters=[], world=World(visual_style_canonical="测试画风")),
            resume=True,
        )

    updated = screenplay_repair.get_production_revision(revision.id)
    assert updated is not None
    derived = evidence_repository.get_artifact(updated.working_artifact_id)
    assert derived is not None
    assert derived["version"] == artifact["version"] + 1


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
        "resolutions": [],
        "skipped": "baseline_identity_already_resolved",
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
    evaluation = evidence_repository.create_evaluation(
        art["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_qa",
            evaluator_version="2",
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            score=100,
        ),
    )
    cert = issue_completion_certificate(
        kind="screenplay",
        scope_id="ep_p",
        artifact_id=art["id"],
        artifact_hash=h,
        contract_version="1",
        qa_profile_version="screenplay-qa-1",
        evaluation_ids=[evaluation["id"]],
    )
    verify_completion_certificate(cert, expected_artifact_hash=h)
    with pytest.raises(ValueError):
        verify_completion_certificate(cert, expected_artifact_hash="deadbeef")


def test_repair_router_no_longer_emits_redo_or_replan():
    assert strategy_for_level("L3") == "insert_shot"
    assert strategy_for_level("L4") == "split_shot"
    assert normalize_strategy("redo_suffix") == "redo_suffix"
    assert normalize_strategy("replan_outline") == "replan_outline"

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
    assert plan.strategy == "repair_window"
    assert plan.needs_semantic_selection is True
    assert "insert_shot" in {candidate.strategy for candidate in plan.candidates}
    assert all(
        candidate.strategy not in {"redo_suffix", "replan_outline"}
        for candidate in plan.candidates
    )
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
    assert capacity.strategy == "repair_window"
    assert capacity.needs_semantic_selection is True
    assert "split_adjacent_shot" in {
        candidate.strategy for candidate in capacity.candidates
    }
    assert all(
        candidate.strategy not in {"redo_suffix", "replan_outline"}
        for candidate in capacity.candidates
    )


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


def test_source_span_normalizer_maps_hard_wrapped_import_text():
    from app.narrative import (
        normalize_source_evidence_text,
        validate_screenplay_narrative,
    )
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    chapter = "开头。\n阿宾考上私立专校，在学校旁边租了间学\n生房，只在周末回家。\n结尾。"
    excerpt = "阿宾考上私立专校，在学校旁边租了间学生房，只在周末回家。"
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-1",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": excerpt,
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    evidence = script.narrative_plan.source_evidence[0]
    raw_slice = chapter[evidence.source_span.start:evidence.source_span.end]
    assert normalize_source_evidence_text(raw_slice) == (
        normalize_source_evidence_text(excerpt)
    )
    assert any(change["kind"] == "source_span" for change in changes)
    validation_errors = validate_screenplay_narrative(
        script,
        require=True,
        expected_scope_id="ep_p",
        authorized_source_chapters={"1": chapter},
    )
    assert not any(
        "SOURCE_SPAN_EXACT_MISMATCH" in error for error in validation_errors
    )


def test_narrative_normalizer_closes_unique_effect_and_perceiver_refs():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "propositions": [{
                "proposition_id": "P1",
                "semantic_identity_key": "result-visible",
                "canonical_statement": "甲完成了动作",
                "narrative_domain": "adapted_story",
                "entity_ids": ["char-a"],
            }],
            "events": [{
                "event_id": "E1",
                "proposition_ids": ["P1"],
                "action_ids": ["A1"],
                "effects_add": ["F2"],
            }],
            "atomic_actions": [{
                "action_id": "A1",
                "actor_ids": ["char-a"],
                "semantic_intent": "完成动作",
                "effects_add": ["F2"],
                "completion_condition": "动作结果可见",
                "temporal_phases": [{
                    "phase_id": "A1/P1",
                    "start_condition": "动作开始",
                    "end_condition": "动作完成",
                    "estimated_min_s": 1.0,
                }],
            }],
            "evidence": [{
                "evidence_id": "EV1",
                "anchor": {"type": "event", "id": "E1"},
                "observable_claim": "甲完成动作",
                "perceivable_by": ["audience"],
                "supports_proposition_ids": ["P1"],
            }],
            "character_beliefs": [{
                "character_belief_id": "CB1",
                "character_id": "char-a",
                "anchor": {"type": "event", "id": "E1"},
                "perceived_evidence_ids": ["EV1"],
                "decision_proposition_ids": ["P1"],
                "decision_basis_ids": ["EV1"],
                "decision_action_ids": ["A1"],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={},
    )

    fact = next(
        item for item in script.narrative_plan.state_facts
        if item.fact_id == "F2"
    )
    evidence = script.narrative_plan.evidence[0]
    assert fact.proposition_id == "P1"
    assert fact.subject_id == "char-a"
    assert "char-a" in evidence.perceivable_by
    assert {item["kind"] for item in changes} >= {
        "missing_effect_fact",
        "evidence_perceiver",
    }


def test_narrative_normalizer_projects_unique_arc_inference_to_setup_promise():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "propositions": [
                {
                    "proposition_id": "P-SETUP",
                    "semantic_identity_key": "setup",
                    "canonical_statement": "观众先看到明确铺垫",
                    "narrative_domain": "adapted_story",
                },
                {
                    "proposition_id": "P-INFERENCE",
                    "semantic_identity_key": "inference",
                    "canonical_statement": "结尾形成推论",
                    "narrative_domain": "adapted_story",
                },
            ],
            "setup_payoff_contracts": [{
                "setup_payoff_id": "SP-1",
                "setup_proposition_ids": ["P-SETUP"],
                "intended_inference_ids": ["P-INFERENCE"],
            }],
            "arc_contracts": [{
                "arc_id": "ARC-1",
                "promise_proposition_ids": ["P-INFERENCE"],
                "payoff_contract_ids": ["SP-1"],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={},
    )

    assert script.narrative_plan.arc_contracts[0].promise_proposition_ids == [
        "P-SETUP"
    ]
    assert any(
        change["kind"] == "arc_promise_setup_projection"
        for change in changes
    )


def test_narrative_normalizer_keeps_ambiguous_arc_promise_for_repair():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "propositions": [
                {
                    "proposition_id": proposition_id,
                    "semantic_identity_key": proposition_id.casefold(),
                    "canonical_statement": proposition_id,
                    "narrative_domain": "adapted_story",
                }
                for proposition_id in ("P-SETUP-1", "P-SETUP-2", "P-INFERENCE")
            ],
            "setup_payoff_contracts": [{
                "setup_payoff_id": "SP-1",
                "setup_proposition_ids": ["P-SETUP-1", "P-SETUP-2"],
                "intended_inference_ids": ["P-INFERENCE"],
            }],
            "arc_contracts": [{
                "arc_id": "ARC-1",
                "promise_proposition_ids": ["P-INFERENCE"],
                "payoff_contract_ids": ["SP-1"],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={},
    )

    assert script.narrative_plan.arc_contracts[0].promise_proposition_ids == [
        "P-INFERENCE"
    ]
    assert not any(
        change["kind"] == "arc_promise_setup_projection"
        for change in changes
    )


def test_spine_spoken_clause_accepts_visible_action_performance():
    from app.validators import validate_screenplay_spine_delivery

    script = _minimal_script(
        plot_spine=PlotSpine(
            episode_premise="胡太太邀请阿宾帮忙整理家",
            spine_beats=[PlotSpineBeat(
                beat_id="S05",
                who="胡太太",
                does="邀请阿宾帮忙整理家，许诺晚上请他吃饭，阿宾答应",
                turn="两人开始共同整理",
                must_keep=True,
            )],
            must_keep_ending="两人开始共同整理客厅",
            drop_list=["无关闲聊", "重复环境描写"],
        ),
    )
    action_text = (
        "胡太太邀请阿宾帮忙整理家，许诺晚上请他吃饭，"
        "阿宾点头答应，两人开始搬动家具。"
    )

    assert validate_screenplay_spine_delivery(
        script,
        action_text=action_text,
    ) == []


def test_source_span_normalizer_expands_one_uniquely_proven_elision():
    from app.narrative import (
        normalize_source_evidence_text,
        validate_screenplay_narrative,
    )
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    first = "胡太太请阿宾帮忙拿包裹，两人一起走到六楼客厅。"
    omitted = "阿宾上楼时看见窗外景色，又在门口停留了一会儿。"
    second = "胡太太询问阿宾下午是否有空，请他一起整理家具。"
    chapter = f"前文。{first}{omitted}{second}后文。"
    excerpt = first + second
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-1",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": excerpt,
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    evidence = script.narrative_plan.source_evidence[0]
    raw_slice = chapter[evidence.source_span.start:evidence.source_span.end]
    assert normalize_source_evidence_text(raw_slice) == (
        normalize_source_evidence_text(evidence.verbatim_excerpt)
    )
    assert omitted in evidence.verbatim_excerpt
    assert any(
        change["kind"] == "source_excerpt_expanded" for change in changes
    )
    validation_errors = validate_screenplay_narrative(
        script,
        require=True,
        expected_scope_id="ep_p",
        authorized_source_chapters={"1": chapter},
    )
    assert not any(
        "SOURCE_SPAN_EXACT_MISMATCH" in error for error in validation_errors
    )


def test_source_span_normalizer_expands_long_unique_elision():
    from app.narrative import normalize_source_evidence_text
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    first = "调查员进入仓库并确认门锁完好，随后开始逐项核对货架。"
    omitted = "中间保存完整授权正文。" * 400
    second = "调查员在最深处找到遗失的蓝色档案盒并完成登记。"
    chapter = first + omitted + second
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-LONG",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": first + "……" + second,
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    evidence = script.narrative_plan.source_evidence[0]
    raw_slice = chapter[evidence.source_span.start:evidence.source_span.end]
    assert normalize_source_evidence_text(raw_slice) == (
        normalize_source_evidence_text(evidence.verbatim_excerpt)
    )
    assert omitted in evidence.verbatim_excerpt
    assert any(
        change["kind"] == "source_excerpt_expanded" for change in changes
    )


def test_source_span_normalizer_uses_linked_proposition_for_duplicate_excerpt():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    excerpt = "咚咚"
    first = "旧仓库的木钟发出咚咚声，值班员随手关上了门。"
    second = "调查员追到地下室，听见咚咚声后找到了被困的同伴。"
    chapter = first + "无关过渡。" * 80 + second
    expected_start = chapter.rindex(excerpt)
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-DUP",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": excerpt,
            }],
            "propositions": [{
                "proposition_id": "P-RESCUE",
                "canonical_statement": "调查员追到地下室并找到被困的同伴",
                "narrative_domain": "source_canon",
                "direct_source_evidence_ids": ["SE-DUP"],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    evidence = script.narrative_plan.source_evidence[0]
    assert evidence.source_span.start == expected_start
    assert evidence.source_span.end == expected_start + len(excerpt)
    assert any(change["kind"] == "source_span" for change in changes)


def test_gate_failure_summary_prioritizes_actual_failed_issue():
    from app.production.screenplay_repair import _gate_failure_message

    backlog = structured_issue(
        code="KEY_LINE_MISSING",
        message="对白链跨场",
        subject="screenplay",
        path="/dialogue_chains",
        stage="screenplay",
    )
    failed = structured_issue(
        code="AUDIENCE_BELIEF_DIFF_UNASSIGNED",
        message="观众信念变化缺少命题绑定",
        subject="screenplay",
        path="/audience_paths/XP-1",
        stage="screenplay",
    )

    message = _gate_failure_message([backlog, failed], failed_issue=failed)

    assert "自动修复停止于 AUDIENCE_BELIEF_DIFF_UNASSIGNED" in message
    assert message.index(failed.message) < message.index(backlog.message)


def test_issue_selection_preserves_validator_order_when_metadata_ties():
    from app.production.screenplay_repair import _choose_issue

    first = structured_issue(
        code="Z_FIRST_BY_VALIDATOR",
        message="上游问题",
        subject="screenplay",
        stage="screenplay",
    )
    second = structured_issue(
        code="A_SECOND_BY_VALIDATOR",
        message="下游问题",
        subject="screenplay",
        stage="screenplay",
    )

    assert _choose_issue([first, second]) is first


def test_narrative_patch_retargets_ancestor_to_named_direct_field_owner():
    from app.production.screenplay_repair import _resolve_narrative_patch_owner

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "experience_intents": [{
            "experience_intent_id": "XI-1",
            "scope_id": "ep_p",
            "director_objective": "测试观众路径",
            "audience_paths": [
                {
                    "audience_path_id": "XP-AP1-1",
                    "audience_prior_id": "AP-1",
                    "audience_state_in_id": "AS-1-IN",
                    "audience_state_out_target_id": "AS-1-OUT",
                    "target_deltas": [],
                },
                {
                    "audience_path_id": "XP-AP2-1",
                    "audience_prior_id": "AP-2",
                    "audience_state_in_id": "AS-2-IN",
                    "audience_state_out_target_id": "AS-2-OUT",
                    "target_deltas": [],
                },
            ],
        }],
    }).model_dump(mode="json")
    issue = structured_issue(
        code="AUDIENCE_BELIEF_DIFF_UNASSIGNED",
        message="XP-AP2-1 信念变化没有绑定相应命题",
        subject="screenplay",
        path="/audience_belief_diff_unassigned",
        stage="screenplay",
    )

    resolved = _resolve_narrative_patch_owner(
        plan["experience_intents"],
        node_id="XI-1",
        patch_field="target_deltas",
        issue=issue,
    )

    assert resolved is not None
    owner, owner_id = resolved
    assert owner_id == "XP-AP2-1"
    assert owner["audience_path_id"] == "XP-AP2-1"


def test_narrative_node_alias_resolves_unique_schema_collection():
    from app.production.screenplay_repair import (
        _narrative_collection_for_node,
    )

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "events": [
            {"event_id": "E3", "effects_add": ["F-5"]},
            {"event_id": "E4", "effects_add": ["F-5"]},
        ],
    }).model_dump(mode="json")

    assert _narrative_collection_for_node(plan, "E3") == "events"
    assert _narrative_collection_for_node(plan, "missing") is None


def test_new_narrative_node_infers_collection_from_stable_identity_field():
    from app.production.screenplay_repair import (
        _narrative_collection_for_new_node,
    )

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "character_beliefs": [{
            "character_belief_id": "CB-1",
            "character_id": "甲",
            "anchor": {"type": "event", "id": "E1"},
        }],
    }).model_dump(mode="json")
    new_belief = {
        "character_belief_id": "CB-2",
        "character_id": "乙",
        "anchor": {"type": "event", "id": "E2"},
    }

    assert _narrative_collection_for_new_node(
        plan,
        "CB-2",
        new_belief,
    ) == "character_beliefs"


def test_new_belief_candidate_routes_to_narrative_preflight():
    from app.production.screenplay_repair import (
        _candidate_targets_narrative_graph,
    )

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "character_beliefs": [{
            "character_belief_id": "CB-1",
            "character_id": "甲",
            "anchor": {"type": "event", "id": "E1"},
        }],
    }).model_dump(mode="json")
    candidate = {
        "operations": [{
            "op": "create_node",
            "target": {
                "kind": "character_belief",
                "id": "CB-2",
                "parent_id": "narrative_plan",
                "parent_field": "character_beliefs",
            },
            "value": {
                "character_belief_id": "CB-2",
                "character_id": "乙",
                "anchor": {"type": "event", "id": "E2"},
            },
        }],
    }

    assert _candidate_targets_narrative_graph(candidate, plan) is True


def test_top_level_narrative_parent_is_removed_with_explicit_collection():
    from app.production.screenplay_repair import (
        _normalize_top_level_narrative_parent,
    )

    target = _normalize_top_level_narrative_parent(
        {
            "kind": "character_belief",
            "collection": "character_beliefs",
            "id": "CB-2",
            "parent_id": "narrative_plan",
            "parent_field": "character_beliefs",
        },
        collection="character_beliefs",
        plan_data={"scope_id": "ep_p"},
    )

    assert "parent_id" not in target
    assert "parent_field" not in target
    assert target["collection"] == "character_beliefs"


def test_event_fact_patch_expands_to_its_single_atomic_action():
    from app.production.patch import PatchOperation
    from app.production.screenplay_repair import (
        _expand_single_action_event_closure,
    )

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "events": [{
            "event_id": "E4",
            "action_ids": ["A2"],
            "effects_add": ["F-5"],
        }],
        "atomic_actions": [{
            "action_id": "A2",
            "actor_ids": ["甲"],
            "semantic_intent": "完成动作",
            "effects_add": ["F-5"],
            "completion_condition": "动作完成",
            "temporal_phases": [{
                "phase_id": "A2/P1",
                "start_condition": "开始",
                "end_condition": "完成",
                "estimated_min_s": 1.0,
            }],
        }],
    }).model_dump(mode="json")
    operations = [PatchOperation(
        op="replace_field",
        path="effects_add",
        value=[],
        target={"kind": "event", "id": "E4"},
    )]

    expanded = _expand_single_action_event_closure(operations, plan)

    assert len(expanded) == 2
    derived = expanded[1]
    assert derived.path == "effects_add"
    assert derived.value == []
    assert derived.target["collection"] == "atomic_actions"
    assert derived.target["id"] == "A2"


def test_dialogue_turn_target_derives_chain_and_index_from_stable_turn_id():
    from app.production.screenplay_document import screenplay_to_document
    from app.production.screenplay_repair import (
        _resolve_dialogue_chain_turn_target,
    )

    script = _minimal_script(dialogue_chains=[KeyDialogueChain(
        chain_id="DC3",
        topic="整理家",
        turns=[
            KeyDialogueTurn(
                speaker="甲",
                line="开始整理。",
                function="statement",
                source_text="开始整理。",
            ),
            KeyDialogueTurn(
                speaker="乙",
                line="好的。",
                function="response",
                source_text="好的。",
            ),
        ],
    )])

    target = _resolve_dialogue_chain_turn_target(
        screenplay_to_document(script),
        target={"kind": "dialogue_chain_turn", "id": "DC3-T2"},
        patch_field="line",
    )

    assert target is not None
    assert target["chain_id"] == "DC3"
    assert target["turn_index"] == 1


def test_candidate_executability_uses_production_patch_path():
    from app.production.screenplay_repair import (
        _candidate_is_executable,
    )

    document = screenplay_to_document(_minimal_script())
    destructive = {
        "operations": [{
            "op": "replace_field",
            "path": "dialogue_turns",
            "target": {"kind": "scene", "id": "SC01"},
            "value": [],
        }],
    }
    bounded = {
        "operations": [{
            "op": "replace",
            "path": "text",
            "target": {"kind": "future_action_type", "id": "AC01-01"},
            "value": "甲在场地中央站定，准备应战。",
        }],
    }

    assert _candidate_is_executable(destructive, document) is False
    assert _candidate_is_executable(bounded, document) is True


def test_document_candidate_preflight_selects_passing_minimal_subset():
    from app.production.screenplay_repair import _preflight_document_candidate
    from app.validators import validate_screenplay

    source = (
        "“救命。” “你是谁？” “还有其他人。” "
        "“你们怎么来的？” “飞！” “别听他胡说，因为我们是被抓来的。” "
        "“我不信。”"
    )
    script = _minimal_script(
        full_script_text=(
            "【场1】夜 / 场地\n"
            "甲：救命。\n"
            "乙：你是谁？\n"
            "甲：还有其他人。\n"
            "乙：你们怎么来的？\n"
            "丙（大声），飞！\n"
            "甲：别听他胡说，因为我们是被抓来的。\n"
            "乙：我不信。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="说明来历",
                turns=[
                    KeyDialogueTurn(
                        speaker="甲",
                        line="救命。",
                        function="trigger",
                        source_text="救命。",
                    ),
                    KeyDialogueTurn(
                        speaker="乙",
                        line="你是谁？",
                        function="question",
                        source_text="你是谁？",
                    ),
                    KeyDialogueTurn(
                        speaker="甲",
                        line="别听他胡说，因为我们是被抓来的。",
                        function="response",
                        source_text="别听他胡说，因为我们是被抓来的。",
                    ),
                    KeyDialogueTurn(
                        speaker="乙",
                        line="我不信。",
                        function="response",
                        source_text="我不信。",
                    ),
                ],
            ),
        ],
    )
    script = document_to_screenplay(screenplay_to_document(script))
    errors = validate_screenplay(
        script,
        Bible(characters=[], world=World(visual_style_canonical="测试画风")),
        expected_beats=1,
        episode_no=1,
        source_text=source,
        require_dialogue_chains=True,
    )
    message = next(error for error in errors if "主线对白上下文断裂" in error)
    issue = structured_issue(
        code="KEY_LINE_MISSING",
        message=message,
        subject="screenplay",
        path="/dialogue_chains",
        rule_id="key_line_context",
        stage="screenplay",
    )
    candidate = {
        "candidate_id": "CAND-PASSING-BACKUP",
        "expected_narrative_gain": 1.0,
        "destructive_cost": 0.1,
        "operations": [
            {
                "op": "replace",
                "path": "turns",
                "target": {"kind": "future_dialogue_type", "id": "DC1"},
                "value": [
                    *[
                        turn.model_dump(mode="json")
                        for turn in script.dialogue_chains[0].turns[:2]
                    ],
                    {
                        "speaker": "乙",
                        "line": "你们怎么来的？",
                        "function": "question",
                        "source_text": "你们怎么来的？",
                    },
                    {
                        "speaker": "丙",
                        "line": "飞！",
                        "function": "statement",
                        "source_text": "飞！",
                    },
                    *[
                        turn.model_dump(mode="json")
                        for turn in script.dialogue_chains[0].turns[2:]
                    ],
                ],
            },
            {
                "op": "replace",
                "path": "unknown_field",
                "target": {"kind": "future_node", "id": "UNKNOWN"},
                "value": "ignored",
            },
        ],
    }

    operations = _preflight_document_candidate(
        candidate,
        document=screenplay_to_document(script),
        source_text=source,
        issue=issue,
    )

    assert len(operations) == 1
    assert operations[0].path == "turns"
    assert operations[0].target["id"] == "DC1"


@pytest.mark.asyncio
async def test_semantic_patch_retries_rejected_and_duplicate_candidates(monkeypatch):
    from app.production import screenplay_repair
    from app.production.patch import PatchOperation

    issue = structured_issue(
        code="AUDIENCE_BELIEF_DIFF_UNASSIGNED",
        message="XP-1 缺少命题绑定",
        subject="screenplay",
        path="/audience_paths/XP-1",
        stage="screenplay",
    )
    rejected = PatchOperation(
        op="replace_field",
        path="stakes",
        value="失败代价",
        target={"kind": "metadata", "id": "stakes"},
    )
    fresh = PatchOperation(
        op="replace_field",
        path="obstacle",
        value="新增阻力",
        target={"kind": "metadata", "id": "obstacle"},
    )
    attempts: list[tuple[int, list[str]]] = []

    async def fake_once(*_args, planner_attempt=1, rejection_feedback=None, **_kwargs):
        attempts.append((planner_attempt, list(rejection_feedback or [])))
        if planner_attempt == 1:
            return []
        if planner_attempt == 2:
            return [rejected]
        return [fresh]

    monkeypatch.setattr(screenplay_repair, "_llm_field_patch_once", fake_once)

    operations = await screenplay_repair._llm_field_patch(
        issue,
        _minimal_script(),
        source_text="原文",
        strategy_history=["fill_stakes"],
    )

    assert operations == [fresh]
    assert [attempt for attempt, _feedback in attempts] == [1, 2, 3]
    assert not attempts[0][1]
    assert "未通过本地结构" in attempts[1][1][0]
    assert "fill_stakes" in attempts[2][1][-1]


@pytest.mark.asyncio
async def test_semantic_patch_repairs_unescaped_inner_quotes(monkeypatch):
    from app import schemas
    from app.harness import model_gateway
    from app.production import screenplay_repair

    issue = structured_issue(
        code="MUST_KEEP_SPINE_BEAT_NOT_PERFORMED",
        message='节拍"许清判定资质"未完整演出',
        subject="screenplay",
        path="/scene_blocks/SC03",
        stage="screenplay",
    )
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan(scope_id="ep_test"),
    )
    options: dict = {}

    async def fake_chat(*_args, **_kwargs):
        return '{"candidate_plans":[]}'

    def fake_extract(text, **kwargs):
        options.update(kwargs)
        return {"candidate_plans": []}

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(schemas, "extract_json", fake_extract)

    result = await screenplay_repair._llm_field_patch_once(
        issue,
        script,
        source_text="许清说道：有些资质。",
    )

    assert result == []
    assert options["repair_unescaped_inner_quotes"] is True


def test_screenplay_narrative_gate_is_quality_error():
    from app.production.screenplay_repair import ScreenplayNarrativeGateError

    assert errors.classify(ScreenplayNarrativeGateError("门禁未通过")) == (
        "quality_gate",
        "QA",
    )


@pytest.mark.asyncio
async def test_recorded_narrative_gate_preserves_repair_state_and_partial_run(
    monkeypatch,
):
    from app.domain import screenplay_ops
    from app.production import screenplay_repair

    message = "剧本工作稿已保留，但叙事/质量硬门禁仍未通过，禁止发布"

    async def fake_discovery(*_args, **_kwargs):
        return {"added": [], "resolutions": [], "warnings": []}

    async def fake_production(*_args, **_kwargs):
        conn = db.get_conn()
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing', screenplay_error=? "
            "WHERE id='ep_p'",
            (message,),
        )
        conn.commit()
        raise screenplay_repair.ScreenplayNarrativeGateError(message)

    monkeypatch.setattr(
        screenplay_ops,
        "_screenplay_character_discovery",
        fake_discovery,
    )
    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_production",
        fake_production,
    )
    recorder = screenplay_ops._new_screenplay_recorder("ep_p")

    result = await screenplay_ops._recorded_screenplay_task("ep_p", recorder)

    assert result is None
    episode = db.get_conn().execute(
        "SELECT screenplay_status, screenplay_error FROM episodes WHERE id='ep_p'"
    ).fetchone()
    run = db.get_conn().execute(
        "SELECT status, failure_code, failure_message FROM workflow_runs WHERE id=?",
        (recorder.run_id,),
    ).fetchone()
    step = db.get_conn().execute(
        "SELECT status, error_code FROM step_runs WHERE run_id=? AND step_key='screenplay'",
        (recorder.run_id,),
    ).fetchone()
    error_log = db.get_conn().execute(
        "SELECT code, category FROM error_logs WHERE action='screenplay_repair' "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()

    assert episode["screenplay_status"] == "repairing"
    assert episode["screenplay_error"] == message
    assert run["status"] == "PARTIAL"
    assert run["failure_code"] == "PARTIAL_RESULT"
    assert run["failure_message"] == message
    assert step["status"] == "FAILED"
    assert step["error_code"] == "SCREENPLAYNARRATIVEGATEERROR"
    assert error_log["code"] == "QA"
    assert error_log["category"] == "quality_gate"
