"""视频输入模式的总分流与 AI 视频 prompt 生成（拆分自 ``run_job.py``）。

``_prepare_video_input_mode`` 是通用（非计划模式）视频输入准备的分流入口；
``_ensure_ai_video_prompt`` 在 prompt 缺失时补一次模型调用；
``_prepare_planned_mode_inputs`` 按 ``planned_mode`` 把请求路由到
``.input_reference``/``.input_first_frame_last`` 里对应的模式专属准备函数——是
四个 ``_prepare_*_mode_inputs`` 之间唯一互相调用的一个，天然放在依赖它们的这
一层。
"""

from __future__ import annotations

import json
from pathlib import Path

from app import hiagent, video_modes
from app.db import get_conn, now
from app.hiagent import ProviderError

from .authority import _assert_job_lease, _assert_video_provider_submission_authority_async
from .enqueue import _load_shot_model
from .fences import VideoInputRepairRequired
from .input_boundary import _ContinuityWait, _resolve_current_execution_plan
from .input_first_frame_last import (
    _prepare_first_frame_mode_inputs,
    _prepare_first_last_mode_inputs,
)
from .input_reference import _prepare_reference_mode_inputs
from .job_state import _set_version


async def _prepare_video_input_mode(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str,
) -> tuple[dict, str]:
    from app.video_plan import (
        ProviderMediaPublicationService,
        VideoGenerationMode,
        capability_allows,
        current_capability_snapshot,
    )
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    shot_plan = _resolve_current_execution_plan(
        conn, str(job["shot_id"]), meta,
    )
    if shot_plan is None:
        raise VideoInputRepairRequired("视频输入计划已过期，需要重新规划")
    snapshot = current_capability_snapshot(
        provider=None, model=None, conn=conn,
    )
    if snapshot.id != shot_plan.capability_snapshot_id or not capability_allows(
        snapshot, VideoGenerationMode.VIDEO_INPUT_MODE, shot_plan.video_input_intent,
    ):
        raise VideoInputRepairRequired("当前能力快照未准入该视频输入意图")
    upstream_id = shot_plan.depends_on_shot_id
    previous = conn.execute(
        "SELECT * FROM shots WHERE id=? AND episode_id=?",
        (upstream_id, job["episode_id"]),
    ).fetchone()
    adopted_id = previous["adopted_version_id"] if previous else None
    if not previous or not adopted_id:
        raise _ContinuityWait("等待上一镜采用后绑定真实参考视频")
    adopted = conn.execute(
        """SELECT * FROM shot_versions
           WHERE id=? AND shot_id=? AND status='succeeded' AND video_path IS NOT NULL""",
        (adopted_id, upstream_id),
    ).fetchone()
    if not adopted or not Path(adopted["video_path"]).is_file():
        raise _ContinuityWait("上一镜采用视频尚不可读取")

    existing = conn.execute(
        """SELECT * FROM provider_media_publications
           WHERE source_revision_id=? AND status='ready' AND url_expires_at>?
           ORDER BY created_at DESC LIMIT 1""",
        (adopted_id, now() + 1800),
    ).fetchone()
    if existing:
        publication = {
            "id": existing["id"],
            "published_url": existing["published_url"],
            "sha256": existing["sha256"],
            "url_expires_at": existing["url_expires_at"],
        }
    else:
        try:
            adopted_meta = json.loads(adopted["image_inputs"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            adopted_meta = {}
        source_url = str(adopted_meta.get("provider_video_source_url") or "")
        source_expiry = float(
            adopted_meta.get("provider_video_source_url_expires_at") or 0
        )
        publication = None
        if source_url and source_expiry > now() + 1800:
            try:
                publication = await ProviderMediaPublicationService().publish(
                    source_revision_id=adopted_id,
                    source_url=source_url,
                    expires_at=source_expiry,
                    conn=conn,
                )
            except Exception as exc:  # noqa: BLE001 - expired source falls back to owned storage
                meta["provider_source_url_reuse_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )[:500]
        if publication is None:
            publication = await ProviderMediaPublicationService().publish(
                source_revision_id=adopted_id,
                local_path=adopted["video_path"],
                conn=conn,
            )
    _assert_job_lease(job["id"], lease_owner)
    meta["video_input_url"] = publication["published_url"]
    meta["provider_media_publication_id"] = publication["id"]
    meta["upstream_adopted_video_revision"] = adopted_id
    meta["video_input_fingerprint"] = publication["sha256"]
    meta["reference_images"] = []
    meta.pop("first_frame_path", None)
    meta.pop("last_frame_path", None)
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


async def _ensure_ai_video_prompt(
    conn,
    job,
    version,
    shot,
    ep,
    meta: dict,
    prompt_text: str,
) -> tuple[dict, str]:
    """Generate the creative provider prompt once, before preparing video inputs."""
    conn = conn or get_conn()
    if not meta.get("ai_video_prompt_required"):
        return meta, prompt_text

    from app.video_prompt_ai import (
        AI_VIDEO_PROMPT_CONTRACT_VERSION,
        generate_ai_video_prompt,
    )
    from app.video_prompt_profiles import resolve_video_prompt_profile

    target_provider = hiagent.active_provider("video")
    target_model = hiagent.active_model("video", target_provider)
    target_profile = resolve_video_prompt_profile(
        provider=target_provider,
        model=target_model,
    )

    if (
        meta.get("ai_video_prompt_contract_version")
        == AI_VIDEO_PROMPT_CONTRACT_VERSION
        and meta.get("ai_video_prompt_profile_id") == target_profile.profile_id
        and meta.get("ai_video_prompt_profile_version") == target_profile.version
        and meta.get("ai_video_prompt_target_provider") == target_provider
        and meta.get("ai_video_prompt_target_model") == target_model
        and isinstance(meta.get("ai_video_prompt_draft"), dict)
        and str(meta.get("ai_video_prompt_base") or "").strip()
    ):
        return meta, prompt_text

    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage
    from app.schemas import Bible

    set_pipeline_stage(
        job["id"],
        media_stages.STAGE_VIDEO_PROMPT,
        conn=conn,
    )
    conn.commit()

    project = conn.execute(
        "SELECT * FROM projects WHERE id=?",
        (job["project_id"],),
    ).fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    from app.portraits import bible_for_episode

    bible = bible_for_episode(job["project_id"], bible, ep["episode_no"])
    shot_model = _load_shot_model(shot)
    from app.continuity import apply_shot_contract

    apply_shot_contract(shot_model, meta.get("shot_contract_json"))
    continuity_contract = str(
        meta.get("continuity_contract_prompt") or prompt_text
    ).strip()
    prompt, draft = await generate_ai_video_prompt(
        shot=shot_model,
        bible=bible,
        continuity_contract=continuity_contract,
        video_generation_mode=str(
            meta.get("planned_mode")
            or meta.get("mode")
            or video_modes.REFERENCE_IMAGE_MODE
        ),
        operation_scope=str(version["id"]),
        target_provider=target_provider,
        target_model=target_model,
        user_instruction=str(meta.get("prompt_user_instruction") or ""),
        critique=[
            str(item).strip()
            for item in (meta.get("prompt_critique") or [])
            if str(item).strip()
        ],
    )
    meta["continuity_contract_prompt"] = continuity_contract
    meta["ai_video_prompt_contract_version"] = (
        AI_VIDEO_PROMPT_CONTRACT_VERSION
    )
    meta["ai_video_prompt_profile_id"] = target_profile.profile_id
    meta["ai_video_prompt_profile_version"] = target_profile.version
    meta["ai_video_prompt_target_provider"] = target_provider
    meta["ai_video_prompt_target_model"] = target_model
    meta["ai_video_prompt_draft"] = draft.model_dump(mode="json")
    meta["ai_video_prompt_base"] = prompt
    meta["ai_video_prompt_generated_at"] = now()
    bible_character_names = {item.name for item in bible.characters}
    meta["required_reference_characters"] = [
        name
        for name in draft.visible_characters
        if name in bible_character_names
    ]
    if draft.interaction_kind == "person_person_contact":
        meta["required_interaction_reference_characters"] = [
            name
            for name in draft.interaction_participants
            if name in bible_character_names
        ]
    else:
        meta.pop("required_interaction_reference_characters", None)
    _set_version(
        version["id"],
        image_inputs=json.dumps(meta, ensure_ascii=False),
        prompt_text=prompt,
    )
    conn.commit()
    return meta, prompt


async def _prepare_planned_mode_inputs(
    conn, job, version, shot, ep, meta: dict, prompt_text: str,
    *, lease_owner: str,
) -> tuple[dict, str]:
    conn = conn or get_conn()
    mode = meta.get("mode") or video_modes.REFERENCE_IMAGE_MODE
    # Reference/keyframe generation, boundary-frame generation, and provider
    # media publication can all incur external work before the final video
    # submit.  One mode-neutral authority fence must therefore run before mode
    # dispatch, not merely before create_video_task.
    selected_plan = await _assert_video_provider_submission_authority_async(
        conn=conn,
        job=job,
        meta=meta,
        actual_mode=str(mode),
        write_point="planned_mode_input_prepare",
    )
    if (
        selected_plan is not None
        and selected_plan.shot_plan_id != meta.get("shot_plan_id")
    ):
        # A fallback/local replan publishes a new episode revision. Unchanged
        # sibling contracts remain executable, but every persisted identity
        # must be rebound before preparing assets or recording attempts.
        meta["submitted_shot_plan_id"] = meta.get("shot_plan_id")
        meta["submitted_episode_video_plan_id"] = meta.get(
            "episode_video_plan_id"
        )
        meta.update({
            "shot_plan_id": selected_plan.shot_plan_id,
            "episode_video_plan_id": selected_plan.episode_video_plan_id,
            "plan_revision": selected_plan.plan_revision,
            "source_storyboard_revision_id": (
                selected_plan.source_storyboard_revision_id
            ),
            "capability_snapshot_id": selected_plan.capability_snapshot_id,
            "input_revision_fingerprints": dict(
                selected_plan.input_revision_fingerprints
            ),
            "planned_mode": selected_plan.mode.value,
            "actual_mode": selected_plan.mode.value,
            "video_input_intent": (
                selected_plan.video_input_intent.value
                if selected_plan.video_input_intent is not None
                else None
            ),
            "depends_on_shot_id": selected_plan.depends_on_shot_id,
            "stale_plan_recovered": True,
            "stale_plan_recovered_at": now(),
        })
        _set_version(
            version["id"],
            image_inputs=json.dumps(meta, ensure_ascii=False),
        )
    if mode == video_modes.REFERENCE_IMAGE_MODE:
        return await _prepare_reference_mode_inputs(
            conn, job, version, shot, ep, meta, prompt_text,
            lease_owner=lease_owner,
        )
    if mode == video_modes.FIRST_FRAME_MODE:
        return await _prepare_first_frame_mode_inputs(
            conn, job, version, shot, ep, meta, prompt_text,
            lease_owner=lease_owner,
        )
    if mode == video_modes.FIRST_LAST_FRAME_MODE:
        return await _prepare_first_last_mode_inputs(
            conn, job, version, shot, ep, meta, prompt_text,
            lease_owner=lease_owner,
        )
    if mode == video_modes.VIDEO_INPUT_MODE:
        return await _prepare_video_input_mode(
            conn, job, version, shot, ep, meta, prompt_text,
            lease_owner=lease_owner,
        )
    raise ProviderError(f"未知视频生成模式：{mode}")

__all__ = [name for name in globals() if not name.startswith("__")]
