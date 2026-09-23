"""场景复核：chunk 抽取之后，专门再问一次「这一段画面还拍到了哪些已登记场景」。

为什么单开一次调用，而不是继续加强 chunk 抽取的提示词（2026-09-17）：

抽取那一次要同时报 characters / scenes / props，场景只是其中一项，被动省略的那部分
天然容易被跳过。本仓 storyboard_pack 2.0.3 已经记过同一个形状的事故——「模型在单次
长 chunk 调用里只完整报出了最先出现的那个场景」。这次的实测同样指向它：龙猫出爪 EP6
段 14 是「【段 12｜人间·老街｜深夜】人物：无」的格局镜，正文写「从门口升到街道，整条街
只有医院玻璃门上一点微光」，画面确实拍到了医院门口。

先把 scenes 的定义从「角色实际所在的地点」放开到「画面所在的地点」（人物：无 不等于
没有场景），命中率从 **0/3** 升到 **1/3**——方向对了，但软约束到此为止，三次里还漏两次。
所以把「顺带报」改成「专门问」：单一职责的调用不跟别的任务抢注意力。仓库里
``/shots/{id}/identity-review`` 为「发声与群演」单开复核调用是同一个先例。

**合法值域用动态 model_type 的 Literal 钉死**：display_name 只能从本集已登记场景清单里
逐字取，细节见 ``response_model``。代码里的后置过滤（enum 之外一律丢弃）照旧保留，
两层都要有：schema 管住模型别产出，过滤管住真产出了也不落库。
已登记场景为空时直接跳过复核（空 enum 是非法 schema，那种情况本来也无从复核）。

**这里是一次独立标注，不是「补遗漏」**：2026-09-18 实测，把抽取已申报的清单塞给它看
（原文写法「人间·医院门口：[13]」）而候选清单是登记名（「晚安宠物医院门口」），模型合理
地认为这个地点已经报过、于是交回 ``{"scenes": []}``——它不是没看懂画面，是被两套名字
绕住了。抽取侧有时自己对齐到登记名、有时不会，命中率就跟着摇摆。改成独立回答「每段
机位在哪些已登记场景」之后没有这个歧义；重复由代码做并集消化，模型不必操心。

复核结果不覆盖抽取结果，只做并集：从不否定已申报的条目。
段号仍要过 ``_prep_pack_gate_segment_indexes`` 的结构闸，与抽取侧同一把尺子。
"""
from __future__ import annotations

import logging
from typing import Any

from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model

from app.source_excerpt import SourceSegment

from .chunking import _prep_pack_gate_segment_indexes, _render_chunk
from .model_call import _call_structured
from .schemas import _ModelSceneMention

log = logging.getLogger(__name__)


def response_model(known_scenes: list[str]) -> type[BaseModel]:
    """按本集已登记场景动态生成响应模型，display_name 是这批名字的 Literal。

    必须让 enum 落在 **model_type** 上，而不是走 ``_call_structured`` 的
    ``output_schema`` 形参：2026-09-18 实测踩中——``output_schema`` 在
    model_gateway 里只被塞进格式/语义修复重试的提示词文本（见该模块
    ``structured_schema`` 的两处用法），首次调用的请求体里根本没有它，供应商侧毫无
    约束。而 ``model_type`` 会经 ``_response_format`` 变成真正下发的
    ``response_format.json_schema``，``model_json_schema()`` 把 Literal 渲染成 enum。

    合法值域由数据推导（本集已登记场景），不是词表。
    """
    mention = create_model(
        "_SceneRecheckMention",
        __config__=ConfigDict(extra="forbid"),
        display_name=(Literal[tuple(known_scenes)], ...),  # type: ignore[valid-type]
        segment_indexes=(list[int], ...),
        quote=(str, ...),
    )
    return create_model(
        "_SceneRecheckResponse",
        __config__=ConfigDict(extra="forbid"),
        scenes=(list[mention], ...),
    )


