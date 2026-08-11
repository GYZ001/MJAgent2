from __future__ import annotations

import json
from pathlib import Path

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
from app.schemas import (
    ActionAgency,
    AtomicAction,
    Bible,
    EpisodeScreenplay,
    IdentityContractEvidence,
    KeyDialogueChain,
    KeyDialogueTurn,
    NarrativeContinuityPlan,
    NarrativeEvent,
    NarrativeIdentityContract,
    NarrativeProposition,
    ScriptScene,
    SourceCoverageDecision,
    SourceEvidence,
    SourceSpan,
    TextProvenance,
    VoiceCanonical,
    World,
)
from app.screenplay_ir import IR_VERSION
from app.screenplay_scene_shards import (
    SCREENPLAY_MERGED_IR_VERSION,
    SCREENPLAY_SCENE_SHARD_VERSION,
)


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
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="公开测验结果并改变萧炎处境",
                turns=[
                    KeyDialogueTurn(
                        speaker="测验员",
                        line="斗之力，三段！",
                        function="announcement",
                        source_text="斗之力，三段！",
                    ),
                ],
            ),
        ],
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


def _run_3f05c2a0fedd_environment_script() -> EpisodeScreenplay:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/run_3f05c2a0fedd_narrator_scene_regression.json"
        ).read_text(encoding="utf-8"),
    )
    scene = fixture["baseline_artifact"]["scene_block"]
    action_text = "\n".join(
        item["text"] for item in scene["action_blocks"]
    )
    return EpisodeScreenplay(
        episode_no=1,
        id="ep_711b29204aa9",
        title="我欲封天第一集",
        logline="孟浩从凡俗世界意外踏入修仙世界",
        dramatic_question="孟浩如何踏上修仙之路？",
        protagonist_goal="孟浩想解决生计困境",
        obstacle="凡俗困顿与意外劫掠",
        stakes="孟浩的生计与自由都将改变",
        scene_outline=[ScriptScene.model_validate({
            key: value
            for key, value in scene.items()
            if key not in {"scene_id", "action_blocks", "dialogue_turns"}
        })],
        full_script_text=f'{scene["scene_heading"]}\n{action_text}',
        emotional_curve="平静铺陈后转入命运变化",
        ending_hook="孟浩抵达靠山宗",
        source_basis="SRC0001,SRC0002",
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_711b29204aa9",
            "events": [{
                "event_id": "E2",
                "onscreen_entity_ids": [],
            }],
            "scene_contracts": [{
                "scene_id": "SC01",
                "point_of_view_character_id": "environment:ep_711b29204aa9",
                "turn_event_ids": ["E2"],
            }],
        }),
    )


