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

否决权（2.0.18，真实案例 proj_ca86b15ab7d7 顾念长安第2集，生产用 2.0.17
第三次重跑 run_21445d406ecb）：B 上只读查 provider_calls 坐实——chunk 抽取
（id 82339）把温念正穿着的三件衣物（浅灰色卫衣/米白针织开衫/浅蓝碎花长裙）
申报成了道具，known_prop_name 提名了已有的旧衣服卡，plot_significant_quote
自己写的就是「套了一件浅灰色卫衣」「温念仍是那件米白针织开衫和浅蓝碎花
长裙」这类明确的穿着句；紧接着的道具复核（id 82341）reasoning 里已经正确
判断「穿在身上的不算」，但复核此前的职责只是"并集补充"——只会往
response.props 里加东西，永远删不掉抽取已经报出的误判。沙箱三轮重跑都没
复现，说明 2.0.13 在 ``_PROP_SEGMENT_CRITERIA`` 里加的那句"人物身上正穿着
的衣物…不作为道具申报"只是降低了误报概率，没有消除——提示词是概率手段，
真正落地要靠下游一道能够反悔的闸。

修法：复核调用本来就要重新看一遍本块原文，顺带把抽取已经申报的本块道具
清单（label/description/plot_significant_quote/source_wording/
segment_indexes）交给它，schema 新增 ``declaration_vetoes``——模型只需要
列出它认为落入判据里明文写出的"不申报情形"的条目（见下方"否决提示词的
真实教训"一节），对每条给出判据说明与一段从这条申报自己的段落原文逐字
摘录的证据句；没问题的条目不用列，默认保留（模型提名，不是模型决定）。
代码侧二次核验（``.prop_veto._prop_veto_is_grounded``）：证据句必须逐字
出自该道具自己申报的某个 segment_index 的原文、且必须包含该道具的 label
或 source_wording——两者都满足，核验通过，才真的从 ``response.props``
里摘除这条申报（打 ``[PREP_PACK_PROP_DECLARATION_VETOED]``）；证据缺失/
不是逐字/不含标签，原样保留申报（打 ``[PREP_PACK_PROP_VETO_UNGROUNDED]``），
绝不静默删。复核调用本身失败/返回不完整时跟既有补漏逻辑同一处置：吞异常
只记告警，这一 chunk 的申报原样不动——宁可多一次误报留给后续人工核查，
也不因为复核这道增强链路本身出问题就连累已经成立的申报被错误删除。

新发现同样需要复核、同一件实物两条不一致裁决都需要另一道闸兜底——这两处
后续修法拆到 ``.prop_recheck_addition_confirm`` 模块（2.0.18 第二/四处，
见该模块 docstring 完整案情），不在这里重复。

否决提示词的真实教训（B 沙箱多轮验证当场复现，完整演化过程）：第一版
否决提示词只说"列出不满足判据的条目"，真实模型响应把"①②任选其一即可"
误解成"必须同时满足"，把 15 件里 13 件真实道具（包括明显有操作动作的
勺子/双人自行车/顾屿外套等）都以"本集只出现一次、不满足贯穿性"为由
否决——评估判据的证据句本身逐字为真、也确实包含 label，``_prop_veto_
is_grounded`` 的结构核验对这种"证据真实但推理错误"的情形完全无感（它只
核验证据有没有被编造，不核验否决的推理本身对不对，这是语义判断，结构
判据做不到）。真正的修法不在代码侧加更多结构核验（语义对错不是能从数据
结构推出来的），而在提示词：把否决的问法从"有没有不满足判据的条目"这种
开放式再审查，收窄成"有没有正穿着被误判成道具"这一种具体误判——协调方
复核指出这个收窄本身又是只堵一种写法（CLAUDE.md"Prompts"一节：写完整
正面陈述，不写禁令/不只堵一种情形），判据里明文写的不申报情形其实有
两种：①正穿着的衣物②背景陈设一笔带过+无人互动+全文只出现一次。最终
修法：否决只裁决"这条申报是否落入判据里明文写出的不申报情形"——两种
情形单源抽成 ``.chunk_extraction._PROP_NON_DECLARE_CONDITIONS`` 常量
（与 ``_PROP_SEGMENT_CRITERIA`` 正文共用，字面逐字不变，不是改写出第二份
措辞），否决提示词直接引用这个常量而不是自己转述，并写清满足①或②任一
条申报理由就不能因"只出现一次"或"没被操作"单独否决——否决理由必须是
落入 ``_PROP_NON_DECLARE_CONDITIONS`` 里的某一种情形，``criterion`` 字段
填是哪一种。代码侧核验不变：它只核验证据是否逐字出自原文、是否点名这件
道具，语义判断交给提示词与模型，不是结构判据能替代的范围。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.source_excerpt import SourceSegment