def _prompt(rendered: str, known_scenes: list[str]) -> str:
    return f"""你在标注一集短剧每个编号的拍摄地点。原文按编号分段列在下面。

本集已登记的场景（只能从这个清单里逐字选名字，不要自造新地点）：
{known_scenes}

任务：逐个编号回答——这个编号的镜头置身在上面清单里的哪些场景。把每个场景连同它
覆盖的编号列出来。这是一次独立标注，不用管别处报过什么，也不用回避重复。

**先假定每个编号可能有不止一个场景，再逐个排除**，而不是先挑一个主场景就收工。
一个编号只属于一个场景是常见情况，但格局镜、转场镜、从室内走到室外、镜头升起拉开
这几类，往往同时置身两个场景——遇到这几类要专门想一遍「除了最明显的那个，机位还
经过/停在别的地方吗」。宁可对同一个编号列两个场景并各自给出 quote，也不要因为已经
找到一个合适的就不往下看了。

判断依据只有一条：**这个编号的摄影机置身在哪个空间**。镜头在哪个地点里取景，
那个地点就是这一段的场景。场景图是给这一镜提供环境的，判据是「机位在哪」，
不是「画面里看得见什么」。

算的情形——

- 摄影机所在的那个空间，无论有没有角色出场：格局镜、空镜、转场镜、只有景物或
  动物的镜头都算，「人物：无」不等于「没有场景」；
- 一个编号的镜头可以置身于不止一个空间：镜头从 A 摇到 B、从 A 升起拉开到 B、
  人物站在 A 而画面同时把 B 的空间整片铺开——A 和 B 都要报；
- 本编号只用省略说法称呼一个地点（"门口""店里""楼下"），而它在已登记清单里有完整
  名字、且**摄影机确实在那个空间里**时，按清单里的完整名字报。

不算的情形（这些一个都不要报）——

- 摄影机在 A 的室内，画面里能看见 B 的一角：窗外的招牌、门上的海报、屏幕里的画面、
  远处的灯光——场景仍然只是 A，B 只是 A 的画面里的一个元素；
- 人物走向 B、说起 B、想到 B，而摄影机没有跟过去；
- 上一个编号在 B，这个编号已经切走了。

一句话分辨：镜头如果就架在那个地点里，算；镜头只是从别处望见它，不算。

每条给 {{"display_name": "清单里的名字，逐字", "segment_indexes": [该地点真的被拍到的
编号], "quote": "从这些编号原文里逐字摘录、能支持这个判断的一小段（不超过约60字），
不得改写或跨编号拼接"}}。

判断不了就不报。漏一个只是少一张参考图，报错一个会让这段戏挂上另一个地方的
参考图——后者要糟得多。某个编号确实没有任何已登记场景对得上时，就不要为它列编号。

原文：
{rendered}
"""



async def recheck_chunk_scenes(
    *,
    episode_id: str,
    chunk_index: int,
    chunk: list[tuple[int, SourceSegment]],
    known_scenes: list[str],
    run_id: str | None,
) -> list[dict[str, Any]]:
    """返回本 chunk 标注到的场景提及（已过段号结构闸）；标不出来时返回空列表。"""
    if not known_scenes:
        return []
    chunk_global_indexes = {index for index, _segment in chunk}
    chunk_by_index = {index: segment for index, segment in chunk}
    response = await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_scene_recheck",
        iteration_no=chunk_index,
        prompt=_prompt(_render_chunk(chunk), known_scenes),
        model_type=response_model(known_scenes),
        schema_name="episode_prep_pack_scene_recheck_v1",
        operation_id=f"episode_prep_pack:{episode_id}:scene_recheck:{chunk_index}",
        max_tokens=4000,
        call_meta={
            "stage_key": "episode_prep_pack_scene_recheck",
            "episode_id": episode_id,
            "chunk_index": chunk_index,
        },
    )
    added: list[dict[str, Any]] = []
    for mention in response.scenes:
        name = str(mention.display_name or "").strip()
        if name not in known_scenes:
            continue  # enum 之外的一律丢弃，不做近似匹配
        valid = _prep_pack_gate_segment_indexes(
            name, mention.segment_indexes, chunk_global_indexes, chunk_by_index,
        )
        if not valid:
            continue
        added.append({
            "display_name": name,
            "suspected_true_name": None,
            "segment_indexes": valid,
            "quote": str(mention.quote or "").strip(),
        })
    return added


async def attach_scene_recheck(
    response: Any,
    *,
    chunk: list[tuple[int, SourceSegment]],
    chunk_index: int,
    episode_id: str,
    known_scenes: list[str],
    run_id: str | None,
) -> Any:
    """就地把复核补回的场景并进 ``response.scenes``，返回同一个 response。

    挂在 ``_extract_chunk`` 的出口而不是主流程里：``_generate_prep_pack_once``
    的 function_lines 早已顶着棘轮，在那里加调用会逼着调基线；挂在这里主流程一行
    不用改，补漏对上游是透明的。

    复核是补漏增强，不是门禁：失败不能把整个映射包拖垮，所以吞掉异常只记一条
    warning，原样交回抽取结果。吞的是「补漏没补成」，不是「抽取失败」——抽取自身
    的失败仍由 ``_call_structured`` 照常抛出。
    """
    try:
        added = await recheck_chunk_scenes(
            episode_id=episode_id, chunk_index=chunk_index, chunk=chunk,
            known_scenes=known_scenes, run_id=run_id,
        )
    except Exception:  # noqa: BLE001 - 补漏失败不阻断主流程
        log.warning("场景复核失败，本 chunk 沿用抽取结果 episode=%s chunk=%s",
                    episode_id, chunk_index, exc_info=True)
        return response
    if not added:
        return response
    gained = 0
    by_name = {m.display_name: m for m in (response.scenes or [])}
    for item in added:
        name = item["display_name"]
        existing = by_name.get(name)
        if existing is None:
            response.scenes.append(_ModelSceneMention(**item))
            gained += len(item["segment_indexes"])
            continue
        before = set(existing.segment_indexes or [])
        after = before | set(item["segment_indexes"])
        gained += len(after - before)
        existing.segment_indexes = sorted(after)
    log.info("场景复核补回 %d 个编号归属 episode=%s chunk=%s", gained, episode_id, chunk_index)
    return response