def _create_working_artifact(
    evidence_repository,
    screenplay_repair,
    script: EpisodeScreenplay,
) -> dict:
    script.source_coverage = [
        SourceCoverageDecision(
            source_segment_id="SRC0001",
            disposition="deliver",
            projection_policy="picture",
            beat_ids=["SC01"],
        ),
    ]
    script.narrative_plan = NarrativeContinuityPlan(
        scope_id="ep_scene",
        source_evidence=[
            SourceEvidence(
                source_evidence_id="SE001",
                source_span=SourceSpan(chapter_id="1", start=0, end=2),
                verbatim_excerpt="原文",
            ),
        ],
        propositions=[
            NarrativeProposition(
                proposition_id="P001",
                semantic_identity_key="public-test-result",
                canonical_statement="测验员公开萧炎的三段结果。",
                narrative_domain="source_canon",
                entity_ids=["identity-examiner", "identity-xiao-yan"],
                direct_source_evidence_ids=["SE001"],
            ),
        ],
        events=[
            NarrativeEvent(
                event_id="EV001",
                proposition_ids=["P001"],
                action_ids=["ACT001"],
                onscreen_entity_ids=["identity-examiner", "identity-xiao-yan"],
                effects_add=["萧炎的三段结果被公开"],
                narrative_layer="story",
                event_priority="causal",
                render_policy="standalone",
                delivery_scope_id="ep_scene",
            ),
        ],
        atomic_actions=[
            AtomicAction(
                action_id="ACT001",
                actor_ids=["identity-examiner"],
                target_ids=["identity-xiao-yan"],
                action_agency=ActionAgency(
                    kind="character_dialogue",
                    identity_bearing=True,
                    source_segment_ids=["SRC0001"],
                ),
                text_provenance=TextProvenance(
                    kind="dialogue",
                    identity_keys=["identity-examiner", "identity-xiao-yan"],
                    source_segment_ids=["SRC0001"],
                ),
                dialogue_text="斗之力，三段！",
                participant_deliveries=[],
                semantic_intent="公开测验结果并改变萧炎的处境。",
                effects_add=["萧炎的三段结果被公开"],
                completion_condition="围观者听见结果且萧炎退回队尾。",
            ),
        ],
        identity_contracts=[
            NarrativeIdentityContract(
                identity_id="identity-xiao-yan",
                display_name="萧炎",
                kind="persistent dramatic person",
                visual_policy="canonical",
                visual_canonical="黑发少年，深色练功服，神情克制",
                asset_requirement="required",
                evidence=IdentityContractEvidence(
                    source_evidence_ids=["SE001"],
                    proposition_ids=["P001"],
                    rationale="萧炎是本场持续可见并承受结果的主体。",
                ),
            ),
            NarrativeIdentityContract(
                identity_id="identity-examiner",
                display_name="测验员",
                kind="scene-bound embodied speaker",
                visual_policy="contextual",
                visual_canonical="站在测验石旁宣读结果的成年测验员",
                asset_requirement="optional",
                voice_ids=["测验员"],
                evidence=IdentityContractEvidence(
                    source_evidence_ids=["SE001"],
                    proposition_ids=["P001"],
                    rationale="测验员在来源中承担可听见的结果宣告。",
                ),
            ),
        ],
    )

    parent_specs = [
        (
            "screenplay_narrative_blueprint",
            "screenplay-narrative-blueprint.v4",
            {"scope_id": "ep_scene", "event_ids": ["EV001"]},
        ),
        (
            "screenplay_identity_registry",
            "screenplay-identity-registry.v1",
            {
                "identity_ids": [
                    "identity-xiao-yan",
                    "identity-examiner",
                ],
            },
        ),
        (
            "screenplay_envelope",
            "screenplay-envelope.v1",
            {"scope_id": "ep_scene", "source_segment_ids": ["SRC0001"]},
        ),
        (
            "screenplay_scene_shard",
            SCREENPLAY_SCENE_SHARD_VERSION,
            {
                "contract_version": SCREENPLAY_SCENE_SHARD_VERSION,
                "shard_id": "SS001",
                "scene_plan_keys": ["SC01"],
                "scenes": [],
                "consumed_source_ids": ["SRC0001"],
                "unresolved_participants": [],
            },
        ),
    ]
    parents = [
        evidence_repository.create_artifact(EvidenceArtifact(
            type=artifact_type,
            scope_type="episode",
            scope_id="ep_scene",
            status="validated",
            trust_level="T1",
            content=content,
            contract_version=contract_version,
        ))
        for artifact_type, contract_version, content in parent_specs
    ]
    merged = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_generation_ir_merged",
        scope_type="episode",
        scope_id="ep_scene",
        status="validated",
        trust_level="T1",
        content={
            "format_version": IR_VERSION,
            "source_scene_owners": {"SRC0001": "SC01"},
            "source_audit_annotations": [],
            "source_semantics": {
                "SRC0001": {
                    "narrative_layer": "story",
                    "event_priority": "causal",
                    "render_policy": "standalone",
                    "disposition": "deliver",
                    "projection_policy": "picture",
                },
            },
            "scenes": [{
                "key": "SC01",
                "units": [{
                    "key": "UNIT001",
                    "source_segment_ids": ["SRC0001"],
                    "narrative_layer": "story",
                    "event_priority": "causal",
                    "render_policy": "standalone",
                }],
            }],
            "events": [{
                "key": "EV001",
                "source_segment_ids": ["SRC0001"],
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
            }],
            "coverage": [{
                "source_segment_ids": ["SRC0001"],
                "disposition": "deliver",
                "projection_policy": "picture",
            }],
        },
        parent_artifact_ids=[parent["id"] for parent in parents],
        contract_version=SCREENPLAY_MERGED_IR_VERSION,
    ))
    return evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_scene",
        status="candidate",
        trust_level="T1",
        content=screenplay_repair.screenplay_artifact_payload(script),
        parent_artifact_ids=[merged["id"]],
        contract_version="4.0.0",
    ))


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


