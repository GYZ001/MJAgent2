"""N: 决议缺逐字真名锚点：同组别的 future 证据含真名就改绑，一条都没有就降 F:（2026-09-06 第 9 轮第 10 集）。"""
from __future__ import annotations

from types import SimpleNamespace

from app.portraits import future_identity_anchor_rebind as rebind


def _context(future_text: str, evidence: dict[str, dict], groups: dict[str, list[str]]):
    decisions = {}
    for group_key in groups:
        decisions[f"N:{group_key}"] = {"decision_id": f"N:{group_key}", "group_key": group_key, "resolution_kind": "new_named"}
        decisions[f"F:{group_key}"] = {"decision_id": f"F:{group_key}", "group_key": group_key, "resolution_kind": "functional"}
    return SimpleNamespace(
        group_keys=list(groups), decision_by_id=decisions, evidence_by_id=evidence,
        evidence_ids_by_group=groups, future_text=future_text,
    )


def _payload(group_key: str, name: str, evidence_id: str) -> dict:
    return {
        "decisions": {group_key: f"N:{group_key}"}, "revealed_names": {group_key: name},
        "reveal_evidence_ids": {group_key: evidence_id}, "revealed_name_kinds": {group_key: "personal_name"},
    }


def test_wrong_evidence_index_is_rebound_to_the_future_evidence_that_names_the_person(monkeypatch) -> None:
    logged: list[dict] = []
    monkeypatch.setattr(rebind, "log_provider_call", lambda *a, **kw: logged.append(kw["meta"]))
    future_text = "这少年虎头虎脑，大声开口。……“我叫赵玉山！”那少年挺起胸膛。"
    evidence = {
        "E1": {"origin": "future", "text": "这少年虎头虎脑，大声开口。"},
        "E2": {"origin": "future", "text": "“我叫赵玉山！”那少年挺起胸膛。"},
    }
    ctx = _context(future_text, evidence, {"G011": ["E1", "E2"]})
    out = rebind.rebind_or_defer_missing_anchor(_payload("G011", "赵玉山", "E1"), ctx)
    assert out["decisions"]["G011"] == "N:G011" and out["reveal_evidence_ids"]["G011"] == "E2"
    assert out["revealed_names"]["G011"] == "赵玉山"
    assert logged[0]["changes"][0] == {"code": "NEW_ANCHOR_REBOUND", "groups": {"G011": "E2"}}


def test_name_absent_from_every_future_evidence_is_left_for_the_hard_failure(monkeypatch) -> None:
    """编造的真名不得降级放行：一条 future 证据都不含真名时原样交给校验硬失败（既有 RCA 回归钉死）。"""
    monkeypatch.setattr(rebind, "log_provider_call", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不该记账")))
    evidence = {"E1": {"origin": "future", "text": "这少年虎头虎脑，大声开口。"}, "E0": {"origin": "current", "text": "赵玉山走了进来。"}}
    ctx = _context("这少年虎头虎脑，大声开口。", evidence, {"G011": ["E1", "E0"]})
    payload = _payload("G011", "赵玉山", "E1")
    assert rebind.rebind_or_defer_missing_anchor(payload, ctx) is payload


def test_properly_anchored_new_and_non_new_decisions_are_untouched(monkeypatch) -> None:
    monkeypatch.setattr(rebind, "log_provider_call", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不该记账")))
    evidence = {"E2": {"origin": "future", "text": "“我叫赵玉山！”那少年挺起胸膛。"}}
    ctx = _context("“我叫赵玉山！”那少年挺起胸膛。", evidence, {"G011": ["E2"], "G012": ["E2"]})
    payload = _payload("G011", "赵玉山", "E2")
    payload["decisions"]["G012"] = "F:G012"; payload["revealed_names"]["G012"] = ""; payload["reveal_evidence_ids"]["G012"] = ""; payload["revealed_name_kinds"]["G012"] = ""
    assert rebind.rebind_or_defer_missing_anchor(payload, ctx) is payload
