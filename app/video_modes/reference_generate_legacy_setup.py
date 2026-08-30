"""Setup phases for the legacy per-shot reference-asset build (see
``reference_generate_legacy.py``'s module docstring for the full phase map):
keyframe-contract fingerprint/staleness (``_prepare_contract_fingerprint``),
visible-identity names (``_prepare_identity_names``), the frozen/rebuilt
asset-dependency manifest (``_prepare_manifest``), the previous-shot
continuity tail (``_prepare_continuity_tail``), and the reusable-asset
evidence gallery (``_prepare_evidence_assets``). Moved verbatim out of the
pre-split single function -- only the wrapping into named phase functions,
and reading/writing through ``state`` instead of bare locals, is new.
"""
from __future__ import annotations

from pathlib import Path

from .keyframe_contract import keyframe_contract_fingerprint
from .mode_selection import KEYFRAME_PROMPT_CONTRACT_VERSION
from .reference_generate_legacy_state import _ReferenceBuildState


def _prepare_contract_fingerprint(state: _ReferenceBuildState) -> bool:
    """Refresh the keyframe-contract fingerprint and reset stale slot state.

    Returns ``prompt_contract_changed``, needed by ``_prepare_manifest`` to
    decide whether a frozen dependency manifest may still be reused.
    """
    from .mode_selection import max_reference_images

    state.plan = state.decision.referenceImagePlan
    state.max_refs = max_reference_images()
    # 每次真正进入重建流程时先清理旧降级标记；只有本轮 3 选 1
    # 的候选全部命中结构硬伤时，才会在下方重新写入。
    state.existing_meta.pop("keyframe_fallback_mode", None)
    state.existing_meta.pop("keyframe_structural_fallback_slots", None)
    state.current_keyframe_fingerprint = keyframe_contract_fingerprint(
        state.shot, state.bible, screenplay=state.screenplay,
    )
    prior_prompt_contract = str(state.existing_meta.get("keyframe_prompt_contract_version") or "")
    prompt_contract_changed = prior_prompt_contract != KEYFRAME_PROMPT_CONTRACT_VERSION
    prior_keyframe_fingerprint = str(state.existing_meta.get("keyframe_contract_fingerprint") or "")
    keyframe_instance_changed = bool(prior_keyframe_fingerprint) and (
        prior_keyframe_fingerprint != state.current_keyframe_fingerprint
    )
    state.slot_state = (
        dict(state.existing_meta.get("reference_slots") or {})
        if not prompt_contract_changed and not keyframe_instance_changed
        else {}
    )
    if prior_prompt_contract and prompt_contract_changed:
        state.existing_meta["keyframe_prompt_contract_stale"] = prior_prompt_contract
    if prompt_contract_changed and state.existing_meta.get("reference_manifest_frozen"):
        state.existing_meta["reference_manifest_asset_stale"] = True
        state.existing_meta["reference_manifest_frozen"] = False
    if keyframe_instance_changed:
        state.existing_meta["keyframe_contract_stale"] = prior_keyframe_fingerprint
    state.existing_meta["keyframe_prompt_contract_version"] = KEYFRAME_PROMPT_CONTRACT_VERSION
    state.existing_meta["keyframe_contract_fingerprint"] = state.current_keyframe_fingerprint
    state.existing_meta["reference_slots"] = state.slot_state
    return prompt_contract_changed


def _prepare_identity_names(state: _ReferenceBuildState) -> None:
    """Resolve the scene name and the visible identities allowed a reusable asset."""
    state.scene_name = getattr(state.shot, "scene_name", "") or ""
    from app.continuity import effective_characters_visible
    visible_character_names = effective_characters_visible(state.shot)
    bible_character_names = {character.name for character in state.bible.characters}
    if state.screenplay is not None and state.screenplay.narrative_plan is not None:
        from app.identity_contracts import narrative_identity_resolver

        identity_resolver = narrative_identity_resolver(state.bible, state.screenplay)
        visible_identities = [
            identity_resolver.resolve(name, usage="visual")
            for name in visible_character_names
        ]
        state.identity_character_names = list(dict.fromkeys(
            identity.asset_name for identity in visible_identities if identity.allows_asset
        ))
    else:
        # Legacy keeps its historical Bible-only reusable-asset policy.
        state.identity_character_names = [
            name for name in visible_character_names if name in bible_character_names
        ]


