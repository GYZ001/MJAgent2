"""「动作与结果不得雷同」必须在**有修复循环的那一层**执行。

生产根因 R6（EP1 / spine_beats[221]）：场次创作单元把 `resulting_state` 原样写成
了 `text`（「孟浩的目光落在王有材身上。」），编译器把 text→does、
resulting_state→turn 逐字带下去，下游 `plot_spine` 硬门禁
`SPINE_ACTION_TURN_DUPLICATE` 命中——而那一层**没有任何修复策略**：
规划器立刻记 `exhausted`，整集停在 WAITING_HUMAN，产出的补丁数为 0。

判据本身是确定性的，所以真正的问题不是「模型写得不好」，而是
**同一条规则被放在了唯一修不好它的那一层**。这些用例锁死判据的一致性
（两层必须用同一个谓词）与它的边界（近乎逐字重复才算重复，正常写法不得误伤）。
"""
from __future__ import annotations

from app.screenplay_ir import screenplay_beat_fields_repeat


def test_verbatim_repetition_is_a_duplicate() -> None:
    text = "孟浩的目光落在王有材身上。"
    assert screenplay_beat_fields_repeat(text, text)


def test_a_real_resulting_state_is_not_a_duplicate() -> None:
    assert not screenplay_beat_fields_repeat(
        "孟浩的目光落在王有材身上。",
        "王有材成为孟浩此刻的注意焦点，两人之间形成对峙。",
    )


def test_punctuation_only_restatement_is_a_duplicate() -> None:
    """只改标点不算改内容。"""
    assert screenplay_beat_fields_repeat(
        "孟浩的目光落在王有材身上。",
        "孟浩的目光，落在王有材身上",
    )


def test_one_added_character_is_not_treated_as_duplicate() -> None:
    """判据刻意保守：只拦近乎逐字的重复，不越界替模型判断语义。"""
    assert not screenplay_beat_fields_repeat(
        "孟浩的目光落在王有材身上。",
        "孟浩的目光落在了王有材身上。",
    )


def test_short_fields_are_not_forced_apart() -> None:
    """短字段不做模糊判定，避免把正常的简短写法误判成重复。"""
    assert not screenplay_beat_fields_repeat("他点头", "他答应了")


def test_empty_side_is_never_a_duplicate() -> None:
    assert not screenplay_beat_fields_repeat("", "任何内容")
    assert not screenplay_beat_fields_repeat("任何内容", "")


def test_scene_shard_validator_uses_the_same_predicate() -> None:
    """两层必须共用同一个谓词，否则早拦与晚拦会给出不一致的判定。"""
    import inspect

    from app import screenplay_scene_shards, validators

    shard_source = inspect.getsource(
        screenplay_scene_shards.validate_screenplay_scene_shard
    )
    gate_source = inspect.getsource(
        validators.validate_plot_spine
    )
    assert "screenplay_beat_fields_repeat" in shard_source
    assert "screenplay_beat_fields_repeat" in gate_source


def test_dialogue_units_are_exempt() -> None:
    """对白 text 被合同钉死为源文原句，不能因此判它重复。"""
    import inspect

    from app import screenplay_scene_shards

    source = inspect.getsource(
        screenplay_scene_shards.validate_screenplay_scene_shard
    )
    assert 'unit.kind != "dialogue" and screenplay_beat_fields_repeat' in source