from .chunk_extraction import _KNOWN_PROP_NAME_FIELD_RULE, _PROP_NON_DECLARE_CONDITIONS, _PROP_SEGMENT_CRITERIA
from .chunking import _prep_pack_gate_segment_indexes, _render_chunk
from .model_call import _call_structured
from .prop_recheck_addition_confirm import _confirm_additions, _propagate_self_check_vetoes_to_declared
from .prop_veto import (
    _PropDeclarationVeto,
    _prop_mention_signature,
    _prop_veto_is_grounded,
    _render_declared_props,
    _VETO_LOG_PREFIX,
    _VETO_UNGROUNDED_LOG_PREFIX,
)
from .schemas import _ModelPropMention

log = logging.getLogger(__name__)


class _PropRecheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    props: list[_ModelPropMention]
    declaration_vetoes: list[_PropDeclarationVeto] = []


def _prompt(
    rendered: str, known_props: list[str], declared_props: list[_ModelPropMention],
) -> str:
    veto_section = ""
    if declared_props:
        veto_section = f"""

上一次抽取已经按下面编号申报过这些道具，这次复核顺带对每一条单独裁决：
这条申报是否其实落入了判据里明文写出的"不申报情形"。判据①②（被操作、
或本集再次出现）是满足其一即可的正面申报条件，{_PROP_NON_DECLARE_CONDITIONS}
——否决一条申报必须确认它落入这两种情形之一，不能仅凭"这件物品本集只
出现一次"或"没看到明确的操作动作"就单独否决，因为另一条判据仍可能独立
成立；只有真的确认落入上面某一种不申报情形才撤销，没问题的条目不用列，
默认保留，不要为了否决而否决，拿不准就不要否决：
{_render_declared_props(declared_props)}

需要撤销的条目，在 declaration_vetoes 里每条给 {{"declared_index": 上面
对应的编号, "criterion": "引用判据原文措辞说明落入的是哪一种不申报情形
——正穿着未脱下的衣物，还是背景陈设一笔带过、没有任何角色与它互动、且
全文只出现这一次（三个条件同时成立）；按判据原文逐字复述，不要自己加
「核心角色」「主要角色」「主角」这类判据原文没有的限定词去缩小「任何
角色」的范围——群演、路人、功能性人物同样算角色，原文写了有人拿着/
使用/操作这件东西就不是「没有任何角色与它互动」", "evidence_quote":
"从这条申报自己的 segment_indexes 任一编号原文中逐字摘录一段原文，要能
证明确实落入该情形，不确定就不要列这条，绝不编造"}}。"""
    return f"""你在给一集短剧的道具做复核标注：原文已经按编号分段列在下面，之前有一次
抽取调用申报过这段的人物/场景/道具，但道具只是其中一类，容易被已登记名单牵着走而漏
报整类物件。本次只列道具，逐段扫一遍原文，把每个被角色拿起/放下/递交/使用/开关/穿脱
的物件和在多段反复出现的视觉线索都列出来——这是一次独立标注，不用管抽取那次报过
什么，也不用回避重复，重复由代码去重，你只管把看到的都报出来。

判据（硬性，与抽取那次完全一致，不要加宽也不要收紧）：{_PROP_SEGMENT_CRITERIA}

已登记道具名单（每条含名称/别名/外观特征，部分还附这张卡在更早集数里「此前出场」的
归属证据；外观与此前出场仅供核对「是不是同一件实物」，见下面 known_prop_name 的说明；
名称/别名仅供拼写对齐——如果原文本身就是这样称呼这件道具的，写法要跟登记名保持一致；
原文没有这样称呼，就不要往上面靠）：{known_props}

每条给 {{"label": "道具名称", "description": "这个道具的外观/特征简述",
"segment_indexes": [该道具实际出现的编号列表], "plot_significant": true/false,
"plot_significant_quote": "从上面 segment_indexes 任一编号原文中逐字摘录的一段原文
（不超过约40字），要能证明这件物品在这段剧情里被某个角色拿起/递给/接过/放下、被贴身
佩戴或收藏、被镜头意味着特写描写，或作为悬念/伏笔被原文特别强调其存在——不确定就填
空字符串，绝不编造", "source_wording": "这件道具在 segment_indexes 所指原文里的称呼，
从原文逐字复制的一段连续文字；不改字、不增字、不拼接；确实没有可摘录的原文称呼就填
空字符串，绝不编造；不用物件库里的登记名替代，登记名只填进 known_prop_name",
"known_prop_name": "{_KNOWN_PROP_NAME_FIELD_RULE}"}}。

判断不了就不报，没有新发现就给空列表，不要为了填满而虚构。{veto_section}

原文：
{rendered}
"""