async def _prepare_manifest(state: _ReferenceBuildState, prompt_contract_changed: bool) -> None:
    """Resolve (or reuse a frozen) asset-dependency manifest for this shot."""
    from app.multiview import assert_manifest_allows_production

    reused = _try_reuse_frozen_manifest(state, prompt_contract_changed)
    if not reused:
        await _rebuild_manifest_with_asset_packs(state)

    # 兼容保留门禁报告 API，但阻塞项只写入告警，不终止付费链路。
    manifest_warnings = assert_manifest_allows_production(state.manifest)
    if manifest_warnings:
        state.existing_meta["asset_manifest_gate_retry_exhausted"] = True
        state.existing_meta["asset_manifest_warnings"] = list(manifest_warnings)


def _try_reuse_frozen_manifest(state: _ReferenceBuildState, prompt_contract_changed: bool) -> bool:
    """Reuse the frozen manifest when its asset revisions still match. Returns whether reused."""
    from app.multiview import manifest_revisions_match, resolve_shot_asset_dependencies

    # 冻结依赖 manifest：worker 重启复用；若本集人物/场景版本已变则判 stale 并重建
    frozen_manifest = state.existing_meta.get("reference_manifest")
    if prompt_contract_changed or not state.existing_meta.get("reference_manifest_frozen") or not isinstance(frozen_manifest, dict):
        return False
    current_probe = resolve_shot_asset_dependencies(
        project_id=state.project_id, episode_no=state.episode_no, shot_id=state.shot_id, shot=state.shot,
        scene_name=state.scene_name or None, conn=state.conn, bible=state.bible, screenplay=state.screenplay,
    )
    if not manifest_revisions_match(frozen_manifest, current_probe):
        state.existing_meta["reference_manifest_asset_stale"] = True
        state.existing_meta["reference_manifest_frozen"] = False
        # 人物/场景版本变更时，旧 passed 关键帧也已失效，不得换上新 manifest 继续复用。
        state.slot_state.clear()
        state.existing_meta["reference_slots"] = state.slot_state
        return False
    state.manifest = frozen_manifest
    return True


async def _rebuild_manifest_with_asset_packs(state: _ReferenceBuildState) -> None:
    """Complete any pending legacy asset packs, then resolve a fresh manifest."""
    from app.multiview import (
        character_multiview_enabled,
        complete_legacy_character_pack,
        complete_legacy_scene_pack,
        pack_result_ok,
        resolve_shot_asset_dependencies,
        scene_multiview_enabled,
    )

    # 进入本集生产前按需补齐 legacy_partial。补齐失败只留作
    # 风险证据；后续仍用已有主图/其他锨点，必要时回退纯文本视频。
    style = state.bible.world.visual_style_canonical
    pack_warnings: list[str] = []
    if character_multiview_enabled():
        for name in state.identity_character_names:
            pack = await complete_legacy_character_pack(state.project_id, name, state.episode_no, style)
            if pack is not None and not pack_result_ok(pack):
                pack_warnings.append(
                    f"人物多视角补齐重试耗尽：{name}"
                    f"（status={pack.get('status')}）"
                )
    if state.scene_name and scene_multiview_enabled():
        pack = await complete_legacy_scene_pack(state.project_id, state.scene_name, state.episode_no, style)
        if pack is not None and not pack_result_ok(pack):
            pack_warnings.append(
                f"场景多视角补齐重试耗尽：{state.scene_name}"
                f"（status={pack.get('status')}）"
            )
    if pack_warnings:
        state.existing_meta["asset_pack_gate_retry_exhausted"] = True
        state.existing_meta["asset_pack_warnings"] = pack_warnings
    state.manifest = resolve_shot_asset_dependencies(
        project_id=state.project_id, episode_no=state.episode_no, shot_id=state.shot_id, shot=state.shot,
        scene_name=state.scene_name or None, conn=state.conn, bible=state.bible, screenplay=state.screenplay,
    )
    state.existing_meta["reference_manifest"] = state.manifest
    state.existing_meta["reference_manifest_frozen"] = True


