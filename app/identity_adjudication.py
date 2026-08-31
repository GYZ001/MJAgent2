"""Conditional AI adjudication for ambiguous screenplay IR identities."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.errors import ContentGenerationError
from app.harness import model_gateway
from app.identity_authority import (
    identity_authority_registry,
    identity_resolution_is_authoritative,
    normalize_character_resolution,
)
from app.portraits.card_owner import bible_known_labels
from app.portraits.card_rebind import rebind_character_card
from app.schemas import Bible, EpisodeScreenplay
from app.screenplay_ir import (
    ScreenplayGenerationIR,
    ScreenplayIRIdentityConflictError,
    IRIdentity,
    IRScene,
    prepare_ir_identity_authorities,
)
from app.source_excerpt import index_source_segments


IDENTITY_ADJUDICATOR_VERSION = "screenplay-ir-identity-adjudicator.v2"


class IdentityAdjudicationDecision(BaseModel):
    identity_key: str
    status: Literal[
        "bind", "new_functional", "new_named", "insufficient_evidence",
    ]
    authority_id: str = ""
    canonical_name: str = ""
    evidence_source_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class IdentityAdjudicationResult(BaseModel):
    decisions: list[IdentityAdjudicationDecision] = Field(default_factory=list)


class IdentityAdjudicationIssue(BaseModel):
    """Minimal semantic issue surface exposed across the model boundary."""

    identity_key: str = ""
    identity_keys: list[str] = Field(default_factory=list)
    reason: str = ""
    candidate_authority_ids: list[str] = Field(default_factory=list)


def _identity_source_evidence(
    candidate: ScreenplayGenerationIR,
    source_text: str,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    segments = index_source_segments(source_text)
    segment_text = {segment.segment_id: segment.text for segment in segments}
    owned: defaultdict[str, list[str]] = defaultdict(list)

    def add(identity_key: str, source_ids: list[str]) -> None:
        if not identity_key:
            return
        for source_id in source_ids:
            if source_id in segment_text and source_id not in owned[identity_key]:
                owned[identity_key].append(source_id)

    for scene in candidate.scenes:
        scene_source_ids = list(dict.fromkeys(
            source_id
            for unit in scene.units
            for source_id in unit.source_segment_ids
        ))
        for identity_key in scene.character_keys:
            add(identity_key, scene_source_ids)
        for unit in scene.units:
            if unit.speaker_key:
                add(unit.speaker_key, unit.source_segment_ids)
    for event in candidate.events:
        for identity_key in [
            *event.actor_keys,
            *event.target_keys,
            *event.perceivable_by,
        ]:
            if identity_key != "audience":
                add(identity_key, event.source_segment_ids)

    for identity in candidate.identities:
        tokens = [
            str(value or "").strip()
            for value in [identity.display_name, *identity.source_names]
            if str(value or "").strip()
        ]
        for segment in segments:
            if any(token in segment.text for token in tokens):
                add(identity.key, [segment.segment_id])
    return dict(owned), segment_text


def _adjudication_payload(
    candidate: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    owned, segment_text = _identity_source_evidence(candidate, source_text)
    authority_registry = identity_authority_registry(
        bible,
        episode.get("character_resolutions") or [],
    )
    valid_authority_ids = {
        str(item.get("authority_id") or "").strip()
        for item in authority_registry
        if str(item.get("authority_id") or "").strip()
    }
    issue_keys = {
        str(key or "").strip()
        for issue in issues
        for key in [
            issue.get("identity_key"),
            *(issue.get("identity_keys") or []),
        ]
        if str(key or "").strip()
    }
    model_issues = [
        IdentityAdjudicationIssue(
            identity_key=str(issue.get("identity_key") or "").strip(),
            identity_keys=[
                key
                for value in issue.get("identity_keys") or []
                if (key := str(value or "").strip())
            ],
            reason=str(issue.get("reason") or "").strip(),
            candidate_authority_ids=[
                authority_id
                for value in issue.get("candidate_authority_ids") or []
                if (
                    (authority_id := str(value or "").strip())
                    in valid_authority_ids
                )
            ],
        ).model_dump(mode="json", exclude_defaults=True)
        for issue in issues
    ]
    relevant_source_ids = list(dict.fromkeys(
        source_id
        for identity_key in issue_keys
        for source_id in owned.get(identity_key, [])
    ))
    identities = [
        {
            "key": identity.key,
            "display_name": identity.display_name,
            "source_names": list(identity.source_names),
            "role_type": identity.role_type,
            "authority_id": (
                "" if identity.key in issue_keys else identity.authority_id
            ),
            "rationale": identity.rationale,
            "owned_source_ids": owned.get(identity.key, []),
        }
        for identity in candidate.identities
    ]
    return {
        "contract_version": IDENTITY_ADJUDICATOR_VERSION,
        "episode_id": str(episode.get("id") or ""),
        "issues": model_issues,
        "identities": identities,
        "authority_registry": authority_registry,
        "source_segments": [
            {"source_segment_id": source_id, "text": segment_text[source_id]}
            for source_id in relevant_source_ids
            if source_id in segment_text
        ],
    }


def _prompt(payload: dict[str, Any]) -> str:
    return f"""任务：仲裁剧本 IR 中无法由精确 authority_id 绑定的人物身份。

