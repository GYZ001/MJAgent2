"""参考图模式两次生成尝试后仍无可用资产的落点判定（拆分自 ``input_reference.py``）。

``_prepare_reference_mode_inputs`` 两次 ``build_reference_assets`` 都拿到空结果时，
过去无条件判成真实故障、把镜头钉在 ``waiting_human``。但「空」有两种：候选池
本来就是空的（群演/一次性人物没有定妆照、镜头没有 scene_name——这是设计使然，
不是漏抽）；和候选池本该有东西却没有（人物/场景该有的造型版本缺失、或生成后
被判不合格）。前者应当退化为纯文本继续出片，后者必须维持原有拦截。

判据挂在 ``resolve_shot_asset_dependencies`` 已经产出的 manifest 上（每个人物/
场景条目自带 ``asset_required``），不新发明规则、不按角色名单枚举：
``app.multiview.assert_manifest_allows_production`` 已经是这套「谁本该有资产却
没有」的唯一权威判据（``manifest_production_blockers`` 的薄封装），本模块直接
复用。``rejection_details`` 非空（VLM 一致性质检已下线，当前恒为空，见
``app.video_modes.reference_assemble._enforce_reference_consistency``）时同样视为
真实故障，为未来可能复活的质检机制预留判据，不假设它永远是空的。

WS3a（docs/failure_triage_and_self_heal_plan_2026-09-05.md）：候选池「本该有
资产却没有」不再直接拦成待人工——按 blockers 指向的人物/场景各自动触发一次
补生成（复用 ``app.refs.generate_refs``/``app.scenes.generate_scene_refs`` 既
有入口，不复制生成逻辑），成功后重新解析依赖再装配；仍缺才落回原有的
``waiting_human`` 拦截。每个镜头只自愈一次，标记写在 job/version 的
``meta["reference_self_heal_attempted"]``，日志前缀 ``[REFERENCE_SELF_HEAL]``。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import config, video_modes
from app.db import log_provider_call, now

from .authority import _assert_job_lease
from .fences import VideoInputRepairRequired
from .job_state import _set_version

_LOGGER = logging.getLogger(__name__)

TEXT_ONLY_FALLBACK_NOTE_MARKER = "[TEXT_ONLY_NO_REFERENCE_IMAGES]"


def _reference_pool_blockers(manifest: dict[str, Any] | None) -> list[str]:
    """候选池是否本该有东西——直接复用已有的 manifest 产物信号，不新发明规则。

    manifest 缺失（连依赖都没解析出来）按最严处理，视为有缺口，不当空集合放行
    （CLAUDE.md「空集合不等于无需检查」）。
    """
    from app.multiview import assert_manifest_allows_production

    if not isinstance(manifest, dict):
        return ["参考图依赖 manifest 缺失"]
    return assert_manifest_allows_production(manifest)


def _reference_repair_guidance(blockers: list[str]) -> str:
    """拦住用户时必须给出路：明确缺口与修复入口，不留「核对状态」式空话。

    ``blockers`` 非空分支的文案与分镜台的资源警告
    （``app.validators.resource_forecast.shot_resource_advisories``）共用
    ``app.validators.resource_forecast.blocked_consequence_text`` 这一份来源，
    避免两处各说各话（WS8-A）。
    """
    if blockers:
        from app.validators.resource_forecast import blocked_consequence_text

        return blocked_consequence_text(blockers)
    return "请到「人物谱」或「场景库」重新生成对应定妆照/场景图后重试。"


def _release_rejected_reference_assets(
    *, job: Any, lease_owner: str | None, rejected_assets: list[Any],
) -> None:
    """删除本轮尝试中被淘汰的候选文件；有租约先续租再删，避免误删新尝试的文件。"""
    if lease_owner is not None:
        _assert_job_lease(job["id"], lease_owner)
    from app.rejected_media import discard_file

    for asset in rejected_assets:
        discard_file(getattr(asset, "path", None))
        asset.path = None
        asset.url = None


def _available_appearance_notes(
    *, shot_model: Any, visible_names: list[str], bible: Any, screenplay: Any,
) -> list[str]:
    """群演/一次性人物的外观描述——原样取自已落盘数据，不新编、不兜底填充。

    两个互不重叠的数据源，谁在场读谁：分镜台 2.0.0 的逐镜资源解析
    （``resources.characters[].description``，本来就是无定妆照身份的专属字段）
    覆盖当前投产的主链路；narrative_plan 身份合同的 ``visual_canonical`` 覆盖仍
    在用旧规划管线的集。两边都没有时诚实返回空列表。
    """
    notes: list[str] = []
    segment = getattr(shot_model, "storyboard_pack_segment", None) or {}
    for entry in (segment.get("resources") or {}).get("characters") or []:
        if entry.get("portrait_id"):
            continue
        description = str(entry.get("description") or "").strip()
        identity_id = str(entry.get("identity_id") or "").strip()
        if description and identity_id:
            notes.append(f"「{identity_id}」外观：{description}")
    if notes or screenplay is None or getattr(screenplay, "narrative_plan", None) is None:
        return notes
    from app.identity_contracts import IdentityContractError, narrative_identity_resolver

    resolver = narrative_identity_resolver(bible, screenplay)
    for name in visible_names:
        try:
            identity = resolver.resolve(name, usage="reference")
        except IdentityContractError:
            continue
        if identity.requires_asset or identity.visual_policy == "offscreen_only":
            continue
        anchor = identity.visual_canonical.strip()
        if anchor:
            notes.append(f"「{identity.display_name}」外观：{anchor}")
    return notes


def _subjective_pov_note(*, visible_names: list[str], bible: Any, screenplay: Any) -> str | None:
    """本镜可见角色若全部是「只准画外出现」的叙事身份，返回主观镜头指令。

    只认一个类型化字段：``NarrativeIdentityContract.visual_policy ==
    "offscreen_only"``（由叙事规划层依 ``voice_bible.role_type=="narrator"``
    生成，见 ``app.identity_contracts``），从不在这里按 entity id / 角色名猜。
    没有 ``narrative_plan`` 的集（例如本集所在的分镜台 2.0.0 管线）目前没有这个
    类型化字段——宁可不判、也不猜，返回 None。
    """
    if not visible_names or screenplay is None or screenplay.narrative_plan is None:
        return None
    from app.identity_contracts import IdentityContractError, narrative_identity_resolver

    resolver = narrative_identity_resolver(bible, screenplay)
    try:
        identities = [resolver.resolve(name, usage="reference") for name in visible_names]
    except IdentityContractError:
        return None
    if any(identity.visual_policy != "offscreen_only" for identity in identities):
        return None
    who = "、".join(dict.fromkeys(identity.display_name for identity in identities))
    return (
        f"本镜可见角色仅为主观视角身份「{who}」：摄像机即其双眼，"
        f"画面不得出现「{who}」本人的正面、侧面、背影或任何身体部位，"
        "只呈现其视线所见的对象、环境，以及互动对象朝向镜头方向的反应。"
    )


def _append_text_only_reference_notes(
    prompt_text: str, notes: list[str], *, duration_s: float | int | None,
) -> str:
    """把可用的外观/主观视角提示追加进提示词；没有笔记时原样返回，不制造空噪音。"""
    if not notes or TEXT_ONLY_FALLBACK_NOTE_MARKER in prompt_text:
        return prompt_text
    from app.compiler import _split_video_args

    prompt_body, prompt_args = _split_video_args(prompt_text, duration_s)
    note = TEXT_ONLY_FALLBACK_NOTE_MARKER + " " + " ".join(notes)
    return prompt_body + " " + note + prompt_args


def _episode_no_for_job(conn: Any, job: Any) -> int | None:
    """job 只带 episode_id；按 episode_no 查询的几处调用统一走这一份。"""
    row = conn.execute(
        "SELECT episode_no FROM episodes WHERE id=?", (job["episode_id"],),
    ).fetchone()
    return row["episode_no"] if row else None


def _time_anchor_advisories_for_job(conn: Any, job: Any, shot_model: Any) -> list[str]:
    """WS9：本镜命中的时间线锚点若与当前实际选用造型不符，取一份 ``[未拦截]``
    告警，记进 image_inputs meta——不影响候选池是否为空的既有判定，只是把
    ``app.validators.resource_forecast.character_time_anchor_advisories`` 的
    结论顺带落一份到本次纯文本回退的产物里，供分镜台/生成台展示。"""
    from app.validators.resource_forecast import character_time_anchor_advisories

    return character_time_anchor_advisories(
        shot=shot_model, project_id=job["project_id"],
        episode_no=_episode_no_for_job(conn, job), conn=conn,
    )


async def _complete_reference_mode_as_text_only(
    *, conn: Any, job: Any, meta: dict[str, Any], prompt_text: str, version: Any,
    shot_model: Any, bible: Any, screenplay: Any, decision: Any,
) -> tuple[dict, str]:
    """候选池本来就是空的：不再等人工，退化为纯文本继续出片。

    metadata 如实标注未使用参考图（``reference_images=[]`` 且显式
    ``reference_mode_text_only_fallback``），避免界面撒谎；``reference_static_
    ready`` 必须留 False——置 True 会被 ``dispatch.py`` 的
    ``static_waiting`` 判据误读成「静态图已备、只等尾帧」，把刚设好的
    ``STAGE_VIDEO_READY`` 重新打回等待态。
    """
    from app.continuity import effective_characters_visible
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    visible_names = effective_characters_visible(shot_model)
    notes = _available_appearance_notes(
        shot_model=shot_model, visible_names=visible_names, bible=bible, screenplay=screenplay,
    )
    pov_note = _subjective_pov_note(visible_names=visible_names, bible=bible, screenplay=screenplay)
    if pov_note:
        notes = [pov_note, *notes]
    prompt_text = _append_text_only_reference_notes(
        prompt_text, notes, duration_s=shot_model.duration_s,
    )
    meta["mode_decision"] = video_modes.decision_to_dict(decision)
    meta["reference_images"] = []
    meta["reference_generation_complete"] = True
    meta["reference_static_ready"] = False
    meta["continuity_anchor_ready"] = False
    meta["reference_group_gate_passed"] = True
    meta["video_input_manifest_frozen"] = True
    meta["reference_mode_text_only_fallback"] = True
    meta["reference_mode_text_only_reason"] = "empty_candidate_pool"
    meta["portrait_time_anchor_advisories"] = _time_anchor_advisories_for_job(conn, job, shot_model)
    meta.pop("first_frame_path", None)
    meta.pop("last_frame_path", None)
    set_pipeline_stage(
        job["id"], media_stages.STAGE_VIDEO_READY,
        scheduler_lane=media_stages.LANE_VIDEO_READY, ready_at=now(), conn=conn,
    )
    _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
    conn.commit()
    return meta, prompt_text


def _raise_reference_mode_repair_required(
    *, job: Any, meta: dict[str, Any], prompt_text: str, version: Any,
    rejection_details: list[dict[str, Any]], blockers: list[str],
) -> None:
    """真实故障：候选池本该有资产却没有，或生成后被判不合格——保持原有拦截。"""
    ref_failure_reason = (
        f"参考图模式 2 次尝试均未产出可用资产（共 {len(rejection_details)} 张被拒绝）。"
        + _reference_repair_guidance(blockers)
    )
    log_provider_call(
        "reference_image_mode_original_failure", config.MODEL_TEXT, "REFERENCE_MODE_ORIGINAL_FAILURE",
        None, 0, meta={
            "shot_id": job["shot_id"],
            "original_failure_reason": ref_failure_reason,
            "rejection_count": len(rejection_details),
            "rejection_details": rejection_details[:10],
            "asset_manifest_blockers": blockers[:10],
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
        version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text,
    )
    raise VideoInputRepairRequired(ref_failure_reason)


_SELF_HEAL_ENTITY_RE = re.compile(r"^(人物|场景)「(.+?)」")


def _blocked_entity_names(blockers: list[str]) -> tuple[list[str], list[str]]:
    """从 blockers 文案摘出人物名/场景名，供自愈定位重生成目标；文案格式来自
    ``app.multiview.manifest_production_blockers`` 的既有产物（"人物「name」…"/
    "场景「name」…"），只解析既有产物，不重新实现它判断"谁该有资产"的条件。"""
    characters: list[str] = []
    scenes: list[str] = []
    for blocker in blockers:
        match = _SELF_HEAL_ENTITY_RE.match(blocker)
        if not match:
            continue
        (characters if match.group(1) == "人物" else scenes).append(match.group(2))
    return characters, scenes


async def _self_heal_reference_pool(*, project_id: str, blockers: list[str]) -> bool:
    """按 blockers 指向的人物/场景各自动触发一次补生成；用既有入口
    （``app.refs.generate_refs``/``app.scenes.generate_scene_refs``），不复制
    生成逻辑。单个目标失败不阻断其它目标；返回是否至少补成功一项。"""
    character_names, scene_names = _blocked_entity_names(blockers)
    healed = False
    if character_names:
        from app.refs import generate_refs

        for name in character_names:
            try:
                await generate_refs(project_id, only_character=name)
            except Exception as exc:  # noqa: BLE001 单项失败不阻断其它自愈目标
                _LOGGER.warning("[REFERENCE_SELF_HEAL] 人物「%s」补生成失败：%s", name, exc)
                continue
            _LOGGER.info("[REFERENCE_SELF_HEAL] 人物「%s」定妆照补生成完成", name)
            healed = True
    if scene_names:
        from app.scenes import generate_scene_refs

        for name in scene_names:
            try:
                await generate_scene_refs(project_id, only_scene=name)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[REFERENCE_SELF_HEAL] 场景「%s」补生成失败：%s", name, exc)
                continue
            _LOGGER.info("[REFERENCE_SELF_HEAL] 场景「%s」场景图补生成完成", name)
            healed = True
    return healed


async def _complete_reference_mode_with_healed_assets(
    *, conn: Any, job: Any, meta: dict[str, Any], prompt_text: str, version: Any,
    shot_model: Any, decision: Any, assets: list[Any],
) -> tuple[dict, str] | None:
    """自愈补生成后重新装配拿到真实资产：落一份完整可用状态，与参考图模式
    正常成功同规格，不是纯文本回退。资产未过关键帧库策略门禁时返回 None，
    交调用方落回原有判定，不在这里悄悄放宽策略。"""
    gate_meta = {**meta, "reference_images": [a.public_dict() for a in assets]}
    if not video_modes.reference_gallery_matches_library_policy(gate_meta):
        return None
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    meta["mode_decision"] = video_modes.decision_to_dict(decision)
    meta["reference_images"] = video_modes.dedupe_reference_dicts(
        [a.public_dict() for a in assets]
    )
    meta["reference_generation_complete"] = True
    meta["reference_static_ready"] = True
    meta["continuity_anchor_ready"] = True
    meta["reference_group_gate_passed"] = True
    meta["video_input_manifest_frozen"] = True
    meta.pop("first_frame_path", None)
    meta.pop("last_frame_path", None)
    prompt_text = video_modes.append_reference_prompt_notes(
        prompt_text, assets, duration_s=shot_model.duration_s,
        required_identity_names=list(meta.get("required_reference_characters") or []),
    )
    try:
        from app.media_pipeline.reference_store import upsert_reference_set_from_meta
        upsert_reference_set_from_meta(
            shot_id=job["shot_id"], version_id=version["id"], meta=meta, conn=conn,
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
    _LOGGER.info(
        "[REFERENCE_SELF_HEAL] 镜头 %s 自愈后重新装配成功，共 %d 项资产",
        job["shot_id"], len(assets),
    )
    return meta, prompt_text


async def _attempt_reference_self_heal(
    *, conn: Any, job: Any, meta: dict[str, Any], prompt_text: str, version: Any,
    shot_model: Any, bible: Any, screenplay: Any, decision: Any, blockers: list[str],
) -> tuple[dict, str] | None:
    """WS3a 编排：补生成 → 重新解析依赖装配 → 尝试完整落地；任何一步没有
    产出真实可用资产都返回 None，交调用方走原有判定——自愈只多给一次机会，
    不改变最终失败语义。"""
    healed = await _self_heal_reference_pool(project_id=job["project_id"], blockers=blockers)
    if not healed:
        return None
    assets = await video_modes.build_reference_assets(
        conn=conn, project_id=job["project_id"], episode_no=_episode_no_for_job(conn, job),
        episode_id=job["episode_id"], shot_id=job["shot_id"], shot=shot_model,
        bible=bible, decision=decision, existing_meta=meta, screenplay=screenplay,
    )
    if not assets:
        return None
    return await _complete_reference_mode_with_healed_assets(
        conn=conn, job=job, meta=meta, prompt_text=prompt_text, version=version,
        shot_model=shot_model, decision=decision, assets=assets,
    )


async def finish_reference_mode_without_assets(
    *, conn: Any, job: Any, meta: dict[str, Any], prompt_text: str, version: Any,
    shot_model: Any, bible: Any, screenplay: Any, decision: Any,
    rejection_details: list[dict[str, Any]], rejected_assets: list[Any],
    lease_owner: str | None,
) -> tuple[dict, str]:
    """两次参考图生成尝试后仍无可用资产时的唯一落点。

    先按 ``_reference_pool_blockers`` 判断候选池是否本来就是空的；是则纯文本
    出片，否则维持原有的人工修复拦截，两条路径都不放行真实故障。候选池「本该
    有却没有」时，先自动补生成一次（WS3a）再重新解析依赖装配，成功即正常出片；
    每个镜头只自愈一次（``meta["reference_self_heal_attempted"]``），仍缺才走
    原有的待人工路径。
    """
    _release_rejected_reference_assets(
        job=job, lease_owner=lease_owner, rejected_assets=rejected_assets,
    )
    manifest = meta.get("reference_manifest")
    blockers = _reference_pool_blockers(manifest if isinstance(manifest, dict) else None)
    if blockers and not meta.get("reference_self_heal_attempted"):
        meta["reference_self_heal_attempted"] = True
        healed_result = await _attempt_reference_self_heal(
            conn=conn, job=job, meta=meta, prompt_text=prompt_text, version=version,
            shot_model=shot_model, bible=bible, screenplay=screenplay, decision=decision,
            blockers=blockers,
        )
        if healed_result is not None:
            return healed_result
        manifest = meta.get("reference_manifest")
        blockers = _reference_pool_blockers(manifest if isinstance(manifest, dict) else None)
    if not blockers and not rejection_details:
        return await _complete_reference_mode_as_text_only(
            conn=conn, job=job, meta=meta, prompt_text=prompt_text, version=version,
            shot_model=shot_model, bible=bible, screenplay=screenplay, decision=decision,
        )
    _raise_reference_mode_repair_required(
        job=job, meta=meta, prompt_text=prompt_text, version=version,
        rejection_details=rejection_details, blockers=blockers,
    )


__all__ = [name for name in globals() if not name.startswith("__")]
