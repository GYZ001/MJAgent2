"""Prompt resolution and seed-image assembly for the legacy per-shot
reference-asset build (see ``reference_generate_legacy.py``'s module
docstring for the full phase map): writing prompts for every spec not
already checkpointed (``_resolve_slot_prompts``), persisting the
``prompt_ready`` checkpoint (``_checkpoint_prompt_ready_slots``), and
assembling the portrait/environment seed images passed to every candidate
generation call (``_assemble_seed_images``). Moved verbatim out of the
pre-split single function -- only the wrapping into named phase functions,
and reading/writing through ``state`` instead of bare locals, is new.

``_resolve_slot_prompts`` ends by writing a new list to ``state.specs`` in
both its batch and per-slot-async branches -- a rebinding (the resolved
prompt text replacing the placeholder), not a mutation of the list read at
the top of the function, so both are explicit ``state.specs = ...``
assignments.
"""
from __future__ import annotations

import asyncio

from .keyframe_contract import _shot_for_keyframe_beat
from .mode_selection import (
    KEYFRAME_PROMPT_CONTRACT_VERSION,
    _screenplay_call_kwargs,
    batch_prompt_enabled,
    reference_prompt_async,
)
from .reference_generate import (
    _portrait_seed_inputs,
    write_reference_prompt,
    write_reference_prompt_batch,
)
from .reference_generate_legacy_state import _ReferenceBuildState


async def _resolve_slot_prompts(state: _ReferenceBuildState) -> None:
    """Write a prompt for every spec not already checkpointed with one."""
    prompts_to_write = [spec for spec in state.specs if spec[0] not in state.checkpointed_prompt_slots]
    if prompts_to_write and batch_prompt_enabled():
        prompts = await write_reference_prompt_batch(
            state.shot, state.bible, [(s, t) for s, t, _, _ in prompts_to_write],
            intents=[o for _, _, o, _ in prompts_to_write],
            beats=[state.beat_by_slot.get(s) for s, _, _, _ in prompts_to_write],
            **_screenplay_call_kwargs(state.screenplay),
        )
        written_by_slot = {
            prompts_to_write[i][0]: prompts[i] or prompts_to_write[i][2]
            for i in range(len(prompts_to_write))
        }
        state.specs = [
            (slot_key, ref_type, written_by_slot.get(slot_key, prompt), ordinal)
            for slot_key, ref_type, prompt, ordinal in state.specs
        ]
    elif prompts_to_write and reference_prompt_async():
        async def _resolve(slot_key: str, ref_type: str, brief: str | None) -> str | None:
            beat_shot = _shot_for_keyframe_beat(state.shot, state.beat_by_slot.get(slot_key))
            written = await write_reference_prompt(
                beat_shot, state.bible, ref_type, intent=brief,
                **_screenplay_call_kwargs(state.screenplay),
            )
            return written or brief or None
        resolved = await asyncio.gather(*[
            _resolve(slot_key, ref_type, brief)
            for slot_key, ref_type, brief, _ordinal in prompts_to_write
        ])
        written_by_slot = {
            prompts_to_write[i][0]: resolved[i] for i in range(len(prompts_to_write))
        }
        state.specs = [
            (slot_key, ref_type, written_by_slot.get(slot_key, prompt), ordinal)
            for slot_key, ref_type, prompt, ordinal in state.specs
        ]


def _checkpoint_prompt_ready_slots(state: _ReferenceBuildState) -> None:
    """Persist every spec's slot as ``prompt_ready`` (or ``generating_candidates``)."""
    for slot_key, ref_type, prompt, _ordinal in state.specs:
        state.slot_state[slot_key] = {
            **(state.slot_state.get(slot_key) or {}),
            "status": "generating_candidates" if state.candidate_pool.get(slot_key) else "prompt_ready",
            "type": ref_type,
            "prompt": prompt,
            "prompt_source": "llm_override" if prompt else "deterministic_template",
            "candidate_target": state.candidate_targets.get(slot_key, 1),
            "candidate_count": len(state.candidate_pool.get(slot_key, [])),
            "candidates": [
                state.candidate_record(slot_key, no, asset)
                for no, asset in state.candidate_pool.get(slot_key, [])
            ],
            "prompt_contract_version": KEYFRAME_PROMPT_CONTRACT_VERSION,
            "keyframe_contract_fingerprint": state.current_keyframe_fingerprint,
            **({"keyframe_beat": dict(state.beat_by_slot[slot_key])} if slot_key in state.beat_by_slot else {}),
        }
    if state.specs and state.existing_meta is not None:
        state.existing_meta["reference_slots"] = state.slot_state
        # prompt_ready 是恢复点：必须在调用图片供应商之前持久化。
        state.publish_progress()


def _assemble_seed_images(state: _ReferenceBuildState) -> None:
    """Assemble the portrait/environment seed images and their role-map note."""
    from app import hiagent
    from app.multiview import (
        PURPOSE_KEYFRAME_SEED,
        keyframe_seed_paths,
        library_anchor_assets_from_manifest,
    )

    # 关键帧种子：优先用本镜选中的人物/场景视角
    seed_paths = keyframe_seed_paths(state.manifest)
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
            state.bible, state.identity_character_names, project_id=state.project_id, episode_no=state.episode_no,
        )
    state.portrait_seeds = portrait_seeds
    env_seeds = [a.url for a in state.forced if a.type == "previous_shot_frame" and a.url]
    env_seeds += [a.url for a in state.evidence_assets if a.type == "scene" and a.url]
    state.env_seeds = env_seeds

    seed_order_lines: list[str] = []
    seed_anchor_by_path = {
        str(anchor.get("image_path") or ""): anchor
        for anchor in library_anchor_assets_from_manifest(state.manifest)
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
    state.seed_order_note = (
        "REFERENCE IMAGE ROLE MAP (match by input order; never blend identities): "
        + "; ".join(seed_order_lines)
        if seed_order_lines else None
    )
