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
    normalize_character_resolution,
)
from app.schemas import Bible, extract_json
from app.screenplay_ir import (
    ScreenplayGenerationIR,
    ScreenplayIRIdentityConflictError,
    prepare_ir_identity_authorities,
)
from app.source_excerpt import index_source_segments


IDENTITY_ADJUDICATOR_VERSION = "screenplay-ir-identity-adjudicator.v1"


class IdentityAdjudicationDecision(BaseModel):
    identity_key: str
    status: Literal["bind", "new_functional", "insufficient_evidence"]
    authority_id: str = ""
    canonical_name: str = ""
    evidence_source_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class IdentityAdjudicationResult(BaseModel):
    decisions: list[IdentityAdjudicationDecision] = Field(default_factory=list)


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
    issue_keys = {
        str(key or "").strip()
        for issue in issues
        for key in [
            issue.get("identity_key"),
            *(issue.get("identity_keys") or []),
        ]
        if str(key or "").strip()
    }
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
            "authority_id": identity.authority_id,
            "rationale": identity.rationale,
            "owned_source_ids": owned.get(identity.key, []),
        }
        for identity in candidate.identities
    ]
    return {
        "contract_version": IDENTITY_ADJUDICATOR_VERSION,
        "episode_id": str(episode.get("id") or ""),
        "issues": issues,
        "identities": identities,
        "authority_registry": identity_authority_registry(
            bible,
            episode.get("character_resolutions") or [],
        ),
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
3. 若原文确认它是独立出场/开口实体，但 registry 尚无对应项，status=new_functional，
   canonical_name 必须逐字存在于引用的 source segment；后端负责生成稳定 ID。
4. 若证据不能唯一判断，status=insufficient_evidence，禁止猜测或按字面相似合并。
5. evidence_source_ids 只能引用输入 source_segments，且必须真正支持身份结论。
6. 同一 authority_id 表示同一实体；两个可区分实体不得因共享“少年、男子、女子、师兄”等泛称而合并。

输入：
{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}

只输出 JSON：
{{"decisions":[{{"identity_key":"IR identity.key","status":"bind|new_functional|insufficient_evidence","authority_id":"bind 时填写已有 ID，否则空串","canonical_name":"new_functional 时填写原文逐字稳定称谓，否则可空","evidence_source_ids":["SRC0001"],"rationale":"基于哪些原文动作、对白或同一性证据"}}]}}"""


def _validate_decisions(
    result: IdentityAdjudicationResult,
    *,
    payload: dict[str, Any],
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
        decision.evidence_source_ids = evidence_ids
        if decision.status == "bind":
            if decision.authority_id not in valid_authorities:
                raise ContentGenerationError(
                    f"人物身份仲裁 {key} 引用了不存在的 authority_id"
                )
        elif decision.status == "new_functional":
            canonical_name = decision.canonical_name.strip()
            if not canonical_name or not any(
                canonical_name in source_by_id[source_id]
                for source_id in evidence_ids
            ):
                raise ContentGenerationError(
                    f"人物身份仲裁 {key} 的功能身份称谓不属于所引原文"
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
) -> ScreenplayGenerationIR:
    """Resolve only semantic cases that exact structural binding cannot decide."""
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
    raw = await model_gateway.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.05,
        max_tokens=4096,
        call_meta={
            "stage": "screenplay_ir_identity_adjudication",
            "episode_id": str(episode.get("id") or ""),
            "operation_id": operation_id,
            "reuse_successful_operation": True,
            "adjudicator_version": IDENTITY_ADJUDICATOR_VERSION,
            "ambiguity_count": len(issues),
        },
    )
    try:
        parsed = extract_json(raw, repair_unescaped_inner_quotes=True)
        result = IdentityAdjudicationResult.model_validate(parsed)
    except (TypeError, ValueError) as exc:
        raise ContentGenerationError(
            "人物身份仲裁返回了不可验证的结构化结果"
        ) from exc
    decisions = _validate_decisions(result, payload=payload)

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
    new_resolutions: list[dict[str, Any]] = []
    for key, decision in decisions.items():
        identity = identities_by_key[key]
        if decision.status == "bind":
            identity.authority_id = decision.authority_id
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
        new_resolutions.append(normalize_character_resolution({
            "source_label": decision.canonical_name,
            "canonical_name": decision.canonical_name,
            "resolution": "functional_identity",
            "identity_group": authority_id,
            "authority_id": authority_id,
            "reason": "AI 根据本集原文确认独立功能身份",
            "evidence": decision.rationale[:160],
            "evidence_source_ids": decision.evidence_source_ids,
            "decision_source": IDENTITY_ADJUDICATOR_VERSION,
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
        if episode_id:
            from app.db import get_conn

            conn = get_conn()
            row = conn.execute(
                "SELECT id FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            if row is not None:
                episode["character_resolutions"] = (
                    persist_screenplay_character_resolutions(
                        conn,
                        episode_id,
                        new_resolutions,
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
