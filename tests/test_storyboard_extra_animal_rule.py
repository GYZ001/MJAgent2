"""阶段二规则里带上「无卡动物与已建卡动物区分」的正面陈述（2026-09-15 第 2 集老太太的猫被画成三花猫、与听听撞脸）。"""

from app.production.storyboard_narrative_arc import EXTRA_ANIMAL_DISTINCT_RULE, phase2_segment_rules


def test_phase2_rules_include_extra_animal_distinct_rule() -> None:
    rules = phase2_segment_rules(
        continuity_rules=["c"], shared_rules=["s"], required_dialogue_rule_text="d", paratext_exclusion_rule=None,
        palette_current="暖", palette_previous="冷", previous_memo=None, staging_rule=None, structure=None,
    )
    assert EXTRA_ANIMAL_DISTINCT_RULE in rules
    assert "relevant_assets.characters" in EXTRA_ANIMAL_DISTINCT_RULE and "物种或毛色" in EXTRA_ANIMAL_DISTINCT_RULE
