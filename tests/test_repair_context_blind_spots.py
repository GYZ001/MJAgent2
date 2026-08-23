"""修复模型必须看得见它要改的字段。

审计发现的结构性盲区：`_narrative_patch_prompt_context` 只带 7 个 key
（metadata / scene_blocks / dialogue_chains / voice_bible / narrative_plan 两跳闭包
/ graph 索引 / scope_note），而 `ScreenplayDocument` 还有 `plot_spine`、
`source_coverage`、`story_events`、`information_ledger` 四个顶层字段**完全不在其中**。
落在这四个字段上的 issue（共 9 个门禁 code）因此只能**盲写**——生产 EP1 的
`SPINE_ACTION_TURN_DUPLICATE` 就是一次补丁都没产出就记 `exhausted`。

切片必须是**有界**的：`plot_spine` 有 300+ 条节拍，整份塞进提示词不可接受。

第一版修复自己踩了一个坑，这里用测试把它钉死：路由表的键必须是**校验器真实
写进 message 的下标标签名**，不是 payload 的字段名。二者不同名时（`events[i]`
的 payload 键是 `story_events`），按字段名建键会让切片对该字段静默失效，
而用捏造的消息文本写的测试照样全绿。因此本文件的测试：

* 用**真实校验器**跑出真实错误消息，再喂给真实的 `_issue_target_excerpt`；
* 扫描 `app/validators.py` 里所有 `name[{i}]` 形态的下标标签，
  要求每个标签要么可路由，要么在带理由的白名单里。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.harness.types import Issue, IssueSeverity
from app.production.screenplay_document import screenplay_to_document
from app.production.screenplay_repair import (
    _ISSUE_TARGET_CONTAINERS,
    _issue_target_excerpt,
    _narrative_patch_prompt_context,
)
from app.schemas import (
    EpisodeScreenplay,
    InformationItem,
    PlotSpine,
    PlotSpineBeat,
    SourceCoverageDecision,
    StoryEvent,
)
from app.production.structured_issues import issues_from_validator_messages
from app.validators import validate_plot_spine


def _issue(message: str, **kwargs) -> Issue:
    return Issue(
        code="X", message=message, subject="screenplay",
        severity=IssueSeverity.BLOCKER, **kwargs,
    )


def _payload(beats: int = 300) -> dict:
    return {
        "plot_spine": {
            "spine_beats": [{"beat_id": f"S{i}"} for i in range(beats)],
            "drop_list": [f"D{i}" for i in range(4)],
        },
        "source_coverage": [{"source_segment_id": f"SRC{i:04d}"} for i in range(5)],
        "story_events": [{"event_id": f"E{i}"} for i in range(6)],
        "information_ledger": [{"info_id": f"I{i}"} for i in range(6)],
    }


# ---------------------------------------------------------------- 真实链路

def _screenplay_with_duplicate_beat() -> EpisodeScreenplay:
    """一份除了「第 3 条节拍 does 与 turn 雷同」以外结构完整的剧本。"""
    beats = [
        PlotSpineBeat(
            beat_id=f"S{i:02d}",
            who="谷言",
            does=f"谷言在第{i}处采取行动推进局势",
            turn=f"局势因此第{i}次改变",
        )
        for i in range(1, 6)
    ]
    beats[2] = PlotSpineBeat(
        beat_id="S03",
        who="谷言",
        does="谷言推开石门走进内室",
        turn="谷言推开石门走进内室。",
    )
    return EpisodeScreenplay(
        episode_no=1,
        plot_spine=PlotSpine(
            episode_premise="主角要证明自己并守住本章结局",
            spine_beats=beats,
            must_keep_ending="本章当场收束，不提前下一章",
            drop_list=["路人起哄的多轮对话"],
        ),
        events=[
            StoryEvent(
                event_id=f"E{i}",
                state_in=f"起始状态{i}",
                visible_change=f"可见变化{i}",
                state_out=f"结束状态{i}",
            )
            for i in range(4)
        ],
        information_ledger=[
            InformationItem(info_id=f"I{i}", content=f"信息{i}")
            for i in range(4)
        ],
        source_coverage=[
            SourceCoverageDecision(
                source_segment_id=f"SRC{i:04d}",
                disposition="deliver",
                beat_ids=[f"S0{i + 1}"],
            )
            for i in range(4)
        ],
    )


def test_real_validator_message_routes_to_the_real_offending_beat() -> None:
    """端到端：真实门禁 → 真实 Issue → 真实切片，窗口里必须有肇事那条。

    这条测试不构造任何消息文本；`SPINE_ACTION_TURN_DUPLICATE` 的措辞、下标
    标签、payload 路径三者只要有一处对不上，窗口就会是空的。
    """
    script = _screenplay_with_duplicate_beat()
    messages = validate_plot_spine(script)
    duplicates = [m for m in messages if "SPINE_ACTION_TURN_DUPLICATE" in m]
    assert duplicates, f"门禁没有按预期触发：{messages}"

    issues = issues_from_validator_messages(
        duplicates, subject="screenplay", stage="structure_validation",
    )
    payload = screenplay_to_document(script).model_dump(mode="json")
    excerpt = _issue_target_excerpt(payload, issues[0])

    assert "spine_beats" in excerpt, "修复模型仍然看不见它要改的那条节拍"
    beat_ids = [item["beat_id"] for item in excerpt["spine_beats"]["items"]]
    assert "S03" in beat_ids
    assert len(beat_ids) <= 2 * 1 + 1


def test_real_event_message_uses_the_tag_the_validator_actually_emits() -> None:
    """`events[i]` 的 payload 键是 `story_events`——路由表必须按标签名建键。

    第一版按 payload 字段名建了 `story_events` 键，于是这一整个字段的切片
    永远为空。这条测试用校验器真实写出的 `events[i]` 标签反查，专杀该缺陷。
    """
    script = _screenplay_with_duplicate_beat()
    # 真实门禁写法见 app/validators.py::validate_screenplay 的 tag = f"events[{i}]"
    issues = issues_from_validator_messages(
        ["events[2].state_in 缺失或过短；事件必须写清状态输入、可见变化和状态输出"],
        subject="screenplay", stage="structure_validation",
    )
    payload = screenplay_to_document(script).model_dump(mode="json")
    excerpt = _issue_target_excerpt(payload, issues[0])

    assert "events" in excerpt
    assert excerpt["events"]["path"] == "story_events"
    assert [item["event_id"] for item in excerpt["events"]["items"]] == ["E1", "E2", "E3"]


_TAG_RE = re.compile(r'f"(?:\[[A-Z_]+\]\s*)?([a-z_][a-z0-9_.]*)\[\{')

# 校验器确实会写出下标，但**故意不路由**的标签，必须写清理由。
_INTENTIONALLY_UNROUTED = {
    # scene_blocks / dialogue_chains 本来就整体在上下文里，再切一份是重复。
    "scene_blocks",
    "dialogue_chains",
    "scene_outline",
    "shots",
    "beats",
    # characters / scenes 是**人物圣经 / 场景圣经**的下标，属于另一份产物，
    # 不在 ScreenplayDocument 上，剧本修复上下文里没有可切的目标。
    "characters",
    "scenes",
}


def test_no_validator_tag_silently_falls_through_the_routing_table() -> None:
    """扫真实校验器源码：出现新的下标标签而没登记，这条会红。

    这是防止「按 payload 字段名建键」那类静默失效再次发生的漂移闸门。
    """
    source = Path("app/validators.py").read_text(encoding="utf-8")
    tags = {m.rsplit(".", 1)[-1] for m in _TAG_RE.findall(source)}
    assert tags, "标签扫描失效（校验器写法变了），这条守卫必须同步更新"

    unrouted = tags - set(_ISSUE_TARGET_CONTAINERS) - _INTENTIONALLY_UNROUTED
    assert not unrouted, (
        f"这些下标标签既不可路由也没登记为故意不路由：{sorted(unrouted)}；"
        "要么加进 _ISSUE_TARGET_CONTAINERS，要么写明为什么不需要切片"
    )


def test_every_routed_key_is_a_tag_some_validator_really_emits() -> None:
    """反向守卫：路由表里不许有校验器从不产出的死键。

    第一版的 `story_events` 与 `drop_list` 就是这样的死键——看着覆盖了字段，
    实际一次也命中不了。
    """
    source = Path("app/validators.py").read_text(encoding="utf-8")
    tags = {m.rsplit(".", 1)[-1] for m in _TAG_RE.findall(source)}

    dead = set(_ISSUE_TARGET_CONTAINERS) - tags
    assert not dead, f"路由表里的死键（没有任何校验器会这样写）：{sorted(dead)}"


def test_routed_keys_resolve_to_real_document_fields() -> None:
    """路由表的值必须是 `ScreenplayDocument` 上真实存在的列表路径。"""
    payload = screenplay_to_document(_screenplay_with_duplicate_beat()).model_dump(
        mode="json"
    )
    for tag, route in _ISSUE_TARGET_CONTAINERS.items():
        container = payload
        for key in route:
            assert isinstance(container, dict), f"{tag} 的路径 {route} 不存在"
            assert key in container, f"{tag} 的路径 {route} 在文档里没有 {key}"
            container = container[key]
        assert isinstance(container, list), f"{tag} 的路径 {route} 不是列表"


# ---------------------------------------------------------------- 切片行为

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
        ("events[2].event_id 不能为空", "events"),
        ("information_ledger[4].content 缺失", "information_ledger"),
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
    """调真实的 `_narrative_patch_prompt_context`：有切片才暴露该 key。"""
    document = screenplay_to_document(_screenplay_with_duplicate_beat())
    duplicates = [
        m for m in validate_plot_spine(_screenplay_with_duplicate_beat())
        if "SPINE_ACTION_TURN_DUPLICATE" in m
    ]
    hit = issues_from_validator_messages(
        duplicates, subject="screenplay", stage="structure_validation",
    )[0]
    miss = _issue("metadata.title 过短")

    context_hit, _ = _narrative_patch_prompt_context(document, hit, "原文")
    context_miss, _ = _narrative_patch_prompt_context(document, miss, "原文")

    assert "issue_target_excerpt" in context_hit
    assert "spine_beats" in context_hit["issue_target_excerpt"]
    # 只有真的带了切片，scope_note 才解释它——否则模型会去找一个不存在的 key。
    assert "issue_target_excerpt" in context_hit["scope_note"]
    assert "issue_target_excerpt" not in context_miss
    assert "issue_target_excerpt" not in context_miss["scope_note"]