def _scene_character_errors(script: EpisodeScreenplay) -> list[str]:
    from app.validators import validate_screenplay

    return [
        error
        for error in validate_screenplay(
            script,
            Bible(characters=[], world=World(visual_style_canonical="国风")),
            expected_beats=max(1, len(script.scene_outline)),
            episode_no=1,
            source_text=script.full_script_text,
            require_dialogue_chains=False,
            validate_narrative=False,
        )
        if ".characters " in error
    ]


def test_run_3f05c2a0fedd_environment_scene_keeps_empty_characters() -> None:
    script = _run_3f05c2a0fedd_environment_script()

    assert script.scene_outline[0].characters == []
    assert _scene_character_errors(script) == []


def test_narrative_scene_with_typed_visible_participant_requires_characters() -> None:
    script = _run_3f05c2a0fedd_environment_script()
    script.narrative_plan = NarrativeContinuityPlan.model_validate({
        "scope_id": script.id,
        "identity_contracts": [{
            "identity_id": "person-menghao",
            "display_name": "孟浩",
            "kind": "named_character",
            "visual_policy": "canonical",
            "visual_canonical": "青色文士长衫的清瘦书生",
            "asset_requirement": "required",
            "voice_ids": ["孟浩"],
        }],
        "events": [{
            "event_id": "E2",
            "onscreen_entity_ids": ["person-menghao"],
        }],
        "scene_contracts": [{
            "scene_id": "SC01",
            "point_of_view_character_id": "person-menghao",
            "turn_event_ids": ["E2"],
        }],
    })

    errors = _scene_character_errors(script)

    assert len(errors) == 1
    assert ".characters 不能为空" in errors[0]


def test_structured_dialogue_speaker_requires_character_without_full_text_line() -> None:
    script = _run_3f05c2a0fedd_environment_script()
    script.narrative_plan = NarrativeContinuityPlan.model_validate({
        "scope_id": script.id,
        "identity_contracts": [{
            "identity_id": "person-menghao",
            "display_name": "孟浩",
            "kind": "named_character",
            "visual_policy": "canonical",
            "visual_canonical": "青色文士长衫的清瘦书生",
            "asset_requirement": "required",
            "voice_ids": ["孟浩"],
        }],
        "scene_contracts": [{"scene_id": "SC01"}],
    })
    script.voice_bible = [VoiceCanonical(
        speaker_id="孟浩",
        voice_canonical="清瘦书生的稳定声线",
        role_type="named_character",
    )]
    script.dialogue_chains = [KeyDialogueChain(
        chain_id="DC1",
        scene_id="SC01",
        topic="孟浩再次落榜",
        turns=[KeyDialogueTurn(
            speaker="孟浩",
            line="又落榜了……",
            source_text="又落榜了……",
        )],
    )]

    errors = _scene_character_errors(script)

    assert len(errors) == 1
    assert ".characters 不能为空" in errors[0]


def test_atomic_action_actor_requires_character_without_state_or_pov() -> None:
    script = _run_3f05c2a0fedd_environment_script()
    script.narrative_plan = NarrativeContinuityPlan.model_validate({
        "scope_id": script.id,
        "identity_contracts": [{
            "identity_id": "person-menghao",
            "display_name": "孟浩",
            "kind": "named_character",
            "visual_policy": "canonical",
            "visual_canonical": "青色文士长衫的清瘦书生",
            "asset_requirement": "required",
        }],
        "atomic_actions": [{
            "action_id": "A-1",
            "actor_ids": ["person-menghao"],
            "semantic_intent": "孟浩抬头看向远山",
            "completion_condition": "孟浩的视线停在远山",
        }],
        "events": [{
            "event_id": "E2",
            "action_ids": ["A-1"],
        }],
        "scene_contracts": [{
            "scene_id": "SC01",
            "turn_event_ids": ["E2"],
        }],
    })

    errors = _scene_character_errors(script)

    assert len(errors) == 1
    assert ".characters 不能为空" in errors[0]


