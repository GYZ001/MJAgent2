"""首帧模式与首末帧模式的视频输入准备（拆分自 ``run_job.py``）。

两个函数、逐行搬移未重写：``_prepare_first_frame_mode_inputs``（单帧驱动）与
``_prepare_first_last_mode_inputs``（首末帧双边界驱动，函数体本身较长，超过
``max_function_lines_python`` 默认阈值，已在 ``app/FILE_CONVENTIONS.toml`` 登记
基线）。二者共用 ``.input_boundary`` 的边界资产原语与 ``.job_state`` 的版本状态
写入，放在同一文件是因为它们是同一族「按边界帧驱动」的模式，被
``.input_video_mode`` 的 ``_prepare_planned_mode_inputs`` 按 ``planned_mode``
分流调用。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from app import config, hiagent, video_modes
from app.db import log_provider_call, now
from app.hiagent import ProviderError

from .enqueue import _load_shot_model, _row_value
from .authority import _assert_job_lease
from .fences import VideoInputRepairRequired
from .input_boundary import (
    _ContinuityWait,
    _load_boundary_asset,
    _normalize_boundary_pair,
    _persist_boundary_asset,
    _resolve_current_execution_plan,
)
from .job_state import _set_version


async def _prepare_first_frame_mode_inputs(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str,
) -> tuple[dict, str]:
    """Use the immediately previous adopted video's real tail as the sole frame input."""
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage
    from app.video_plan import AssetSource

    shot_plan = _resolve_current_execution_plan(
        conn, str(job["shot_id"]), meta,
    )
    if shot_plan is None:
        raise VideoInputRepairRequired("首帧计划已过期，需要重新规划")
    requirements = list(shot_plan.required_assets)
    if len(requirements) != 1 or requirements[0].role != "first_frame":
        raise VideoInputRepairRequired("首帧计划必须且只能声明一个 first_frame")
    first_req = requirements[0]
    if first_req.source != AssetSource.PREVIOUS_ADOPTED_TAIL:
        raise VideoInputRepairRequired("首帧必须来自紧邻上一镜采用视频的真实尾帧")
    source_shot_id = first_req.source_shot_id or shot_plan.depends_on_shot_id
    if not source_shot_id or source_shot_id != shot_plan.depends_on_shot_id:
        raise VideoInputRepairRequired("首帧来源镜头与视频计划依赖不一致")

    previous = conn.execute(
        "SELECT * FROM shots WHERE id=? AND episode_id=?",
        (source_shot_id, job["episode_id"]),
    ).fetchone()
    source_contract = video_modes.previous_tail_source_contract(conn, previous)
    if not source_contract:
        raise _ContinuityWait("等待上一镜采用后提取真实尾帧")
    fingerprint = hashlib.sha256(json.dumps({
        "shot_plan_id": shot_plan.shot_plan_id,
        "role": "first_frame",
        "source": first_req.source.value,
        "continuity_source": source_contract,
        "policy": "previous_video_tail_first_frame_v1",
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    cached = _load_boundary_asset(
        conn, shot_plan.shot_plan_id, "first_frame", fingerprint,
    )
    if cached:
        first_path = str(cached["path"])
    else:
        _assert_job_lease(job["id"], lease_owner)
        dest_dir = (
            config.PROJECTS_DIR / job["project_id"] / "episodes"
            / str(ep["episode_no"]) / "shots" / str(shot["shot_no"])
            / "boundaries"
        )
        asset = video_modes.previous_tail_reference_asset(
            conn, previous, dest_dir=dest_dir,
        )
        if not asset or not asset.path or not Path(asset.path).is_file():
            raise VideoInputRepairRequired("上一镜采用视频无法稳定抽取尾帧")
        first_path = str(asset.path)
        _persist_boundary_asset(
            conn,
            shot_plan=shot_plan,
            role="first_frame",
            source=first_req.source.value,
            source_revision_id=str(source_contract["adopted_version_id"]),
            source_shot_id=source_shot_id,
            source_adopted_version_id=str(source_contract["adopted_version_id"]),
            path=first_path,
            fingerprint=fingerprint,
            qa={
                "source_adopted_version_id": source_contract["adopted_version_id"],
                "extracted_from_previous_video": True,
            },
        )
        conn.commit()

    meta["first_frame_path"] = first_path
    meta["first_frame_source"] = AssetSource.PREVIOUS_ADOPTED_TAIL.value
    meta["first_frame_source_shot_id"] = source_shot_id
    meta["first_frame_fingerprint"] = fingerprint
    meta["upstream_adopted_video_revision"] = source_contract["adopted_version_id"]
    meta["reference_images"] = []
    meta.pop("last_frame_path", None)
    meta.pop("last_frame_url", None)
    meta.pop("video_input_url", None)
    meta["reference_generation_complete"] = True
    meta["video_input_manifest_frozen"] = True
    meta["plan_status"] = "ready"
    set_pipeline_stage(
        job["id"], media_stages.STAGE_VIDEO_READY,
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


async def _prepare_first_last_mode_inputs(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str,
) -> tuple[dict, str]:
    from app.schemas import Bible
    from app.video_plan import AssetSource
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    shot_plan = _resolve_current_execution_plan(
        conn, str(job["shot_id"]), meta,
    )
    if shot_plan is None:
        raise VideoInputRepairRequired("首尾帧计划已过期，需要重新规划")
    current_first = str(meta.get("first_frame_path") or "")
    current_last = str(meta.get("last_frame_path") or "")
    if current_first and current_last and Path(current_first).is_file() and Path(current_last).is_file():
        return meta, prompt_text

    project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    from app.portraits import bible_for_episode
    bible = bible_for_episode(job["project_id"], bible, ep["episode_no"])
    screenplay = None
    if _row_value(ep, "id") or _row_value(ep, "screenplay_json"):
        from app.production.screenplay_authority import resolve_downstream_screenplay

        screenplay = resolve_downstream_screenplay(
            job["episode_id"], conn=conn,
        ).screenplay
    shot_model = _load_shot_model(shot)
    from app.continuity import apply_shot_contract
    apply_shot_contract(shot_model, meta.get("shot_contract_json"))
    requirements = {item.role: item for item in shot_plan.required_assets}
    first_req = requirements.get("first_frame")
    last_req = requirements.get("last_frame")
    if not first_req or not last_req:
        raise ProviderError("首尾帧计划缺少 first_frame 或 last_frame 素材合同")
    plan_relations = getattr(shot_plan, "relations", None)
    boundary_prompt_contract = (
        meta.get("boundary_prompt_contract")
        if isinstance(meta.get("boundary_prompt_contract"), dict)
        else {}
    )
    relation_edit = str(
        boundary_prompt_contract.get("relation_edit")
        or getattr(plan_relations, "edit", "unknown")
        or "unknown"
    )
    relation_action = str(
        boundary_prompt_contract.get("relation_action")
        or getattr(plan_relations, "action", "unknown")
        or "unknown"
    )
    from app.multiview import keyframe_seed_paths, resolve_shot_asset_dependencies

    manifest = resolve_shot_asset_dependencies(
        project_id=job["project_id"],
        episode_no=ep["episode_no"],
        shot_id=job["shot_id"],
        shot=shot_model,
        scene_name=shot_model.scene_name or None,
        conn=conn,
        bible=bible,
        screenplay=screenplay,
    )
    boundary_seed_inputs = []
    for seed_path in keyframe_seed_paths(manifest):
        try:
            boundary_seed_inputs.append(hiagent.data_url_from_file(seed_path))
        except OSError:
            continue
    if not boundary_seed_inputs:
        from app.continuity import effective_characters_visible
        boundary_seed_inputs = video_modes._portrait_seed_inputs(
            bible,
            effective_characters_visible(shot_model),
            project_id=job["project_id"],
            episode_no=ep["episode_no"],
        )

    set_pipeline_stage(
        job["id"], media_stages.STAGE_REFERENCE_GENERATE,
        reason_code="PREFETCHING_STATIC_TAIL",
        reason_text="正在预生成可供下一镜复用的静态尾帧",
        conn=conn,
    )
    conn.commit()

    async def _resolve(
        role: str,
        requirement,
        description: str,
        index: int,
        *,
        pair_attempt: int,
        seed_inputs: list[str],
        pair_start_fingerprint: str | None = None,
    ) -> str:
        _assert_job_lease(job["id"], lease_owner)
        source_revision = str(requirement.asset_revision_id or shot_plan.source_storyboard_revision_id)
        upstream_version_id = None
        source_shot_id = requirement.source_shot_id or shot_plan.depends_on_shot_id
        upstream_static_asset = None
        fingerprint_material: dict[str, Any] = {
            "shot_plan_id": shot_plan.shot_plan_id,
            "role": role,
            "source": requirement.source.value,
            "source_revision": source_revision,
            "description": description,
            "boundary_contract": "shared_static_tail_v3",
            "generation_attempt": (
                pair_attempt
                if requirement.source == AssetSource.STATIC_BOUNDARY_ASSET
                else 1
            ),
        }
        if pair_start_fingerprint:
            fingerprint_material["pair_start_fingerprint"] = pair_start_fingerprint
        if requirement.source == AssetSource.PREVIOUS_ADOPTED_TAIL:
            previous = conn.execute(
                "SELECT * FROM shots WHERE id=? AND episode_id=?",
                (source_shot_id, job["episode_id"]),
            ).fetchone()
            upstream_version_id = previous["adopted_version_id"] if previous else None
            if not previous or not upstream_version_id:
                raise _ContinuityWait("等待上一镜采用后提取真实尾帧")
            fingerprint_material["upstream_adopted_version_id"] = upstream_version_id
        elif requirement.source == AssetSource.PREVIOUS_STATIC_TAIL:
            source_plan = conn.execute(
                """SELECT id FROM shot_video_generation_plans
                   WHERE episode_video_plan_id=? AND shot_id=?""",
                (shot_plan.episode_video_plan_id, source_shot_id),
            ).fetchone()
            if source_plan:
                upstream_static_asset = conn.execute(
                    """SELECT * FROM video_boundary_assets
                       WHERE episode_video_plan_id=? AND shot_plan_id=?
                         AND role='last_frame' AND qa_status='passed'
                       ORDER BY created_at DESC LIMIT 1""",
                    (shot_plan.episode_video_plan_id, source_plan["id"]),
                ).fetchone()
            if (
                not upstream_static_asset
                or not upstream_static_asset["path"]
                or not Path(upstream_static_asset["path"]).is_file()
            ):
                source_job = conn.execute(
                    """SELECT status FROM jobs
                       WHERE episode_id=? AND shot_id=? AND kind='video'
                       ORDER BY created_at DESC LIMIT 1""",
                    (job["episode_id"], source_shot_id),
                ).fetchone()
                if source_job and source_job["status"] in {
                    "failed", "waiting_human", "cancelled", "paused",
                }:
                    raise VideoInputRepairRequired(
                        "上一镜静态尾帧未生成成功，需要保持首尾帧模式修复该边界素材"
                    )
                raise _ContinuityWait(
                    "等待上一镜预生成静态尾帧",
                    reason_code="WAITING_STATIC_BOUNDARY_ASSET",
                )
            fingerprint_material.update({
                "upstream_boundary_fingerprint": upstream_static_asset["fingerprint"],
                "upstream_boundary_sha256": upstream_static_asset["sha256"],
            })
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_material, ensure_ascii=False, sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cached = _load_boundary_asset(conn, shot_plan.shot_plan_id, role, fingerprint)
        if cached:
            return str(cached["path"])

        if requirement.source == AssetSource.PREVIOUS_ADOPTED_TAIL:
            dest_dir = (
                config.PROJECTS_DIR / job["project_id"] / "episodes"
                / str(ep["episode_no"]) / "shots" / str(shot["shot_no"]) / "boundaries"
            )
            asset = video_modes.previous_tail_reference_asset(
                conn, previous, dest_dir=dest_dir,
            )
            if not asset or not asset.path:
                raise VideoInputRepairRequired("上一镜采用视频无法稳定抽取尾帧")
            asset.qa = {**(asset.qa or {}), "source_adopted_version_id": upstream_version_id}
        elif requirement.source == AssetSource.PREVIOUS_STATIC_TAIL:
            source_path = Path(str(upstream_static_asset["path"]))
            dest_dir = (
                config.PROJECTS_DIR / job["project_id"] / "episodes"
                / str(ep["episode_no"]) / "shots" / str(shot["shot_no"])
                / "boundaries"
            )
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / (
                f"first-from-{source_shot_id}-"
                f"{str(upstream_static_asset['sha256'])[:12]}"
                f"{source_path.suffix or '.jpg'}"
            )
            if not dest_path.is_file():
                shutil.copy2(source_path, dest_path)
            asset = video_modes.ReferenceImageAsset(
                id=f"{shot_plan.shot_plan_id}:first_frame",
                url="",
                type="plot_key_frame",
                source="previous_static_tail",
                path=str(dest_path),
                qa={
                    "source_boundary_asset_id": upstream_static_asset["id"],
                    "source_boundary_fingerprint": upstream_static_asset["fingerprint"],
                    "shared_without_regeneration": True,
                },
            )
        elif requirement.source == AssetSource.STATIC_BOUNDARY_ASSET:
            if role == "last_frame":
                boundary_instruction = (
                    "STATIC TAIL PREFETCH: render only this shot's contracted final "
                    "state. This immutable image is also the next shot's exact first "
                    "frame when a next shot exists. Preserve every named identity, "
                    "outfit, environment, fixed landmark, screen direction, and scale "
                    "from the supplied character/scene truth anchors. Do not blend "
                    "endpoints, morph faces, merge people, teleport, hard-cut, or add "
                    "uncontracted people. The current shot's dynamic first frame may "
                    "not be available yet; do not wait for it or invent a start pose. "
                    f"Edit relation: {relation_edit}; "
                    f"action relation: {relation_action}."
                )
            else:
                boundary_instruction = (
                    "STATIC FIRST BOUNDARY: render only the contracted starting state "
                    "for this shot. Preserve every named identity, outfit, environment, "
                    "fixed landmark, screen direction, and scale from the supplied "
                    "character/scene truth anchors. Do not render the final action state."
                )
            asset = await video_modes._generate_one_reference(
                project_id=job["project_id"],
                episode_no=ep["episode_no"],
                shot=shot_model,
                bible=bible,
                ref_type="plot_key_frame",
                index=index + pair_attempt - 1,
                content_override=description,
                seed_inputs=seed_inputs,
                extra_instruction=boundary_instruction,
                skip_inline_qa=False,
                screenplay=screenplay,
            )
            if not asset.path or not Path(asset.path).is_file():
                raise VideoInputRepairRequired(f"{role} 边界帧生成后文件不可用")
            if not asset.selectedForSeedance:
                raise VideoInputRepairRequired(f"{role} 边界帧未通过生成前质量门禁")
        else:
            raise VideoInputRepairRequired(
                f"{role} 使用了不支持的素材来源：{requirement.source.value}"
            )
        qa = dict(asset.qa or {})
        _persist_boundary_asset(
            conn,
            shot_plan=shot_plan,
            role=role,
            source=requirement.source.value,
            source_revision_id=source_revision,
            source_shot_id=source_shot_id,
            source_adopted_version_id=upstream_version_id,
            path=str(asset.path),
            fingerprint=fingerprint,
            qa=qa,
        )
        conn.commit()
        return str(asset.path)

    # The tail is an independently frozen narrative boundary. Generate it before
    # resolving the dynamic first-frame dependency so the next shot can consume
    # it even while this shot is still waiting for an adopted/static upstream.
    tail_attempt_limit = max(1, min(3, int(shot_plan.max_attempts or 1)))
    tail_seed_inputs = list(dict.fromkeys(boundary_seed_inputs[:4]))
    last_path = ""
    first_size = (0, 0)
    last_repair_error = ""
    for tail_attempt in range(1, tail_attempt_limit + 1):
        try:
            last_path = await _resolve(
                "last_frame",
                last_req,
                shot_model.last_frame_desc,
                902,
                pair_attempt=tail_attempt,
                seed_inputs=tail_seed_inputs,
            )
        except VideoInputRepairRequired as exc:
            last_repair_error = str(exc)
            log_provider_call(
                "last_frame_same_mode_repair",
                config.MODEL_IMAGE,
                "REPAIRING",
                None,
                0,
                meta={
                    "shot_id": job["shot_id"],
                    "shot_plan_id": shot_plan.shot_plan_id,
                    "tail_attempt": tail_attempt,
                    "tail_attempt_limit": tail_attempt_limit,
                    "reason": last_repair_error,
                },
            )
            continue
        break
    else:
        raise VideoInputRepairRequired(
            f"{last_repair_error or '尾帧输入准备未通过'}；"
            f"已在 FIRST_LAST_FRAME_MODE 内修复 {tail_attempt_limit} 次，"
            "未更改生成模式"
        )

    meta["last_frame_path"] = last_path
    meta["boundary_tail_prefetched"] = True
    meta["boundary_tail_prefetched_at"] = now()
    meta["reference_generation_complete"] = False
    _set_version(
        version["id"],
        image_inputs=json.dumps(meta, ensure_ascii=False),
    )
    conn.commit()

    first_path = await _resolve(
        "first_frame",
        first_req,
        shot_model.first_frame_desc,
        901,
        pair_attempt=1,
        seed_inputs=boundary_seed_inputs,
    )
    first_bytes = Path(first_path).read_bytes()
    first_fingerprint = hashlib.sha256(first_bytes).hexdigest()

    while True:
        try:
            first_path, last_path, first_size = _normalize_boundary_pair(
                first_path, last_path,
            )
            break
        except VideoInputRepairRequired as exc:
            if tail_attempt >= tail_attempt_limit:
                raise
            conn.execute(
                """UPDATE video_boundary_assets
                      SET qa_status='failed'
                    WHERE shot_plan_id=? AND role='last_frame' AND path=?""",
                (shot_plan.shot_plan_id, last_path),
            )
            conn.commit()
            tail_attempt += 1
            log_provider_call(
                "last_frame_dimension_repair",
                config.MODEL_IMAGE,
                "REPAIRING",
                None,
                0,
                meta={
                    "shot_id": job["shot_id"],
                    "shot_plan_id": shot_plan.shot_plan_id,
                    "tail_attempt": tail_attempt,
                    "tail_attempt_limit": tail_attempt_limit,
                    "reason": str(exc),
                },
            )
            last_path = await _resolve(
                "last_frame",
                last_req,
                shot_model.last_frame_desc,
                902,
                pair_attempt=tail_attempt,
                seed_inputs=tail_seed_inputs,
            )
    for role, path in (
        ("first_frame", first_path),
        ("last_frame", last_path),
    ):
        raw = Path(path).read_bytes()
        conn.execute(
            """UPDATE video_boundary_assets
                  SET path=?,sha256=?,width=?,height=?
                WHERE shot_plan_id=? AND role=? AND path=?""",
            (
                path,
                hashlib.sha256(raw).hexdigest(),
                first_size[0],
                first_size[1],
                shot_plan.shot_plan_id,
                role,
                path,
            ),
        )
    boundary_contract = {
        "status": "deterministic_checks_only",
        "semantic_pair_review_performed": False,
        "first_frame_source": first_req.source.value,
        "last_frame_source": last_req.source.value,
        "shared_boundary_contract": "shared_static_tail_v3",
        "camera_bridge_contract": "continuous_endpoint_bridge_v2",
        "tail_conditioned_on_first_frame": False,
        "tail_prefetched_before_first_frame": True,
        "first_frame_sha256": first_fingerprint,
        "relation_edit": relation_edit,
        "relation_action": relation_action,
    }
    meta["first_frame_path"] = first_path
    meta["last_frame_path"] = last_path
    meta["reference_images"] = []
    meta.pop("video_input_url", None)
    meta["boundary_frame_dimensions"] = list(first_size)
    meta["boundary_pair_qa"] = boundary_contract
    meta["camera_bridge_contract"] = "continuous_endpoint_bridge_v2"
    meta["reference_generation_complete"] = True
    meta["video_input_manifest_frozen"] = True
    meta["plan_status"] = "ready"
    set_pipeline_stage(
        job["id"], media_stages.STAGE_VIDEO_READY,
        scheduler_lane=media_stages.LANE_VIDEO_READY, ready_at=now(), conn=conn,
    )
    _set_version(
        version["id"], image_inputs=json.dumps(meta, ensure_ascii=False),
        prompt_text=prompt_text,
    )
    conn.commit()
    return meta, prompt_text

__all__ = [name for name in globals() if not name.startswith("__")]
