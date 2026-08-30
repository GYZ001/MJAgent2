"""剧本角色决议的合并、加载与持久化（按 episode/source 维度）。
"""

from __future__ import annotations

import json

from app.errors import ContentGenerationError
from app.identity_authority import (
    IdentityAuthorityConflictError,
    normalize_character_resolution,
    normalize_character_resolutions,
)
from app.orchestration.state_machine import StateConflict

from ._db_probe import _has_column
from .constants import (
    DURABLE_IDENTITY_DECISION_PROVENANCE,
    FUTURE_IDENTITY_DECISION_VERSION,
    IDENTITY_ADJUDICATION_SOURCE_PROVENANCE,
)
from .evidence_receipt import _validate_current_identity_receipt_bundle
from .structural_coverage import (
    _identity_adjudication_receipt_is_valid,
    screenplay_identity_resolution_is_current_for_scope,
    screenplay_identity_resolution_is_current_for_source,
)

def merge_screenplay_character_resolutions(
    existing: list[dict] | None,
    incoming: list[dict] | None,
) -> list[dict]:
    """合并模型决议：后续真名证据可升级早期路人降级，不反向覆盖。

    ``identity_group`` 是模型已经做出的同一实体决议。结构审计可能为该
    实体增加新的稳定句柄（例如“大青山被困少年1”），但这不能因为
    描述性 canonical_name 变化就签发第二个 authority。同组的功能身份
    因此稳定复用已有权威；只有更高优先级的真名证据可整组升级。
    同组出现两个不同真名时证据自相矛盾，必须失败，不做猜测归并。
    """
    priority = {
        "functional_extra": 0,
        "functional_identity": 1,
        "reference_identity": 2,
        "future_identity": 3,
    }
    normalized_existing = normalize_character_resolutions(existing)
    normalized_incoming = normalize_character_resolutions(incoming)

    # A group token is scoped to one discovery input.  A fresh owned-source
    # discovery retires functional rows carrying the same bare token from an
    # older or unscoped epoch instead of guessing that F1 still means the same
    # person after the source changed.
    incoming_scopes_by_group: dict[str, set[str]] = {}
    for item in normalized_incoming:
        group = str(item.get("identity_group") or "").strip()
        scope = str(item.get("identity_scope_fingerprint") or "").strip()
        if group and scope and str(item.get("resolution") or "") != "future_identity":
            incoming_scopes_by_group.setdefault(group, set()).add(scope)
    normalized_existing = [
        item
        for item in normalized_existing
        if not (
            str(item.get("resolution") or "") != "future_identity"
            and str(item.get("identity_group") or "").strip()
            in incoming_scopes_by_group
            and str(item.get("identity_scope_fingerprint") or "").strip()
            not in incoming_scopes_by_group[
                str(item.get("identity_group") or "").strip()
            ]
        )
    ]

    def group_key(item: dict) -> tuple[str, str] | None:
        group = str(item.get("identity_group") or "").strip()
        if not group:
            return None
        return (
            str(item.get("identity_scope_fingerprint") or "").strip(),
            group,
        )

    existing_by_group: dict[tuple[str, str], list[dict]] = {}
    incoming_by_group: dict[tuple[str, str], list[dict]] = {}
    for item in normalized_existing:
        if (key := group_key(item)) is not None:
            existing_by_group.setdefault(key, []).append(item)
    for item in normalized_incoming:
        if (key := group_key(item)) is not None:
            incoming_by_group.setdefault(key, []).append(item)

    def top_authorities(items: list[dict]) -> tuple[int, dict[tuple[str, str], dict]]:
        top_priority = max(
            (priority.get(str(item.get("resolution") or ""), 0) for item in items),
            default=-1,
        )
        choices = {
            (item["canonical_name"], item["authority_id"]): item
            for item in items
            if priority.get(str(item.get("resolution") or ""), 0) == top_priority
        }
        return top_priority, choices

    group_authorities: dict[tuple[str, str], dict] = {}
    for key in set(existing_by_group) | set(incoming_by_group):
        existing_priority, existing_choices = top_authorities(
            existing_by_group.get(key, [])
        )
        incoming_priority, incoming_choices = top_authorities(
            incoming_by_group.get(key, [])
        )
        authority = None
        if len(existing_choices) == 1:
            authority = next(iter(existing_choices.values()))
            if incoming_priority > existing_priority:
                authority = (
                    next(iter(incoming_choices.values()))
                    if len(incoming_choices) == 1
                    else None
                )
            elif (
                incoming_priority == existing_priority == priority["future_identity"]
                and incoming_choices
                and set(incoming_choices) != set(existing_choices)
            ):
                authority = None
        elif len(existing_choices) > 1:
            # Legacy divergent rows are repairable only when the current
            # owned-source pass supplies one unambiguous authority at equal or
            # higher strength.  Array order is never an authority signal.
            if incoming_priority >= existing_priority and len(incoming_choices) == 1:
                authority = next(iter(incoming_choices.values()))
        elif len(incoming_choices) == 1:
            authority = next(iter(incoming_choices.values()))
        if authority is None:
            scope, group = key
            names = sorted({
                item["canonical_name"]
                for item in [
                    *existing_by_group.get(key, []),
                    *incoming_by_group.get(key, []),
                ]
            })
            raise IdentityAuthorityConflictError([{
                "reason": "identity_group_authority_ambiguous",
                "identity_group": group,
                "identity_scope_fingerprint": scope,
                "canonical_names": names,
                "message": (
                    f"identity_group={group} 缺少唯一可验证权威：{names}"
                ),
            }])
        group_authorities[key] = authority

    def bind_to_group_authority(candidate: dict) -> dict:
        key = group_key(candidate)
        authority = group_authorities.get(key) if key is not None else None
        if authority is None:
            return candidate
        rebound = {
            **candidate,
            "canonical_name": authority["canonical_name"],
            "resolution": authority["resolution"],
            "authority_id": authority["authority_id"],
        }
        # source_instance_key is an occurrence scope, not an identity-group
        # alias.  Preserve it byte-for-byte and never synthesize one.
        if "source_instance_key" not in candidate:
            rebound.pop("source_instance_key", None)
        return normalize_character_resolution(rebound)

    merged: list[dict] = []
    for candidate in [*normalized_existing, *normalized_incoming]:
        candidate = bind_to_group_authority(candidate)
        source_label = str(candidate.get("source_label") or "").strip()
        source_instance_key = str(
            candidate.get("source_instance_key") or ""
        ).strip()
        current_index = next((
            index
            for index, current_item in enumerate(merged)
            if (
                str(current_item.get("source_label") or "").strip()
                == source_label
                and str(current_item.get("identity_group") or "").strip()
                == str(candidate.get("identity_group") or "").strip()
                and str(
                    current_item.get("identity_scope_fingerprint") or ""
                ).strip() == str(
                    candidate.get("identity_scope_fingerprint") or ""
                ).strip()
                and str(
                    current_item.get("source_instance_key") or ""
                ).strip() == source_instance_key
            )
        ), None)
        current = merged[current_index] if current_index is not None else None
        if current is None:
            merged.append(candidate)
            continue
        current_priority = priority.get(
            str(current.get("resolution") or ""), 0,
        )
        candidate_priority = priority.get(
            str(candidate.get("resolution") or ""), 0,
        )
        if candidate_priority > current_priority:
            merged[current_index] = candidate
        elif (
            candidate_priority == current_priority
            and current.get("canonical_name") == candidate.get("canonical_name")
        ):
            merged[current_index] = {**current, **candidate}
    return merged


