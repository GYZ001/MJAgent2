"""未来章节身份候选解析——把按组决议结果合并回原始候选列表。

从 ``future_identity_resolution.py`` 拆出：原来内联在
``resolve_future_identity_candidates`` 尾部的最终装配步骤，按
``identity_group``（真正唯一键）把 ``_future_identity_response_decisions`` 产出
的按人结果写回每条候选，未被消歧的候选原样透传。
"""
from __future__ import annotations

from .constants import FUTURE_IDENTITY_DECISION_VERSION
from .discovery_resample import _canonical_named_authority_id


def _merge_future_identity_resolution(
    candidates: list[dict],
    resolved_by_group: dict[str, dict],
) -> list[dict]:
    # 真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性排查命中：resolved_by_group
    # 直接按 identity_group（真正唯一键，见 response_decisions 上方注释）
    # 取解析结果，不再经过裸 source_label 的两跳字典推导式（原设计里两个
    # 不同的人共享同一个裸标签时，Python 字典推导式会静默用后者覆盖前者，
    # 两个人的解析结果被悄悄合并成一个——"外宗弟子"甲乙正是这个形状）。
    merged: list[dict] = []
    for item in candidates:
        resolution = resolved_by_group.get(str(item.get("identity_group") or "").strip())
        if not resolution or resolution.get("identity_kind") != "named":
            merged.append(item)
            continue
        canonical_name = str(resolution.get("canonical_name") or "").strip()
        resolved_authority_id = (
            str(resolution.get("authority_id") or "")
            or _canonical_named_authority_id(canonical_name)
        )
        merged.append({
            **item,
            "name": canonical_name,
            "identity_kind": "named",
            "authority_id": resolved_authority_id,
            # The selected backend decision owns this verdict.  Never inherit
            # a previous functional candidate's optimistic flag, and never
            # recompute from the final authority ID alone: its origin group may
            # still be a durable incompatible manual/reference group.
            "materialization_compatible": bool(
                resolution.get("materialization_compatible")
            ),
            "future_evidence": str(
                resolution.get("future_evidence") or ""
            ),
            "decision_contract_version": FUTURE_IDENTITY_DECISION_VERSION,
            # 归一观测（第26轮 ERR-20260824-88ece5）：resolution_kind 是
            # 后端自己判定这个 group 具体走的是哪条决议（known_named/
            # new_named/reissue_known），此前从未向调用方暴露——加上后
            # 调用方可以直接 `sum(1 for c in result if c.get("resolution_
            # kind") == REISSUE_KNOWN_RESOLUTION_KIND)` 得到
            # normalized_new_reissues 计数，不需要另开一条平行的计数
            # 通路。纯附加字段，不影响任何既有消费者。
            "resolution_kind": str(resolution.get("resolution_kind") or ""),
        })
    return merged
