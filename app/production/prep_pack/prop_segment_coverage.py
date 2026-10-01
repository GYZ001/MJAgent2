"""道具段号全文补全（2026-10-01，用户反馈「分镜台道具大多没图」三路只读调查第③
项）：``discovery._prep_pack_build_prop_manifest`` 只把 ``segment_indexes`` 记到
模型这次申报并通过锚定核验的那些段落——同一件道具若在本集其它段落的原文里还会
再次出现（例如手机在原文第 7/16/20/24/54 段反复被人物操作），manifest 不会自动
把那些段落并进来，因为模型抽取是按 chunk 分批进行的，没有任何一次调用能看到整集
全文。这里做纯确定性（零语义、不调模型）的二次检索：对 manifest 里每条道具，
在本集全部原文段落里重新找一遍它「已经被核验过的原文写法」，把新命中的段号并入
``segment_indexes``；``provenance``（anchor_segments/anchor_phrase）保持调用方
已经算好的锚点不变——这一步只扩展"这件道具在哪些段落出场"的覆盖面，不改变"这条
申报凭什么立住"的证据。

是否正式建卡（``app.props.judge.is_key_prop_mention`` 的四选一判据）完全不受这里
影响：段号补全发生在建卡判定之前的 manifest 阶段，补全后 segment_indexes 变多只是
让判据能看到道具真实的出场频次，取舍仍然交给 judge 自己算。
"""
from __future__ import annotations

from typing import Any, Sequence

from app.schemas import Prop
from app.source_excerpt import SourceSegment


def _prop_coverage_retrieval_words(
    entry: dict[str, Any], declared_text: str, cards_by_name: dict[str, Prop],
) -> list[str]:
    """候选检索词：这条道具的 ``label``、``source_wording``，以及——道具已经
    绑定既有卡（``canonical_name`` 非空）时——那张卡自己的 ``name`` 与每一个
    ``alias``。但只保留「本身已经在模型申报并通过锚定核验的段落（``entry
    ["segment_indexes"]`` 对应的原文拼接，即 ``declared_text``）里逐字命中过」
    的那些，其余一律丢弃，不参与下面的全集检索。

    不设最短长度魔数（数据推导，不是拍脑袋）：一个词只要已经在已核验段落里逐字
    命中过，就说明它在这段上下文里已经被证明确实指这件道具——继续拿它去全集
    检索是在复用已核验的证据，不是放行任意短词模糊匹配。真正会引发误命中的是
    「从未在任何已核验段落出现过」的词，这条过滤已经把它们排除在外；长度本身
    不是风险来源的充分或必要条件——例如道具卡别名"伞"（单字）若确实在已核验
    段落命中过，排除它不会减少误报，只会把这件道具真实的其它出场段落漏掉；反过来
    "一支缠着细银丝的木簪"（十个字）如果连在已核验段落里都没命中过，再长也不该
    被信任为检索词。"""
    candidates = [
        str(entry.get("label") or "").strip(),
        str(entry.get("source_wording") or "").strip(),
    ]
    canonical_name = entry.get("canonical_name")
    card = cards_by_name.get(canonical_name) if canonical_name else None
    if card is not None:
        candidates.append(str(card.name or "").strip())
        candidates.extend(str(alias or "").strip() for alias in card.aliases)
    # dict.fromkeys 去重且保序，比 set 更适合这里——后面只用于迭代，顺序无关
    # 紧要，但避免引入 set 遍历序不确定性这类已经在本仓库踩过的坑（参见
    # app.stages 说话人判定的历史教训），习惯性用保序去重。
    return [word for word in dict.fromkeys(candidates) if word and word in declared_text]


def fill_prop_segment_coverage(
    props_payload: list[dict[str, Any]],
    segments: list[SourceSegment],
    *,
    cards: Sequence[Prop] = (),
) -> list[dict[str, Any]]:
    """manifest 建好之后的确定性二次检索（完整说明见模块 docstring）。

    ``segments`` 必须是本集全部原文段落（下标 1-based，与 segment_indexes
    同一编号体系）——不能只传某个 chunk 的子集，否则「本集其它段落是否再次
    出现」这条判据无从谈起。原地更新每条 entry 的 ``segment_indexes`` 并整体
    返回 ``props_payload``（不改 label/description/provenance 等其它任何
    字段，不增删条目）。"""
    cards_by_name = {str(card.name): card for card in cards if card.name}
    for entry in props_payload:
        declared_indexes = [
            index for index in entry.get("segment_indexes") or []
            if 1 <= index <= len(segments)
        ]
        declared_text = "\n".join(segments[index - 1].text for index in declared_indexes)
        words = _prop_coverage_retrieval_words(entry, declared_text, cards_by_name)
        if not words:
            continue
        hits = {
            index for index, segment in enumerate(segments, start=1)
            if any(word in segment.text for word in words)
        }
        entry["segment_indexes"] = sorted(set(declared_indexes) | hits)
    return props_payload