def _prepare_continuity_tail(state: _ReferenceBuildState) -> None:
    """Force in the previous shot's tail frame when this shot continues its action."""
    from app.multiview import PURPOSE_QA_ANCHOR, PURPOSE_VIDEO_INPUT
    from .asset_lookup import reference_image_path
    from .reference_generate import previous_tail_reference_asset

    # 旧执行入口同样服从同场景真实尾帧策略；孤立测试/兼容调用没有
    # prev_shot 且没有数据库连接时，不得凭 shot_no 猜测或伪造上游尾帧。
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    state.needs_tail = (
        not state.decision.shotPlanId
        and (state.prev_shot is not None or state.conn is not None)
        and uses_previous_tail_frame(derive_continuity_mode(state.shot, prev=state.prev_shot))
    )
    if state.needs_tail:
        prev = state.prev_shot
        if prev is None and int(getattr(state.shot, "shot_no", 0) or 0) > 1:
            prev = state.conn.execute(
                "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
                (state.episode_id, int(state.shot.shot_no) - 1)).fetchone()
        if prev is not None:
            ref_dir = reference_image_path(state.project_id, state.episode_no, state.shot.shot_no, "previous_shot_frame", 0).parent
            tail = previous_tail_reference_asset(state.conn, prev, dest_dir=ref_dir)
            if tail:
                tail.purposes = [PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR]
                tail.required = True
                tail.entity_type = "continuity"
                state.forced.append(tail)
            elif not state.allow_missing_continuity_tail:
                pass


def _prepare_evidence_assets(state: _ReferenceBuildState) -> None:
    """Assemble the reusable person/scene evidence gallery from the manifest."""
    from app.multiview import (
        PURPOSE_KEYFRAME_SEED,
        PURPOSE_QA_ANCHOR,
        library_anchor_assets_from_manifest,
        narrative_keyframe_required,
    )
    from .asset_lookup import _asset_from_path, character_reference_assets, scene_reference_assets
    from .mode_selection import REFERENCE_IMAGE_MODE, min_generated_references

    # 每镜必需 1 张叙事关键帧
    min_gen = max(min_generated_references(), 1 if narrative_keyframe_required() else 0)
    if state.decision.mode == REFERENCE_IMAGE_MODE:
        state.want_gen = max(int(state.plan.generateNewCount or 0), min_gen)
    else:
        state.want_gen = 0

    # 证据锚点（人物/场景多视角）进入画廊但不默认挤占 video_input 名额
    for anchor in library_anchor_assets_from_manifest(state.manifest):
        path = anchor.get("image_path")
        if not path or not Path(path).exists():
            continue
        try:
            state.evidence_assets.append(_asset_from_path(
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
        state.bible, getattr(state.shot, "scene_name", "") or "", project_id=state.project_id, episode_no=state.episode_no,
    )
    if not any(a.entity_type == "scene" for a in state.evidence_assets):
        state.evidence_assets.extend(scene_assets)
    char_assets = character_reference_assets(
        state.bible, state.identity_character_names, limit=max(1, len(state.identity_character_names)),
        project_id=state.project_id, episode_no=state.episode_no,
    )
    if not any(a.entity_type == "character" for a in state.evidence_assets):
        state.evidence_assets.extend(char_assets)
