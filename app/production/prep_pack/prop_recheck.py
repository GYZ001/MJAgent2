"""道具复核：chunk 抽取之后，专门再问一次「这一段原文里还有哪些道具被操作/
反复出现」（2026-10-01，PREP_PACK_VERSION 2.0.10）。

为什么单开一次调用，而不是继续加强 chunk 抽取的提示词：抽取那一次要同时报
characters/scenes/props，道具只是其中一项，被已登记道具名单（known_props）
牵着走的实测很明确——用 2.0.9 重跑我欲封天系列 EP1 映射，模型报的 8 件道具
里 7 件是物件库已有卡，原文里被角色操作、本集多段出现的脸盆（段3/7）、电闸
（段6/7）、勺子（段56/59）、顾屿的大衣（段12/34）、黑鸟振翅意象（段22/
59-60）一个都没报。这正是 scene_recheck.py 模块文档记的「单次长 chunk 调用
会漏掉整个类别」同一形状——本仓库已经有先例（见该模块 docstring），这里原样
复用它的调用时机/提示词结构/结果合并/可见信号/测试写法，只把素材类型换成
道具。

判据原文单源自 ``.chunk_extraction._PROP_SEGMENT_CRITERIA``（2.0.9 引入的
两条正面条件），不在这里复制一份措辞——两处用词漂移是真实踩过的坑（场景侧
措辞摇摆导致命中率波动，见 scene_recheck.py docstring），道具侧没有理由
重蹈。

合并规则：抽取已经报过的道具（按 label/known_prop_name/source_wording 任一
相同判重，结构判据，不枚举具体名字）不重复，新出现的追加进 response.props，
之后走原有 ``_prep_pack_build_prop_manifest`` 核验/建卡/段号补全全链路——
复核产出的仍只是"提及"，不绕过任何既有锚定/建卡判据。

复核失败只记可见告警，不阻断映射（同 scene_recheck.attach_scene_recheck 的
处置）。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.source_excerpt import SourceSegment

from .chunk_extraction import _PROP_SEGMENT_CRITERIA
from .chunking import _prep_pack_gate_segment_indexes, _render_chunk
from .model_call import _call_structured
from .schemas import _ModelPropMention

log = logging.getLogger(__name__)


class _PropRecheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    props: list[_ModelPropMention]


def _prompt(rendered: str, known_props: list[str]) -> str:
    return f"""你在给一集短剧的道具做复核标注：原文已经按编号分段列在下面，之前有一次
抽取调用申报过这段的人物/场景/道具，但道具只是其中一类，容易被已登记名单牵着走而漏
报整类物件。本次只列道具，逐段扫一遍原文，把每个被角色拿起/放下/递交/使用/开关/穿脱
的物件和在多段反复出现的视觉线索都列出来——这是一次独立标注，不用管抽取那次报过
什么，也不用回避重复，重复由代码去重，你只管把看到的都报出来。

判据（硬性，与抽取那次完全一致，不要加宽也不要收紧）：{_PROP_SEGMENT_CRITERIA}

已登记道具名（仅供拼写对齐——如果原文本身就是这样称呼这件道具的，写法要跟登记名
保持一致；原文没有这样称呼，就不要往上面靠）：{known_props}

