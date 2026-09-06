"""N: 决议缺逐字真名锚点时的确定性改写（2026-09-06 第 9-11 轮第 10 集 ERR-20260906-989ee7 / 2262d7）。

``_validate_future_identity_response`` 要求选 N: 的组：``reveal_evidence_ids`` 指向本组证据目录里的一条
future 证据，且逐字含该真名。实测两种形状让正确的判断也过不了：
* 模型挑错了证据下标——真名在同组另一条 future 证据里；
* 本组证据窗口是按本集称谓（「曹某」）检索的，而真名（「曹阳」）只在后续章节出现（第 11-20 章 46 次，
  「曹某」一次都没有）：目录里根本没有一条含真名的窗口，N: 在结构上不可能合法，第 10 集每轮必死。

判据全来自本批证据目录与后端持有的 future_text，不二次问模型（``identity_degrade`` 同一纪律）：
1. 同组有别的 future 证据逐字含真名 → 改绑第一条（同 ``identity_literal_evidence``「多命中改绑首条」）；
2. 否则 future_text 里存在一个 120 字窗口同时含本组某个称谓与真名（与目录窗口同一强度的共现证据）
   → 后端自己切出这个窗口登记进本组证据目录并改绑；
3. 否则真名逐字在 future_text 里但没有共现证据 → 此刻签不了真名，降为 ``F:{group}`` 功能身份
   （与 ``_normalize_future_identity_payload`` 的「非真名形态降级」同一出口），等真名逐字出现在
   本集原文里再由 K 决议认领；
4. 真名压根不在 future_text 里 → 维持既有硬失败（多条 RCA 回归钉死「编造的真名不得降级放行」）。
1-3 都记一条 NORMALIZED 账本，缺失可见。
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.db import log_provider_call

_WINDOW = 120


def _anchored(context: Any, evidence_id: str, name: str) -> bool:
    evidence = context.evidence_by_id.get(evidence_id) or {}
    text = str(evidence.get("text") or "")
    return evidence.get("origin") == "future" and bool(text) and text in context.future_text and name in text


def _group_labels(context: Any, group_key: str) -> list[str]:
    for spec in getattr(context, "group_specs", None) or []:
        if str(spec.get("group_key") or "") == group_key:
            return [str(x) for x in spec.get("source_labels") or [] if str(x)]
    return []


def _cooccurrence_window(future_text: str, labels: list[str], name: str) -> tuple[int, int] | None:
    """future_text 里第一个同时含某个称谓与真名、长度 ≤120 字的窗口（起止偏移）。"""
    start = 0
    while (idx := future_text.find(name, start)) >= 0:
        lo, hi = max(0, idx - _WINDOW + len(name)), min(len(future_text), idx + _WINDOW)
        for label in labels:
            pos = future_text.find(label, lo, hi)
            if pos >= 0:
                begin = max(0, min(pos, idx) - 10)
                return begin, min(len(future_text), begin + _WINDOW)
        start = idx + 1
    return None


def _synthesize_evidence(context: Any, group_key: str, begin: int, end: int) -> str:
    text = context.future_text[begin:end]
    evidence_id = "E:anchor:" + hashlib.sha1(f"{group_key}:{begin}:{text}".encode("utf-8")).hexdigest()[:20]
    context.evidence_by_id[evidence_id] = {
        "evidence_id": evidence_id, "origin": "future", "start_offset": begin, "end_offset": end, "text": text,
    }
    context.evidence_ids_by_group.setdefault(group_key, []).append(evidence_id)
    return evidence_id


def rebind_or_defer_missing_anchor(payload: Any, context: Any) -> Any:
    """跑在 ``_normalize_future_identity_payload`` 之后、校验之前；没有需要改写的组时原样返回。"""
    if not isinstance(payload, dict):
        return payload
    decisions, names, evidence_ids = payload.get("decisions"), payload.get("revealed_names"), payload.get("reveal_evidence_ids")
    if not all(isinstance(x, dict) for x in (decisions, names, evidence_ids)):
        return payload
    kinds = payload.get("revealed_name_kinds") if isinstance(payload.get("revealed_name_kinds"), dict) else {}
    rebound: dict[str, str] = {}
    synthesized: dict[str, str] = {}
    deferred: list[str] = []
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
            continue
        if name not in context.future_text:
            continue  # 编造的真名：维持硬失败
        window = _cooccurrence_window(context.future_text, _group_labels(context, group_key), name)
        if window is not None:
            synthesized[group_key] = _synthesize_evidence(context, group_key, *window)
        elif f"F:{group_key}" in context.decision_by_id:
            deferred.append(group_key)
    if not (rebound or synthesized or deferred):
        return payload
    log_provider_call(
        "future_identity_normalization", "", "NORMALIZED", None, 0,
        meta={"changes": [
            {"code": "NEW_ANCHOR_REBOUND", "groups": rebound},
            {"code": "NEW_ANCHOR_SYNTHESIZED", "groups": synthesized},
            {"code": "NEW_DEFERRED_NO_COOCCURRENCE", "groups": deferred},
        ]},
    )
    blank = {group_key: "" for group_key in deferred}
    return {
        **payload,
        "decisions": {**decisions, **{group_key: f"F:{group_key}" for group_key in deferred}},
        "revealed_names": {**names, **blank},
        "reveal_evidence_ids": {**evidence_ids, **rebound, **synthesized, **blank},
        "revealed_name_kinds": {**kinds, **blank},
    }
