"""未来章节身份候选解析——把模型的决议选择投影回按 identity_group 的结果。

从 ``future_identity_resolution.py`` 拆出：原来内联在
``resolve_future_identity_candidates`` 里的 ``response_decisions`` 闭包，把
``FutureIdentityCandidateResponse``（按 group_key 选择的 decision_id）转换成
每个 source_label 都能查到的扁平决议列表，以及按 identity_group（唯一真正可靠
的按人区分键）索引的结果映射。
"""
from __future__ import annotations

from .constants import REISSUE_KNOWN_RESOLUTION_KIND
from .discovery_resample import (
    _bounded_owned_identity_evidence,
    _canonical_named_authority_id,
)
from .future_identity_response_contract import _FutureIdentityResolutionContext
from .identity_schemas import FutureIdentityCandidateResponse


def _future_identity_response_decisions(
    value: FutureIdentityCandidateResponse,
    context: _FutureIdentityResolutionContext,
) -> tuple[list[dict], dict[str, dict]]:
    # resolved_by_group（真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性
    # 排查命中）：identity_group 是这条流水线里唯一真正可靠的按人区分的
    # 键——一个 group 可能因为模型判定"这些称谓是同一个人"而含多个不同
    # 的 source_label（见下面 group["labels"]），也可能两个不同的人
    # 恰好共享同一个裸 source_label 字符串（"外宗弟子"甲/乙，各自不同
    # identity_group）。旧代码只留了 decisions（按 source_label 展开的
    # 扁平列表），下游再用 dict 推导式按裸 source_label 二次索引
    # （resolved_by_label/candidate_by_label）——两个人共享同一个裸标签
    # 时，字典推导式静默用后者覆盖前者，两个人的解析结果被合并成一个。
    # 直接在这里按 identity_group（拼接的真正唯一键）建一份可靠映射，
    # 调用方不再需要经过裸标签这一跳。
    group_specs = context.group_specs
    decision_by_id = context.decision_by_id
    evidence_by_id = context.evidence_by_id
    decisions: list[dict] = []
    resolved_by_group: dict[str, dict] = {}
    for group in group_specs:
        group_key = str(group["group_key"])
        selected_id = str(value.decisions.get(group_key) or "")
        selected = decision_by_id.get(selected_id, {})
        resolution_kind = str(selected.get("resolution_kind") or "")
        if resolution_kind == "known_named":
            anchors = [
                str(value)
                for value in selected.get("proof_anchors") or []
                if str(value)
            ]
            evidence_options = [
                evidence_by_id.get(str(evidence_id), {})
                for evidence_id in selected.get("evidence_ids") or []
            ]
            evidence = next(
                (
                    item for item in evidence_options
                    if any(
                        anchor and anchor in str(item.get("text") or "")
                        for anchor in anchors
                    )
                ),
                {},
            )
            bounded_evidence = _bounded_owned_identity_evidence(
                str(evidence.get("text") or ""),
                anchors=anchors,
            )
            common = {
                "resolution_kind": resolution_kind,
                "identity_kind": "named",
                "canonical_name": str(
                    selected.get("canonical_name") or ""
                ),
                "authority_id": str(
                    selected.get("authority_id") or ""
                ),
                "materialization_compatible": bool(
                    selected.get("materialization_compatible")
                ),
                "future_evidence": bounded_evidence,
            }
        elif resolution_kind == REISSUE_KNOWN_RESOLUTION_KIND:
            # 归一分支（第26轮，见 normalize_identity_payload 上方完整
            # 说明）：这个 group 被确定性判定为对一个已有 authority 的
            # 冗余重复声明，不是新身份——future_evidence 留空，不假装
            # 有一条这次才核验出的逐字锚点（已知身份的锚点在它初次
            # 签发时已经验过，这里不重新造一份）。
            common = {
                "resolution_kind": resolution_kind,
                "identity_kind": "named",
                "canonical_name": str(
                    selected.get("canonical_name") or ""
                ),
                "authority_id": str(
                    selected.get("authority_id") or ""
                ),
                "materialization_compatible": bool(
                    selected.get("materialization_compatible")
                ),
                "future_evidence": "",
            }
        elif resolution_kind == "new_named":
            canonical_name = str(
                value.revealed_names.get(group_key) or ""
            )
            evidence = evidence_by_id.get(
                str(value.reveal_evidence_ids.get(group_key) or ""),
                {},
            )
            bounded_evidence = _bounded_owned_identity_evidence(
                str(evidence.get("text") or ""),
                anchors=[canonical_name],
            )
            common = {
                "resolution_kind": resolution_kind,
                "identity_kind": "named",
                "canonical_name": canonical_name,
                "authority_id": (
                    _canonical_named_authority_id(canonical_name)
                    if canonical_name.strip() else ""
                ),
                "materialization_compatible": True,
                "future_evidence": bounded_evidence,
            }
        else:
            common = {
                "resolution_kind": "functional",
                "identity_kind": "functional",
                "canonical_name": "",
                "authority_id": "",
                "materialization_compatible": False,
                "future_evidence": "",
            }
        resolved_by_group[str(group["identity_group"])] = common
        for source_label in group["labels"]:
            decisions.append({
                **common,
                "source_label": str(source_label),
            })
    return decisions, resolved_by_group
