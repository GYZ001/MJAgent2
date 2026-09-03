"""WS5：v6 当前身份契约的定向重试评估与可见降级——跨项目验证。

B（生产）上 2026-09-01/02 全平台剧本运行失败 30 次，其中 7 次是身份契约 v6
「业务校验失败」，分布在 4 个项目、5 类规则。本文件用从 B 的 provider_calls
真实取出的模型响应片段（`response_json`，operation_id 见各测试 docstring）
重建最小复现夹具，逐条核对新代码的实际处理结果：

- 3 类规则（named 具名标签绑错证据/无出处、K decision absorbed_functional_
  keys 越界）在本次调查开始前就已被同日更早的提交（9e95380/5a7ff9b）修复，
  本文件的作用是把 B 上的真实响应片段钉成回归夹具，防止将来被悄悄改回硬失败。
- 1 类规则（P token 复用缺失）调查后发现 ``tests/test_character_discovery.py
  ::test_current_identity_cross_batch_same_label_new_group_fails_once`` 已经
  用几乎相同的真实场景锁定"必须继续硬失败"，本次不改；这里同样用 B 的真实
  响应片段钉一条对照测试，说明"跑不快的孩子" ERR-20260903-91dce2 这次失败
  是设计内行为，不是缺陷。
- 1 类规则（source_label 重复，橘座在上"陶总"/"黄总"）是本次 WS5 真正新增
  的降级，见 ``app/portraits/identity_degrade.py``。

另外两类（``authority_id 跨多个 identity_group``、场景 provenance/未解析场景
门禁）分别落在 ``app/identity_authority.py`` 与
``app/production/prep_pack/generate_once.py``，不在本次改动范围内，见任务
报告；不在本文件覆盖。
"""
from __future__ import annotations

from app import portraits


def _evidence(*texts: str) -> dict[str, dict]:
    records = portraits._current_identity_evidence_records("\n\n".join(texts))
    return {f"E{index:03d}": record for index, record in enumerate(records, start=1)}


