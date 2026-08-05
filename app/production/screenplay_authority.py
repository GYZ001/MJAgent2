"""Immutable screenplay/source authority resolution for downstream narrative work."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from app import config
from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.ingest import chapter_is_stub, chapter_titles_match
from app.schemas import Bible, EpisodeScreenplay


SCREENPLAY_QA_PROFILE_VERSION = "screenplay-qa-gate-2"


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
        bible_hash = evidence_repository.content_hash(bible.model_dump(mode="json"))
    else:
        raw_bible = _episode_value(project, "bible_json", "") if project else ""
        try:
            bible_content = json.loads(raw_bible) if raw_bible else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            bible_content = {"invalid_raw": str(raw_bible or "")}
        bible_hash = evidence_repository.content_hash(bible_content)

    constraints = {
        "title": _episode_value(episode, "title", "") or "",
        "hook": _episode_value(episode, "hook", "") or "",
        "cliffhanger": _episode_value(episode, "cliffhanger", "") or "",
        "synopsis": _episode_value(episode, "synopsis", "") or "",
        "target_duration_s": int(_episode_value(episode, "target_duration_s", 0) or 0),
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
    return {
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


def _published_authority_input_fingerprint(
    episode_id: str,
    *,
    conn: Any,
    certificate_id: str,
    contract_version: str,
) -> str:
    """Recover only the known legacy storyboard duration contamination.

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

    constraints = material.get("adaptation_constraints")
    if not isinstance(constraints, dict):
        return current_fingerprint
    current_target = constraints.get("target_duration_s")
    legal_targets = list(range(
        config.EPISODE_TARGET_MIN_S,
        config.EPISODE_TARGET_MAX_S + 1,
        config.EPISODE_TARGET_STEP_S,
    ))
    if current_target not in legal_targets:
        return current_fingerprint
    matches: list[str] = []
    for target in legal_targets:
        if target == current_target:
            continue
        candidate = {
            **material,
            "adaptation_constraints": {
                **constraints,
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
                EpisodeScreenplay.model_validate_json(raw_projection).narrative_plan
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
        projection = EpisodeScreenplay.model_validate_json(raw_projection)
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
    raw_projection = _episode_value(episode, "screenplay_json", "")
    if not raw_projection:
        raise ValueError("已发布剧本缺少页面投影")
    projection = EpisodeScreenplay.model_validate_json(raw_projection)
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
    if (
        qa_row["artifact_id"] != artifact_id
        or qa_row["evaluator_version"] != SCREENPLAY_QA_PROFILE_VERSION
        or qa_row["evaluation_role"] != "runtime_gate"
        or not bool(qa_row["runtime_blocking"])
        or qa_row["status"] != "passed"
        or not bool(qa_row["hard_gate_passed"])
        or not isinstance(issues, list)
        or any(
            isinstance(issue, dict)
            and (
                str(issue.get("severity") or "").lower() == "blocker"
                or bool(issue.get("must_fix"))
            )
            for issue in (issues or [])
        )
    ):
        raise ValueError("剧本生产 QA Evaluation 已漂移或不再通过")
    try:
        evidence = json.loads(qa_row["evidence_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("剧本 QA 证据无法解析") from exc
    if evidence.get("authority_input_fingerprint") != input_fingerprint:
        raise ValueError("剧本 QA 与当前原文/Bible/改编约束指纹不一致")

    records, source_text = _source_records(db, episode)
    from app.narrative import validate_screenplay_narrative

    errors = validate_screenplay_narrative(
        screenplay,
        require=require_narrative,
        source_text=source_text,
        expected_scope_id=episode_id,
        authorized_source_chapters=screenplay_authorized_source_chapters(
            episode_id,
            conn=db,
        ),
    )
    if errors:
        raise ValueError("已发布剧本无法在当前原文上重验：" + "；".join(errors[:6]))
    return ResolvedScreenplayAuthority(
        episode_id=episode_id,
        screenplay=screenplay,
        source_text=source_text,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        certificate_id=certificate_id,
        input_fingerprint=input_fingerprint,
    )
