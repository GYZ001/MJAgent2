"""视频补齐授权（VideoCompletionGrant）。

用户选择「补齐到全片可用」时签发；分镜始终由人工确认，不存在自动确认授权。
token 只存哈希，不存明文。
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db import get_conn, new_id, now

GRANT_TTL_S = 6 * 3600  # 6 小时
VIDEO_PERMISSION = "video.complete_episode"
DEFAULT_VIDEO_BUDGET_CAP_CNY = 150.0
DEFAULT_VIDEO_WALL_CLOCK_CAP_S = 4 * 3600
DEFAULT_FALLBACK_QUOTA_FRACTION = 0.2


class VideoCompletionGrant(BaseModel):
    grant_id: str
    episode_id: str
    project_id: str
    storyboard_artifact_id: str
    release_qualification_hash: str = ""
    release_qualification: dict[str, Any] = Field(default_factory=dict)
    episode_video_plan_id: str | None = None
    episode_video_plan_revision: int | None = None
    video_plan_release_hash: str | None = None
    capability_snapshot_id: str | None = None
    permission: Literal["video.complete_episode"] = VIDEO_PERMISSION
    kind: Literal["video"] = "video"
    budget_cap_cny: float = DEFAULT_VIDEO_BUDGET_CAP_CNY
    wall_clock_cap_s: float = DEFAULT_VIDEO_WALL_CLOCK_CAP_S
    deadline_at: float
    allow_fallback_adopt: bool = True
    max_fallback_shots: int = 0
    allow_storyboard_edit: bool = False
    issued_by: str = "user"
    issued_at: float
    expires_at: float
    consumed_at: float | None = None
    revoked_at: float | None = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_completion_grants_table(conn=None) -> None:
    db = conn or get_conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS completion_grants (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            screenplay_artifact_id TEXT NOT NULL,
            bible_artifact_id TEXT,
            permission TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            issued_by TEXT NOT NULL,
            issued_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at REAL,
            revoked_at REAL,
            impact_snapshot_json TEXT
        )"""
    )
    for stmt in (
        "ALTER TABLE completion_grants ADD COLUMN kind TEXT NOT NULL DEFAULT 'video'",
        "ALTER TABLE completion_grants ADD COLUMN storyboard_artifact_id TEXT",
        "ALTER TABLE completion_grants ADD COLUMN budget_cap_cny REAL",
        "ALTER TABLE completion_grants ADD COLUMN wall_clock_cap_s REAL",
        "ALTER TABLE completion_grants ADD COLUMN deadline_at REAL",
        "ALTER TABLE completion_grants ADD COLUMN allow_fallback_adopt INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE completion_grants ADD COLUMN max_fallback_shots INTEGER",
        "ALTER TABLE completion_grants ADD COLUMN allow_storyboard_edit INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE completion_grants ADD COLUMN release_qualification_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE completion_grants ADD COLUMN release_qualification_hash TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE completion_grants ADD COLUMN episode_video_plan_id TEXT",
        "ALTER TABLE completion_grants ADD COLUMN episode_video_plan_revision INTEGER",
        "ALTER TABLE completion_grants ADD COLUMN video_plan_release_hash TEXT",
        "ALTER TABLE completion_grants ADD COLUMN capability_snapshot_id TEXT",
    ):
        try:
            db.execute(stmt)
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    db.execute(
        "DELETE FROM completion_grants WHERE kind='storyboard' OR permission='storyboard.generate_and_confirm'"
    )
    db.commit()


def default_max_fallback_shots(shots_total: int) -> int:
    return max(1, int(math.ceil(max(0, shots_total) * DEFAULT_FALLBACK_QUOTA_FRACTION)))


