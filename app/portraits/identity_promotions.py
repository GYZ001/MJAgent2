"""识别流程里的确定性改选（放在 schema 之外：identity_schemas 已到 500 行）。

「登记名＋关系称谓」→ 那条 K 决议，与 identity_schemas.promote_functional_matching_known
同一形状：模型给出的申报若与后端已签发的答案只差一个称谓后缀，直接采用后端的答案。
"""
from __future__ import annotations

from .identity_schemas import (
    CurrentFunctionalIdentityDecision,
    CurrentIdentityCandidateResponse,
    CurrentKnownIdentityDecision,
    CurrentNewNamedIdentityDecision,
)


def promote_titled_known(
    value: CurrentIdentityCandidateResponse, known_decisions: dict[str, dict] | None,
) -> CurrentIdentityCandidateResponse:
    """n/f 里的称呼是「某条 K 决议的 canonical_name＋关系称谓后缀」时，确定性改成选那条 K。
    原文写「韩宗师兄」而人物谱登记的是「韩宗」（2026-09-05 第 5 集实测：模型申报成新身份，
    分镜台建了一个没有定妆照的群演）。判据是精确相等：去掉闭集里的一个称谓后缀后剩余部分
    逐字等于 K 目录里的 canonical_name（同一证据里的优先）；剩余部分匹配到 ≥2 个不同 K 名时
    不猜，原样交给校验。"""
    from app.portraits.card_owner import strip_relational_title

    if not known_decisions or not (value.n or value.f):
        return value
    by_name: dict[str, list[tuple[str, dict]]] = {}
    for decision_id, decision in known_decisions.items():
        canonical = str(decision.get("canonical_name") or "").strip()
        if canonical:
            by_name.setdefault(canonical, []).append((decision_id, decision))

    def _target(label: str, evidence_ref: str) -> tuple[str, dict] | None:
        stem = strip_relational_title(label)
        candidates = by_name.get(stem or "", [])
        if not candidates or len({d.get("canonical_name") for _, d in candidates}) != 1:
            return None
        same_ref = [c for c in candidates if str(c[1].get("evidence_ref") or "") == evidence_ref]
        return (same_ref or candidates)[0]

    chosen = {item.decision_id for item in value.k}
    promoted: list[CurrentKnownIdentityDecision] = []
    kept_n: list[CurrentNewNamedIdentityDecision] = []
    kept_f: list[CurrentFunctionalIdentityDecision] = []
    for branch, items, kept in (("n", value.n, kept_n), ("f", value.f, kept_f)):
        for item in items:
            label = item.identity_label if branch == "n" else item.source_label
            hit = _target(label, item.evidence_ref)
            if hit is None:
                kept.append(item)
                continue
            decision_id, decision = hit
            allowed = [str(k) for k in (decision.get("allowed_kinds") or [])]
            kind = item.kind if not allowed or item.kind in allowed else allowed[0]
            if decision_id not in chosen:
                promoted.append(CurrentKnownIdentityDecision(decision_id=decision_id, kind=kind))
                chosen.add(decision_id)
    if not promoted and len(kept_n) == len(value.n) and len(kept_f) == len(value.f):
        return value
    return value.model_copy(update={"k": [*value.k, *promoted], "n": kept_n, "f": kept_f})