def test_zhangfei_multi_hit_rebinds_to_first_literal_occurrence() -> None:
    """三国演义_前二十回 ep1，ERR-20260902-0c4f46/6ca183/c587ac（B
    provider_calls id=28009 等 3 次撞同一形态）：模型把「张飞」的
    identity_label 绑在不含该词的 E（对应真实事故里"姓张，名飞，字翼德"那
    一段），本批另有一条证据逐字包含"张飞"。9e95380 已把这类具名多命中改绑
    到首条逐字出处，不再整集硬失败——这里钉成回归夹具。"""
    evidence_by_ref = _evidence(
        "那人姓张，名飞，字翼德。",  # E001：不含"张飞"逐字（拆开了）
        "张飞在桃园之中朗声大笑。",  # E002：逐字含"张飞"
    )
    payload = {"k": [], "f": [], "n": [{
        "evidence_ref": "E001", "identity_label": "张飞",
        "kind": "onscreen", "name_kind": "personal_name",
    }]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    assert len(projected) == 1
    assert projected[0]["source_label"] == "张飞"
    assert (
        projected[0]["source_evidence_receipt"]["evidence_id"]
        == evidence_by_ref["E002"]["evidence_id"]
    )


def test_wuzhi_zero_hit_named_label_dropped_not_fatal() -> None:
    """金瓶梅词话 ep1，ERR-20260901-ca388b/0fbce1（B provider_calls
    id=27051）：模型按民间说法把武大写成本名「武植」，这个字符串在本批全部
    证据里一次都没有逐字出现过（真实事故：16 条证据「武大」52 次、「武植」0
    次）。5a7ff9b 已把 named 0 命中改成确定性丢弃（WARNING，不致命）——这里
    钉成回归夹具：候选被丢弃，不出现在 projected 里，errors 为空。"""
    evidence_by_ref = _evidence(
        "武大郎挑着担子走街串巷。",  # E001：不含"武植"
        "潘金莲倚门张望。",           # E002：不含"武植"
    )
    payload = {"k": [], "f": [], "n": [{
        "evidence_ref": "E001", "identity_label": "武植",
        "kind": "onscreen", "name_kind": "personal_name",
    }]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    assert projected == []


def test_wangpozi_forged_absorbed_token_stays_fatal_not_degraded() -> None:
    """金瓶梅词话 ep1，ERR-20260901-1ac3fa（B provider_calls id=27460，
    decision_id=K:E011:5a73475502c25364aceddaf2）：K 决议把「王婆子」塞进了
    absorbed_functional_keys，但这个 token 本批 f 分支没声明过、也不在前批
    P token 或本集已有 functional route 里——三类合法来源都不满足，是伪造
    token。WS5 调查过对 absorbed_functional_keys 越界做同类降级，但
    ``tests/test_character_discovery.py::
    test_current_identity_absorbed_functional_keys_rejects_forged_token`` 等
    3 个真实回归明确要求这类越界必须继续硬失败（安全默认，不得静默接受）——
    这里用 B 的真实 token 钉一条对照，确认新代码没有放松这道校验。"""
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
    assert any("王婆子" in message for message in errors)


def test_duiyou_p_token_missing_stays_fatal_not_degraded() -> None:
    """跑不快的孩子 ep1 batch2，ERR-20260903-91dce2（B provider_calls
    id=29657）：模型在 batch2 里又把「队友」声明成 functional_identity_key
    ="队友"（模型自造的、不带 P: 前缀的本批局部 key），而 batch1 已经把
    「队友」登记为一个 prior functional group。WS5 调查过对这类"称谓字面
    匹配 prior group 但没显式给 P token"做自动认领，但
    ``tests/test_character_discovery.py::
    test_current_identity_cross_batch_same_label_new_group_fails_once``
    用几乎相同的真实场景（同 source_label，下一批不给 P token）锁定"必须
    继续 StructuredSemanticError"——按标签字面自动认领会让"这次是新开一个人
    只是称谓撞车"和"这次真的是想复用前一个人"两种情况在结构上无法区分，
    P token 是模型唯一的显式确认信号。这里同样用真实响应片段钉一条对照，
    确认新代码没有放松这道校验、这次失败是设计内行为。"""
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
    assert any("必须用P token显式复用" in message for message in errors)


def test_taozong_huangzong_declared_repeat_zero_hit_degrades_and_merges() -> None:
    """橘座在上 ep6/ep10，ERR-20260901-1875be（陶总，B provider_calls
    id=27179）/ ERR-20260901-605cd1（黄总，B provider_calls id=27181）：两条
    真实生产失败都不是 F 分支模型显式声明同一 functional_identity_key，而是
    N 分支敬称（honorific，如"陶总"/"黄总"）被降级为 functional 时各自拿到
    同一个纯标签哈希（``_identity_form_functional_key`` 只认标签文本）；实测
    确认这两个称谓在各自那次请求的整份文本（含证据目录）里逐字出现次数为
    0——不是"存在但引用错位置"的可改绑歧义。WS5 新增的
    ``identity_degrade.merge_declared_functional_repeat_if_eligible`` 把这类
    "申报字段完全一致 + 全批零逐字出处"的重复声明按模型自己的判断合并为一条，
    整集不再判死；对照 `test_declared_repeat_label_with_ambiguous_literal_
    home_still_hard_fails`（标签在批里确有逐字出处、只是没被引用到）与
    `test_current_identity_declared_conflict_stays_fatal_with_side_by_side_
    diff`（同 F 键但 kind 自相矛盾）证明判定没有放松：那两种真矛盾/真歧义
    仍然维持致命。"""
    evidence_by_ref = _evidence(
        "陶经理在会议室里发言，众人对他毕恭毕敬。",  # E001：不含字面"陶总"
        "散会后有人低声提起过去的往事。",             # E002：不含字面"陶总"
    )
    payload = {"k": [], "f": [], "n": [
        {"evidence_ref": "E001", "identity_label": "陶总", "kind": "mentioned", "name_kind": "honorific"},
        {"evidence_ref": "E002", "identity_label": "陶总", "kind": "mentioned", "name_kind": "honorific"},
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    matches = [item for item in projected if item["source_label"] == "陶总"]
    assert len(matches) == 1
    assert matches[0]["_current_identity_normalized_duplicate"] is True
    assert "陶总" in matches[0]["_current_identity_degrade_note"]
    assert "全批" in matches[0]["_current_identity_degrade_note"] or "从未" in matches[0]["_current_identity_degrade_note"]
