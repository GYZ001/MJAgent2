from __future__ import annotations

from typing import Any

from app.db import get_conn


_CONFIRMED_EPISODE_STATUSES = frozenset({"confirmed", "generating", "done", "mixed"})


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
        """SELECT scope_type,scope_id,status,content_hash,contract_version
             FROM artifacts WHERE id=?""",
        (published_id,),
    ).fetchone()
    if (
        artifact is None
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