每条给 {{"label": "道具名称", "description": "这个道具的外观/特征简述",
"segment_indexes": [该道具实际出现的编号列表], "plot_significant": true/false,
"plot_significant_quote": "从上面 segment_indexes 任一编号原文中逐字摘录的一段原文
（不超过约40字），要能证明这件物品在这段剧情里被某个角色拿起/递给/接过/放下、被贴身
佩戴或收藏、被镜头意味着特写描写，或作为悬念/伏笔被原文特别强调其存在——不确定就填
空字符串，绝不编造", "source_wording": "这件道具在 segment_indexes 所指原文里的称呼，
从原文逐字复制的一段连续文字；不改字、不增字、不拼接；确实没有可摘录的原文称呼就填
空字符串，绝不编造；不用物件库里的登记名替代，登记名只填进 known_prop_name",
"known_prop_name": "这件道具如果就是已登记道具名单中的某一件（同一件实物，不只是同类
或名字相近的东西），从名单里逐字复制那个名字；原文里这是另一件东西、或名单里没有它，
填空字符串，绝不硬凑一个名字相近的名单条目"}}。

判断不了就不报，没有新发现就给空列表，不要为了填满而虚构。

原文：
{rendered}
"""


async def recheck_chunk_props(
    *,
    episode_id: str,
    chunk_index: int,
    chunk: list[tuple[int, SourceSegment]],
    known_props: list[str],
    run_id: str | None,
) -> list[dict[str, Any]]:
    """返回本 chunk 复核到的道具提及（原始 dict 形状，同 chunk_extraction 的
    props mention）；没有发现时返回空列表。判重/合并交给调用方
    attach_prop_recheck，这里只负责"模型这次看到了哪些道具"并过段号结构闸。
    """
    chunk_global_indexes = {index for index, _segment in chunk}
    chunk_by_index = {index: segment for index, segment in chunk}
    response = await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_prop_recheck",
        iteration_no=chunk_index,
        prompt=_prompt(_render_chunk(chunk), known_props),
        model_type=_PropRecheckResponse,
        schema_name="episode_prep_pack_prop_recheck_v1",
        operation_id=f"episode_prep_pack:{episode_id}:prop_recheck:{chunk_index}",
        max_tokens=4000,
        call_meta={
            "stage_key": "episode_prep_pack_prop_recheck",
            "episode_id": episode_id,
            "chunk_index": chunk_index,
        },
    )
    added: list[dict[str, Any]] = []
    for mention in response.props:
        label = str(mention.label or "").strip()
        if not label:
            continue
        valid = _prep_pack_gate_segment_indexes(
            label, mention.segment_indexes, chunk_global_indexes, chunk_by_index,
        )
        if not valid:
            continue
        added.append({
            "label": label,
            "description": str(mention.description or "").strip(),
            "segment_indexes": valid,
            "plot_significant": bool(mention.plot_significant),
            "plot_significant_quote": str(mention.plot_significant_quote or "").strip(),
            "source_wording": str(mention.source_wording or "").strip(),
            "known_prop_name": str(mention.known_prop_name or "").strip(),
        })
    return added


def _prop_mention_signature(mention: Any) -> set[str]:
    """判重用的候选字符串集合：label/known_prop_name/source_wording 三者
    的非空值（结构判据，不枚举具体道具名）。``mention`` 既可能是 dict
    （recheck 候选），也可能是 ``_ModelPropMention``（已有抽取结果）。"""
    get = mention.get if isinstance(mention, dict) else lambda k: getattr(mention, k, None)
    return {
        str(get(key) or "").strip()
        for key in ("label", "known_prop_name", "source_wording")
        if str(get(key) or "").strip()
    }


async def attach_prop_recheck(
    response: Any,
    *,
    chunk: list[tuple[int, SourceSegment]],
    chunk_index: int,
    episode_id: str,
    known_props: list[str],
    run_id: str | None,
) -> Any:
    """就地把复核新发现的道具追加进 ``response.props``，返回同一个 response。

    复核是补漏增强，不是门禁：失败不能把整个映射包拖垮，所以吞掉异常只记一条
    warning，原样交回抽取结果（同 scene_recheck.attach_scene_recheck 的处置）。
    """
    try:
        added = await recheck_chunk_props(
            episode_id=episode_id, chunk_index=chunk_index, chunk=chunk,
            known_props=known_props, run_id=run_id,
        )
    except Exception:  # noqa: BLE001 - 补漏失败不阻断主流程
        log.warning("道具复核失败，本 chunk 沿用抽取结果 episode=%s chunk=%s",
                    episode_id, chunk_index, exc_info=True)
        return response
    if not added:
        return response
    known_signatures: set[str] = set()
    for mention in response.props:
        known_signatures |= _prop_mention_signature(mention)
    gained = 0
    for item in added:
        signature = _prop_mention_signature(item)
        if signature & known_signatures:
            continue
        response.props.append(_ModelPropMention(**item))
        known_signatures |= signature
        gained += 1
    if gained:
        log.info("道具复核补回 %d 件新道具 episode=%s chunk=%s", gained, episode_id, chunk_index)
    return response