def test_required_visible_identity_cannot_be_replaced_by_another_known_person() -> None:
    script = _run_3f05c2a0fedd_environment_script()
    script.scene_outline[0].characters = ["王有材"]
    script.narrative_plan = NarrativeContinuityPlan.model_validate({
        "scope_id": script.id,
        "identity_contracts": [
            {
                "identity_id": "person-menghao",
                "display_name": "孟浩",
                "kind": "named_character",
                "visual_policy": "canonical",
                "visual_canonical": "青色文士长衫的清瘦书生",
                "asset_requirement": "required",
            },
            {
                "identity_id": "person-wangyoucai",
                "display_name": "王有材",
                "kind": "named_character",
                "visual_policy": "canonical",
                "visual_canonical": "布衣少年",
                "asset_requirement": "required",
            },
        ],
        "events": [{
            "event_id": "E2",
            "onscreen_entity_ids": ["person-menghao"],
        }],
        "scene_contracts": [{
            "scene_id": "SC01",
            "turn_event_ids": ["E2"],
        }],
    })

    errors = _scene_character_errors(script)

    assert any("缺少结构化权威" in error and "孟浩" in error for error in errors)


def _two_scene_dialogue_binding_script(*, scene_id: str) -> EpisodeScreenplay:
    script = _script(story_function="建立山顶的生计困境")
    script.scene_outline = [
        script.scene_outline[0].model_copy(update={
            "scene_heading": "【场1】日 / 山顶",
            "summary": "孟浩独自坐在山顶，远处群山在夕阳中延伸。",
            "characters": ["孟浩"],
        }),
        ScriptScene(
            scene_no=2,
            scene_heading="【场2】日 / 山洞",
            story_function="交付山洞里的求救声",
            characters=["王有材"],
            summary="王有材被困在山洞深处，向外发出急促求救声。",
            turn="孟浩听见求救声",
            source_basis="原文山洞求救段落",
        ),
    ]
    # Deliberately put the line under SC01: structured scene_id must win.
    script.full_script_text = (
        "【场1】日 / 山顶\n王有材：救命啊！\n"
        "【场2】日 / 山洞\n山洞深处传来回声。"
    )
    script.dialogue_chains = [KeyDialogueChain(
        chain_id="DC1",
        scene_id=scene_id,
        topic="山洞里的求救声",
        turns=[KeyDialogueTurn(
            speaker="王有材",
            line="救命啊！",
            source_text="救命啊！",
        )],
    )]
    return script


def _add_two_scene_dialogue_authority(script: EpisodeScreenplay) -> None:
    script.narrative_plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_scene",
        "identity_contracts": [
            {
                "identity_id": "person-menghao",
                "display_name": "孟浩",
                "kind": "named_character",
                "visual_policy": "canonical",
                "visual_canonical": "青色文士长衫的清瘦书生",
                "asset_requirement": "required",
            },
            {
                "identity_id": "person-wangyoucai",
                "display_name": "王有材",
                "kind": "named_character",
                "visual_policy": "canonical",
                "visual_canonical": "布衣少年",
                "asset_requirement": "required",
                "voice_ids": ["王有材"],
            },
        ],
        "scene_contracts": [
            {"scene_id": "SC01"},
            {"scene_id": "SC02"},
        ],
    })
    script.voice_bible = [VoiceCanonical(
        speaker_id="王有材",
        voice_canonical="急促紧张的少年声线",
        role_type="named_character",
    )]


def test_dialogue_scene_id_wins_when_prose_semantics_conflict() -> None:
    script = _two_scene_dialogue_binding_script(scene_id="SC02")
    _add_two_scene_dialogue_authority(script)
    document = screenplay_to_document(script)

    result = document_to_screenplay(document)

    assert "王有材：救命啊！" not in result.full_script_text.split("【场2】")[0]
    assert "【场2】日 / 山洞\n山洞深处传来回声。\n王有材：救命啊！" in result.full_script_text
    # The stale prose occurrence under SC01 is not a second authority claim.
    assert _scene_character_errors(script) == []


def test_invalid_dialogue_scene_id_fails_typed_instead_of_guessing() -> None:
    from app.production.screenplay_document import DialogueSceneBindingError
    from app.validators import validate_screenplay

    script = _two_scene_dialogue_binding_script(scene_id="SC99")
    _add_two_scene_dialogue_authority(script)
    document = screenplay_to_document(script)

    with pytest.raises(DialogueSceneBindingError, match="SC99"):
        document_to_screenplay(document)
    errors = validate_screenplay(
        script,
        Bible(characters=[], world=World(visual_style_canonical="国风")),
        expected_beats=2,
        episode_no=1,
        require_dialogue_chains=False,
        validate_narrative=False,
    )
    assert any(
        error.startswith("[DIALOGUE_SCENE_BINDING_INVALID]") and "SC99" in error
        for error in errors
    )


