"""未来章节身份候选解析——已有人物权威目录与已在场真名集合。

从 ``future_identity_resolution.py`` 拆出三段原来内联在
``resolve_future_identity_candidates`` 里、互相独立（都只读 ``candidates``/
``bible``，不依赖待决分组）的权威目录构造逻辑：

* ``_future_identity_known_names`` —— Bible 已登记角色真名列表。
* ``_future_identity_authority_by_id`` —— 按权威 id 汇总的人物权威目录
  （``authority_by_id``/``authority_projection``）。
* ``_future_identity_named_authority_context`` —— 本集内已具名候选按
  identity_group 汇总的权威集合（``named_authorities_by_identity_group``），
  以及本集已独立具名在场的权威集合（``episode_named_authorities``）。
"""
from __future__ import annotations

from app.errors import ContentGenerationError
from app.schemas import Bible

from .constants import DURABLE_IDENTITY_DECISION_PROVENANCE
from .discovery_resample import _canonical_named_authority_id


def _future_identity_known_names(bible: Bible) -> list[str]:
    return [
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
    ]


def _future_identity_authority_by_id(
    candidates: list[dict],
    known_names: list[str],
) -> tuple[dict[str, dict], list[dict]]:
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
    return authority_by_id, authority_projection


def _future_identity_named_authority_context(
    candidates: list[dict],
) -> tuple[dict[str, set[str]], set[str]]:
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
    return named_authorities_by_identity_group, episode_named_authorities
