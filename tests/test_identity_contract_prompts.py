from __future__ import annotations

from app.production.screenplay_repair import _identity_contract_repair_policy


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
