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
    assert evaluation.status == "failed"
    assert evaluation.evaluation_role == "score_only"
    assert evaluation.runtime_blocking is False


def test_unresolved_character_identity_is_a_non_waivable_runtime_gate() -> None:
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
    assert evaluation.evaluation_role == "business_safety"
    assert evaluation.runtime_blocking is True


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

    with pytest.raises(RuntimeError, match="人物身份预检未通过"):
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
    paused = screenplay_repair.get_production_revision(revision.id)
    assert paused is not None
    assert paused.checkpoint_json["phase"] == "SUCCEEDED"
    assert paused.checkpoint_json["yield_reason"] == "gate_retry_exhausted_fallback"


@pytest.mark.asyncio
async def test_resume_replays_persisted_identity_before_first_qa(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_repair

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

    def stop_after_identity(candidate, **_kwargs):
        assert "青衣人" not in candidate.scene_outline[0].characters
        assert "路人甲" in candidate.scene_outline[0].characters
        assert "路人甲：此路不通。" in candidate.full_script_text
        raise RuntimeError("identity replay verified")

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
    assert normalize_strategy("redo_suffix") == "repair_window"
    assert normalize_strategy("replan_outline") == "repair_window"

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
