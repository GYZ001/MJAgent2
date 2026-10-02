"""道具复核新发现的独立自查（2.0.18 第二处，从 ``prop_recheck.py`` 拆出）。

``prop_recheck.py`` 的否决权只审查"抽取已经申报"的条目（``declaration_
vetoes``），但复核自己独立重新扫描原文时新发现的候选（``_prop_recheck_
added`` 的结果）是另一批完全没有被审查过的申报——真实回归（B 沙箱两轮
验证第1轮当场复现）：本集这次重跑抽取调用干脆一件道具都没报，复核自己
却把"浅灰色卫衣"当新发现报了出来，自己给的 plot_significant_quote 就写着
"套了一件浅灰色卫衣"，跟真实事故同一处原文、同一类自我拆台的证据，只是
这次是复核自己犯的错，不是抽取犯的错转给复核去挡。两个调用面对的是同一份
``_PROP_SEGMENT_CRITERIA``，谁都不能免于出这个错——复核能查别人的错，
不代表查自己的错会更准，必须有独立的第二次审视。

修法对称：把复核自己新发现的候选再喂给同一个模型做一次独立自查，复用
``prop_veto.py`` 的同一套结构判据（``_PropDeclarationVeto`` schema、
``_prop_veto_is_grounded`` 核验函数），不另造一套判据或口径；自查调用
失败照样全部保留，不静默删。

同一件实物两条不一致裁决的真实回归（第二轮真实验证发现）：复核独立重新
扫描原文时，有时会把抽取已经申报过的同一件实物又报一遍（结构正常——复核
本来就不知道抽取报过什么，也不要求它回避重复，判重交给代码），如果这份
"重复的新发现"被这里的自查正确否决，但 ``declaration_vetoes`` 那边没有
对同一件实物的原始申报给出否决（模型在同一次调用里对同一件实物给出了
两个不一致的结论——真实案例"毛线围巾"/"手机"），光删掉这份重复的新发现
不够，原始申报会原样存活，日志打了否决信号但没有真的拦住。
``_propagate_self_check_vetoes_to_declared`` 补上这个缺口：自查否决的
候选如果和 ``declared_props`` 里某条原始申报结构匹配（同 ``_prop_
mention_signature``），同一个否决结论也要套用到那条原始申报，重新跑一遍
核验（不直接照搬，两份实例的 segment_indexes 不一定完全相同）。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.source_excerpt import SourceSegment

from .chunk_extraction import _PROP_NON_DECLARE_CONDITIONS, _PROP_SEGMENT_CRITERIA
from .chunking import _render_chunk
from .model_call import _call_structured
from .prop_veto import (
    _PropDeclarationVeto,
    _prop_mention_signature,
    _prop_veto_is_grounded,
    _render_declared_props,
    _VETO_LOG_PREFIX,
)
from .schemas import _ModelPropMention

log = logging.getLogger(__name__)


class _PropAdditionConfirmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vetoes: list[_PropDeclarationVeto] = []


def _addition_confirm_prompt(rendered: str, candidates: list[_ModelPropMention]) -> str:
    return f"""你刚才在复核时新报出了下面这些道具候选——这是独立的第二次自查：逐条裁决
这条候选是否其实落入了判据里明文写出的"不申报情形"。判据①②（被操作、或本集再次
出现）是满足其一即可的正面申报条件（硬性，与之前完全一致：{_PROP_SEGMENT_CRITERIA}）；
{_PROP_NON_DECLARE_CONDITIONS}——否决一条候选必须确认它落入这两种情形之一，不能
仅凭"这件物品本集只出现一次"或"没看到明确的操作动作"就单独否决，因为另一条判据
仍可能独立成立；只有真的确认落入上面某一种不申报情形才撤销，拿不准就不要否决，
没有查到就不用列任何条目。

候选清单：
{_render_declared_props(candidates)}

