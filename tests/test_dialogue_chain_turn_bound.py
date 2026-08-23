"""话轮上限必须由共享归一化器保证，且按「发言」而不是按「片段」切分。

生产根因 R8（EP4，2026-08-22 23:44）：`DIALOGUE_CHAIN_LENGTH_INVALID`
自动修复耗尽（启动 1 轮、实际应用 0 个补丁），整集停在 WAITING_HUMAN。

三条叠加缺陷：

R8-1 **度量单位错了**。一句台词被 `_split_spoken_line` 按单镜口播容量
   （`MAX_SPOKEN_CHARS_PER_SHOT`=36）**有意**切成多段，每段登记成独立话轮——
   这是 `DIALOGUE_TURN_CAPACITY_EXCEEDED` 这条硬门禁**要求**的。
   而 `DIALOGUE_CHAIN_LENGTH_INVALID` 数的是「1~8 个连续**话轮**」，
   话轮是语义概念。全库 233 条 chain 里 52 条（22.3%）带这种虚高计数。
   两条门禁对足够长的单场对话**联合不可满足**：想满足容量就必须切碎，
   切碎就撞长度上限。因此**不能把片段合并回去**，只能多切几条 chain。

R8-2 **上限只有一个生产者在执行**。`DIALOGUE_CHAIN_TURNS_HARD_MAX` 原先只有
   `compile_screenplay_ir` 的分块循环真正执行，另外 7 处写 `chain.turns` 的
   地方都不检查。实测：把违规的 11 轮 chain 喂给共享归一化器，**原样返回**。
   而修复层没有任何策略能让 chain 变短，只有往里补话轮的
   `dialogue_chain_continuity`——规则又一次被放在唯一修不好它的那一层。

R8-3 **分块边界会切在一句话中间**。EP4 实测 DC3 末轮与 DC4 首轮、
   DC5 末轮与 DC6 首轮，都是同一说话人同一句话的片段。
"""
from __future__ import annotations

import pytest

from app.renderability import (
    DIALOGUE_CHAIN_TURNS_HARD_MAX,
    chunk_dialogue_turns,
    dialogue_turn_speech_acts,
)
from app.schemas import EpisodeScreenplay, KeyDialogueChain, KeyDialogueTurn
from app.validators import derive_key_lines, normalize_screenplay_dialogue_chains


def _turn(speaker: str, line: str, source_text: str, function: str = "statement"):
    return KeyDialogueTurn(
        speaker=speaker, line=line, source_text=source_text, function=function
    )


def _ep4_shaped_turns() -> list[KeyDialogueTurn]:
    """复刻 EP4 DC3 的真实形状：11 个片段 = 5 次发言（1/3/1/2/4）。"""
    spec = [("甲", "s1", 1), ("甲", "s2", 3), ("乙", "s3", 1),
            ("甲", "s4", 2), ("甲", "s5", 4)]
    turns: list[KeyDialogueTurn] = []
    for speaker, src, parts in spec:
        for part in range(parts):
            turns.append(_turn(speaker, f"{src}片段{part}", f"{src}完整句"))
    return turns


# ------------------------------------------------------------ 发言分组判据

def test_fragments_of_one_utterance_are_one_speech_act() -> None:
    acts = dialogue_turn_speech_acts(_ep4_shaped_turns())
    assert [len(a) for a in acts] == [1, 3, 1, 2, 4]


def test_same_speaker_different_source_is_two_speech_acts() -> None:
    """同一人连着说两句不同的话，是两次发言，不能并成一次。"""
    turns = [_turn("甲", "a", "句一"), _turn("甲", "b", "句二")]
    assert len(dialogue_turn_speech_acts(turns)) == 2


def test_empty_source_text_never_groups() -> None:
    """没有 source_text 就无法证明同源，保守地各自成一次发言。"""
    turns = [_turn("甲", "a", ""), _turn("甲", "b", "")]
    assert len(dialogue_turn_speech_acts(turns)) == 2


# ------------------------------------------------------------ 切分不入句中

def test_chunking_never_splits_a_speech_act() -> None:
    chunks = chunk_dialogue_turns(_ep4_shaped_turns())
    assert all(len(c) <= DIALOGUE_CHAIN_TURNS_HARD_MAX for c in chunks)
    for index in range(len(chunks) - 1):
        last, first = chunks[index][-1], chunks[index + 1][0]
        assert not (
            last.speaker == first.speaker
            and last.source_text == first.source_text
        ), "切在了一次发言中间"


