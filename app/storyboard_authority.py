"""Versioned storyboard-outline duration authority."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import EvidenceArtifact
from app.schemas import Storyboard, StoryboardOutline


OUTLINE_AUTHORITY_VERSION = "storyboard-outline-authority.v1"
PLANNING_DURATION_SOURCE = "episodes.target_duration_s.before_storyboard_outline"
_EXPECTED_OUTLINE_ARTIFACT_UNSET = object()


class StoryboardOutlineAuthorityError(ValueError):
    """The persisted outline authority bundle is absent or internally split."""


class StoryboardOutlineMigrationRequired(StoryboardOutlineAuthorityError):
    """A pre-versioned outline must be adopted through the explicit migration path."""


@dataclass(frozen=True)
class StoryboardOutlineAuthority:
    episode_id: str
    outline: StoryboardOutline
    canonical_json: str
    authoritative_duration_s: int
    planning_duration_s: int
    planning_duration_source: str
    revision: int
    fingerprint: str
    artifact_id: str
    prompt_version: str


def _outline_values(
    outline: StoryboardOutline | dict[str, Any] | str,
) -> tuple[StoryboardOutline, dict[str, Any], str, str, int]:
    try:
        if isinstance(outline, StoryboardOutline):
            model = outline.model_copy(deep=True)
        elif isinstance(outline, str):
            model = StoryboardOutline.model_validate_json(outline)
        else:
            model = StoryboardOutline.model_validate(outline)
    except (TypeError, ValueError) as exc:
        raise StoryboardOutlineAuthorityError(
            f"storyboard outline JSON 无法验证：{exc}"
        ) from exc
    if not model.shots:
        raise StoryboardOutlineAuthorityError("storyboard outline 不含镜头")
    payload = model.model_dump(mode="json")
    canonical_json = model.model_dump_json()
    fingerprint = evidence_repository.content_hash(payload)
    duration_s = sum(int(shot.duration_s or 0) for shot in model.shots)
    if duration_s <= 0:
        raise StoryboardOutlineAuthorityError("storyboard outline 权威时长必须大于 0")
    return model, payload, canonical_json, fingerprint, duration_s


def outline_fingerprint(
    outline: StoryboardOutline | dict[str, Any] | str,
) -> str:
    return _outline_values(outline)[3]


def _metadata_complete(row: Any) -> bool:
    return bool(
        int(row["storyboard_outline_revision"] or 0) > 0
        and str(row["storyboard_outline_fingerprint"] or "")
        and str(row["storyboard_outline_artifact_id"] or "")
        and str(row["target_duration_authority"] or "")
        == OUTLINE_AUTHORITY_VERSION
        and row["planning_target_duration_s"] is not None
        and str(row["planning_duration_source"] or "")
    )


def _resolve_row(
    row: Any,
    *,
    conn,
    verify_shots: bool,
) -> StoryboardOutlineAuthority:
    episode_id = str(row["id"])
    if not row["storyboard_outline_json"]:
        raise StoryboardOutlineAuthorityError("当前剧集缺少权威 storyboard outline JSON")
    if not _metadata_complete(row):
        raise StoryboardOutlineMigrationRequired(
            "当前 storyboard outline 缺少完整 revision/fingerprint/duration authority；"
            "必须显式迁移，禁止按 target_duration_s 猜测恢复"
        )
    model, payload, canonical_json, fingerprint, duration_s = _outline_values(
        str(row["storyboard_outline_json"])
    )
    stored_fingerprint = str(row["storyboard_outline_fingerprint"] or "")
    if stored_fingerprint != fingerprint:
        raise StoryboardOutlineAuthorityError(
            "storyboard outline JSON 与已存 fingerprint 不一致"
        )
    stored_duration = int(row["target_duration_s"] or 0)
    if stored_duration != duration_s:
        raise StoryboardOutlineAuthorityError(
            "storyboard outline 权威时长与 episodes.target_duration_s 不一致："
            f"stored={stored_duration}, authoritative={duration_s}"
        )
    artifact_id = str(row["storyboard_outline_artifact_id"] or "")
    artifact = evidence_repository.get_artifact(artifact_id, conn=conn)
    if (
        artifact is None
        or artifact.get("type") != "storyboard_outline"
        or artifact.get("scope_type") != "episode"
        or str(artifact.get("scope_id") or "") != episode_id
        or artifact.get("status")
        in {"stale", "rejected", "superseded", "needs_revision"}
    ):
        raise StoryboardOutlineAuthorityError(
            "storyboard outline revision 未绑定当前集可用 Artifact"
        )
    artifact_fingerprint = str(artifact.get("content_hash") or "")
    if (
        artifact_fingerprint != fingerprint
        or evidence_repository.content_hash(artifact.get("content")) != fingerprint
    ):
        raise StoryboardOutlineAuthorityError(
            "storyboard outline Artifact 与已存 fingerprint 不一致"
        )
    revision = int(row["storyboard_outline_revision"] or 0)
    if revision != int(artifact.get("version") or 0):
        raise StoryboardOutlineAuthorityError(
            "storyboard outline revision 与 Artifact version 不一致"
        )
    planning_duration_s = int(row["planning_target_duration_s"] or 0)
    if planning_duration_s <= 0:
        raise StoryboardOutlineAuthorityError("规划阶段时长审计值无效")

    if verify_shots:
        shot_rows = conn.execute(
            "SELECT shot_no,duration_s FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        if len(shot_rows) != len(model.shots):
            raise StoryboardOutlineAuthorityError(
                "正式 shots 数量与权威 storyboard outline revision 不一致"
            )
        for row_shot, outline_shot in zip(shot_rows, model.shots):
            if (
                int(row_shot["shot_no"]) != int(outline_shot.shot_no)
                or int(row_shot["duration_s"] or 0)
                != int(outline_shot.duration_s or 0)
            ):
                raise StoryboardOutlineAuthorityError(
                    "正式 shots 时长投影与权威 storyboard outline revision 不一致"
                )

    return StoryboardOutlineAuthority(
        episode_id=episode_id,
        outline=model,
        canonical_json=canonical_json,
        authoritative_duration_s=duration_s,
        planning_duration_s=planning_duration_s,
        planning_duration_source=str(row["planning_duration_source"]),
        revision=revision,
        fingerprint=fingerprint,
        artifact_id=artifact_id,
        prompt_version=str(artifact.get("prompt_version") or ""),
    )


def resolve_storyboard_outline_authority(
    episode_id: str,
    *,
    conn=None,
    verify_shots: bool = False,
) -> StoryboardOutlineAuthority:
    db = conn or get_conn()
    row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if row is None:
        raise StoryboardOutlineAuthorityError(f"剧集不存在：{episode_id}")
    return _resolve_row(row, conn=db, verify_shots=verify_shots)


def _validated_outline_artifact(
    *,
    conn,
    episode_row: Any,
    payload: dict[str, Any],
    fingerprint: str,
    duration_s: int,
    artifact_id: str | None,
    migration_reason: str | None,
) -> dict[str, Any]:
    if artifact_id:
        artifact = evidence_repository.get_artifact(str(artifact_id), conn=conn)
        if (
            artifact is None
            or artifact.get("type") != "storyboard_outline"
            or artifact.get("scope_type") != "episode"
            or str(artifact.get("scope_id") or "") != str(episode_row["id"])
            or artifact.get("status")
            in {"stale", "rejected", "superseded", "needs_revision"}
            or str(artifact.get("content_hash") or "") != fingerprint
            or evidence_repository.content_hash(artifact.get("content")) != fingerprint
        ):
            raise StoryboardOutlineAuthorityError(
                "待采用 storyboard outline Artifact 与权威 JSON 不一致"
            )
        return artifact

    parent_ids = [
        str(value)
        for value in (
            episode_row["storyboard_outline_artifact_id"],
            episode_row["screenplay_artifact_id"],
        )
        if str(value or "").strip()
    ]
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard_outline",
            scope_type="episode",
            scope_id=str(episode_row["id"]),
            status="validated",
            trust_level="T2",
            content=payload,
            parent_artifact_ids=list(dict.fromkeys(parent_ids)),
            contract_version=get_contract("storyboard").version,
            prompt_version=OUTLINE_AUTHORITY_VERSION,
            model_snapshot={
                "authority_version": OUTLINE_AUTHORITY_VERSION,
                "authoritative_duration_s": duration_s,
                "migration_reason": migration_reason,
            },
        ),
        conn=conn,
        commit=False,
    )
    if str(artifact.get("content_hash") or "") != fingerprint:
        raise StoryboardOutlineAuthorityError(
            "新建 storyboard outline Artifact fingerprint 对账失败"
        )
    return artifact


def persist_storyboard_outline_authority(
    episode_id: str,
    outline: StoryboardOutline | dict[str, Any] | str,
    *,
    artifact_id: str | None = None,
    conn=None,
    commit: bool = True,
    allow_unversioned_migration: bool = False,
    migration_reason: str | None = None,
    expected_outline_artifact_id: str | None | object = (
        _EXPECTED_OUTLINE_ARTIFACT_UNSET
    ),
    expected_outline_revision: int | None = None,
    expected_outline_fingerprint: str | None = None,
    sync_supervisor_checkpoint: bool = True,
) -> StoryboardOutlineAuthority:
    """Atomically adopt one gate-passed outline and its duration authority."""
    db = conn or get_conn()
    model, payload, canonical_json, fingerprint, duration_s = _outline_values(outline)
    started_transaction = not db.in_transaction
    if started_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if row is None:
            raise StoryboardOutlineAuthorityError(f"剧集不存在：{episode_id}")
        observed_artifact_id = str(
            row["storyboard_outline_artifact_id"] or ""
        )
        observed_revision = int(row["storyboard_outline_revision"] or 0)
        observed_fingerprint = str(
            row["storyboard_outline_fingerprint"] or ""
        )
        if (
            expected_outline_artifact_id
            is not _EXPECTED_OUTLINE_ARTIFACT_UNSET
            and observed_artifact_id
            != str(expected_outline_artifact_id or "")
        ):
            raise StoryboardOutlineAuthorityError(
                "storyboard outline authority CAS 并发冲突：Artifact 已变化"
            )
        if (
            expected_outline_revision is not None
            and observed_revision != int(expected_outline_revision)
        ):
            raise StoryboardOutlineAuthorityError(
                "storyboard outline authority CAS 并发冲突：revision 已变化"
            )
        if (
            expected_outline_fingerprint is not None
            and observed_fingerprint
            != str(expected_outline_fingerprint or "")
        ):
            raise StoryboardOutlineAuthorityError(
                "storyboard outline authority CAS 并发冲突：fingerprint 已变化"
            )
        existing_json = str(row["storyboard_outline_json"] or "")
        if existing_json and not _metadata_complete(row):
            if not allow_unversioned_migration:
                raise StoryboardOutlineMigrationRequired(
                    "检测到未版本化 storyboard outline；必须显式迁移后才能继续"
                )
        if _metadata_complete(row):
            current = _resolve_row(row, conn=db, verify_shots=False)
            if current.fingerprint == fingerprint:
                if artifact_id and str(artifact_id) != current.artifact_id:
                    candidate = _validated_outline_artifact(
                        conn=db,
                        episode_row=row,
                        payload=payload,
                        fingerprint=fingerprint,
                        duration_s=duration_s,
                        artifact_id=artifact_id,
                        migration_reason=migration_reason,
                    )
                    if int(candidate.get("version") or 0) < current.revision:
                        raise StoryboardOutlineAuthorityError(
                            "不得把 storyboard outline revision 回退到旧 Artifact"
                        )
                if sync_supervisor_checkpoint:
                    from app.storyboard_supervisor import (
                        _persist_outline_authority_checkpoint,
                    )

                    _persist_outline_authority_checkpoint(
                        db,
                        current,
                    )
                if commit:
                    db.commit()
                return current

        artifact = _validated_outline_artifact(
            conn=db,
            episode_row=row,
            payload=payload,
            fingerprint=fingerprint,
            duration_s=duration_s,
            artifact_id=artifact_id,
            migration_reason=migration_reason,
        )
        planning_duration_s = int(
            row["planning_target_duration_s"]
            if row["planning_target_duration_s"] is not None
            else row["target_duration_s"] or 0
        )
        if planning_duration_s <= 0:
            raise StoryboardOutlineAuthorityError("规划阶段时长审计值无效")
        planning_source = str(
            row["planning_duration_source"] or PLANNING_DURATION_SOURCE
        )
        revision = int(artifact.get("version") or 0)
        if revision <= 0:
            raise StoryboardOutlineAuthorityError("storyboard outline Artifact version 无效")
        cursor = db.execute(
            """UPDATE episodes
                  SET planning_target_duration_s=?,
                      planning_duration_source=?,
                      target_duration_s=?,
                      target_duration_authority=?,
                      storyboard_outline_json=?,
                      storyboard_outline_revision=?,
                      storyboard_outline_fingerprint=?,
                      storyboard_outline_artifact_id=?
                WHERE id=?
                  AND COALESCE(storyboard_outline_artifact_id,'')=?
                  AND storyboard_outline_revision=?
                  AND COALESCE(storyboard_outline_fingerprint,'')=?""",
            (
                planning_duration_s,
                planning_source,
                duration_s,
                OUTLINE_AUTHORITY_VERSION,
                canonical_json,
                revision,
                fingerprint,
                str(artifact["id"]),
                episode_id,
                observed_artifact_id,
                observed_revision,
                observed_fingerprint,
            ),
        )
        if cursor.rowcount != 1:
            raise StoryboardOutlineAuthorityError(
                "storyboard outline authority 持久化发生并发冲突"
            )
        adopted = _resolve_row(
            db.execute(
                "SELECT * FROM episodes WHERE id=?", (episode_id,)
            ).fetchone(),
            conn=db,
            verify_shots=False,
        )
        if adopted.outline.model_dump(mode="json") != model.model_dump(mode="json"):
            raise StoryboardOutlineAuthorityError(
                "storyboard outline authority 持久化后内容对账失败"
            )
        if sync_supervisor_checkpoint:
            from app.storyboard_supervisor import (
                _persist_outline_authority_checkpoint,
            )

            _persist_outline_authority_checkpoint(
                db,
                adopted,
            )
        if commit:
            db.commit()
        return adopted
    except Exception:
        if started_transaction:
            db.rollback()
        raise


def persist_storyboard_outline_projection(
    episode_id: str,
    outline: StoryboardOutline | dict[str, Any] | str,
    *,
    artifact_id: str | None = None,
    conn=None,
    commit: bool = True,
    expected_outline_artifact_id: str | None | object = (
        _EXPECTED_OUTLINE_ARTIFACT_UNSET
    ),
    expected_outline_revision: int | None = None,
    expected_outline_fingerprint: str | None = None,
    sync_supervisor_checkpoint: bool = True,
) -> StoryboardOutlineAuthority | None:
    """Persist modern authority strictly while retaining plan-null legacy drafts."""
    db = conn or get_conn()
    model = (
        outline.model_copy(deep=True)
        if isinstance(outline, StoryboardOutline)
        else StoryboardOutline.model_validate_json(outline)
        if isinstance(outline, str)
        else StoryboardOutline.model_validate(outline)
    )
    row = db.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if row is None:
        raise StoryboardOutlineAuthorityError(f"剧集不存在：{episode_id}")
    narrative_authority = False
    raw_screenplay = str(row["screenplay_json"] or "")
    if raw_screenplay:
        from app.schemas import EpisodeScreenplay

        try:
            narrative_authority = (
                EpisodeScreenplay.model_validate_json(
                    raw_screenplay
                ).narrative_plan
                is not None
            )
        except (TypeError, ValueError):
            narrative_authority = bool(
                str(row["target_duration_authority"] or "")
                == OUTLINE_AUTHORITY_VERSION
            )
    authority_required = bool(
        narrative_authority
        or str(row["target_duration_authority"] or "")
        == OUTLINE_AUTHORITY_VERSION
    )
    has_complete_durations = bool(
        model.shots
        and all(int(shot.duration_s or 0) > 0 for shot in model.shots)
    )
    if authority_required or has_complete_durations:
        return persist_storyboard_outline_authority(
            episode_id,
            model,
            artifact_id=artifact_id,
            conn=db,
            commit=commit,
            allow_unversioned_migration=(
                has_complete_durations and not authority_required
            ),
            migration_reason=(
                "legacy_plan_null_outline_adoption"
                if has_complete_durations and not authority_required
                else None
            ),
            expected_outline_artifact_id=expected_outline_artifact_id,
            expected_outline_revision=expected_outline_revision,
            expected_outline_fingerprint=expected_outline_fingerprint,
            sync_supervisor_checkpoint=sync_supervisor_checkpoint,
        )

    # A plan-null legacy draft without durations is not an authoritative
    # production input. Preserve its resumable JSON without inventing a total.
    db.execute(
        "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
        (model.model_dump_json(), episode_id),
    )
    if commit:
        db.commit()
    return None


def migrate_storyboard_outline_authority(
    episode_id: str,
    *,
    expected_stored_target_duration_s: int,
    expected_outline_fingerprint: str,
    conn=None,
) -> StoryboardOutlineAuthority:
    """Explicitly adopt a legacy outline after operator-supplied CAS checks."""
    db = conn or get_conn()
    row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if row is None:
        raise StoryboardOutlineAuthorityError(f"剧集不存在：{episode_id}")
    if _metadata_complete(row):
        raise StoryboardOutlineAuthorityError("当前 outline 已有完整权威版本，无需迁移")
    if int(row["target_duration_s"] or 0) != int(expected_stored_target_duration_s):
        raise StoryboardOutlineAuthorityError("迁移前 stored target_duration_s 已变化")
    raw_outline = str(row["storyboard_outline_json"] or "")
    if not raw_outline or outline_fingerprint(raw_outline) != expected_outline_fingerprint:
        raise StoryboardOutlineAuthorityError("迁移前 storyboard outline fingerprint 已变化")
    return persist_storyboard_outline_authority(
        episode_id,
        raw_outline,
        conn=db,
        allow_unversioned_migration=True,
        migration_reason="explicit_legacy_outline_authority_migration",
    )


def assert_storyboard_matches_outline_authority(
    authority: StoryboardOutlineAuthority,
    board: Storyboard,
) -> None:
    if len(board.shots) != len(authority.outline.shots):
        raise StoryboardOutlineAuthorityError(
            "Storyboard Artifact 镜头数与权威 outline revision 不一致"
        )
    for shot, planned in zip(board.shots, authority.outline.shots):
        if (
            int(shot.shot_no) != int(planned.shot_no)
            or int(shot.duration_s or 0) != int(planned.duration_s or 0)
        ):
            raise StoryboardOutlineAuthorityError(
                "Storyboard Artifact 镜头时长与权威 outline revision 不一致"
            )


def clear_storyboard_outline_authority(
    episode_id: str,
    *,
    conn=None,
    clear_outline: bool = True,
) -> None:
    """Return target_duration_s to its preserved planning estimate."""
    db = conn or get_conn()
    db.execute(
        """UPDATE episodes
              SET target_duration_s=COALESCE(
                    planning_target_duration_s,
                    target_duration_s
                  ),
                  target_duration_authority='planning_estimate',
                  storyboard_outline_json=CASE
                    WHEN ? THEN NULL ELSE storyboard_outline_json END,
                  storyboard_outline_revision=0,
                  storyboard_outline_fingerprint=NULL,
                  storyboard_outline_artifact_id=NULL
            WHERE id=?""",
        (1 if clear_outline else 0, episode_id),
    )
