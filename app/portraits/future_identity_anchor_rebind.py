"""N: 决议缺逐字真名锚点时的确定性改写（2026-09-06 第 9 轮第 10 集 ERR-20260906-989ee7）。

``_validate_future_identity_response`` 要求选 N: 的组：``reveal_evidence_ids`` 指向的证据必须
origin=future、逐字在 future_text 里、且逐字含该真名。模型挑错了证据下标（真名明明在同组另一条
future 证据里）或压根没有一条证据含真名时，原来整集映射台判死。判据全来自本批证据目录，
不二次问模型（``identity_degrade`` 同一纪律）：

* 同组有别的 future 证据逐字含真名 → 改绑第一条（同 ``identity_literal_evidence`` 的「多命中改绑首条」）；
* 一条都没有 → 真名没有逐字依据，维持既有硬失败（tests/test_character_discovery.py 多条 RCA 回归
  钉死「编造的真名不得降级放行」）。
改绑记一条 NORMALIZED 账本，缺失可见。
"""
from __future__ import annotations

from typing import Any

from app.db import log_provider_call


def _anchored(context: Any, evidence_id: str, name: str) -> bool:
    evidence = context.evidence_by_id.get(evidence_id) or {}
    text = str(evidence.get("text") or "")
    return evidence.get("origin") == "future" and bool(text) and text in context.future_text and name in text


def rebind_or_defer_missing_anchor(payload: Any, context: Any) -> Any:
    """跑在 ``_normalize_future_identity_payload`` 之后、校验之前；没有需要改写的组时原样返回。"""
    if not isinstance(payload, dict):
        return payload
    decisions, names, evidence_ids = payload.get("decisions"), payload.get("revealed_names"), payload.get("reveal_evidence_ids")
    if not all(isinstance(x, dict) for x in (decisions, names, evidence_ids)):
        return payload
    rebound: dict[str, str] = {}
    for group_key in context.group_keys:
        selected = context.decision_by_id.get(str(decisions.get(group_key) or ""))
        if selected is None or str(selected.get("resolution_kind") or "") != "new_named":
            continue
        name = str(names.get(group_key) or "").strip()
        evidence_id = str(evidence_ids.get(group_key) or "").strip()
        if not name or not evidence_id or _anchored(context, evidence_id, name):
            continue  # evidence_id 为空是另一条既有硬失败（ERR-20260831-45404d），不代填
        candidate = next((eid for eid in context.evidence_ids_by_group.get(group_key, []) if _anchored(context, eid, name)), None)
        if candidate is not None:
            rebound[group_key] = candidate
        # 一条 future 证据都不含真名：真名没有逐字依据，维持既有硬失败（多条 RCA 回归钉死
        # 「不得把编造的真名降级放行」），这里不降 F:。
    if not rebound:
        return payload
    log_provider_call(
        "future_identity_normalization", "", "NORMALIZED", None, 0,
        meta={"changes": [{"code": "NEW_ANCHOR_REBOUND", "groups": rebound}]},
    )
    return {**payload, "reveal_evidence_ids": {**evidence_ids, **rebound}}
