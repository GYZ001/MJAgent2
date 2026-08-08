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
    if resolution == "future_identity" and canonical_name:
        return f"bible:{canonical_name}"

    source_label = str(value.get("source_label") or "").strip()
    identity_group = str(value.get("identity_group") or "").strip()
    seed = {
        "canonical_name": canonical_name,
        "identity_group": identity_group or f"source:{source_label}",
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


def identity_authority_registry(
    bible: object,
    resolutions: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Project Bible and preflight decisions into one exact-reference registry."""
    entries: dict[str, dict[str, Any]] = {}
    for character in getattr(bible, "characters", None) or []:
        name = str(getattr(character, "name", "") or "").strip()
        if not name:
            continue
        entries[f"bible:{name}"] = {
            "authority_id": f"bible:{name}",
            "canonical_name": name,
            "identity_kind": "named",
            "source_labels": [name],
            "identity_group": f"bible:{name}",
            "evidence": "角色圣经已登记身份",
            "future_evidence": "",
        }

    for item in normalize_character_resolutions(resolutions):
        authority_id = item["authority_id"]
        entry = entries.setdefault(authority_id, {
            "authority_id": authority_id,
            "canonical_name": item["canonical_name"],
            "identity_kind": (
                "named"
                if str(item.get("resolution") or "") == "future_identity"
                else "functional"
            ),
            "source_labels": [],
            "identity_group": item.get("identity_group") or "",
            "evidence": item.get("evidence") or "",
            "future_evidence": item.get("future_evidence") or "",
        })
        source_label = item["source_label"]
        if source_label and source_label not in entry["source_labels"]:
            entry["source_labels"].append(source_label)
        if entry["canonical_name"] != item["canonical_name"]:
            entry["conflicting_canonical_names"] = sorted({
                entry["canonical_name"],
                item["canonical_name"],
            })
    return list(entries.values())
