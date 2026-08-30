"""旧版逐镜生成参考资产的整体流程（单一巨型编排函数，移动未拆分，见文件内说明）。"""
from __future__ import annotations

import asyncio
import hashlib
import json

from pathlib import Path
from typing import Any, Callable

from app import hiagent
from app.schemas import Bible, EpisodeScreenplay, Shot

from .asset_lookup import _asset_from_path, character_reference_assets, reference_image_path, scene_reference_assets
from .continuity_tail import _finalize_reference_selection
from .keyframe_contract import (
    _shot_for_keyframe_beat,
    is_narrative_keyframe_slot,
    keyframe_contract_fingerprint,
    narrative_keyframe_beats,
    required_visual_anchor_names,
    timeline_keyframe_plan,
)
from .mode_selection import (
    KEYFRAME_PROMPT_CONTRACT_VERSION,
    KEYFRAME_STRUCTURAL_FALLBACK_MODE,
    REFERENCE_IMAGE_MODE,
    REFERENCE_IMAGE_TYPES,
    ReferenceImageAsset,
    ShotVideoModeDecision,
    _MULTI_KEYFRAME_INVARIANCE_NOTE,
    _dedupe_str,
    _reference_runtime_blocking,
    _screenplay_call_kwargs,
    batch_prompt_enabled,
    keyframe_candidate_count,
    max_character_reference_images,
    max_reference_images,
    min_generated_references,
    reference_gen_retries,
    reference_prompt_async,
    supporting_keyframe_candidate_count,
)
from .reference_assemble import _enforce_reference_consistency, _enforce_timeline_keyframe_invariance
from .reference_generate import (
    _SLOT_ROLE_CYCLE,
    _generate_reference_keep_best,
    _portrait_seed_inputs,
    previous_tail_reference_asset,
    write_reference_prompt,
    write_reference_prompt_batch,
)
from .seedance_pack import _dedupe_assets



