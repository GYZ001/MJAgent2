"""把 provider 返回的当前身份候选响应投影为后端决议结构
（K/F/N 分支的核验、functional 归并越界拦截等）。
"""

from __future__ import annotations

import json

from app.evidence import repository as evidence_repository

from ._identity_tokens import (
    _identity_disambiguating_suffix,
    _identity_source_label_has_list_separator,
    _visual_entity_id_for_resolution_safe,
)
from .constants import (
    CURRENT_IDENTITY_LITERAL_PROVENANCE,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    IDENTITY_NAME_FORM_PERSONAL,
    IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH,
)
from .discovery_resample import _bounded_owned_identity_evidence
from .evidence_merge import (
    _CurrentIdentitySchemaViolation,
    _current_identity_decision_cap,
    _current_identity_disambiguation_key,
    _current_identity_receipt_sort_key,
    _current_identity_reconcile_as_single,
    _identity_form_functional_key,
    _resolved_evidence_ref,
)
from .identity_schemas import CurrentIdentityCandidateResponse

def _project_current_identity_response(
    value: CurrentIdentityCandidateResponse,
    *,
    evidence_by_ref: dict[str, dict],
    known_decisions: dict[str, dict],
    prior_functional_groups: dict[str, dict] | None = None,
    reserved_authority_labels: set[str] | None = None,
    group_scope: str,
    existing_functional_routes: set[str],
    existing_functional_route_labels: dict[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Resolve the RF10 K/N/F wire through backend-owned evidence receipts.

    Returned ``errors`` stays a flat ``list[str]`` for backward compatibility
    (every existing caller/test does ``"..." in errors`` or ``"；".join(errors)``);
    entries that violate a wire-schema-declared constraint (see
    ``_CurrentIdentitySchemaViolation``) are instances of that ``str``
    subclass instead of plain ``str`` so callers can tell them apart with
    ``_current_identity_is_schema_violation`` without touching message text.
    """
    errors: list[str] = []
    projected: list[dict] = []
    expected_refs = set(evidence_by_ref)
    if set(value.model_fields_set) != {"k", "n", "f"}:
        errors.append("current identity root keys 非闭合")
    # 第22轮总审计 ERR-20260824-aeee2d：帽子随本批 evidence ref 数量缩放，
    # 见 _current_identity_decision_cap 的完整推导。
    decision_cap = _current_identity_decision_cap(len(expected_refs))
    for branch, items in (("k", value.k), ("n", value.n), ("f", value.f)):
        if len(items) > decision_cap:
            errors.append(f"current identity {branch} decisions 过多")

    # rule 6 makes functional_identity_key the model's own explicit "this is
    # the same person" signal: two F entries that repeat both the identical
    # source_label *and* the identical functional_identity_key are the model
    # asserting one entity, not two.  That declared-repeat shape is narrower
    # than "any non-literal functional citation" -- a single, unrepeated F
    # entry whose cited E happens not to contain its label stays exactly the
    # legitimate synthetic observation prompt rule 4 describes (never
    # auto-rebound; see test_current_identity_literal_label_isolated_as_synthetic_once).
    functional_repeat_pairs: dict[tuple[str, str], int] = {}
    for item in value.f:
        pair = (
            str(item.source_label or "").strip(),
            str(item.functional_identity_key or "").strip(),
        )
        functional_repeat_pairs[pair] = functional_repeat_pairs.get(pair, 0) + 1
    declared_repeat_labels = {
        label for (label, key), count in functional_repeat_pairs.items()
        if count > 1 and label and key
    }

    # 反过来，同一个 source_label 被分给**不同**的 functional_identity_key，是
    # 模型在说「本集有好几个人都这么称呼」。这样的称谓在本集就不指向唯一身份，
    # 下面的「冒用已登记身份」判据对它不成立——那条判据默认了「称谓字面相同即
    # 身份相同」，这对真名成立，对外貌类描述不成立。
    #
    # 生产 EP1：人物谱里存着一张主名为「绿袍男子」的卡（描述性称呼建卡，本身
    # 就是上游的问题），而第 1 章原文写的是「两个穿着绿色长袍的男子」。模型判
    # 得完全正确——两条 functional，F4/F5，scope_qualifier 分别是「两个绿袍男子
    # 之一/之二」——却被按「冒用」硬失败，整集映射包卡死且重试必然再失败。
    #
    # 判据取自本次输入里模型自己的产出，不含任何词表：一个称谓是不是通称，由
    # 它在这批证据里指向几个个体决定。王有材那类真正的降级误判仍然被拦：那种
    # 情形下模型只会报一条，label 不会跨 key 复用。
    functional_keys_by_label: dict[str, set[str]] = {}
    for item in value.f:
        label = str(item.source_label or "").strip()
        key = str(item.functional_identity_key or "").strip()
        if label and key:
            functional_keys_by_label.setdefault(label, set()).add(key)
    labels_shared_across_individuals = {
        label for label, keys in functional_keys_by_label.items() if len(keys) > 1
    }

    # 同批折叠通道（absorbed_functional_keys，见设计文档 §4.2 "同批折叠
    # 通道"）需要反查每个可吸收 token 背后的 (source_label, scope_qualifier)，
    # 用于纯函数式计算该 functional 组当时会被分配到的 visual_entity_id。
    # 这里只建一份 token -> pairs 索引，不改变下面 f 循环本身的既有行为。
    batch_functional_label_sources: dict[str, list[tuple[str, str]]] = {}
    for item in value.f:
        key = str(item.functional_identity_key or "").strip()
        if not key:
            continue
        pair = (
            str(item.source_label or "").strip(),
            str(item.scope_qualifier or "").strip(),
        )
        batch_functional_label_sources.setdefault(key, []).append(pair)
    absorbable_functional_tokens = (
        set(batch_functional_label_sources)
        | set(prior_functional_groups or {})
        | set(existing_functional_routes or set())
    )

    def append_candidate(
        *,
        source_label: str,
        canonical_name: str,
        identity_kind: str,
        functional_key: str,
        kind: str,
        record: dict,
        authority_id: str = "",
        authority_group: str = "",
        known_authority: bool = False,
        materialization_compatible: bool = False,
        fixed_identity_group: str = "",
        scope_qualifier: str = "",
        functional_key_synthetic: bool = False,
    ) -> None:
        source_label = str(source_label or "")
        canonical_name = str(canonical_name or "")
        functional_key = str(functional_key or "")
        scope_qualifier = str(scope_qualifier or "").strip()
        if source_label != source_label.strip():
            errors.append(f"source_label 含首尾空白：{source_label!r}")
        if (
            not source_label
            or len(source_label) > IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH
            or _identity_source_label_has_list_separator(source_label)
        ):
            errors.append(f"source_label 非法：{source_label!r}")
        evidence_text = str(record.get("text") or "")
        literal = bool(source_label and source_label in evidence_text)
        eligible_for_rebind = identity_kind == "named" or (
            identity_kind == "functional" and source_label in declared_repeat_labels
        )
        if not literal and eligible_for_rebind and source_label:
            # 模型只是在一批 backend-owned 证据里挑下标，证据本身始终由后端拥有。
            # 一个逐字称谓如果确实逐字出现在本批另一条证据里，那是选错了 E，不是
            # 凭空捏造：直接改绑到真正承载它的那条证据，而不是让整集预检硬失败
            # （否则模型每挑错一次下标，整集剧本就必须人工重试一次）。
            # named 一直如此。functional 只在模型自己用同一 source_label +
            # 同一 functional_identity_key 重复声明「这是同一个人」时才享有同样
            # 的改绑（rule 6：不同 source_label 若明确是同一人必须共用同一 ID —
            # 同一 source_label 重复同一 ID 是更强的同一性声明）。单次、未重复的
            # 非逐字 functional 引用（如「门卫」被错误但仅一次地绑到无关证据）
            # 仍然是 prompt rule 4 允许的合法 synthetic 观察，不做改绑
            # （见 test_current_identity_literal_label_isolated_as_synthetic_once /
            # cross_f gate）。
            # 生产 EP5：两条同 key「男子」都被错误绑到了不含该词的段落，唯一真正
            # 逐字出现「男子」的段落反而没有被引用，导致本应合并的一个人被按证据
            # 分别隔离出不同 identity_group，触发 source_label 重复硬失败。
            # 只在全批唯一匹配时才自动改绑；命中多条视为歧义，不得静默挑一个可能
            # 错的目标——这种情况维持原判（named 硬失败，functional 隔离为
            # synthetic）。
            literal_matches = [
                owned
                for owned in evidence_by_ref.values()
                if source_label in str(owned.get("text") or "")
            ]
            if len(literal_matches) == 1:
                record = literal_matches[0]
                evidence_text = str(record.get("text") or "")
                literal = True
        if canonical_name != canonical_name.strip():
            errors.append(f"canonical_name 含首尾空白：{source_label}")
        if identity_kind == "named":
            if not known_authority and canonical_name != source_label:
                errors.append(
                    "current named 只允许逐字自称谓，别名必须留待 typed authority："
                    f"{source_label}->{canonical_name}"
                )
            if (
                not known_authority
                and source_label in (reserved_authority_labels or set())
            ):
                errors.append(
                    "current 已登记身份必须选择 K decision："
                    f"{source_label}"
                )
            if not literal:
                errors.append(
                    f"current named 缺少逐字 owned evidence：{source_label}"
                )
            if (
                known_authority
                and kind == "onscreen"
                and not materialization_compatible
            ):
                errors.append(
                    "current K authority 不可直接物化人物卡："
                    f"{source_label}->{canonical_name}"
                )
        if functional_key != functional_key.strip():
            errors.append(
                f"functional_identity_key 含首尾空白：{source_label}"
            )
        if identity_kind == "functional" and not functional_key:
            errors.append(f"functional_identity_key 为空：{source_label}")
        if (
            identity_kind == "functional"
            and source_label in (reserved_authority_labels or set())
            and source_label not in labels_shared_across_individuals
        ):
            errors.append(
                "current functional 不得冒用已登记身份称谓："
                f"{source_label}"
            )

        prior_functional_group = (
            (prior_functional_groups or {}).get(functional_key)
            if identity_kind == "functional"
            else None
        )
        if (
            identity_kind == "functional"
            and functional_key.startswith("P:")
            and prior_functional_group is None
        ):
            errors.append(
                f"current prior functional decision 越界：{functional_key}"
            )
        prior_groups_for_label = [
            group
            for group in (prior_functional_groups or {}).values()
            if source_label in set(group.get("source_labels") or [])
        ]
        if (
            identity_kind == "functional"
            and prior_functional_group is None
            and prior_groups_for_label
        ):
            errors.append(
                "current 后续batch的同称谓必须用P token显式复用 prior group："
                f"{source_label}"
            )
        if prior_functional_group is not None and not literal:
            errors.append(
                "current synthetic functional 不得复用 prior group："
                f"{source_label}"
            )
        existing_route_name = (
            str(prior_functional_group.get("existing_route_name") or "")
            if prior_functional_group is not None
            else (
                functional_key
                if identity_kind == "functional"
                and literal
                and functional_key in existing_functional_routes
                else ""
            )
        )
        if identity_kind == "named" and fixed_identity_group:
            identity_group = fixed_identity_group
            provenance = CURRENT_IDENTITY_LITERAL_PROVENANCE
        elif identity_kind == "named" and authority_id:
            identity_group = authority_group or authority_id
            provenance = CURRENT_IDENTITY_LITERAL_PROVENANCE
        elif identity_kind == "named":
            identity_group = (
                f"{group_scope}:named:"
                + evidence_repository.content_hash(source_label)[:16]
            )
            provenance = CURRENT_IDENTITY_LITERAL_PROVENANCE
        elif not literal:
            identity_group = (
                f"{group_scope}:synthetic:"
                + evidence_repository.content_hash({
                    "source_label": source_label,
                    "evidence_id": str(record.get("evidence_id") or ""),
                })[:16]
            )
            provenance = CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        else:
            identity_group = (
                str(prior_functional_group.get("identity_group") or "")
                if prior_functional_group is not None
                else (
                    f"existing:{existing_route_name}"
                    if existing_route_name
                    else f"{group_scope}:{functional_key}"
                )
            )
            provenance = CURRENT_IDENTITY_LITERAL_PROVENANCE
        evidence = _bounded_owned_identity_evidence(
            evidence_text,
            anchors=[source_label] if literal else [],
            max_chars=80,
        )
        if not evidence:
            evidence = evidence_text.strip()[:80]
        projected.append({
            "name": canonical_name or source_label,
            "source_label": source_label,
            "identity_kind": identity_kind,
            "identity_group": identity_group,
            "authority_id": authority_id,
            "existing_route_name": existing_route_name,
            "kind": (
                "mentioned" if kind == "mentioned" else "onscreen"
            ),
            "evidence": evidence,
            "future_evidence": "",
            "source_segment_id": str(record.get("source_segment_id") or ""),
            "source_quote": evidence_text,
            "source_label_provenance": provenance,
            "source_evidence_receipt": dict(record),
            "source_evidence_receipts": [dict(record)],
            "source_segment_ids": [str(record.get("source_segment_id") or "")],
            "_current_materialization_compatible": bool(
                materialization_compatible or not authority_id
            ),
            "materialization_compatible": bool(
                materialization_compatible or not authority_id
            ),
            "_current_response_group_key": (
                functional_key if identity_kind == "functional" else ""
            ),
            # EP4 真实回归（"同宗"，见 _current_identity_disambiguation_key
            # 完整说明）：N 分支被降级为 functional 的称谓，functional_key
            # 来自 _identity_form_functional_key 对标签文本的纯哈希，跟
            # 真正的 F 分支 functional_identity_key 不是同一类信号——前者
            # 不随"这是不是同一个人"变化，只随标签文本变化；两个不同的
            # 指称对象共用同一泛指标签时会拿到完全相同的哈希，不能当作
            # 模型已经用结构性字段区分了"这是哪个人"。仅 N 分支降级路径
            # 置位 True；真正 F 分支的 functional_key 继续保留原有强信号
            # 语义（马脸青年案②分支：同一真 F key 内部自相矛盾仍须致命）。
            "_current_identity_group_key_synthetic": bool(
                functional_key_synthetic and identity_kind == "functional"
            ),
            "scope_qualifier": scope_qualifier,
            "_typed_source_evidence_owned": True,
        })

    # 第35轮真实回归 ERR-20260824-bc3d14（EP10，李富贵）：模型在同一次响应里
    # 既在 k 里为 (source_label, evidence_ref) 正确签发了合规决议，又在 n 里
    # 重复申报同一 (identity_label, evidence_ref) 作为「新」具名声明——这是
    # 冗余回显，不是未经核验的具名注入。记录本响应内每条合规 K 决议实际锚定
    # 的 (source_label, evidence_ref) 复合键，供下面 n 循环判断是否为冗余
    # 回显；键里同时要求 label 与 evidence_ref 都一致（第35轮用例 C：同
    # label 不同 ref 不算——那种情况下 k 决议并未覆盖 n 这条具体声明所引用
    # 的证据，仍然维持硬失败，见下方 append_candidate 里 known_authority
    # 闸门）。
    redundant_n_echo_k_pairs: dict[tuple[str, str], dict] = {}
    for item in value.k:
        decision_id = str(item.decision_id or "")
        selected = known_decisions.get(decision_id)
        evidence_ref = str((selected or {}).get("evidence_ref") or "")
        record = evidence_by_ref.get(evidence_ref)
        if selected is None or record is None:
            # decision_id is enum-declared in _current_identity_schema()
            # (known_item["properties"]["decision_id"]["enum"] = decision_ids)
            # and that enum keyword survives _identity_strict_provider_schema's
            # whitelist, so it really is sent to the provider -- a decision_id
            # outside it is a wire-schema violation (selected is None).
            # `record is None` can only fire when `selected` is not None, but
            # both _current_identity_known_decision_catalog and
            # _current_identity_prior_decision_catalog only ever mint a
            # known_decisions entry by iterating evidence_by_ref.items()
            # itself, so every entry's evidence_ref is structurally guaranteed
            # to already be a key of evidence_by_ref -- that branch is dead
            # defensive code, not a second reachable failure mode, so the
            # whole check is schema-declared in practice.
            errors.append(
                _CurrentIdentitySchemaViolation(
                    f"current K decision 越界：{decision_id}"
                )
            )
            continue
        append_candidate(
            source_label=str(selected.get("source_label") or ""),
            canonical_name=str(selected.get("canonical_name") or ""),
            identity_kind="named",
            functional_key="",
            kind=item.kind,
            record=record,
            authority_id=str(selected.get("authority_id") or ""),
            authority_group=str(selected.get("identity_group") or ""),
            known_authority=bool(
                selected.get("decision_type") == "registered_authority"
                or selected.get("known_authority")
            ),
            materialization_compatible=bool(
                selected.get("materialization_compatible")
            ),
            fixed_identity_group=(
                str(selected.get("identity_group") or "")
                if selected.get("decision_type") == "prior_named"
                else ""
            ),
        )
        k_source_label = str(selected.get("source_label") or "")
        if k_source_label and evidence_ref:
            redundant_n_echo_k_pairs[(k_source_label, evidence_ref)] = projected[-1]
        absorbed_tokens = [
            token for token in (
                str(raw or "").strip()
                for raw in (item.absorbed_functional_keys or [])
            )
            if token
        ]
        if absorbed_tokens:
            invalid_tokens = [
                token for token in absorbed_tokens
                if token not in absorbable_functional_tokens
            ]
            if invalid_tokens:
                # 安全默认：核验不过就拒绝该声明（硬失败强制重采样），不得
                # 静默接受——伪造的 token 不得混入合法折叠通道。
                errors.append(
                    "current K decision absorbed_functional_keys 越界："
                    f"{decision_id}->{invalid_tokens}"
                )
            else:
                projected[-1]["_current_identity_absorbed_functional_keys"] = (
                    list(absorbed_tokens)
                )
                canonical_name = str(selected.get("canonical_name") or "").strip()
                to_visual_entity_id = (
                    _visual_entity_id_for_resolution_safe({
                        "resolution": "future_identity",
                        "canonical_name": canonical_name,
                    })
                    if canonical_name else None
                ) or (f"bible:{canonical_name}" if canonical_name else "")
                merges: list[dict] = []
                if to_visual_entity_id:
                    label_pairs: list[tuple[str, str]] = []
                    for token in absorbed_tokens:
                        label_pairs.extend(
                            batch_functional_label_sources.get(token, [])
                        )
                        prior_group = (prior_functional_groups or {}).get(token)
                        if prior_group is not None:
                            for label in prior_group.get("source_labels") or []:
                                label_pairs.append(
                                    (str(label or "").strip(), "")
                                )
                        existing_label = (
                            existing_functional_route_labels or {}
                        ).get(token)
                        if existing_label:
                            label_pairs.append((existing_label, ""))
                    seen_pairs: set[tuple[str, str]] = set()
                    for source_label_pair, scope_qualifier_pair in label_pairs:
                        if not source_label_pair:
                            continue
                        pair_key = (source_label_pair, scope_qualifier_pair)
                        if pair_key in seen_pairs:
                            continue
                        seen_pairs.add(pair_key)
                        from_visual_entity_id = (
                            _visual_entity_id_for_resolution_safe({
                                "source_label": source_label_pair,
                                "scope_qualifier": scope_qualifier_pair,
                            })
                            or ""
                        )
                        if (
                            from_visual_entity_id
                            and from_visual_entity_id != to_visual_entity_id
                        ):
                            merges.append({
                                "from_visual_entity_id": from_visual_entity_id,
                                "to_visual_entity_id": to_visual_entity_id,
                                "canonical_name": canonical_name,
                                "merge_rule": "same_batch_k_absorption",
                            })
                if merges:
                    projected[-1][
                        "_current_identity_absorbed_visual_merges"
                    ] = merges
    for item in value.n:
        evidence_ref = _resolved_evidence_ref(
            item.evidence_ref, expected_refs
        )
        record = evidence_by_ref.get(evidence_ref)
        if evidence_ref not in expected_refs or record is None:
            # evidence_ref is enum-declared on CurrentNewNamedIdentityDecision
            # in _current_identity_schema() (new_item["properties"]
            # ["evidence_ref"]["enum"] = refs) and that enum keyword is on
            # _identity_strict_provider_schema's whitelist, so it really is
            # sent to the provider -- see _resolved_evidence_ref's own
            # docstring ("The schema pins evidence_ref to a closed enum, but
            # the provider does not always honour strict mode"). expected_refs
            # == set(evidence_by_ref), so `record is None` cannot fire once
            # evidence_ref is in expected_refs; the only reachable failure
            # mode is the enum violation -- RCA ERR-20260824-e3628f (0.5%
            # supplier-side non-strict-mode sampling defect on a deep array
            # enum), safe to resample the whole episode instead of halting
            # for human RCA.
            errors.append(
                _CurrentIdentitySchemaViolation(
                    f"current N evidence_ref 越界：{evidence_ref}"
                )
            )
            continue
        identity_label = str(item.identity_label or "").strip()
        if (
            str(item.name_kind or "") != IDENTITY_NAME_FORM_PERSONAL
            and identity_label not in (reserved_authority_labels or set())
        ):
            # 尊称与代称永远不能签发新的人物权威。它们先落为功能身份，保留原文
            # 里的逐字称谓，等真名真正出现在证据中时再由 K 决议认领同一个人。
            #
            # 例外：identity_label 命中 reserved_authority_labels 时不适用这条
            # 短路。该集合只收录人物谱已登记的真名/别名，以及本集之前批次已由
            # K/N 决议确认过的身份称谓——都是经过核验的既成事实，不是模型这次
            # 现场的臆测。真名>尊称>代称这条阶梯是为了拦截模型凭空签发新人物卡
            # （生产事故：模型据"许师姐"擅自签发过一张全新人物卡），对已核验
            # 事实继续套用同一条防臆测规则就是把规则用错了对象——未登记的尊称/
            # 代称仍然一律在这里落 functional，不受影响。命中后放行到下面与
            # personal_name 相同的处理路径，由已有的 reserved_authority_labels
            # 命中逻辑（含"必须选择 K decision"的强制与 K/N 冗余回显丢弃）接管。
            append_candidate(
                source_label=identity_label,
                canonical_name="",
                identity_kind="functional",
                functional_key=_identity_form_functional_key(identity_label),
                kind=item.kind,
                record=record,
                functional_key_synthetic=True,
            )
            continue
        if identity_label in (reserved_authority_labels or set()) and not any(
            identity_label in str(owned.get("text") or "")
            for owned in evidence_by_ref.values()
        ):
            # 模型是从上下文认出了一位已登记人物，而不是读到了逐字姓名。合同要求
            # 这类「称谓 A 其实是名字 B」的判断先落为 functional，可这里没有任何
            # 逐字称谓可以留下来当 source_label。后面的结构化身份覆盖审计会用原文
            # 中真正出现的称谓把这个人补回来，所以丢弃这一条声明，而不是让整集
            # 预检硬失败。
            continue
        if identity_label in (reserved_authority_labels or set()):
            k_echo_candidate = redundant_n_echo_k_pairs.get(
                (identity_label, evidence_ref)
            )
            if k_echo_candidate is not None:
                # 第35轮 ERR-20260824-bc3d14：这条 n 声明命中的 (identity_label,
                # evidence_ref) 复合键已经在本响应的 k 数组里拿到一份合规决议
                # ——模型对同一个人签发了两份声明，k 是权威、n 是冗余回显。
                # 静默丢弃这条 n（不采信其中任何字段，包括它自己可能携带的
                # canonical_name/identity_kind 判断），身份仍以那条 K 决议为
                # 准；在 K 决议对应的候选上留一个可观测标记（风格对齐
                # _current_identity_synthesized_qualifier），供测试/回归核验
                # 丢弃确实发生，而不是让整集因模型自己已经正确处理过的人复读
                # 一次就预检硬失败。
                k_echo_candidate[
                    "_current_identity_redundant_n_echo_dropped"
                ] = True
                continue
        append_candidate(
            source_label=identity_label,
            canonical_name=identity_label,
            identity_kind="named",
            functional_key="",
            kind=item.kind,
            record=record,
        )
    for item in value.f:
        evidence_ref = _resolved_evidence_ref(
            item.evidence_ref, expected_refs
        )
        record = evidence_by_ref.get(evidence_ref)
        if evidence_ref not in expected_refs or record is None:
            # 同上 N 分支：evidence_ref 在 CurrentFunctionalIdentityDecision 上
            # 同样是 enum 声明（functional_item["properties"]["evidence_ref"]
            # ["enum"] = refs），且 enum 在 provider 白名单内，真正发给了供应商。
            # ERR-20260824-e3628f 的 F evidence_ref="E0" 就是这条分支：目录
            # 84 项、strict=true 全部具备，供应商依然低频（约 0.5%）吐出枚举外
            # 的值——是供应商侧非严格解码的格式缺陷，不是我们的信息缺口，
            # 安全可重采样整集，不需要人工 RCA。
            errors.append(
                _CurrentIdentitySchemaViolation(
                    f"current F evidence_ref 越界：{evidence_ref}"
                )
            )
            continue
        append_candidate(
            source_label=item.source_label,
            canonical_name="",
            identity_kind="functional",
            functional_key=item.functional_identity_key,
            kind=item.kind,
            record=record,
            scope_qualifier=item.scope_qualifier,
        )

    merged: list[dict] = []
    # 唯一性判定键（真实第18轮 EP10 回归 ERR-20260824-b16bb4，结构性方案 a）：
    # 复合键 (source_label, scope_qualifier)，不再是裸 source_label。关系
    # 称谓（"师弟"类）天然可以在同一章合法指向不同人——旧的裸 source_label
    # 唯一键假设对这类称谓不成立（模型行为正确，是契约键设计过窄）。
    # scope_qualifier 是模型自己按 prompt 规则8申报的区分限定语，默认空串
    # （未申报=沿用旧行为，同一 source_label 仍然只有一个唯一性域，见
    # test_two_distinct_people_same_label_different_key_still_hard_fails
    # ——那条测试没有用到 scope_qualifier，必须继续因同一复合键
    # ("男子","") 硬拒，不受本次改动影响）。判据是结构性的，不认识
    # "师弟"这个具体词形，也不需要模型说明"这是关系称谓"，模型只要在
    # 自己判断可能有歧义时给出限定语即可。
    by_label: dict[tuple[str, str], list[dict]] = {}
    for item in projected:
        key = (
            str(item.get("source_label") or "").strip(),
            str(item.get("scope_qualifier") or "").strip(),
        )
        by_label.setdefault(key, []).append(item)
    for (source_label, _scope_qualifier), options in by_label.items():
        reconciled = _current_identity_reconcile_as_single(options)
        if reconciled is not None:
            merged.append(reconciled)
            continue
        # 第31轮真实回归 ERR-20260824-614276（EP5，两条"老者"）：跟马脸
        # 青年案（申报逐字段雷同→归一合并）方向相反——这次模型用不同
        # functional_identity_key（或不同 decision_id，见
        # _current_identity_disambiguation_key）明确申报了两个人（第 5 章
        # 确实有两位老者），只是没填人类可读的 scope_qualifier，导致两条
        # 都落进同一个 (source_label, "") 复合键、彼此"撞车"。区分的事实
        # 判断模型已经做出（不同 F 键本身就是模型自己的区分信号，跟"jason
        # 逐字段雷同"是完全相反的申报形状），拒绝重来是浪费——按各子组
        # （子组内部仍然分别用①②同一套判据核验，子组内部若还自相矛盾，
        # 说明连"是不是同一个人"这个最基本的申报都不自洽，那才是真矛盾，
        # 见下方 subgroup_conflict 分支）各自的最早证据首现顺序，用第20轮
        # 既有 _identity_disambiguating_suffix 机制（甲/乙/丙...）确定性
        # 补足 scope_qualifier，标记 synthesized（观测计数），复合键唯一性
        # 随即满足，不需要模型重新申报。
        identity_subgroups: dict[str, list[dict]] = {}
        for item in options:
            identity_subgroups.setdefault(
                _current_identity_disambiguation_key(item), [],
            ).append(item)
        subgroup_conflict = False
        resolved_subgroups: list[dict] = []
        if len(identity_subgroups) > 1:
            for subgroup_options in identity_subgroups.values():
                sub_reconciled = _current_identity_reconcile_as_single(
                    subgroup_options,
                )
                if sub_reconciled is None:
                    # 同一 F 键/decision_id 内部仍然自相矛盾（马脸青年案的
                    # ②分支，本次不动）：这不是"两个人缺限定语"的形状，是
                    # 模型对同一个身份的申报本身自相矛盾，跌回下面原有的
                    # 致命反馈路径，用完整的 options（不是子组）报冲突。
                    subgroup_conflict = True
                    break
                resolved_subgroups.append(sub_reconciled)
        if len(identity_subgroups) > 1 and not subgroup_conflict:
            resolved_subgroups.sort(
                key=lambda item: _current_identity_receipt_sort_key(
                    item.get("source_evidence_receipt") or {},
                ),
            )
            for index, resolved in enumerate(resolved_subgroups, start=1):
                resolved["scope_qualifier"] = _identity_disambiguating_suffix(index)
                resolved["_current_identity_synthesized_qualifier"] = True
                merged.append(resolved)
            continue
        # ②实质分歧：申报内容本身就不一致（不同 kind、不同 functional_
        # identity_key、不同 canonical_name……），真的没法确定是不是同一个
        # 人，维持致命——但反馈必须让模型看得懂"错在哪、怎么改"，不能只
        # 甩一个错误码：把冲突的每一条内容并排列出，并给出确定性的修复
        # 指令（同一称谓指多人 -> 各自给 scope_qualifier；指同一人 -> 合并
        # 为一条、共用同一个 functional_identity_key/decision_id）。
        conflict_dump = json.dumps(
            [
                {
                    "identity_kind": item.get("identity_kind"),
                    "name": item.get("name"),
                    "kind": item.get("kind"),
                    "functional_identity_key": item.get(
                        "_current_response_group_key"
                    ),
                    "authority_id": item.get("authority_id"),
                }
                for item in options
            ],
            ensure_ascii=False, separators=(",", ":"),
        )
        errors.append(
            f"source_label 重复：{source_label}；冲突内容并排对比："
            f"{conflict_dump}；若这几条指的是不同的人，请为每一条各自的 "
            "scope_qualifier 填写能互相区分的限定语；若指的是同一个人，"
            "请合并为一条并共用同一个 functional_identity_key（f 分支）"
            "或同一个 decision_id（k 分支）"
        )
        groups = {
            str(item.get("identity_group") or "").strip() for item in options
        }
        if len(groups) > 1:
            errors.append(
                "current 同一 source_label 对应多个 identity_group："
                f"{source_label}"
            )
        merged.extend(options)
    return merged, errors