需要撤销的条目，在 vetoes 里每条给 {{"declared_index": 上面对应的编号, "criterion":
"引用判据原文措辞说明落入的是哪一种不申报情形——正穿着未脱下的衣物，还是背景
陈设一笔带过、没有任何角色与它互动、且全文只出现这一次（三个条件同时成立）；
按判据原文逐字复述，不要自己加「核心角色」「主要角色」「主角」这类判据原文没有的限定词
去缩小「任何角色」的范围——群演、路人、功能性人物同样算角色，原文写了有人拿着/使用/
操作这件东西就不是「没有任何角色与它互动」", "evidence_quote":
"从这条候选自己的 segment_indexes 任一编号原文中逐字摘录一段原文，要能证明确实
落入该情形，绝不编造"}}；没问题的不用列。

原文：
{rendered}
"""


async def _confirm_additions(
    *, episode_id: str, chunk_index: int, chunk: list[tuple[int, SourceSegment]],
    run_id: str | None, candidates: list[dict[str, Any]],
    chunk_by_index: dict[int, SourceSegment],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """新发现独立自查（见本模块顶部大注释）：``candidates`` 是复核自己刚
    报出的新发现（``prop_recheck._prop_recheck_added`` 的结果），不是抽取
    申报的。没有候选、调用失败都原样返回全部候选+空否决列表，不静默删
    （同既有"失败不删"纪律）。

    返回 ``(kept, vetoed)``：``vetoed`` 是核验通过、真的被摘除的记录
    （``{"mention": _ModelPropMention, "criterion": str, "evidence_quote":
    str}``），供调用方核对这些"新发现"是不是恰好是抽取已经申报过的同一件
    实物——是的话这个否决结论要同步套用到那条原始申报，见
    ``_propagate_self_check_vetoes_to_declared``。"""
    if not candidates:
        return candidates, []
    mentions = [_ModelPropMention(**item) for item in candidates]
    try:
        response = await _call_structured(
            run_id=run_id,
            step_key="episode_prep_pack_prop_recheck_addition_confirm",
            iteration_no=chunk_index,
            prompt=_addition_confirm_prompt(_render_chunk(chunk), mentions),
            model_type=_PropAdditionConfirmResponse,
            schema_name="episode_prep_pack_prop_recheck_addition_confirm_v1",
            operation_id=(
                f"episode_prep_pack:{episode_id}:prop_recheck_addition_confirm:{chunk_index}"
            ),
            max_tokens=2000,
            call_meta={
                "stage_key": "episode_prep_pack_prop_recheck_addition_confirm",
                "episode_id": episode_id,
                "chunk_index": chunk_index,
            },
        )
    except Exception:  # noqa: BLE001 - 自查失败不阻断，候选原样保留
        log.warning("道具复核新发现自查失败，候选原样保留 episode=%s chunk=%s",
                    episode_id, chunk_index, exc_info=True)
        return candidates, []
    vetoed_at: set[int] = set()
    vetoed: list[dict[str, Any]] = []
    for verdict in response.vetoes:
        index = verdict.declared_index
        if not (1 <= index <= len(mentions)):
            continue
        quote = str(verdict.evidence_quote or "").strip()
        if not _prop_veto_is_grounded(mentions[index - 1], quote, chunk_by_index):
            continue
        vetoed_at.add(index)
        criterion = str(verdict.criterion or "").strip()
        vetoed.append({"mention": mentions[index - 1], "criterion": criterion, "evidence_quote": quote})
        log.warning(
            "%s episode=%s chunk=%s label=「%s」判据=%s 证据=「%s」（新发现自查）",
            _VETO_LOG_PREFIX, episode_id, chunk_index, mentions[index - 1].label,
            criterion, quote,
        )
    kept = [c for i, c in enumerate(candidates, start=1) if i not in vetoed_at]
    return kept, vetoed


def _propagate_self_check_vetoes_to_declared(
    vetoed: list[dict[str, Any]], declared_props: list[_ModelPropMention],
    chunk_by_index: dict[int, SourceSegment],
) -> list[dict[str, Any]]:
    """同一件实物两条不一致裁决的修法（见本模块顶部大注释完整案情）：
    按 label/known_prop_name/source_wording 结构判据（``_prop_mention_
    signature``）匹配到 ``declared_props`` 里同一件实物时，用同一份证据句
    对那条原始申报重新跑一次 ``_prop_veto_is_grounded``（不同实例可能
    segment_indexes 不完全相同，不假定核验结果必然一致，照样过一遍结构
    核验），核验通过才计入否决，不直接照搬复核那边的核验结论。"""
    results: list[dict[str, Any]] = []
    for record in vetoed:
        signature = _prop_mention_signature(record["mention"])
        for item in declared_props:
            if not (signature & _prop_mention_signature(item)):
                continue
            results.append({
                "item": item,
                "grounded": _prop_veto_is_grounded(item, record["evidence_quote"], chunk_by_index),
                "criterion": record["criterion"],
                "evidence_quote": record["evidence_quote"],
            })
    return results
