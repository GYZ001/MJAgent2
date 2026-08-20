import asyncio
import json
from pathlib import Path
import uuid

from app import stages
from app.narrative_blueprint import (
    BlueprintSemanticReview,
    NarrativeBlueprint,
    blueprint_semantic_voice_issue_has_dialogue_authority,
    blueprint_voice_identity_issues,
    derive_blueprint_scene_plans,
    filter_blueprint_semantic_review_voice_issues,
)
from app.source_facts import source_facts


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "run_3fb9353fed4b_voice_review_contract.json"
)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _source_text(payload: dict) -> str:
    return "\n\n".join(
        segment["text"] for segment in payload["source_segments"]
    )


def _blueprint(payload: dict) -> NarrativeBlueprint:
    nodes = json.loads(json.dumps(payload["nodes"], ensure_ascii=False))
    for node in nodes:
        deliveries = []
        for evidence in node.get("participant_evidence") or []:
            if evidence.get("usage") != "voice":
                continue
            deliveries.extend({
                "source_unit_key": source_unit_key,
                "mode": "spoken_dialogue",
                "content_owner_key": evidence["identity_key"],
                "performer_key": evidence["identity_key"],
            } for source_unit_key in evidence.get("source_unit_keys") or [])
        node["source_unit_deliveries"] = deliveries
    return NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": nodes,
    })


def test_production_prose_narration_cannot_become_voice_issue() -> None:
    payload = _fixture()
    source_text = _source_text(payload)
    blueprint = _blueprint(payload)
    facts = {
        fact.source_unit_key: fact.projection
        for fact in source_facts(source_text)
    }

    assert facts["SRC0001:unit:001"] == "action"
    assert facts["SRC0002:unit:001"] == "action"
    assert facts["SRC0005:unit:001"] == "quoted"
    assert blueprint_voice_identity_issues(blueprint, source_text) == []

    review = BlueprintSemanticReview.model_validate({
        "issues": [payload["unsupported_reviewer_issue"]],
    })
    assert not blueprint_semantic_voice_issue_has_dialogue_authority(
        review.issues[0],
        blueprint,
        source_text,
    )
    assert filter_blueprint_semantic_review_voice_issues(
        review,
        blueprint,
        source_text,
    ) == 1
    assert review.issues == []


def test_real_dialogue_still_requires_exact_unique_speaker() -> None:
    payload = _fixture()
    source_text = _source_text(payload)
    blueprint = _blueprint(payload)
    node = next(
        node for node in blueprint.nodes if node.key == "S001-N002"
    )
    node.participant_evidence = [
        evidence
        for evidence in node.participant_evidence
        if evidence.usage != "voice"
    ]

    deterministic_issues = blueprint_voice_identity_issues(
        blueprint,
        source_text,
    )

    assert [
        (
            issue.code,
            issue.node_keys,
            issue.source_segment_ids,
        )
        for issue in deterministic_issues
    ] == [(
        "voice_identity_missing",
        ["S001-N002"],
        ["SRC0005"],
    )]
    assert deterministic_issues[0].source_unit_keys == [
        "SRC0005:unit:001"
    ]
    review = BlueprintSemanticReview.model_validate({
        "issues": [payload["supported_dialogue_issue"]],
    })
    assert blueprint_semantic_voice_issue_has_dialogue_authority(
        review.issues[0],
        blueprint,
        source_text,
    )
    correct_unit_issue = review.issues[0].model_copy(
        update={"source_unit_keys": ["SRC0005:unit:001"]},
        deep=True,
    )
    wrong_unit_issue = review.issues[0].model_copy(
        update={"source_unit_keys": ["SRC0001:unit:001"]},
        deep=True,
    )
    assert blueprint_semantic_voice_issue_has_dialogue_authority(
        correct_unit_issue,
        blueprint,
        source_text,
    )
    correct_unit_review = BlueprintSemanticReview(issues=[
        correct_unit_issue,
    ])
    assert filter_blueprint_semantic_review_voice_issues(
        correct_unit_review,
        blueprint,
        source_text,
    ) == 0
    assert correct_unit_review.issues == [correct_unit_issue]
    assert not blueprint_semantic_voice_issue_has_dialogue_authority(
        wrong_unit_issue,
        blueprint,
        source_text,
    )
    wrong_unit_review = BlueprintSemanticReview(issues=[wrong_unit_issue])
    assert filter_blueprint_semantic_review_voice_issues(
        wrong_unit_review,
        blueprint,
        source_text,
    ) == 1
    assert wrong_unit_review.issues == []
    unscoped_issue = review.issues[0].model_copy(
        update={"source_segment_ids": []},
    )
    assert not blueprint_semantic_voice_issue_has_dialogue_authority(
        unscoped_issue,
        blueprint,
        source_text,
    )
    assert filter_blueprint_semantic_review_voice_issues(
        review,
        blueprint,
        source_text,
    ) == 0
    assert len(review.issues) == 1


def test_unsupported_voice_issue_is_dropped_before_consensus(
    monkeypatch,
) -> None:
    payload = _fixture()
    source_text = _source_text(payload)
    blueprint = _blueprint(payload)
    derive_blueprint_scene_plans(blueprint)
    artifacts = []
    prompts: list[str] = []

    class EmptyRows:
        @staticmethod
        def fetchall():
            return []

    class EmptyConnection:
        @staticmethod
        def execute(*_args, **_kwargs):
            return EmptyRows()

    async def fake_structured(messages, **_kwargs):
        prompts.append(messages[-1]["content"])
        return BlueprintSemanticReview.model_validate({
            "issues": [payload["unsupported_reviewer_issue"]],
        })

    async def forbidden_repair(*_args, **_kwargs):
        raise AssertionError("unsupported prose voice issue reached repair")

    def fake_create_artifact(artifact, **_kwargs):
        artifacts.append(artifact)
        return {"id": f"art-{uuid.uuid4()}"}

    monkeypatch.setattr(stages, "get_conn", lambda: EmptyConnection())
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: "true"
        if key == "screenplay_targeted_blueprint_review_enabled"
        else "1",
    )
    monkeypatch.setattr(
        stages.model_gateway,
        "chat_structured",
        fake_structured,
    )
    monkeypatch.setattr(
        stages,
        "_repair_narrative_blueprint",
        forbidden_repair,
    )
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        fake_create_artifact,
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={
            "id": "ep-run-3fb9353fed4b-regression",
            "episode_no": 1,
        },
        source_text=source_text,
    ))

    assert result is blueprint
    assert len(prompts) == 2
    assert all(
        '"source_unit_key":"SRC0001:unit:001"' in prompt
        and '"projection":"action"' in prompt
        and "旁白介绍" in prompt
        for prompt in prompts
    )
    reviewer_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.type == "screenplay_narrative_blueprint_review"
    ]
    consensus = next(
        artifact
        for artifact in artifacts
        if artifact.type
        == "screenplay_narrative_blueprint_review_consensus"
    )
    assert len(reviewer_artifacts) == 2
    assert all(
        artifact.content["issues"] == []
        and artifact.model_snapshot[
            "dropped_unsupported_voice_issue_count"
        ] == 1
        for artifact in reviewer_artifacts
    )
    assert consensus.content["consensus_issue_keys"] == []
    assert consensus.content[
        "dropped_unsupported_voice_issue_count"
    ] == 2