def load_screenplay_character_resolutions(conn, episode_id: str) -> list[dict]:
    if not _has_column(conn, "episodes", "screenplay_character_resolutions"):
        return []
    row = conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if not row:
        return []
    try:
        payload = json.loads(row["screenplay_character_resolutions"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return (
        normalize_character_resolutions(payload)
        if isinstance(payload, list)
        else []
    )


def screenplay_character_resolutions_for_source(
    resolutions: list[dict] | None,
    *,
    episode_no: int,
    source_text: str,
) -> list[dict]:
    """Return the only resolution set downstream screenplay code may trust.

    Durable manual/Bible decisions remain portable.  Automatic decisions are
    admitted only when their aggregate/source epoch and, for RF11 current
    identities, complete owned-evidence receipt bundle are current.
    """
    return [
        item
        for item in normalize_character_resolutions(resolutions)
        if screenplay_identity_resolution_is_current_for_source(
            item,
            episode_no=episode_no,
            source_text=source_text,
        )
    ]


def load_screenplay_character_resolutions_for_source(
    conn,
    episode_id: str,
    *,
    episode_no: int,
    source_text: str,
) -> list[dict]:
    return screenplay_character_resolutions_for_source(
        load_screenplay_character_resolutions(conn, episode_id),
        episode_no=episode_no,
        source_text=source_text,
    )


def persist_screenplay_character_resolutions(
    conn,
    episode_id: str,
    resolutions: list[dict] | None,
    *,
    retire_legacy_future_identity: bool = False,
    expected_active_run_id: str | None = None,
    expected_revision_id: str | None = None,
    replace_identity_scope: str | None = None,
    retire_stale_structural_identity_policy: str | None = None,
    retire_stale_identity_scope_fingerprint: str | None = None,
    retire_automatic_identity_keys: set[tuple[str, str, str]] | None = None,
) -> list[dict]:
    columns = "screenplay_character_resolutions"
    if expected_active_run_id is not None:
        columns += ", active_screenplay_run_id"
    row = conn.execute(
        f"SELECT {columns} FROM episodes WHERE id=?",  # noqa: S608 - fixed columns
        (episode_id,),
    ).fetchone()
    if row is None:
        raise StateConflict("episode", episode_id, {episode_id}, "missing")
    old_json = str(row["screenplay_character_resolutions"] or "[]")
    try:
        old_payload = json.loads(old_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        old_payload = []
    current = (
        normalize_character_resolutions(old_payload)
        if isinstance(old_payload, list)
        else []
    )
    if replace_identity_scope is not None:
        # This call is the complete owned-source discovery replacement
        # boundary, not an incremental structural audit.  Retire every prior
        # automatic decision (including same-hash rows omitted by the fresh
        # result); only explicitly durable human/Bible provenance survives.
        current = [
            item
            for item in current
            if str(item.get("decision_provenance") or "").strip()
            in DURABLE_IDENTITY_DECISION_PROVENANCE
        ]
    if expected_active_run_id is not None:
        actual_owner = str(row["active_screenplay_run_id"] or "")
        if actual_owner != expected_active_run_id:
            raise StateConflict(
                "screenplay_resolution_owner",
                episode_id,
                {expected_active_run_id},
                actual_owner,
            )
    if expected_revision_id is not None:
        revision_row = conn.execute(
            "SELECT id FROM production_revisions "
            "WHERE episode_id=? AND kind='screenplay' AND status='active' "
            "ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        actual_revision = str(revision_row["id"] or "") if revision_row else ""
        if actual_revision != expected_revision_id:
            raise StateConflict(
                "screenplay_resolution_revision",
                episode_id,
                {expected_revision_id},
                actual_revision,
            )
    if retire_legacy_future_identity:
        current = [
            item for item in current
            if (
                str(item.get("resolution") or "") != "future_identity"
                or str(item.get("decision_contract_version") or "")
                == FUTURE_IDENTITY_DECISION_VERSION
            )
        ]
    if retire_stale_structural_identity_policy is not None:
        current = [
            item for item in current
            if (
                str(item.get("decision_provenance") or "").strip()
                in DURABLE_IDENTITY_DECISION_PROVENANCE
                or str(
                    item.get("structural_identity_policy_version") or ""
                ).strip() == retire_stale_structural_identity_policy
            )
        ]
    if retire_stale_identity_scope_fingerprint is not None:
        current = [
            item
            for item in current
            if screenplay_identity_resolution_is_current_for_scope(
                item,
                identity_scope_fingerprint=(
                    retire_stale_identity_scope_fingerprint
                ),
            )
        ]
    if retire_automatic_identity_keys:
        current = [
            item
            for item in current
            if (
                str(item.get("decision_provenance") or "").strip()
                in DURABLE_IDENTITY_DECISION_PROVENANCE
                or (
                    str(item.get("source_label") or "").strip(),
                    str(item.get("identity_group") or "").strip(),
                    str(
                        item.get("identity_scope_fingerprint") or ""
                    ).strip(),
                ) not in retire_automatic_identity_keys
            )
        ]
    merged = merge_screenplay_character_resolutions(current, resolutions)
    # Fingerprint stability guard. A fresh discovery pass that reproduces the
    # SAME semantic identity decisions (same authority_id / resolution /
    # identity group / provenance) must not rewrite the stored rows just because
    # the model re-authored volatile free-text (reason/evidence) or row order.
    # That churn changed screenplay_authority_fingerprint between a retry-grant
    # activation and its baseline task, superseding the revision the
    # user_retry_approval grant was bound to and deadlocking every retry
    # (BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED). Comparing against the ORIGINAL
    # stored payload (not the post-retire ``current``) keeps genuine retire /
    # scope-replacement writes intact while suppressing no-op semantic rewrites.
    stored_current = (
        normalize_character_resolutions(old_payload)
        if isinstance(old_payload, list)
        else []
    )

    def _receipt_semantic_key(item: dict) -> tuple[str, str]:
        if str(item.get("source_label_provenance") or "").strip() == (
            IDENTITY_ADJUDICATION_SOURCE_PROVENANCE
        ):
            return (
                "adjudication_v2"
                if _identity_adjudication_receipt_is_valid(
                    item,
                    source_text=None,
                )
                else "invalid_adjudication",
                "",
            )
        try:
            bundle = _validate_current_identity_receipt_bundle(
                item,
                source_text=None,
            )
        except ContentGenerationError:
            return ("invalid", "")
        if bundle is not None:
            return ("current_v2", "")
        return ("typed_or_none", "")

    def _semantic_identity_key(items: list[dict]) -> list[tuple[str, ...]]:
        return sorted(
            (
                str(item.get("authority_id") or ""),
                str(item.get("source_label") or ""),
                str(item.get("canonical_name") or ""),
                str(item.get("resolution") or ""),
                str(item.get("identity_group") or ""),
                str(item.get("identity_scope_fingerprint") or ""),
                str(item.get("decision_provenance") or ""),
                str(item.get("decision_contract_version") or ""),
                str(
                    item.get("structural_identity_policy_version") or ""
                ),
                *_receipt_semantic_key(item),
            )
            for item in items
        )

    if _semantic_identity_key(merged) == _semantic_identity_key(stored_current):
        return stored_current
    if _has_column(conn, "episodes", "screenplay_character_resolutions"):
        clauses = ["id=?", "screenplay_character_resolutions=?"]
        params: list[object] = [
            json.dumps(merged, ensure_ascii=False),
            episode_id,
            old_json,
        ]
        if expected_active_run_id is not None:
            clauses.append("COALESCE(active_screenplay_run_id, '')=?")
            params.append(expected_active_run_id)
        if expected_revision_id is not None:
            clauses.append(
                "?=(SELECT id FROM production_revisions "
                "WHERE episode_id=episodes.id AND kind='screenplay' "
                "AND status='active' ORDER BY updated_at DESC LIMIT 1)"
            )
            params.append(expected_revision_id)
        cursor = conn.execute(
            "UPDATE episodes SET screenplay_character_resolutions=? WHERE "
            + " AND ".join(clauses),
            params,
        )
        if cursor.rowcount != 1:
            # This helper owns the persistence commit.  A failed optimistic
            # write must not leave the process-global SQLite connection inside
            # an open transaction or retain a write lock.
            conn.rollback()
            raise StateConflict(
                "screenplay_resolution_cas",
                episode_id,
                {expected_active_run_id or "unchanged-owner-and-value"},
                "stale-owner-revision-or-value",
            )
        conn.commit()
    return merged

