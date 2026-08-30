"""参考图生成 prompt 组装与既有图库是否满足关键帧契约的判定。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.refs import visual_style_lock
from app.schemas import Bible, EpisodeScreenplay, Shot

from .keyframe_contract import (
    _keyframe_character_anchors,
    _keyframe_contract,
    _keyframe_contract_instructions,
    _keyframe_text_instruction,
)
from .mode_selection import (
    KEYFRAME_PROMPT_CONTRACT_VERSION,
    KEYFRAME_STRUCTURAL_FALLBACK_MODE,
    REFERENCE_INPUT_POLICY_VERSION,
    _KEYFRAME_LLM_PROMPT_MAX_CHARS,
    _MAX_TIMELINE_KEYFRAMES,
    _SHORT_SHOT_MAX_SECONDS,
)



def reference_gallery_matches_keyframe_contract(
    meta: dict[str, Any], *, expected_fingerprint: str | None = None,
) -> bool:
    """旧画廊只有在关键帧提示词合同一致时才能自动复用。

    人工编辑过的画廊代表明确用户选择，保留它而不自动覆盖。
    """
    refs = [ref for ref in (meta.get("reference_images") or []) if isinstance(ref, dict)]

    def _technically_usable(ref: dict[str, Any]) -> bool:
        path = str(ref.get("path") or ref.get("image_path") or "").strip()
        url = str(ref.get("url") or "").strip()
        if path:
            try:
                return Path(path).is_file() and Path(path).stat().st_size > 0
            except OSError:
                return False
        return url.startswith("data:image/")

    selected_refs = [
        ref for ref in refs if ref.get("selectedForSeedance") and not ref.get("deleted")
    ]
    if any(not _technically_usable(ref) for ref in selected_refs):
        return False

    fallback_slots = {
        str(slot or "").strip()
        for slot in (meta.get("keyframe_structural_fallback_slots") or [])
        if str(slot or "").strip()
    }
    structural_fallback = (
        meta.get("keyframe_fallback_mode") == KEYFRAME_STRUCTURAL_FALLBACK_MODE
        and bool(fallback_slots)
    )
    has_keyframe = any(
        str(ref.get("type") or "") == "plot_key_frame"
        and not ref.get("deleted")
        and bool(ref.get("selectedForSeedance"))
        and _technically_usable(ref)
        for ref in refs
    )
    if not has_keyframe:
        from app.multiview import narrative_keyframe_required

        if not structural_fallback:
            return not narrative_keyframe_required()
        # 结构硬伤候选已全部物理删除时，允许画廊只保留固定人物/场景锚点。
        # 仍要求至少有一张可用默认锚点，避免将空画廊伪装成合法降级。
        if not any(ref.get("type") in {"character", "scene"} for ref in selected_refs):
            return False
    sequence = meta.get("keyframe_sequence")
    selected_keyframes = [
        ref for ref in selected_refs
        if ref.get("type") == "plot_key_frame" and _technically_usable(ref)
    ]
    if len(selected_keyframes) > _MAX_TIMELINE_KEYFRAMES:
        return False
    if isinstance(sequence, dict) and isinstance(sequence.get("beats"), list):
        beats = [beat for beat in sequence["beats"] if isinstance(beat, dict)]
        if not (1 <= len(beats) <= _MAX_TIMELINE_KEYFRAMES):
            return False
        keyframe_plan = sequence.get("keyframe_plan") or {}
        try:
            duration_s = float(keyframe_plan.get("duration_s"))
        except (TypeError, ValueError):
            duration_s = None
        if duration_s is not None and duration_s <= _SHORT_SHOT_MAX_SECONDS and len(selected_keyframes) > 1:
            return False
        expected_slots = {
            str(beat.get("slot_key") or "")
            for beat in beats
            if str(beat.get("slot_key") or "")
        }
        selected_slots = {
            str(ref.get("slot_key") or "")
            for ref in selected_refs
            if ref.get("type") == "plot_key_frame" and _technically_usable(ref)
        }
        if structural_fallback and not fallback_slots.issubset(expected_slots):
            return False
        required_slots = expected_slots - fallback_slots if structural_fallback else expected_slots
        if required_slots and not required_slots.issubset(selected_slots):
            return False
        if len(selected_slots) > _MAX_TIMELINE_KEYFRAMES:
            return False
    if meta.get("reference_gallery_contract_override"):
        return True
    if str(meta.get("keyframe_prompt_contract_version") or "") != KEYFRAME_PROMPT_CONTRACT_VERSION:
        return False
    if expected_fingerprint:
        frozen_fingerprint = str(meta.get("keyframe_contract_fingerprint") or "").strip()
        if not frozen_fingerprint:
            frozen_fingerprint = next(
                (
                    str(ref.get("keyframe_contract_fingerprint") or "").strip()
                    for ref in refs
                    if ref.get("type") == "plot_key_frame" and ref.get("keyframe_contract_fingerprint")
                ),
                "",
            )
        return frozen_fingerprint == expected_fingerprint
    return True


def reference_gallery_matches_library_policy(meta: dict[str, Any]) -> bool:
    """Only selected, readable character/scene-library assets may reach video input."""
    if meta.get("reference_input_policy_version") != REFERENCE_INPUT_POLICY_VERSION:
        return False
    selected = [
        ref for ref in (meta.get("reference_images") or [])
        if isinstance(ref, dict)
        and ref.get("selectedForSeedance")
        and not ref.get("deleted")
    ]
    if not selected:
        return False
    for ref in selected:
        entity_type = str(ref.get("entity_type") or ref.get("type") or "")
        if ref.get("source") != "asset_library" or entity_type not in {"character", "scene"}:
            return False
        path = str(ref.get("path") or ref.get("image_path") or "").strip()
        url = str(ref.get("url") or "").strip()
        try:
            usable = bool(path and Path(path).is_file() and Path(path).stat().st_size > 0)
        except OSError:
            usable = False
        if not usable and not url.startswith("data:image/"):
            return False
    return True


def _seeded_structured_endpoint(
    shot: Shot,
    contract: dict[str, object],
    provider_aliases: dict[str, str],
) -> str:
    """Compile a seeded endpoint from typed state instead of free narrative prose."""

    phase = next(
        (
            str(tag).split(":", 1)[1]
            for tag in (shot.risk_tags or [])
            if str(tag).startswith("timeline_keyframe_phase:")
        ),
        "",
    )
    if phase and phase not in {"decisive", "closing"}:
        return ""
    if not phase and str(contract.get("target_source") or "") not in {
        "last_frame_desc",
        "state_out",
    }:
        return ""

    def provider_text(value: object) -> str:
        normalized = str(value or "").strip()
        for name in sorted(provider_aliases, key=len, reverse=True):
            normalized = normalized.replace(name, provider_aliases[name])
        return normalized

    state = shot.continuity_state_out
    character_states = getattr(state, "characters", {}) or {}
    visible = [
        str(name).strip()
        for name in (contract.get("visible_characters") or [])
        if str(name).strip()
    ]
    character_parts: list[str] = []
    for name in visible:
        item = character_states.get(name)
        if item is None:
            continue
        details = [
            f"{label}={provider_text(getattr(item, field, ''))}"
            for label, field in (
                ("pose", "pose"),
                ("facing", "facing"),
                ("left hand", "left_hand"),
                ("right hand", "right_hand"),
            )
            if provider_text(getattr(item, field, ""))
        ]
        if details:
            character_parts.append(
                f"{provider_aliases.get(name, name)} endpoint: " + "; ".join(details)
            )
    if visible and not character_parts:
        return ""

    prop_parts: list[str] = []
    for item in (getattr(state, "props", {}) or {}).values():
        if not bool(getattr(item, "required", False)) and (
            str(getattr(item, "visibility", "") or "") != "required"
        ):
            continue
        details = [
            f"{label}={provider_text(getattr(item, field, ''))}"
            for label, field in (
                ("name", "canonical_name"),
                ("location", "location"),
                ("state", "form"),
            )
            if provider_text(getattr(item, field, ""))
        ]
        if details:
            prop_parts.append("required prop: " + "; ".join(details))

    parts = [*character_parts, *prop_parts]
    if not parts:
        return ""
    return (
        "Literal endpoint geometry for the scripted practical action: "
        + ". ".join(parts)
        + ". Show only these visible states and do not infer unlisted interaction."
    )


def _photographic_medium_instruction(visual_style_canonical: str) -> str:
    """English-language rendering-medium clause for Seedance reference/keyframe prompts.

    Historically this was a blanket "stay non-photorealistic / anime proportions"
    instruction, written back when every visual style preset was CG-only. That
    directly contradicts the photo-realistic presets (真人摄影风/精修真人风) added
    later: it would silently repaint an already-photographic seeded reference back
    into a cartoon look. Branch on the resolved style instead of hardcoding one
    medium for every project.
    """
    from app.visual_styles import is_photographic_style_prompt
    if is_photographic_style_prompt(visual_style_canonical):
        return (
            "Keep the image fully photographic and photorealistic, matching the "
            "reference images' real-camera look; never switch to a cartoon, anime, "
            "or CG-rendered medium."
        )
    return (
        "Keep the image fully non-live-action and non-photorealistic; never "
        "switch to a real-person photo look."
    )


def reference_generation_prompt(
    shot: Shot,
    bible: Bible,
    ref_type: str,
    index: int,
    *,
    content_override: str | None = None,
    screenplay: EpisodeScreenplay | None = None,
    identity_seeded: bool = False,
) -> str:
    anchors = _keyframe_character_anchors(shot, bible, screenplay=screenplay)
    provider_names = list(dict.fromkeys([
        *anchors,
        *[
            str(character.name).strip()
            for character in bible.characters
            if str(character.name).strip()
        ],
    ]))
    provider_aliases = (
        {
            name: f"subject {position}"
            for position, name in enumerate(provider_names, start=1)
        }
        if identity_seeded else {}
    )

    def provider_text(value: str) -> str:
        normalized = str(value or "")
        for name in sorted(provider_aliases, key=len, reverse=True):
            normalized = normalized.replace(name, provider_aliases[name])
        return normalized

    anchor_text = "; ".join(
        (
            f"{provider_aliases.get(name, name)}: identity, face, body build, "
            "and outfit are locked by "
            "the provided reference images"
        )
        if identity_seeded else f"{name}: {appearance}"
        for name, appearance in anchors.items()
    )
    # content_override：LLM 按剧本写的内容提示词。它只能补充美术细节，不能覆盖下方
    # 确定性构图合同。最终合同在截断后追加，避免第二人物/接触点被截掉。
    if content_override:
        body = provider_text(
            content_override.strip()[:_KEYFRAME_LLM_PROMPT_MAX_CHARS]
        )
    else:
        contract = _keyframe_contract(shot, bible, screenplay=screenplay)
        body = provider_text(
            f"Create one clean 9:16 anime-drama reference image for Seedance. "
            f"Reference type: {ref_type}. Shot {shot.shot_no}. Scene: {shot.scene_setting}. "
            f"Single narrative keyframe target: {contract['target_keyframe_desc']}."
        )
    style_contract = visual_style_lock(bible.world.visual_style_canonical)
    if identity_seeded and ref_type == "plot_key_frame":
        contract = _keyframe_contract(shot, bible, screenplay=screenplay)
        target = _seeded_structured_endpoint(
            shot,
            contract,
            provider_aliases,
        ) or provider_text(str(
            contract.get("target_keyframe_desc")
            or shot.last_frame_desc
            or shot.action_desc
        ).strip())
        camera_angle = str(contract.get("camera_angle") or "eye-level").strip()
        visible = [
            provider_aliases.get(str(name).strip(), str(name).strip())
            for name in (contract.get("visible_characters") or [])
            if str(name).strip()
        ]
        scene_canonical = provider_text(
            str(contract.get("scene_canonical") or "").strip()
        )
        scene_landmarks = [
            str(item).strip()
            for item in (contract.get("scene_landmarks") or [])
            if str(item).strip()
        ]
        dialogue_focus = str(
            contract.get("dialogue_focus_subject") or ""
        ).strip()
        dialogue_focus = provider_aliases.get(dialogue_focus, dialogue_focus)
        compact_contract = [
            "Create one clean 9:16 portrait narrative keyframe.",
            "SEEDED KEYFRAME CONTRACT:",
            f"Freeze exactly one final instant: {target}.",
            f"Composition: {shot.shot_size}; camera: {camera_angle}; "
            "preserve the scripted screen direction and scene axis.",
            (
                "Visible named identities, each exactly once: "
                + ", ".join(visible)
                + ". No additional recognizable person."
                if visible else
                "No recognizable person is required in frame."
            ),
            (
                "Scene geometry: " + scene_canonical
                + (
                    "; fixed landmarks: " + ", ".join(scene_landmarks)
                    if scene_landmarks else ""
                )
                + "."
                if scene_canonical or scene_landmarks else ""
            ),
            (
                f"Dialogue framing: only {dialogue_focus} is visible; "
                "the listener remains fully offscreen."
                if dialogue_focus else ""
            ),
            (
                "SIDE CAMERA REQUIRED: preserve the side interaction axis and "
                "keep the interaction zone unobstructed."
                if contract.get("contact_camera_required") else ""
            ),
            (
                "Show the exact established contact point."
                if contract.get("established_contact_required") else (
                    "Keep the subjects visibly separated."
                    if contract.get("target_contact_phase") == "separated"
                    else (
                        "Keep the scripted approach phase without inventing contact."
                        if contract.get("target_contact_phase") == "approach"
                        else ""
                    )
                )
            ),
            (
                "Reference images are authoritative for identity, outfit, "
                "proportions, environment, and visual style."
            ),
            (
                _photographic_medium_instruction(bible.world.visual_style_canonical)
            ),
            (
                "Render the target as one physically continuous progression "
                "from reference image 1; no cut, morph, duplicate, or identity swap."
            ),
            _keyframe_text_instruction(shot, contract),
            "Clean 9:16 portrait still; no watermark or malformed anatomy.",
            f"Policy version: {KEYFRAME_PROMPT_CONTRACT_VERSION}.",
        ]
        return " ".join(part for part in compact_contract if part)
    common = (
        f"{body} Characters: {anchor_text or '(no visible character)'}. "
        "Episode style: "
        + (
            "locked by the provided reference images. "
            if identity_seeded
            else f"{bible.world.visual_style_canonical}. Style lock: {style_contract}. "
        )
    )
    if ref_type != "plot_key_frame":
        return (
            common
            + _photographic_medium_instruction(bible.world.visual_style_canonical) + " "
            + "No text, no subtitles, no watermark, no logo, no extra limbs, no motion blur. 9:16 portrait. "
            "The image must be suitable as a Seedance 2.0 reference image."
        )
    mandatory = " ".join(
        _keyframe_contract_instructions(shot, bible, screenplay=screenplay)
    )
    return f"{common}{mandatory} Policy version: {KEYFRAME_PROMPT_CONTRACT_VERSION}."


# i2i 种子使用守则：参考图只锁「身份/服饰/环境」，姿态构图一律走文字——否则图生图会照搬
# 种子的站姿/构图，导致同镜多张雷同、且照搬定妆照站姿（见 worker.py:355 关键帧系统的同款教训）。
