"""称谓解析响应条目缺 raw_label 的确定性修补（WS2-2a）。

背景：``appellation_resolve._appellation_resolution_call`` 用
``model_gateway.chat_structured`` 拿 ``_AppellationResolutionResponse``，
schema 里 ``raw_label`` 是必填字段（``model_config = ConfigDict(extra=
"forbid")``，字段本身无默认值）。近 7 天 B 库多次撞见结构化输出失败：
``appellations.N.raw_label Field required``——响应本身语法合法（能被
json 解析成 dict），只是数组里某一条缺了这一个 key；``_appellation_
resolution_call`` 没有传 ``require_response_format=True``，供应商不支持
严格 json_schema 时会静默降级成不强制校验的生成模式（见
``model_gateway.chat_structured`` docstring），因此模型确实能漏填一个
被 schema 标为 required 的字段。此前这种缺一个字段的响应会被
``model_gateway.chat_structured`` 的 pydantic 校验整体判定为格式失败，
触发一次语义重试甚至让整段叙述向称谓归属落空——一条缺字段的记录不该
拖累同一批里其它已经完整申报的记录。

修补时机：``chat_structured`` 的 ``normalize_payload`` 钩子在响应 dict
进 pydantic 校验之前调用（``app/harness/model_gateway.py`` 里
``_coerce_structured`` 之前那一步），这里只做两件事，都不编造内容：

* 缺失/空白 raw_label 的条目，若它的 (identity, evidence,
  segment_indexes) 三元组与本响应里另一条**已经带着完整 raw_label**的
  条目完全一致——说明这是模型对同一次申报重复输出了两遍、其中一遍漏填
  ——直接借用那条的 raw_label（借用的是模型自己已经写过的原文，不是
  凭空生成）；
* 借不到（没有可借用的完整兄弟条目）：整条从数组里丢弃，不用空字符串
  顶替。空字符串本可以让 pydantic 校验通过（``raw_label: str`` 接受空
  串），但那样会把"缺字段"悄悄伪装成"模型申报了一个空标签"，且
  ``appellation_resolve._verified_verdicts`` 已有的
  ``if not raw_label: continue`` 分支会在校验通过之后再次静默吃掉它——
  两次静默叠加，调用方永远看不到"丢了几条、丢的是谁"。这里直接丢弃并
  留痕，让丢失可观测。

不放松 schema：``_AppellationVerdict``/``_AppellationResolutionResponse``
定义原封不动，``extra="forbid"`` 照旧；这里只是在数据进模型之前把"确定
能救"的救回来、"救不了"的清出去，救不了的从来不会被留在数组里让
pydantic 去报错——那条报错路径因此不会再被走到，除非 payload 本身不是
预期的 dict/list 形状（这种更严重的畸形交还给原有的格式重试处理）。
"""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def _is_blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def _borrow_signature(item: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        item.get("identity"),
        item.get("evidence"),
        tuple(item.get("segment_indexes") or []),
    )


def repair_appellation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """按位置补齐/丢弃 ``appellations[].raw_label`` 缺失的条目。

    不就地修改传入的 ``payload``（``chat_structured`` 的 ``normalize_
    payload`` 约定：无变化时原样返回同一个对象，供调用方用 ``!=`` 判断
    是否发生过本地归一，见该函数其它调用点的既有写法）。
    """
    if not isinstance(payload, dict):
        return payload
    items = payload.get("appellations")
    if not isinstance(items, list):
        return payload
    complete_by_signature: dict[tuple[Any, Any, Any], str] = {}
    for item in items:
        if isinstance(item, dict) and not _is_blank(item.get("raw_label")):
            complete_by_signature.setdefault(_borrow_signature(item), item["raw_label"])
    repaired: list[Any] = []
    changed = False
    for position, item in enumerate(items):
        if not isinstance(item, dict) or not _is_blank(item.get("raw_label")):
            repaired.append(item)
            continue
        changed = True
        borrowed = complete_by_signature.get(_borrow_signature(item))
        if borrowed is not None:
            repaired.append({**item, "raw_label": borrowed})
            _LOGGER.info(
                "[APPELLATION_REPAIR] 第 %d 条 appellations 缺 raw_label，"
                "(identity, evidence, segment_indexes)=%r 与另一条完整申报的"
                "条目完全一致，借用其 raw_label=%r",
                position, _borrow_signature(item), borrowed,
            )
        else:
            _LOGGER.info(
                "[APPELLATION_REPAIR] 第 %d 条 appellations 缺 raw_label 且无"
                "可借用的完整兄弟条目，丢弃该条（identity=%r, evidence=%r, "
                "segment_indexes=%r）",
                position, item.get("identity"), item.get("evidence"),
                item.get("segment_indexes"),
            )
    if not changed:
        return payload
    return {**payload, "appellations": repaired}


__all__ = ["repair_appellation_payload"]
