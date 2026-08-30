"""Binds and merges IR identities against the backend identity-authority registry into final authoritative identity keys."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from app.identity_authority import (
    backend_owned_identity_authority,
    identity_authority_registry,
    identity_resolution_is_authoritative,
)
from app.schemas import Bible

from .constants import ScreenplayIRIdentityConflictError
from .contract_validation import _structural_context_authority_id
from .models_core import IRIdentity
from .models_event import ScreenplayGenerationIR


def _apply_authoritative_ir_identity_resolutions(
    value: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    bible: Bible,
    audit: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind IR identities through exact authority references only.

    The function intentionally performs no semantic inference.  Missing or
    conflicting bindings are surfaced to the async AI adjudication stage.
    """
    changes, issues = prepare_ir_identity_authorities(
        value,
        episode=episode,
        bible=bible,
        audit=audit,
    )
    if issues:
        first = issues[0]
        reason = str(first.get("reason") or "identity_authority_unresolved")
        if reason == "multiple_exact_authorities":
            message = (
                f"IR 身份 {first.get('identity_key')} 命中冲突的身份权威："
                + "、".join(first.get("candidate_authority_ids") or [])
            )
        else:
            message = (
                f"IR 身份 {first.get('identity_key')} 缺少可验证的身份权威"
            )
        raise ScreenplayIRIdentityConflictError(message, issues=issues)
    return changes


def _bind_ir_identity_authority(
    identity: IRIdentity,
    authority: dict[str, Any],
    *,
    bible_by_name: dict[str, Any],
    audit: list[dict[str, Any]],
) -> dict[str, Any] | None:
    authority_id = str(authority.get("authority_id") or "").strip()
    canonical_name = str(authority.get("canonical_name") or "").strip()
    if not authority_id or not canonical_name:
        return None
    before = {
        "authority_id": identity.authority_id,
        "display_name": identity.display_name,
        "source_names": list(identity.source_names),
        "role_type": identity.role_type,
    }
    identity.authority_id = authority_id
    identity.display_name = canonical_name
    identity.source_names = list(dict.fromkeys([
        *identity.source_names,
        *(
            str(value or "").strip()
            for value in authority.get("source_labels") or []
            if str(value or "").strip()
        ),
    ]))
    character = bible_by_name.get(canonical_name)
    if character is not None:
        identity.kind = character.role or identity.kind
        identity.visual_policy = "canonical"
        identity.visual_canonical = character.appearance_canonical
        identity.asset_requirement = "required"
        identity.voice_canonical = (
            character.speech_style
            or character.personality
            or identity.voice_canonical
        )
        identity.role_type = "named_character"
    elif str(authority.get("identity_kind") or "") == "functional":
        identity.role_type = "functional_character"
    after = {
        "authority_id": identity.authority_id,
        "display_name": identity.display_name,
        "source_names": list(identity.source_names),
        "role_type": identity.role_type,
    }
    if before == after:
        return None
    change = {
        "path": f"identities.{identity.key}",
        "operation": str(
            authority.get("binding_operation")
            or "bind_exact_identity_authority"
        ),
        "from": before,
        "to": after,
        "reason": str(
            authority.get("binding_reason")
            or "explicit_or_unique_exact_authority_reference"
        ),
    }
    audit.append(change)
    return change


# 文字归属型单元：文字本身刻在道具上、写在屏幕上或作为必现文本出现，
# 由 text_provenance.content_owner_keys 归属，而不是某个在场人物的状态。
# 与 screenplay_scene_shards._ATTRIBUTED_TEXT_DELIVERY_MODES 同源，只是那边
# 用的是蓝图侧的 delivery mode，这边用的是编译后的 provenance kind。
ATTRIBUTED_TEXT_PROVENANCE_KINDS = frozenset({
    "required_text",
    "prop_text",
    "on_screen_text",
})


def _rewrite_ir_identity_key(
    value: ScreenplayGenerationIR,
    old_key: str,
    new_key: str,
) -> None:
    def replace(tokens: list[str]) -> list[str]:
        return list(dict.fromkeys(
            new_key if token == old_key else token
            for token in tokens
        ))

    for scene in value.scenes:
        scene.character_keys = replace(scene.character_keys)
        for unit in scene.units:
            if unit.speaker_key == old_key:
                unit.speaker_key = new_key
            unit.actor_keys = replace(unit.actor_keys)
            unit.target_keys = replace(unit.target_keys)
            unit.onscreen_entity_keys = replace(unit.onscreen_entity_keys)
            unit.text_provenance.content_owner_keys = replace(
                unit.text_provenance.content_owner_keys
            )
            for delivery in unit.participant_deliveries:
                if delivery.participant_key == old_key:
                    delivery.participant_key = new_key
    for event in value.events:
        event.actor_keys = replace(event.actor_keys)
        event.target_keys = replace(event.target_keys)
        event.onscreen_entity_keys = replace(event.onscreen_entity_keys)
        event.perceivable_by = replace(event.perceivable_by)
        event.text_provenance.content_owner_keys = replace(
            event.text_provenance.content_owner_keys
        )
        for delivery in event.participant_deliveries:
            if delivery.participant_key == old_key:
                delivery.participant_key = new_key
    for beat in value.beats:
        if beat.who == old_key:
            beat.who = new_key


