"""对外镜头版本列表投影（图片输入字符上限常量与版本裁剪）。

从 app/domain/storyboard_ops.py 按原样搬移；被 episode_detail 依赖。
"""
from __future__ import annotations

import json

from app.db import rows_to_dicts
from app.domain.common import (
    _media_url,
    _public_failure_log,
    _public_reference_image,
)
from app.media_urls import build_media_url


_MAX_PUBLIC_IMAGE_INPUT_CHARS = 1_000_000

def _public_shot_versions(conn, shot_id: str, *, include_inputs: bool) -> list[dict]:
    if include_inputs:
        rows = conn.execute(
            """SELECT id, shot_id, version_no, prompt_text, status, error,
                      video_path, qa_json, cost_cny, latency_s, artifact_id,
                      adoption_reason, playback_rate, technical_validation_json, created_at,
                      provider_task_id,
                      (SELECT job.attempt_started_at FROM jobs AS job
                        WHERE job.version_id=shot_versions.id
                          AND job.attempt_started_at IS NOT NULL
                          AND job.status NOT IN ('succeeded','failed','cancelled')
                        ORDER BY job.attempt_started_at DESC LIMIT 1) AS running_since,
                      CASE WHEN status='rejected_static_fallback'
                           THEN 1 ELSE 0 END AS delivery_fallback,
                      CASE WHEN length(image_inputs) <= ? THEN image_inputs END AS image_inputs,
                      CASE WHEN length(image_inputs) > ? THEN 1 ELSE 0 END AS image_inputs_omitted
               FROM shot_versions
               WHERE shot_id=? AND status!='cleared'
               ORDER BY version_no DESC""",
            (_MAX_PUBLIC_IMAGE_INPUT_CHARS, _MAX_PUBLIC_IMAGE_INPUT_CHARS, shot_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, shot_id, version_no, '' AS prompt_text, status, error,
                      video_path, qa_json, cost_cny, latency_s, artifact_id,
                      adoption_reason, playback_rate, technical_validation_json, created_at,
                      provider_task_id,
                      (SELECT job.attempt_started_at FROM jobs AS job
                        WHERE job.version_id=shot_versions.id
                          AND job.attempt_started_at IS NOT NULL
                          AND job.status NOT IN ('succeeded','failed','cancelled')
                        ORDER BY job.attempt_started_at DESC LIMIT 1) AS running_since,
                      CASE WHEN status='rejected_static_fallback'
                           THEN 1 ELSE 0 END AS delivery_fallback,
                      NULL AS image_inputs
               FROM shot_versions
               WHERE shot_id=? AND status!='cleared'
               ORDER BY version_no DESC""",
            (shot_id,),
        ).fetchall()
    versions = [
        version for version in rows_to_dicts(rows)
        if not bool(version.pop("delivery_fallback", 0))
    ]
    reference_lineage: dict[str, list[str]] = {}
    if include_inputs:
        for version in versions:
            raw_meta = json.loads(version.get("image_inputs") or "{}")
            for raw_ref in raw_meta.get("reference_images") or []:
                ref_id = raw_ref.get("id") if isinstance(raw_ref, dict) else None
                if ref_id:
                    reference_lineage.setdefault(str(ref_id), []).append(str(version["id"]))
    for version in versions:
        version["qa"] = json.loads(version["qa_json"]) if version["qa_json"] else None
        version.pop("qa_json", None)
        meta = json.loads(version.get("image_inputs") or "{}") if include_inputs else {}
        inputs_omitted = bool(version.pop("image_inputs_omitted", 0))
        boundary_contract = (
            meta.get("boundary_pair_qa")
            if isinstance(meta.get("boundary_pair_qa"), dict)
            else {}
        )
        upstream_video_url = None
        upstream_video_revision = str(
            meta.get("upstream_adopted_video_revision") or ""
        )
        if upstream_video_revision:
            upstream_video = conn.execute(
                """SELECT video_path FROM shot_versions
                   WHERE id=? AND status='succeeded'""",
                (upstream_video_revision,),
            ).fetchone()
            if upstream_video:
                upstream_video_url = _media_url(upstream_video["video_path"])
        refs = [
            _public_reference_image(ref)
            for ref in (meta.get("reference_images") or [])
            if isinstance(ref, dict)
        ]
        for ref in refs:
            ref["referenced_by_version_ids"] = reference_lineage.get(str(ref.get("id")), [])
        version["image_inputs"] = {
            "first_frame_used": bool(meta.get("first_frame_used")),
            "first_frame_src": meta.get("first_frame_src"),
            "first_frame_source": boundary_contract.get("first_frame_source"),
            "first_frame_scene_id": meta.get("first_frame_scene_id"),
            "first_frame_image_url": _media_url(meta.get("first_frame_path")),
            "last_frame_used": bool(meta.get("last_frame_used")),
            "last_frame_src": meta.get("last_frame_src"),
            "last_frame_source": boundary_contract.get("last_frame_source"),
            "last_frame_scene_id": meta.get("last_frame_scene_id"),
            "last_frame_image_url": _media_url(meta.get("last_frame_path")),
            "video_input_url": upstream_video_url or meta.get("video_input_url"),
            "video_input_source_revision_id": upstream_video_revision or None,
            "mode": meta.get("mode"),
            "mode_decision": meta.get("mode_decision"),
            "planned_mode": meta.get("planned_mode"),
            "actual_mode": meta.get("actual_mode"),
            "video_input_intent": meta.get("video_input_intent"),
            "ai_video_prompt_contract_version": meta.get(
                "ai_video_prompt_contract_version"
            ),
            "ai_video_prompt_generated_at": meta.get(
                "ai_video_prompt_generated_at"
            ),
            "required_reference_characters": list(
                meta.get("required_reference_characters") or []
            ),
            "required_interaction_reference_characters": list(
                meta.get("required_interaction_reference_characters") or []
            ),
            "reference_image_used": bool(meta.get("reference_image_used")),
            "reference_images": refs,
            "reference_failure_logs": [
                _public_failure_log(item)
                for item in (meta.get("reference_failure_logs") or [])
                if isinstance(item, dict)
            ],
            "fallback_reason": meta.get("fallback_reason"),
            "retry_reason": meta.get("retry_reason"),
            "omitted_for_size": inputs_omitted,
        }
        if version.get("video_path"):
            version["video_url"] = build_media_url(version["video_path"])
    return versions
