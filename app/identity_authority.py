"""Stable screenplay identity authority helpers.

Semantic identity decisions come from the character-discovery model.  This
module only gives those decisions durable IDs and validates their structural
shape; it never classifies a person from a name, title, age, costume, or role
word list.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


IDENTITY_AUTHORITY_VERSION = "screenplay-identity-authority.v1"
BACKEND_OWNED_IDENTITY_AUTHORITY_VERSION = (
    "screenplay-backend-owned-identity-authority.v1"
)
NON_AUTHORITATIVE_SOURCE_LABEL_PROVENANCES = frozenset({
    "provider_synthetic_functional.v1",
})


class IdentityAuthorityConflictError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = list(issues)
        super().__init__(
            "；".join(
                str(issue.get("message") or issue.get("reason") or "")
                for issue in self.issues
            )
        )


def backend_owned_identity_authority(
    *,
    identity_key: str,
    display_name: str,
    role_type: str,
    source_names: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Return authority that follows directly from the typed IR contract.

    This boundary is intentionally structural: it does not inspect names,
    titles, professions, appearance, source prose, or any vocabulary list.
    A pure narrator is an episode-local voice identity owned by the compiler,
    so a provider-supplied ID must not turn it into a semantic adjudication.
    """
    if str(role_type or "").strip() != "narrator":
        return None
    key = str(identity_key or "").strip()
    if not key:
        return None
    return {
        "authority_id": f"narrator:{key}",
        "canonical_name": str(display_name or "").strip() or key,
        "identity_kind": "narrator",
        "source_labels": [
            label
            for value in (source_names or [])
            if (label := str(value or "").strip())
        ],
        "authority_version": BACKEND_OWNED_IDENTITY_AUTHORITY_VERSION,
        "binding_operation": "bind_backend_owned_identity_authority",
        "binding_reason": "typed_role_contract_is_compiler_owned",
    }


def model_identity_authority_prompt_rule() -> str:
    """Keep provider, registry, adjudicator, and compiler authority in sync."""
    return (
        "authority_id 只允许逐字引用人物谱或身份预解析中已有 ID，模型不得自行生成；"
        "没有精确已登记 authority 的身份必须留空，交由后端根据 owned source evidence 条件式仲裁；"
        "role_type=narrator 的纯旁白也必须留空，由后端根据 identity.key 确定性生成。"
    )