def _merge_ir_identities_with_same_authority(
    value: ScreenplayGenerationIR,
    *,
    explicit_identity_keys: set[str],
    audit: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_authority: defaultdict[str, list[IRIdentity]] = defaultdict(list)
    for identity in value.identities:
        if identity.authority_id:
            by_authority[identity.authority_id].append(identity)
    removed: set[str] = set()
    changes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for authority_id, identities in by_authority.items():
        if len(identities) < 2:
            continue
        identity_keys = {identity.key for identity in identities}
        if not identity_keys.issubset(explicit_identity_keys):
            issues.append({
                "identity_key": identities[0].key,
                "identity_keys": [identity.key for identity in identities],
                "reason": "shared_inferred_authority",
                "candidate_authority_ids": [authority_id],
            })
            continue
        keeper = identities[0]
        for duplicate in identities[1:]:
            _rewrite_ir_identity_key(value, duplicate.key, keeper.key)
            keeper.source_names = list(dict.fromkeys([
                *keeper.source_names,
                *duplicate.source_names,
            ]))
            removed.add(duplicate.key)
            change = {
                "path": f"identities.{duplicate.key}",
                "operation": "merge_same_identity_authority",
                "from": duplicate.key,
                "to": keeper.key,
                "authority_id": authority_id,
                "reason": "identical_explicit_authority_reference",
            }
            changes.append(change)
            audit.append(change)
    if removed:
        value.identities = [
            identity for identity in value.identities
            if identity.key not in removed
        ]
    return changes, issues


def prepare_ir_identity_authorities(
    value: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    bible: Bible,
    audit: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply exact bindings and return unresolved semantic cases for AI."""
    referenced_identity_keys = {
        key
        for scene in value.scenes
        for key in [
            *scene.character_keys,
            *(
                key
                for unit in scene.units
                for key in (
                    *unit.actor_keys,
                    *unit.target_keys,
                    *unit.onscreen_entity_keys,
                    *unit.text_provenance.content_owner_keys,
                )
            ),
            *(
                unit.speaker_key
                for unit in scene.units
                if unit.speaker_key
            ),
        ]
        if key
    }
    referenced_identity_keys.update(
        key
        for event in value.events
        for key in [
            *event.actor_keys,
            *event.target_keys,
            *event.onscreen_entity_keys,
            *event.text_provenance.content_owner_keys,
            *(
                perceiver_key
                for perceiver_key in event.perceivable_by
                if perceiver_key != "audience"
            ),
        ]
        if key
    )
    orphan_identities = [
        identity
        for identity in value.identities
        if identity.key not in referenced_identity_keys
        and not any(
            str(beat.who or "").strip()
            in {identity.key, identity.display_name}
            for beat in value.beats
        )
    ]
    if orphan_identities:
        orphan_keys = {identity.key for identity in orphan_identities}
        value.identities = [
            identity
            for identity in value.identities
            if identity.key not in orphan_keys
        ]
        for identity in orphan_identities:
            audit.append({
                "path": f"identities.{identity.key}",
                "operation": "remove_unreferenced_identity",
                "reason": "identity_has_no_structural_scene_dialogue_event_or_beat_reference",
            })
    registry = identity_authority_registry(
        bible,
        episode.get("character_resolutions") or [],
    )
    by_id = {
        str(item.get("authority_id") or "").strip(): item
        for item in registry
        if str(item.get("authority_id") or "").strip()
    }
    bible_by_name = {
        str(character.name or "").strip(): character
        for character in bible.characters
        if str(character.name or "").strip()
    }
    legacy_self_authority = bool(
        not str(value.format_version or "").startswith(
            "screenplay-generation-ir.v1.4"
        )
        and not any(
            identity_resolution_is_authoritative(item)
            for item in (episode.get("character_resolutions") or [])
        )
    )
    changes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    explicit_identity_keys = {
        identity.key
        for identity in value.identities
        if str(identity.authority_id or "").strip()
    }
    for identity in value.identities:
        explicit = str(identity.authority_id or "").strip()
        authority = by_id.get(explicit) if explicit else None
        if authority is None:
            authority = backend_owned_identity_authority(
                identity_key=identity.key,
                display_name=identity.display_name,
                role_type=identity.role_type,
                source_names=identity.source_names,
            )
        if (
            explicit
            and authority is None
            and identity.kind in {
                "source_backed_scene_context_actor",
                "event_referenced_contextual_identity",
            }
        ):
            expected_context_id = _structural_context_authority_id(
                episode,
                identity.key,
            )
            if explicit == expected_context_id:
                authority = {
                    "authority_id": explicit,
                    "canonical_name": identity.display_name or identity.key,
                    "identity_kind": "functional",
                    "source_labels": list(identity.source_names),
                }
        if (
            explicit
            and authority is None
            and identity.kind == "referenced_identity"
            and explicit == f"reference:{identity.display_name}"
        ):
            # 后端自己签发的非人物引用权威（宗门、器物这类"文字/物件归属"）。
            # identity_authority_registry 只从人物谱与本集人物决议派生，看不到它，
            # 于是它会被当成"未知权威"送进人物身份仲裁——而那个仲裁只有
            # 绑定已有人物 / 新真名 / 新功能身份三种结论，宗门一个都满足不了，
            # 只能判"证据不足"，整集必死（生产上 EP2 每次都停在 reference:靠山宗）。
            # 权威 ID 是由 display_name 逐字派生的，可被后端自证，不会被模型伪造。
            authority = {
                "authority_id": explicit,
                "canonical_name": identity.display_name,
                "identity_kind": "reference",
                "source_labels": list(identity.source_names),
            }
        if explicit and authority is None and legacy_self_authority:
            legacy_seed = json.dumps(
                {
                    "key": identity.key,
                    "display_name": identity.display_name,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            expected_legacy_id = "legacy:" + hashlib.sha256(
                legacy_seed.encode("utf-8")
            ).hexdigest()[:16]
            if explicit == expected_legacy_id:
                authority = {
                    "authority_id": explicit,
                    "canonical_name": identity.display_name or identity.key,
                    "identity_kind": (
                        "named"
                        if identity.role_type == "named_character"
                        else "functional"
                    ),
                    "source_labels": list(identity.source_names),
                }
        if explicit and authority is None:
            issues.append({
                "identity_key": identity.key,
                "reason": "unknown_explicit_authority",
                "authority_id": explicit,
            })
            continue
        if authority is None:
            tokens = {
                str(identity.display_name or "").strip(),
                *(
                    str(name or "").strip()
                    for name in identity.source_names
                    if str(name or "").strip()
                ),
            }
            candidate_ids = {
                str(item.get("authority_id") or "").strip()
                for item in registry
                if (
                    str(item.get("canonical_name") or "").strip() in tokens
                    or bool(tokens.intersection({
                        str(label or "").strip()
                        for label in item.get("source_labels") or []
                        if str(label or "").strip()
                    }))
                )
            }
            candidate_ids.discard("")
            if len(candidate_ids) == 1:
                authority = by_id[next(iter(candidate_ids))]
            elif len(candidate_ids) > 1:
                issues.append({
                    "identity_key": identity.key,
                    "reason": "multiple_exact_authorities",
                    "tokens": sorted(tokens),
                    "candidate_authority_ids": sorted(candidate_ids),
                })
                continue
            else:
                if legacy_self_authority:
                    legacy_seed = json.dumps(
                        {
                            "key": identity.key,
                            "display_name": identity.display_name,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    authority = {
                        "authority_id": "legacy:" + hashlib.sha256(
                            legacy_seed.encode("utf-8")
                        ).hexdigest()[:16],
                        "canonical_name": (
                            identity.display_name or identity.key
                        ),
                        "identity_kind": (
                            "named"
                            if identity.role_type == "named_character"
                            else "functional"
                        ),
                        "source_labels": list(identity.source_names),
                    }
                else:
                    issues.append({
                        "identity_key": identity.key,
                        "reason": "missing_exact_authority",
                        "tokens": sorted(tokens),
                        "candidate_authority_ids": [],
                    })
                    continue
        change = _bind_ir_identity_authority(
            identity,
            authority,
            bible_by_name=bible_by_name,
            audit=audit,
        )
        if change:
            changes.append(change)

    merge_changes, merge_issues = _merge_ir_identities_with_same_authority(
        value,
        explicit_identity_keys=explicit_identity_keys,
        audit=audit,
    )
    changes.extend(merge_changes)
    issues.extend(merge_issues)
    return changes, issues
