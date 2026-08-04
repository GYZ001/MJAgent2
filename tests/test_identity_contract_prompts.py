from __future__ import annotations

import json

from app.production.screenplay_repair import _identity_contract_repair_policy
from app.stages import _narrative_plan_prompt_block, _narrative_plan_schema_example


def test_narrative_plan_prompt_declares_every_example_entity_with_typed_policy() -> None:
    example = json.loads(_narrative_plan_schema_example("episode-prompt-contract"))
    contracts = example["identity_contracts"]
    by_id = {item["identity_id"]: item for item in contracts}
    referenced_entity_ids = {
        entity_id
        for proposition in example["propositions"]
        for entity_id in proposition["entity_ids"]
    }

    assert referenced_entity_ids <= set(by_id)
    assert set(by_id["character-id"]) == {
        "identity_id",
        "display_name",
        "kind",
        "visual_policy",
        "visual_canonical",
        "asset_requirement",
        "voice_ids",
        "evidence",
    }
    assert set(by_id["character-id"]["evidence"]) == {
        "source_evidence_ids",
        "proposition_ids",
        "adaptation_decision_ids",
        "rationale",
    }

    prompt = _narrative_plan_prompt_block("episode-prompt-contract")
    assert "未声明身份不得进入剧本或分镜" in prompt
    assert "不得使用姓名、称谓或题材白名单判定" in prompt
    assert all(
        policy in prompt
        for policy in ("canonical", "contextual", "collective", "offscreen_only")
    )
    assert "voice_ids 必须精确回指 voice_bible.speaker_id" in prompt
    assert "speaker_id 必须直接使用人物谱准确姓名" in prompt
    assert "禁止另造 V-MH 一类声音别名" in prompt


def test_narrative_repair_prompt_policy_preserves_identity_fail_closed_rules() -> None:
    policy = _identity_contract_repair_policy()

    assert set(policy["contract_fields"]) == {
        "identity_id",
        "display_name",
        "kind",
        "visual_policy",
        "visual_canonical",
        "asset_requirement",
        "voice_ids",
        "evidence",
    }
    assert "canonical 必须 asset_requirement=required" in policy["typed_invariants"]
    assert "offscreen_only 必须 asset_requirement=forbidden" in policy["typed_invariants"]
    assert "姓名、称谓、身份类型或题材白名单" in policy["semantic_decision"]
    assert "未声明" in policy["authority"]
