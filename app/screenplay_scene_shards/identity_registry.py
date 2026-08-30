"""Scene-shard errors and the frozen per-episode identity registry: ownership
assertion, blueprint/source-ownership hashing, and
``build_frozen_identity_registry`` which resolves the character/identity keys
a shard's units are allowed to reference.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import hashlib
from app.character_policy import functional_extra_anchor
from app.db import get_conn
from app.identity_authority import identity_authority_registry
from app.narrative_blueprint import (
    NarrativeBlueprint,
    effective_source_unit_deliveries,
)
from app.observability.tracing import current_trace
from app.schemas import Bible
from app.screenplay_ir import IRIdentity
from typing import Any

from .common import _hash


class ScreenplaySceneShardError(ValueError):
    def __init__(
        self,
        shard_id: str,
        errors: list[str],
        *,
        unresolved_semantic_units: dict[str, list[str]] | None = None,
    ):
        self.shard_id = shard_id
        self.errors = list(errors)
        # 语义门禁耗尽全部修复轮次后仍未收口的 unit → 双审共识原文。
        # 它标识的是**上游冻结分类无解**，而不是文案没写好：本层唯一能做的
        # 补救（重写文案）修不好一个分类错误，所以要把证据原样交给能改分类的那一层。
        self.unresolved_semantic_units = dict(unresolved_semantic_units or {})
        super().__init__(f"{shard_id}: " + "；".join(errors[:10]))


class ScreenplaySceneMergeError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("；".join(errors[:20]))


class ScreenplaySceneShardOwnershipLost(RuntimeError):
    """A provider response returned after another run acquired the episode."""


def _assert_episode_owner(episode_id: str) -> None:
    trace = current_trace()
    if not trace.run_id:
        return
    row = get_conn().execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not row or row["active_screenplay_run_id"] != trace.run_id:
        raise ScreenplaySceneShardOwnershipLost(
            "场次分片返回时剧集 owner 已变化，旧 worker 不得持久化结果"
        )


def blueprint_content_hash(blueprint: NarrativeBlueprint) -> str:
    return _hash(blueprint.model_dump(mode="json"))


def _source_ownership_hash(blueprint: NarrativeBlueprint) -> str:
    return _hash({
        "source_scene_owners": blueprint.source_scene_owners,
        "source_semantics": {
            source_id: semantics.model_dump(mode="json")
            for source_id, semantics in blueprint.source_semantics.items()
        },
        "scene_derivations": [
            relation.model_dump(mode="json")
            for relation in blueprint.scene_derivations
        ],
    })


def blueprint_referenced_content_owners(
    blueprint: NarrativeBlueprint,
) -> list[str]:
    """Return content-owner tokens that no one performs.

    A quoted unit's ``content_owner_key`` is documented as possibly being a
    text or object attribution -- the sect that engraved a token, the author of
    a notice -- and the Blueprint contract deliberately allows it.  Everything
    downstream, however, resolves content owners as identity references, so an
    attribution that is nobody's identity used to abort scene planning with a
    bare ValueError at a stage that owns no repair loop.

    Only owners that are never a ``performer_key`` are returned: whoever
    performs a line must remain a frozen person, and that check stays strict.
    """
    owners: list[str] = []
    performers: set[str] = set()
    for node in blueprint.nodes:
        if node.source_semantics().projection_policy != "picture":
            continue
        for delivery in effective_source_unit_deliveries(node):
            performer = delivery.performer_key.strip()
            if performer:
                performers.add(performer)
            owner = delivery.content_owner_key.strip()
            if owner:
                owners.append(owner)
    return [
        owner for owner in dict.fromkeys(owners)
        if owner not in performers
    ]


def build_frozen_identity_registry(
    bible: Bible,
    resolutions: list[dict[str, Any]] | None,
    referenced_content_owners: list[str] | None = None,
) -> tuple[list[IRIdentity], list[dict[str, Any]], str]:
    """Project durable authorities into stable IR identity keys."""
    authorities = identity_authority_registry(bible, resolutions)
    if referenced_content_owners:
        known_tokens = {
            str(value or "").strip()
            for authority in authorities
            for value in (
                authority.get("authority_id"),
                authority.get("identity_group"),
                authority.get("source_instance_key"),
                authority.get("canonical_name"),
                *(authority.get("source_labels") or []),
            )
            if str(value or "").strip()
        }
        for owner in referenced_content_owners:
            token = str(owner or "").strip()
            if not token or token in known_tokens:
                continue
            # An attribution nobody performs is exactly what the registry's
            # ``reference`` kind is for: offscreen only, assets forbidden.  It
            # can never become a performer or a rendered character, so a
            # mis-attributed token stays inert instead of ending the episode.
            known_tokens.add(token)
            authorities.append({
                "authority_id": f"reference:{token}",
                "canonical_name": token,
                "identity_kind": "reference",
                "source_labels": [token],
                "identity_group": f"reference:{token}",
                "source_instance_key": f"reference:{token}",
                "materialization_compatible": False,
            })
    identities: list[IRIdentity] = []
    projected: list[dict[str, Any]] = []
    for authority in sorted(
        authorities,
        key=lambda item: str(item.get("authority_id") or ""),
    ):
        authority_id = str(authority.get("authority_id") or "").strip()
        if not authority_id:
            continue
        canonical_name = str(
            authority.get("canonical_name") or authority_id
        ).strip()
        source_names = list(dict.fromkeys(
            [canonical_name]
            + [
                str(value).strip()
                for value in authority.get("source_labels") or []
                if str(value).strip()
            ]
        ))
        digest = hashlib.sha256(authority_id.encode("utf-8")).hexdigest()[:12]
        identity_key = f"person_{digest}"
        named = str(authority.get("identity_kind") or "") == "named"
        reference_only = (
            str(authority.get("identity_kind") or "") == "reference"
        )
        identity = IRIdentity(
            key=identity_key,
            display_name=canonical_name,
            authority_id=authority_id,
            source_names=source_names,
            kind=(
                "referenced_identity"
                if reference_only
                else "named_character" if named else "functional_character"
            ),
            visual_policy=(
                "offscreen_only"
                if reference_only
                else "canonical" if named else "contextual"
            ),
            visual_canonical=(
                ""
                if named or reference_only
                else functional_extra_anchor(
                    canonical_name,
                    declared_functional_names={canonical_name},
                )
            ),
            asset_requirement=(
                "forbidden"
                if reference_only
                else "required" if named else "optional"
            ),
            voice_canonical="",
            role_type=(
                "named_character"
                if named or reference_only
                else "functional_character"
            ),
            rationale="来自冻结的人物谱/本集身份决议",
        )
        identities.append(identity)
        projected.append({
            **authority,
            "identity_key": identity_key,
            "source_instance_key": str(
                authority.get("source_instance_key")
                or authority.get("identity_group")
                or authority_id
            ),
        })
    registry_hash = _hash(projected)
    return identities, projected, registry_hash


def _identity_aliases(
    identity_registry: list[dict[str, Any]],
    *,
    identity_keys: set[str] | None = None,
) -> dict[str, str]:
    candidates: dict[str, set[str]] = {
        key: {key} for key in (identity_keys or set())
    }
    for item in identity_registry:
        identity_key = str(item.get("identity_key") or "").strip()
        if not identity_key:
            continue
        for value in (
            identity_key,
            item.get("authority_id"),
            item.get("identity_group"),
            item.get("source_instance_key"),
            item.get("canonical_name"),
            *(item.get("source_labels") or []),
        ):
            label = str(value or "").strip()
            if label:
                candidates.setdefault(label, set()).add(identity_key)
    conflicts = {
        reference: sorted(keys)
        for reference, keys in candidates.items()
        if len(keys) > 1
    }
    if conflicts:
        raise ScreenplaySceneShardError(
            "identity-registry",
            [
                "typed identity reference 指向多个 canonical identity："
                f"{reference}={keys}"
                for reference, keys in sorted(conflicts.items())
            ],
        )
    return {
        reference: next(iter(keys))
        for reference, keys in candidates.items()
        if keys
    }