def authority_id_for_resolution(value: dict[str, Any]) -> str:
    """Return a deterministic episode-local authority ID for one decision."""
    explicit = str(value.get("authority_id") or "").strip()
    if explicit:
        return explicit

    canonical_name = str(value.get("canonical_name") or "").strip()
    resolution = str(value.get("resolution") or "").strip()
    # ``future_identity`` and ``reference_identity`` are both named-family
    # decisions: a stable named entity that either materializes a card now
    # (future) or is only referenced offscreen this episode (reference).  Both
    # must resolve to the canonical named-authority namespace so the persisted
    # ``authority_id`` agrees with the ``named:`` identity_group that the
    # discovery projector assigns to the very same label.  Minting a
    # ``functional:`` authority for a named group is the self-contradictory row
    # that makes the structural-coverage gate see two authority classes in one
    # group.  Only functional extras (no stable canonical name) fall through to
    # the synthetic functional namespace below.
    if canonical_name and resolution in {"future_identity", "reference_identity"}:
        return f"bible:{canonical_name}"

    source_label = str(value.get("source_label") or "").strip()
    identity_group = str(value.get("identity_group") or "").strip()
    identity_scope_fingerprint = str(
        value.get("identity_scope_fingerprint") or ""
    ).strip()
    seed = {
        "canonical_name": canonical_name,
        "identity_group": identity_group or f"source:{source_label}",
        # current-1:F1 and similar model-local group tokens are only meaningful
        # inside one discovery input.  Never let the same token from a changed
        # source epoch silently reuse the old authority.
        "identity_scope_fingerprint": identity_scope_fingerprint,
    }
    digest = hashlib.sha256(
        json.dumps(
            seed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"functional:{digest}"


def normalize_character_resolution(
    value: dict[str, Any],
) -> dict[str, Any]:
    """Backfill authority metadata without changing the semantic decision."""
    normalized = dict(value)
    normalized["source_label"] = str(
        normalized.get("source_label") or ""
    ).strip()
    normalized["canonical_name"] = str(
        normalized.get("canonical_name") or ""
    ).strip()
    normalized["identity_group"] = str(
        normalized.get("identity_group") or ""
    ).strip()
    identity_scope_fingerprint = str(
        normalized.get("identity_scope_fingerprint") or ""
    ).strip()
    if identity_scope_fingerprint:
        normalized["identity_scope_fingerprint"] = identity_scope_fingerprint
    else:
        normalized.pop("identity_scope_fingerprint", None)
    source_instance_key = str(
        normalized.get("source_instance_key") or ""
    ).strip()
    if source_instance_key:
        normalized["source_instance_key"] = source_instance_key
    else:
        normalized.pop("source_instance_key", None)
    normalized["authority_id"] = authority_id_for_resolution(normalized)
    normalized.setdefault("authority_version", IDENTITY_AUTHORITY_VERSION)
    return normalized


def normalize_character_resolutions(
    values: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    return [
        normalize_character_resolution(value)
        for value in (values or [])
        if isinstance(value, dict)
        and str(value.get("source_label") or "").strip()
        and str(value.get("canonical_name") or "").strip()
    ]


def identity_resolution_is_authoritative(value: object) -> bool:
    """Synthetic provider labels are observations, never identity authority."""
    return bool(
        isinstance(value, dict)
        and str(value.get("source_label_provenance") or "").strip()
        not in NON_AUTHORITATIVE_SOURCE_LABEL_PROVENANCES
    )


def identity_authority_registry(
    bible: object,
    resolutions: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Project Bible and preflight decisions into one exact-reference registry."""
    entries: dict[str, dict[str, Any]] = {}
    groups_by_authority: dict[str, set[str]] = {}
    authorities_by_group: dict[str, set[str]] = {}
    authorities_by_named_canonical: dict[str, set[str]] = {}

    def scoped_group_key(
        identity_group: str,
        identity_scope_fingerprint: str = "",
    ) -> str:
        return (
            f"{identity_scope_fingerprint}:{identity_group}"
            if identity_scope_fingerprint
            else identity_group
        )

    def register_group(
        authority_id: str,
        identity_group: str,
        identity_scope_fingerprint: str = "",
        *,
        semantic_group: str | None = None,
    ) -> None:
        if not identity_group:
            return
        raw_scoped_group = scoped_group_key(
            identity_group, identity_scope_fingerprint
        )
        # Forward uniqueness always uses the raw model decision group.  It must
        # catch a future identity and a functional identity that both claim the
        # same scoped F1 but resolve to different authorities.
        authorities_by_group.setdefault(raw_scoped_group, set()).add(authority_id)
        # Reverse uniqueness uses the semantic canonical group.  Confirmed
        # aliases from multiple raw groups may all join one Bible identity.
        groups_by_authority.setdefault(authority_id, set()).add(
            semantic_group or raw_scoped_group
        )

    for character in getattr(bible, "characters", None) or []:
        name = str(getattr(character, "name", "") or "").strip()
        if not name:
            continue
        register_group(f"bible:{name}", f"bible:{name}")
        authorities_by_named_canonical.setdefault(name, set()).add(
            f"bible:{name}"
        )
        entries[f"bible:{name}"] = {
            "authority_id": f"bible:{name}",
            "canonical_name": name,
            "identity_kind": "named",
            "source_labels": [name],
            "identity_group": f"bible:{name}",
            "source_instance_key": f"bible:{name}",
            "evidence": "角色圣经已登记身份",
            "future_evidence": "",
        }

    for item in normalize_character_resolutions(resolutions):
        if not identity_resolution_is_authoritative(item):
            continue
        authority_id = item["authority_id"]
        identity_group = str(item.get("identity_group") or "").strip()
        identity_scope_fingerprint = str(
            item.get("identity_scope_fingerprint") or ""
        ).strip()
        raw_identity_group = identity_group
        semantic_group = None
        if (
            str(item.get("resolution") or "") == "future_identity"
        ):
            # A confirmed name is no longer an episode-local functional group.
            # This applies equally to Bible and backend-signed future-name /
            # candidate authorities: multiple former F1 tokens may be verified
            # aliases of one authority, while the forward raw-group check below
            # still forbids one F1 from resolving to two authorities.
            semantic_group = authority_id
            authorities_by_named_canonical.setdefault(
                item["canonical_name"], set()
            ).add(authority_id)
        register_group(
            authority_id,
            raw_identity_group,
            identity_scope_fingerprint,
            semantic_group=semantic_group,
        )
        entry = entries.setdefault(authority_id, {
            "authority_id": authority_id,
            "canonical_name": item["canonical_name"],
            "identity_kind": (
                "reference"
                if str(item.get("resolution") or "")
                == "reference_identity"
                else (
                    "named"
                    if str(item.get("resolution") or "")
                    == "future_identity"
                    else "functional"
                )
            ),
            "resolution": str(item.get("resolution") or ""),
            "source_labels": [],
            "identity_group": semantic_group or raw_identity_group,
            "decision_identity_group": raw_identity_group,
            "identity_scope_fingerprint": identity_scope_fingerprint,
            "source_instance_key": (
                item.get("source_instance_key")
                or scoped_group_key(
                    raw_identity_group,
                    identity_scope_fingerprint,
                )
                or authority_id
            ),
            "evidence": item.get("evidence") or "",
            "future_evidence": item.get("future_evidence") or "",
        })
        source_label = item["source_label"]
        if source_label and source_label not in entry["source_labels"]:
            entry["source_labels"].append(source_label)
        if entry["canonical_name"] != item["canonical_name"]:
            names = sorted({
                entry["canonical_name"],
                item["canonical_name"],
            })
            raise ValueError(
                f"authority_id={authority_id} 同时声明了多个 canonical_name："
                f"{names}"
            )
    issues = [
        {
            "reason": "identity_group_multiple_canonical_identities",
            "identity_group": identity_group,
            "authority_ids": sorted(authority_ids),
            "message": (
                f"identity_group={identity_group} 对应多个 canonical identity："
                f"{sorted(authority_ids)}"
            ),
        }
        for identity_group, authority_ids in authorities_by_group.items()
        if len(authority_ids) > 1
    ]
    issues.extend(
        {
            "reason": "canonical_identity_multiple_identity_groups",
            "authority_id": authority_id,
            "identity_groups": sorted(identity_groups),
            "message": (
                f"authority_id={authority_id} 跨多个 identity_group："
                f"{sorted(identity_groups)}"
            ),
        }
        for authority_id, identity_groups in groups_by_authority.items()
        if len(identity_groups) > 1
    )
    issues.extend(
        {
            "reason": "canonical_name_multiple_named_authorities",
            "canonical_name": canonical_name,
            "authority_ids": sorted(authority_ids),
            "message": (
                f"canonical_name={canonical_name} 对应多个 named authority："
                f"{sorted(authority_ids)}"
            ),
        }
        for canonical_name, authority_ids in (
            authorities_by_named_canonical.items()
        )
        if len(authority_ids) > 1
    )
    if issues:
        raise IdentityAuthorityConflictError(issues)
    return list(entries.values())