你只能根据输入中的人物身份合同、原文来源段、角色圣经权威和已有预检证据判断；
不得根据姓名形状、职业、服饰、年龄、性别、称号后缀或任何固定词表猜测。

规则：
1. 每个 issues 涉及的 identity_key 都必须且只能输出一个 decision。
2. 若证据确认它属于 authority_registry 中某个实体，status=bind，authority_id 必须逐字引用该项。
   identities 中留空的 authority_id 表示未决输入，不构成任何既有身份权威；若本集原文首次揭晓与已登记 canonical_name 不同的稳定真名，canonical_name 须逐字来自 owned SRC，否则留空、不得猜测。
3. 若原文唯一明确给出稳定真名且 registry 尚无对应项，status=new_named，canonical_name 必须逐字来自 owned SRC；
   后端先完成最小文字卡，再签发 bible:<name> authority。
4. 若原文确认它是独立出场/开口实体，但 registry 尚无对应项，status=new_functional，
   canonical_name 优先逐字引用 source segment；若同一原文称谓明确指向多个实体，必须逐字沿用
   该 identity 当前的 display_name 作为区分显示名，后端负责生成不同的稳定 ID，禁止另造称谓。
5. 若证据不能唯一判断，status=insufficient_evidence，禁止猜测或按字面相似合并。
6. evidence_source_ids 只能引用输入 source_segments，且必须真正支持身份结论。
   每个 decision 只能从该 identity 的 owned_source_ids 中选择；若 owned_source_ids 为空，
   必须 status=insufficient_evidence，禁止借用其他 identity 的来源段。
7. 同一 authority_id 表示同一实体；两个可区分实体不得因共享泛称而合并。

输入：
{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}

