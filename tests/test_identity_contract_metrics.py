"""WS14：v6 当前身份契约硬失败按规则计数，观测台可见——只观测，不改判定。

``app/portraits/identity_degrade.py::record_current_identity_hard_fail_metrics``
在 ``discovery_legacy.py::validate_current_response`` 收集
``_project_current_identity_response`` 返回的 ``errors`` 处调用（同一份
errors，不重算），按结构前缀分类写入
``app.observability.metrics.inc("identity_contract_hard_fail_total", rule=...,
project_id=..., episode_no=..., batch_index=...)``。这里验证两件事：

- 分类表能命中 5 类已知规则的真实错误文案（前缀逐字取自各自的
  ``errors.append(...)`` 调用处，不是猜中文语义），未知文案落 ``other``；
- 计数只读 ``errors``、不改变 ``_project_current_identity_response`` 的判定
  结果——P token 复用缺失 / absorbed_functional_keys 越界仍然硬失败，
  source_label 重复的可降级形状仍然合并成功、``errors == []``、零计数。

夹具复用 ``tests/test_identity_contract_degrade.py`` 与
``tests/test_character_discovery.py`` 里已经钉死的 B 真实响应片段，不重新
造一遍判定逻辑的回归覆盖。
"""
from __future__ import annotations

from app import portraits
from app.portraits import identity_degrade
from app.portraits.identity_literal_evidence import named_literal_miss_verdict


def _evidence(*texts: str) -> dict[str, dict]:
    records = portraits._current_identity_evidence_records("\n\n".join(texts))
    return {f"E{index:03d}": record for index, record in enumerate(records, start=1)}


# ---------------------------------------------------------------------------
# 规则分类表：结构前缀匹配，不猜中文语义
# ---------------------------------------------------------------------------


def test_rule_classification_hits_each_known_prefix() -> None:
    cases = {
        "current 后续batch的同称谓必须用P token显式复用 prior group：队友": "p_token_reuse_missing",
        "current K decision absorbed_functional_keys 越界：K:E011->['王婆子']": "absorbed_keys_out_of_bounds",
        (
            "current named 缺少逐字 owned evidence：武大（所选证据里没有它，"
            "本批另有 2 条证据逐字含它，无法确定该改绑哪一条）"
        ): "owned_evidence_missing",
        "source_label 重复：陶总；冲突内容并排对比：[]": "source_label_duplicate",
        "current 同一 source_label 对应多个 identity_group：雨馨": "authority_multi_group",
    }
    for text, expected_rule in cases.items():
        assert identity_degrade._current_identity_hard_fail_rule(text) == expected_rule


def test_rule_classification_unknown_text_falls_back_to_other() -> None:
    for text in (
        "current identity root keys 非闭合",
        "current identity k decisions 过多（99 条，上限 10）",
        "current F evidence_ref 越界：E9",
        "functional_identity_key 为空：门卫",
        "",
    ):
        assert identity_degrade._current_identity_hard_fail_rule(text) == "other"


# ---------------------------------------------------------------------------
# record_current_identity_hard_fail_metrics：写指标、维度正确、空输入零调用
# ---------------------------------------------------------------------------


def test_record_metrics_calls_inc_once_per_error_with_scope_dimensions(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        identity_degrade, "inc",
        lambda metric, **labels: calls.append({"metric": metric, **labels}),
    )

    identity_degrade.record_current_identity_hard_fail_metrics(
        [
            "current 后续batch的同称谓必须用P token显式复用 prior group：队友",
            "source_label 重复：陶总；冲突内容并排对比：[]",
            "some brand new wording nobody classified yet",
        ],
        project_id="proj-1",
        episode_no=3,
        batch_index=2,
    )

    assert len(calls) == 3
    assert [c["rule"] for c in calls] == [
        "p_token_reuse_missing", "source_label_duplicate", "other",
    ]
    for call in calls:
        assert call["metric"] == "identity_contract_hard_fail_total"
        assert call["project_id"] == "proj-1"
        assert call["episode_no"] == 3
        assert call["batch_index"] == 2