RELEASE_QUALIFICATION_VERSION = "video-completion-release-qualification.v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _content_fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _legacy_screenplay_projection_material(
    episode: Any,
    *,
    mode: str,
    projection_hash: str,
) -> dict[str, Any]:
    """Content-address every legacy fact without pretending it is certified."""
    from app.evidence import repository as evidence_repository

    keys = set(episode.keys())
    artifact_id = str(
        (episode["published_screenplay_artifact_id"] if "published_screenplay_artifact_id" in keys else "")
        or (episode["screenplay_artifact_id"] if "screenplay_artifact_id" in keys else "")
        or ""
    )
    binding: dict[str, Any] = {
        "artifact_id": artifact_id,
        "compatibility": "legacy_projection_only",
    }
    artifact = evidence_repository.get_artifact(artifact_id) if artifact_id else None
    if artifact is not None:
        try:
            current_hash = evidence_repository.content_hash(
                artifact.get("content"), artifact.get("file_path")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("legacy screenplay artifact cannot be content-addressed") from exc
        if not artifact.get("content_hash") or artifact.get("content_hash") != current_hash:
            raise ValueError("legacy screenplay artifact content hash drifted")
        binding.update({
            "content_hash": current_hash,
            "type": artifact.get("type"),
            "scope_type": artifact.get("scope_type"),
            "scope_id": artifact.get("scope_id"),
            "status": artifact.get("status"),
            "contract_version": artifact.get("contract_version"),
        })
    return {
        "mode": mode,
        "immutable_authority_required": False,
        "narrative_authority_required": False,
        "projection_hash": projection_hash,
        "artifact": binding,
    }


def _screenplay_release_material(episode_id: str, *, conn) -> dict[str, Any]:
    """Resolve screenplay authority without allowing a mutable downgrade.

    Historical projection-only episodes remain readable through an explicit
    compatibility contract.  The first durable production revision,
    certificate, or narrative review makes the immutable resolver mandatory.
    """
    from app.production.screenplay_authority import (
        episode_requires_immutable_screenplay_authority,
        resolve_current_screenplay_authority,
        resolve_downstream_screenplay,
    )

    episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    immutable_required = episode_requires_immutable_screenplay_authority(
        episode,
        conn=conn,
    )
    try:
        context = resolve_downstream_screenplay(episode_id, conn=conn)
    except ValueError:
        if immutable_required:
            raise
        raw = ""
        try:
            raw = str(episode["screenplay_json"] or "")
        except (KeyError, IndexError):
            pass
        return _legacy_screenplay_projection_material(
            episode,
            mode="legacy_plan_null_projection_absent" if not raw else "legacy_plan_null",
            projection_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )
    if not context.immutable_authority_required:
        return _legacy_screenplay_projection_material(
            episode,
            mode="legacy_plan_null",
            projection_hash=_content_fingerprint(
                context.screenplay.model_dump(mode="json")
            ),
        )
    resolved = resolve_current_screenplay_authority(
        episode_id,
        conn=conn,
        require_narrative=context.narrative_authority_required,
    )
    revision_id = ""
    try:
        revision_id = str(episode["screenplay_production_revision_id"] or "")
    except (KeyError, IndexError):
        pass
    return {
        "mode": "immutable_narrative" if context.narrative_authority_required else "immutable",
        "immutable_authority_required": True,
        "narrative_authority_required": context.narrative_authority_required,
        "published_screenplay_artifact_id": resolved.artifact_id,
        "published_screenplay_artifact_hash": resolved.artifact_hash,
        "screenplay_completion_certificate_id": resolved.certificate_id,
        "screenplay_production_revision_id": revision_id,
        "screenplay_input_fingerprint": resolved.input_fingerprint,
    }


def _storyboard_release_material(
    episode_id: str,
    *,
    conn,
    legacy_plan_null: bool,
) -> dict[str, Any]:
    from app.evidence import repository as evidence_repository
    from app.video_plan import (
        canonical_shot_contract_fingerprint,
        current_storyboard_release_manifest,
    )

    try:
        manifest = current_storyboard_release_manifest(episode_id, conn=conn)
    except (TypeError, ValueError):
        if not legacy_plan_null:
            raise
        # Explicit historical compatibility: some pre-contract rows contain
        # nullable camera/scene fields and cannot instantiate today's Shot
        # model. Bind every persisted column instead of weakening validation
        # for modern releases.
        episode = conn.execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        legacy_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no,id",
            (episode_id,),
        ).fetchall()
        artifact_id = str(
            (
                episode["published_storyboard_artifact_id"]
                if "published_storyboard_artifact_id" in episode.keys()
                else None
            )
            or episode["storyboard_artifact_id"]
            or ""
        )
        if not artifact_id:
            raise ValueError("legacy storyboard projection has no release pointer")
        raw_projection_hash = _content_fingerprint(
            [dict(row) for row in legacy_rows]
        )
        manifest = {
            "published_storyboard_artifact_id": artifact_id,
            "published_storyboard_artifact_hash": raw_projection_hash,
            "completion_certificate_id": str(
                episode["storyboard_completion_certificate_id"] or ""
            ) if "storyboard_completion_certificate_id" in episode.keys() else "",
            "narrative_review_artifact_id": str(
                episode["narrative_review_artifact_id"] or ""
            ) if "narrative_review_artifact_id" in episode.keys() else "",
        }
        manifest["release_qualification_hash"] = _content_fingerprint({
            "manifest_version": "storyboard-release-manifest.legacy-plan-null.v1",
            "episode_id": episode_id,
            **manifest,
        })
    artifact_id = manifest["published_storyboard_artifact_id"]
    artifact = evidence_repository.get_artifact(artifact_id)
    if artifact is None:
        artifact_binding: dict[str, Any] = {
            "compatibility": "legacy_projection_pointer",
            "artifact_id": artifact_id,
            "content_hash": manifest["published_storyboard_artifact_hash"],
        }
    else:
        try:
            current_hash = evidence_repository.content_hash(
                artifact.get("content"), artifact.get("file_path")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("current storyboard artifact cannot be content-addressed") from exc
        if (
            not artifact.get("content_hash")
            or artifact.get("content_hash") != current_hash
            or current_hash != manifest["published_storyboard_artifact_hash"]
        ):
            raise ValueError("current storyboard artifact content hash drifted")
        artifact_binding = {
            "artifact_id": artifact_id,
            "content_hash": current_hash,
            "type": artifact.get("type"),
            "scope_type": artifact.get("scope_type"),
            "scope_id": artifact.get("scope_id"),
            "status": artifact.get("status"),
            "contract_version": artifact.get("contract_version"),
        }
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no, id",
        (episode_id,),
    ).fetchall()
    shot_projection: list[dict[str, Any]] = []
    for row in rows:
        try:
            contract_hash = canonical_shot_contract_fingerprint(row)
            projection_mode = "canonical_shot_contract"
        except (TypeError, ValueError):
            if not legacy_plan_null:
                raise
            contract_hash = _content_fingerprint(dict(row))
            projection_mode = "legacy_complete_database_row"
        shot_projection.append({
            "database_shot_id": str(row["id"]),
            "shot_uid": str(row["shot_uid"] or "") if "shot_uid" in row.keys() else "",
            "shot_no": int(row["shot_no"]),
            "projection_mode": projection_mode,
            "canonical_contract_hash": contract_hash,
        })
    return {
        **manifest,
        "artifact": artifact_binding,
        "shots_authority_projection_hash": _content_fingerprint(shot_projection),
        "shots_authority_projection": shot_projection,
    }


