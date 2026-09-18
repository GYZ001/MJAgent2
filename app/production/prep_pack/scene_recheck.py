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

**合法值域用 output_schema 的 enum 钉死，不靠提示词自觉**：display_name 只能从本集
已登记场景清单里逐字取，模型没有能力自造一个新地点名——这是 schema 层面的保证，比
「请不要自己编」这类禁令强。已登记场景为空时直接跳过复核（空 enum 是非法 schema，
而且那种情况本来就无从复核）。

复核结果不覆盖抽取结果，只做并集：这里只回答「还漏了哪些」，从不否定已申报的条目。
段号仍要过 ``_prep_pack_gate_segment_indexes`` 的结构闸，与抽取侧同一把尺子。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.source_excerpt import SourceSegment

from .chunk_extraction import _call_structured, _render_chunk
from .chunking import _prep_pack_gate_segment_indexes
from .schemas import _ModelSceneMention

log = logging.getLogger(__name__)


class _SceneRecheckMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    segment_indexes: list[int]
    quote: str


class _SceneRecheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[_SceneRecheckMention]


def _output_schema(known_scenes: list[str]) -> dict[str, Any]:
    """把 display_name 收紧到本集已登记场景的 enum——合法值域由数据推导，不是词表。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["scenes"],
        "properties": {
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["display_name", "segment_indexes", "quote"],
                    "properties": {
                        "display_name": {"type": "string", "enum": list(known_scenes)},
                        "segment_indexes": {"type": "array", "items": {"type": "integer"}},
                        "quote": {"type": "string"},
                    },
                },
            },
        },
    }


def _prompt(rendered: str, known_scenes: list[str], declared_lines: list[str]) -> str:
    return f"""你在核对一集短剧的场景素材清单。原文按编号分段列在下面。

本集已登记的场景（只能从这个清单里逐字选名字，不要自造新地点）：
{known_scenes}

上一轮已经申报的场景与编号（这些是已知的，不用重复确认）：
{chr(10).join(declared_lines) if declared_lines else "（上一轮没有申报任何场景）"}

任务：逐个编号看画面里**拍到了哪些**已登记场景，只把上面**遗漏的**补出来。

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
参考图——后者要糟得多。确实没有遗漏时交回空列表 {{"scenes": []}}，那是完全正常的结果。

原文：
{rendered}
"""


async def recheck_chunk_scenes(
    *,
    episode_id: str,
    chunk_index: int,
    chunk: list[tuple[int, SourceSegment]],
    known_scenes: list[str],
    declared: list[dict[str, Any]],
    run_id: str | None,
) -> list[dict[str, Any]]:
    """返回本 chunk 补充的场景提及（已过段号结构闸）；没有遗漏时返回空列表。"""
    if not known_scenes:
        return []
    chunk_global_indexes = {index for index, _segment in chunk}
    chunk_by_index = {index: segment for index, segment in chunk}
    declared_lines = [
        f"- {item.get('display_name')}：{sorted(item.get('segment_indexes') or [])}"
        for item in declared
    ]
    response = await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_scene_recheck",
        iteration_no=chunk_index,
        prompt=_prompt(_render_chunk(chunk), known_scenes, declared_lines),
        model_type=_SceneRecheckResponse,
        schema_name="episode_prep_pack_scene_recheck_v1",
        operation_id=f"episode_prep_pack:{episode_id}:scene_recheck:{chunk_index}",
        max_tokens=4000,
        call_meta={
            "stage_key": "episode_prep_pack_scene_recheck",
            "episode_id": episode_id,
            "chunk_index": chunk_index,
        },
        output_schema=_output_schema(known_scenes),
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
    declared = [
        {"display_name": m.display_name, "segment_indexes": list(m.segment_indexes or [])}
        for m in (response.scenes or [])
    ]
    try:
        added = await recheck_chunk_scenes(
            episode_id=episode_id, chunk_index=chunk_index, chunk=chunk,
            known_scenes=known_scenes, declared=declared, run_id=run_id,
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
