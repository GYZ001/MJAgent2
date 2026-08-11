from __future__ import annotations

from typing import Any
import hashlib
import json
from pathlib import Path

from app.db import get_conn


_CONFIRMED_EPISODE_STATUSES = frozenset({"confirmed", "generating", "done", "mixed"})


def current_adopted_video_delivery_manifest(
    episode_id: str,
    *,
    conn=None,
) -> dict[str, Any]:
    """Return a content-addressed snapshot of every adopted video."""
    db = conn or get_conn()
    rows = db.execute(
        """SELECT s.id AS shot_id,s.shot_no,s.adopted_version_id,
                  v.status AS version_status,v.video_path,v.artifact_id,v.playback_rate,
                  v.technical_validation_json
             FROM shots s
             LEFT JOIN shot_versions v ON v.id=s.adopted_version_id
            WHERE s.episode_id=? ORDER BY s.shot_no""",
        (episode_id,),
    ).fetchall()
    if not rows:
        raise ValueError("本集没有可交付镜头")
    items: list[dict[str, Any]] = []
    for row in rows:
        path = Path(str(row["video_path"] or ""))
        if (
            not row["adopted_version_id"]
            or row["version_status"] != "succeeded"
            or not path.is_file()
            or not row["artifact_id"]
        ):
            raise ValueError(f"镜 {row['shot_no']} 缺少已采纳的有效视频权威")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        from app.evidence import repository as evidence_repository

        artifact = evidence_repository.get_artifact(str(row["artifact_id"]), conn=db)
        if (
            artifact is None
            or artifact.get("type") != "shot_video"
            or artifact.get("scope_type") != "shot"
            or artifact.get("scope_id") != str(row["shot_id"])
            # Automatic best-candidate adoption retains a validated T3
            # Artifact; human adoption promotes it to approved. Both are
            # authoritative only with the exact technical gate below.
            or artifact.get("status") not in {"validated", "approved"}
            or artifact.get("contract_version") != "video-2.0.0"
            or Path(str(artifact.get("file_path") or "")).resolve() != path.resolve()
            or not isinstance(artifact.get("content"), dict)
            or str(artifact["content"].get("version_id") or "")
            != str(row["adopted_version_id"])
        ):
            raise ValueError(f"镜 {row['shot_no']} 的视频 Artifact 已失效")
        try:
            technical = json.loads(row["technical_validation_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"镜 {row['shot_no']} 的视频技术门禁证据损坏") from exc
        technical_evaluation = db.execute(
            """SELECT status,hard_gate_passed FROM evaluations
                 WHERE artifact_id=? AND evaluator_type='file'
                   AND evaluator_name='video_technical_validator'
                 ORDER BY created_at DESC LIMIT 1""",
            (row["artifact_id"],),
        ).fetchone()
        if (
            technical.get("passed") is not True
            or technical_evaluation is None
            or technical_evaluation["status"] != "passed"
            or int(technical_evaluation["hard_gate_passed"] or 0) != 1
        ):
            raise ValueError(f"镜 {row['shot_no']} 的视频技术门禁未通过")
        try:
            actual_artifact_hash = evidence_repository.content_hash(
                artifact.get("content"),
                artifact.get("file_path"),
            )
        except OSError as exc:
            raise ValueError(f"镜 {row['shot_no']} 的视频 Artifact 文件证据缺失") from exc
        if actual_artifact_hash != str(artifact.get("content_hash") or ""):
            raise ValueError(f"镜 {row['shot_no']} 的视频 Artifact 实际内容已漂移")
        items.append({
            "shot_id": str(row["shot_id"]),
            "shot_no": int(row["shot_no"]),
            "adopted_version_id": str(row["adopted_version_id"]),
            "artifact_id": str(row["artifact_id"]),
            "artifact_hash": actual_artifact_hash,
            "file_sha256": digest.hexdigest(),
            "playback_rate": float(row["playback_rate"] or 1.0),
        })
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "manifest_version": "adopted-video-delivery.v1",
        "episode_id": episode_id,
        "items": items,
        "manifest_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def verify_current_storyboard_release_authority(
    episode_id: str,
    *,
    conn=None,
) -> dict[str, Any]:
    """Verify the immutable storyboard release consumed by downstream outputs.

    Concatenation and delivery are release operations, so neither may infer
    authority from mutable shots or a merely validated candidate artifact.
    """
    db = conn or get_conn()
    episode = db.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise ValueError(f"分集不存在：{episode_id}")
    if str(episode["status"] or "") not in _CONFIRMED_EPISODE_STATUSES:
        raise ValueError("分镜尚未确认，禁止生成下游发布产物")

    projected_id = str(episode["storyboard_artifact_id"] or "")
    published_id = str(episode["published_storyboard_artifact_id"] or "")
    revision_id = str(episode["storyboard_production_revision_id"] or "")
    certificate_id = str(episode["storyboard_completion_certificate_id"] or "")
    if not all((projected_id, published_id, revision_id, certificate_id)):
        raise ValueError("当前分镜缺少已发布 Artifact、revision 或完成凭证")
    if projected_id != published_id:
        raise ValueError("当前分镜投影已偏离已发布 Artifact")

    revision = db.execute(
        """SELECT kind,episode_id,status,working_artifact_id,published_artifact_id,
                  input_fingerprint,contract_version,qa_profile_version
             FROM production_revisions WHERE id=?""",
        (revision_id,),
    ).fetchone()
    if (
        revision is None
        or str(revision["kind"] or "") != "storyboard"
        or str(revision["episode_id"] or "") != episode_id
        or str(revision["status"] or "") != "published"
        or str(revision["published_artifact_id"] or "") != published_id
    ):
        raise ValueError("当前分镜 production revision 未发布或绑定已漂移")

    artifact = db.execute(
        """SELECT type,scope_type,scope_id,status,content_hash,contract_version
             FROM artifacts WHERE id=?""",
        (published_id,),
    ).fetchone()
    if (
        artifact is None
        or str(artifact["type"] or "") != "storyboard"
        or str(artifact["scope_type"] or "") != "episode"
        or str(artifact["scope_id"] or "") != episode_id
        or str(artifact["status"] or "") != "approved"
        or not str(artifact["content_hash"] or "")
    ):
        raise ValueError("当前分镜 Artifact 不是本集已批准发布权威")

    from app.evidence import repository as evidence_repository

    artifact_record = evidence_repository.get_artifact(published_id, conn=db)
    if artifact_record is None:
        raise ValueError("当前分镜 Artifact 已不存在")
    try:
        actual_artifact_hash = evidence_repository.content_hash(
            artifact_record.get("content"),
            artifact_record.get("file_path"),
        )
    except OSError as exc:
        raise ValueError("当前分镜 Artifact 文件证据已缺失或不可读") from exc
    if actual_artifact_hash != str(artifact["content_hash"] or ""):
        raise ValueError("当前分镜 Artifact 实际内容与存储哈希不一致")

    from app.production.certificate import verify_completion_certificate

    certificate = verify_completion_certificate(
        certificate_id,
        expected_kind="storyboard",
        expected_scope_id=episode_id,
        expected_artifact_id=published_id,
        expected_artifact_hash=str(artifact["content_hash"] or ""),
        expected_production_revision_id=revision_id,
        allow_consumed=True,
        conn=db,
    )
    if certificate.consumed_at is None:
        raise ValueError("当前分镜完成凭证尚未被原子发布消费")
    if (
        str(revision["published_artifact_id"] or "") != certificate.artifact_id
        or str(revision["working_artifact_id"] or "") != certificate.artifact_id
        or str(revision["input_fingerprint"] or "") != certificate.input_fingerprint
        or str(revision["contract_version"] or "") != certificate.contract_version
        or str(revision["qa_profile_version"] or "") != certificate.qa_profile_version
        or str(artifact["contract_version"] or "") != certificate.contract_version
    ):
        raise ValueError("当前分镜 revision、凭证与 Artifact 合同绑定已漂移")

    from app.video_plan import current_storyboard_release_manifest

    manifest = current_storyboard_release_manifest(episode_id, conn=db)
    if (
        str(manifest.get("published_storyboard_artifact_id") or "") != published_id
        or str(manifest.get("published_storyboard_artifact_hash") or "")
        != str(artifact["content_hash"] or "")
        or str(manifest.get("completion_certificate_id") or "") != certificate_id
        or not str(manifest.get("release_qualification_hash") or "")
    ):
        raise ValueError("当前分镜 release qualification 已漂移")
    return {
        **manifest,
        "storyboard_production_revision_id": revision_id,
        "storyboard_completion_certificate_id": certificate_id,
        "episode_status": str(episode["status"] or ""),
    }