def _narrative_review_material(
    episode_id: str,
    *,
    screenplay_material: dict[str, Any],
) -> dict[str, Any]:
    """Keep the legacy qualification slot explicitly score-only.

    Storyboard release authority is already verified by
    ``_storyboard_release_material``. Optional audience scoring is not an
    authored input and must not invalidate a paid-work grant when it changes.
    """
    return {
        "required": False,
        "verified": True,
        "evaluation_role": "score_only",
        "episode_id": episode_id,
        "narrative_project": bool(
            screenplay_material.get("narrative_authority_required")
        ),
    }


def _generation_plan_material(
    episode_id: str,
    *,
    conn,
    applicable: bool | None,
) -> dict[str, Any]:
    from app.video_plan import (
        capability_snapshot_by_id,
        load_latest_plan,
        shot_video_execution_contract_fingerprint,
        verify_episode_plan_is_current,
    )

    if applicable is False:
        return {"applicable": False, "compatibility": "plan_pending_at_grant_issue"}
    plan = load_latest_plan(episode_id, conn=conn)
    if plan is None:
        if applicable:
            raise ValueError("current episode video generation plan is missing")
        return {"applicable": False, "compatibility": "plan_pending_at_grant_issue"}
    if plan.status != "valid" or not verify_episode_plan_is_current(
        plan,
        conn=conn,
        mark_stale=False,
    ):
        if applicable is None:
            return {
                "applicable": False,
                "compatibility": "plan_pending_at_grant_issue",
            }
        raise ValueError("current episode video generation plan is not valid")
    snapshot = capability_snapshot_by_id(plan.capability_snapshot_id, conn=conn)
    if snapshot is None:
        raise ValueError("video generation plan capability snapshot is missing")
    return {
        "applicable": True,
        "episode_video_plan_id": plan.episode_video_plan_id,
        "plan_revision": int(plan.plan_revision),
        "source_storyboard_revision_id": plan.source_storyboard_revision_id,
        "release_qualification_hash": plan.release_qualification_hash,
        "capability_snapshot_id": plan.capability_snapshot_id,
        "capability_snapshot_hash": _content_fingerprint(
            snapshot.model_dump(mode="json")
        ),
        "planner_provider": plan.planner_provider,
        "planner_model": plan.planner_model,
        "planner_prompt_fingerprint": plan.planner_prompt_fingerprint,
        "shot_execution_contracts": [
            {
                "shot_id": shot.shot_id,
                "shot_plan_id": shot.shot_plan_id,
                "contract_hash": shot_video_execution_contract_fingerprint(shot),
            }
            for shot in plan.shots
        ],
    }