def test_empty_dialogue_scene_id_explicitly_allows_semantic_fallback() -> None:
    script = _two_scene_dialogue_binding_script(scene_id="")
    _add_two_scene_dialogue_authority(script)
    script.full_script_text = (
        "【场1】日 / 山顶\n孟浩独自坐在山顶。\n"
        "【场2】日 / 山洞\n山洞里的求救声在石壁间回荡。"
    )

    result = document_to_screenplay(screenplay_to_document(script))

    assert "王有材：救命啊！" in result.full_script_text.split("【场2】")[1]
    assert _scene_character_errors(script) == []


def _run_3f05c2a0fedd_sc03_parser_script() -> tuple[EpisodeScreenplay, dict]:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/run_3f05c2a0fedd_narrator_scene_regression.json"
        ).read_text(encoding="utf-8"),
    )
    regression = fixture["sc03_parser_regression"]
    script = _run_3f05c2a0fedd_environment_script()
    base = script.scene_outline[0]
    script.scene_outline = [
        base.model_copy(update={
            "scene_no": 1,
            "scene_heading": "【场1】四月黄昏 / 大青山高空",
        }),
        base.model_copy(update={
            "scene_no": 2,
            "scene_heading": "【场2】四月黄昏 / 大青山山顶",
        }),
        base.model_copy(update={
            "scene_no": 3,
            "scene_heading": "【场3】四月黄昏 / 大青山山顶",
            "characters": ["孟浩"],
            "story_function": "孟浩回想科举落榜后的生计困境",
            "summary": "孟浩面对连续落榜的现实，心中的迷茫与生计压力不断加深。",
        }),
    ]
    script.full_script_text = (
        "【场1】四月黄昏 / 大青山高空\n群山在暮色中延伸。\n"
        "【场2】四月黄昏 / 大青山山顶\n山风掠过岩石。\n"
        f'【场3】四月黄昏 / 大青山山顶\n{regression["action_prose"]}\n'
        f'{regression["authoritative_dialogue"]}'
    )
    script.dialogue_chains = [KeyDialogueChain(
        chain_id="DC3",
        scene_id="SC03",
        topic="孟浩落榜后的迷茫",
        turns=[KeyDialogueTurn(
            speaker="孟浩",
            line="又落榜了……",
            source_text="又落榜了……",
        )],
    )]
    script.voice_bible = [VoiceCanonical(
        speaker_id="孟浩",
        voice_canonical="清瘦书生的稳定声线",
        role_type="named_character",
    )]
    script.narrative_plan = NarrativeContinuityPlan.model_validate({
        "scope_id": script.id,
        "identity_contracts": [{
            "identity_id": "person-menghao",
            "display_name": "孟浩",
            "kind": "named_character",
            "visual_policy": "canonical",
            "visual_canonical": "青色文士长衫的清瘦书生",
            "asset_requirement": "required",
            "voice_ids": ["孟浩"],
        }],
        "scene_contracts": [
            {"scene_id": "SC01"},
            {"scene_id": "SC02"},
            {"scene_id": "SC03"},
        ],
    })
    return script, regression


def test_run_3f05c2a0fedd_sc03_action_colon_does_not_create_speaker() -> None:
    from app.production.screenplay_document import rederive_projections

    script, regression = _run_3f05c2a0fedd_sc03_parser_script()

    document = rederive_projections(screenplay_to_document(script))
    scene = document.scene_blocks[2]

    assert regression["action_prose"] in [item.text for item in scene.action_blocks]
    assert [turn.speaker for turn in scene.dialogue_turns] == ["孟浩"]
    assert _scene_character_errors(script) == []


def test_known_speaker_chinese_colon_remains_dialogue() -> None:
    from app.production.screenplay_document import rederive_projections

    script, _regression = _run_3f05c2a0fedd_sc03_parser_script()

    document = rederive_projections(screenplay_to_document(script))

    assert [
        (turn.speaker, turn.line)
        for turn in document.scene_blocks[2].dialogue_turns
    ] == [("孟浩", "又落榜了……")]