def test_chunking_preserves_order_and_count() -> None:
    turns = _ep4_shaped_turns()
    flat = [t for chunk in chunk_dialogue_turns(turns) for t in chunk]
    assert flat == turns


def test_single_oversized_speech_act_still_terminates() -> None:
    """一次发言本身就超上限时退化为硬切——保证长度，不能死循环或不切。"""
    turns = [_turn("甲", f"片段{i}", "同一句") for i in range(11)]
    chunks = chunk_dialogue_turns(turns)
    assert [len(c) for c in chunks] == [8, 3]


def test_chunk_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        chunk_dialogue_turns([_turn("甲", "a", "x")], limit=0)


# ------------------------------------- 共享归一化器兜底（R8-2 的核心断言）

def _script_with_oversized_chain() -> EpisodeScreenplay:
    turns = _ep4_shaped_turns()
    script = EpisodeScreenplay(
        episode_no=4,
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1", scene_id="SC01",
                topic="精明男子吹捧铜镜并劝孟浩收下", turns=turns,
            )
        ],
        full_script_text="\n".join(f"{t.speaker}：{t.line}" for t in turns),
    )
    return script


def test_shared_normalizer_bounds_a_chain_no_producer_bounded() -> None:
    """核心断言：违规 chain 进来，归一化器必须自己把它切回上限内。

    改动前这里会原样返回 11 轮，一路走到硬门禁才被拒，
    而修复层没有任何策略能让它变短。
    """
    script = _script_with_oversized_chain()
    assert len(script.dialogue_chains[0].turns) > DIALOGUE_CHAIN_TURNS_HARD_MAX

    normalize_screenplay_dialogue_chains(script)

    lengths = [len(c.turns) for c in script.dialogue_chains]
    assert lengths, "chain 被切没了"
    assert max(lengths) <= DIALOGUE_CHAIN_TURNS_HARD_MAX, lengths


def test_bounding_preserves_key_lines_verbatim() -> None:
    """拆分只增加 chain 边界，不动话轮顺序，因此 KL## 编号必须逐字不变。

    这是选择「拆分」而不是「合并」的关键理由：合并会改写 key_lines，
    让已生成分镜里的 key_line_ids 全部错位。
    """
    script = _script_with_oversized_chain()
    before = derive_key_lines(script.dialogue_chains, script.full_script_text)

    normalize_screenplay_dialogue_chains(script)
    after = derive_key_lines(script.dialogue_chains, script.full_script_text)

    # 先证明真的拆了——否则 before == after 在 no-op 上恒真，等于没测。
    assert len(script.dialogue_chains) > 1
    assert before == after


def test_bounding_preserves_every_turn() -> None:
    script = _script_with_oversized_chain()
    before = [
        (t.speaker, t.line)
        for c in script.dialogue_chains for t in c.turns
    ]
    normalize_screenplay_dialogue_chains(script)
    after = [
        (t.speaker, t.line)
        for c in script.dialogue_chains for t in c.turns
    ]
    assert len(script.dialogue_chains) > 1, "没有发生拆分，本断言恒真"
    assert before == after


def test_bounding_keeps_chain_ids_unique() -> None:
    script = _script_with_oversized_chain()
    normalize_screenplay_dialogue_chains(script)
    ids = [c.chain_id for c in script.dialogue_chains]
    assert len(ids) > 1, "没有发生拆分，唯一性断言恒真"
    assert len(ids) == len(set(ids))


def test_continuation_chain_is_marked_and_does_not_start_with_response() -> None:
    script = _script_with_oversized_chain()
    script.dialogue_chains[0].turns[7].function = "response"

    normalize_screenplay_dialogue_chains(script)

    tails = script.dialogue_chains[1:]
    assert tails, "没有产生续链"
    assert all("（续）" in (c.topic or "") for c in tails)
    assert all(
        (c.turns[0].function or "") != "response" for c in tails if c.turns
    )


def test_already_bounded_chains_are_left_alone() -> None:
    """不得为了兜底而无差别重排——本来合规的输入必须原样通过。"""
    turns = [_turn("甲", f"line{i}", f"句{i}") for i in range(4)]
    script = EpisodeScreenplay(
        episode_no=4,
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1", scene_id="SC01", topic="一段短对话", turns=turns,
        )],
        full_script_text="\n".join(f"{t.speaker}：{t.line}" for t in turns),
    )
    normalize_screenplay_dialogue_chains(script)
    assert len(script.dialogue_chains) == 1
    assert [t.line for t in script.dialogue_chains[0].turns] == [
        f"line{i}" for i in range(4)
    ]
