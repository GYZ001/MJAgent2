"""道具卡复核「两次独立判定取交集」的合并逻辑（2026-10-03-v2 新增）。

背景（B 沙箱两轮真实模型 dry-run 实测，见 ``app.props.card_audit`` 模块
docstring 与本次派单）：复核调用温度 0.1 仍存在采样不稳定——同一条子句/别名
两次调用一次判删一次判留。此前只发一次调用，漏删/误删都是**静默**的；这里
把"发两次独立调用、只有两次都判删才真的删"做成固定流程，把采样不稳定从
"静默错误"变成"可见存疑"（``doubts``），交给人工确认（见
``app.domain.bible_ops.props_api`` 的确认/保留端点）。

延迟评估：两次调用用 ``asyncio.gather`` 并发发起，不是串行等两次——调用方
（建卡后同步复核 ``app.props.service``、生成前懒复核
``app.props.card_audit_ensure``）等待的是两次调用里较慢的那一次，不是两次
相加；网络延迟不翻倍，只有"模型算力这一侧要多算一次"的成本（CLAUDE.md
「文本免费但视频有额度」——这部分成本是免费的）。

子句合并（``merge_clause_judgments``）：两次调用各自先过
``card_audit_rules.verify_clause_removal_verdicts`` 的单次核验（下标/类别/
owner 归属证据），再取"两次都判删"的交集作为真正删除的子句；两次不一致、
或任一次判定降级为存疑（owner 没有自己的卡 / 模型自述拿不准），都不删，
转成 ``doubts`` 里的一条记录，供人工在道具库页面确认删除或保留。

别名合并（``merge_alias_judgments``）同一套取舍，只是别名判定没有 owner
归属证据这一环（判据本身就是"是否只剩品类名"，不涉及"这是谁的"）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Sequence

from app.schemas import Prop

from . import card_audit_rules

DOUBT_TYPE_INCONSISTENT = "inconsistent_between_two_judgments"
DOUBT_TYPE_OWNER_WITHOUT_CARD = "owner_without_card"
DOUBT_TYPE_MODEL_SELF_DOUBT = "model_self_doubt"


def clause_doubt_key(text: str) -> str:
    """存疑的持久化定位键——按子句原文（不是下标，下标会在子句被删除后
    错位），人工保留的决定靠这个键识别"同一条外观信息"，规则版本不变时不
    重复询问（见 ``app.props.card_audit_store`` 的决定表）。"""
    return f"clause:{text}"


def alias_doubt_key(alias: str) -> str:
    return f"alias:{alias}"


async def run_two_independent_clause_judgments(
    prop: Prop, clauses: list[str], owner_catalog_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """并发发起两次独立判定调用（不同 ``operation_id``，见
    ``card_audit_rules.request_prop_card_audit_judgment`` 的说明）。"""
    return await asyncio.gather(
        card_audit_rules.request_prop_card_audit_judgment(prop, clauses, owner_catalog_text, call_tag="a"),
        card_audit_rules.request_prop_card_audit_judgment(prop, clauses, owner_catalog_text, call_tag="b"),
    )


def _clause_doubt_entry(
    index: int, clauses: list[str], records_a: dict[int, dict], records_b: dict[int, dict],
    owner_doubts_a_by_index: dict[int, dict], owner_doubts_b_by_index: dict[int, dict],
) -> dict[str, Any]:
    """一条子句下标最多产出**一条**存疑记录（审查发现：此前对同一下标分别从
    "A/B 不一致"与"A 的 owner 降级"与"B 的 owner 降级"三路独立产出，同一
    ``doubt_key`` 下会出现多条各自残缺的重复记录——前端按这个 key 当 React
    key 用，重复 key 会互相顶替渲染、"确认删除"的忙碌态也会在视觉上串台）。
    这里按下标把 A/B 两路理由先合并好，再统一产出这一条。"""
    doubt_a = owner_doubts_a_by_index.get(index)
    doubt_b = owner_doubts_b_by_index.get(index)
    reason_a = doubt_a["reason"] if doubt_a else records_a.get(index, {}).get("reason", "（A 判定不删除）")
    reason_b = doubt_b["reason"] if doubt_b else records_b.get(index, {}).get("reason", "（B 判定不删除）")
    # 两次都降级时优先取 A 的 doubt_type/owner/category（信息性字段，不影响是否
    # 删除的判定——两次降级类型不同属于边缘情况，与 removed_records 合并时"取
    # call A 的 category"同一取舍，见 card_audit.py 已知局限）。
    chosen = doubt_a or doubt_b
    doubt_type = chosen["doubt_type"] if chosen else DOUBT_TYPE_INCONSISTENT
    entry = {
        "kind": "clause", "index": index, "text": clauses[index - 1],
        "doubt_type": doubt_type, "reason_a": reason_a, "reason_b": reason_b,
    }
    for extra_key in ("owner", "category"):
        value = (doubt_a or {}).get(extra_key) or (doubt_b or {}).get(extra_key)
        if value:
            entry[extra_key] = value
    return entry


def _clause_doubts(
    removed_a: set[int], removed_b: set[int], owner_doubts_a: list[dict], owner_doubts_b: list[dict],
    records_a: dict[int, dict], records_b: dict[int, dict], clauses: list[str],
) -> list[dict[str, Any]]:
    """两次判定不一致、或任一次（或两次）降级为存疑的下标，各产出一条合并
    记录；两次都同意删除的下标（``removed_a & removed_b``）不在此列，由调用方
    直接采信删除。"""
    owner_a_by_index = {d["index"]: d for d in owner_doubts_a}
    owner_b_by_index = {d["index"]: d for d in owner_doubts_b}
    candidate_indexes = (removed_a | removed_b | set(owner_a_by_index) | set(owner_b_by_index)) - (
        removed_a & removed_b
    )
    return [
        _clause_doubt_entry(i, clauses, records_a, records_b, owner_a_by_index, owner_b_by_index)
        for i in sorted(candidate_indexes)
    ]


def merge_clause_judgments(
    clause_count: int, raw_a: list[dict], raw_b: list[dict],
    other_identifiers: frozenset[str], clauses: list[str],
) -> dict[str, Any]:
    """两次独立判定取交集：一条子句两次都判删（且都通过 owner 归属证据核验）
    才真正删除；任何不一致或降级都转成存疑，不删。返回
    ``{"removed_indexes", "removed_records", "missing_indexes", "doubts"}``。
    """
    removed_a, records_a_list, missing_a, owner_doubts_a = card_audit_rules.verify_clause_removal_verdicts(
        clause_count, raw_a, other_identifiers,
    )
    removed_b, records_b_list, missing_b, owner_doubts_b = card_audit_rules.verify_clause_removal_verdicts(
        clause_count, raw_b, other_identifiers,
    )
    records_a = {r["index"]: r for r in records_a_list}
    records_b = {r["index"]: r for r in records_b_list}
    removed_indexes = removed_a & removed_b
    removed_records = [
        {**records_a[i], "reason": f"A：{records_a[i]['reason']}；B：{records_b[i]['reason']}"}
        for i in sorted(removed_indexes)
    ]
    doubts = _clause_doubts(removed_a, removed_b, owner_doubts_a, owner_doubts_b, records_a, records_b, clauses)
    return {
        "removed_indexes": removed_indexes, "removed_records": removed_records,
        "missing_indexes": missing_a | missing_b, "doubts": doubts,
    }


def merge_alias_judgments(aliases: Sequence[str], raw_a: list[dict], raw_b: list[dict]) -> dict[str, Any]:
    """别名提名两次取交集，同一套取舍，没有 owner 归属证据这一环。"""
    nominated_a = {r["alias"]: r for r in card_audit_rules.verify_alias_removal_verdicts(aliases, raw_a)}
    nominated_b = {r["alias"]: r for r in card_audit_rules.verify_alias_removal_verdicts(aliases, raw_b)}
    agreed = set(nominated_a) & set(nominated_b)
    removed = [
        {**nominated_a[alias], "reason": f"A：{nominated_a[alias]['reason']}；B：{nominated_b[alias]['reason']}"}
        for alias in sorted(agreed)
    ]
    doubts = [
        {
            "kind": "alias", "alias": alias, "text": alias, "doubt_type": DOUBT_TYPE_INCONSISTENT,
            "reason_a": nominated_a.get(alias, {}).get("reason", "（A 判定保留）"),
            "reason_b": nominated_b.get(alias, {}).get("reason", "（B 判定保留）"),
        }
        for alias in sorted((set(nominated_a) | set(nominated_b)) - agreed)
    ]
    return {"removed": removed, "doubts": doubts}


def doubt_key(doubt: dict[str, Any]) -> str:
    """一条存疑记录的持久化定位键——与 ``clause_doubt_key``/``alias_doubt_
    key`` 同一套，供人工保留决定的查找/写入复用同一份判据。"""
    if doubt["kind"] == "alias":
        return alias_doubt_key(doubt["alias"])
    return clause_doubt_key(doubt["text"])


def filter_doubts_against_kept_decisions(
    doubts: list[dict[str, Any]], kept_keys: frozenset[str],
) -> list[dict[str, Any]]:
    """人工点过「保留」的存疑，同一规则版本内不再重复询问（见
    ``app.props.card_audit_store`` 的决定表）——dry-run 路径不读决定表，始终
    传入空集合，不受历史决定影响（纯计算，供沙箱验证用）。"""
    return [d for d in doubts if doubt_key(d) not in kept_keys]
