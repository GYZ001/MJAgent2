"""修复模型必须看得见它要改的字段。

审计发现的结构性盲区：`_narrative_patch_prompt_context` 只带 7 个 key
（metadata / scene_blocks / dialogue_chains / voice_bible / narrative_plan 两跳闭包
/ graph 索引 / scope_note），而 `ScreenplayDocument` 还有 `plot_spine`、
`source_coverage`、`story_events`、`information_ledger` 四个顶层字段**完全不在其中**。
落在这四个字段上的 issue（共 9 个门禁 code）因此只能**盲写**——生产 EP1 的
`SPINE_ACTION_TURN_DUPLICATE` 就是一次补丁都没产出就记 `exhausted`。

切片必须是**有界**的：`plot_spine` 有 300+ 条节拍，整份塞进提示词不可接受。
"""
from __future__ import annotations

import pytest

from app.harness.types import Issue, IssueSeverity
from app.production.screenplay_repair import _issue_target_excerpt


def _issue(message: str, **kwargs) -> Issue:
    return Issue(
        code="X", message=message, subject="screenplay",
        severity=IssueSeverity.BLOCKER, **kwargs,
    )


def _payload(beats: int = 300) -> dict:
    return {
        "plot_spine": {
            "spine_beats": [{"beat_id": f"S{i}"} for i in range(beats)],
            "drop_list": [{"item": f"D{i}"} for i in range(4)],
        },
        "source_coverage": [{"source_segment_id": f"SRC{i:04d}"} for i in range(5)],
        "story_events": [{"event_id": f"E{i}"} for i in range(6)],
        "information_ledger": [{"info_id": f"I{i}"} for i in range(6)],
    }


def test_spine_beat_issue_gets_a_bounded_window() -> None:
    excerpt = _issue_target_excerpt(
        _payload(),
        _issue("plot_spine.spine_beats[221].does 与 turn 语义重复"),
    )

    assert excerpt["spine_beats"]["path"] == "plot_spine.spine_beats"
    assert excerpt["spine_beats"]["window_start_index"] == 220
    ids = [item["beat_id"] for item in excerpt["spine_beats"]["items"]]
    assert ids == ["S220", "S221", "S222"]


@pytest.mark.parametrize(
    ("message", "field"),
    [
        ("source_coverage[3] 缺少 beat_ids", "source_coverage"),
        ("story_events[2] 无效", "story_events"),
        ("information_ledger[4] 缺失", "information_ledger"),
        ("plot_spine.drop_list[1] 无理由", "drop_list"),
    ],
)
def test_every_previously_blind_field_becomes_visible(
    message: str, field: str,
) -> None:
    excerpt = _issue_target_excerpt(_payload(), _issue(message))

    assert field in excerpt
    assert 1 <= len(excerpt[field]["items"]) <= 3


def test_fields_already_in_the_context_are_not_duplicated() -> None:
    """scene_blocks / dialogue_chains 本来就整体在上下文里，不需要再切一份。"""
    excerpt = _issue_target_excerpt(
        _payload(), _issue("scene_blocks[2].story_function 过短"),
    )

    assert excerpt == {}


def test_window_never_leaves_the_list_bounds() -> None:
    excerpt = _issue_target_excerpt(
        _payload(beats=2), _issue("plot_spine.spine_beats[0].does 重复"),
    )

    assert excerpt["spine_beats"]["window_start_index"] == 0
    assert len(excerpt["spine_beats"]["items"]) == 2


def test_out_of_range_index_yields_no_excerpt_instead_of_crashing() -> None:
    excerpt = _issue_target_excerpt(
        _payload(beats=3), _issue("plot_spine.spine_beats[999].does 重复"),
    )

    # 越界只会得到空窗口，绝不能抛异常打断整个修复流程。
    assert excerpt.get("spine_beats", {}).get("items", []) == []


def test_missing_container_is_tolerated() -> None:
    assert _issue_target_excerpt({}, _issue("plot_spine.spine_beats[1].does 重复")) == {}


def test_context_exposes_the_excerpt_only_when_it_exists() -> None:
    import inspect

    from app.production import screenplay_repair

    source = inspect.getsource(
        screenplay_repair._narrative_patch_prompt_context
    )
    assert 'context["issue_target_excerpt"] = target_excerpt' in source
    assert "if target_excerpt:" in source
