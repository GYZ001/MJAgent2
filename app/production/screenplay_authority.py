"""Immutable screenplay/source authority resolution for downstream narrative work."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from threading import RLock
import hashlib
import json
import re
from typing import Any

from app import config
from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.ingest import chapter_is_stub, chapter_titles_match
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    KeyDialogueChain,
    KeyDialogueTurn,
    PrepPackCharacterAsset,
    PrepPackSceneAsset,
    ScriptScene,
)
from app.spoken_contract import content_char_count


SCREENPLAY_QA_PROFILE_VERSION = "screenplay-qa-gate-4"


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
    """Return whether this contract generation requires typed narrative authority.

    Only the retired heavy blueprint/scene-shard pipeline (major 3-5) produces
    a ``narrative_plan``. Contract 6.0.0+ is the lightweight episode_prep_pack
    pipeline (docs/TRANSFORM_FREEZE_PLAN.md), which has no narrative_plan
    concept at all -- it must not be routed into the narrative-authority gate.
    """
    major = _contract_major(contract_version)
    return 3 <= major < 6


def screenplay_contract_tracks_bible_projection(
    contract_version: str | None,
) -> bool:
    """Return whether the screenplay binds the composed project Bible view."""
    return _contract_major(contract_version) >= 4


def screenplay_contract_is_prep_pack(contract_version: str | None) -> bool:
    """Return whether this contract's artifact self-declares as
    episode_prep_pack (screenplay contract 6.0.0+).

    This is an EXPLICIT declaration and must win over proxy inference (see
    app.production.patch._historical_screenplay_artifact_is_bound, which
    treats "referenced by production_revisions/completion_certificates/
    episode pointers" as evidence of being an old heavy-pipeline artifact --
    a prep_pack artifact gets bound to those same tables/pointers by its own,
    different publish path, so that proxy false-positives on every prep_pack
    publish. Caught via a real EP1 run: the startup recovery sweep
    (recover_screenplay_tasks) flipped a freshly-published, fully valid
    'ready' episode to 'failed' with ARTIFACT_NEEDS_REBUILD on the very next
    process restart).
    """
    return _contract_major(contract_version) >= 6


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
    try:
        return evidence_repository.verified_artifact_content_hash(artifact)
    except ValueError as exc:
        raise ValueError(f"{label} 当前内容无法重新计算指纹") from exc


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
    #
    # hook/cliffhanger are normalized to a fixed "" here rather than read from
    # the episode row. docs/PROMPT_SPEC.md marks both as editable display
    # metadata that must never substitute for source chapters as screenplay
    # or storyboard evidence, so they were never meant to be authority
    # material. Since app/production/publish.py now mirrors this episode's
    # resolved ending_hook into episodes.cliffhanger (and the previous
    # episode's ending_hook into episodes.hook) inside the same transaction
    # that publishes the screenplay, the DB value can legitimately change
    # after this certificate is issued -- and did so unconditionally through
    # every already-issued certificate's history, since the only write path
    # before that mirroring existed (app/planning.py's episode INSERT) always
    # set both columns to "". Fixing them here reproduces the exact byte-for-
    # byte material every historical certificate was already computed from,
    # so this is 100% backward compatible, and it removes the field from the
    # fingerprint entirely -- no future write path can smuggle unverified
    # content into a resolved authority fingerprint through it.
    constraints = {
        "title": _episode_value(episode, "title", "") or "",
        "hook": "",
        "cliffhanger": "",
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
        # v1 is an immutable certificate serialization.  Keep the exact
        # historical rows here; runtime normalization belongs only to compile /
        # recovery boundaries and must not invalidate an issued fingerprint.
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
        try:
            _verified_artifact_hash(artifact, label="approved portrait Artifact")
        except ValueError:
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
    """Project compiler-owned action and text attribution semantics."""
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
                "text_provenance": action.text_provenance.model_dump(
                    mode="json"
                ),
                "dialogue_text": action.dialogue_text,
                "required_text": action.required_text,
                "prop_text": action.prop_text,
                "on_screen_text": action.on_screen_text,
            }
            for action in plan.atomic_actions
        ],
    }


def screenplay_action_agency_errors(
    screenplay: EpisodeScreenplay,
) -> list[str]:
    plan = screenplay.narrative_plan
    if plan is None:
        return []
    errors: list[str] = []
    for action in plan.atomic_actions:
        has_relation = bool(action.actor_ids or action.target_ids)
        if action.action_agency.identity_bearing != has_relation:
            errors.append(
                f"{action.action_id} identity_bearing 与 actor/target 不等价"
            )
        if action.action_agency.is_character_agency and not has_relation:
            errors.append(
                f"{action.action_id} character agency 缺少 actor/target 关系"
            )
        explicit_text_kinds = [
            kind
            for kind, content in (
                ("dialogue", action.dialogue_text),
                ("required_text", action.required_text),
                ("prop_text", action.prop_text),
                ("on_screen_text", action.on_screen_text),
            )
            if content.strip()
        ]
        expected_kind = (
            explicit_text_kinds[0]
            if explicit_text_kinds
            else "creative_action"
        )
        expected_identity_keys = (
            []
            if expected_kind in (
                "required_text", "prop_text", "on_screen_text",
            )
            else list(dict.fromkeys([
                *action.actor_ids,
                *action.target_ids,
            ]))
        )
        if len(explicit_text_kinds) > 1:
            errors.append(
                f"{action.action_id} 含多个冲突的文字结构字段"
            )
        if action.text_provenance.kind != expected_kind:
            errors.append(
                f"{action.action_id} text provenance kind 未由文字结构确定"
            )
        if action.text_provenance.identity_keys != expected_identity_keys:
            errors.append(
                f"{action.action_id} text provenance identity "
                "未由 actor/target 关系确定"
            )
        if (
            action.text_provenance.source_segment_ids
            != action.action_agency.source_segment_ids
        ):
            errors.append(
                f"{action.action_id} text provenance 与 agency 来源不等价"
            )
    return errors


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


def _validated_v7_source_artifacts(
    artifact: dict[str, Any],
    *,
    conn: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    from app.screenplay_scene_shards import (
        SCREENPLAY_MERGED_IR_VERSION,
        SCREENPLAY_SCENE_SHARD_VERSION,
    )
    from app.errors import ArtifactNeedsRebuildError

    ancestors = _artifact_ancestors_by_depth(
        artifact,
        conn=conn,
    )
    for _depth, candidate in ancestors:
        if candidate.get("type") != "screenplay_generation_ir_merged":
            continue
        try:
            _verified_artifact_hash(candidate, label="validated merged IR Artifact")
        except ValueError as exc:
            raise ArtifactNeedsRebuildError(
                artifact_id=str(artifact.get("id") or ""),
                artifact_type=str(artifact.get("type") or ""),
                reason=str(exc),
            ) from exc
        direct_parent_ids = [
            str(parent_id)
            for parent_id in candidate.get("parent_artifact_ids") or []
            if str(parent_id)
        ]
        direct_parents = [
            evidence_repository.get_artifact(str(parent_id), conn=conn)
            for parent_id in direct_parent_ids
        ]
        if any(parent is None for parent in direct_parents):
            raise ArtifactNeedsRebuildError(
                artifact_id=str(artifact.get("id") or ""),
                artifact_type=str(artifact.get("type") or ""),
                reason="merged IR lineage 缺少已声明的直接父 Artifact",
            )
        authority_parent_types = {
            "screenplay_narrative_blueprint",
            "screenplay_identity_registry",
            "screenplay_envelope",
            "screenplay_scene_shard",
        }
        try:
            for direct_parent in direct_parents:
                if (
                    direct_parent is not None
                    and direct_parent.get("type") in authority_parent_types
                ):
                    _verified_artifact_hash(
                        direct_parent,
                        label="validated screenplay lineage Artifact",
                    )
        except ValueError as exc:
            raise ArtifactNeedsRebuildError(
                artifact_id=str(artifact.get("id") or ""),
                artifact_type=str(artifact.get("type") or ""),
                reason=str(exc),
            ) from exc
        shard_parents = [
            parent
            for parent in direct_parents
            if parent is not None
            and parent.get("type") == "screenplay_scene_shard"
        ]
        if not shard_parents:
            raise ArtifactNeedsRebuildError(
                artifact_id=str(artifact.get("id") or ""),
                artifact_type=str(artifact.get("type") or ""),
                reason="merged IR lineage 缺少 validated scene shard 父链",
            )
        try:
            for shard_parent in shard_parents:
                _verified_artifact_hash(
                    shard_parent,
                    label="validated scene shard Artifact",
                )
        except ValueError as exc:
            raise ArtifactNeedsRebuildError(
                artifact_id=str(artifact.get("id") or ""),
                artifact_type=str(artifact.get("type") or ""),
                reason=str(exc),
            ) from exc
        has_current_source = any(
            str(parent.get("contract_version") or "")
            == SCREENPLAY_SCENE_SHARD_VERSION
            for parent in shard_parents
        )
        if not has_current_source:
            raise ArtifactNeedsRebuildError(
                artifact_id=str(artifact.get("id") or ""),
                artifact_type=str(artifact.get("type") or ""),
                reason="scene shard lineage 仍使用旧 state-subject 合同",
            )
        invalid = [
            parent
            for parent in shard_parents
            if (
                str(parent.get("contract_version") or "")
                != SCREENPLAY_SCENE_SHARD_VERSION
                or parent.get("status") != "validated"
            )
        ]
        if (
            invalid
            or candidate.get("status") != "validated"
            or str(candidate.get("contract_version") or "")
            != SCREENPLAY_MERGED_IR_VERSION
        ):
            raise ArtifactNeedsRebuildError(
                artifact_id=str(artifact.get("id") or ""),
                artifact_type=str(artifact.get("type") or ""),
                reason=(
                    "source projection 依赖的 scene shards/merged IR "
                    "不是完整的当前 validated 权威"
                ),
            )
        return candidate, shard_parents
    if any(
        candidate.get("type")
        in {"screenplay_generation_ir_merged", "screenplay_scene_shard"}
        for _depth, candidate in ancestors
    ):
        raise ArtifactNeedsRebuildError(
            artifact_id=str(artifact.get("id") or ""),
            artifact_type=str(artifact.get("type") or ""),
            reason="scene shard lineage 未包含完整当前 merged IR 权威",
        )
    return None


# 重编译已发布剧本的「场次源投影」是纯函数：输入全部是不可变 Artifact
# （已发布剧本 / merged IR / 各 scene shard，都按 content_hash 封印）加上本集的
# episodes / projects 行与原文章节。实测一次重编译 437 ms（其中
# compile_screenplay_ir 339 ms），而剧本台每次打开或轮询都要跑一次。
# 这里按「全部输入的内容指纹」缓存结果：任何一个输入变化都会改变键，
# 因此缓存命中与重算在语义上完全等价，不存在读到过期权威的可能。
# 与 patch.py 同理，缓存里存的是序列化 JSON，保证每个调用方拿到独立可变模型。
_V7_SOURCE_PROJECTION_CACHE: OrderedDict[
    str, tuple[str, str, tuple[str, ...]]
] = OrderedDict()
_V7_SOURCE_PROJECTION_CACHE_SIZE = 8
_V7_SOURCE_PROJECTION_CACHE_LOCK = RLock()


def _row_fingerprint(row: Any) -> str:
    """Content fingerprint of one sqlite row, independent of column order."""
    if row is None:
        return "none"
    data = dict(row)
    return hashlib.blake2b(
        json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        ),
        digest_size=16,
    ).hexdigest()


def _v7_source_projection_cache_key(
    *,
    episode_id: str,
    artifact: dict[str, Any],
    merged_artifact: dict[str, Any],
    shard_artifacts: list[dict[str, Any]],
    episode_row: Any,
    project_row: Any,
    source_text: str,
) -> str:
    from app.screenplay_scene_shards import (
        SCREENPLAY_MERGED_IR_VERSION,
        SCREENPLAY_SCENE_SHARD_VERSION,
    )
    from app.schemas import NARRATIVE_CONTRACT_VERSION

    material = {
        "episode_id": episode_id,
        "published": [
            str(artifact.get("id") or ""),
            str(artifact.get("content_hash") or ""),
            str(artifact.get("contract_version") or ""),
        ],
        "merged": [
            str(merged_artifact.get("id") or ""),
            str(merged_artifact.get("content_hash") or ""),
        ],
        "shards": sorted(
            [
                str(shard.get("id") or ""),
                str(shard.get("content_hash") or ""),
            ]
            for shard in shard_artifacts
        ),
        "episode_row": _row_fingerprint(episode_row),
        "project_row": _row_fingerprint(project_row),
        "source_text": hashlib.blake2b(
            source_text.encode("utf-8"), digest_size=16
        ).hexdigest(),
        "contracts": [
            SCREENPLAY_MERGED_IR_VERSION,
            SCREENPLAY_SCENE_SHARD_VERSION,
            NARRATIVE_CONTRACT_VERSION,
            SCREENPLAY_QA_PROFILE_VERSION,
        ],
    }
    return hashlib.blake2b(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        digest_size=20,
    ).hexdigest()


def _compile_validated_v7_source_projection(
    *,
    episode_id: str,
    artifact: dict[str, Any],
    conn: Any,
) -> tuple[EpisodeScreenplay, str, tuple[str, ...]] | None:
    source_artifacts = _validated_v7_source_artifacts(
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
    _records, source_text = _source_records(conn, episode)
    cache_key = _v7_source_projection_cache_key(
        episode_id=episode_id,
        artifact=artifact,
        merged_artifact=merged_artifact,
        shard_artifacts=shard_artifacts,
        episode_row=episode,
        project_row=project,
        source_text=source_text,
    )
    with _V7_SOURCE_PROJECTION_CACHE_LOCK:
        cached = _V7_SOURCE_PROJECTION_CACHE.get(cache_key)
        if cached is not None:
            _V7_SOURCE_PROJECTION_CACHE.move_to_end(cache_key)
    if cached is not None:
        cached_json, cached_merged_id, cached_shard_ids = cached
        return (
            EpisodeScreenplay.model_validate_json(cached_json),
            cached_merged_id,
            cached_shard_ids,
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
            reason=f"validated scene source projection 无法解析：{exc}",
        ) from exc
    if any(
        shard.contract_version != SCREENPLAY_SCENE_SHARD_VERSION
        for shard in shards
    ):
        raise ArtifactNeedsRebuildError(
            artifact_id=str(artifact.get("id") or ""),
            artifact_type=str(artifact.get("type") or ""),
            reason="validated scene source projection 内容合同漂移",
        )

    source_scenes: dict[str, IRScene] = {}
    for shard in sorted(shards, key=lambda item: item.shard_id):
        for scene in shard.scenes:
            if scene.key in source_scenes:
                raise ArtifactNeedsRebuildError(
                    artifact_id=str(artifact.get("id") or ""),
                    artifact_type=str(artifact.get("type") or ""),
                    reason=(
                        "validated scene source projection 含重复 scene："
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
            reason="validated scene source shards 与 merged IR 场次集合漂移",
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
            reason="validated scene source shards 与 merged IR 内容漂移",
        )
    merged_ir.scenes = ordered_source_scenes

    bible_projection = _project_bible_projection(project)
    bible = Bible.model_validate(bible_projection or {})
    episode_input = dict(episode)
    from app.portraits import load_screenplay_character_resolutions_for_source

    episode_input["character_resolutions"] = (
        load_screenplay_character_resolutions_for_source(
            conn,
            episode_id,
            episode_no=int(_episode_value(episode, "episode_no", 0) or 0),
            source_text=source_text,
        )
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
            reason=f"validated scene source projection 无法重新编译：{exc}",
        ) from exc
    merged_artifact_id = str(merged_artifact.get("id") or "")
    shard_artifact_ids = tuple(
        str(shard_artifact.get("id") or "")
        for shard_artifact in shard_artifacts
    )
    with _V7_SOURCE_PROJECTION_CACHE_LOCK:
        _V7_SOURCE_PROJECTION_CACHE[cache_key] = (
            source_screenplay.model_dump_json(),
            merged_artifact_id,
            shard_artifact_ids,
        )
        _V7_SOURCE_PROJECTION_CACHE.move_to_end(cache_key)
        while (
            len(_V7_SOURCE_PROJECTION_CACHE)
            > _V7_SOURCE_PROJECTION_CACHE_SIZE
        ):
            _V7_SOURCE_PROJECTION_CACHE.popitem(last=False)
    return (source_screenplay, merged_artifact_id, shard_artifact_ids)


def assert_screenplay_matches_validated_v7_source(
    *,
    episode_id: str,
    artifact: dict[str, Any],
    screenplay: EpisodeScreenplay,
    conn: Any | None = None,
    mark_stale: bool = True,
) -> ScreenplaySourceProjection | None:
    """Fail closed when the current shard-derived attribution drifts."""
    db = conn or get_conn()
    try:
        source = _compile_validated_v7_source_projection(
            episode_id=episode_id,
            artifact=artifact,
            conn=db,
        )
        if source is None:
            return None
        source_screenplay, merged_artifact_id, shard_artifact_ids = source
        for projection_name, candidate in (
            ("validated scene source", source_screenplay),
            ("published screenplay", screenplay),
        ):
            agency_errors = screenplay_action_agency_errors(candidate)
            if agency_errors:
                from app.errors import ArtifactNeedsRebuildError

                raise ArtifactNeedsRebuildError(
                    artifact_id=str(artifact.get("id") or ""),
                    artifact_type=str(artifact.get("type") or ""),
                    reason=(
                        f"{projection_name} action agency 合同失效："
                        + "；".join(agency_errors[:20])
                    ),
                )
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
            stale_id = str(artifact.get("id") or "")
            db.execute(
                "UPDATE artifacts SET status='stale',stale_reason=? "
                "WHERE id=? AND status!='rejected'",
                (str(exc), stale_id),
            )
            db.commit()
            # 这条写入可以发生在只读端点开着读作用域的时候（剧本台首屏 →
            # resolve_current_screenplay_authority → 这里）。不失效的话，
            # 同一次请求里后续对这份 artifact 的读取仍会拿到写前的
            # status='approved'，把刚判定出来的 stale 掩盖掉。
            evidence_repository.invalidate_artifact_read_scope(stale_id)
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


def published_stale_screenplay_rebuild_error(
    episode: Any,
    *,
    conn: Any | None = None,
):
    """Return the typed rebuild error for the bound stale published Artifact.

    A stale reason is diagnostic text, not an authority contract. Unknown
    validation failures must propagate so callers fail closed.
    """
    from app.errors import ArtifactNeedsRebuildError
    from app.production.patch import screenplay_from_artifact_record

    artifact_id = str(
        _episode_value(episode, "published_screenplay_artifact_id", "") or ""
    )
    if (
        not artifact_id
        or artifact_id
        != str(_episode_value(episode, "screenplay_artifact_id", "") or "")
    ):
        return None
    episode_id = str(_episode_value(episode, "id", "") or "")
    artifact = evidence_repository.get_artifact(artifact_id, conn=conn)
    if (
        artifact is None
        or artifact.get("type") != "screenplay_document"
        or artifact.get("scope_type") != "episode"
        or artifact.get("scope_id") != episode_id
        or artifact.get("status") != "stale"
    ):
        return None
    try:
        screenplay = screenplay_from_artifact_record(artifact)
        assert_screenplay_matches_validated_v7_source(
            episode_id=episode_id,
            artifact=artifact,
            screenplay=screenplay,
            conn=conn,
            mark_stale=False,
        )
    except ArtifactNeedsRebuildError as exc:
        if (
            exc.artifact_id == artifact_id
            and exc.artifact_type == "screenplay_document"
        ):
            return exc
        raise
    return None


# ---------------------------------------------------------------------------
# episode_prep_pack projection (screenplay contract 6.0.0+, see
# docs/TRANSFORM_FREEZE_PLAN.md P1). The storyboard stage still consumes the
# legacy EpisodeScreenplay shape; this section projects the lightweight
# prep_pack payload into that shape deterministically instead of rewriting
# the storyboard's consumption logic. See resolve_current_screenplay_authority
# and app.production.patch.screenplay_from_artifact_record for the two call
# sites that must route a prep_pack payload here instead of the legacy parse.
# ---------------------------------------------------------------------------

#: Recorded on ``EpisodeScreenplay.script_format_note`` for every projected
#: prep_pack screenplay. ``full_script_text`` on that object is NOT authored
#: prose -- it is a deterministic splice of verbatim quotes already inside
#: the published prep_pack (source_evidence[].quote + key_lines[].line),
#: ordered by segment_index. This marker lets any caller tell the two shapes
#: apart without re-deriving it, without leaking a disclaimer sentence into
#: the storyboard prompt text itself (which reads full_script_text directly).
PREP_PACK_PROJECTION_FORMAT_NOTE = "prep_pack_quote_splice:v1"


def is_prep_pack_payload(payload: Any) -> bool:
    """Return whether ``payload`` is a raw episode_prep_pack dict.

    The marker is the payload's own ``prep_pack_version`` key -- the same
    explicit self-declaration ``app.domain.common._load_screenplay`` already
    keys off of (see its docstring). Centralized here so every parse site
    that might see either the legacy EpisodeScreenplay shape or the
    episode_prep_pack shape uses the identical predicate.
    """
    return isinstance(payload, dict) and "prep_pack_version" in payload


_SENTENCE_UNIT_RE = re.compile(r".*?[。！？…]|.+$")


def _split_prep_pack_spoken_line(value: str, *, max_chars: int) -> list[str]:
    """Prep_pack 专用口播切分：句子优先，只有单句本身仍超容量才退到旧算法。

    真实回归（EP6 run_9bfcd5cbe128，大纲被判「未安排 3 条必保留关键台词」，2026-08-25）
    定位到两条根因，此函数只处理其中一条：`app.screenplay_ir._split_spoken_line`
    把句号和逗号当成同权重的切分点，贪心地尽量堆满每个单元，容易把一句话切在句中
    逗号处，产出以「，」收尾、单独看不像完整台词的半句。

    这里先按句末标点（。！？…）切出"句子"单元，再对句子做同样的贪心堆叠；只有
    单个句子本身仍超过 max_chars 时，才对那一句退到 `_split_spoken_line` 的逗号/
    字级兜底。相比旧算法（逗号、句号一视同仁），这保证：一段引述只要由多句话组成，
    切分结果里任何一刀都不会落在某句话中途——旧算法在"上一句尾部 + 下一句头部
    的某个逗号小句"恰好能塞进同一单元时会这么做。

    仍然存在、且不能靠算法"修复"的硬限制：若整条引述本身就是单个长句、除了句尾
    只有逗号（EP6 真实案例里 74 字/54 字的孟浩内心独白，通篇只有最后一个句号），
    任何切分方案都必须在最后一个句号之前至少切一刀，那一刀左边的单元就只能停在
    逗号上——这是原文自身的标点结构决定的语法边界，不是切分算法的缺陷，也不能
    靠切分算法编造原文没有的句号来消除（那会篡改台词逐字保真）。这种情况下本函数
    的输出与旧算法逐字相同（同样退到逗号/字级兜底），不作退化，也不假装能改善。

    大纲阶段是否仍会因为这类"必然以逗号收尾"的分句被判定为漏戏，交由结构化
    key_line_ids 台账判定，不再看这段散文本身像不像完整台词
    （见 app.validators.validate_storyboard_outline 的 missing_lines 分支）。
    """
    line = str(value or "").strip()
    if not line or content_char_count(line) <= max_chars:
        return [line] if line else []
    from app.screenplay_ir import _split_spoken_line

    sentences = [unit for unit in _SENTENCE_UNIT_RE.findall(line) if unit.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and content_char_count(current + sentence) > max_chars:
            chunks.append(current)
            current = ""
        if content_char_count(sentence) <= max_chars:
            current += sentence
            continue
        # 单句本身仍超容量：只对这一句退到旧算法的逗号/字级兜底，不影响其余句子。
        if current:
            chunks.append(current)
            current = ""
        chunks.extend(_split_spoken_line(sentence, max_chars=max_chars))
    if current:
        chunks.append(current)
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def project_prep_pack_to_screenplay(payload: dict[str, Any]) -> EpisodeScreenplay:
    """Deterministically project an episode_prep_pack payload into the legacy
    EpisodeScreenplay shape the storyboard stage (narrative_plan is None
    branch) already knows how to consume.

    This is a projection, not a re-generation: every field is either copied
    verbatim from the already-published, already-QA'd prep_pack payload, or
    mechanically derived from verbatim quotes already inside it. No prose is
    authored here and no model is called. Fields with no prep_pack
    equivalent (dramatic_question/protagonist_goal/obstacle/stakes,
    plot_spine, narrative_plan, information_ledger, voice_bible, ...) are
    left at their EpisodeScreenplay default (empty string / empty list /
    None) rather than invented -- the legacy (non-narrative_plan) storyboard
    path already null/empty-checks every one of them.

    The identity triad (visual_entity_id / portrait_id / display_appellation)
    from ``asset_manifest.characters[]`` is carried on
    ``prep_pack_character_assets`` (and the scene equivalent on
    ``prep_pack_scene_assets``) purely as a lossless passthrough; no
    consumption logic reads them yet (next phase, see
    docs/TRANSFORM_FREEZE_PLAN.md P1's remaining prompt-consumption item).
    """
    if not is_prep_pack_payload(payload):
        raise ValueError("payload 不是 episode_prep_pack：缺少 prep_pack_version 标记")

    events = sorted(
        (event for event in (payload.get("event_chain") or []) if isinstance(event, dict)),
        key=lambda ev: (int(ev.get("order") or 0), str(ev.get("event_id") or "")),
    )
    asset_manifest = payload.get("asset_manifest") or {}
    characters = [c for c in (asset_manifest.get("characters") or []) if isinstance(c, dict)]
    scenes = [s for s in (asset_manifest.get("scenes") or []) if isinstance(s, dict)]

    # -- identity triad passthrough: never consumed here, never dropped --
    character_assets = [
        PrepPackCharacterAsset(
            identity_id=str(c.get("identity_id") or ""),
            display_name=str(c.get("display_name") or ""),
            display_appellation=str(
                c.get("display_appellation") or c.get("display_name") or ""
            ),
            visual_entity_id=str(c.get("visual_entity_id") or c.get("identity_id") or ""),
            portrait_id=c.get("portrait_id"),
            event_ids=[str(x) for x in (c.get("event_ids") or [])],
        )
        for c in characters
    ]
    scene_assets = [
        PrepPackSceneAsset(
            scene_id=str(s.get("scene_id") or ""),
            display_name=str(s.get("display_name") or ""),
            scene_reference_id=s.get("scene_reference_id"),
            event_ids=[str(x) for x in (s.get("event_ids") or [])],
        )
        for s in scenes
    ]

    # -- full_script_text: deterministic quote splice, zero authored prose --
    fragments: list[tuple[int, int, str]] = []  # (segment_index, kind_rank, text)
    seen_texts: set[str] = set()

    def _add_fragment(segment_index: Any, text: Any, kind_rank: int) -> None:
        clean = str(text or "").strip()
        if not clean or clean in seen_texts:
            return
        seen_texts.add(clean)
        try:
            idx = int(segment_index)
        except (TypeError, ValueError):
            idx = 0
        fragments.append((idx, kind_rank, clean))

    # event_chain (and with it source_evidence/key_lines quotes) does not
    # exist in prep_pack 2.0.0 (commit 48e01ff) -- ``events`` above is always
    # ``[]`` for a 2.0.0 payload, not a bug to route around. Looping over it
    # here would silently produce an empty full_script_text while looking
    # like real quote-splicing logic; instead branch explicitly so the empty
    # result is a documented consequence of the field genuinely not existing
    # upstream, not an accident of reading a dead key (docs/
    # STORYBOARD_PROMPT_IR_DESIGN.md; this is the storyboard-core fix for the
    # "静默退化" this function used to have). The storyboard stage itself
    # (app.production.storyboard_pack) does not consume this projection at
    # all -- it reads the raw prep_pack payload plus the real chapter text
    # directly -- so this branch only matters for other, non-generation
    # consumers of ``resolve_downstream_screenplay`` (e.g. patch/editing UI).
    is_legacy_event_chain_payload = "event_chain" in payload
    if is_legacy_event_chain_payload:
        for event in events:
            for item in event.get("source_evidence") or []:
                if isinstance(item, dict):
                    _add_fragment(item.get("segment_index"), item.get("quote"), 0)
            for item in event.get("key_lines") or []:
                if isinstance(item, dict):
                    speaker = str(item.get("speaker") or "").strip()
                    line = str(item.get("line") or "").strip()
                    text = f"{speaker}：{line}" if speaker and line else line
                    _add_fragment(item.get("segment_index"), text, 1)

    fragments.sort(key=lambda item: (item[0], item[1]))
    full_script_text = "\n".join(text for _idx, _rank, text in fragments)

    # -- scene_outline: one ScriptScene per asset_manifest scene. --
    event_order_by_id = {
        str(ev.get("event_id") or ""): int(ev.get("order") or 0) for ev in events
    }
    event_summary_by_id = {
        str(ev.get("event_id") or ""): str(ev.get("summary") or "") for ev in events
    }

    scene_outline: list[ScriptScene] = []
    event_to_scene_heading: dict[str, str] = {}
    if is_legacy_event_chain_payload:
        # Ordered by the lowest event order it participates in (deterministic
        # from the event chain's own order; does not trust the payload array
        # order).
        def _scene_sort_key(scene: dict[str, Any]) -> tuple[int, str]:
            ids = [str(x) for x in (scene.get("event_ids") or [])]
            orders = [event_order_by_id.get(eid, 0) for eid in ids]
            return (min(orders) if orders else 0, str(scene.get("scene_id") or ""))

        ordered_scenes = sorted(scenes, key=_scene_sort_key)
        for scene_no, scene in enumerate(ordered_scenes, start=1):
            heading = str(scene.get("display_name") or "")
            scene_event_ids = [str(x) for x in (scene.get("event_ids") or [])]
            for eid in scene_event_ids:
                event_to_scene_heading[eid] = heading
            scene_characters = list(dict.fromkeys(
                asset.display_name
                for asset in character_assets
                if asset.display_name and set(asset.event_ids) & set(scene_event_ids)
            ))
            ordered_event_ids = sorted(
                scene_event_ids, key=lambda eid: event_order_by_id.get(eid, 0),
            )
            summary = "；".join(
                event_summary_by_id[eid] for eid in ordered_event_ids
                if event_summary_by_id.get(eid)
            )
            scene_outline.append(ScriptScene(
                scene_no=scene_no,
                scene_heading=heading,
                story_function="",
                characters=scene_characters,
                summary=summary,
            ))
    else:
        # 2.0.0: no event order to sort by -- keep the manifest's own order
        # (already emitted in source-scan order by the mapping stage) and
        # derive each scene's character roster from the one anchor 2.0.0
        # actually carries: segment_indexes intersection (real evidence of
        # "this character and this scene co-occur in the same source
        # segments"), instead of the now-permanently-empty event_ids
        # intersection this used to (silently) compute to nothing.
        for scene_no, scene in enumerate(scenes, start=1):
            heading = str(scene.get("display_name") or "")
            scene_segment_indexes = {int(x) for x in (scene.get("segment_indexes") or [])}
            scene_characters = list(dict.fromkeys(
                str(c.get("display_name") or "")
                for c in characters
                if str(c.get("display_name") or "")
                and scene_segment_indexes & {int(x) for x in (c.get("segment_indexes") or [])}
            ))
            scene_outline.append(ScriptScene(
                scene_no=scene_no,
                scene_heading=heading,
                story_function="",
                characters=scene_characters,
                summary="",
            ))

    # -- dialogue_chains: one chain per event with key_lines, turns derived
    #    from key_lines[]; key_lines (flat) re-derived through the project's
    #    own canonical algorithm so this projection produces the identical
    #    shape a legacy screenplay would (see app.validators.derive_key_lines
    #    -- "Single source of truth for EpisodeScreenplay.key_lines") --
    #
    # Per-turn spoken-capacity split (real EP6 failure, run_8c369bc4da23,
    # ERR-20260825-07c92e, 2026-08-25): prep_pack's key_lines[].line is a
    # VERBATIM novel excerpt -- its own extraction prompt requires "line 同样
    # 必须逐字取自该编号原文" (app/production/prep_pack.py, ~line 4321) and its
    # payload docstring frames key_lines/speaker_ref as speaker-roster/
    # identity-anchoring evidence (prep_pack 1.5.0b), not a pre-compressed
    # spoken-form line. It is therefore NOT capacity-bounded the way the
    # legacy screenplay generator's key_lines always were: every dialogue
    # unit's adapted text there is passed through
    # `_split_spoken_line(unit.text, max_chars=MAX_SPOKEN_CHARS_PER_SHOT)`
    # before becoming a KeyDialogueTurn (app/screenplay_ir.py, function
    # compile_screenplay_ir, ~line 5266) -- confirmed against the live DB:
    # all 41 historical screenplay_document artifacts' derived key_lines
    # (1372 lines) top out at exactly 36 chars (MAX_SPOKEN_CHARS_PER_SHOT),
    # min=1 p50=16 p90=33, 0 over 36, even though the underlying authored
    # dialogue is frequently longer. EP6's own prep_pack has a 74-char
    # verbatim quote (event ev_007, 孟浩's internal monologue) that cannot
    # fit any shot at all (max shot capacity is 36 chars at 10s) -- the
    # outline's OUTLINE_KEY_LINE_CAPACITY_INVALID gate correctly refused an
    # unsatisfiable contract; that is not a false positive to relax.
    #
    # Internal monologue is not exempt from the spoken-time budget either:
    # it plays back as offscreen_voice (see
    # app.spoken_contract.SPOKEN_DELIVERIES), which consumes the same
    # shot-duration seconds as spoken dialogue, so it still needs a home in
    # some shot's key_line_ids. The fix mirrors the legacy generator exactly:
    # split each raw quote on its own punctuation boundaries into
    # shot-capacity-sized units, byte-for-byte verbatim, deterministically
    # (`_split_spoken_line` is pure function of its input), never rewritten,
    # never dropped -- not exempting long quotes from the budget (that would
    # only move the "can't fit" failure downstream into per-shot repair,
    # which the gate's own message explicitly forbids: "不可满足合同交给
    # 逐镜修复"), and not inventing an undocumented length-based "this one
    # doesn't count as a key line" rule (the frozen prep_pack payload has no
    # field distinguishing monologue from dialogue; a threshold-only carve-
    # out would just be "short lines are key lines, long ones aren't" with no
    # semantic basis). `_split_spoken_line`'s own fallback (character-level
    # chunking once a clause has no more punctuation to split on) guarantees
    # every returned chunk is non-empty and <= max_chars -- there is no
    # "too long to split further" failure mode left unhandled.
    #
    # 2026-08-25 追加（同一次 EP6 回归，见 _split_prep_pack_spoken_line 的完整
    # 说明）：把逐字拆分从 `_split_spoken_line`（逗号/句号同权重、纯贪心堆叠）
    # 换成 `_split_prep_pack_spoken_line`（句子优先，只有单句本身超容量才退到
    # 前者的逗号/字级兜底）。目的是减少"切分产出的单元以逗号收尾、读起来像半句"
    # 的情况；对单句超长、通篇只有末尾一个句号的引述，两者结果逐字相同（这是
    # 原文标点决定的语法边界，见该函数文档）。
    # events is [] for a 2.0.0 payload (see is_legacy_event_chain_payload
    # above) so this naturally yields dialogue_chains=[]/key_plot_points=[]
    # below -- correctly empty because prep_pack 2.0.0 genuinely extracts no
    # dialogue/plot-point data, not a silent failure to route around.
    dialogue_chains: list[KeyDialogueChain] = []
    for event in events:
        turns: list[KeyDialogueTurn] = []
        for item in (event.get("key_lines") or []):
            if not isinstance(item, dict):
                continue
            raw_line = str(item.get("line") or "")
            speaker = str(item.get("speaker") or "")
            for part in _split_prep_pack_spoken_line(
                raw_line, max_chars=config.MAX_SPOKEN_CHARS_PER_SHOT,
            ):
                turns.append(KeyDialogueTurn(
                    speaker=speaker,
                    line=part,
                    # source_text keeps the full original quote (not the
                    # split part) as provenance evidence for every part cut
                    # from it, matching the legacy compiler's own
                    # dialogue_source_evidence convention of citing the whole
                    # authored utterance rather than a sub-fragment.
                    source_text=raw_line,
                ))
        if not turns:
            continue
        event_id = str(event.get("event_id") or "")
        dialogue_chains.append(KeyDialogueChain(
            chain_id=event_id,
            scene_id=event_to_scene_heading.get(event_id, ""),
            topic=str(event.get("summary") or "")[:60],
            turns=turns,
        ))

    from app.validators import derive_key_lines

    key_lines = derive_key_lines(dialogue_chains, full_script_text)
    key_plot_points = [
        str(ev.get("summary") or "").strip() for ev in events
        if str(ev.get("summary") or "").strip()
    ]

    episode_scope = payload.get("episode_scope") or {}
    chapter_indexes = [int(x) for x in (episode_scope.get("chapter_indexes") or [])]
    if len(chapter_indexes) == 1:
        source_text_range = f"第 {chapter_indexes[0]} 章"
    elif chapter_indexes:
        source_text_range = f"第 {chapter_indexes[0]}-{chapter_indexes[-1]} 章"
    else:
        source_text_range = ""

    return EpisodeScreenplay(
        episode_no=int(payload.get("episode_no") or 0),
        source_text_range=source_text_range,
        script_format_note=PREP_PACK_PROJECTION_FORMAT_NOTE,
        key_lines=key_lines,
        dialogue_chains=dialogue_chains,
        key_plot_points=key_plot_points,
        scene_outline=scene_outline,
        full_script_text=full_script_text,
        ending_hook=str(payload.get("cliffhanger") or "").strip(),
        prep_pack_character_assets=character_assets,
        prep_pack_scene_assets=scene_assets,
    )


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
        raw_projection_payload = json.loads(raw_projection)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"当前剧本投影无法解析：{exc}") from exc
    if is_prep_pack_payload(raw_projection_payload):
        # episode_prep_pack (screenplay contract 6.0.0+) is a structurally
        # different payload from the legacy EpisodeScreenplay projection
        # _validated_screenplay_projection parses -- route it through the
        # deterministic projection instead of the legacy parser (which would
        # either raise on the payload's extra keys, now that
        # EpisodeScreenplay is extra="forbid", or -- before that hardening --
        # silently succeed with an almost-empty object).
        try:
            projection = project_prep_pack_to_screenplay(raw_projection_payload)
        except Exception as exc:
            raise ValueError(f"当前分集准备包投影无法验证：{exc}") from exc
    else:
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


def _resolve_current_prep_pack_authority(
    episode_id: str,
    *,
    conn: Any,
    episode: Any,
    artifact: dict[str, Any],
    artifact_id: str,
) -> ResolvedScreenplayAuthority:
    """Resolve one immutable published episode_prep_pack, fail closed on drift.

    Sibling of resolve_current_screenplay_authority (same return type, same
    fail-closed posture), dispatched from it once the published Artifact's
    type is known. Verifies the prep_pack-specific immutable chain:
    app.production.prep_pack._publish_prep_pack issues its completion
    certificate with production_revision_id=None (prep_pack never creates a
    production_revisions row) and its QA evaluation uses
    app.production.prep_pack.QA_PROFILE_VERSION rather than
    SCREENPLAY_QA_PROFILE_VERSION -- both asserted explicitly here rather
    than making the legacy resolver's revision/QA-profile checks conditional.
    """
    db = conn
    if (
        artifact.get("scope_type") != "episode"
        or artifact.get("scope_id") != episode_id
        or artifact.get("status") != "approved"
    ):
        raise ValueError("已发布分集准备包 Artifact 的作用域或状态无效")
    artifact_hash = _verified_artifact_hash(artifact, label="已发布分集准备包 Artifact")
    payload = artifact.get("content")
    if not is_prep_pack_payload(payload):
        raise ValueError("已发布分集准备包 Artifact 内容不是有效的 episode_prep_pack")
    screenplay = project_prep_pack_to_screenplay(payload)

    raw_projection = _episode_value(episode, "screenplay_json", "")
    if not raw_projection:
        raise ValueError("已发布分集准备包缺少页面投影")
    try:
        projection_payload = json.loads(raw_projection)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"页面 screenplay_json 无法解析：{exc}") from exc
    if projection_payload != payload:
        raise ValueError("页面 screenplay_json 与已发布 Artifact 内容漂移")

    certificate_id = str(
        _episode_value(episode, "screenplay_completion_certificate_id", "") or ""
    )
    if not certificate_id:
        raise ValueError("已发布分集准备包缺少当前完成凭证")
    contract_version = str(artifact.get("contract_version") or "")
    input_fingerprint = evidence_repository.content_hash({
        "episode_id": episode_id,
        "episode_scope": payload["episode_scope"],
    })
    from app.production.certificate import verify_completion_certificate
    from app.production.prep_pack import (
        QA_PROFILE_VERSION as PREP_PACK_QA_PROFILE_VERSION,
    )

    cert = verify_completion_certificate(
        certificate_id,
        expected_kind="screenplay",
        expected_scope_id=episode_id,
        expected_artifact_id=artifact_id,
        expected_artifact_hash=artifact_hash,
        expected_input_fingerprint=input_fingerprint,
        expected_contract_version=contract_version,
        expected_qa_profile_version=PREP_PACK_QA_PROFILE_VERSION,
        allow_consumed=True,
    )
    if cert.consumed_at is None:
        raise ValueError("分集准备包完成凭证尚未被原子发布消费")

    evaluation_ids = list(cert.evaluation_ids)
    if not evaluation_ids:
        raise ValueError("分集准备包完成凭证缺少 QA 证据")
    marks = ",".join("?" for _ in evaluation_ids)
    evaluations = db.execute(
        f"SELECT * FROM evaluations WHERE id IN ({marks})", evaluation_ids,
    ).fetchall()
    qa_rows = [
        row for row in evaluations
        if row["evaluator_name"] == "screenplay_production_qa"
    ]
    if len(qa_rows) != 1:
        raise ValueError("分集准备包权威链必须精确绑定一个生产 QA")
    qa_row = qa_rows[0]
    if (
        qa_row["artifact_id"] != artifact_id
        or qa_row["evaluator_version"] != PREP_PACK_QA_PROFILE_VERSION
        or qa_row["evaluation_role"] != "score_only"
        or bool(qa_row["runtime_blocking"])
        or qa_row["status"] != "passed"
    ):
        raise ValueError("分集准备包质量评分 Evaluation 已漂移或版本不匹配")
    try:
        evidence = json.loads(qa_row["evidence_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("分集准备包 QA 证据无法解析") from exc
    if evidence.get("prep_pack_version") != payload.get("prep_pack_version"):
        raise ValueError("分集准备包 QA 证据与当前发布版本不一致")

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
    if artifact is not None and artifact.get("type") == "episode_prep_pack":
        # Dispatch here (rather than duplicating this branch in every one of
        # resolve_current_screenplay_authority's ~7 direct callers) so every
        # caller -- resolve_downstream_screenplay and the handful of modules
        # that call this function directly (storyboard_supervisor,
        # narrative_review, narrative_calibration_ops, completion_grant,
        # domain.common) -- gets correct episode_prep_pack handling for free.
        # A separate resolver rather than branching every check below: the
        # two immutable chains are structurally different (prep_pack has no
        # production_revision, no v7 scene-shard source projection, and a
        # different QA profile/evaluator payload shape -- see
        # app.production.prep_pack._publish_prep_pack), so interleaving both
        # into one function would make neither chain independently auditable.
        if require_narrative:
            raise ValueError(
                "已发布产物是分集准备包（episode_prep_pack），不具备叙事权威图"
                "（narrative_plan）概念，无法满足 require_narrative=True 的调用"
            )
        return _resolve_current_prep_pack_authority(
            episode_id, conn=db, episode=episode, artifact=artifact, artifact_id=artifact_id,
        )
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
    assert_screenplay_matches_validated_v7_source(
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
