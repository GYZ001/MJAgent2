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

import pytest

from app.screenplay_ir import screenplay_beat_fields_repeat
from app.screenplay_scene_shards import (
    build_screenplay_scene_shard_plans,
    validate_screenplay_scene_shard,
)
from tests.test_screenplay_scene_shards import (
    SOURCE,
    _blueprint,
    _contracts,
    _shard,
)

_DUPLICATE_ERROR = "resulting_state 与 text 语义重复"


def _validate_with_unit(mutate) -> list[str]:
    """跑**真实**的分片校验器，只改动第一个非对白单元。"""
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    shard = _shard(plan, blueprint)
    unit = next(
        u for scene in shard.scenes for u in scene.units if u.kind != "dialogue"
    )
    mutate(unit)
    return validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=_contracts([plan], blueprint)[plan.shard_id],
        identity_keys={"narrator"},
    )


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


# --------------------------------------------------- 真实分片校验器上的行为

def test_scene_shard_validator_really_rejects_a_restated_unit() -> None:
    """真实调用：非对白单元把 resulting_state 写回 text，必须当场拦下。

    这是 R6 的核心断言——规则必须在**这一层**生效，而不是等到下游那个
    修不好它的门禁。
    """
    def restate(unit) -> None:
        unit.resulting_state = unit.text

    errors = _validate_with_unit(restate)
    assert any(_DUPLICATE_ERROR in error for error in errors), errors


def test_scene_shard_validator_accepts_a_real_resulting_state() -> None:
    """正常写法不得误伤：这条保证新规则不是「一律报错」。"""
    def proper(unit) -> None:
        unit.text = "孟浩的目光落在王有材身上。"
        unit.resulting_state = "王有材成为孟浩此刻的注意焦点，两人之间形成对峙。"

    errors = _validate_with_unit(proper)
    assert not any(_DUPLICATE_ERROR in error for error in errors), errors


def test_dialogue_units_are_exempt_on_the_real_validator() -> None:
    """对白 text 被合同钉死为源文原句，不能因此判它重复。

    对同一份 text/resulting_state 只翻转 `kind` 这一个维度做差分：
    action 必须报重复，dialogue 必须不报。（翻转 kind 会另外触发结构漂移
    错误，那是别的规则，这里只断言重复这一条的有无。）
    """
    def restate(unit) -> None:
        unit.resulting_state = unit.text

    def restate_as_dialogue(unit) -> None:
        unit.resulting_state = unit.text
        unit.kind = "dialogue"

    assert any(_DUPLICATE_ERROR in e for e in _validate_with_unit(restate))
    assert not any(
        _DUPLICATE_ERROR in e for e in _validate_with_unit(restate_as_dialogue)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("narrative_layer", "paratext"), ("render_policy", "exclude_from_spine")],
)
def test_paratext_units_are_exempt_because_the_downstream_gate_never_sees_them(
    field: str, value: str,
) -> None:
    """域必须与下游门禁一致，不能更宽。

    `finalize_screenplay_ir` 的「非剧情旁文本隔离」把 paratext /
    exclude_from_spine 事件整体剔出 events / beats / units，它们根本走不到
    `validate_plot_spine`。若在分片层顺手把它们也判掉，就是凭空发明了一条
    下游从不存在的约束，去卡下游刻意排除的内容——那不是提前拦截，是加严。
    """
    def restate_as_paratext(unit) -> None:
        unit.resulting_state = unit.text
        setattr(unit, field, value)

    errors = _validate_with_unit(restate_as_paratext)
    assert not any(_DUPLICATE_ERROR in error for error in errors), errors
