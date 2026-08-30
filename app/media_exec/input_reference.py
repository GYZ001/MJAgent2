"""参考图模式的视频输入准备（拆分自 ``run_job.py``）。

单函数文件：``_prepare_reference_mode_inputs`` 是参考图（角色/场景画廊）驱动的
视频输入组装，逐行搬移自 ``run_job.py``，未重写、未拆分——函数体本身较长（超
过 ``max_function_lines_python`` 默认阈值），已在 ``app/FILE_CONVENTIONS.toml``
登记为已知的单一大函数基线（与 ``_prepare_first_last_mode_inputs`` 等同类模式
准备函数同理，CLAUDE.md「移动，不是重写」）。
"""

from __future__ import annotations

import json
from typing import Any

from app import config, video_modes
from app.db import log_provider_call, now
from app.hiagent import ProviderError

from .enqueue import _load_shot_model, _row_value
from .authority import _assert_job_lease
from .fences import VideoInputRepairRequired
from .input_boundary import _ContinuityWait
from .job_state import _set_version
from .reference_progress import _narrative_keyframe_candidate_progress


async def _prepare_reference_mode_inputs(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str | None = None,
) -> tuple[dict, str]:
    if meta.get("mode") != video_modes.REFERENCE_IMAGE_MODE:
        return meta, prompt_text

    def _assert_reference_lease() -> None:
        if lease_owner is not None:
            _assert_job_lease(job["id"], lease_owner)

    def _invalidate_reference_checkpoint(reason: str) -> None:
        meta["stale_reference_reason"] = reason
        meta["stale_keyframe_prompt_contract_version"] = meta.get("keyframe_prompt_contract_version")
        meta["keyframe_prompt_contract_version"] = video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION
        meta["reference_input_policy_version"] = video_modes.REFERENCE_INPUT_POLICY_VERSION
        meta.pop("keyframe_contract_fingerprint", None)
        meta["reference_images"] = []
        meta["reference_slots"] = {}
        meta.pop("keyframe_sequence", None)
        meta["reference_manifest_frozen"] = False
        meta["reference_manifest_asset_stale"] = True
        meta["reference_generation_complete"] = False
        meta["reference_static_ready"] = False
        meta["continuity_anchor_ready"] = False
        meta["reference_group_gate_passed"] = False
        meta["video_input_manifest_frozen"] = False
        meta.pop("narrative_keyframe_missing", None)
        # 新画廊不得沿用旧 fingerprint/refset，否则 reference_store 会早返并指回旧图。
        for stale_key in (
            "reference_set_id", "reference_gallery_fingerprint", "reference_gallery_revision",
            "reference_gallery_source_version_id", "reference_gallery_edited",
            "reference_gallery_contract_override", "video_input_fingerprint",
        ):
            meta.pop(stale_key, None)

    # Historical galleries predate this marker and are complete.  A gallery
    # explicitly marked incomplete is a streamed checkpoint from an interrupted
    # generation and must resume instead of being mistaken for the final set.
    complete_gallery_candidate = False
    if meta.get("reference_images"):
        incomplete_checkpoint = meta.get("reference_generation_complete") is False
        if incomplete_checkpoint:
            checkpoint_matches = video_modes.reference_gallery_matches_library_policy(meta)
            if not checkpoint_matches:
                _invalidate_reference_checkpoint("library_reference_checkpoint_invalid")
            elif (
                meta.get("reference_static_ready")
                and not video_modes.reference_gallery_matches_library_policy(meta)
            ):
                _invalidate_reference_checkpoint("library_reference_file_invalid")
        else:
            gallery_matches = video_modes.reference_gallery_matches_library_policy(meta)
            if gallery_matches:
                complete_gallery_candidate = True
            else:
                _invalidate_reference_checkpoint("reference_input_policy_or_file_invalid")
    from app.schemas import Bible
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    # 本集视图：关键帧文字锚点与参考图按集取覆盖该集的分段定妆照（同段同源）
    from app.portraits import bible_for_episode
    bible = bible_for_episode(job["project_id"], bible, ep["episode_no"])
    screenplay = None
    # Real episode rows always carry ``id``.  Lightweight legacy unit-test
    # rows intentionally do not; keep those on the explicit legacy path.
    if _row_value(ep, "id") or _row_value(ep, "screenplay_json"):
        from app.production.screenplay_authority import resolve_downstream_screenplay

        screenplay = resolve_downstream_screenplay(
            job["episode_id"], conn=conn,
        ).screenplay
    shot_model = _load_shot_model(shot)
    # 入队时 compile_prompt 已把接触镜机位确定性归一为“侧面”。执行时必须使用
    # 该视频版本冻结的合同，不能只重读 shots 行中可能较旧的 camera_angle。
    from app.continuity import apply_shot_contract
    apply_shot_contract(shot_model, meta.get("shot_contract_json"))
    prev_shot = conn.execute("SELECT * FROM shots WHERE id=?", (meta.get("after_shot_id"),)).fetchone() if meta.get("after_shot_id") else None
    needs_tail = False
    if complete_gallery_candidate:
        # 提示词合同相同仍不代表人物/场景锚点未变。入队复用会把
        # manifest 一起带过来；兼容从关键帧 asset 内的冻结副本回退读取。
        frozen_manifest = meta.get("reference_manifest")
        if not isinstance(frozen_manifest, dict):
            frozen_manifest = next(
                (
                    ref.get("dependency_manifest") for ref in (meta.get("reference_images") or [])
                    if isinstance(ref, dict) and isinstance(ref.get("dependency_manifest"), dict)
                ),
                None,
            )
        if not video_modes.reference_gallery_matches_library_policy(meta):
            _invalidate_reference_checkpoint("reference_input_policy_changed")
            complete_gallery_candidate = False
        if complete_gallery_candidate and needs_tail:
            frozen_tail_contract = next(
                (
                    (ref.get("dependency_manifest") or {}).get("continuity_source")
                    for ref in (meta.get("reference_images") or [])
                    if isinstance(ref, dict) and ref.get("type") == "previous_shot_frame"
                ),
                None,
            )
            current_tail_contract = video_modes.previous_tail_source_contract(conn, prev_shot)
            if not isinstance(frozen_tail_contract, dict) or frozen_tail_contract != current_tail_contract:
                _invalidate_reference_checkpoint("continuity_tail_source_changed")
                complete_gallery_candidate = False
        from app.multiview import manifest_revisions_match, resolve_shot_asset_dependencies

        if complete_gallery_candidate:
            current_manifest = resolve_shot_asset_dependencies(
                project_id=job["project_id"], episode_no=ep["episode_no"], shot_id=job["shot_id"],
                shot=shot_model, scene_name=getattr(shot_model, "scene_name", "") or None,
                conn=conn, bible=bible, screenplay=screenplay,
            )
            if isinstance(frozen_manifest, dict) and manifest_revisions_match(frozen_manifest, current_manifest):
                meta["reference_manifest"] = frozen_manifest
                meta["reference_manifest_frozen"] = True
                if video_modes.REFERENCE_PROMPT_NOTE_MARKER not in prompt_text:
                    packed_refs = video_modes.pack_reference_images_for_seedance(
                        list(meta.get("reference_images") or []),
                        required_identity_names=list(
                            meta.get("required_reference_characters") or []
                        ),
                    )
                    prompt_text = (
                        video_modes.append_reference_prompt_notes_from_dicts(
                            prompt_text,
                            packed_refs,
                            duration_s=shot_model.duration_s,
                        )
                    )
                set_pipeline_stage(
                    job["id"],
                    media_stages.STAGE_VIDEO_READY,
                    scheduler_lane=media_stages.LANE_VIDEO_READY,
                    ready_at=now(),
                    conn=conn,
                )
                _set_version(
                    version["id"],
                    image_inputs=json.dumps(meta, ensure_ascii=False),
                    prompt_text=prompt_text,
                )
                conn.commit()
                return meta, prompt_text
            _invalidate_reference_checkpoint("reference_dependency_manifest_changed")
    # 复用入队时已确定的模式决策，不在生成时再跑一次 LLM 选择：既省每镜一次文本调用，
    # 又避免模式在入队与执行之间无谓翻转（决策应在入队时一次定死）。
    decision = video_modes.dict_to_decision(meta.get("mode_decision") or {})
    if decision.mode != video_modes.REFERENCE_IMAGE_MODE:
        raise ProviderError("参考图输入准备收到非参考图计划，禁止执行层改写模式")
    shot_id = job["shot_id"]
    if meta.get("reference_static_ready") and needs_tail and meta.get("reference_images"):
        from app.multiview import manifest_revisions_match, resolve_shot_asset_dependencies

        frozen_manifest = meta.get("reference_manifest")
        current_manifest = resolve_shot_asset_dependencies(
            project_id=job["project_id"], episode_no=ep["episode_no"], shot_id=shot_id,
            shot=shot_model, scene_name=getattr(shot_model, "scene_name", "") or None,
            conn=conn, bible=bible, screenplay=screenplay,
        )
        if not isinstance(frozen_manifest, dict) or not manifest_revisions_match(frozen_manifest, current_manifest):
            _invalidate_reference_checkpoint("reference_dependency_manifest_changed")
        elif not video_modes.reference_gallery_matches_library_policy(meta):
            # 静态预取点可能在 worker 崩溃后只剩 evidence，或关键帧文件已丢失。
            # 连续性快路不能只装配尾帧就把这组资产标成完成。
            _invalidate_reference_checkpoint("static_keyframe_contract_or_file_invalid")
    rejection_details: list[dict[str, Any]] = []
    rejected_assets: list = []

    def _reference_keyframe_gate_passed(current_assets: list) -> bool:
        """Validate the exact existing-library files returned by the builder."""
        return video_modes.reference_gallery_matches_library_policy({
            **meta,
            "reference_images": [a.public_dict() for a in current_assets],
        })

    def _delete_rejected_assets(items: list) -> None:
        # Never let a recovered/stale worker remove files owned by the new
        # attempt.  This check also extends the lease at every checkpoint.
        _assert_reference_lease()
        from app.rejected_media import discard_file
        for asset in items:
            discard_file(getattr(asset, "path", None))
            asset.path = None
            asset.url = None

    def _persist_reference_progress(current_assets: list, current_rejected: list) -> None:
        """Checkpoint usable references only; rejected images are irrecoverably removed."""
        _delete_rejected_assets(current_rejected)
        meta["mode_decision"] = video_modes.decision_to_dict(decision)
        meta["reference_generation_complete"] = False
        meta["reference_images"] = video_modes.dedupe_reference_dicts(
            [a.public_dict() for a in current_assets]
        )
        candidate_done, candidate_total = _narrative_keyframe_candidate_progress(meta)
        set_pipeline_stage(
            job["id"], media_stages.STAGE_REFERENCE_GENERATE,
            stage_progress={
                "current": candidate_done,
                "total": candidate_total,
                "unit": "library_assets",
            },
            scheduler_lane=media_stages.LANE_REFERENCE_CRITICAL if needs_tail else media_stages.LANE_REFERENCE_NORMAL,
            conn=conn,
        )
        _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))
        conn.commit()

    # 连续镜两段式：静态参考可预取；缺尾帧时不得宣称最终完成
    set_pipeline_stage(job["id"], media_stages.STAGE_REFERENCE_PROMPT, conn=conn)
    conn.commit()

    # 若已静态就绪、仅等尾帧：只做装配，不重跑整组生成
    if meta.get("reference_static_ready") and needs_tail and meta.get("reference_images"):
        from app.media_pipeline.scheduler import continuity_anchor_ready
        ready, reason = continuity_anchor_ready(conn, job["after_shot_id"] or (prev_shot["id"] if prev_shot else None))
        if not ready:
            set_pipeline_stage(
                job["id"], media_stages.STAGE_WAITING_CONTINUITY,
                reason_code="WAITING_CONTINUITY_ANCHOR",
                reason_text=reason or "参考图已备齐，等待上一镜尾帧",
                conn=conn,
            )
            conn.commit()
            raise _ContinuityWait(reason or "参考图已备齐，等待上一镜尾帧")
        set_pipeline_stage(job["id"], media_stages.STAGE_CONTINUITY_ASSEMBLING, conn=conn)
        conn.commit()
        assets = await video_modes.assemble_continuity_tail(
            conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
            shot_id=shot_id, shot=shot_model, bible=bible, meta=meta, prev_shot=prev_shot,
            rejection_details=rejection_details, rejected_out=rejected_assets,
            screenplay=screenplay,
        )
        if assets:
            _delete_rejected_assets(rejected_assets)
            assembled_refs = video_modes.dedupe_reference_dicts(
                [a.public_dict() for a in assets]
            )
            assembled_meta = {**meta, "reference_images": assembled_refs}
            if not video_modes.reference_gallery_matches_library_policy(assembled_meta):
                _invalidate_reference_checkpoint("continuity_assembly_library_asset_invalid")
                assets = []
        if assets:
            meta["reference_images"] = assembled_refs
            meta["reference_generation_complete"] = True
            meta["reference_static_ready"] = True
            meta["continuity_anchor_ready"] = True
            meta["reference_group_gate_passed"] = True
            meta["video_input_manifest_frozen"] = True
            meta.pop("first_frame_path", None)
            meta.pop("last_frame_path", None)
            prompt_text = video_modes.append_reference_prompt_notes(
                prompt_text,
                assets,
                duration_s=shot_model.duration_s,
                required_identity_names=list(
                    meta.get("required_reference_characters") or []
                ),
            )
            try:
                from app.media_pipeline.reference_store import upsert_reference_set_from_meta
                upsert_reference_set_from_meta(
                    shot_id=shot_id, version_id=version["id"], meta=meta, conn=conn,
                    static_ready=True, continuity_ready=True, group_gate_passed=True,
                )
            except Exception:  # noqa: BLE001
                pass
            set_pipeline_stage(
                job["id"], media_stages.STAGE_VIDEO_READY,
                scheduler_lane=media_stages.LANE_VIDEO_READY, ready_at=now(), conn=conn,
            )
            _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
            conn.commit()
            return meta, prompt_text

    assets = await video_modes.build_reference_assets(
        conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
        shot_id=shot_id, shot=shot_model, bible=bible, decision=decision, prev_shot=prev_shot,
        rejection_details=rejection_details, rejected_out=rejected_assets,
        on_progress=_persist_reference_progress,
        allow_missing_continuity_tail=needs_tail,
        job_id=job["id"],
        existing_meta=meta,
        screenplay=screenplay,
    )

    # A pre-fix recovered worker could leave a selected/passed keyframe row
    # after another stale worker deleted the underlying file.  Anchors still
    # make ``assets`` truthy, so the ordinary empty-result retry cannot repair
    # this poisoned checkpoint.  Clear it durably and rebuild once in the same
    # task before surfacing an error or attempting a paid video submission.
    if assets and not _reference_keyframe_gate_passed(assets):
        _assert_reference_lease()
        log_provider_call(
            "reference_keyframe_checkpoint_auto_repair",
            config.MODEL_TEXT,
            "REFERENCE_CHECKPOINT_AUTO_REPAIR",
            None,
            0,
            meta={
                "shot_id": shot_id,
                "reason": "final_keyframe_file_missing",
                "repair_attempt": 1,
            },
        )
        _delete_rejected_assets(rejected_assets)
        rejected_assets = []
        _invalidate_reference_checkpoint("final_keyframe_file_missing")
        meta["keyframe_file_repair_count"] = int(meta.get("keyframe_file_repair_count") or 0) + 1
        _set_version(
            version["id"],
            image_inputs=json.dumps(meta, ensure_ascii=False),
            prompt_text=prompt_text,
        )
        conn.commit()

        repair_rejection: list[dict[str, Any]] = []
        assets = await video_modes.build_reference_assets(
            conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
            shot_id=shot_id, shot=shot_model, bible=bible, decision=decision, prev_shot=prev_shot,
            rejection_details=repair_rejection, rejected_out=rejected_assets,
            on_progress=_persist_reference_progress,
            allow_missing_continuity_tail=needs_tail,
            job_id=job["id"],
            existing_meta=meta,
            screenplay=screenplay,
        )
        rejection_details.extend(repair_rejection)

    # 静态完成但缺强制尾帧 → 停在 waiting_continuity，不标 complete
    if assets and needs_tail:
        has_tail = any(getattr(a, "type", None) == "previous_shot_frame" for a in assets)
        if not has_tail:
            meta["mode_decision"] = video_modes.decision_to_dict(decision)
            _delete_rejected_assets(rejected_assets)
            meta["reference_images"] = video_modes.dedupe_reference_dicts(
                [a.public_dict() for a in assets]
            )
            meta["reference_static_ready"] = True
            meta["reference_generation_complete"] = False
            meta["continuity_anchor_ready"] = False
            try:
                from app.media_pipeline.reference_store import upsert_reference_set_from_meta
                upsert_reference_set_from_meta(
                    shot_id=shot_id, version_id=version["id"], meta=meta, conn=conn,
                    static_ready=True, continuity_ready=False, group_gate_passed=False,
                )
            except Exception:  # noqa: BLE001
                pass
            set_pipeline_stage(
                job["id"], media_stages.STAGE_WAITING_CONTINUITY,
                reason_code="WAITING_CONTINUITY_ANCHOR",
                reason_text="参考图已备齐，等待上一镜尾帧",
                conn=conn,
            )
            _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
            conn.commit()
            raise _ContinuityWait("参考图已备齐，等待上一镜尾帧")

    # ── 第 1 次失败：记录原始失败原因并重试 1 次 ──
    if not assets:
        log_provider_call(
            "reference_image_mode_attempt_1_failed", config.MODEL_TEXT, "REFERENCE_ATTEMPT_FAILED",
            None, 0, meta={
                "shot_id": shot_id,
                "attempt": 1,
                "original_failure_reason": f"第 1 次参考图生成未产出可用资产（{len(rejection_details)} 张被拒绝）",
                "rejection_details": rejection_details[:5],
            })

        retry_rejection: list[dict[str, Any]] = []
        _delete_rejected_assets(rejected_assets)
        rejected_assets = []
        assets = await video_modes.build_reference_assets(
            conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
            shot_id=shot_id, shot=shot_model, bible=bible, decision=decision, prev_shot=prev_shot,
            rejection_details=retry_rejection, rejected_out=rejected_assets,
            on_progress=_persist_reference_progress,
            allow_missing_continuity_tail=needs_tail,
            job_id=job["id"],
            existing_meta=meta,
            screenplay=screenplay,
        )
        rejection_details.extend(retry_rejection)

        if assets:
            log_provider_call(
                "reference_image_mode_retry_success", config.MODEL_TEXT, "REFERENCE_RETRY_SUCCESS",
                None, 0, meta={"shot_id": shot_id, "attempt": 2, "count": len(assets)})
        else:
            log_provider_call(
                "reference_image_mode_retry_failed", config.MODEL_TEXT, "REFERENCE_RETRY_FAILED",
                None, 0, meta={
                    "shot_id": shot_id,
                    "attempt": 2,
                    "total_rejection_count": len(rejection_details),
                    "rejection_details": rejection_details[:10],
                    "original_failure_reason": f"参考图模式 2 次尝试均未产出可用资产（共 {len(rejection_details)} 张被拒绝）",
                })

    # ── 参考图模式成功 ──
    if assets:
        meta["mode_decision"] = video_modes.decision_to_dict(decision)
        _delete_rejected_assets(rejected_assets)
        meta["reference_images"] = video_modes.dedupe_reference_dicts(
            [a.public_dict() for a in assets]
        )
        meta["reference_generation_complete"] = True
        meta["reference_static_ready"] = True
        meta["continuity_anchor_ready"] = True
        if not _reference_keyframe_gate_passed(assets):
            _assert_reference_lease()
            meta["reference_gate_retry_exhausted"] = True
            meta["reference_group_gate_passed"] = False
            meta["video_input_manifest_frozen"] = False
            log_provider_call(
                "reference_keyframe_gate_repair_required",
                config.MODEL_TEXT,
                "REPAIR_REQUIRED",
                None,
                0,
                meta={
                    "shot_id": shot_id,
                    "mode": video_modes.REFERENCE_IMAGE_MODE,
                },
            )
            _set_version(
                version["id"],
                image_inputs=json.dumps(meta, ensure_ascii=False),
            )
            raise VideoInputRepairRequired(
                "人物谱或场景库参考图文件不可用"
            )
        meta["reference_group_gate_passed"] = True
        meta["video_input_manifest_frozen"] = True
        meta.pop("first_frame_path", None)
        meta.pop("last_frame_path", None)
        meta.pop("first_frame_scene_id", None)
        meta.pop("last_frame_scene_id", None)
        prompt_text = video_modes.append_reference_prompt_notes(
            prompt_text,
            assets,
            duration_s=shot_model.duration_s,
            required_identity_names=list(
                meta.get("required_reference_characters") or []
            ),
        )
        _assert_reference_lease()
        try:
            from app.media_pipeline.reference_store import upsert_reference_set_from_meta
            upsert_reference_set_from_meta(
                shot_id=shot_id, version_id=version["id"], meta=meta, conn=conn,
                static_ready=True, continuity_ready=True, group_gate_passed=True,
            )
        except Exception:  # noqa: BLE001 参考图集落库失败不阻断视频
            pass
        set_pipeline_stage(
            job["id"], media_stages.STAGE_VIDEO_READY,
            scheduler_lane=media_stages.LANE_VIDEO_READY, ready_at=now(), conn=conn,
        )
        _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
        conn.commit()
        return meta, prompt_text

    # ── 参考图模式两次均未得到文件：保留原模式并进入修复 ──
    _delete_rejected_assets(rejected_assets)
    ref_failure_reason = (
        f"参考图模式 2 次尝试均未产出可用资产 "
        f"（共 {len(rejection_details)} 张被拒绝）"
    )
    log_provider_call(
        "reference_image_mode_original_failure", config.MODEL_TEXT, "REFERENCE_MODE_ORIGINAL_FAILURE",
        None, 0, meta={
            "shot_id": shot_id,
            "original_failure_reason": ref_failure_reason,
            "rejection_count": len(rejection_details),
            "rejection_details": rejection_details[:10],
        })

    meta["reference_failure_logs"] = (meta.get("reference_failure_logs") or []) + [{
        "mode": video_modes.REFERENCE_IMAGE_MODE,
        "original_failure_reason": ref_failure_reason,
        "rejection_count": len(rejection_details),
        "rejection_details": rejection_details[:10],
        "prompt": prompt_text[:500],
    }]
    meta["reference_generation_complete"] = False
    meta["reference_static_ready"] = False
    meta["continuity_anchor_ready"] = False
    meta["reference_group_gate_passed"] = False
    meta["video_input_manifest_frozen"] = False
    meta["narrative_keyframe_missing"] = False
    meta["reference_gate_retry_exhausted"] = True
    meta["reference_images"] = []
    _set_version(
        version["id"],
        image_inputs=json.dumps(meta, ensure_ascii=False),
        prompt_text=prompt_text,
    )
    raise VideoInputRepairRequired(ref_failure_reason)

__all__ = [name for name in globals() if not name.startswith("__")]