def _prop_recheck_added(
    mentions: list[_ModelPropMention], chunk_global_indexes: set[int],
    chunk_by_index: dict[int, SourceSegment],
) -> list[dict[str, Any]]:
    """补漏候选：复核新报出的道具，过段号结构闸（同既有逻辑，未改动语义，
    只是从 ``recheck_chunk_props`` 里拆出来给否决腾函数行数预算）。"""
    added: list[dict[str, Any]] = []
    for mention in mentions:
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


def _prop_recheck_vetoes(
    verdicts: list[_PropDeclarationVeto], declared_props: list[_ModelPropMention],
    chunk_by_index: dict[int, SourceSegment],
) -> list[dict[str, Any]]:
    """否决候选：序号越界（协议之外/凭空编造）的一律丢弃，不当成任何一条
    申报的否决；序号合法的逐条跑二次核验，核验结果原样带回（``grounded``
    字段），真正的摘除/日志由调用方 ``_apply_prop_vetoes`` 统一处理。"""
    results: list[dict[str, Any]] = []
    for verdict in verdicts:
        index = verdict.declared_index
        if not (1 <= index <= len(declared_props)):
            continue
        item = declared_props[index - 1]
        quote = str(verdict.evidence_quote or "").strip()
        results.append({
            "item": item,
            "grounded": _prop_veto_is_grounded(item, quote, chunk_by_index),
            "criterion": str(verdict.criterion or "").strip(),
            "evidence_quote": quote,
        })
    return results


