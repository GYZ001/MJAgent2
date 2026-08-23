"""场次语义死结必须回到拥有该决定的那一层（叙事蓝图），而不是杀死整集。

生产根因 R5（EP2 / SS002）：源文按逗号切分出的 unit 里，有些在语法上无法独立成画
（「但他性格坚毅，」是人物内在特质，「可在看到…绿袍男子后，」是时间状语从句）。
蓝图把前者标成 `environment_source_unit_keys`（纯环境），而环境 slot 的合同禁止写
任何人物内容 —— 于是写环境与源文矛盾、写人物触发 environment_personification，
**可证明无解**。场次层唯一的补救手段是重写文案，修不好一个分类错误：
三轮修复每轮得到完全相同的双审共识，SS002 历史上累计 254 次 provider 调用仍卡死。

这些用例锁死的是**处置方式**，不是某段文本：
  * 语义门禁未收口时，失败必须携带 unit → 双审共识原文；
  * 带着这份证据重建蓝图，且证据必须进入分片的 source payload（既改变
    source_hash 使缓存分片不被复用，也作为显式约束进入提示词）；
  * 重建严格有界，用尽后照常失败；
  * 其它类型的分片失败（schema/归属/预算）绝不触发重建。
"""
from __future__ import annotations

import pytest

from app import stages
from app.screenplay_scene_shards import ScreenplaySceneShardError


class _Segment:
    def __init__(self, segment_id: str, text: str):
        self.segment_id = segment_id
        self.text = text


def test_gate_failure_carries_the_unresolved_units() -> None:
    error = ScreenplaySceneShardError(
        "SS002",
        ["bp-sc004:SRC0014:008:unit creative semantic gate 未收口：x"],
        unresolved_semantic_units={"SRC0014:unit:008": ["双审共识原话"]},
    )

    assert error.shard_id == "SS002"
    assert error.unresolved_semantic_units == {
        "SRC0014:unit:008": ["双审共识原话"]
    }


def test_other_shard_failures_carry_no_rebuild_evidence() -> None:
    """schema / 归属 / 预算类失败不是分类问题，不得触发蓝图重建。"""
    error = ScreenplaySceneShardError("SS001", ["slot 缺失"])

    assert error.unresolved_semantic_units == {}


def test_feedback_enters_the_shard_source_payload() -> None:
    segment = _Segment("SRC0014", "不远处，一块山石上，坐着一个青年。")

    entry = stages._blueprint_shard_source_entry(
        segment,
        {"SRC0014:unit:008": ["把时间状语从句改写为主动作陈述"]},
    )

    assert entry["source_segment_id"] == "SRC0014"
    assert entry["downstream_semantic_conflicts"] == {
        "SRC0014:unit:008": ["把时间状语从句改写为主动作陈述"]
    }
    # 结构化事实本身不能被污染：schema 构造只读 source_facts。
    assert all(
        "downstream_semantic_conflicts" not in fact
        for fact in entry["source_facts"]
    )


def test_feedback_for_another_segment_is_not_leaked() -> None:
    segment = _Segment("SRC0014", "不远处，一块山石上，坐着一个青年。")

    entry = stages._blueprint_shard_source_entry(
        segment, {"SRC0012:unit:003": ["别的分段的死结"]},
    )

    assert "downstream_semantic_conflicts" not in entry


def test_feedback_changes_the_shard_source_hash() -> None:
    """反馈必须改变 source_hash，否则重建会直接复用缓存分片，等于空操作。"""
    import hashlib
    import json

    segment = _Segment("SRC0014", "不远处，一块山石上，坐着一个青年。")

    def source_hash(feedback):
        payload = [stages._blueprint_shard_source_entry(segment, feedback)]
        return hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    assert source_hash(None) != source_hash(
        {"SRC0014:unit:008": ["死结"]}
    )


def test_prompt_states_the_reclassification_constraint() -> None:
    payload = [
        stages._blueprint_shard_source_entry(
            _Segment("SRC0012", "但他性格坚毅，此刻深吸口气。"),
            {"SRC0012:unit:003": ["环境 slot 与源文矛盾"]},
        )
    ]

    prompt = stages._blueprint_shard_prompt(
        episode_no=2, shard_index=1, shard_count=1, errors=[],
        bible_context={}, boundary={}, source_payload=payload,
    )

    assert "downstream_semantic_conflicts" in prompt
    assert "environment_source_unit_keys" in prompt
    assert "state_subject" in prompt


def test_prompt_stays_unchanged_without_feedback() -> None:
    payload = [
        stages._blueprint_shard_source_entry(
            _Segment("SRC0012", "但他性格坚毅，此刻深吸口气。"), None,
        )
    ]

    prompt = stages._blueprint_shard_prompt(
        episode_no=2, shard_index=1, shard_count=1, errors=[],
        bible_context={}, boundary={}, source_payload=payload,
    )

    assert "downstream_semantic_conflicts" not in prompt


def test_rebuild_budget_is_bounded_and_explicit() -> None:
    assert stages.SCREENPLAY_BLUEPRINT_SEMANTIC_REBUILD_LIMIT >= 1
    # 真正无解的输入不会因为多试几次而变得有解：预算必须很小。
    assert stages.SCREENPLAY_BLUEPRINT_SEMANTIC_REBUILD_LIMIT <= 2