def current_video_completion_qualification(
    episode_id: str,
    *,
    generation_plan_applicable: bool | None = None,
    conn=None,
) -> tuple[dict[str, Any], str]:
    """Build the complete release contract rechecked before every paid stage."""
    db = conn or get_conn()
    episode = db.execute(
        "SELECT id,project_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    screenplay = _screenplay_release_material(episode_id, conn=db)
    storyboard = _storyboard_release_material(
        episode_id,
        conn=db,
        legacy_plan_null=str(screenplay.get("mode") or "").startswith("legacy_plan_null"),
    )
    material = {
        "qualification_version": RELEASE_QUALIFICATION_VERSION,
        "episode_id": episode_id,
        "project_id": str(episode["project_id"]),
        "screenplay_authority": screenplay,
        "storyboard_authority": storyboard,
        "narrative_review_authority": _narrative_review_material(
            episode_id,
            screenplay_material=screenplay,
        ),
        "generation_plan": _generation_plan_material(
            episode_id,
            conn=db,
            applicable=generation_plan_applicable,
        ),
    }
    return material, _content_fingerprint(material)


def issue_video_completion_grant(
    *,
    episode_id: str,
    project_id: str,
    storyboard_artifact_id: str,
    budget_cap_cny: float | None = None,
    wall_clock_cap_s: float | None = None,
    allow_fallback_adopt: bool = True,
    max_fallback_shots: int | None = None,
    allow_storyboard_edit: bool = False,
    shots_total: int = 0,
    issued_by: str = "user",
    ttl_s: int = GRANT_TTL_S,
    impact_snapshot: dict[str, Any] | None = None,
) -> tuple[VideoCompletionGrant, str]:
    """签发视频补齐授权。"""
    ensure_completion_grants_table()
    conn = get_conn()
    episode = conn.execute(
        "SELECT project_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if episode is None:
        raise GrantValidationError("GRANT_SCOPE_MISSING", "视频补齐授权的分集不存在")
    if str(episode["project_id"]) != str(project_id):
        raise GrantValidationError("GRANT_PROJECT_MISMATCH", "视频补齐授权的项目与分集不匹配")
    try:
        qualification, qualification_hash = current_video_completion_qualification(
            episode_id,
            conn=conn,
        )
    except ValueError as exc:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_INVALID", str(exc)
        ) from exc
    bound_storyboard_id = str(
        qualification["storyboard_authority"]["published_storyboard_artifact_id"]
    )
    if (storyboard_artifact_id or "") != bound_storyboard_id:
        raise GrantValidationError(
            "UPSTREAM_VERSION_CHANGED",
            "请求授权的分镜 Artifact 不是当前发布版",
        )
    generation_plan = qualification["generation_plan"]
    grant_id = new_id("grant")
    token = secrets.token_urlsafe(24)
    issued_at = now()
    cap = float(budget_cap_cny if budget_cap_cny is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY)
    wall = float(wall_clock_cap_s if wall_clock_cap_s is not None else DEFAULT_VIDEO_WALL_CLOCK_CAP_S)
    if not math.isfinite(cap) or not 1 <= cap <= 100000:
        raise GrantValidationError("INVALID_BUDGET", "视频补齐预算必须是 1–100000 的有限数")
    if not math.isfinite(wall) or not 60 <= wall <= 604800:
        raise GrantValidationError("INVALID_WALL_CLOCK", "视频补齐时长墙必须是 60–604800 秒的有限数")
    deadline_at = issued_at + wall
    expires_at = issued_at + max(60, int(ttl_s), int(wall) + 3600)
    fallback_quota = (
        int(max_fallback_shots)
        if max_fallback_shots is not None
        else default_max_fallback_shots(shots_total)
    )
    conn.execute(
        """INSERT INTO completion_grants(
            id, episode_id, project_id, screenplay_artifact_id, bible_artifact_id,
            permission, token_hash, issued_by, issued_at, expires_at, consumed_at, revoked_at,
            impact_snapshot_json, kind, storyboard_artifact_id, budget_cap_cny, wall_clock_cap_s, deadline_at,
            allow_fallback_adopt, max_fallback_shots, allow_storyboard_edit,
            release_qualification_json, release_qualification_hash,
            episode_video_plan_id, episode_video_plan_revision,
            video_plan_release_hash, capability_snapshot_id
        ) VALUES(?,?,?,?,NULL,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            grant_id, episode_id, project_id, "",
            VIDEO_PERMISSION, _hash_token(token), issued_by, issued_at, expires_at,
            json.dumps(impact_snapshot or {}, ensure_ascii=False),
            "video", storyboard_artifact_id or "", cap, wall, deadline_at,
            1 if allow_fallback_adopt else 0, fallback_quota,
            1 if allow_storyboard_edit else 0,
            _canonical_json(qualification), qualification_hash,
            generation_plan.get("episode_video_plan_id"),
            generation_plan.get("plan_revision"),
            generation_plan.get("release_qualification_hash"),
            generation_plan.get("capability_snapshot_id"),
        ),
    )
    conn.commit()
    grant = VideoCompletionGrant(
        grant_id=grant_id,
        episode_id=episode_id,
        project_id=project_id,
        storyboard_artifact_id=storyboard_artifact_id or "",
        release_qualification_hash=qualification_hash,
        release_qualification=qualification,
        episode_video_plan_id=generation_plan.get("episode_video_plan_id"),
        episode_video_plan_revision=generation_plan.get("plan_revision"),
        video_plan_release_hash=generation_plan.get("release_qualification_hash"),
        capability_snapshot_id=generation_plan.get("capability_snapshot_id"),
        budget_cap_cny=cap,
        wall_clock_cap_s=wall,
        deadline_at=deadline_at,
        allow_fallback_adopt=allow_fallback_adopt,
        max_fallback_shots=fallback_quota,
        allow_storyboard_edit=allow_storyboard_edit,
        issued_by=issued_by,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return grant, token


def _row_to_video_grant(row) -> VideoCompletionGrant:
    def _col(name, default=None):
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return default

    try:
        release_qualification = json.loads(
            _col("release_qualification_json", "{}") or "{}"
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        release_qualification = {}
    return VideoCompletionGrant(
        grant_id=row["id"],
        episode_id=row["episode_id"],
        project_id=row["project_id"],
        storyboard_artifact_id=_col("storyboard_artifact_id") or "",
        release_qualification_hash=_col("release_qualification_hash") or "",
        release_qualification=release_qualification,
        episode_video_plan_id=_col("episode_video_plan_id") or None,
        episode_video_plan_revision=(
            int(_col("episode_video_plan_revision"))
            if _col("episode_video_plan_revision") is not None
            else None
        ),
        video_plan_release_hash=_col("video_plan_release_hash") or None,
        capability_snapshot_id=_col("capability_snapshot_id") or None,
        budget_cap_cny=float(_col("budget_cap_cny") or DEFAULT_VIDEO_BUDGET_CAP_CNY),
        wall_clock_cap_s=float(_col("wall_clock_cap_s") or DEFAULT_VIDEO_WALL_CLOCK_CAP_S),
        deadline_at=float(
            _col("deadline_at")
            or (float(row["issued_at"]) + float(_col("wall_clock_cap_s") or DEFAULT_VIDEO_WALL_CLOCK_CAP_S))
        ),
        allow_fallback_adopt=bool(int(_col("allow_fallback_adopt", 1) or 0)),
        max_fallback_shots=int(_col("max_fallback_shots") or 0),
        allow_storyboard_edit=bool(int(_col("allow_storyboard_edit", 0) or 0)),
        issued_by=row["issued_by"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        revoked_at=row["revoked_at"],
    )


def get_video_grant(grant_id: str) -> VideoCompletionGrant | None:
    ensure_completion_grants_table()
    row = get_conn().execute(
        "SELECT * FROM completion_grants WHERE id=?", (grant_id,)
    ).fetchone()
    if not row:
        return None
    try:
        kind = row["kind"]
    except (KeyError, IndexError, TypeError):
        kind = None
    if kind != "video" and row["permission"] != VIDEO_PERMISSION:
        return None
    return _row_to_video_grant(row)


class GrantValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_video_grant(
    grant_id: str,
    *,
    episode_id: str,
    storyboard_artifact_id: str | None,
) -> VideoCompletionGrant:
    """视频补齐前校验授权。"""
    grant = get_video_grant(grant_id)
    if grant is None:
        raise GrantValidationError("GRANT_NOT_FOUND", "视频补齐授权不存在")
    if grant.episode_id != episode_id:
        raise GrantValidationError("GRANT_EPISODE_MISMATCH", "授权不属于本集")
    if grant.revoked_at is not None:
        raise GrantValidationError("GRANT_REVOKED", "视频补齐授权已撤销")
    if grant.consumed_at is not None:
        raise GrantValidationError("GRANT_CONSUMED", "视频补齐授权已使用")
    if now() > grant.expires_at:
        raise GrantValidationError("GRANT_EXPIRED", "视频补齐授权已过期")
    if (storyboard_artifact_id or "") != (grant.storyboard_artifact_id or ""):
        raise GrantValidationError(
            "UPSTREAM_VERSION_CHANGED",
            "分镜 Artifact 已变更，视频补齐授权失效",
        )
    stored = grant.release_qualification
    stored_hash = grant.release_qualification_hash
    if not stored or not stored_hash or _content_fingerprint(stored) != stored_hash:
        raise GrantValidationError(
            "GRANT_RELEASE_QUALIFICATION_MISSING",
            "视频补齐授权缺少可重算的发布资格绑定",
        )
    plan_binding = dict(stored.get("generation_plan") or {})
    plan_applicable = bool(plan_binding.get("applicable"))
    try:
        current, current_hash = current_video_completion_qualification(
            episode_id,
            generation_plan_applicable=plan_applicable,
        )
    except ValueError as exc:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_INVALID",
            f"当前发布资格无法验证：{exc}",
        ) from exc
    if current_hash != stored_hash or current != stored:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_CHANGED",
            "剧本、分镜、审读、凭证、Shot 投影或视频计划已变更，请重新授权",
        )
    if plan_applicable and (
        grant.episode_video_plan_id != plan_binding.get("episode_video_plan_id")
        or grant.episode_video_plan_revision != plan_binding.get("plan_revision")
        or grant.video_plan_release_hash != plan_binding.get("release_qualification_hash")
        or grant.capability_snapshot_id != plan_binding.get("capability_snapshot_id")
    ):
        raise GrantValidationError(
            "GRANT_PLAN_BINDING_CORRUPT",
            "授权的视频计划绑定与内容指纹不一致",
        )
    return grant


def bind_video_grant_generation_plan(
    grant_id: str,
    *,
    episode_id: str,
    storyboard_artifact_id: str | None,
) -> VideoCompletionGrant:
    """Atomically bind the first valid plan before any paid media preparation.

    A grant may be issued before the asynchronous planner has run.  Its release
    facts are already immutable at that point; this operation may only replace
    the explicit ``plan_pending_at_grant_issue`` slot while every other release
    fact is byte-for-byte unchanged.
    """
    grant = validate_video_grant(
        grant_id,
        episode_id=episode_id,
        storyboard_artifact_id=storyboard_artifact_id,
    )
    old = grant.release_qualification
    old_plan = dict(old.get("generation_plan") or {})
    if old_plan.get("applicable"):
        return grant
    try:
        current, current_hash = current_video_completion_qualification(
            episode_id,
            generation_plan_applicable=True,
        )
    except ValueError as exc:
        raise GrantValidationError("VIDEO_PLAN_INVALID", str(exc)) from exc
    old_release = {key: value for key, value in old.items() if key != "generation_plan"}
    current_release = {
        key: value for key, value in current.items() if key != "generation_plan"
    }
    if old_release != current_release:
        raise GrantValidationError(
            "RELEASE_QUALIFICATION_CHANGED",
            "视频计划产生前发布资格已变更，不得继续绑定",
        )
    plan = current["generation_plan"]
    conn = get_conn()
    updated = conn.execute(
        """UPDATE completion_grants
              SET release_qualification_json=?,release_qualification_hash=?,
                  episode_video_plan_id=?,episode_video_plan_revision=?,
                  video_plan_release_hash=?,capability_snapshot_id=?
            WHERE id=? AND release_qualification_hash=?""",
        (
            _canonical_json(current),
            current_hash,
            plan["episode_video_plan_id"],
            plan["plan_revision"],
            plan["release_qualification_hash"],
            plan["capability_snapshot_id"],
            grant_id,
            grant.release_qualification_hash,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise GrantValidationError(
            "GRANT_CONCURRENTLY_CHANGED",
            "视频补齐授权在绑定计划时已被并发修改",
        )
    conn.commit()
    return validate_video_grant(
        grant_id,
        episode_id=episode_id,
        storyboard_artifact_id=storyboard_artifact_id,
    )


def refresh_video_grant_storyboard_artifact(grant_id: str, storyboard_artifact_id: str) -> None:
    """Published story changes always require a newly content-addressed grant."""
    del grant_id, storyboard_artifact_id
    raise GrantValidationError(
        "GRANT_RENEWAL_REQUIRED",
        "分镜发布版变更后必须重新授权，不得就地刷新旧授权",
    )


def bump_video_grant_budget(
    grant_id: str, *, add_cny: float, add_wall_s: float = 0
) -> VideoCompletionGrant:
    """追加预算/时长并返回更新后的 grant。"""
    ensure_completion_grants_table()
    grant = get_video_grant(grant_id)
    if grant is None:
        raise GrantValidationError("GRANT_NOT_FOUND", "视频补齐授权不存在")
    if grant.revoked_at is not None:
        raise GrantValidationError("GRANT_REVOKED", "视频补齐授权已撤销")
    add_cny = float(add_cny)
    add_wall_s = float(add_wall_s)
    if not math.isfinite(add_cny) or add_cny < 0 or add_cny > 100000:
        raise GrantValidationError("INVALID_BUDGET", "追加预算必须是 0–100000 的有限数")
    if not math.isfinite(add_wall_s) or add_wall_s < 0 or add_wall_s > 604800:
        raise GrantValidationError("INVALID_WALL_CLOCK", "追加时长必须是 0–604800 秒的有限数")
    if add_cny == 0 and add_wall_s == 0:
        raise GrantValidationError("EMPTY_TOPUP", "追加预算和时长不能同时为 0")
    new_cap = float(grant.budget_cap_cny) + add_cny
    new_wall = float(grant.wall_clock_cap_s) + add_wall_s
    if new_cap > 100000 or new_wall > 604800:
        raise GrantValidationError("GRANT_LIMIT_EXCEEDED", "追加后授权超过最大上限")
    new_deadline = float(grant.issued_at) + new_wall
    new_expires = max(float(grant.expires_at), now() + GRANT_TTL_S)
    conn = get_conn()
    conn.execute(
        """UPDATE completion_grants
           SET budget_cap_cny=?, wall_clock_cap_s=?, deadline_at=?, expires_at=?, consumed_at=NULL
           WHERE id=?""",
        (new_cap, new_wall, new_deadline, new_expires, grant_id),
    )
    conn.commit()
    updated = get_video_grant(grant_id)
    assert updated is not None
    return updated


def consume_grant(grant_id: str) -> None:
    ensure_completion_grants_table()
    conn = get_conn()
    conn.execute(
        "UPDATE completion_grants SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
        (now(), grant_id),
    )
    conn.commit()


def revoke_grant(grant_id: str) -> None:
    ensure_completion_grants_table()
    conn = get_conn()
    conn.execute(
        "UPDATE completion_grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (now(), grant_id),
    )
    conn.commit()


def revoke_active_video_grants_for_episode(episode_id: str) -> int:
    ensure_completion_grants_table()
    conn = get_conn()
    cur = conn.execute(
        """UPDATE completion_grants SET revoked_at=?
           WHERE episode_id=? AND kind='video' AND revoked_at IS NULL AND consumed_at IS NULL""",
        (now(), episode_id),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def active_video_grant_budget_cap(episode_id: str) -> float | None:
    """若本集有未撤销的视频 grant，返回其 budget_cap_cny，供 enqueue 优先读取。"""
    ensure_completion_grants_table()
    row = get_conn().execute(
        """SELECT budget_cap_cny FROM completion_grants
           WHERE episode_id=? AND kind='video' AND revoked_at IS NULL
             AND (consumed_at IS NULL OR consumed_at=0)
             AND expires_at > ?
           ORDER BY issued_at DESC LIMIT 1""",
        (episode_id, now()),
    ).fetchone()
    if not row:
        return None
    try:
        return float(row["budget_cap_cny"]) if row["budget_cap_cny"] is not None else None
    except (TypeError, ValueError, KeyError):
        return None