def test_record_metrics_missing_project_id_defaults_to_empty_string(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(identity_degrade, "inc", lambda metric, **labels: calls.append(labels))
    identity_degrade.record_current_identity_hard_fail_metrics(
        ["source_label 重复：X；冲突内容并排对比：[]"],
        project_id=None, episode_no=1, batch_index=1,
    )
    assert calls[0]["project_id"] == ""


def test_record_metrics_empty_errors_makes_zero_calls(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(identity_degrade, "inc", lambda metric, **labels: calls.append(labels))
    identity_degrade.record_current_identity_hard_fail_metrics(
        [], project_id="p", episode_no=1, batch_index=1,
    )
    assert calls == []


# ---------------------------------------------------------------------------
# 端到端：真实 B 夹具驱动 _project_current_identity_response，判定与计数同时
# 核验——硬失败的两类仍然硬失败，可降级的一类仍然降级、不计数。
# ---------------------------------------------------------------------------


def test_p_token_reuse_missing_still_hard_fails_and_is_counted(monkeypatch) -> None:
    """跑不快的孩子 ep1 batch2，ERR-20260903-91dce2/26bce0（B 两次撞同一形态）：
    与 tests/test_identity_contract_degrade.py::
    test_duiyou_p_token_missing_stays_fatal_not_degraded 同一夹具——WS5「不
    降级」的判定本次不动，这里额外验证 errors 落地处会计数一次
    p_token_reuse_missing。"""
    evidence_by_ref = _evidence("队友们在场边为他鼓掌。")
    payload = {"k": [], "n": [], "f": [{
        "evidence_ref": "E001", "source_label": "队友",
        "functional_identity_key": "队友", "kind": "mentioned",
    }]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        prior_functional_groups={
            "P:F:priorbatchtoken": {
                "decision_id": "P:F:priorbatchtoken",
                "identity_group": "current-0:F1",
                "existing_route_name": "",
                "source_labels": ["队友"],
                "response_group_keys": [],
            },
        },
        reserved_authority_labels=set(),
        group_scope="current-2",
        existing_functional_routes=set(),
    )
    assert any("必须用P token显式复用" in message for message in errors)  # 判定未变

    calls: list[dict] = []
    monkeypatch.setattr(identity_degrade, "inc", lambda metric, **labels: calls.append(labels))
    identity_degrade.record_current_identity_hard_fail_metrics(
        errors, project_id="p", episode_no=1, batch_index=2,
    )
    assert any(c["rule"] == "p_token_reuse_missing" for c in calls)


def test_absorbed_keys_out_of_bounds_still_hard_fails_and_is_counted(monkeypatch) -> None:
    """金瓶梅词话 ep1，ERR-20260901-1ac3fa：与
    tests/test_identity_contract_degrade.py::
    test_wangpozi_forged_absorbed_token_stays_fatal_not_degraded 同一夹具。"""
    evidence_by_ref = _evidence("武大郎与王婆立在门前说话。")
    payload = {"k": [{
        "decision_id": "K:E001:placeholder",
        "kind": "onscreen",
        "absorbed_functional_keys": ["王婆子"],
    }], "f": [], "n": []}
    known = {
        "K:E001:placeholder": {
            "source_label": "武大郎", "canonical_name": "武大郎",
            "evidence_ref": "E001", "decision_type": "registered_authority",
            "known_authority": True, "materialization_compatible": True,
        },
    }
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known,
        reserved_authority_labels={"武大郎"},
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any("absorbed_functional_keys 越界" in message for message in errors)

    calls: list[dict] = []
    monkeypatch.setattr(identity_degrade, "inc", lambda metric, **labels: calls.append(labels))
    identity_degrade.record_current_identity_hard_fail_metrics(
        errors, project_id="p", episode_no=1, batch_index=1,
    )
    assert any(c["rule"] == "absorbed_keys_out_of_bounds" for c in calls)


def test_source_label_duplicate_ambiguous_home_still_hard_fails_and_is_counted(
    monkeypatch,
) -> None:
    """与 tests/test_character_discovery.py::
    test_declared_repeat_label_with_ambiguous_literal_home_still_hard_fails
    同一夹具：申报签名一致但称谓在批里确有逐字出处、只是没被引用到，改绑存在
    真正歧义，identity_degrade 不得合并，必须继续硬失败——本次改动不动这条
    红灯，只加一次计数。"""
    text = "一个男子站在桥头。\n\n远处又有一个男子骑马而来。\n\n孟浩打了个哈欠。"
    evidence_by_ref = _evidence(text)
    payload = {"k": [], "n": [], "f": [
        {"evidence_ref": "E003", "source_label": "男子", "functional_identity_key": "F3", "kind": "mentioned"},
        {"evidence_ref": "E003", "source_label": "男子", "functional_identity_key": "F3", "kind": "mentioned"},
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any("source_label 重复" in message for message in errors)

    calls: list[dict] = []
    monkeypatch.setattr(identity_degrade, "inc", lambda metric, **labels: calls.append(labels))
    identity_degrade.record_current_identity_hard_fail_metrics(
        errors, project_id="p", episode_no=1, batch_index=1,
    )
    assert any(c["rule"] == "source_label_duplicate" for c in calls)


def test_source_label_duplicate_degrades_zero_hit_makes_zero_calls(monkeypatch) -> None:
    """与 tests/test_identity_contract_degrade.py::
    test_taozong_huangzong_declared_repeat_zero_hit_degrades_and_merges 同一
    夹具：申报签名一致且称谓全批无逐字出处，identity_degrade 按模型申报合并，
    errors == []——判定继续降级，且降级路径不产生任何计数（不是把"没有硬
    失败"也算一次事件）。"""
    evidence_by_ref = _evidence(
        "陶经理在会议室里发言，众人对他毕恭毕敬。",
        "散会后有人低声提起过去的往事。",
    )
    payload = {"k": [], "f": [], "n": [
        {"evidence_ref": "E001", "identity_label": "陶总", "kind": "mentioned", "name_kind": "honorific"},
        {"evidence_ref": "E002", "identity_label": "陶总", "kind": "mentioned", "name_kind": "honorific"},
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []  # 判定未变：仍然降级成功

    calls: list[dict] = []
    monkeypatch.setattr(identity_degrade, "inc", lambda metric, **labels: calls.append(labels))
    identity_degrade.record_current_identity_hard_fail_metrics(
        errors, project_id="p", episode_no=1, batch_index=1,
    )
    assert calls == []


def test_wire_schema_violation_falls_back_to_other(monkeypatch) -> None:
    """F evidence_ref 越界（供应商违反自己声明的 enum 契约）不是 5 类业务规则
    之一，落 other——与 tests/test_character_discovery.py::
    test_current_identity_ambiguous_ref_still_fails_closed 同一夹具。"""
    evidence_by_ref = {
        "E001": {"text": "绿袍修士站在山门。", "evidence_id": "e1"},
        "EE01": {"text": "许师姐收起风幡。", "evidence_id": "e2"},
    }
    response = portraits.CurrentIdentityCandidateResponse.model_validate({
        "k": [], "n": [],
        "f": [{
            "evidence_ref": "E9", "source_label": "绿袍修士",
            "functional_identity_key": "F1", "kind": "onscreen",
        }],
    })
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any("evidence_ref 越界" in message for message in errors)

    calls: list[dict] = []
    monkeypatch.setattr(identity_degrade, "inc", lambda metric, **labels: calls.append(labels))
    identity_degrade.record_current_identity_hard_fail_metrics(
        errors, project_id="p", episode_no=1, batch_index=1,
    )
    assert any(c["rule"] == "other" for c in calls)


def test_owned_evidence_missing_classification_via_named_literal_miss_verdict() -> None:
    """owned_evidence_missing 对应 ``named_literal_miss_verdict`` 的"多条命中"
    防御分支（见 app/portraits/identity_literal_evidence.py 与
    tests/test_identity_literal_evidence.py::
    test_multi_match_says_the_binding_is_ambiguous_not_fabricated）。9e95380
    之后 ``append_candidate`` 的 named 分支总是先 ``literal_rebind_target``
    改绑，真实响应已经绕不到这条分支（B 上 ERR-20260902-0c4f46/6ca183/c587ac、
    ERR-20260901-ca388b/0fbce1 共 5 次历史失败，全部发生在那次修复之前）——
    这里只验证分类表命中它的字面前缀，不重建已经堵死的整条管线。"""
    evidence = {
        "E001": {"text": "這武松因酒醉，打了童樞密。"},
        "E002": {"text": "只見武大買了些肉菜、果餅歸來。"},
        "E003": {"text": "武大自依前上街賣炊餅。"},
    }
    message = named_literal_miss_verdict("武大", evidence)
    assert message is not None
    assert identity_degrade._current_identity_hard_fail_rule(message) == "owned_evidence_missing"