@pytest.mark.asyncio
async def test_gate_dead_end_rebuilds_the_blueprint_once_then_succeeds(
    monkeypatch,
) -> None:
    """死结 → 带证据重建蓝图 → 第二次成功；重建必须真的带上证据。"""
    calls: list[dict[str, list[str]] | None] = []
    shard_attempts = {"n": 0}

    async def fake_workflow_step(_key, operation, **_kwargs):
        return None, await operation()

    async def fake_blueprint(_episode, _source, _bible, *, semantic_feedback=None):
        calls.append(semantic_feedback)
        return object()

    async def fake_shards(_episode, _source, _bible, *, narrative_blueprint):
        shard_attempts["n"] += 1
        if shard_attempts["n"] == 1:
            raise ScreenplaySceneShardError(
                "SS002", ["未收口"],
                unresolved_semantic_units={"SRC0012:unit:003": ["与源文矛盾"]},
            )
        return "screenplay"

    monkeypatch.setattr(stages, "_run_screenplay_workflow_step", fake_workflow_step)
    monkeypatch.setattr(
        stages, "_generate_screenplay_narrative_blueprint", fake_blueprint
    )
    monkeypatch.setattr(
        stages, "_generate_screenplay_scene_sharded_baseline", fake_shards
    )

    result = await stages.generate_screenplay({"id": "e1", "episode_no": 2}, "源文", None)

    assert result == "screenplay"
    assert shard_attempts["n"] == 2
    # 第一次无反馈，第二次必须带着下游死结证据。
    assert calls[0] == {}
    assert calls[1] == {"SRC0012:unit:003": ["与源文矛盾"]}


@pytest.mark.asyncio
async def test_dead_end_that_survives_the_rebuild_still_fails(monkeypatch) -> None:
    """用尽重建预算后照常失败——不得无限重建，也不得吞掉失败。"""
    attempts = {"n": 0}

    async def fake_workflow_step(_key, operation, **_kwargs):
        return None, await operation()

    async def fake_blueprint(_episode, _source, _bible, *, semantic_feedback=None):
        return object()

    async def fake_shards(_episode, _source, _bible, *, narrative_blueprint):
        attempts["n"] += 1
        raise ScreenplaySceneShardError(
            "SS002", ["未收口"],
            unresolved_semantic_units={"SRC0012:unit:003": ["与源文矛盾"]},
        )

    monkeypatch.setattr(stages, "_run_screenplay_workflow_step", fake_workflow_step)
    monkeypatch.setattr(
        stages, "_generate_screenplay_narrative_blueprint", fake_blueprint
    )
    monkeypatch.setattr(
        stages, "_generate_screenplay_scene_sharded_baseline", fake_shards
    )

    with pytest.raises(ScreenplaySceneShardError):
        await stages.generate_screenplay({"id": "e1", "episode_no": 2}, "源文", None)

    assert attempts["n"] == stages.SCREENPLAY_BLUEPRINT_SEMANTIC_REBUILD_LIMIT + 1


@pytest.mark.asyncio
async def test_non_dead_end_shard_failure_never_rebuilds(monkeypatch) -> None:
    attempts = {"n": 0}

    async def fake_workflow_step(_key, operation, **_kwargs):
        return None, await operation()

    async def fake_blueprint(_episode, _source, _bible, *, semantic_feedback=None):
        return object()

    async def fake_shards(_episode, _source, _bible, *, narrative_blueprint):
        attempts["n"] += 1
        raise ScreenplaySceneShardError("SS001", ["slot 缺失"])

    monkeypatch.setattr(stages, "_run_screenplay_workflow_step", fake_workflow_step)
    monkeypatch.setattr(
        stages, "_generate_screenplay_narrative_blueprint", fake_blueprint
    )
    monkeypatch.setattr(
        stages, "_generate_screenplay_scene_sharded_baseline", fake_shards
    )

    with pytest.raises(ScreenplaySceneShardError):
        await stages.generate_screenplay({"id": "e1", "episode_no": 2}, "源文", None)

    assert attempts["n"] == 1


def test_cached_leaf_is_a_miss_only_when_this_activation_changed_its_input() -> None:
    """重建刻意改了分片输入 ⇒ 旧 leaf 不适用；这不是权威漂移。

    不区分这两者的话，重建必然停在 BLUEPRINT_SPLIT_MANIFEST_AUTHORITY——
    恰好死在这个机制要救的那个场景里（第一轮蓝图成功过，它的 leaf 一定已被缓存）。
    """
    payload_with_feedback = [
        stages._blueprint_shard_source_entry(
            _Segment("SRC0012", "但他性格坚毅，此刻深吸口气。"),
            {"SRC0012:unit:003": ["与源文矛盾"]},
        )
    ]
    payload_plain = [
        stages._blueprint_shard_source_entry(
            _Segment("SRC0012", "但他性格坚毅，此刻深吸口气。"), None,
        )
    ]

    # 本次注入了证据、且哈希确实变了 ⇒ 视为缓存未命中，重新生成该分片。
    assert stages._cached_leaf_superseded_by_feedback(
        cached_source_hash="old", source_hash="new",
        source_payload=payload_with_feedback,
    )
    # 没有注入证据的分片，哈希不一致仍然是权威漂移，严格性一个字都没放松。
    assert not stages._cached_leaf_superseded_by_feedback(
        cached_source_hash="old", source_hash="new",
        source_payload=payload_plain,
    )
    # 哈希一致就是正常命中，无论是否带证据。
    assert not stages._cached_leaf_superseded_by_feedback(
        cached_source_hash="same", source_hash="same",
        source_payload=payload_with_feedback,
    )