def test_explicit_unknown_dialogue_reaches_typed_identity_failure() -> None:
    from app.production.screenplay_document import rederive_projections

    script, regression = _run_3f05c2a0fedd_sc03_parser_script()
    script.full_script_text += f'\n{regression["invalid_unknown_dialogue"]}'

    document = rederive_projections(screenplay_to_document(script))
    speakers = [turn.speaker for turn in document.scene_blocks[2].dialogue_turns]

    assert speakers == ["孟浩", "青衣人"]
    errors = _scene_character_errors(script)
    assert any("缺少结构化权威" in error and "青衣人" in error for error in errors)


def test_unowned_action_prose_colon_remains_action() -> None:
    from app.production.screenplay_document import _parse_full_script_scenes

    _script, regression = _run_3f05c2a0fedd_sc03_parser_script()
    parsed = _parse_full_script_scenes(
        f'【场3】四月黄昏 / 大青山山顶\n{regression["action_prose"]}',
        known_speakers={"孟浩": "孟浩"},
    )

    assert parsed[3]["turns"] == []
    assert parsed[3]["actions"] == [regression["action_prose"]]


def test_legal_offscreen_voice_stays_out_of_scene_characters() -> None:
    script = _run_3f05c2a0fedd_environment_script()
    script.narrative_plan = NarrativeContinuityPlan.model_validate({
        "scope_id": script.id,
        "identity_contracts": [{
            "identity_id": "voice-well",
            "display_name": "井下回声",
            "kind": "diegetic offscreen speaker",
            "visual_policy": "offscreen_only",
            "asset_requirement": "forbidden",
            "voice_ids": ["井下回声"],
        }],
        "scene_contracts": [{"scene_id": "SC01"}],
    })
    script.voice_bible = [VoiceCanonical(
        speaker_id="井下回声",
        voice_canonical="遥远、沉闷且带空间混响",
        role_type="offscreen_speaker",
    )]
    script.dialogue_chains = [KeyDialogueChain(
        chain_id="DC1",
        scene_id="SC01",
        topic="井下的警告",
        turns=[KeyDialogueTurn(
            speaker="井下回声",
            line="别下来。",
            source_text="别下来。",
        )],
    )]

    assert _scene_character_errors(script) == []
    script.scene_outline[0].characters = ["井下回声"]
    errors = _scene_character_errors(script)
    assert any("仅声音/离屏身份" in error for error in errors)


def test_legal_structured_narrator_speaks_without_visible_character() -> None:
    script = _run_3f05c2a0fedd_environment_script()
    script.voice_bible = [VoiceCanonical(
        speaker_id="旁白",
        voice_canonical="沉稳克制的叙事声线",
        role_type="narrator",
    )]
    script.dialogue_chains = [KeyDialogueChain(
        chain_id="DC1",
        scene_id="SC01",
        topic="结构化时代介绍",
        turns=[KeyDialogueTurn(
            speaker="旁白",
            line="这是一个修仙者存在的世界。",
            source_text="这是一个修仙者存在的世界。",
        )],
    )]

    assert script.scene_outline[0].characters == []
    assert _scene_character_errors(script) == []