只输出 JSON：
{{"decisions":[{{"identity_key":"IR identity.key","status":"bind|new_named|new_functional|insufficient_evidence","authority_id":"bind 时填写已有 ID，否则空串","canonical_name":"new_named/new_functional 时填写 owned SRC 逐字称谓；bind 时若本集首次揭晓与已登记 canonical_name 不同的真名则逐字填写，否则可空","evidence_source_ids":["SRC0001"],"rationale":"基于哪些原文动作、对白或同一性证据"}}]}}"""


def _source_segment_document_order(source_text: str) -> dict[str, int]:
    """Return the document-order rank of every source segment id.

    This is the single ordering authority shared with the read fence in
    ``portraits._identity_adjudication_receipt_is_valid`` (which requires the
    receipt/source ids to equal the ``index_source_segments`` document subset
    order).  Every producer field derived from ``evidence_source_ids`` sorts by
    this rank so the persisted order matches the consumer's strong fence.
    """
    return {
        segment.segment_id: index
        for index, segment in enumerate(index_source_segments(source_text))
    }


def _order_source_ids_by_document(
    source_ids: list[str],
    document_order: dict[str, int],
) -> list[str]:
    """Sort validated source ids into consumer document order (no set change)."""
    return sorted(source_ids, key=lambda source_id: document_order[source_id])


def _validate_decisions(
    result: IdentityAdjudicationResult,
    *,
    payload: dict[str, Any],
    source_text: str,
) -> dict[str, IdentityAdjudicationDecision]:
    issue_keys = {
        str(key or "").strip()
        for issue in payload["issues"]
        for key in [
            issue.get("identity_key"),
            *(issue.get("identity_keys") or []),
        ]
        if str(key or "").strip()
    }
    decisions: dict[str, IdentityAdjudicationDecision] = {}
    valid_authorities = {
        str(item.get("authority_id") or "")
        for item in payload["authority_registry"]
    }
    source_by_id = {
        item["source_segment_id"]: item["text"]
        for item in payload["source_segments"]
    }
    identity_by_key = {
        str(item.get("key") or "").strip(): item
        for item in payload["identities"]
    }
    document_order = _source_segment_document_order(source_text)
    for decision in result.decisions:
        key = decision.identity_key.strip()
        if key not in issue_keys or key in decisions:
            raise ContentGenerationError(
                "人物身份仲裁返回了未知或重复的 identity_key"
            )
        evidence_ids = list(dict.fromkeys(
            source_id.strip()
            for source_id in decision.evidence_source_ids
            if source_id.strip()
        ))
        if not evidence_ids or any(
            source_id not in source_by_id for source_id in evidence_ids
        ):
            raise ContentGenerationError(
                f"人物身份仲裁 {key} 缺少可验证的原文来源段"
            )
        owned_source_ids = {
            str(source_id or "").strip()
            for source_id in identity_by_key[key].get("owned_source_ids") or []
            if str(source_id or "").strip()
        }
        if any(source_id not in owned_source_ids for source_id in evidence_ids):
            raise ContentGenerationError(
                f"人物身份仲裁 {key} 引用了不属于该身份的原文来源段"
            )
        # Normalize to consumer document order once, before any downstream
        # writer derives from it.  ``resolution.evidence_source_ids``,
        # ``resolution.source_segment_ids`` and ``receipt.source_segment_ids``
        # all read ``decision.evidence_source_ids`` after this point, so the
        # producer and the read fence share one ordering authority
        # (``index_source_segments``).  This only reorders; the deduped
        # owned/known-source set stays identical.
        decision.evidence_source_ids = _order_source_ids_by_document(
            evidence_ids,
            document_order,
        )
        if decision.status == "bind":
            if decision.authority_id not in valid_authorities:
                raise ContentGenerationError(f"人物身份仲裁 {key} 引用了不存在的 authority_id")
            if (revealed_name := decision.canonical_name.strip()) and not any(revealed_name in source_by_id[s] for s in evidence_ids):
                raise ContentGenerationError(f"人物身份仲裁 {key} 声称揭晓的真名不属于所引原文")
        elif decision.status in {"new_functional", "new_named"}:
            canonical_name = decision.canonical_name.strip()
            identity_display_name = str(
                identity_by_key[key].get("display_name") or ""
            ).strip()
            if not canonical_name or not any(
                canonical_name in source_by_id[source_id]
                for source_id in evidence_ids
            ) and canonical_name != identity_display_name:
                raise ContentGenerationError(
                    f"人物身份仲裁 {key} 的功能身份称谓既不属于所引原文，"
                    "也不是当前 IR 的稳定显示名"
                )
            decision.canonical_name = canonical_name
        decisions[key] = decision
    if set(decisions) != issue_keys:
        raise ContentGenerationError(
            "人物身份仲裁没有完整覆盖全部冲突 identity_key"
        )
    return decisions


async def adjudicate_screenplay_ir_identities(
    candidate: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    persist_new_resolutions: bool = True,
) -> ScreenplayGenerationIR:
    """Resolve only semantic cases that exact structural binding cannot decide."""
    from app.portraits import screenplay_character_resolutions_for_source

    episode["character_resolutions"] = (
        screenplay_character_resolutions_for_source(
            list(episode.get("character_resolutions") or []),
            episode_no=int(episode.get("episode_no") or candidate.episode_no or 0),
            source_text=source_text,
        )
    )
    deterministic_audit: list[dict[str, Any]] = []
    _changes, issues = prepare_ir_identity_authorities(
        candidate,
        episode=episode,
        bible=bible,
        audit=deterministic_audit,
    )
    if deterministic_audit:
        candidate.normalization_log.extend(deterministic_audit)
    if not issues:
        return candidate

    payload = _adjudication_payload(
        candidate,
        episode=episode,
        source_text=source_text,
        bible=bible,
        issues=issues,
    )
    prompt = _prompt(payload)
    operation_id = "op_ir_identity_" + hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest()[:32]
    try:
        result = await model_gateway.chat_structured(
            [{"role": "user", "content": prompt}],
            model_type=IdentityAdjudicationResult,
            validate=lambda value: (
                []
                if _validate_decisions(
                    value,
                    payload=payload,
                    source_text=source_text,
                )
                else []
            ),
            operation_id=(
                f"screenplay.identity-adjudication:{IDENTITY_ADJUDICATOR_VERSION}:"
                f"{operation_id}"
            ),
            temperature=0.05,
            max_tokens=4096,
            format_retry_limit=1,
            semantic_retry_limit=1,
            call_meta={
                "stage": "screenplay_ir_identity_adjudication",
                "episode_id": str(episode.get("id") or ""),
                "operation_id": operation_id,
                "reuse_successful_operation": True,
                "adjudicator_version": IDENTITY_ADJUDICATOR_VERSION,
                "ambiguity_count": len(issues),
            },
            repair_context=json.dumps(
                payload.get("source_segments") or [],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    except model_gateway.StructuredOutputError as exc:
        # Preserve the domain-level error contract after the bounded structured
        # retry runner has exhausted its content budget.
        raise ContentGenerationError(str(exc)) from exc
    decisions = _validate_decisions(
        result,
        payload=payload,
        source_text=source_text,
    )

    unresolved = [
        decision
        for decision in decisions.values()
        if decision.status == "insufficient_evidence"
    ]
    if unresolved:
        raise ScreenplayIRIdentityConflictError(
            "人物身份 AI 仲裁确认原文证据不足："
            + "、".join(decision.identity_key for decision in unresolved),
            issues=issues,
        )

    identities_by_key = {identity.key: identity for identity in candidate.identities}
    from app.evidence import repository as evidence_repository
    from app.portraits import (
        AUTOMATIC_IDENTITY_DECISION_PROVENANCE,
        FUTURE_IDENTITY_DECISION_VERSION,
        IDENTITY_ADJUDICATION_SOURCE_PROVENANCE,
        STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        screenplay_identity_scope_fingerprint,
    )

    identity_scope_fingerprint = screenplay_identity_scope_fingerprint(
        int(episode.get("episode_no") or candidate.episode_no or 0),
        source_text,
    )
    source_hash = evidence_repository.content_hash(source_text)

    def source_receipt(source_ids: list[str]) -> dict[str, Any]:
        receipt_payload = {
            "version": IDENTITY_ADJUDICATOR_VERSION,
            "source_hash": source_hash,
            "source_segment_ids": list(source_ids),
        }
        return {
            **receipt_payload,
            "hash": evidence_repository.content_hash(receipt_payload),
        }

    new_resolutions: list[dict[str, Any]] = []
    source_by_id = {item["source_segment_id"]: item["text"] for item in payload["source_segments"]}
    for key, decision in decisions.items():
        identity = identities_by_key[key]
        if decision.status == "bind":
            identity.authority_id = decision.authority_id
            if (revealed_name := decision.canonical_name.strip()) and (from_label := decision.authority_id.removeprefix("bible:")) != decision.authority_id and from_label != revealed_name:
                await rebind_character_card(str(episode.get("project_id") or ""), from_label, revealed_name)
            continue
        if decision.status == "new_named":
            from app.portraits import ensure_character_card

            card = await ensure_character_card(
                str(episode.get("project_id") or ""),
                decision.canonical_name,
                int(episode.get("episode_no") or 1),
                generate_portrait=False, require_identity_card=True,
                identity_source_labels=identity.source_names,
            )
            if card.get("status") not in {"exists", "added", "ready", "created"}:
                raise ContentGenerationError(
                    "具名身份仲裁未能完成最小人物卡："
                    + str(card.get("reason") or card.get("status") or "unknown")
                )
            authority_id = f"bible:{decision.canonical_name}"
            identity.authority_id = authority_id
            source_label = next((
                token
                for token in [*identity.source_names, identity.display_name]
                if str(token or "").strip()
            ), decision.canonical_name)
            new_resolutions.append(normalize_character_resolution({
                "source_label": source_label,
                "canonical_name": decision.canonical_name,
                "resolution": "future_identity",
                "identity_group": authority_id,
                "authority_id": authority_id,
                "source_instance_key": authority_id,
                "reason": "AI 根据 owned SRC 确认稳定具名身份并完成最小人物卡",
                "evidence": decision.rationale[:160],
                "evidence_source_ids": decision.evidence_source_ids,
                "decision_source": IDENTITY_ADJUDICATOR_VERSION,
                "decision_provenance": (
                    AUTOMATIC_IDENTITY_DECISION_PROVENANCE
                ),
                "decision_contract_version": FUTURE_IDENTITY_DECISION_VERSION,
                "structural_identity_policy_version": (
                    STRUCTURAL_IDENTITY_COVERAGE_VERSION
                ),
                "identity_scope_fingerprint": identity_scope_fingerprint,
                "source_label_provenance": (
                    IDENTITY_ADJUDICATION_SOURCE_PROVENANCE
                ),
                "source_segment_ids": decision.evidence_source_ids,
                "identity_adjudication_receipt": source_receipt(
                    decision.evidence_source_ids
                ),
            }))
            continue
        seed = {
            "episode_id": str(episode.get("id") or ""),
            "identity_key": key,
            "canonical_name": decision.canonical_name,
            "evidence_source_ids": decision.evidence_source_ids,
        }
        authority_id = "functional:" + hashlib.sha256(
            json.dumps(
                seed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        identity.authority_id = authority_id
        source_label = next((
            token
            for token in [*identity.source_names, identity.display_name]
            if str(token or "").strip()
            and any(
                str(token).strip() in source_by_id[source_id]
                for source_id in decision.evidence_source_ids
            )
        ), decision.canonical_name)
        new_resolutions.append(normalize_character_resolution({
            "source_label": source_label,
            "canonical_name": decision.canonical_name,
            "resolution": "functional_identity",
            "identity_group": authority_id,
            "authority_id": authority_id,
            "source_instance_key": authority_id,
            "reason": "AI 根据本集原文确认独立功能身份",
            "evidence": decision.rationale[:160],
            "evidence_source_ids": decision.evidence_source_ids,
            "decision_source": IDENTITY_ADJUDICATOR_VERSION,
            "decision_provenance": AUTOMATIC_IDENTITY_DECISION_PROVENANCE,
            "decision_contract_version": FUTURE_IDENTITY_DECISION_VERSION,
            "structural_identity_policy_version": (
                STRUCTURAL_IDENTITY_COVERAGE_VERSION
            ),
            "identity_scope_fingerprint": identity_scope_fingerprint,
            "source_label_provenance": (
                IDENTITY_ADJUDICATION_SOURCE_PROVENANCE
            ),
            "source_segment_ids": decision.evidence_source_ids,
            "identity_adjudication_receipt": source_receipt(
                decision.evidence_source_ids
            ),
        }))

    if new_resolutions:
        from app.portraits import (
            merge_screenplay_character_resolutions,
            persist_screenplay_character_resolutions,
        )

        episode["character_resolutions"] = merge_screenplay_character_resolutions(
            episode.get("character_resolutions") or [],
            new_resolutions,
        )
        episode_id = str(episode.get("id") or "").strip()
        if episode_id and persist_new_resolutions:
            from app.db import get_conn

            conn = get_conn()
            row = conn.execute(
                "SELECT id FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            if row is not None:
                expected_run_id = None
                expected_revision_id = None
                try:
                    from app.observability.tracing import current_trace
                    from app.production.revision import (
                        get_active_production_revision,
                    )

                    expected_run_id = current_trace().run_id
                    active_revision = get_active_production_revision(
                        episode_id, "screenplay"
                    )
                    expected_revision_id = (
                        active_revision.id if active_revision is not None else None
                    )
                except Exception:  # noqa: BLE001 - manual callers have no run owner
                    pass
                persisted_resolutions = persist_screenplay_character_resolutions(
                    conn,
                    episode_id,
                    new_resolutions,
                    expected_active_run_id=expected_run_id,
                    expected_revision_id=expected_revision_id,
                )
                episode["character_resolutions"] = (
                    screenplay_character_resolutions_for_source(
                        persisted_resolutions,
                        episode_no=int(
                            episode.get("episode_no")
                            or candidate.episode_no
                            or 0
                        ),
                        source_text=source_text,
                    )
                )
    adjudication_audit = [{
        "path": "identities",
        "operation": "ai_identity_adjudication",
        "adjudicator_version": IDENTITY_ADJUDICATOR_VERSION,
        "operation_id": operation_id,
        "decisions": [
            decision.model_dump(mode="json")
            for decision in decisions.values()
        ],
        "reason": "exact_identity_authority_binding_was_ambiguous",
    }]
    candidate.normalization_log.extend(adjudication_audit)

    post_audit: list[dict[str, Any]] = []
    _post_changes, remaining = prepare_ir_identity_authorities(
        candidate,
        episode=episode,
        bible=bible,
        audit=post_audit,
    )
    candidate.normalization_log.extend(post_audit)
    if remaining:
        raise ScreenplayIRIdentityConflictError(
            "人物身份 AI 仲裁后仍存在无法闭合的 authority_id",
            issues=remaining,
        )
    return candidate


async def adjudicate_screenplay_document_identities(
    screenplay: EpisodeScreenplay,
    *,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
) -> list[dict[str, Any]]:
    """Resolve only typed identity references from a manual/repair Document.

    This adapter intentionally never sends the full screenplay or full chapter.
    It projects identity-bearing fields, then reuses the owned-SRC adjudicator.
    """
    from app.portraits import screenplay_character_resolutions_for_source

    episode["character_resolutions"] = (
        screenplay_character_resolutions_for_source(
            list(episode.get("character_resolutions") or []),
            episode_no=int(episode.get("episode_no") or screenplay.episode_no or 0),
            source_text=source_text,
        )
    )
    labels: list[str] = []

    def add(value: Any) -> None:
        label = str(value or "").strip()
        if label and label not in labels:
            labels.append(label)

    for scene in screenplay.scene_outline:
        for character in scene.characters:
            add(character)
    for chain in screenplay.dialogue_chains:
        for turn in chain.turns:
            add(turn.speaker)
    for voice in screenplay.voice_bible:
        add(voice.speaker_id)
    if screenplay.narrative_plan is not None:
        for contract in screenplay.narrative_plan.identity_contracts:
            add(contract.display_name)
            for voice_id in contract.voice_ids:
                add(voice_id)

    # 收敛到唯一的身份归属解析器（app.portraits.card_owner）：name + 全部 alias，
    # 不再只比对 name——只看 name 会把"人物谱里已有别名的角色"误判成"查无此人"，
    # 从而对已有角色再次触发身份仲裁 + ensure_character_card，制造第二张卡。
    known = bible_known_labels(bible)
    if screenplay.narrative_plan is not None:
        known.update(
            token
            for contract in screenplay.narrative_plan.identity_contracts
            for token in {
                str(contract.identity_id or "").strip(),
                str(contract.display_name or "").strip(),
                *(
                    str(voice_id or "").strip()
                    for voice_id in (contract.voice_ids or [])
                ),
            }
            if token
        )
    for resolution in episode.get("character_resolutions") or []:
        if (
            not isinstance(resolution, dict)
            or not identity_resolution_is_authoritative(resolution)
        ):
            continue
        known.update({
            str(resolution.get("source_label") or "").strip(),
            str(resolution.get("canonical_name") or "").strip(),
        })
    unresolved = [
        label for label in labels
        if label not in known and label in source_text
    ]
    if not unresolved:
        return list(episode.get("character_resolutions") or [])

    pseudo_identities = [
        IRIdentity(
            key=(
                "document_identity_"
                + hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]
            ),
            display_name=label,
            source_names=[label],
            authority_id="",
            role_type="functional_character",
            rationale="来自完整 Document 的 typed identity-bearing fields",
        )
        for label in unresolved
    ]
    pseudo = ScreenplayGenerationIR(
        episode_no=int(episode.get("episode_no") or screenplay.episode_no),
        source_semantics={},
        source_audit_annotations=[],
        identities=pseudo_identities,
        scenes=[IRScene(
            key="document-identity-projection",
            scene_heading="【身份投影】",
            story_function="只用于未决身份仲裁，不进入剧本正文",
            summary="完整 Document typed identity-bearing fields",
            character_keys=[identity.key for identity in pseudo_identities],
        )],
    )
    await adjudicate_screenplay_ir_identities(
        pseudo,
        episode=episode,
        source_text=source_text,
        bible=bible,
        persist_new_resolutions=True,
    )
    return list(episode.get("character_resolutions") or [])
