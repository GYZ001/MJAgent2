"""关键帧契约：叙事关键帧拍点、结构锚点与关键帧指纹。"""
from __future__ import annotations

import hashlib
import json

from typing import Any

from app.schemas import Bible, EpisodeScreenplay, Shot

from .mode_selection import (
    KEYFRAME_PROMPT_CONTRACT_VERSION,
    _MULTI_KEYFRAME_INVARIANCE_NOTE,
)



def _keyframe_character_anchors(
    shot: Shot,
    bible: Bible,
    *,
    screenplay: EpisodeScreenplay | None = None,
) -> dict[str, str]:
    """关键帧的可见人物锚点；功能性路人不能因为没有人物谱资产而被漏掉。"""
    from app.continuity import effective_characters_visible

    if screenplay is not None and screenplay.narrative_plan is not None:
        from app.identity_contracts import narrative_identity_resolver

        resolver = narrative_identity_resolver(bible, screenplay)
        return {
            name: resolver.visual_anchor(name)
            for name in effective_characters_visible(shot)
        }
    from app.character_policy import (
        collective_role_anchor,
        functional_extra_anchor,
        is_collective_role,
        is_functional_extra,
        typed_functional_identity_names,
    )

    by_name = {c.name: c for c in bible.characters}
    declared_functional_names = typed_functional_identity_names(screenplay)
    anchors: dict[str, str] = {}
    for name in effective_characters_visible(shot):
        character = by_name.get(name)
        if character is not None:
            anchors[name] = character.appearance_canonical
        elif is_collective_role(name):
            anchors[name] = collective_role_anchor(name)
        elif is_functional_extra(name) or name in declared_functional_names:
            anchors[name] = functional_extra_anchor(
                name,
                declared_functional_names=declared_functional_names,
            )
        else:
            # 上游编译门禁会拦截未知具名角色；这里仍保留可见名单，
            # 防止历史分镜在图片边界被静默省略。
            anchors[name] = f'visible character "{name}"; keep one stable, distinct identity in this shot'
    return anchors


def _keyframe_contract(
    shot: Shot,
    bible: Bible | None,
    *,
    screenplay: EpisodeScreenplay | None = None,
) -> dict[str, Any]:
    from app.compiler import keyframe_visual_contract

    return keyframe_visual_contract(shot, bible, screenplay=screenplay)


def is_narrative_keyframe_slot(slot_key: str | None) -> bool:
    """识别决定性 master 槽与同镜的其他时序节拍槽。"""
    from app.multiview import NARRATIVE_KEYFRAME_SLOT

    value = str(slot_key or "")
    return value == NARRATIVE_KEYFRAME_SLOT or value.startswith(f"{NARRATIVE_KEYFRAME_SLOT}_")