@pytest.mark.asyncio
async def test_run_3f05c2a0fedd_repair_rejects_same_slot_narrator_swap(
    monkeypatch,
) -> None:
    from app.harness import model_gateway
    from app.production import screenplay_repair

    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/run_3f05c2a0fedd_narrator_scene_regression.json"
        ).read_text(encoding="utf-8"),
    )
    script = _run_3f05c2a0fedd_environment_script()
    historical = fixture["repair_artifact"]
    issue = issues_from_validator_messages(
        [historical["reason"]],
        subject="screenplay",
        stage="screenplay",
    )[0]
    bad_candidate = {
        "candidate_id": "CAND-001",
        "operations": [historical["operation"]],
        "satisfies_gap_test": True,
        "passes_deletion_test": True,
        "passes_marginal_gain_test": True,
        "preserves_invariants": True,
        "expected_narrative_gain": 0.8,
        "destructive_cost": 0.0,
    }
    prompts: list[dict] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(json.loads(messages[1]["content"]))
        return json.dumps({
            "semantic_gap": "环境建立场被误判为缺角色",
            "candidate_plans": [
                bad_candidate,
                {
                    **bad_candidate,
                    "candidate_id": "CAND-002",
                    "satisfies_gap_test": False,
                },
            ],
            "selected_candidate_id": "CAND-001",
            "selection_reason": "填入旁白",
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    operations = await screenplay_repair._llm_field_patch_once(
        issue,
        script,
        source_text=script.full_script_text,
    )

    assert operations == []
    policy = prompts[0]["identity_contract_policy"]
    assert "包括旁白" in policy["authority"]
    assert any("prose/summary" in rule for rule in policy["typed_invariants"])


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
async def test_story_function_patch_targets_scene_when_graph_reuses_scene_id(
    monkeypatch,
):
    from app.harness import model_gateway
    from app.production import screenplay_repair

    script = _script()
    script.narrative_plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_scene",
        "scene_contracts": [{"scene_id": "SC01"}],
    })
    message = (
        "[SCENE_STORY_FUNCTION_TOO_SHORT] "
        "scene_outline 第1场「日 / 萧家测验广场」.story_function "
        "过短；请说明本场戏剧功能"
    )
    issue = issues_from_validator_messages(
        [message],
        subject="screenplay",
        stage="screenplay",
    )[0]
    replacement = "建立公开测验冲突并推动萧炎退场"
    candidate = {
        "candidate_id": "SCENE-FIELD",
        "operations": [{
            "op": "replace_field",
            "path": "story_function",
            "target": {"kind": "scene_block", "id": "SC01"},
            "value": replacement,
        }],
        "satisfies_gap_test": True,
        "passes_deletion_test": True,
        "passes_marginal_gain_test": True,
        "preserves_invariants": True,
        "expected_narrative_gain": 1.0,
        "destructive_cost": 0.0,
    }

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({
            "semantic_gap": "场功能未完整说明",
            "candidate_plans": [
                candidate,
                {
                    **candidate,
                    "candidate_id": "REJECTED",
                    "satisfies_gap_test": False,
                },
            ],
            "selected_candidate_id": "SCENE-FIELD",
            "selection_reason": "直接补齐场景字段",
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    document = screenplay_to_document(script)
    plan_data = script.narrative_plan.model_dump(mode="json")

    assert screenplay_repair._candidate_targets_narrative_graph(
        candidate,
        plan_data,
        document=document,
    ) is False

    operations = await screenplay_repair._llm_field_patch_once(
        issue,
        script,
        source_text="原文第一章测验广场段落",
    )

    assert len(operations) == 1
    operation = operations[0]
    assert operation.target["kind"] == "scene"
    assert operation.target["id"] == "SC01"
    patched, touched = apply_field_patch(
        document,
        path=operation.path,
        value=operation.value,
        target=operation.target,
    )
    result = document_to_screenplay(patched)
    assert result.scene_outline[0].story_function == replacement
    assert result.scene_outline[0].summary == script.scene_outline[0].summary
    assert result.scene_outline[0].conflict == script.scene_outline[0].conflict
    assert result.scene_outline[0].turn == script.scene_outline[0].turn
    assert touched == ["SC01"]




@pytest.mark.asyncio
async def test_old_exhausted_checkpoint_resumes_without_second_baseline(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production.patch import PatchOperation
    from app.production import screenplay_authority, screenplay_repair

    revision = ensure_production_revision(
        episode_id="ep_scene",
        kind="screenplay",
        resume=False,
    )
    script = _script()
    artifact = _create_working_artifact(
        evidence_repository,
        screenplay_repair,
        script,
    )
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

    async def semantic_scene_repair(*_args, **_kwargs):
        return [PatchOperation(
            op="replace_field",
            path="story_function",
            value="建立公开测验冲突并推动萧炎退场",
            target={"kind": "screenplay_scene", "id": "SC01"},
        )]

    monkeypatch.setattr(
        screenplay_authority,
        "screenplay_authority_fingerprint",
        lambda *_args, **_kwargs: "authority-test",
    )
    monkeypatch.setattr(screenplay_repair, "run_screenplay_qa", fake_qa)
    monkeypatch.setattr("app.stages.generate_screenplay_baseline", forbidden_baseline)
    monkeypatch.setattr(screenplay_repair, "_llm_field_patch", semantic_scene_repair)
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
    assert resumed.checkpoint_json["open_issue_ids"] == []
    assert resumed.checkpoint_json["last_issue_fingerprints"] == []
    assert len(resumed.checkpoint_json["patch_artifact_ids"]) == 1
    assert resumed.checkpoint_json["issue_strategy_history"]
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
    artifact = _create_working_artifact(
        evidence_repository,
        screenplay_repair,
        script,
    )
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
    async def no_semantic_candidate(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        screenplay_repair,
        "_llm_field_patch",
        no_semantic_candidate,
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