async def recheck_chunk_props(
    *,
    episode_id: str,
    chunk_index: int,
    chunk: list[tuple[int, SourceSegment]],
    known_props: list[str],
    run_id: str | None,
    declared_props: list[_ModelPropMention],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 ``(added, vetoes)``：``added`` 是本 chunk 复核到的新道具提及
    （原始 dict 形状，同 chunk_extraction 的 props mention），已经过新发现
    独立自查（``.prop_recheck_addition_confirm._confirm_additions``）；
    ``vetoes`` 是对 ``declared_props``（抽取已申报、调用方传入的本 chunk
    快照）逐条裁决、已完成代码侧核验的否决候选列表（见 ``_prop_recheck_
    vetoes``/``_prop_veto_is_grounded``），外加新发现自查否决、且与某条
    原始申报结构匹配时同步传播过来的否决（见 ``_propagate_self_check_
    vetoes_to_declared``）。判重/合并/摘除交给调用方 ``attach_prop_
    recheck``，这里只负责"模型这次看到了什么、裁决了什么、新发现自查完了
    没有"并过结构闸。"""
    chunk_global_indexes = {index for index, _segment in chunk}
    chunk_by_index = {index: segment for index, segment in chunk}
    response = await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_prop_recheck",
        iteration_no=chunk_index,
        prompt=_prompt(_render_chunk(chunk), known_props, declared_props),
        model_type=_PropRecheckResponse,
        schema_name="episode_prep_pack_prop_recheck_v2",
        operation_id=f"episode_prep_pack:{episode_id}:prop_recheck:{chunk_index}",
        max_tokens=4000,
        call_meta={
            "stage_key": "episode_prep_pack_prop_recheck",
            "episode_id": episode_id,
            "chunk_index": chunk_index,
        },
    )
    added = _prop_recheck_added(response.props, chunk_global_indexes, chunk_by_index)
    added, self_check_vetoed = await _confirm_additions(
        episode_id=episode_id, chunk_index=chunk_index, chunk=chunk, run_id=run_id,
        candidates=added, chunk_by_index=chunk_by_index,
    )
    vetoes = _prop_recheck_vetoes(response.declaration_vetoes, declared_props, chunk_by_index)
    vetoes += _propagate_self_check_vetoes_to_declared(
        self_check_vetoed, declared_props, chunk_by_index,
    )
    return added, vetoes


def _apply_prop_vetoes(
    response: Any, vetoes: list[dict[str, Any]], *, episode_id: str, chunk_index: int,
) -> None:
    """就地从 ``response.props`` 摘除核验通过的否决；核验不过的否决原样
    保留申报，只打可见信号——复核证据不实/缺失都不许静默删（见本模块顶部
    2.0.18 大注释）。"""
    vetoed_ids = {id(v["item"]) for v in vetoes if v["grounded"]}
    for veto in vetoes:
        item = veto["item"]
        if veto["grounded"]:
            log.warning(
                "%s episode=%s chunk=%s label=「%s」判据=%s 证据=「%s」",
                _VETO_LOG_PREFIX, episode_id, chunk_index, item.label,
                veto["criterion"], veto["evidence_quote"],
            )
        else:
            log.warning(
                "%s episode=%s chunk=%s label=「%s」给出的否决证据未核验通过"
                "（需逐字出自该道具自己申报的段落、且包含 label 或"
                " source_wording），保留原申报",
                _VETO_UNGROUNDED_LOG_PREFIX, episode_id, chunk_index, item.label,
            )
    if vetoed_ids:
        response.props = [p for p in response.props if id(p) not in vetoed_ids]


async def attach_prop_recheck(
    response: Any,
    *,
    chunk: list[tuple[int, SourceSegment]],
    chunk_index: int,
    episode_id: str,
    known_props: list[str],
    run_id: str | None,
) -> Any:
    """就地把复核新发现的道具追加进 ``response.props``、把核验通过的否决
    摘除出去，返回同一个 response。

    复核是补漏增强+否决增强，不是门禁：失败不能把整个映射包拖垮，所以吞掉
    异常只记一条 warning，原样交回抽取结果，既不补漏也不否决（同
    scene_recheck.attach_scene_recheck 的处置，2.0.18 起否决同样遵守这条
    "失败不删"纪律）。
    """
    declared_snapshot = list(response.props)
    try:
        added, vetoes = await recheck_chunk_props(
            episode_id=episode_id, chunk_index=chunk_index, chunk=chunk,
            known_props=known_props, run_id=run_id, declared_props=declared_snapshot,
        )
    except Exception:  # noqa: BLE001 - 补漏/否决失败不阻断主流程
        log.warning("道具复核失败，本 chunk 沿用抽取结果 episode=%s chunk=%s",
                    episode_id, chunk_index, exc_info=True)
        return response
    _apply_prop_vetoes(response, vetoes, episode_id=episode_id, chunk_index=chunk_index)
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
