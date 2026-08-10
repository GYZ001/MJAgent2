"""Immutable screenplay/source authority resolution for downstream narrative work."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from typing import Any

from app import config
from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.ingest import chapter_is_stub, chapter_titles_match
from app.schemas import Bible, EpisodeScreenplay


SCREENPLAY_QA_PROFILE_VERSION = "screenplay-qa-gate-2"


@lru_cache(maxsize=16)
def _cached_validated_screenplay_projection(
    raw_projection: str,
) -> EpisodeScreenplay:
    """Parse one persisted projection into a process-local template."""
    return EpisodeScreenplay.model_validate_json(raw_projection)


def _validated_screenplay_projection(raw_projection: str) -> EpisodeScreenplay:
    """Return an isolated model for a complete persisted JSON projection."""
    return _cached_validated_screenplay_projection(raw_projection).model_copy(
        deep=True,
    )

# ``screenplay-source-authority.v1`` is an append-only serialization contract,
# not a live dump of whichever fields the current Pydantic models expose.
# ``Scene.forbidden_elements`` was retired from the product model after v4
# certificates had already hashed its default value.  Keep the empty slot in
# the authority payload so a harmless schema cleanup cannot invalidate every
# published screenplay on the next process restart.
_RETIRED_SCENE_AUTHORITY_DEFAULTS: dict[str, Any] = {
    "forbidden_elements": [],
}


def _contract_major(contract_version: str | None) -> int:
    raw = str(contract_version or "").strip()
    try:
        return int(raw.split(".", 1)[0])
    except (TypeError, ValueError):
        return 0


def screenplay_contract_requires_narrative(contract_version: str | None) -> bool:
    """Return whether this contract generation requires typed narrative authority."""
    return _contract_major(contract_version) >= 3


def screenplay_contract_tracks_bible_projection(
    contract_version: str | None,
) -> bool:
    """Return whether the screenplay binds the composed project Bible view."""
    return _contract_major(contract_version) >= 4


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"),
    )


def _decode_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def screenplay_bible_payload(value: Bible | dict[str, Any]) -> dict[str, Any]:
    """Return the semantic Bible payload consumed by screenplay generation.

    Character/scene image paths and asset-generation prompt overrides live in
    the mutable media projection. They must not change screenplay authority or
    leak local file-system paths into a text-model prompt.
    """
    bible = value if isinstance(value, Bible) else Bible.model_validate(value)
    payload = bible.model_dump(mode="json")
    for character in payload.get("characters") or []:
        character.pop("ref_image_path", None)
        character.pop("portrait_prompt_override", None)
    for scene in payload.get("scenes") or []:
        scene.pop("ref_image_path", None)
        scene.pop("scene_prompt_override", None)
        for field, default in _RETIRED_SCENE_AUTHORITY_DEFAULTS.items():
            # Copy mutable defaults: authority payloads are subsequently
            # transformed while reconstructing append-only Bible history.
            scene.setdefault(field, list(default))
    return payload


def _project_bible_projection(project: Any) -> dict[str, Any]:
    raw = _episode_value(project, "bible_json", "") if project else ""
    if not raw:
        return {}
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(payload, dict):
            raise TypeError("Bible JSON must be an object")
        return screenplay_bible_payload(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("项目 Bible JSON 无法解析为当前人物谱合同") from exc


def _bible_extends_by_appending_cards(
    base: dict[str, Any],
    extended: dict[str, Any],
) -> bool:
    """Return whether characters/scenes only gained uniquely named cards."""
    if (
        {**base, "characters": [], "scenes": []}
        != {**extended, "characters": [], "scenes": []}
    ):
        return False

    for collection in ("characters", "scenes"):
        base_items = list(base.get(collection) or [])
        extended_items = list(extended.get(collection) or [])
        if (
            len(extended_items) < len(base_items)
        ):
            return False
        if collection == "characters":
            if extended_items[:len(base_items)] != base_items:
                return False
        else:
            for base_scene, extended_scene in zip(
                base_items,
                extended_items[:len(base_items)],
            ):
                if not (
                    isinstance(base_scene, dict)
                    and isinstance(extended_scene, dict)
                    and str(base_scene.get("name") or "")
                    == str(extended_scene.get("name") or "")
                ):
                    return False
                for field in ("aliases", "discovery_sources"):
                    base_values = list(base_scene.get(field) or [])
                    extended_values = list(extended_scene.get(field) or [])
                    if (
                        len(extended_values) < len(base_values)
                        or extended_values[:len(base_values)] != base_values
                    ):
                        return False
                stable_base = {
                    **base_scene,
                    "aliases": [],
                    "discovery_sources": [],
                }
                stable_extended = {
                    **extended_scene,
                    "aliases": [],
                    "discovery_sources": [],
                }
                if (
                    stable_base != stable_extended
                    and not (
                        stable_base.get("first_episode") is None
                        and stable_extended.get("first_episode") is not None
                        and {
                            **stable_base,
                            "first_episode": stable_extended.get(
                                "first_episode"
                            ),
                        } == stable_extended
                    )
                ):
                    return False
        base_names = {
            str(item.get("name") or "")
            for item in base_items
            if isinstance(item, dict)
        }
        extension_names = [
            str(item.get("name") or "")
            for item in extended_items[len(base_items):]
            if isinstance(item, dict)
        ]
        if (
            len(extension_names) != len(extended_items) - len(base_items)
            or any(not name for name in extension_names)
            or len(extension_names) != len(set(extension_names))
            or bool(base_names.intersection(extension_names))
        ):
            return False
    return True


def _bible_projections_are_append_compatible(
    projection: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> bool:
    """Allow append-only character/scene growth across production stages.

    Runtime may contain legacy compatibility cards not yet persisted, while the
    project projection may gain cards during another episode or storyboard
    prefetch. Existing cards and every non-card field must remain byte-for-byte
    equivalent in either direction.
    """
    return (
        runtime_payload == projection
        or _bible_extends_by_appending_cards(projection, runtime_payload)
        or _bible_extends_by_appending_cards(runtime_payload, projection)
    )


def _episode_value(episode: Any, key: str, default: Any = None) -> Any:
    try:
        return episode[key]
    except (KeyError, IndexError, TypeError):
        return getattr(episode, key, default)


def _verified_artifact_hash(artifact: dict[str, Any], *, label: str) -> str:
    """Return an artifact hash only after re-hashing its current payload.

    ``content_hash`` is persisted metadata, not proof that ``content_json`` (or
    an attached file) has remained unchanged.  Authority resolution is a paid
    production boundary, so trusting that column would let a direct payload
    mutation keep an old certificate alive.
    """
    stored_hash = str(artifact.get("content_hash") or "")
    try:
        current_hash = evidence_repository.content_hash(
            artifact.get("content"),
            artifact.get("file_path"),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 当前内容无法重新计算指纹") from exc
    if not stored_hash or stored_hash != current_hash:
        raise ValueError(f"{label} 内容与存储指纹漂移")
    return current_hash


def _source_records(conn: Any, episode: Any) -> tuple[list[dict[str, Any]], str]:
    indexes = [int(value) for value in _decode_list(_episode_value(episode, "source_chapters"))]
    if not indexes:
        return [], ""
    marks = ",".join("?" for _ in indexes)
    rows = conn.execute(
        f"SELECT id,idx,title,content FROM chapters WHERE project_id=? "
        f"AND idx IN ({marks}) ORDER BY idx",
        (_episode_value(episode, "project_id"), *indexes),
    ).fetchall()
    chapters = [dict(row) for row in rows]
    # Match the source projection used by generation for historical imports.
    if len(chapters) == 1 and chapter_is_stub(chapters[0]):
        following = conn.execute(
            "SELECT id,idx,title,content FROM chapters WHERE project_id=? "
            "AND idx>? ORDER BY idx LIMIT 1",
            (_episode_value(episode, "project_id"), chapters[0]["idx"]),
        ).fetchone()
        if following is not None:
            candidate = dict(following)
            if not chapter_is_stub(candidate) and chapter_titles_match(chapters[0], candidate):
                chapters = [candidate]
    records = [
        {
            "chapter_id": int(chapter["id"]),
            "chapter_idx": int(chapter["idx"]),
            "title": str(chapter.get("title") or ""),
            "content_sha256": hashlib.sha256(
                str(chapter.get("content") or "").encode("utf-8")
            ).hexdigest(),
        }
        for chapter in chapters
    ]
    source_text = "\n\n".join(
        f"【{chapter.get('title') or ''}】\n{chapter.get('content') or ''}"
        for chapter in chapters
    )
    return records, source_text


def screenplay_authorized_source_chapter_ids(
    episode_id: str,
    *,
    conn: Any | None = None,
) -> set[str]:
    """Return every stable chapter handle accepted by the narrative contract.

    Historical prompts used chapter indices while current source records also
    expose database IDs.  Both resolve to the same episode-scoped chapter rows;
    arbitrary IDs from another project or episode remain invalid.
    """
    db = conn or get_conn()
    episode = db.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    records, _source_text = _source_records(db, episode)
    return {
        str(value)
        for record in records
        for value in (record.get("chapter_id"), record.get("chapter_idx"))
        if value not in (None, "")
    }


def screenplay_authorized_source_chapters(
    episode_id: str,
    *,
    conn: Any | None = None,
) -> dict[str, str]:
    """Return episode-scoped chapter text keyed by both database ID and index."""
    db = conn or get_conn()
    episode = db.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    records, _source_text = _source_records(db, episode)
    ids = [int(record["chapter_id"]) for record in records]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT id,idx,content FROM chapters WHERE id IN ({marks})",
        ids,
    ).fetchall()
    return {
        str(key): str(row["content"] or "")
        for row in rows
        for key in (row["id"], row["idx"])
    }


def screenplay_authority_material(
    episode_id: str,
    *,
    conn: Any | None = None,
    source_text: str | None = None,
    bible: Bible | None = None,
    contract_version: str = "",
    qa_profile_version: str = SCREENPLAY_QA_PROFILE_VERSION,
) -> dict[str, Any]:
    """Build the complete, content-addressed authority input for one episode."""
    db = conn or get_conn()
    episode = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    project = db.execute(
        "SELECT * FROM projects WHERE id=?", (_episode_value(episode, "project_id"),),
    ).fetchone()
    chapter_records, stored_source_text = _source_records(db, episode)
    exact_source = stored_source_text if source_text is None else str(source_text)
    if stored_source_text and source_text is not None and exact_source != stored_source_text:
        raise ValueError("剧本 QA 使用的原文与当前章节权威内容不一致")

    bible_artifact_id = _episode_value(project, "bible_artifact_id", "") if project else ""
    bible_artifact = (
        evidence_repository.get_artifact(str(bible_artifact_id))
        if bible_artifact_id else None
    )
    bible_projection = _project_bible_projection(project)
    if bible_artifact is not None:
        if (
            bible_artifact.get("type") != "character_bible"
            or bible_artifact.get("scope_type") != "project"
            or bible_artifact.get("scope_id")
            != str(_episode_value(episode, "project_id", "") or "")
            or bible_artifact.get("status")
            in {"stale", "rejected", "superseded", "needs_revision"}
        ):
            raise ValueError("Bible Artifact 的类型、作用域或状态无效")
        bible_hash = _verified_artifact_hash(bible_artifact, label="Bible Artifact")
    elif bible is not None:
        bible_hash = evidence_repository.content_hash(
            screenplay_bible_payload(bible)
        )
    else:
        bible_hash = evidence_repository.content_hash(bible_projection)

    projection_hash = ""
    if screenplay_contract_tracks_bible_projection(contract_version):
        if not bible_projection and bible_artifact is not None:
            try:
                bible_projection = screenplay_bible_payload(
                    bible_artifact.get("content") or {}
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("Bible Artifact 无法解析为当前人物谱合同") from exc
        runtime_payload = (
            screenplay_bible_payload(bible)
            if bible is not None
            else bible_projection
        )
        if not bible_projection:
            bible_projection = runtime_payload
        if not _bible_projections_are_append_compatible(
            bible_projection,
            runtime_payload,
        ):
            raise ValueError(
                "本次剧本运行使用的人物谱与项目当前组合投影不一致"
            )
        projection_hash = evidence_repository.content_hash(bible_projection)

    target_duration_s = int(
        _episode_value(episode, "target_duration_s", 0) or 0
    )
    from app.storyboard_authority import OUTLINE_AUTHORITY_VERSION

    if (
        _episode_value(episode, "target_duration_authority", "")
        == OUTLINE_AUTHORITY_VERSION
        and _episode_value(episode, "planning_target_duration_s") is not None
    ):
        # Storyboard publication promotes the accepted outline duration for
        # downstream production. The screenplay certificate remains bound to
        # the planning estimate that was its actual generation input.
        target_duration_s = int(
            _episode_value(episode, "planning_target_duration_s", 0) or 0
        )

    # The two dialogue fields are no longer production inputs. Keep their
    # historical values in v1 authority material so already-issued completion
    # certificates remain verifiable after the feature removal.
    constraints = {
        "title": _episode_value(episode, "title", "") or "",
        "hook": _episode_value(episode, "hook", "") or "",
        "cliffhanger": _episode_value(episode, "cliffhanger", "") or "",
        "synopsis": _episode_value(episode, "synopsis", "") or "",
        "target_duration_s": target_duration_s,
        "required_dialogues": _decode_list(
            _episode_value(episode, "screenplay_required_dialogues", "[]")
        ),
        "required_dialogue_occurrences": _decode_list(
            _episode_value(episode, "screenplay_required_dialogue_occurrences", "[]")
        ),
        "constraint_version": int(
            _episode_value(episode, "screenplay_constraint_version", 0) or 0
        ),
    }
    material = {
        "authority_contract": "screenplay-source-authority.v1",
        "episode_id": episode_id,
        "project_id": str(_episode_value(episode, "project_id", "") or ""),
        "source_chapters": chapter_records,
        "source_text_sha256": hashlib.sha256(exact_source.encode("utf-8")).hexdigest(),
        "bible_artifact_id": str(bible_artifact_id or ""),
        "bible_content_hash": bible_hash,
        "character_resolutions": _decode_list(
            _episode_value(episode, "screenplay_character_resolutions", "[]")
        ),
        "adaptation_constraints": constraints,
        "contract_version": str(contract_version or ""),
        "qa_profile_version": str(qa_profile_version or ""),
    }
    if projection_hash:
        material["bible_projection_hash"] = projection_hash
    return material


def screenplay_authority_fingerprint(
    episode_id: str,
    *,
    conn: Any | None = None,
    source_text: str | None = None,
    bible: Bible | None = None,
    contract_version: str = "",
    qa_profile_version: str = SCREENPLAY_QA_PROFILE_VERSION,
) -> str:
    material = screenplay_authority_material(
        episode_id,
        conn=conn,
        source_text=source_text,
        bible=bible,
        contract_version=contract_version,
        qa_profile_version=qa_profile_version,
    )
    return _authority_material_fingerprint(material)


def _authority_material_fingerprint(material: dict[str, Any]) -> str:
    return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()


def _projection_before_recorded_bible_append(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    """Reverse one append operation recorded by a reactive Bible writer."""
    snapshot = artifact.get("model_snapshot") or {}
    operation = str(snapshot.get("operation") or "")
    prompt_version = str(artifact.get("prompt_version") or "")
    allowed_prompt = {
        "incremental_add": "incremental-character-discovery-1.0.0",
        "incremental_scene_add": "reactive-scene-bible-1.0.0",
        "incremental_scene_alias": "reactive-scene-bible-1.0.0",
    }
    if allowed_prompt.get(operation) != prompt_version:
        return None
    try:
        projection = screenplay_bible_payload(artifact.get("content") or {})
    except (TypeError, ValueError):
        return None

    if operation == "incremental_add":
        character_name = str(snapshot.get("character_name") or "")
        characters = list(projection.get("characters") or [])
        if (
            not character_name
            or not characters
            or str(characters[-1].get("name") or "") != character_name
        ):
            return None
        projection["characters"] = characters[:-1]
    elif operation == "incremental_scene_add":
        scene_name = str(snapshot.get("scene_name") or "")
        scenes = list(projection.get("scenes") or [])
        if (
            not scene_name
            or not scenes
            or str(scenes[-1].get("name") or "") != scene_name
        ):
            return None
        projection["scenes"] = scenes[:-1]
    else:
        scene_name = str(snapshot.get("scene_name") or "")
        matches = [
            scene
            for scene in projection.get("scenes") or []
            if str(scene.get("name") or "") == scene_name
        ]
        if len(matches) != 1:
            return None
        aliases = list(matches[0].get("aliases") or [])
        if not aliases:
            return None
        matches[0]["aliases"] = aliases[:-1]
    return projection


def _recorded_portrait_explains_appearance(
    *,
    conn: Any,
    project_id: str,
    bible_artifact_id: str,
    character_name: str,
    appearance: str,
) -> bool:
    """Verify one downstream appearance change against its approved asset."""
    rows = conn.execute(
        """SELECT artifact_id,appearance,pack_status,change_json
             FROM character_portraits
            WHERE project_id=? AND character_name=? AND appearance=?
              AND artifact_id IS NOT NULL
            ORDER BY created_at DESC""",
        (project_id, character_name, appearance),
    ).fetchall()
    for row in rows:
        if str(row["pack_status"] or "") != "ready":
            continue
        try:
            change = json.loads(row["change_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(change, dict) or not change:
            continue
        artifact = evidence_repository.get_artifact(str(row["artifact_id"]))
        if (
            artifact is None
            or artifact.get("type") != "character_portrait"
            or artifact.get("scope_type") != "reference_asset"
            or artifact.get("status") not in {"approved", "validated"}
            or bible_artifact_id not in (
                artifact.get("parent_artifact_ids") or []
            )
        ):
            continue
        content = artifact.get("content") or {}
        if (
            isinstance(content, dict)
            and str(content.get("character_name") or "") == character_name
            and str(content.get("appearance") or "") == appearance
            and isinstance(content.get("change"), dict)
            and bool(content["change"])
        ):
            return True
    return False


def _bible_extends_by_recorded_downstream_changes(
    base: dict[str, Any],
    extended: dict[str, Any],
    *,
    conn: Any,
    project_id: str,
    bible_artifact_id: str,
) -> bool:
    """Allow append growth plus asset-backed per-episode appearance evolution."""
    base_characters = list(base.get("characters") or [])
    extended_characters = list(extended.get("characters") or [])
    if len(extended_characters) < len(base_characters):
        return False
    normalized_characters = [
        dict(character) if isinstance(character, dict) else character
        for character in extended_characters
    ]
    found_change = False
    for index, base_character in enumerate(base_characters):
        extended_character = normalized_characters[index]
        if not (
            isinstance(base_character, dict)
            and isinstance(extended_character, dict)
            and str(base_character.get("name") or "")
            == str(extended_character.get("name") or "")
        ):
            return False
        base_appearance = str(
            base_character.get("appearance_canonical") or ""
        )
        extended_appearance = str(
            extended_character.get("appearance_canonical") or ""
        )
        if base_appearance == extended_appearance:
            continue
        if not _recorded_portrait_explains_appearance(
            conn=conn,
            project_id=project_id,
            bible_artifact_id=bible_artifact_id,
            character_name=str(base_character.get("name") or ""),
            appearance=extended_appearance,
        ):
            return False
        extended_character["appearance_canonical"] = base_character.get(
            "appearance_canonical"
        )
        found_change = True
    if not found_change:
        return False
    normalized = {
        **extended,
        "characters": normalized_characters,
    }
    return _bible_extends_by_appending_cards(base, normalized)


def _append_compatible_historical_materials(
    episode_id: str,
    *,
    conn: Any,
    material: dict[str, Any],
    contract_version: str,
) -> list[dict[str, Any]]:
    """Rebuild prior authority inputs reachable by card-only appends."""
    if (
        not screenplay_contract_tracks_bible_projection(contract_version)
        or "bible_projection_hash" not in material
    ):
        return []
    episode = conn.execute(
        "SELECT project_id FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if episode is None:
        return []
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?",
        (_episode_value(episode, "project_id", ""),),
    ).fetchone()
    projection = _project_bible_projection(project)
    characters = list(projection.get("characters") or [])
    scenes = list(projection.get("scenes") or [])
    candidates: list[dict[str, Any]] = []
    projection_prefixes: list[tuple[dict[str, Any], str]] = [(
        projection,
        evidence_repository.content_hash(projection),
    )]
    for character_count in range(len(characters), -1, -1):
        for scene_count in range(len(scenes), -1, -1):
            if (
                character_count == len(characters)
                and scene_count == len(scenes)
            ):
                continue
            historical_projection = {
                **projection,
                "characters": characters[:character_count],
                "scenes": scenes[:scene_count],
            }
            projection_hash = evidence_repository.content_hash(
                historical_projection
            )
            projection_prefixes.append((
                historical_projection,
                projection_hash,
            ))
            candidate = {
                **material,
                "bible_projection_hash": projection_hash,
            }
            if not candidate.get("bible_artifact_id"):
                candidate["bible_content_hash"] = projection_hash
            candidates.append(candidate)

    project_id = str(_episode_value(episode, "project_id", "") or "")
    artifact_rows = conn.execute(
        "SELECT id FROM artifacts WHERE type='character_bible' "
        "AND scope_type='project' AND scope_id=? "
        "AND status NOT IN ('rejected','needs_revision') "
        "ORDER BY version DESC",
        (project_id,),
    ).fetchall()

    # A later storyboard run can advance the project pointer through several
    # approved Bible artifacts and then apply an asset-backed appearance
    # update to the mutable projection.  Recover every verified ancestor, not
    # just the immediate predecessor.  Each edge must be an exact immutable
    # parent relation whose semantic payload only appends character/scene
    # cards or scene aliases; one broken edge stops the walk fail-closed.
    current_artifact_id = str(material.get("bible_artifact_id") or "")
    current_artifact = (
        evidence_repository.get_artifact(current_artifact_id)
        if current_artifact_id else None
    )
    if current_artifact is not None:
        current_artifact_projection: dict[str, Any] = {}
        try:
            current_artifact_projection = screenplay_bible_payload(
                current_artifact.get("content") or {}
            )
            _verified_artifact_hash(
                current_artifact,
                label="当前 Bible Artifact",
            )
        except (TypeError, ValueError):
            current_artifact = None
        projection_reachable = bool(
            current_artifact is not None
            and (
                current_artifact_projection == projection
                or _bible_extends_by_appending_cards(
                    current_artifact_projection,
                    projection,
                )
                or _bible_extends_by_recorded_downstream_changes(
                    current_artifact_projection,
                    projection,
                    conn=conn,
                    project_id=project_id,
                    bible_artifact_id=current_artifact_id,
                )
            )
        )
        visited: set[str] = set()
        child = current_artifact if projection_reachable else None
        child_projection = current_artifact_projection
        while child is not None:
            child_id = str(child.get("id") or "")
            if not child_id or child_id in visited:
                break
            visited.add(child_id)
            try:
                child_hash = _verified_artifact_hash(
                    child,
                    label="Bible Artifact 血缘节点",
                )
            except ValueError:
                break
            candidates.append({
                **material,
                "bible_artifact_id": child_id,
                "bible_content_hash": child_hash,
                "bible_projection_hash": evidence_repository.content_hash(
                    child_projection
                ),
            })
            parent_ids = list(child.get("parent_artifact_ids") or [])
            if len(parent_ids) != 1:
                break
            parent = evidence_repository.get_artifact(str(parent_ids[0]))
            if (
                parent is None
                or parent.get("type") != "character_bible"
                or parent.get("scope_type") != "project"
                or parent.get("scope_id") != project_id
                or parent.get("status") in {"rejected", "needs_revision"}
                or int(parent.get("version") or 0)
                >= int(child.get("version") or 0)
            ):
                break
            try:
                parent_projection = screenplay_bible_payload(
                    parent.get("content") or {}
                )
                _verified_artifact_hash(
                    parent,
                    label="Bible Artifact 父节点",
                )
            except (TypeError, ValueError):
                break
            if not (
                _bible_extends_by_appending_cards(
                    parent_projection,
                    child_projection,
                )
                or _bible_extends_by_recorded_downstream_changes(
                    parent_projection,
                    child_projection,
                    conn=conn,
                    project_id=project_id,
                    bible_artifact_id=str(parent.get("id") or ""),
                )
            ):
                break
            child = parent
            child_projection = parent_projection

    for row in artifact_rows:
        artifact = evidence_repository.get_artifact(str(row["id"]))
        if artifact is None:
            continue
        try:
            artifact_projection = screenplay_bible_payload(
                artifact.get("content") or {}
            )
            artifact_hash = _verified_artifact_hash(
                artifact,
                label="历史 Bible Artifact",
            )
        except (TypeError, ValueError):
            continue
        parents = list(artifact.get("parent_artifact_ids") or [])
        predecessor = _projection_before_recorded_bible_append(artifact)
        if predecessor is not None and len(parents) == 1:
            parent = evidence_repository.get_artifact(str(parents[0]))
            if (
                parent is not None
                and parent.get("type") == "character_bible"
                and parent.get("scope_type") == "project"
                and parent.get("scope_id") == project_id
                and int(parent.get("version") or 0)
                < int(artifact.get("version") or 0)
                and _bible_extends_by_appending_cards(
                    predecessor,
                    projection,
                )
            ):
                try:
                    parent_hash = _verified_artifact_hash(
                        parent,
                        label="追加前 Bible Artifact",
                    )
                except ValueError:
                    pass
                else:
                    candidates.append({
                        **material,
                        "bible_artifact_id": str(parent["id"]),
                        "bible_content_hash": parent_hash,
                        "bible_projection_hash": (
                            evidence_repository.content_hash(predecessor)
                        ),
                    })
        for historical_projection, projection_hash in projection_prefixes:
            if not (
                artifact_projection == historical_projection
                or _bible_extends_by_appending_cards(
                    artifact_projection,
                    historical_projection,
                )
                or _bible_extends_by_recorded_downstream_changes(
                    artifact_projection,
                    historical_projection,
                    conn=conn,
                    project_id=project_id,
                    bible_artifact_id=str(artifact["id"]),
                )
            ):
                continue
            candidates.append({
                **material,
                "bible_artifact_id": str(artifact["id"]),
                "bible_content_hash": artifact_hash,
                "bible_projection_hash": evidence_repository.content_hash(
                    artifact_projection
                ),
            })
            break
    return candidates


def _published_authority_input_fingerprint(
    episode_id: str,
    *,
    conn: Any,
    certificate_id: str,
    contract_version: str,
) -> str:
    """Recover append-only Bible growth and legacy duration contamination.

    A screenplay certificate remains valid when the project Bible only gained
    uniquely appended character or scene cards after publication. Reproducing
    the exact signed fingerprint from current list prefixes proves that no
    existing card or non-card field changed.

    Older storyboard runs persisted their derived planning duration back into
    ``episodes.target_duration_s`` after screenplay publication.  The release
    certificate still contains the original complete authority fingerprint.
    Accept that historical row only when replacing this one field with exactly
    one legal product duration reproduces the certificate fingerprint.  Any
    other input drift remains fail-closed.
    """
    material = screenplay_authority_material(
        episode_id,
        conn=conn,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    current_fingerprint = _authority_material_fingerprint(material)
    from app.production.certificate import get_completion_certificate

    certificate = get_completion_certificate(certificate_id, conn=conn)
    if (
        certificate is None
        or not certificate.input_fingerprint
        or certificate.input_fingerprint == current_fingerprint
    ):
        return current_fingerprint

    historical_materials = _append_compatible_historical_materials(
        episode_id,
        conn=conn,
        material=material,
        contract_version=contract_version,
    )
    for candidate in historical_materials:
        if (
            _authority_material_fingerprint(candidate)
            == certificate.input_fingerprint
        ):
            return certificate.input_fingerprint

    constraints = material.get("adaptation_constraints")
    if not isinstance(constraints, dict):
        return current_fingerprint
    current_target = constraints.get("target_duration_s")
    # Historical contamination only affected the former bounded UI choices.
    # Current production duration is unbounded and is never brute-forced here.
    legal_targets = list(config.EPISODE_TARGET_CHOICES)
    if current_target not in legal_targets:
        return current_fingerprint
    matches: list[str] = []
    for base_material in [material, *historical_materials]:
        base_constraints = base_material.get("adaptation_constraints")
        if not isinstance(base_constraints, dict):
            continue
        for target in legal_targets:
            if target == current_target:
                continue
            candidate = {
                **base_material,
                "adaptation_constraints": {
                    **base_constraints,
                    "target_duration_s": target,
                },
            }
            candidate_fingerprint = _authority_material_fingerprint(candidate)
            if candidate_fingerprint == certificate.input_fingerprint:
                matches.append(candidate_fingerprint)
    if len(matches) == 1:
        return matches[0]
    return current_fingerprint


@dataclass(frozen=True)
class ResolvedScreenplayAuthority:
    episode_id: str
    screenplay: EpisodeScreenplay
    source_text: str
    artifact_id: str
    artifact_hash: str
    certificate_id: str
    input_fingerprint: str


@dataclass(frozen=True)
class DownstreamScreenplayContext:
    """Screenplay selected for downstream work and its authority mode."""

    screenplay: EpisodeScreenplay
    narrative_authority_required: bool
    immutable_authority_required: bool


@dataclass(frozen=True)
class ScreenplaySourceProjection:
    source_screenplay: EpisodeScreenplay
    source_projection_hash: str
    published_projection_hash: str
    merged_ir_artifact_id: str
    source_shard_artifact_ids: tuple[str, ...]


def screenplay_action_agency_projection(
    screenplay: EpisodeScreenplay,
) -> dict[str, Any]:
    """Project only source-owned action identity/provenance semantics."""
    plan = screenplay.narrative_plan
    if plan is None:
        return {
            "contract_version": "screenplay-action-agency-projection.v1",
            "actions": [],
        }
    return {
        "contract_version": "screenplay-action-agency-projection.v1",
        "actions": [
            {
                "action_id": action.action_id,
                "actor_ids": list(action.actor_ids),
                "target_ids": list(action.target_ids),
                "action_agency": action.action_agency.model_dump(mode="json"),
            }
            for action in plan.atomic_actions
        ],
    }


def _artifact_ancestors_by_depth(
    artifact: dict[str, Any],
    *,
    conn: Any,
) -> list[tuple[int, dict[str, Any]]]:
    pending = [
        (1, str(parent_id))
        for parent_id in artifact.get("parent_artifact_ids") or []
        if str(parent_id)
    ]
    seen: set[str] = set()
    ancestors: list[tuple[int, dict[str, Any]]] = []
    while pending:
        depth, artifact_id = pending.pop(0)
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        parent = evidence_repository.get_artifact(artifact_id, conn=conn)
        if parent is None:
            continue
        ancestors.append((depth, parent))
        pending.extend(
            (depth + 1, str(parent_id))
            for parent_id in parent.get("parent_artifact_ids") or []
            if str(parent_id) and str(parent_id) not in seen
        )
    return ancestors


def _validated_v6_source_artifacts(
    artifact: dict[str, Any],
    *,
    conn: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    from app.screenplay_scene_shards import SCREENPLAY_SCENE_SHARD_VERSION

    for _depth, candidate in _artifact_ancestors_by_depth(
        artifact,
        conn=conn,
    ):
        if candidate.get("type") != "screenplay_generation_ir_merged":
            continue
        direct_parents = [
            evidence_repository.get_artifact(str(parent_id), conn=conn)
            for parent_id in candidate.get("parent_artifact_ids") or []
        ]
        shard_parents = [
            parent
            for parent in direct_parents
            if parent is not None
            and parent.get("type") == "screenplay_scene_shard"
        ]
        if not shard_parents:
            continue
        has_v6_source = any(
            str(parent.get("contract_version") or "")
            == SCREENPLAY_SCENE_SHARD_VERSION
            for parent in shard_parents
        )
        if not has_v6_source:
            continue
        invalid = [
            parent
            for parent in shard_parents
            if (
                str(parent.get("contract_version") or "")
                != SCREENPLAY_SCENE_SHARD_VERSION
                or parent.get("status") != "validated"
            )
        ]
        if invalid or candidate.get("status") != "validated":
            from app.errors import ArtifactNeedsRebuildError

            raise ArtifactNeedsRebuildError(
                artifact_id=str(artifact.get("id") or ""),
                artifact_type=str(artifact.get("type") or ""),
                reason=(
                    "source projection 依赖的 scene shards/merged IR "
                    "不是完整 validated v6 权威"
                ),
            )
        return candidate, shard_parents
    return None


def _compile_validated_v6_source_projection(
    *,
    episode_id: str,
    artifact: dict[str, Any],
    conn: Any,
) -> tuple[EpisodeScreenplay, str, tuple[str, ...]] | None:
    source_artifacts = _validated_v6_source_artifacts(
        artifact,
        conn=conn,
    )
    if source_artifacts is None:
        return None
    merged_artifact, shard_artifacts = source_artifacts
    from app.errors import ArtifactNeedsRebuildError
    from app.screenplay_ir import IRScene, ScreenplayGenerationIR, compile_screenplay_ir
    from app.screenplay_scene_shards import (
        SCREENPLAY_SCENE_SHARD_VERSION,
        ScreenplaySceneShardIR,
    )

    try:
        merged_ir = ScreenplayGenerationIR.model_validate(
            merged_artifact.get("content") or {}
        )
        shards = [
            ScreenplaySceneShardIR.model_validate(
                shard_artifact.get("content") or {}
            )
            for shard_artifact in shard_artifacts
        ]
    except Exception as exc:
        raise ArtifactNeedsRebuildError(
            artifact_id=str(artifact.get("id") or ""),
            artifact_type=str(artifact.get("type") or ""),
            reason=f"validated v6 source projection 无法解析：{exc}",
        ) from exc
    if any(
        shard.contract_version != SCREENPLAY_SCENE_SHARD_VERSION
        for shard in shards
    ):
        raise ArtifactNeedsRebuildError(
            artifact_id=str(artifact.get("id") or ""),
            artifact_type=str(artifact.get("type") or ""),
            reason="validated v6 source projection 内容合同漂移",
        )

    source_scenes: dict[str, IRScene] = {}
    for shard in sorted(shards, key=lambda item: item.shard_id):
        for scene in shard.scenes:
            if scene.key in source_scenes:
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(artifact.get("id") or ""),
                    artifact_type=str(artifact.get("type") or ""),
                    reason=(
                        "validated v6 source projection 含重复 scene："
                        f"{scene.key}"
                    ),
                )
            source_scenes[scene.key] = IRScene.model_validate(
                scene.model_dump(mode="json")
            )
    merged_scene_keys = [scene.key for scene in merged_ir.scenes]
    if set(source_scenes) != set(merged_scene_keys):
        raise ArtifactNeedsRebuildError(
            artifact_id=str(artifact.get("id") or ""),
            artifact_type=str(artifact.get("type") or ""),
            reason="validated v6 source shards 与 merged IR 场次集合漂移",
        )
    ordered_source_scenes = [
        source_scenes[scene_key] for scene_key in merged_scene_keys
    ]
    if [
        scene.model_dump(mode="json") for scene in ordered_source_scenes
    ] != [
        scene.model_dump(mode="json") for scene in merged_ir.scenes
    ]:
        raise ArtifactNeedsRebuildError(
            artifact_id=str(artifact.get("id") or ""),
            artifact_type=str(artifact.get("type") or ""),
            reason="validated v6 source shards 与 merged IR 内容漂移",
        )
    merged_ir.scenes = ordered_source_scenes

    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?",
        (_episode_value(episode, "project_id"),),
    ).fetchone()
    bible_projection = _project_bible_projection(project)
    bible = Bible.model_validate(bible_projection or {})
    _records, source_text = _source_records(conn, episode)
    episode_input = dict(episode)
    episode_input["character_resolutions"] = _decode_list(
        _episode_value(episode, "screenplay_character_resolutions", "[]")
    )
    episode_input["authorized_source_chapters"] = (
        screenplay_authorized_source_chapters(episode_id, conn=conn)
    )
    try:
        source_screenplay = compile_screenplay_ir(
            merged_ir,
            episode=episode_input,
            source_text=source_text,
            bible=bible,
        )
    except Exception as exc:
        raise ArtifactNeedsRebuildError(
            artifact_id=str(artifact.get("id") or ""),
            artifact_type=str(artifact.get("type") or ""),
            reason=f"validated v6 source projection 无法重新编译：{exc}",
        ) from exc
    return (
        source_screenplay,
        str(merged_artifact.get("id") or ""),
        tuple(
            str(shard_artifact.get("id") or "")
            for shard_artifact in shard_artifacts
        ),
    )


def assert_screenplay_matches_validated_v6_source(
    *,
    episode_id: str,
    artifact: dict[str, Any],
    screenplay: EpisodeScreenplay,
    conn: Any | None = None,
    mark_stale: bool = True,
) -> ScreenplaySourceProjection | None:
    """Fail closed when a v6 shard-derived action projection drifted."""
    db = conn or get_conn()
    try:
        source = _compile_validated_v6_source_projection(
            episode_id=episode_id,
            artifact=artifact,
            conn=db,
        )
        if source is None:
            return None
        source_screenplay, merged_artifact_id, shard_artifact_ids = source
        source_projection_hash = evidence_repository.content_hash(
            screenplay_action_agency_projection(source_screenplay)
        )
        published_projection_hash = evidence_repository.content_hash(
            screenplay_action_agency_projection(screenplay)
        )
        if source_projection_hash != published_projection_hash:
            from app.errors import ArtifactNeedsRebuildError

            raise ArtifactNeedsRebuildError(
                artifact_id=str(artifact.get("id") or ""),
                artifact_type=str(artifact.get("type") or ""),
                reason=(
                    "source projection hash 与 published screenplay 漂移："
                    f"source={source_projection_hash}, "
                    f"published={published_projection_hash}, "
                    f"merged_ir={merged_artifact_id}"
                ),
            )
        return ScreenplaySourceProjection(
            source_screenplay=source_screenplay,
            source_projection_hash=source_projection_hash,
            published_projection_hash=published_projection_hash,
            merged_ir_artifact_id=merged_artifact_id,
            source_shard_artifact_ids=shard_artifact_ids,
        )
    except Exception as exc:
        from app.errors import ArtifactNeedsRebuildError

        if not isinstance(exc, ArtifactNeedsRebuildError):
            raise
        if mark_stale:
            db.execute(
                "UPDATE artifacts SET status='stale',stale_reason=? "
                "WHERE id=? AND status!='rejected'",
                (str(exc), str(artifact.get("id") or "")),
            )
            db.commit()
        raise


def episode_requires_immutable_screenplay_authority(
    episode: Any,
    *,
    conn: Any | None = None,
) -> bool:
    """Return whether legacy projection-only handling is no longer allowed.

    The decision is monotonic: durable release evidence or a narrative plan in
    either immutable Artifact or mutable projection can require authority; an
    empty or downgraded projection can never turn those facts off.
    """
    del conn  # Artifact repository owns the authoritative storage connection.
    if any(
        _episode_value(episode, field, "")
        for field in (
            "screenplay_completion_certificate_id",
            "screenplay_production_revision_id",
            "narrative_review_artifact_id",
            "narrative_calibration_artifact_id",
        )
    ):
        return True
    artifact_id = str(
        _episode_value(episode, "published_screenplay_artifact_id", "") or ""
    )
    if artifact_id:
        artifact = evidence_repository.get_artifact(artifact_id)
        if artifact is not None:
            try:
                from app.production.patch import load_screenplay_from_artifact

                if load_screenplay_from_artifact(artifact_id).narrative_plan is not None:
                    return True
            except Exception:
                # A present but unreadable published Artifact is authority drift,
                # not evidence that this episode is safely legacy.
                return True
    raw_projection = _episode_value(episode, "screenplay_json", "")
    if raw_projection:
        try:
            return (
                _validated_screenplay_projection(raw_projection).narrative_plan
                is not None
            )
        except Exception:
            # Malformed historical projections are handled by their caller;
            # without durable evidence they do not acquire modern authority.
            return False
    return False


def resolve_downstream_screenplay(
    episode_id: str,
    *,
    conn: Any | None = None,
) -> DownstreamScreenplayContext:
    """Resolve downstream screenplay without trusting a mutable downgrade.

    Historical episodes may only have a page projection.  Once an immutable
    production revision/certificate exists, or the published Artifact contains
    a typed narrative plan, every downstream consumer must use the complete
    authority resolver.  The mutable ``screenplay_json`` can tighten this
    requirement but can never relax it by deleting ``narrative_plan``.
    """
    db = conn or get_conn()
    episode = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError("剧集不存在")
    raw_projection = _episode_value(episode, "screenplay_json", "")
    if not raw_projection:
        raise ValueError("当前剧集缺少剧本投影")
    try:
        projection = _validated_screenplay_projection(raw_projection)
    except Exception as exc:
        if not episode_requires_immutable_screenplay_authority(
            episode,
            conn=db,
        ):
            return DownstreamScreenplayContext(
                screenplay=EpisodeScreenplay(
                    episode_no=int(
                        _episode_value(episode, "episode_no", 1) or 1
                    ),
                ),
                narrative_authority_required=False,
                immutable_authority_required=False,
            )
        raise ValueError(f"当前剧本投影无法验证：{exc}") from exc

    durable_authority = episode_requires_immutable_screenplay_authority(
        episode,
        conn=db,
    )
    published_requires_narrative = False
    artifact_id = str(
        _episode_value(episode, "published_screenplay_artifact_id", "") or ""
    )
    if artifact_id:
        artifact = evidence_repository.get_artifact(artifact_id)
        if artifact is not None:
            from app.production.patch import load_screenplay_from_artifact

            try:
                artifact_screenplay = load_screenplay_from_artifact(artifact_id)
            except Exception as exc:
                raise ValueError(f"已发布剧本 Artifact 无法解析：{exc}") from exc
            if (
                screenplay_contract_requires_narrative(
                    str(artifact.get("contract_version") or "")
                )
                and artifact_screenplay.narrative_plan is None
            ):
                raise ValueError(
                    "已发布剧本合同要求 narrative_plan，但 Artifact 缺失该权威图"
                )
            published_requires_narrative = artifact_screenplay.narrative_plan is not None

    immutable_required = bool(
        durable_authority
        or published_requires_narrative
        or projection.narrative_plan is not None
    )
    if not immutable_required:
        return DownstreamScreenplayContext(
            screenplay=projection,
            narrative_authority_required=False,
            immutable_authority_required=False,
        )
    resolved = resolve_current_screenplay_authority(
        episode_id,
        conn=db,
        require_narrative=bool(
            published_requires_narrative or projection.narrative_plan is not None
        ),
    )
    return DownstreamScreenplayContext(
        screenplay=resolved.screenplay,
        narrative_authority_required=resolved.screenplay.narrative_plan is not None,
        immutable_authority_required=True,
    )


def resolve_current_screenplay_authority(
    episode_id: str,
    *,
    conn: Any | None = None,
    require_narrative: bool = True,
) -> ResolvedScreenplayAuthority:
    """Resolve one immutable published screenplay or fail closed on any drift."""
    db = conn or get_conn()
    episode = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError("剧集不存在")
    artifact_id = str(
        _episode_value(episode, "published_screenplay_artifact_id", "") or ""
    )
    projection_artifact_id = str(
        _episode_value(episode, "screenplay_artifact_id", "") or ""
    )
    if not artifact_id or artifact_id != projection_artifact_id:
        raise ValueError("当前剧本投影未绑定唯一已发布 Artifact")
    artifact = evidence_repository.get_artifact(artifact_id)
    if (
        artifact is None
        or artifact.get("type") != "screenplay_document"
        or artifact.get("scope_type") != "episode"
        or artifact.get("scope_id") != episode_id
        or artifact.get("status") != "approved"
    ):
        raise ValueError("已发布剧本 Artifact 的类型、作用域或状态无效")
    artifact_hash = _verified_artifact_hash(artifact, label="已发布剧本 Artifact")
    from app.production.patch import load_screenplay_from_artifact

    screenplay = load_screenplay_from_artifact(artifact_id)
    assert_screenplay_matches_validated_v6_source(
        episode_id=episode_id,
        artifact=artifact,
        screenplay=screenplay,
        conn=db,
    )
    raw_projection = _episode_value(episode, "screenplay_json", "")
    if not raw_projection:
        raise ValueError("已发布剧本缺少页面投影")
    projection = _validated_screenplay_projection(raw_projection)
    if projection.model_dump(mode="json") != screenplay.model_dump(mode="json"):
        raise ValueError("页面 screenplay_json 与已发布 Artifact 内容漂移")
    if require_narrative and screenplay.narrative_plan is None:
        raise ValueError("已发布剧本缺少叙事权威合同")

    certificate_id = str(
        _episode_value(episode, "screenplay_completion_certificate_id", "") or ""
    )
    revision_id = str(
        _episode_value(episode, "screenplay_production_revision_id", "") or ""
    )
    if not certificate_id or not revision_id:
        raise ValueError("已发布剧本缺少当前完成凭证或 revision")
    contract_version = str(artifact.get("contract_version") or "")
    input_fingerprint = _published_authority_input_fingerprint(
        episode_id,
        conn=db,
        certificate_id=certificate_id,
        contract_version=contract_version,
    )
    from app.production.certificate import verify_completion_certificate

    cert = verify_completion_certificate(
        certificate_id,
        expected_kind="screenplay",
        expected_scope_id=episode_id,
        expected_artifact_id=artifact_id,
        expected_artifact_hash=artifact_hash,
        expected_input_fingerprint=input_fingerprint,
        expected_contract_version=contract_version,
        expected_qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        expected_production_revision_id=revision_id,
        allow_consumed=True,
    )
    if cert.consumed_at is None:
        raise ValueError("剧本完成凭证尚未被原子发布消费")

    revision = db.execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone()
    if (
        revision is None
        or revision["kind"] != "screenplay"
        or revision["episode_id"] != episode_id
        or revision["status"] != "published"
        or revision["working_artifact_id"] != artifact_id
        or revision["published_artifact_id"] != artifact_id
        or str(revision["input_fingerprint"] or "") != input_fingerprint
        or str(revision["contract_version"] or "") != contract_version
        or str(revision["qa_profile_version"] or "")
        != SCREENPLAY_QA_PROFILE_VERSION
    ):
        raise ValueError("剧本 production revision 与当前已发布权威链漂移")

    evaluation_ids = list(cert.evaluation_ids)
    if not evaluation_ids:
        raise ValueError("剧本完成凭证缺少 QA 证据")
    marks = ",".join("?" for _ in evaluation_ids)
    evaluations = db.execute(
        f"SELECT * FROM evaluations WHERE id IN ({marks})", evaluation_ids,
    ).fetchall()
    qa_rows = [
        row for row in evaluations
        if row["evaluator_name"] == "screenplay_production_qa"
    ]
    if len(qa_rows) != 1:
        raise ValueError("剧本权威链必须精确绑定一个生产 QA")
    qa_row = qa_rows[0]
    try:
        issues = json.loads(qa_row["issues_json"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        issues = None
    score_only = (
        qa_row["evaluation_role"] == "score_only"
        and not bool(qa_row["runtime_blocking"])
    )
    legacy_runtime_gate = (
        qa_row["evaluation_role"] == "runtime_gate"
        and bool(qa_row["runtime_blocking"])
        and qa_row["status"] == "passed"
        and bool(qa_row["hard_gate_passed"])
    )
    if (
        qa_row["artifact_id"] != artifact_id
        or qa_row["evaluator_version"] != SCREENPLAY_QA_PROFILE_VERSION
        or not (score_only or legacy_runtime_gate)
        or not isinstance(issues, list)
        or (
            legacy_runtime_gate
            and any(
                isinstance(issue, dict)
                and (
                    str(issue.get("severity") or "").lower() == "blocker"
                    or bool(issue.get("must_fix"))
                )
                for issue in (issues or [])
            )
        )
    ):
        raise ValueError("剧本质量评分 Evaluation 已漂移或版本不匹配")
    try:
        evidence = json.loads(qa_row["evidence_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("剧本 QA 证据无法解析") from exc
    if evidence.get("authority_input_fingerprint") != input_fingerprint:
        raise ValueError("剧本 QA 与当前原文/Bible/改编约束指纹不一致")

    _records, source_text = _source_records(db, episode)
    return ResolvedScreenplayAuthority(
        episode_id=episode_id,
        screenplay=screenplay,
        source_text=source_text,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        certificate_id=certificate_id,
        input_fingerprint=input_fingerprint,
    )
