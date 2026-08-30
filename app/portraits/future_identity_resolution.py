"""未来章节身份候选的解析：resolve_future_identity_candidates 单一巨函数，
按集判定待揭示身份是否已在未来文本中出现。
"""

from __future__ import annotations

import json

from app.evidence import repository as evidence_repository
from app import hiagent
from app.errors import ContentGenerationError
from app.schemas import Bible
from app.source_excerpt import SourceSegment

from ._identity_tokens import _identity_source_label_has_list_separator
from .constants import (
    CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    DURABLE_IDENTITY_DECISION_PROVENANCE,
    FUTURE_IDENTITY_DECISION_VERSION,
    IDENTITY_NAME_FORM_PERSONAL,
    IDENTITY_NAME_FORM_RULE,
    IDENTITY_REQUEST_MAX_TOKENS,
    IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH,
    REISSUE_KNOWN_RESOLUTION_KIND,
)
from .discovery_resample import (
    _bounded_owned_identity_evidence,
    _canonical_named_authority_id,
    _identity_operation_retry_epoch,
    _identity_structured_with_resample,
)
from .identity_schemas import (
    FutureIdentityCandidateResponse,
    _future_identity_schema,
    _identity_strict_response_format,
)

async def resolve_future_identity_candidates(
    candidates: list[dict],
    *,
    source_text: str,
    future_text: str,
    bible: Bible,
    episode_no: int,
    future_label: str = "",
) -> list[dict]:
    """Resolve current unresolved identity groups from bounded future evidence.

    The provider never copies a label, authority, group or evidence quote on
    this wire.  It selects one backend-owned decision token per exact group
    key.  Only a genuinely new name remains open text, and that name must be
    anchored verbatim in one backend-owned raw-future evidence span.
    """
    unresolved_onscreen_groups = {
        str(item.get("identity_group") or "").strip()
        for item in candidates
        if item.get("identity_kind") == "functional"
        and item.get("source_label_provenance")
        != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        and item.get("kind") == "onscreen"
        and str(item.get("identity_group") or "").strip()
    }
    unresolved = [
        dict(item) for item in candidates
        if item.get("identity_kind") == "functional"
        and item.get("source_label_provenance")
        != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        and (
            item.get("kind") == "onscreen"
            or str(item.get("identity_group") or "").strip()
            in unresolved_onscreen_groups
            or str(item.get("source_label") or "").strip() in future_text
        )
    ]
    if not unresolved or not str(future_text or "").strip():
        return candidates
    known_names = [
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
    ]
    authority_by_id: dict[str, dict] = {}
    for name in known_names:
        authority_by_id[f"bible:{name}"] = {
            "authority_id": f"bible:{name}",
            "canonical_name": name,
            "identity_group": "",
            "aliases": [],
            "materialization_compatible": True,
        }
    for candidate in candidates:
        if str(candidate.get("identity_kind") or "") != "named":
            continue
        canonical_name = str(candidate.get("name") or "").strip()
        if not canonical_name:
            continue
        identity_group = str(candidate.get("identity_group") or "").strip()
        # Every named candidate which can authorize a future alias must converge
        # on the same final card authority.  The resolution is persisted only
        # after ``ensure_character_card`` succeeds, so this does not claim a
        # durable Bible identity before materialization.
        authority_id = str(candidate.get("authority_id") or "").strip()
        if not authority_id:
            authority_id = _canonical_named_authority_id(canonical_name)
        candidate_materialization_compatible = bool(
            authority_id == _canonical_named_authority_id(canonical_name)
            and identity_group in {"", authority_id}
            and candidate.get("materialization_compatible", True)
        )
        authority = authority_by_id.setdefault(authority_id, {
            "authority_id": authority_id,
            "canonical_name": canonical_name,
            "identity_group": identity_group,
            "aliases": [],
            "materialization_compatible": candidate_materialization_compatible,
        })
        if authority["canonical_name"] != canonical_name:
            raise ContentGenerationError(
                f"identity authority={authority_id} 对应多个真名"
            )
        source_label = str(candidate.get("source_label") or "").strip()
        if source_label and source_label not in authority["aliases"]:
            authority["aliases"].append(source_label)
        # An authority assembled from several backend routes is safe to
        # materialize only when every origin converges on the final Bible
        # authority/group.  A Bible entry must not mask a durable alias whose
        # origin group is incompatible with that card authority.
        authority["materialization_compatible"] = bool(
            authority.get("materialization_compatible", True)
            and candidate_materialization_compatible
        )
    authority_projection = list(authority_by_id.values())

    # 真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性排查命中：这里原本按裸
    # source_label 键控（label_to_group: dict[str, str]），跟 _project_
    # current_identity_response 单批内的 (source_label, scope_qualifier)
    # 复合键判定不一致——上游合法放行的"两个外宗弟子"（不同 scope_
    # qualifier、不同 identity_group）流到这里，因为只看裸 label 又被判成
    # "同一称谓对应多个身份组"重新拦下。键升级为复合键；raw_group 的兜底
    # 生成也带上 qualifier，避免两个原本不同的人在都没有 identity_group 时
    # 被兜底成同一个 group（"label:外宗弟子"），把两个人的 candidates 揉
    # 到一起。
    raw_groups: dict[str, dict] = {}
    label_to_group: dict[tuple[str, str], str] = {}
    for candidate in unresolved:
        source_label = str(candidate.get("source_label") or "").strip()
        if not source_label:
            raise ContentGenerationError(
                "future identity candidate 缺少 source_label"
            )
        scope_qualifier = str(candidate.get("scope_qualifier") or "").strip()
        raw_group = str(candidate.get("identity_group") or "").strip()
        if not raw_group:
            raw_group = (
                f"label:{source_label}:{scope_qualifier}"
                if scope_qualifier else f"label:{source_label}"
            )
        previous_group = label_to_group.setdefault(
            (source_label, scope_qualifier), raw_group,
        )
        if previous_group != raw_group:
            raise ContentGenerationError(
                "future identity 同一称谓对应多个身份组："
                f"{source_label}"
            )
        group = raw_groups.setdefault(raw_group, {
            "identity_group": raw_group,
            "labels": [],
            "candidates": [],
        })
        if source_label not in group["labels"]:
            group["labels"].append(source_label)
        group["candidates"].append(candidate)

    group_specs: list[dict] = []
    for index, group in enumerate(raw_groups.values(), start=1):
        group_specs.append({
            **group,
            "group_key": f"G{index:03d}",
        })
    group_keys = [str(group["group_key"]) for group in group_specs]

    # Evidence IDs always resolve to an exact raw-future span.  Current-tail
    # context is shown separately for semantic handoff, but can never be cited
    # as the owned evidence which authorizes a decision.
    # Use one overlap policy across the complete raw future source.  Applying
    # overlap only inside a long balanced quotation leaves ordinary 120-char
    # segment boundaries able to split a <=16-char name.  A 32-char overlap
    # guarantees every allowed label/name is complete in at least one window.
    future_segments = [
        SourceSegment(
            segment_id=f"FUTURE:E{index + 1}",
            text=future_text[offset:offset + 120],
            start_offset=offset,
            end_offset=min(len(future_text), offset + 120),
        )
        for index, offset in enumerate(range(0, len(future_text), 88))
        if future_text[offset:offset + 120]
    ]
    evidence_by_id: dict[str, dict] = {}
    evidence_ids_by_group: dict[str, list[str]] = {}
    # 事故 RCA（EP2「绿袍男子」误并入「李富贵」）：当某个标签在整段未来文本
    # 里从未逐字出现，下面的 else 分支盲抓未来文本开头约 900 字符作为该组
    # 的证据窗口，内容与该标签毫无关系——纯属兜底，只是为了让 N: 分支（发现
    # 新真名）仍有文本可看。这样取得的窗口绝不能被当成"这就是该标签的身份
    # 证据"去背书任何 K: 决议：窗口里偶然出现的任何已登记角色别名/真名都只
    # 是巧合共现，不是该标签与那个角色同一人的证据。用这个集合记录哪些组是
    # 纯兜底取得证据，供下面铸造决议时拒绝为它们产出 K: 选项。
    fallback_evidence_group_keys: set[str] = set()
    per_group_budget = min(
        1800,
        max(
            120,
            CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET // max(1, len(group_specs)),
        ),
    )
    for group in group_specs:
        group_key = str(group["group_key"])
        group_labels = [str(value) for value in group["labels"]]
        label_source_indexes = {
            index for index, segment in enumerate(future_segments)
            if any(label in segment.text for label in group_labels)
        }
        if label_source_indexes:
            context_source_indexes = {
                neighbor
                for index in label_source_indexes
                for neighbor in (index - 1, index, index + 1)
                if 0 <= neighbor < len(future_segments)
            }
            context_source_indexes.add(len(future_segments) - 1)
            context_source_indexes.update(
                index for index, segment in enumerate(future_segments)
                if any(name in segment.text for name in known_names)
            )
            matching = [
                segment for index, segment in enumerate(future_segments)
                if index in context_source_indexes
            ]
        else:
            fallback_evidence_group_keys.add(group_key)
            matching = [
                segment for segment in future_segments
                if segment.start_offset < 900
            ]
        label_window_indexes = {
            index for index, segment in enumerate(matching)
            if any(label in segment.text for label in group_labels)
        }
        adjacent_label_window_indexes = {
            neighbor
            for index in label_window_indexes
            for neighbor in (index - 1, index + 1)
            if 0 <= neighbor < len(matching)
        }
        ranked = sorted(
            enumerate(matching),
            key=lambda item: (
                0 if item[0] in label_window_indexes else (
                    1 if item[0] in adjacent_label_window_indexes else 2
                ),
                -sum(name in item[1].text for name in known_names),
                0 if item[0] == 0 else 1,
                0 if item[0] == len(matching) - 1 else 1,
                item[1].start_offset,
            ),
        )
        selected: list = []
        used = 0
        max_windows = max(1, min(6, per_group_budget // 120))
        for _rank, segment in ranked:
            if used >= per_group_budget or len(selected) >= max_windows:
                break
            if segment.text in {item.text for item in selected}:
                continue
            if selected and used + len(segment.text) > per_group_budget:
                continue
            selected.append(segment)
            used += len(segment.text)
        selected.sort(key=lambda item: item.start_offset)
        group_evidence_ids: list[str] = []
        for segment in selected:
            evidence_id = "E:" + evidence_repository.content_hash({
                "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
                "origin": "future",
                "source_hash": evidence_repository.content_hash(future_text),
                "start_offset": segment.start_offset,
                "end_offset": segment.end_offset,
                "text": segment.text,
            })[:20]
            evidence_by_id.setdefault(evidence_id, {
                "evidence_id": evidence_id,
                "origin": "future",
                "start_offset": segment.start_offset,
                "end_offset": segment.end_offset,
                "text": segment.text,
            })
            group_evidence_ids.append(evidence_id)
        evidence_ids_by_group[group_key] = list(dict.fromkeys(
            group_evidence_ids
        ))

    named_authorities_by_identity_group: dict[str, set[str]] = {}
    for candidate in candidates:
        if str(candidate.get("identity_kind") or "") != "named":
            continue
        # Same-group K is a recovery authority, not a way for two values from
        # the same provider response to certify each other.  A free functional
        # key may collide with a named group string; only an explicitly durable
        # backend decision may authorize this shortcut.
        if str(candidate.get("decision_provenance") or "").strip() not in (
            DURABLE_IDENTITY_DECISION_PROVENANCE
        ):
            continue
        identity_group = str(candidate.get("identity_group") or "").strip()
        canonical_name = str(candidate.get("name") or "").strip()
        if not identity_group or not canonical_name:
            continue
        named_authorities_by_identity_group.setdefault(
            identity_group, set()
        ).add(_canonical_named_authority_id(canonical_name))

    # An authority which already stands on this episode's stage under its own
    # name cannot be revealed by a later window that merely mentions that name:
    # that is co-occurrence ("A talked about B"), and the unresolved label is
    # then someone else.  An authority with no independent named presence here
    # has no such alternative reading, and a future window naming it is the
    # only way this episode can learn who the label is.
    episode_named_authorities = {
        str(candidate.get("authority_id") or "").strip()
        or _canonical_named_authority_id(str(candidate.get("name") or ""))
        for candidate in candidates
        if str(candidate.get("identity_kind") or "") == "named"
        and str(candidate.get("name") or "").strip()
    }

    decision_by_id: dict[str, dict] = {}
    decision_ids_by_group: dict[str, list[str]] = {}
    for group in group_specs:
        group_key = str(group["group_key"])
        functional_id = f"F:{group_key}"
        decision_by_id[functional_id] = {
            "decision_id": functional_id,
            "group_key": group_key,
            "resolution_kind": "functional",
        }
        decision_ids = [functional_id]
        # 兜底证据不得为 K 决议背书（见上面 fallback_evidence_group_keys 的
        # 注释）：这个组的证据窗口和它的标签毫无逐字关联，窗口里出现的任何
        # 已登记权威的别名/真名都只是巧合共现，不是"这个组就是那个人"的
        # 证据。可选项在这里被硬性收窄到只剩 F:（证据不足）与下面的 N:
        # （若窗口内确实首次揭示了新真名）——不做成"允许但弱置信度标注"，
        # 因为一旦选项出现在 schema 枚举里，模型就可能选中它，且后续任何
        # 环节都无法再用"这是不是兜底窗口"这条信息去否决一个已经铸造出的
        # decision_id。
        group_label_texts = [
            str(value).strip()
            for value in group.get("labels") or []
            if str(value).strip()
        ]
        if group_key not in fallback_evidence_group_keys:
            for authority_id, authority in authority_by_id.items():
                canonical_name = str(
                    authority.get("canonical_name") or ""
                ).strip()
                # K decisions need an anchor the backend can bind to this
                # group's own evidence: a registered non-canonical alias, an
                # authority already bound to this exact current identity
                # group, or -- for an authority not otherwise present in this
                # episode -- its canonical name.  Excluding the canonical name
                # outright was the production defect: every Bible-seeded
                # authority starts with an empty alias list, so no K decision
                # was ever minted, "this group is an already-registered
                # person" became unrepresentable, and the run died on rule 5
                # instead.
                registered_aliases = [
                    str(value).strip()
                    for value in authority.get("aliases") or []
                    if str(value).strip()
                    and str(value).strip() != canonical_name
                ]
                same_group_authority = authority_id in (
                    named_authorities_by_identity_group.get(
                        str(group.get("identity_group") or ""), set()
                    )
                )
                if same_group_authority:
                    proof_anchors = [
                        str(value) for value in group.get("labels") or []
                    ]
                    proof_kind = "same_group_authority"
                else:
                    canonical_anchor = (
                        [canonical_name]
                        if canonical_name
                        and authority_id not in episode_named_authorities
                        else []
                    )
                    proof_anchors = list(dict.fromkeys([
                        *registered_aliases,
                        *canonical_anchor,
                    ]))
                    proof_kind = (
                        "registered_alias" if registered_aliases
                        else "canonical_name"
                    )
                if not proof_anchors:
                    continue
                # 锚定窗口必须同时含有本组自己的标签。这个组的证据目录并不只
                # 装"提到本组标签"的窗口：上面选窗时还把"提到任一已登记真名"
                # 的窗口一并收了进来（供 N: 分支看新名字）。那些窗口跟本组标签
                # 毫无逐字关联，窗口里出现的权威名字只是巧合共现。
                #
                # 真实故障 ERR-20260828-e65955（《罗刹海市》EP1）：G001 的标签是
                # 「父亲」，铸出 K:G001:bible:马骥 的那扇窗口是"三天之内，马骥
                # 遍游各处海国。从此，「龙媒」的名声传遍四海"——整扇窗口没有
                # 「父亲」二字，只因含有马骥的本集别名「龙媒」就成了锚点。模型
                # 选中它，「父亲」就此成为马骥的已登记称谓；同一次运行的后一批
                # current 识别正确地把「父亲」判成 functional，撞上「不得冒用已
                # 登记身份称谓」，整集失败且重试必然复现。
                #
                # fallback_evidence_group_keys 拦的是同一件事，但它的判据挂在
                # 「整个未来文本里出没出现过这个标签」这个组级信号上：标签只要
                # 在别处出现过一次，全组的窗口就都成了合法锚点。判据下沉到每扇
                # 窗口自己。same_group_authority 分支的 proof_anchors 本来就是
                # 这批标签，这条要求对它恒真。
                anchored_evidence_ids = []
                for evidence_id in evidence_ids_by_group[group_key]:
                    window_text = str(
                        evidence_by_id.get(evidence_id, {}).get("text") or ""
                    )
                    if not any(
                        anchor and anchor in window_text
                        for anchor in proof_anchors
                    ):
                        continue
                    if not any(
                        label in window_text for label in group_label_texts
                    ):
                        continue
                    anchored_evidence_ids.append(evidence_id)
                if not anchored_evidence_ids:
                    continue
                known_hash = evidence_repository.content_hash({
                    "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
                    "group_key": group_key,
                    "authority_id": authority_id,
                    "evidence_ids": anchored_evidence_ids,
                })[:12]
                known_id = f"K:{group_key}:{authority_id}:{known_hash}"
                decision_by_id[known_id] = {
                    "decision_id": known_id,
                    "group_key": group_key,
                    "resolution_kind": "known_named",
                    "authority_id": authority_id,
                    "canonical_name": canonical_name,
                    "evidence_ids": anchored_evidence_ids,
                    "materialization_compatible": bool(
                        authority.get("materialization_compatible")
                    ),
                    "proof_kind": proof_kind,
                    "proof_anchors": proof_anchors,
                }
                decision_ids.append(known_id)
        if evidence_ids_by_group[group_key]:
            new_id = f"N:{group_key}"
            decision_by_id[new_id] = {
                "decision_id": new_id,
                "group_key": group_key,
                "resolution_kind": "new_named",
            }
            decision_ids.append(new_id)
        decision_ids_by_group[group_key] = decision_ids

    identity_schema = _future_identity_schema(
        group_keys,
        decision_ids_by_group=decision_ids_by_group,
        evidence_ids_by_group=evidence_ids_by_group,
    )
    identity_response_format = _identity_strict_response_format(
        identity_schema,
        name="screenplay_future_identity_resolution_v10",
    )
    group_projection = [
        {
            "group_key": group["group_key"],
            "identity_group": group["identity_group"],
            "source_labels": group["labels"],
        }
        for group in group_specs
    ]
    decision_projection = [
        decision for decision in decision_by_id.values()
    ]
    evidence_projection = [
        evidence for evidence in evidence_by_id.values()
    ]
    current_boundary = str(source_text or "").strip()[-700:]
    prompt = f"""任务：只为当前集尚未确认的身份组做后续姓名消歧。
未决身份组（group_key 是唯一可输出的键）：
{json.dumps(group_projection, ensure_ascii=False, separators=(',', ':'))}
已有人物权威目录（只读）：
{json.dumps(authority_projection, ensure_ascii=False, separators=(',', ':'))}
后续证据目录（{future_label or '后续章节'}；evidence_id 对应未改写的原文连续片段）：
{json.dumps(evidence_projection, ensure_ascii=False, separators=(',', ':'))}
可选决议目录（已将 group/authority/evidence 组合绑定为不透明 decision_id）：
{json.dumps(decision_projection, ensure_ascii=False, separators=(',', ':'))}
当前章末交接上下文（仅供推理，绝不是可选 evidence_id）：
{current_boundary or '（无）'}
规则：
1. decisions/revealed_names/reveal_evidence_ids/revealed_name_kinds 四个对象都必须精确输出全部
   group_key，不得增删键。
2. 证据不足时选 F: 决议，这是合法终态；此时两个侧载字段都必须是空字符串。
3. 证据显示该组就是「已有人物权威目录」中的人时，只能选对应那名 authority 的 K: 决议；
   该 token 已绑定 authority 与原文证据，两个侧载字段都必须为空；
   若目录里没有为该组与该 authority 列出 K: 决议，则只能选 F: 决议。
   K: 成立的判据是证据原文把这个称谓和那个人写成同一个人：同一个动作或同一段
   经历换着两种称呼来写、有人点破「原来他就是某某」、或此人自述。称谓与那个
   人的名字同时出现在一段话里不算数——两人对答、一方提起另一方、并列出场、
   一方是另一方的亲属或下属，都是两个人同时在场，这种情况选 F: 决议。
4. 只有证据目录首次逐字揭示了不在已有权威目录中的稳定真名，才能选 N: 决议；
   revealed_names 写真名，reveal_evidence_ids 选包含该真名的 evidence_id，
   revealed_name_kinds 写 personal_name。
   {IDENTITY_NAME_FORM_RULE}
   「某师姐」「某爷」「某掌柜」这类姓氏或关系加称呼是 honorific，不是真名：
   这种情况选 F: 决议，四个对象里除 decisions 外都写空字符串。
   非 N: 决议的组，revealed_names/reveal_evidence_ids/revealed_name_kinds 三项都必须是空字符串。
5. 不得回抄或改写证据文本，不得为已有权威重新签发新名，不得输出只在后续出场的人。
只输出符合下列 Schema 的 JSON：
{json.dumps(identity_schema, ensure_ascii=False, separators=(',', ':'))}"""

    def normalize_identity_payload(payload: dict) -> dict:
        """Route a NEW answer to the decision its own evidence supports.

        Two deterministic rewrites, both before validation:

        * a NEW whose declared form is not a personal name is demoted to this
          group's functional decision -- 真名 > 尊称 > 代称, and only a real
          name may mint a new authority; and

        * a NEW that actually names an already-registered person is rewritten
          onto that person's own backend decision.

        The provider sometimes expresses "this group is that already-registered
        person" with the N token plus that person's existing canonical name.
        When the backend has itself already minted a K decision for the exact
        same (group, authority, evidence) tuple, the answer carries every fact
        the K decision requires and differs only in which token was written --
        so it is canonicalised onto the backend's own token instead of failing
        the episode.  Without a matching backend decision nothing is rewritten
        and the NEW rule stays fail-closed: this can never bind an authority
        the backend has not already anchored in this group's own evidence.
        """
        if not isinstance(payload, dict):
            return payload
        decisions = payload.get("decisions")
        revealed_names = payload.get("revealed_names")
        reveal_evidence_ids = payload.get("reveal_evidence_ids")
        if not (
            isinstance(decisions, dict)
            and isinstance(revealed_names, dict)
            and isinstance(reveal_evidence_ids, dict)
        ):
            return payload
        name_kinds = payload.get("revealed_name_kinds")
        if not isinstance(name_kinds, dict):
            name_kinds = {}
        rewritten: dict[str, str] = {}
        for group_key in group_keys:
            selected = decision_by_id.get(str(decisions.get(group_key) or ""))
            if (
                selected is None
                or str(selected.get("resolution_kind") or "") != "new_named"
            ):
                continue
            if str(
                name_kinds.get(group_key) or ""
            ) != IDENTITY_NAME_FORM_PERSONAL:
                # 真名 > 尊称 > 代称：只有真名可以签发新的人物权威。尊称与代称
                # （以及没有明确声明形态的情况）确定性降级为功能身份，本组仍然是
                # 一个独立身份，等真名出现在证据里再由 K 决议认领同一个人。
                functional_id = f"F:{group_key}"
                if functional_id in decision_by_id:
                    rewritten[group_key] = functional_id
                continue
            canonical_name = str(
                revealed_names.get(group_key) or ""
            ).strip()
            evidence_id = str(
                reveal_evidence_ids.get(group_key) or ""
            ).strip()
            if not canonical_name or not evidence_id:
                continue
            # An ambiguous name matches no single authority: fail closed.
            authority_ids = [
                authority_id
                for authority_id, authority in authority_by_id.items()
                if str(
                    authority.get("canonical_name") or ""
                ).strip() == canonical_name
            ]
            if len(authority_ids) != 1:
                continue
            known = next(
                (
                    decision for decision in decision_by_id.values()
                    if str(decision.get("group_key") or "") == group_key
                    and str(
                        decision.get("resolution_kind") or ""
                    ) == "known_named"
                    and str(
                        decision.get("authority_id") or ""
                    ) == authority_ids[0]
                    and evidence_id in (decision.get("evidence_ids") or [])
                ),
                None,
            )
            if known is not None:
                rewritten[group_key] = str(known["decision_id"])
                continue
            # 归一规则（真实第26轮 EP5 回归 ERR-20260824-88ece5）：门禁立意
            # （防重复铸造身份）本身没错，错的是对"多报"的响应形态。
            # authority_ids 唯一命中只证明"这个真名字符串对应项目里唯一
            # 一个已有身份"，还不足以证明"这个 group 真的是那个人"——两个
            # 回归夹具（"三哥"被声称是"陈三"、"小胖子"被声称是"李富贵"，
            # 但各自的 future_text 里那个真名压根不存在，是纯粹的臆断/
            # 嫁接昵称）证明 authority_ids 唯一命中单独作为归一条件太松，
            # 必须补一条真正的"确定性一致性比对"：真名整体是否至少在这个
            # 组能看到的完整 future_text 里逐字出现过——不要求命中后端
            # 预先按 proof_anchors 筛出的那个更窄的 evidence 子集（那正是
            # 要豁免的负担：已知身份的锚点在它初次签发时已经验过，不需要
            # 精确复现是哪一条 evidence_id 命中的），只要求这个名字本身
            # 真的出现在模型能看到的原文里，不是凭一个昵称/称谓单方面嫁接。
            # 按门禁不对称教义（缺失致命、冗余归一、矛盾致命）：
            #   - authority_ids 唯一命中 + 真名整体逐字出现在 future_text
            #     = 冗余，归一为对既有身份的引用（见 REISSUE_KNOWN_
            #     RESOLUTION_KIND 分支在 validate_response/response_
            #     decisions 里的处理）；
            #   - authority_ids 命中多个（len!=1，上面已经 continue 掉）
            #     = 矛盾，同一个真名字符串被项目内不同的 authority_id
            #     分别持有，无法确定性判断该并入哪一个；
            #   - authority_ids 唯一命中但真名整体不在 future_text 里
            #     = 同样按矛盾/不相容处理，维持原始 NEW 校验路径不动
            #     （不重写，交给下面 validate_response 的既有 NEW 规则
            #     去拦——它本就会因为"不得重新签发已有 authority"报错）。
            if canonical_name not in future_text:
                continue
            reissue_id = f"{REISSUE_KNOWN_RESOLUTION_KIND}:{group_key}:{authority_ids[0]}"
            if reissue_id not in decision_by_id:
                reissue_authority = authority_by_id.get(authority_ids[0], {})
                decision_by_id[reissue_id] = {
                    "decision_id": reissue_id,
                    "group_key": group_key,
                    "resolution_kind": REISSUE_KNOWN_RESOLUTION_KIND,
                    "authority_id": authority_ids[0],
                    "canonical_name": canonical_name,
                    "materialization_compatible": bool(
                        reissue_authority.get("materialization_compatible")
                    ),
                }
            rewritten[group_key] = reissue_id
        if not rewritten:
            return payload
        return {
            **payload,
            "decisions": {**decisions, **rewritten},
            "revealed_names": {
                **revealed_names,
                **{group_key: "" for group_key in rewritten},
            },
            "reveal_evidence_ids": {
                **reveal_evidence_ids,
                **{group_key: "" for group_key in rewritten},
            },
            "revealed_name_kinds": {
                **name_kinds,
                **{group_key: "" for group_key in rewritten},
            },
        }

    def response_decisions(
        value: FutureIdentityCandidateResponse,
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

    def validate_response(
        value: FutureIdentityCandidateResponse,
    ) -> list[str]:
        errors: list[str] = []
        expected_keys = set(group_keys)
        maps = {
            "decisions": value.decisions,
            "revealed_names": value.revealed_names,
            "reveal_evidence_ids": value.reveal_evidence_ids,
            "revealed_name_kinds": value.revealed_name_kinds,
        }
        for field_name, values in maps.items():
            actual_keys = set(values)
            if actual_keys != expected_keys:
                errors.append(
                    f"future identity {field_name} keys 不闭合"
                )
        existing_identity_names = {
            name
            for authority in authority_by_id.values()
            for name in (
                str(authority.get("canonical_name") or ""),
                *[
                    str(value)
                    for value in authority.get("aliases") or []
                ],
            )
            if name
        }
        for group_key in group_keys:
            selected_id = str(value.decisions.get(group_key) or "")
            selected = decision_by_id.get(selected_id)
            if (
                selected is None
                or str(selected.get("group_key") or "") != group_key
            ):
                errors.append(
                    f"future identity decision_id 越界：{group_key}"
                )
                continue
            canonical_name = str(
                value.revealed_names.get(group_key) or ""
            )
            evidence_id = str(
                value.reveal_evidence_ids.get(group_key) or ""
            )
            resolution_kind = str(selected.get("resolution_kind") or "")
            declared_form = str(
                value.revealed_name_kinds.get(group_key) or ""
            )
            if resolution_kind != "new_named":
                if canonical_name or evidence_id or declared_form:
                    errors.append(
                        "future identity 非 NEW 决议侧载必须为空："
                        f"{group_key}"
                    )
                if resolution_kind == "known_named":
                    authority = authority_by_id.get(
                        str(selected.get("authority_id") or "")
                    )
                    selected_evidence_ids = [
                        str(value)
                        for value in selected.get("evidence_ids") or []
                    ]
                    proof_kind = str(selected.get("proof_kind") or "")
                    proof_anchors = [
                        str(value)
                        for value in selected.get("proof_anchors") or []
                        if str(value)
                    ]
                    same_group_authority = str(
                        selected.get("authority_id") or ""
                    ) in named_authorities_by_identity_group.get(
                        str(
                            next(
                                (
                                    group.get("identity_group")
                                    for group in group_specs
                                    if group.get("group_key") == group_key
                                ),
                                "",
                            )
                        ),
                        set(),
                    )
                    if (
                        authority is None
                        or proof_kind not in {
                            "registered_alias",
                            "canonical_name",
                            "same_group_authority",
                        }
                        or (
                            proof_kind == "same_group_authority"
                            and not same_group_authority
                        )
                        or not proof_anchors
                        or not selected_evidence_ids
                        or any(
                            value not in evidence_ids_by_group.get(
                                group_key, []
                            )
                            for value in selected_evidence_ids
                        )
                        or not any(
                            anchor
                            and anchor in str(
                                evidence_by_id.get(value, {}).get("text")
                                or ""
                            )
                            for value in selected_evidence_ids
                            for anchor in proof_anchors
                        )
                    ):
                        errors.append(
                            "future identity known 缺少后端登记的权威锚点："
                            f"{group_key}"
                        )
                elif resolution_kind == REISSUE_KNOWN_RESOLUTION_KIND:
                    # 归一分支（第26轮 ERR-20260824-88ece5，见
                    # normalize_identity_payload 上方完整说明）：已知身份
                    # 不需要重新锚定真名——锚点在它初次签发时已经验过。
                    # 只做最基本的健全性检查：authority_id 必须真的存在于
                    # 权威目录（防止归一逻辑自身出 bug 生造一个不存在的
                    # authority_id），不重新要求 proof_anchors/evidence_id
                    # 命中——那正是这条归一规则要豁免的负担。
                    if str(selected.get("authority_id") or "") not in authority_by_id:
                        errors.append(
                            "future identity reissue 指向不存在的 authority："
                            f"{group_key}"
                        )
                continue
            if canonical_name != canonical_name.strip() or not canonical_name:
                errors.append(
                    f"future identity NEW 真名无效：{group_key}"
                )
            if len(canonical_name) > IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH:
                errors.append(
                    f"future identity NEW 真名过长：{group_key}"
                )
            if _identity_source_label_has_list_separator(canonical_name):
                errors.append(
                    f"future identity NEW 真名不得包含身份列表分隔符：{group_key}"
                )
            if declared_form != IDENTITY_NAME_FORM_PERSONAL:
                errors.append(
                    "future identity NEW 只能签发真名（真名 > 尊称 > 代称）："
                    f"{group_key}"
                )
            if canonical_name in existing_identity_names:
                errors.append(
                    "future identity NEW 不得重新签发已有 authority："
                    f"{group_key}"
                )
            if evidence_id not in evidence_ids_by_group.get(group_key, []):
                errors.append(
                    f"future identity NEW evidence_id 越界：{group_key}"
                )
                continue
            evidence = evidence_by_id.get(evidence_id, {})
            evidence_text = str(evidence.get("text") or "")
            if (
                evidence.get("origin") != "future"
                or evidence_text not in future_text
                or canonical_name not in evidence_text
            ):
                errors.append(
                    f"future identity NEW 缺少逐字真名锚点：{group_key}"
                )
        return errors
    identity_provider, identity_model, identity_effective_max = (
        hiagent.text_request_token_limits(
            requested_max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        )
    )
    identity_semantic_settings = hiagent.text_request_semantic_settings(
        identity_provider
    )
    operation_id = (
        "screenplay.identity.future.v11:"
        + evidence_repository.content_hash({
            "episode_no": episode_no,
            "provider": identity_provider,
            "model": identity_model,
            "requested_max_tokens": 4096,
            "effective_max_tokens": identity_effective_max,
            "temperature": 0.1,
            "provider_semantic_settings": identity_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
            "messages": [{"role": "user", "content": prompt}],
            "output_schema": identity_schema,
            "response_format": identity_response_format,
            "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
        })
    )

    response = await _identity_structured_with_resample(
        [{"role": "user", "content": prompt}],
        model_type=FutureIdentityCandidateResponse,
        validate=validate_response,
        operation_id_for_attempt=lambda resample_attempt: (
            operation_id
            if not resample_attempt
            else f"{operation_id}:resample:{resample_attempt}"
        ),
        max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        temperature=0.1,
        format_retry_limit=0,
        semantic_retry_limit=0,
        call_meta={
            "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
            "provider": identity_provider,
            "model": identity_model,
            "effective_max_tokens": identity_effective_max,
            "provider_semantic_settings": identity_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
            "stage": "discover_character_candidates",
            "stage_key": "screenplay_character_discovery",
            "substage": "future_identity",
            "discovery_phase": "future_identity",
            "episode_no": episode_no,
            "reuse_successful_operation": False,
            "disable_provider_retries": True,
            "disable_provider_candidate_fallback": True,
            "disable_reasoning_fallback": True,
            "schema_hash": evidence_repository.content_hash(identity_schema),
            "decision_catalog_hash": evidence_repository.content_hash(
                decision_projection
            ),
            "evidence_catalog_hash": evidence_repository.content_hash(
                evidence_projection
            ),
        },
        repair_context=json.dumps(
            {
                "groups": group_projection,
                "decisions": decision_projection,
                "evidence": evidence_projection,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        output_schema=identity_schema,
        response_format=identity_response_format,
        require_response_format=True,
        normalize_payload=normalize_identity_payload,
    )

    # 真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性排查命中：resolved_by_group
    # 直接按 identity_group（真正唯一键，见 response_decisions 上方注释）
    # 取解析结果，不再经过裸 source_label 的两跳字典推导式（原设计里两个
    # 不同的人共享同一个裸标签时，Python 字典推导式会静默用后者覆盖前者，
    # 两个人的解析结果被悄悄合并成一个——"外宗弟子"甲乙正是这个形状）。
    _decisions, resolved_by_group = response_decisions(response)
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

