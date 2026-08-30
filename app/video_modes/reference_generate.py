"""单张参考图生成调用：seed 回退、尾帧提取与批量 prompt 写入。"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess

from pathlib import Path
from typing import Any

from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.db import new_id
from app.errors import code_ref
from app.harness import model_gateway
from app.hiagent import ProviderError
from app.schemas import Bible, EpisodeScreenplay, Shot, extract_json

from .asset_lookup import _asset_from_path, _safe_ref_name, reference_image_path
from .keyframe_contract import (
    _keyframe_character_anchors,
    _keyframe_contract,
    _keyframe_text_instruction,
    _shot_for_keyframe_beat,
)
from .mode_selection import (
    KEYFRAME_PROMPT_CONTRACT_VERSION,
    ReferenceImageAsset,
    _KEYFRAME_LLM_PROMPT_MAX_CHARS,
    _MULTI_KEYFRAME_INVARIANCE_NOTE,
    _screenplay_call_kwargs,
)
from .reference_prompt import reference_generation_prompt



_SEED_USAGE_NOTE = (
    " Reference images lock identity, outfit, style, and environment only—not pose, framing, camera, or physical "
    "height. Each character image is one separate named identity; never merge, swap, omit, or duplicate identities. "
    "Ignore crop-size differences and follow the mandatory action/geometry contract."
)


async def _generate_image_with_seed_fallback(prompt: str, seed_inputs: list[str] | None, *,
                                             call_meta: dict | None = None) -> dict[str, Any]:
    """Generate with the complete seed contract; never drop identity inputs."""
    return await hiagent.generate_image(
        prompt,
        size=config.REF_IMAGE_SIZE,
        image_inputs=seed_inputs or None,
        call_meta=call_meta,
    )



async def _generate_one_reference(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                  ref_type: str, index: int, content_override: str | None = None,
                                  seed_inputs: list[str] | None = None,
                                  extra_instruction: str | None = None,
                                  skip_inline_qa: bool = False,
                                  screenplay: EpisodeScreenplay | None = None) -> ReferenceImageAsset:
    from app.continuity import effective_characters_visible

    prompt = reference_generation_prompt(
        shot,
        bible,
        ref_type,
        index,
        content_override=content_override,
        screenplay=screenplay,
        identity_seeded=bool(seed_inputs),
    )
    if seed_inputs:
        prompt += _SEED_USAGE_NOTE
    if extra_instruction:
        instruction = extra_instruction.strip()
        if seed_inputs:
            seed_names = list(dict.fromkeys([
                *_keyframe_character_anchors(
                    shot,
                    bible,
                    screenplay=screenplay,
                ),
                *[
                    str(character.name).strip()
                    for character in bible.characters
                    if str(character.name).strip()
                ],
            ]))
            aliases = {
                name: f"subject {position}"
                for position, name in enumerate(seed_names, start=1)
            }
            for name in sorted(aliases, key=len, reverse=True):
                instruction = instruction.replace(name, aliases[name])
        prompt += " " + instruction
    # 每次生成使用独立文件名：合同升级/并发重抽不得覆盖历史版本已引用的字节。
    base_dest = reference_image_path(project_id, episode_no, shot.shot_no, ref_type, index)
    artifact_token = _safe_ref_name(new_id("img"))
    dest = base_dest.with_name(f"{base_dest.stem}_{artifact_token}{base_dest.suffix}")
    item = await _generate_image_with_seed_fallback(
        prompt,
        seed_inputs,
        call_meta={
            "asset_kind": "reference_image",
            "episode_no": episode_no,
            "shot_no": shot.shot_no,
            "reference_type": ref_type,
            "reference_index": index,
            "artifact_token": artifact_token,
        })
    if item.get("url"):
        await hiagent.download(item["url"], str(dest))
    elif item.get("b64_json"):
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise ProviderError(f"Reference image response missing url/b64_json: {list(item.keys())}")
    # VLM 图片质检已下线：技术产物（文件）落盘即可用，不再跑单图或批量评分。
    # ``skip_inline_qa`` 形参不再改变行为，保留仅为调用方兼容。
    del skip_inline_qa
    qa = {"overall": 1.0, "issues": []}
    asset = _asset_from_path(
        path=str(dest),
        ref_type=ref_type,
        source="seedream_generated",
        quality_score=1.0,
        qa=qa,
        related_character_ids=(
            effective_characters_visible(shot) if ref_type in {"character", "plot_key_frame"} else []
        ),
    )
    asset.selectedForSeedance = True
    asset.rejectReason = None
    return asset


def _extract_last_frame(video_path: str, dest: Path) -> bool:
    """用 ffmpeg 抽取视频最后一帧到 dest。成功返回 True。"""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return False
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, check=True).stdout.strip() or 0)
        if dur <= 0:
            return False
        ts = max(0.0, dur - 0.1)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ts:.2f}", "-i", video_path,
             "-vframes", "1", "-q:v", "3", str(dest)],
            check=True, capture_output=True)
        return dest.exists()
    except (subprocess.SubprocessError, ValueError, OSError):
        return False


def previous_tail_source_contract(conn: Any, prev_shot: Any) -> dict[str, Any] | None:
    """冻结上一镜实际采用成片，用于检测重抽/重新采用后的尾帧过期。"""
    if prev_shot is None:
        return None

    def _g(key: str) -> Any:
        if hasattr(prev_shot, "keys"):
            return prev_shot[key] if key in prev_shot.keys() else None
        return prev_shot.get(key)

    prev_id = _g("id")
    adopted = _g("adopted_version_id")
    if not adopted:
        return None
    v = conn.execute(
        "SELECT video_path FROM shot_versions WHERE id=? AND status='succeeded'", (adopted,),
    ).fetchone()
    if not v or not v["video_path"]:
        return None
    video_path = Path(v["video_path"])
    if not video_path.is_file():
        return None
    try:
        stat = video_path.stat()
    except OSError:
        return None
    return {
        "shot_id": prev_id,
        "adopted_version_id": adopted,
        "video_path": str(video_path.resolve()),
        "video_size": stat.st_size,
        "video_mtime_ns": stat.st_mtime_ns,
    }


def previous_tail_reference_asset(conn: Any, prev_shot: Any, *, dest_dir: Path) -> ReferenceImageAsset | None:
    """从上一镜实际采用成片抽尾帧，作为连续镜的参考图锚点。"""
    source_contract = previous_tail_source_contract(conn, prev_shot)
    if source_contract:
        signature = hashlib.sha256(
            json.dumps(source_contract, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"00_previous_tail_{signature}.jpg"
        if (dest.is_file() and dest.stat().st_size > 0) or _extract_last_frame(
            source_contract["video_path"], dest,
        ):
            asset = _asset_from_path(
                path=str(dest), ref_type="previous_shot_frame", source="previous_shot",
                shot_id=source_contract["shot_id"], quality_score=1.0,
                qa={"overall": 1.0, "issues": ["forced_continuity"]},
            )
            asset.dependency_manifest = {"continuity_source": source_contract}
            return asset
    return None


async def write_reference_prompt(
    shot: Shot,
    bible: Bible,
    ref_type: str,
    *,
    intent: str | None = None,
    screenplay: EpisodeScreenplay | None = None,
) -> str:
    """为【单张】新参考图独立写一条详尽的 Seedream 英文提示词（一图一次 LLM 调用）。
    逐图独立调用 + 上游并发，避免一次性写多张时模型偷懒只给空泛短提示。失败返回空串（上游回退模板）。"""
    anchors = _keyframe_character_anchors(shot, bible, screenplay=screenplay)
    contract = _keyframe_contract(shot, bible, screenplay=screenplay)
    payload = {
        "task": (
            "Write ONE concrete English image-generation prompt for ONE narrative keyframe still. "
            "When intent is non-empty it is the authoritative timeline instant and overrides the generic "
            "target_keyframe_desc; otherwise render target_keyframe_desc. Render one frozen instant, never a sequence, montage, "
            "before/after composite, neutral lineup, or character sheet. Describe each visible subject's exact "
            "pose, orientation, expression, interaction point, framing, lighting, and background."
        ),
        "reference_type": ref_type,
        "intent": intent or "",
        "shot": {
            "scene_setting": shot.scene_setting,
            "visible_characters": list(contract.get("visible_characters") or []),
            "character_appearance": anchors,
            "target_keyframe_desc": contract.get("target_keyframe_desc"),
            "action_context": shot.primary_action or shot.action_desc,
            "shot_size": shot.shot_size,
            "camera_angle": contract.get("camera_angle"),
            "spatial_anchor": contract.get("spatial_anchor"),
            "scene_canonical": contract.get("scene_canonical"),
            "scene_landmarks": contract.get("scene_landmarks"),
            "scene_geometry_contract": contract.get("scene_geometry_contract"),
            "contact_required": contract.get("contact_required"),
            "established_contact_required": contract.get("established_contact_required"),
            "relative_height_policy": contract.get("relative_height_policy"),
            "height_difference_evidence": contract.get("height_difference_evidence"),
            "dialogues": [d.model_dump() if hasattr(d, "model_dump") else dict(d) for d in shot.dialogues],
        },
        "geometry_contract": contract,
        "style": bible.world.visual_style_canonical,
        "constraints": [
            "English only", "9:16 portrait", _keyframe_text_instruction(shot, contract), "no watermark/logo",
            "no extra limbs, no motion blur", "single coherent still image",
            "keep character face/hair/clothing exactly as character_appearance",
            _MULTI_KEYFRAME_INVARIANCE_NOTE,
            "show each individual_visible_characters entry exactly once; render collective_visible_roles with the "
            "multiplicity required by target_keyframe_desc; do not omit functional extras",
            "obey geometry_contract exactly; the deterministic provider suffix will enforce it again",
        ],
        "policy_version": KEYFRAME_PROMPT_CONTRACT_VERSION,
        "output_schema": {"prompt": "the full English image prompt, one paragraph"},
    }
    try:
        raw = await model_gateway.chat([
            {"role": "system", "content": "Return exactly one JSON object with a single 'prompt' string field. English only."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], temperature=0.2, max_tokens=700,
            call_meta={"initiator_label": "参考图提示词生成", "reference_type": ref_type, "shot_no": shot.shot_no})
        data = extract_json(raw)
        return str(data.get("prompt") or "").strip()[:_KEYFRAME_LLM_PROMPT_MAX_CHARS]
    except Exception:
        return ""


_SLOT_ROLE_CYCLE = [
    ("narrative_keyframe", "plot_key_frame"),
]


async def write_reference_prompt_batch(
    shot: Shot,
    bible: Bible,
    slots: list[tuple[str, str]],
    *,
    intents: list[str | None] | None = None,
    beats: list[dict[str, Any] | None] | None = None,
    screenplay: EpisodeScreenplay | None = None,
) -> list[str]:
    """一镜一次返回全部槽位提示词合同（P1）。缺项/重复时仅对异常槽回退单图调用。"""
    anchors = _keyframe_character_anchors(shot, bible, screenplay=screenplay)
    planned = []
    slot_shots: list[Shot] = []
    for i, (slot_key, ref_type) in enumerate(slots):
        beat = beats[i] if beats and i < len(beats) else None
        slot_shot = _shot_for_keyframe_beat(shot, beat)
        slot_contract = _keyframe_contract(
            slot_shot, bible, screenplay=screenplay,
        )
        slot_shots.append(slot_shot)
        planned.append({
            "slot": slot_key,
            "type": ref_type,
            "intent": (intents[i] if intents and i < len(intents) else None) or "",
            "shot": {
                "target_keyframe_desc": slot_contract.get("target_keyframe_desc"),
                "action_context": slot_shot.primary_action or slot_shot.action_desc,
                "camera_angle": slot_contract.get("camera_angle"),
                "contact_required": slot_contract.get("contact_required"),
                "established_contact_required": slot_contract.get("established_contact_required"),
                "relative_height_policy": slot_contract.get("relative_height_policy"),
                "height_difference_evidence": slot_contract.get("height_difference_evidence"),
            },
            "geometry_contract": slot_contract,
            "text_constraint": _keyframe_text_instruction(slot_shot, slot_contract),
        })
    payload = {
        "task": (
            "Return exactly the planned slots below, with one concrete English image-generation prompt per slot. "
            "Do not add, rename, or omit slots. Each slot's shot, geometry_contract, text_constraint, and non-empty "
            "intent are authoritative for that slot. Make the slots visibly different moments "
            "of one chronological shot. Render ONE frozen instant per image; never blend timeline beats, the first "
            "and last frame, or fall back to a neutral lineup."
        ),
        "slots": planned,
        "shot": {
            "scene_setting": shot.scene_setting,
            "visible_characters": list(
                _keyframe_contract(shot, bible, screenplay=screenplay).get("visible_characters") or []
            ),
            "character_appearance": anchors,
            "shot_size": shot.shot_size,
            "spatial_anchor": shot.spatial_anchor,
        },
        "style": bible.world.visual_style_canonical,
        "constraints": [
            "English only", "9:16 portrait", "no watermark/logo",
            "no spoilers for later shots", "show every individual visible identity exactly once; render collective "
            "roles as groups with the target-described multiplicity",
            _MULTI_KEYFRAME_INVARIANCE_NOTE,
            "obey each slot's own geometry_contract and text_constraint exactly; do not restate the full policy verbatim",
        ],
        "policy_version": KEYFRAME_PROMPT_CONTRACT_VERSION,
        "output_schema": {
            "slots": [
                {"slot": item["slot"], "type": item["type"], "prompt": "full English prompt"}
                for item in planned
            ],
        },
    }
    prompts: list[str] = [""] * len(slots)
    try:
        raw = await model_gateway.chat([
            {"role": "system", "content": "Return exactly one JSON object with a 'slots' array. English prompts only."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], temperature=0.3, max_tokens=1600,
            call_meta={"initiator_label": "参考图批量提示词合同", "shot_no": shot.shot_no, "slot_count": len(slots)})
        data = extract_json(raw)
        by_slot = {}
        for item in data.get("slots") or []:
            key = str(item.get("slot") or "")
            prompt = str(item.get("prompt") or "").strip()[:_KEYFRAME_LLM_PROMPT_MAX_CHARS]
            if key and prompt:
                by_slot[key] = prompt
        for i, (slot_key, _) in enumerate(slots):
            prompts[i] = by_slot.get(slot_key, "")
    except Exception:
        pass
    # 缺项定向修复
    for i, (slot_key, ref_type) in enumerate(slots):
        if prompts[i]:
            continue
        intent = intents[i] if intents and i < len(intents) else None
        prompts[i] = await write_reference_prompt(
            slot_shots[i], bible, ref_type, intent=intent,
            **_screenplay_call_kwargs(screenplay),
        )
    # 近重复检测：若两槽文本高度相似，重写后者
    for i in range(len(prompts)):
        for j in range(i):
            a, b = prompts[i], prompts[j]
            if a and b and a.lower()[:80] == b.lower()[:80]:
                original_intent = intents[i] if intents and i < len(intents) else ""
                prompts[i] = await write_reference_prompt(
                    slot_shots[i],
                    bible,
                    slots[i][1],
                    intent=(
                        f"{original_intent} Make this frozen instant visibly distinct from slot {slots[j][0]}."
                    ),
                    **_screenplay_call_kwargs(screenplay),
                ) or prompts[i]
    return prompts


async def _generate_reference_keep_best(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                        ref_type: str, index: int, content_override: str | None,
                                        retries: int, seed_inputs: list[str] | None = None,
                                        extra_instruction: str | None = None,
                                        skip_inline_qa: bool = False,
                                        screenplay: EpisodeScreenplay | None = None) -> tuple[ReferenceImageAsset | None, list[ReferenceImageAsset], list[dict[str, Any]]]:
    """生成单张参考图；技术产物存在即可用，不再有"不达标重试"这回事。

    VLM 图片质检已下线：``_generate_one_reference`` 现在生成成功即
    ``selectedForSeedance=True``，所以这里第一次尝试成功就直接返回；
    重试循环只在生成本身抛异常（供应商失败）时才会用到。``retries``/
    ``skip_inline_qa`` 形参保留仅为调用方兼容。
    """
    rejections: list[dict[str, Any]] = []
    for attempt in range(retries + 1):
        attempt_index = index * 100 + attempt
        try:
            asset = await _generate_one_reference(
                project_id=project_id, episode_no=episode_no, shot=shot, bible=bible,
                ref_type=ref_type, index=attempt_index, content_override=content_override,
                seed_inputs=seed_inputs, extra_instruction=extra_instruction,
                skip_inline_qa=skip_inline_qa,
                **_screenplay_call_kwargs(screenplay))
        except Exception as exc:
            rejections.append({"type": ref_type, "source": "seedream_generated",
                               "reason": "参考图生成异常" + code_ref(
                                   exc, action="generate_reference_image",
                                   context={"project_id": project_id, "episode_no": episode_no,
                                            "shot_id": getattr(shot, "id", None), "ref_type": ref_type})})
            continue
        return asset, [], rejections
    return None, [], rejections


def _portrait_seed_inputs(bible: Bible, character_names: list[str], *, project_id: str | None,
                          episode_no: int | None, limit: int = 2) -> list[str]:
    """出场角色定妆照的 data URL，作为新参考图的 i2i 种子（锁长相/发型/服饰，姿态仍走文字）。
    用 refs.refs_as_image_inputs 走「按集分段定妆照」选版，与喂给 Seedance 的人物锚点同源。"""
    from app.refs import refs_as_image_inputs
    return [url for url, _ in refs_as_image_inputs(
        bible, list(character_names), max(limit, 0), project_id=project_id, episode_no=episode_no)]
