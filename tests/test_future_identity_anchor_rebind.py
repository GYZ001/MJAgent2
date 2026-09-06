"""N: 决议缺逐字真名锚点的确定性改写：改绑 / 后端切共现窗口 / 无共现降 F: / 编造真名仍硬失败。"""
from __future__ import annotations

from types import SimpleNamespace

from app.portraits import future_identity_anchor_rebind as rebind


def _context(future_text: str, evidence: dict[str, dict], groups: dict[str, list[str]], labels: dict[str, list[str]] | None = None):
    decisions = {}
    for group_key in groups:
        decisions[f"N:{group_key}"] = {"decision_id": f"N:{group_key}", "group_key": group_key, "resolution_kind": "new_named"}
        decisions[f"F:{group_key}"] = {"decision_id": f"F:{group_key}", "group_key": group_key, "resolution_kind": "functional"}
    return SimpleNamespace(
        group_keys=list(groups), decision_by_id=decisions, evidence_by_id=dict(evidence),
        evidence_ids_by_group={k: list(v) for k, v in groups.items()}, future_text=future_text,
        group_specs=[{"group_key": k, "source_labels": (labels or {}).get(k, [])} for k in groups],
    )


def _payload(group_key: str, name: str, evidence_id: str) -> dict:
    return {
        "decisions": {group_key: f"N:{group_key}"}, "revealed_names": {group_key: name},
        "reveal_evidence_ids": {group_key: evidence_id}, "revealed_name_kinds": {group_key: "personal_name"},
    }


def test_wrong_evidence_index_is_rebound_to_the_group_window_that_names_the_person(monkeypatch) -> None:
    logged: list[dict] = []
    monkeypatch.setattr(rebind, "log_provider_call", lambda *a, **kw: logged.append(kw["meta"]))
    future_text = "这少年虎头虎脑，大声开口。……“我叫赵玉山！”那少年挺起胸膛。"
    evidence = {"E1": {"origin": "future", "text": "这少年虎头虎脑，大声开口。"}, "E2": {"origin": "future", "text": "“我叫赵玉山！”那少年挺起胸膛。"}}
    ctx = _context(future_text, evidence, {"G011": ["E1", "E2"]})
    out = rebind.rebind_or_defer_missing_anchor(_payload("G011", "赵玉山", "E1"), ctx)
    assert out["decisions"]["G011"] == "N:G011" and out["reveal_evidence_ids"]["G011"] == "E2"
    assert logged[0]["changes"][0] == {"code": "NEW_ANCHOR_REBOUND", "groups": {"G011": "E2"}}


def test_label_and_name_cooccurring_in_future_text_get_a_backend_window(monkeypatch) -> None:
    """第 10 集形状：本组窗口按「曹某」检索、不含「曹阳」；future_text 里两者在 120 字内共现 → 后端切窗登记并改绑。"""
    monkeypatch.setattr(rebind, "log_provider_call", lambda *a, **kw: None)
    future_text = "山门前一片喧哗。" * 10 + "“曹某在此恭候多时。”来人正是曹阳，他抱拳一礼。" + "众人散去。" * 10
    evidence = {"E1": {"origin": "future", "text": "山门前一片喧哗。山门前一片喧哗。"}}
    ctx = _context(future_text, evidence, {"G004": ["E1"]}, labels={"G004": ["曹某"]})
    out = rebind.rebind_or_defer_missing_anchor(_payload("G004", "曹阳", "E1"), ctx)
    new_id = out["reveal_evidence_ids"]["G004"]
    assert new_id.startswith("E:anchor:") and new_id in ctx.evidence_ids_by_group["G004"]
    entry = ctx.evidence_by_id[new_id]
    assert entry["origin"] == "future" and "曹某" in entry["text"] and "曹阳" in entry["text"]
    assert entry["text"] == future_text[entry["start_offset"]:entry["end_offset"]] and len(entry["text"]) <= 120
    assert out["decisions"]["G004"] == "N:G004" and out["revealed_names"]["G004"] == "曹阳"


def test_name_in_future_text_without_cooccurrence_is_deferred_to_functional(monkeypatch) -> None:
    monkeypatch.setattr(rebind, "log_provider_call", lambda *a, **kw: None)
    future_text = "曹阳独自站在山顶。" + "风声呼啸。" * 40 + "外宗弟子们议论纷纷。"
    evidence = {"E1": {"origin": "future", "text": "外宗弟子们议论纷纷。"}}
    ctx = _context(future_text, evidence, {"G004": ["E1"]}, labels={"G004": ["曹某"]})
    out = rebind.rebind_or_defer_missing_anchor(_payload("G004", "曹阳", "E1"), ctx)
    assert out["decisions"]["G004"] == "F:G004"
    assert out["revealed_names"]["G004"] == "" and out["reveal_evidence_ids"]["G004"] == "" and out["revealed_name_kinds"]["G004"] == ""


def test_fabricated_name_absent_from_future_text_is_left_for_the_hard_failure(monkeypatch) -> None:
    monkeypatch.setattr(rebind, "log_provider_call", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不该记账")))
    evidence = {"E1": {"origin": "future", "text": "这少年虎头虎脑，大声开口。"}}
    ctx = _context("这少年虎头虎脑，大声开口。", evidence, {"G011": ["E1"]}, labels={"G011": ["少年"]})
    payload = _payload("G011", "赵玉山", "E1")
    assert rebind.rebind_or_defer_missing_anchor(payload, ctx) is payload
    # evidence_id 为空也不代填（既有硬失败）
    payload_empty = _payload("G011", "少年", "")
    ctx2 = _context("这少年虎头虎脑，大声开口。", evidence, {"G011": ["E1"]}, labels={"G011": ["少年"]})
    assert rebind.rebind_or_defer_missing_anchor(payload_empty, ctx2) is payload_empty


def test_properly_anchored_and_non_new_decisions_are_untouched(monkeypatch) -> None:
    monkeypatch.setattr(rebind, "log_provider_call", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不该记账")))
    evidence = {"E2": {"origin": "future", "text": "“我叫赵玉山！”那少年挺起胸膛。"}}
    ctx = _context("“我叫赵玉山！”那少年挺起胸膛。", evidence, {"G011": ["E2"], "G012": ["E2"]})
    payload = _payload("G011", "赵玉山", "E2")
    payload["decisions"]["G012"] = "F:G012"; payload["revealed_names"]["G012"] = ""; payload["reveal_evidence_ids"]["G012"] = ""; payload["revealed_name_kinds"]["G012"] = ""
    assert rebind.rebind_or_defer_missing_anchor(payload, ctx) is payload