def keyframe_contract_fingerprint(
    shot: Shot,
    bible: Bible,
    *,
    screenplay: EpisodeScreenplay | None = None,
) -> str:
    """冻结一张叙事关键帧的镜头级语义。

    全局 policy version 只能表示“规则代码没变”；动作、机位、可见人物或必需文字
    变化后，即使定妆照/场景版本相同，旧图也不能复用。
    """
    required_text = getattr(shot, "required_text", None)
    required_text_payload = (
        required_text.model_dump(mode="json")
        if required_text is not None and hasattr(required_text, "model_dump")
        else None
    )
    dialogue_payload = [
        dialogue.model_dump(mode="json")
        if hasattr(dialogue, "model_dump")
        else (dict(dialogue) if isinstance(dialogue, dict) else str(dialogue))
        for dialogue in (getattr(shot, "dialogues", None) or [])
    ]
    payload = {
        "policy_version": KEYFRAME_PROMPT_CONTRACT_VERSION,
        "geometry": _keyframe_contract(shot, bible, screenplay=screenplay),
        "scene_setting": (getattr(shot, "scene_setting", "") or "").strip(),
        "shot_size": (getattr(shot, "shot_size", "") or "").strip(),
        "camera_move": (getattr(shot, "camera_move", "") or "").strip(),
        "story_context": {
            "primary_action": (getattr(shot, "primary_action", "") or "").strip(),
            "action_desc": (getattr(shot, "action_desc", "") or "").strip(),
            "emotion_beat": (getattr(shot, "emotion_beat", "") or "").strip(),
            "narration": (getattr(shot, "narration", "") or "").strip(),
            "dialogues": dialogue_payload,
        },
        "required_text": required_text_payload,
        "visual_style": (getattr(getattr(bible, "world", None), "visual_style_canonical", "") or "").strip(),
        "character_anchors": _keyframe_character_anchors(
            shot, bible, screenplay=screenplay,
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _keyframe_contract_instructions(
    shot: Shot,
    bible: Bible,
    *,
    screenplay: EpisodeScreenplay | None = None,
) -> list[str]:
    """最终发给图片模型的硬约束；必须放在 LLM 文案之后，以覆盖冲突描述。"""
    contract = _keyframe_contract(shot, bible, screenplay=screenplay)
    target = str(contract["target_keyframe_desc"] or "the shot's decisive action").strip()
    camera_angle = str(contract["camera_angle"] or "eye-level").strip()
    visible = [str(name) for name in (contract.get("visible_characters") or []) if str(name).strip()]
    lines = [
        "MANDATORY KEYFRAME CONTRACT (overrides conflicts above):",
        f"ONE frozen instant only — {target}",
        f"Shot: {shot.shot_size}, camera '{camera_angle}', preserve the scripted scene axis; no montage, endpoint blend, "
        "or neutral character-sheet pose.",
    ]
    scene_canonical = str(contract.get("scene_canonical") or "").strip()
    scene_landmarks = [
        str(item).strip() for item in (contract.get("scene_landmarks") or []) if str(item).strip()
    ]
    if scene_canonical or scene_landmarks:
        lines.append(
            "FIXED SCENE GEOMETRY: preserve the canonical set layout and every visible permanent landmark exactly; "
            "never delete, duplicate, morph, resize, or relocate a stele, gate, table, screen, or other fixed prop. "
            + (f"Canonical scene: {scene_canonical}. " if scene_canonical else "")
            + (f"Explicit landmarks: {', '.join(scene_landmarks)}." if scene_landmarks else "")
        )
    individual = [str(name) for name in (contract.get("individual_visible_characters") or []) if str(name).strip()]
    collective = [str(name) for name in (contract.get("collective_visible_roles") or []) if str(name).strip()]
    if individual:
        lines.append(
            "Named/individual visible identities, each exactly once: " + ", ".join(individual)
            + ". No omission, duplicate, swap, or identity merge."
        )
    dialogue_focus = str(contract.get("dialogue_focus_subject") or "").strip()
    if dialogue_focus:
        lines.append(
            f"SPEAKER CLOSE-UP HARD CONTRACT: '{dialogue_focus}' is the ONLY visible person. "
            "Use a vertical close-up or medium close-up with a clear face, natural speaking mouth, and eyeline toward "
            "an offscreen listener. Every listener, other speaker, crowd member, shoulder, back, reflection, silhouette, "
            "and blurred face stays completely out of frame. No two-shot, group lineup, or crowd composition."
        )
    if contract.get("collective_presence_forbidden"):
        lines.append(
            "NO CROWD IN FRAME: the target explicitly places the crowd offscreen, dispersed, departed, or only in "
            "memory. Render no crowd members and do not turn offscreen voices into visible people."
        )
    elif collective:
        lines.append(
            "Scripted collective/group roles: " + ", ".join(collective)
            + ". Render each as the target-described group and multiplicity, never as one fixed identity; the group may "
            "be foreground or secondary exactly as the target requires, and its members must not copy a named face."
        )
    elif contract.get("collective_presence_required"):
        lines.append(
            "A scripted anonymous crowd is REQUIRED at exactly the target-described prominence and multiplicity; "
            "it must not replace, duplicate, or resemble a named identity."
        )
    elif contract.get("anonymous_background_allowed"):
        lines.append(
            "A scripted anonymous crowd is allowed at exactly the target-described prominence and multiplicity; "
            "it must not replace, duplicate, or resemble a named identity."
        )
    elif visible:
        lines.append("No additional recognizable person.")
    if contract.get("contact_camera_required"):
        lines.append(
            "SIDE CAMERA REQUIRED: place the camera on the side of the interaction axis so the interaction zone is "
            "unobstructed; never a frontal lineup. Bodies/faces may turn naturally three-quarter for identity."
        )
    if contract.get("established_contact_required"):
        lines.append(
            "The target moment has established physical contact: show the exact touch/hold/impact point clearly, "
            "with anatomically connected limbs and no floating hand or gap."
        )
    elif contract.get("target_contact_phase") == "separated":
        lines.append(
            "The target moment is after release/separation: keep a clearly visible gap and the released hand/body "
            "state; do not reconnect the subjects."
        )
    elif contract.get("target_contact_phase") == "approach":
        lines.append(
            "Preserve the target's approach/near-contact phase exactly; do not invent a touch, catch, or impact that "
            "has not happened yet."
        )
    elif contract.get("contact_axis_inherited"):
        lines.append(
            "This timeline waypoint inherits the contact shot's side camera axis, but this frozen target does not "
            "declare a contact phase. Preserve only the contact or gap explicitly visible in the target; do not invent it."
        )
    height_policy = contract.get("relative_height_policy")
    if height_policy == "equal_scale":
        lines.extend([
            "STRICT EQUAL-HEIGHT CONTRACT: co-present teen/adult characters have the same canonical upright standing height, "
            "head-to-body ratio, and body scale unless the script explicitly states a difference; this is an approximately "
            "equal upright standing-height baseline with only small natural tolerance.",
            "When both are standing, place their supporting feet on the same ground/depth plane and align head-top, shoulder, "
            "hip, and eye-line baselines within a small natural tolerance. Do not make either character child-sized, taller, "
            "shorter, foreground-giant, or background-miniature.",
            "Words such as look up, look down, raise the head, or lower the head describe only eye/head/neck direction; they "
            "never authorize a height difference. Separate reference-image crop size is identity evidence, never physical height.",
        ])
    elif height_policy == "preserve_explicit_difference":
        evidence = "; ".join(
            str(item).strip() for item in (contract.get("height_difference_evidence") or []) if str(item).strip()
        )
        lines.append(
            "Preserve only the relative height difference explicitly stated by the story/character anchors"
            + (f" (exact evidence: {evidence})" if evidence else "")
            + "; do not exaggerate it with wide-angle, foreground/background placement, or forced perspective."
        )
    lines.append(
        "Use natural perspective and physically coherent human scale throughout; never infer physical height from a "
        "reference image's crop or subject size."
    )
    lines.append(
        "Preserve the named character's natural canonical head-to-body ratio even in a close-up; never enlarge the "
        "physical head, make the body childlike/chibi, or infer anatomy from the reference crop size."
    )
    lines.append(
        "Keep each face, hairstyle, outfit, age, and build faithful to its named anchor. Clean 9:16 portrait still; "
        + _keyframe_text_instruction(shot, contract)
        + " No watermark/logo, motion blur, malformed hands, or extra limbs."
    )
    lines.append(_MULTI_KEYFRAME_INVARIANCE_NOTE)
    return lines


def _keyframe_text_instruction(shot: Shot, contract: dict[str, Any]) -> str:
    required = getattr(shot, "required_text", None)
    exact = str(getattr(required, "exact_text", "") or "").strip() if required is not None else ""
    if not exact or not contract.get("required_text_expected"):
        return "no text or subtitles."
    surface = str(getattr(required, "surface", "") or "the specified story surface").strip()
    style = str(getattr(required, "style", "") or "clear and legible").strip()
    return (
        f"the only permitted text is the exact string '{exact}' on {surface}, rendered {style}; "
        "no other text or subtitles."
    )