async def _build_generated_reference_assets_legacy(*, conn: Any, project_id: str, episode_no: int, episode_id: str,
                                 shot_id: str, shot: Shot, bible: Bible,
                                 decision: ShotVideoModeDecision, prev_shot: Any | None = None,
                                 rejection_details: list[dict[str, Any]] | None = None,
                                 rejected_out: list[ReferenceImageAsset] | None = None,
                                 on_progress: Callable[
                                     [list[ReferenceImageAsset], list[ReferenceImageAsset]], None
                                 ] | None = None,
                                 allow_missing_continuity_tail: bool = False,
                                 job_id: str | None = None,
                                 existing_meta: dict[str, Any] | None = None,
                                 screenplay: EpisodeScreenplay | None = None) -> list[ReferenceImageAsset]:
    from app.multiview import (
        PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR, PURPOSE_VIDEO_INPUT,
        NARRATIVE_KEYFRAME_SLOT, resolve_shot_asset_dependencies, keyframe_seed_paths,
        library_anchor_assets_from_manifest,
        narrative_keyframe_required, complete_legacy_character_pack, complete_legacy_scene_pack,
        assert_manifest_allows_production, manifest_revisions_match, pack_result_ok,
        character_multiview_enabled, scene_multiview_enabled,
    )
    plan = decision.referenceImagePlan
    max_refs = max_reference_images()
    if existing_meta is None:
        existing_meta = {}
    # 每次真正进入重建流程时先清理旧降级标记；只有本轮 3 选 1
    # 的候选全部命中结构硬伤时，才会在下方重新写入。
    existing_meta.pop("keyframe_fallback_mode", None)
    existing_meta.pop("keyframe_structural_fallback_slots", None)
    current_keyframe_fingerprint = keyframe_contract_fingerprint(
        shot, bible, screenplay=screenplay,
    )
    prior_prompt_contract = str(existing_meta.get("keyframe_prompt_contract_version") or "")
    prompt_contract_changed = prior_prompt_contract != KEYFRAME_PROMPT_CONTRACT_VERSION
    prior_keyframe_fingerprint = str(existing_meta.get("keyframe_contract_fingerprint") or "")
    keyframe_instance_changed = bool(prior_keyframe_fingerprint) and (
        prior_keyframe_fingerprint != current_keyframe_fingerprint
    )
    slot_state: dict[str, Any] = (
        dict(existing_meta.get("reference_slots") or {})
        if not prompt_contract_changed and not keyframe_instance_changed
        else {}
    )
    if prior_prompt_contract and prompt_contract_changed:
        existing_meta["keyframe_prompt_contract_stale"] = prior_prompt_contract
    if prompt_contract_changed and existing_meta.get("reference_manifest_frozen"):
        existing_meta["reference_manifest_asset_stale"] = True
        existing_meta["reference_manifest_frozen"] = False
    if keyframe_instance_changed:
        existing_meta["keyframe_contract_stale"] = prior_keyframe_fingerprint
    existing_meta["keyframe_prompt_contract_version"] = KEYFRAME_PROMPT_CONTRACT_VERSION
    existing_meta["keyframe_contract_fingerprint"] = current_keyframe_fingerprint
    existing_meta["reference_slots"] = slot_state
    scene_name = getattr(shot, "scene_name", "") or ""
    from app.continuity import effective_characters_visible
    visible_character_names = effective_characters_visible(shot)
    bible_character_names = {character.name for character in bible.characters}
    if screenplay is not None and screenplay.narrative_plan is not None:
        from app.identity_contracts import narrative_identity_resolver

        identity_resolver = narrative_identity_resolver(bible, screenplay)
        visible_identities = [
            identity_resolver.resolve(name, usage="visual")
            for name in visible_character_names
        ]
        identity_character_names = list(dict.fromkeys(
            identity.asset_name for identity in visible_identities if identity.allows_asset
        ))
    else:
        # Legacy keeps its historical Bible-only reusable-asset policy.
        identity_character_names = [
            name for name in visible_character_names if name in bible_character_names
        ]

    # 冻结依赖 manifest：worker 重启复用；若本集人物/场景版本已变则判 stale 并重建
    reuse_frozen = False
    frozen_manifest = existing_meta.get("reference_manifest")
    if not prompt_contract_changed and existing_meta.get("reference_manifest_frozen") and isinstance(frozen_manifest, dict):
        current_probe = resolve_shot_asset_dependencies(
            project_id=project_id, episode_no=episode_no, shot_id=shot_id, shot=shot,
            scene_name=scene_name or None, conn=conn, bible=bible, screenplay=screenplay,
        )
        if manifest_revisions_match(frozen_manifest, current_probe):
            manifest = frozen_manifest
            reuse_frozen = True
        else:
            existing_meta["reference_manifest_asset_stale"] = True
            existing_meta["reference_manifest_frozen"] = False
            # 人物/场景版本变更时，旧 passed 关键帧也已失效，不得换上新 manifest 继续复用。
            slot_state.clear()
            existing_meta["reference_slots"] = slot_state

    if not reuse_frozen:
        # 进入本集生产前按需补齐 legacy_partial。补齐失败只留作
        # 风险证据；后续仍用已有主图/其他锨点，必要时回退纯文本视频。
        style = bible.world.visual_style_canonical
        pack_warnings: list[str] = []
        if character_multiview_enabled():
            for name in identity_character_names:
                pack = await complete_legacy_character_pack(project_id, name, episode_no, style)
                if pack is not None and not pack_result_ok(pack):
                    pack_warnings.append(
                        f"人物多视角补齐重试耗尽：{name}"
                        f"（status={pack.get('status')}）"
                    )
        if scene_name and scene_multiview_enabled():
            pack = await complete_legacy_scene_pack(project_id, scene_name, episode_no, style)
            if pack is not None and not pack_result_ok(pack):
                pack_warnings.append(
                    f"场景多视角补齐重试耗尽：{scene_name}"
                    f"（status={pack.get('status')}）"
                )
        if pack_warnings:
            existing_meta["asset_pack_gate_retry_exhausted"] = True
            existing_meta["asset_pack_warnings"] = pack_warnings
        manifest = resolve_shot_asset_dependencies(
            project_id=project_id, episode_no=episode_no, shot_id=shot_id, shot=shot,
            scene_name=scene_name or None, conn=conn, bible=bible, screenplay=screenplay,
        )
        existing_meta["reference_manifest"] = manifest
        existing_meta["reference_manifest_frozen"] = True

    # 兼容保留门禁报告 API，但阻塞项只写入告警，不终止付费链路。
    manifest_warnings = assert_manifest_allows_production(manifest)
    if manifest_warnings:
        existing_meta["asset_manifest_gate_retry_exhausted"] = True
        existing_meta["asset_manifest_warnings"] = list(manifest_warnings)

    # 旧执行入口同样服从同场景真实尾帧策略；孤立测试/兼容调用没有
    # prev_shot 且没有数据库连接时，不得凭 shot_no 猜测或伪造上游尾帧。
    forced: list[ReferenceImageAsset] = []
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    needs_tail = (
        not decision.shotPlanId
        and (prev_shot is not None or conn is not None)
        and uses_previous_tail_frame(derive_continuity_mode(shot, prev=prev_shot))
    )
    if needs_tail:
        prev = prev_shot
        if prev is None and int(getattr(shot, "shot_no", 0) or 0) > 1:
            prev = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
                (episode_id, int(shot.shot_no) - 1)).fetchone()
        if prev is not None:
            ref_dir = reference_image_path(project_id, episode_no, shot.shot_no, "previous_shot_frame", 0).parent
            tail = previous_tail_reference_asset(conn, prev, dest_dir=ref_dir)
            if tail:
                tail.purposes = [PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR]
                tail.required = True
                tail.entity_type = "continuity"
                forced.append(tail)
            elif not allow_missing_continuity_tail:
                pass

    # 每镜必需 1 张叙事关键帧
    min_gen = max(min_generated_references(), 1 if narrative_keyframe_required() else 0)
    if decision.mode == REFERENCE_IMAGE_MODE:
        want_gen = max(int(plan.generateNewCount or 0), min_gen)
    else:
        want_gen = 0

    # 证据锚点（人物/场景多视角）进入画廊但不默认挤占 video_input 名额
    evidence_assets: list[ReferenceImageAsset] = []
    for anchor in library_anchor_assets_from_manifest(manifest):
        path = anchor.get("image_path")
        if not path or not Path(path).exists():
            continue
        try:
            evidence_assets.append(_asset_from_path(
                path=path,
                ref_type=anchor.get("type") or "character",
                source="asset_library",
                related_character_ids=[anchor["entity_name"]] if anchor.get("entity_type") == "character" else None,
                quality_score=None,
                qa={"status": "library", "overall": None, "issues": []},
                entity_type=anchor.get("entity_type"),
                entity_name=anchor.get("entity_name"),
                library_revision_id=anchor.get("library_revision_id"),
                library_view_id=anchor.get("library_view_id"),
                view_role=anchor.get("view_role"),
                purposes=list(anchor.get("purposes") or [PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR]),
            ))
        except OSError:
            continue

    # 兼容旧路径：若 manifest 无锚点，回退 scene/character helpers
    scene_assets = scene_reference_assets(
        bible, getattr(shot, "scene_name", "") or "", project_id=project_id, episode_no=episode_no,
    )
    if not any(a.entity_type == "scene" for a in evidence_assets):
        evidence_assets.extend(scene_assets)
    char_assets = character_reference_assets(
        bible, identity_character_names, limit=max(1, len(identity_character_names)),
        project_id=project_id, episode_no=episode_no,
    )
    if not any(a.entity_type == "character" for a in evidence_assets):
        evidence_assets.extend(char_assets)

    selected: list[ReferenceImageAsset] = list(forced)

    # Seedance 最终输入不再只有生成关键帧：人物定妆与场景定场各至少预留一席，
    # 其余容量也不会驱动多生成关键帧；时序关键帧由 1–2 帧策略独立控制。
    # 人物只取各身份的首选视角，避免多张定妆照诱发分身。
    # 即使上一镜尾帧尚未到达，action_continuation 也先预留该席，保证恢复前后时序计划稳定。
    continuity_slot_reserve = 1 if needs_tail else 0
    keyframe_slot_reserve = 1 if decision.mode == REFERENCE_IMAGE_MODE and narrative_keyframe_required() else 0
    anchor_budget = max(0, max_refs - continuity_slot_reserve - keyframe_slot_reserve)

    role_priority = {
        "front_full": 0,
        "three_quarter": 1,
        "profile": 2,
        "side_full": 2,
        "action_zone": 0,
        "establishing": 1,
        "reverse_angle": 2,
    }

    def _anchor_rank(asset: ReferenceImageAsset) -> tuple[int, int, float, str]:
        kind = asset.entity_type or asset.type
        kind_rank = 0 if kind == "character" else (1 if kind == "scene" else 2)
        try:
            score = float(asset.qualityScore) if asset.qualityScore is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        return (kind_rank, role_priority.get(str(asset.view_role or ""), 9), -score, asset.path or asset.id)

    video_anchor_assets: list[ReferenceImageAsset] = []
    seen_anchor_characters: set[str] = set()
    # The setting caps redundant views of one identity, not the number of
    # distinct named people. Every visible named identity gets one anchor when
    # capacity allows; otherwise later characters silently lose their outfit and
    # body-scale evidence at the paid video boundary.
    character_anchor_limit = max(
        max_character_reference_images(), len(identity_character_names),
    )
    for asset in sorted(evidence_assets, key=_anchor_rank):
        if len(video_anchor_assets) >= anchor_budget:
            break
        kind = asset.entity_type or asset.type
        if kind != "character" or len(seen_anchor_characters) >= character_anchor_limit:
            continue
        character_key = str(asset.entity_name or "") or "|".join(asset.relatedCharacterIds) or asset.path or asset.id
        if character_key in seen_anchor_characters:
            continue
        seen_anchor_characters.add(character_key)
        video_anchor_assets.append(asset)
    if len(video_anchor_assets) < anchor_budget:
        scene_anchor = next(
            (
                asset for asset in sorted(evidence_assets, key=_anchor_rank)
                if (asset.entity_type or asset.type) == "scene" and asset not in video_anchor_assets
            ),
            None,
        )
        if scene_anchor is not None:
            video_anchor_assets.append(scene_anchor)

    for asset in video_anchor_assets:
        asset.purposes = _dedupe_str([*(asset.purposes or []), PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR])
        asset.required = True
        asset.selectedForSeedance = True
    selected.extend(video_anchor_assets)

    available_generated_slots = max(
        0,
        max_refs - continuity_slot_reserve - len(video_anchor_assets),
    )
    keyframe_plan = timeline_keyframe_plan(shot)
    if decision.mode != REFERENCE_IMAGE_MODE:
        generated_needed = 0
    elif decision.defaulted:
        # 默认生产合同：人物/场景锚点之外只允许 1–2 个时间路标。
        # 0–7 秒强制 1 张；更长镜头由剧情阶段复杂度决定是否需要第 2 张。
        generated_needed = min(int(keyframe_plan["count"]), available_generated_slots)
    else:
        # 自定义计划中的场景/道具图不受关键帧数量限制；关键帧本身在下方单独裁剪。
        generated_needed = min(want_gen, available_generated_slots)

    temporal_beat_count = generated_needed if decision.defaulted else (1 if generated_needed else 0)
    temporal_beats = narrative_keyframe_beats(shot, temporal_beat_count) if temporal_beat_count else []
    beat_by_slot = {str(beat["slot_key"]): beat for beat in temporal_beats}

    sequence_material = {
        "policy_version": KEYFRAME_PROMPT_CONTRACT_VERSION,
        "max_images": max_refs,
        "continuity_reserved": bool(needs_tail),
        "keyframe_plan": keyframe_plan,
        "anchor_keys": [
            {
                "entity_type": asset.entity_type or asset.type,
                "entity_name": asset.entity_name,
                "library_revision_id": asset.library_revision_id,
                "library_view_id": asset.library_view_id,
                "path": asset.path,
            }
            for asset in video_anchor_assets
        ],
        "beats": temporal_beats,
    }
    sequence_fingerprint = hashlib.sha256(
        json.dumps(sequence_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    prior_sequence = existing_meta.get("keyframe_sequence")
    prior_sequence_fingerprint = str(
        prior_sequence.get("fingerprint") if isinstance(prior_sequence, dict) else ""
    )
    if prior_sequence_fingerprint and prior_sequence_fingerprint != sequence_fingerprint:
        # 席位数/锚点/时间点改变时，旧 winner 不再是同一个冻结节拍。
        slot_state.clear()
        existing_meta["reference_slots"] = slot_state
    existing_meta["keyframe_sequence"] = {
        **sequence_material,
        "fingerprint": sequence_fingerprint,
        "beat_count": len(temporal_beats),
        "reserved_input_count": continuity_slot_reserve + len(video_anchor_assets),
    }

    def _publish_progress() -> None:
        if on_progress is None:
            return
        gallery = _dedupe_assets(list(selected) + list(evidence_assets))
        for asset in gallery:
            asset.shotId = asset.shotId or shot_id
            asset.episodeId = asset.episodeId or episode_id
            # 仅 video_input 用途默认选中
            if PURPOSE_VIDEO_INPUT in (asset.purposes or []) or asset.type == "previous_shot_frame":
                asset.selectedForSeedance = (
                    not asset.deleted
                    and not _reference_runtime_blocking(asset)
                )
            else:
                asset.selectedForSeedance = False
        visible_rejected = rejected_out or []
        for asset in visible_rejected:
            asset.selectedForSeedance = False
            asset.shotId = asset.shotId or shot_id
            asset.episodeId = asset.episodeId or episode_id
        on_progress(list(gallery), list(visible_rejected))

    _publish_progress()

    type_cycle = [t for t in plan.types if t in REFERENCE_IMAGE_TYPES and t not in {"previous_shot_frame"}] or ["plot_key_frame"]
    model_specs = [p for p in (plan.prompts or []) if p.get("prompt")]
    # specs 是逻辑槽位；叙事关键帧的 3 张图是同一 slot 下的 candidates，
    # 不得装成 extra slots，否则三张都会进入 Seedance。
    specs: list[tuple[str, str, str | None, int]] = []  # slot_key, ref_type, prompt, slot ordinal
    checkpointed_prompt_slots: set[str] = set()
    candidate_pool: dict[str, list[tuple[int, ReferenceImageAsset]]] = {}
    candidate_targets: dict[str, int] = {}
    candidate_ref_types: dict[str, str] = {}
    candidate_statuses: dict[tuple[str, int], str] = {}
    candidate_audit_records: dict[str, dict[int, dict[str, Any]]] = {}
    candidate_cleanup_pool: dict[str, list[tuple[int, ReferenceImageAsset]]] = {}
    selection_ready_slots: set[str] = set()

    def _apply_keyframe_beat(asset: ReferenceImageAsset, slot_key: str) -> None:
        beat = beat_by_slot.get(slot_key)
        if not beat:
            return
        asset.keyframe_index = int(beat["beat_index"])
        asset.keyframe_total = int(beat["beat_total"])
        asset.keyframe_time_ratio = float(beat["time_ratio"])
        asset.keyframe_target_desc = str(beat["target_desc"])
        asset.qa = {**(asset.qa or {}), "keyframe_beat": dict(beat)}

    def _candidate_record(
        slot_key: str,
        candidate_no: int,
        asset: ReferenceImageAsset,
        *,
        include_path: bool = True,
        status: str | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "candidate_no": candidate_no,
            "id": asset.id,
            "status": status or candidate_statuses.get((slot_key, candidate_no), "qa_pending"),
            "qa": asset.qa,
            "quality_score": asset.qualityScore,
        }
        if include_path and asset.path:
            record["path"] = asset.path
        return record

    def _checkpoint_candidates(slot_key: str, status: str) -> None:
        records = dict(candidate_audit_records.get(slot_key) or {})
        for candidate_no, asset in candidate_pool.get(slot_key, []):
            records[candidate_no] = _candidate_record(slot_key, candidate_no, asset)
        target = candidate_targets.get(slot_key, len(records) or 1)
        slot_state[slot_key] = {
            **(slot_state.get(slot_key) or {}),
            "status": status,
            "type": candidate_ref_types.get(slot_key, "plot_key_frame"),
            "candidate_target": target,
            "candidate_count": len(records),
            "candidates": [records[n] for n in sorted(records)],
            "prompt_contract_version": KEYFRAME_PROMPT_CONTRACT_VERSION,
            "keyframe_contract_fingerprint": current_keyframe_fingerprint,
            **({"keyframe_beat": dict(beat_by_slot[slot_key])} if slot_key in beat_by_slot else {}),
        }
        existing_meta["reference_slots"] = slot_state

    def _rehydrate_candidate(
        slot_key: str,
        ref_type: str,
        candidate_no: int,
        record: dict[str, Any],
    ) -> ReferenceImageAsset | None:
        path = str(record.get("path") or "")
        if not path or not Path(path).is_file():
            return None
        try:
            asset = _asset_from_path(
                path=path,
                ref_type=ref_type,
                source="seedream_generated",
                quality_score=(
                    float(record.get("quality_score"))
                    if record.get("quality_score") is not None else None
                ),
                qa=record.get("qa") or {"overall": None, "status": "qa_pending", "resumed": True},
                purposes=[PURPOSE_QA_ANCHOR],
                required=True,
                slot_key=slot_key,
                entity_type="shot",
            )
        except (OSError, TypeError, ValueError):
            return None
        asset.id = str(record.get("id") or asset.id)
        asset.candidate_no = candidate_no
        asset.selectedForSeedance = False
        asset.dependency_manifest = manifest
        asset.prompt_contract_version = KEYFRAME_PROMPT_CONTRACT_VERSION
        asset.keyframe_contract_fingerprint = current_keyframe_fingerprint
        _apply_keyframe_beat(asset, slot_key)
        return asset

    resumable_slots = {
        k: v for k, v in slot_state.items()
        if isinstance(v, dict)
        and v.get("status") in {"passed", "unverified", "scored_warning"}
        and v.get("path")
        and (not is_narrative_keyframe_slot(k) or v.get("type") == "plot_key_frame")
        and v.get("prompt_contract_version") == KEYFRAME_PROMPT_CONTRACT_VERSION
        and v.get("keyframe_contract_fingerprint") == current_keyframe_fingerprint
    }
    planned_slots: list[tuple[str, str, str | None, int]] = []
    if decision.defaulted:
        planned_slots = [
            (
                str(beat["slot_key"]),
                "plot_key_frame",
                str(beat["prompt_intent"]),
                index,
            )
            for index, beat in enumerate(temporal_beats)
        ]
    else:
        custom_keyframe_count = 0
        for i in range(generated_needed):
            role = _SLOT_ROLE_CYCLE[i % len(_SLOT_ROLE_CYCLE)]
            proposed_type = (
                (model_specs[i].get("type") if i < len(model_specs) else None)
                or type_cycle[i % len(type_cycle)]
            )
            if proposed_type == "plot_key_frame":
                if custom_keyframe_count >= int(keyframe_plan["count"]):
                    continue
                slot_key = (
                    "narrative_keyframe"
                    if custom_keyframe_count == 0
                    else f"narrative_keyframe_{custom_keyframe_count:02d}"
                )
                custom_keyframe_count += 1
            else:
                slot_key = role[0] if i < len(_SLOT_ROLE_CYCLE) else f"extra_{i}"
            brief = model_specs[i].get("prompt") if i < len(model_specs) else None
            planned_slots.append((slot_key, proposed_type, brief, i))

    for slot_key, proposed_type, planned_brief, i in planned_slots:
        # 叙事关键帧是必需几何合同槽；旧/custom mode plan 不得把它降成 character/scene。
        ref_type = "plot_key_frame" if is_narrative_keyframe_slot(slot_key) else proposed_type
        prior = slot_state.get(slot_key) if isinstance(slot_state.get(slot_key), dict) else {}
        candidate_ref_types[slot_key] = ref_type
        if slot_key in resumable_slots:
            prev = resumable_slots[slot_key]
            path = prev.get("path")
            if path and Path(path).is_file():
                asset = _asset_from_path(
                    path=path,
                    ref_type=prev.get("type") or ref_type,
                    source="seedream_generated",
                    quality_score=float(prev.get("quality_score") or 0.0) if prev.get("quality_score") is not None else None,
                    qa=prev.get("qa") or {"overall": None, "status": "unverified", "resumed": True},
                    purposes=[PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR],
                    required=True,
                    slot_key=slot_key,
                    entity_type="shot",
                )
                asset.dependency_manifest = manifest
                asset.prompt_contract_version = KEYFRAME_PROMPT_CONTRACT_VERSION
                asset.keyframe_contract_fingerprint = current_keyframe_fingerprint
                asset.candidate_no = int(prev.get("winner_candidate_no") or 1)
                _apply_keyframe_beat(asset, slot_key)
                if prev.get("status") == "unverified":
                    asset.rejectReason = "qa_unverified_score_only"
                elif prev.get("status") == "scored_warning":
                    asset.rejectReason = "quality_below_threshold_score_only"
                selected.append(asset)
                continue

        prior_contract_ok = (
            prior.get("type") == ref_type
            and prior.get("prompt_contract_version") == KEYFRAME_PROMPT_CONTRACT_VERSION
            and prior.get("keyframe_contract_fingerprint") == current_keyframe_fingerprint
        )
        if slot_key == NARRATIVE_KEYFRAME_SLOT:
            target = keyframe_candidate_count()
        elif is_narrative_keyframe_slot(slot_key):
            target = supporting_keyframe_candidate_count()
        else:
            target = 1
        prior_records = prior.get("candidates") if prior_contract_ok else None
        # 兼容旧的单图 qa_pending 恢复点：已付费的图不在升级中重生。
        legacy_single_pending = bool(
            prior_contract_ok
            and prior.get("status") == "qa_pending"
            and not isinstance(prior_records, list)
            and prior.get("path")
        )
        if legacy_single_pending:
            target = 1
            prior_records = [{
                "candidate_no": 1,
                "id": prior.get("id"),
                "path": prior.get("path"),
                "status": "qa_pending",
                "qa": prior.get("qa"),
                "quality_score": prior.get("quality_score"),
            }]
        elif prior_contract_ok and prior.get("candidate_target") is not None:
            try:
                target = max(1, min(int(prior["candidate_target"]), 5))
            except (TypeError, ValueError):
                pass
        candidate_targets[slot_key] = target
        candidate_pool.setdefault(slot_key, [])
        candidate_audit_records.setdefault(slot_key, {})
        seen_candidate_nos: set[int] = set()
        for raw_record in prior_records or []:
            if not isinstance(raw_record, dict):
                continue
            try:
                candidate_no = int(raw_record.get("candidate_no"))
            except (TypeError, ValueError):
                continue
            if candidate_no < 1 or candidate_no > target or candidate_no in seen_candidate_nos:
                continue
            seen_candidate_nos.add(candidate_no)
            asset = _rehydrate_candidate(slot_key, ref_type, candidate_no, raw_record)
            if asset is not None:
                candidate_pool[slot_key].append((candidate_no, asset))
                candidate_statuses[(slot_key, candidate_no)] = str(raw_record.get("status") or "qa_pending")
            elif raw_record.get("status") in {
                "discarded_deleted", "discarded_pending_cleanup", "cleanup_pending", "generation_failed",
            }:
                recovered_status = str(raw_record.get("status"))
                if recovered_status in {"discarded_pending_cleanup", "cleanup_pending"}:
                    # checkpoint 之后、最终状态落库之前已删除：按已清理恢复。
                    recovered_status = "discarded_deleted"
                candidate_audit_records[slot_key][candidate_no] = {
                    "candidate_no": candidate_no,
                    "id": raw_record.get("id"),
                    "status": recovered_status,
                    "qa": raw_record.get("qa"),
                    "quality_score": raw_record.get("quality_score"),
                }
        candidate_pool[slot_key].sort(key=lambda pair: pair[0])
        winner_no = int(prior.get("winner_candidate_no") or 0)
        if prior_contract_ok and prior.get("status") == "selection_pending_cleanup" and any(
            no == winner_no for no, _asset in candidate_pool[slot_key]
        ):
            selection_ready_slots.add(slot_key)
            continue
        if len(candidate_pool[slot_key]) >= target:
            continue
        prior_prompt = str((prior or {}).get("prompt") or "").strip()
        if (
            prior_contract_ok
            and (
                prior_prompt
                or prior.get("prompt_source") == "deterministic_template"
                or bool(candidate_pool[slot_key])
            )
        ):
            specs.append((slot_key, ref_type, prior_prompt or None, i))
            checkpointed_prompt_slots.add(slot_key)
            continue
        # 旧计划若把必需关键帧声明成 character/scene，其 portrait/environment 正文也必须作废，
        # 不能只把 type 标签改成 plot_key_frame 后继续稀释硬合同。
        brief = planned_brief
        if is_narrative_keyframe_slot(slot_key) and proposed_type != ref_type:
            brief = None
        # 失效 passed/错类型 slot 不得在 **old 合并时残留旧 path/qa/score。
        if not candidate_pool[slot_key]:
            slot_state.pop(slot_key, None)
        specs.append((slot_key, ref_type, brief, i))

    prompts_to_write = [spec for spec in specs if spec[0] not in checkpointed_prompt_slots]
    if prompts_to_write and batch_prompt_enabled():
        prompts = await write_reference_prompt_batch(
            shot, bible, [(s, t) for s, t, _, _ in prompts_to_write],
            intents=[o for _, _, o, _ in prompts_to_write],
            beats=[beat_by_slot.get(s) for s, _, _, _ in prompts_to_write],
            **_screenplay_call_kwargs(screenplay),
        )
        written_by_slot = {
            prompts_to_write[i][0]: prompts[i] or prompts_to_write[i][2]
            for i in range(len(prompts_to_write))
        }
        specs = [
            (slot_key, ref_type, written_by_slot.get(slot_key, prompt), ordinal)
            for slot_key, ref_type, prompt, ordinal in specs
        ]
    elif prompts_to_write and reference_prompt_async():
        async def _resolve(slot_key: str, ref_type: str, brief: str | None) -> str | None:
            beat_shot = _shot_for_keyframe_beat(shot, beat_by_slot.get(slot_key))
            written = await write_reference_prompt(
                beat_shot, bible, ref_type, intent=brief,
                **_screenplay_call_kwargs(screenplay),
            )
            return written or brief or None
        resolved = await asyncio.gather(*[
            _resolve(slot_key, ref_type, brief)
            for slot_key, ref_type, brief, _ordinal in prompts_to_write
        ])
        written_by_slot = {
            prompts_to_write[i][0]: resolved[i] for i in range(len(prompts_to_write))
        }
        specs = [
            (slot_key, ref_type, written_by_slot.get(slot_key, prompt), ordinal)
            for slot_key, ref_type, prompt, ordinal in specs
        ]

    for slot_key, ref_type, prompt, _ordinal in specs:
        slot_state[slot_key] = {
            **(slot_state.get(slot_key) or {}),
            "status": "generating_candidates" if candidate_pool.get(slot_key) else "prompt_ready",
            "type": ref_type,
            "prompt": prompt,
            "prompt_source": "llm_override" if prompt else "deterministic_template",
            "candidate_target": candidate_targets.get(slot_key, 1),
            "candidate_count": len(candidate_pool.get(slot_key, [])),
            "candidates": [
                _candidate_record(slot_key, no, asset)
                for no, asset in candidate_pool.get(slot_key, [])
            ],
            "prompt_contract_version": KEYFRAME_PROMPT_CONTRACT_VERSION,
            "keyframe_contract_fingerprint": current_keyframe_fingerprint,
            **({"keyframe_beat": dict(beat_by_slot[slot_key])} if slot_key in beat_by_slot else {}),
        }
    if specs and existing_meta is not None:
        existing_meta["reference_slots"] = slot_state
        # prompt_ready 是恢复点：必须在调用图片供应商之前持久化。
        _publish_progress()

    # 关键帧种子：优先用本镜选中的人物/场景视角
    seed_paths = keyframe_seed_paths(manifest)
    portrait_seeds = []
    loaded_seed_paths: list[str] = []
    for p in seed_paths:
        try:
            portrait_seeds.append(hiagent.data_url_from_file(p))
            loaded_seed_paths.append(p)
        except OSError:
            continue
    if not portrait_seeds:
        portrait_seeds = _portrait_seed_inputs(
            bible, identity_character_names, project_id=project_id, episode_no=episode_no,
        )
    env_seeds = [a.url for a in forced if a.type == "previous_shot_frame" and a.url]
    env_seeds += [a.url for a in evidence_assets if a.type == "scene" and a.url]

    seed_order_lines: list[str] = []
    seed_anchor_by_path = {
        str(anchor.get("image_path") or ""): anchor
        for anchor in library_anchor_assets_from_manifest(manifest)
        if PURPOSE_KEYFRAME_SEED in (anchor.get("purposes") or [])
    }
    for seed_position, path in enumerate(loaded_seed_paths, start=1):
        anchor = seed_anchor_by_path.get(path) or {}
        entity_type = str(anchor.get("entity_type") or anchor.get("type") or "reference")
        entity_name = str(anchor.get("entity_name") or "unnamed")
        view_role = str(anchor.get("view_role") or "unspecified view")
        seed_order_lines.append(
            f"input image {seed_position} = {entity_type} '{entity_name}', {view_role}, identity/environment anchor only"
        )
    seed_order_note = (
        "REFERENCE IMAGE ROLE MAP (match by input order; never blend identities): "
        + "; ".join(seed_order_lines)
        if seed_order_lines else None
    )

    def _seeds_for(ref_type: str) -> list[str]:
        seeds = (portrait_seeds + env_seeds) if ref_type in {"character", "plot_key_frame"} else list(env_seeds)
        return _dedupe_str(seeds)

    if specs:
        async def _run_candidate(
            slot_key: str,
            ref_type: str,
            override: str | None,
            candidate_no: int,
            generation_index: int,
        ) -> tuple[str, str, int, ReferenceImageAsset | None, list[ReferenceImageAsset], list[dict[str, Any]]]:
            beat_shot = _shot_for_keyframe_beat(shot, beat_by_slot.get(slot_key))
            invariance_note = _MULTI_KEYFRAME_INVARIANCE_NOTE if len(temporal_beats) > 1 else None
            extra_instruction = " ".join(
                part for part in (seed_order_note, invariance_note) if part
            ) or None
            asset, discarded, rej = await _generate_reference_keep_best(
                project_id=project_id,
                episode_no=episode_no,
                shot=beat_shot,
                bible=bible,
                ref_type=ref_type,
                index=generation_index,
                content_override=override,
                retries=reference_gen_retries(),
                seed_inputs=_seeds_for(ref_type),
                extra_instruction=extra_instruction if ref_type == "plot_key_frame" else None,
                skip_inline_qa=True,
                **_screenplay_call_kwargs(screenplay),
            )
            return slot_key, ref_type, candidate_no, asset, discarded, rej

        generation_tasks = []
        for slot_key, ref_type, override, ordinal in specs:
            existing_nos = {no for no, _asset in candidate_pool.get(slot_key, [])}
            # 5 是候选数上限；不同 slot/candidate 的产物索引永不冲突。
            for candidate_no in range(1, candidate_targets.get(slot_key, 1) + 1):
                if candidate_no in existing_nos:
                    continue
                generation_index = ordinal * 5 + candidate_no
                generation_tasks.append(asyncio.create_task(_run_candidate(
                    slot_key, ref_type, override, candidate_no, generation_index,
                )))

        for completed in asyncio.as_completed(generation_tasks):
            slot_key, ref_type, candidate_no, asset, discarded, rej = await completed
            if rejection_details is not None:
                rejection_details.extend(rej)
            # skip_inline_qa 正常不会返回 discarded；防御性清理也不把它们放进画廊。
            for stale in discarded:
                stale.selectedForSeedance = False
                stale.deleted = True
                if stale.path:
                    try:
                        Path(stale.path).unlink(missing_ok=True)
                    except OSError:
                        pass
            if asset is None:
                candidate_audit_records.setdefault(slot_key, {})[candidate_no] = {
                    "candidate_no": candidate_no,
                    "id": None,
                    "status": "generation_failed",
                    "qa": None,
                    "quality_score": None,
                }
            else:
                asset.slot_key = slot_key
                asset.candidate_no = candidate_no
                asset.required = is_narrative_keyframe_slot(slot_key) or asset.type == "plot_key_frame"
                asset.entity_type = "shot"
                # winner 未决出前只是 QA staging，绝不能进入视频参考图。
                asset.purposes = [PURPOSE_QA_ANCHOR]
                asset.selectedForSeedance = False
                asset.rejectReason = None
                asset.qa = {"status": "qa_pending", "overall": None, "issues": []}
                asset.qualityScore = None
                asset.dependency_manifest = manifest
                asset.prompt_contract_version = KEYFRAME_PROMPT_CONTRACT_VERSION
                asset.keyframe_contract_fingerprint = current_keyframe_fingerprint
                _apply_keyframe_beat(asset, slot_key)
                candidate_pool.setdefault(slot_key, []).append((candidate_no, asset))
                candidate_pool[slot_key].sort(key=lambda pair: pair[0])
                candidate_statuses[(slot_key, candidate_no)] = "qa_pending"
                candidate_audit_records.get(slot_key, {}).pop(candidate_no, None)
            attempted = len(candidate_pool.get(slot_key, [])) + len(candidate_audit_records.get(slot_key, {}))
            status = "qa_pending" if attempted >= candidate_targets.get(slot_key, 1) else "generating_candidates"
            _checkpoint_candidates(slot_key, status)
            _publish_progress()

    active_candidate_slots = set(candidate_pool) | selection_ready_slots
    empty_slots = [slot for slot in active_candidate_slots if not candidate_pool.get(slot)]
    if empty_slots:
        for slot_key in empty_slots:
            _checkpoint_candidates(slot_key, "technical_failed")
        existing_meta["keyframe_generation_retry_exhausted"] = True
        existing_meta["keyframe_generation_warnings"] = [
            f"{slot_key}: 候选全部生成失败，改用已有锨点或纯文本"
            for slot_key in empty_slots
        ]
        active_candidate_slots.difference_update(empty_slots)
        _publish_progress()

    # 三张候选并发证据化 QA。任何一张 QA 崩溃时，图片 checkpoint 已经落盘；
    # worker 恢复后只补 QA，不再调付费图片供应商。
    qa_tasks = []

    async def _review_candidate(
        slot_key: str,
        ref_type: str,
        candidate_no: int,
        asset: ReferenceImageAsset,
        payload: str,
    ) -> tuple[str, int, ReferenceImageAsset, dict[str, Any]]:
        # VLM 关键帧/参考图质检已下线：技术产物（文件已成功编码为 payload）
        # 存在即视为可用，不再调用模型评审。
        del payload
        qa = {"status": "scored", "overall": 1.0, "issues": []}
        return slot_key, candidate_no, asset, qa

    for slot_key in sorted(active_candidate_slots):
        if slot_key in selection_ready_slots:
            continue
        ref_type = candidate_ref_types[slot_key]
        valid_pairs: list[tuple[int, ReferenceImageAsset]] = []
        for candidate_no, asset in candidate_pool.get(slot_key, []):
            saved_status = candidate_statuses.get((slot_key, candidate_no), "qa_pending")
            qa_snapshot = asset.qa if isinstance(asset.qa, dict) else {}
            already_reviewed = (
                (saved_status == "scored" and qa_snapshot.get("overall") is not None)
                or (saved_status == "unverified" and qa_snapshot.get("status") == "unverified")
            )
            if already_reviewed:
                valid_pairs.append((candidate_no, asset))
                continue
            if not asset.path or not Path(asset.path).is_file():
                candidate_audit_records[slot_key][candidate_no] = {
                    "candidate_no": candidate_no,
                    "id": asset.id,
                    "status": "technical_failed",
                    "qa": {"status": "unverified", "overall": None, "issues": ["关键帧文件缺失"]},
                    "quality_score": None,
                }
                candidate_cleanup_pool.setdefault(slot_key, []).append((candidate_no, asset))
                continue
            try:
                payload = hiagent.encode_image_file(asset.path)
            except OSError:
                candidate_audit_records[slot_key][candidate_no] = {
                    "candidate_no": candidate_no,
                    "id": asset.id,
                    "status": "technical_failed",
                    "qa": {"status": "unverified", "overall": None, "issues": ["关键帧无法读取"]},
                    "quality_score": None,
                }
                candidate_cleanup_pool.setdefault(slot_key, []).append((candidate_no, asset))
                continue
            valid_pairs.append((candidate_no, asset))
            qa_tasks.append(asyncio.create_task(_review_candidate(
                slot_key, ref_type, candidate_no, asset, payload,
            )))
        candidate_pool[slot_key] = valid_pairs

    unreadable_slots = [
        slot for slot in active_candidate_slots
        if slot not in selection_ready_slots and not candidate_pool.get(slot)
    ]
    if unreadable_slots:
        for slot_key in unreadable_slots:
            for _candidate_no, asset in candidate_cleanup_pool.get(slot_key, []):
                if asset.path:
                    try:
                        Path(asset.path).unlink(missing_ok=True)
                    except OSError:
                        pass
            _checkpoint_candidates(slot_key, "technical_failed")
        existing_meta["keyframe_file_retry_exhausted"] = True
        existing_meta["keyframe_file_warnings"] = [
            f"{slot_key}: 候选均不可读，改用已有锨点或纯文本"
            for slot_key in unreadable_slots
        ]
        active_candidate_slots.difference_update(unreadable_slots)
        _publish_progress()

    for completed in asyncio.as_completed(qa_tasks):
        slot_key, candidate_no, asset, qa = await completed
        asset.qa = dict(qa or {})
        _apply_keyframe_beat(asset, slot_key)
        if asset.qa.get("status") == "unverified" or asset.qa.get("overall") is None:
            asset.qualityScore = None
            asset.rejectReason = "qa_unverified_score_only"
            candidate_statuses[(slot_key, candidate_no)] = "unverified"
        else:
            try:
                overall = float(asset.qa.get("overall"))
            except (TypeError, ValueError):
                overall = 0.0
            asset.qualityScore = overall
            asset.qa.setdefault("absolute_quality", overall)
            asset.rejectReason = None
            candidate_statuses[(slot_key, candidate_no)] = "scored"
        asset.selectedForSeedance = False
        asset.purposes = [PURPOSE_QA_ANCHOR]
        _checkpoint_candidates(slot_key, "qa_pending")
        _publish_progress()

    # VLM 跨槽一致性比对已下线（原用于在双关键帧之间比较身份/服装/体型/身高比例）：
    # 已知限制，双关键帧之间的一致性不再有自动化校验，需要人工在候选列表里复核。

    def _numeric_qa_score(asset: ReferenceImageAsset) -> float | None:
        value = (asset.qa or {}).get("overall")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # VLM 关键帧身份/几何门禁与运行时硬失败检测已下线（原 keyframe_gate_passed/
    # keyframe_runtime_blocking_failures）：技术产物存在即可用，不再按身份契约
    # 剔除候选或删除候选文件。已知限制：多候选中撞脸/换装/几何错误的候选不再
    # 被自动过滤，需要人工在候选列表里复核后再采用。
    eligible_by_slot: dict[str, list[tuple[int, ReferenceImageAsset]]] = {
        slot_key: list(candidate_pool.get(slot_key, []))
        for slot_key in active_candidate_slots
    }
    contract_blocked_by_slot: dict[str, list[dict[str, Any]]] = {}
    existing_meta["reference_slots"] = slot_state

    all_cleanup_errors: list[str] = []
    required_identity_names = required_visual_anchor_names(manifest)
    anchored_identity_names = {
        str(asset.entity_name or "").strip()
        for asset in video_anchor_assets
        if (asset.entity_type or asset.type) == "character"
        and str(asset.entity_name or "").strip()
    }
    for slot_key in sorted(active_candidate_slots):
        slot_cleanup_errors: list[str] = []
        all_pairs = candidate_pool.get(slot_key, [])
        pairs = eligible_by_slot.get(slot_key, all_pairs)
        if not all_pairs:
            _checkpoint_candidates(slot_key, "technical_failed")
            continue
        if not pairs and contract_blocked_by_slot.get(slot_key):
            final_records: list[dict[str, Any]] = []
            for candidate_no, asset in all_pairs:
                asset.selectedForSeedance = False
                asset.deleted = True
                asset.rejectReason = "identity_contract_failed"
                asset.purposes = [
                    purpose for purpose in (asset.purposes or [])
                    if purpose != PURPOSE_VIDEO_INPUT
                ]
                delete_failed = False
                if asset.path:
                    try:
                        Path(asset.path).unlink(missing_ok=True)
                    except OSError as exc:
                        delete_failed = True
                        message = f"{slot_key} candidate {candidate_no}: {exc}"
                        slot_cleanup_errors.append(message)
                        all_cleanup_errors.append(message)
                final_records.append(_candidate_record(
                    slot_key,
                    candidate_no,
                    asset,
                    include_path=delete_failed,
                    status="cleanup_pending" if delete_failed else "contract_rejected_deleted",
                ))
                if rejection_details is not None:
                    rejection_details.append({
                        "type": asset.type,
                        "source": asset.source,
                        "reason": "identity_contract_failed",
                        "candidate_no": candidate_no,
                        "identity_contract_passed": False,
                    })
            slot_state[slot_key] = {
                **(slot_state.get(slot_key) or {}),
                "status": (
                    "contract_gate_cleanup_pending"
                    if slot_cleanup_errors
                    else "contract_gate_failed"
                ),
                "gate_retry_exhausted": True,
                "gate_warnings": contract_blocked_by_slot[slot_key],
                "candidate_target": candidate_targets.get(slot_key, len(final_records)),
                "candidate_count": len(final_records),
                "candidates": final_records,
                "winner_candidate_no": None,
                "path": None,
                "qa": None,
                "quality_score": None,
            }
            if required_identity_names.issubset(anchored_identity_names):
                fallback_slots = {
                    str(item)
                    for item in (
                        existing_meta.get("keyframe_structural_fallback_slots") or []
                    )
                    if str(item)
                }
                fallback_slots.add(slot_key)
                existing_meta["keyframe_fallback_mode"] = (
                    KEYFRAME_STRUCTURAL_FALLBACK_MODE
                )
                existing_meta["keyframe_structural_fallback_slots"] = sorted(
                    fallback_slots
                )
            existing_meta["reference_slots"] = slot_state
            _publish_progress()
            continue
        prior = slot_state.get(slot_key) or {}
        frozen_winner_no = int(prior.get("winner_candidate_no") or 0)
        if slot_key in selection_ready_slots and any(no == frozen_winner_no for no, _asset in pairs):
            winner_no, winner = next(pair for pair in pairs if pair[0] == frozen_winner_no)
        else:
            # 有数字 QA 的候选永远优先于 unverified；同分/全未评分按 candidate_no 稳定取第一张。
            winner_no, winner = max(
                pairs,
                key=lambda pair: (
                    _numeric_qa_score(pair[1]) is not None,
                    _numeric_qa_score(pair[1]) or 0.0,
                    -pair[0],
                ),
            )
        winner.candidate_no = winner_no
        winner.required = is_narrative_keyframe_slot(slot_key) or winner.type == "plot_key_frame"
        winner.purposes = [PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR]
        winner.selectedForSeedance = True
        winner.deleted = False
        winner_status = "unverified"
        if _numeric_qa_score(winner) is None:
            winner.rejectReason = "qa_unverified_score_only"
        else:
            # VLM 身份/几何门禁与运行时硬失败检测已下线：技术产物存在即通过。
            passed = True
            structural_warnings: set[str] = set()
            if passed and not structural_warnings:
                winner_status = "passed"
                winner.rejectReason = None
            else:
                winner_status = "blocked"
                winner.selectedForSeedance = False
                winner.purposes = [
                    purpose
                    for purpose in winner.purposes
                    if purpose != PURPOSE_VIDEO_INPUT
                ]
                winner.rejectReason = "runtime_contract_blocked"
                winner.qa = {
                    **(winner.qa or {}),
                    "gate_retry_exhausted": True,
                }
        if winner.selectedForSeedance and winner not in selected:
            selected.append(winner)

        for candidate_no, _asset in all_pairs:
            candidate_statuses[(slot_key, candidate_no)] = (
                "selected_pending_cleanup" if candidate_no == winner_no else "discarded_pending_cleanup"
            )
        _checkpoint_candidates(slot_key, "selection_pending_cleanup")
        slot_state[slot_key].update({
            "winner_candidate_no": winner_no,
            "path": winner.path,
            "qa": winner.qa,
            "quality_score": winner.qualityScore,
        })
        existing_meta["reference_slots"] = slot_state
        # 先持久化“画廊只有 winner”，再删文件；崩溃只会留下不可达的孤儿文件。
        _publish_progress()

        winner_resolved: Path | None = None
        if winner.path:
            try:
                winner_resolved = Path(winner.path).resolve(strict=False)
            except OSError:
                winner_resolved = Path(winner.path).absolute()
        final_records: dict[int, dict[str, Any]] = dict(candidate_audit_records.get(slot_key) or {})
        for candidate_no, asset in all_pairs:
            if asset is winner:
                final_records[candidate_no] = _candidate_record(
                    slot_key, candidate_no, asset, status="selected",
                )
                continue
            asset.selectedForSeedance = False
            asset.deleted = True
            asset.rejectReason = "best_of_three_not_selected"
            asset.purposes = [p for p in (asset.purposes or []) if p != PURPOSE_VIDEO_INPUT]
            delete_failed = False
            if asset.path:
                try:
                    loser_resolved = Path(asset.path).resolve(strict=False)
                except OSError:
                    loser_resolved = Path(asset.path).absolute()
                if winner_resolved is None or loser_resolved != winner_resolved:
                    try:
                        Path(asset.path).unlink(missing_ok=True)
                    except OSError as exc:
                        delete_failed = True
                        message = f"{slot_key} candidate {candidate_no}: {exc}"
                        slot_cleanup_errors.append(message)
                        all_cleanup_errors.append(message)
            final_records[candidate_no] = _candidate_record(
                slot_key,
                candidate_no,
                asset,
                include_path=delete_failed,
                status="cleanup_pending" if delete_failed else "discarded_deleted",
            )
            if rejection_details is not None:
                rejection_details.append({
                    "type": asset.type,
                    "source": asset.source,
                    "reason": "best_of_three_not_selected",
                    "candidate_no": candidate_no,
                    "quality_score": asset.qualityScore,
                })

        for candidate_no, asset in candidate_cleanup_pool.get(slot_key, []):
            delete_failed = False
            if asset.path:
                try:
                    technical_resolved = Path(asset.path).resolve(strict=False)
                except OSError:
                    technical_resolved = Path(asset.path).absolute()
                if winner_resolved is None or technical_resolved != winner_resolved:
                    try:
                        Path(asset.path).unlink(missing_ok=True)
                    except OSError as exc:
                        delete_failed = True
                        message = f"{slot_key} candidate {candidate_no}: {exc}"
                        slot_cleanup_errors.append(message)
                        all_cleanup_errors.append(message)
            if delete_failed:
                final_records[candidate_no] = {
                    **(final_records.get(candidate_no) or {}),
                    "candidate_no": candidate_no,
                    "id": asset.id,
                    "status": "cleanup_pending",
                    "path": asset.path,
                }

        slot_state[slot_key] = {
            **slot_state[slot_key],
            "status": winner_status if not slot_cleanup_errors else "selection_pending_cleanup",
            "candidate_target": candidate_targets.get(slot_key, len(final_records)),
            "candidate_count": candidate_targets.get(slot_key, len(final_records)),
            "candidates": [final_records[n] for n in sorted(final_records)],
            "winner_candidate_no": winner_no,
            "path": winner.path,
            "qa": winner.qa,
            "quality_score": winner.qualityScore,
        }
        existing_meta["reference_slots"] = slot_state
        _publish_progress()

    if all_cleanup_errors:
        existing_meta["candidate_cleanup_warnings"] = all_cleanup_errors

    # Phase 2：整组相对一致性检查（仅对 video_input 候选）
    if job_id:
        try:
            from app.media_pipeline import stages as media_stages
            from app.media_pipeline.stage_state import set_pipeline_stage
            set_pipeline_stage(job_id, media_stages.STAGE_REFERENCE_CONSISTENCY)
        except Exception:  # noqa: BLE001
            pass
    video_candidates = [a for a in selected if PURPOSE_VIDEO_INPUT in (a.purposes or []) or a.type == "previous_shot_frame"]
    video_candidates = await _enforce_reference_consistency(
        selected=video_candidates, shot=shot, bible=bible, project_id=project_id, episode_no=episode_no,
        rejection_details=rejection_details, rejected_out=rejected_out,
        screenplay=screenplay)
    video_candidates, invariant_dropped_slots = await _enforce_timeline_keyframe_invariance(
        selected=video_candidates,
        shot=shot,
        bible=bible,
        rejection_details=rejection_details,
        rejected_out=rejected_out,
        screenplay=screenplay,
    )
    if invariant_dropped_slots:
        # 两帧无法证明人物不变量时，回退为单一决定性关键帧，并同步冻结元数据；
        # 不能只在装箱时偷偷少传一张，否则恢复链路会把已删除的辅助帧当缺失重建。
        temporal_beats = narrative_keyframe_beats(shot, 1)
        beat_by_slot = {str(beat["slot_key"]): beat for beat in temporal_beats}
        master_beat = temporal_beats[0]
        for asset in video_candidates:
            if asset.slot_key == "narrative_keyframe":
                asset.keyframe_index = 1
                asset.keyframe_total = 1
                asset.keyframe_time_ratio = float(master_beat["time_ratio"])
                asset.keyframe_target_desc = str(master_beat["target_desc"])
                asset.qa = {**(asset.qa or {}), "keyframe_beat": dict(master_beat)}
        for dropped_slot in invariant_dropped_slots:
            raw_slot = slot_state.get(dropped_slot)
            if not isinstance(raw_slot, dict):
                continue
            records = []
            for raw_record in raw_slot.get("candidates") or []:
                if not isinstance(raw_record, dict):
                    continue
                records.append({
                    **raw_record,
                    "status": "discarded_deleted",
                    "path": None,
                })
            slot_state[dropped_slot] = {
                **raw_slot,
                "status": "excluded_cross_frame_identity_drift",
                "path": None,
                "candidates": records,
            }
        if isinstance(slot_state.get("narrative_keyframe"), dict):
            slot_state["narrative_keyframe"]["keyframe_beat"] = dict(master_beat)
        sequence_material["beats"] = temporal_beats
        sequence_material["keyframe_plan"] = {
            **keyframe_plan,
            "count": 1,
            "reason": "cross_frame_identity_invariance_fallback",
            "requested_count": keyframe_plan["count"],
        }
        sequence_fingerprint = hashlib.sha256(
            json.dumps(
                sequence_material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing_meta["keyframe_sequence"] = {
            **sequence_material,
            "fingerprint": sequence_fingerprint,
            "beat_count": 1,
            "reserved_input_count": continuity_slot_reserve + len(video_anchor_assets),
        }
        existing_meta["reference_slots"] = slot_state

    video_candidates = _dedupe_assets(video_candidates)
    video_candidates = _finalize_reference_selection(
        video_candidates, rejected_out=rejected_out, rejection_details=rejection_details)

    # QA 只评分：必需关键帧只做技术/结构门禁，VLM 低分或未评分不伪装成文件缺失。
    valid_keyframe_slots = {
        str(a.slot_key or "")
        for a in video_candidates
        if a.type == "plot_key_frame"
        and not a.deleted
        and a.selectedForSeedance
        and PURPOSE_VIDEO_INPUT in (a.purposes or [])
        and (
            bool(a.path and Path(a.path).is_file())
            or str(a.url or "").startswith("data:image")
        )
    }
    expected_keyframe_slots = {
        str(beat.get("slot_key") or "")
        for beat in temporal_beats
        if str(beat.get("slot_key") or "")
    }
    fallback_slots = {
        str(slot or "").strip()
        for slot in (existing_meta.get("keyframe_structural_fallback_slots") or [])
        if str(slot or "").strip()
    }
    structural_fallback = (
        existing_meta.get("keyframe_fallback_mode") == KEYFRAME_STRUCTURAL_FALLBACK_MODE
        and bool(fallback_slots)
        and fallback_slots.issubset(expected_keyframe_slots)
    )
    required_keyframe_slots = (
        expected_keyframe_slots - fallback_slots if structural_fallback else expected_keyframe_slots
    )
    has_keyframe = (
        required_keyframe_slots.issubset(valid_keyframe_slots)
        if expected_keyframe_slots
        else bool(valid_keyframe_slots)
    )
    if narrative_keyframe_required() and not has_keyframe:
        existing_meta["narrative_keyframe_missing"] = True
        existing_meta["reference_group_gate_passed"] = False
    else:
        existing_meta["narrative_keyframe_missing"] = False

    for asset in video_candidates:
        if PURPOSE_VIDEO_INPUT not in (asset.purposes or []):
            asset.purposes = list(asset.purposes or []) + [PURPOSE_VIDEO_INPUT]
        asset.selectedForSeedance = True
        asset.shotId = asset.shotId or shot_id
        asset.episodeId = asset.episodeId or episode_id

    # 合并证据锚点进画廊（不选中为 video_input，除非显式加入）
    gallery = _dedupe_assets(list(video_candidates) + list(evidence_assets))
    for asset in gallery:
        asset.shotId = asset.shotId or shot_id
        asset.episodeId = asset.episodeId or episode_id
        if PURPOSE_VIDEO_INPUT not in (asset.purposes or []) and asset.type != "previous_shot_frame":
            asset.selectedForSeedance = False
    if rejected_out is not None:
        for asset in rejected_out:
            asset.selectedForSeedance = False
            asset.shotId = asset.shotId or shot_id
            asset.episodeId = asset.episodeId or episode_id
    if existing_meta is not None:
        existing_meta["reference_slots"] = slot_state
        existing_meta["reference_manifest"] = manifest
        existing_meta["reference_manifest_frozen"] = True
        existing_meta["keyframe_prompt_contract_version"] = KEYFRAME_PROMPT_CONTRACT_VERSION
        existing_meta["keyframe_contract_fingerprint"] = current_keyframe_fingerprint
    _publish_progress()
    return gallery
