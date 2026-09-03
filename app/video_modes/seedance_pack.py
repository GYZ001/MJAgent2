"""Seedance 供应商参考/视频输入打包、去重与 prompt 附注。"""
from __future__ import annotations


from pathlib import Path
from typing import Any

from app import hiagent
from app.hiagent import ProviderError
from .text_only_submission import empty_reference_submission
from app.video_plan import VideoInputIntent

from .keyframe_contract import is_narrative_keyframe_slot
from .mode_selection import (
    FIRST_FRAME_MODE,
    FIRST_LAST_FRAME_MODE,
    REFERENCE_IMAGE_MODE,
    ReferenceImageAsset,
    VIDEO_INPUT_MODE,
    _MAX_TIMELINE_KEYFRAMES,
    max_character_reference_images,
    max_reference_images,
)
from .reference_prompt import reference_gallery_matches_library_policy



def _reference_identity_names(ref: dict[str, Any]) -> set[str]:
    """返回参考图明确承载的具名人物身份。"""
    names = {
        str(name).strip()
        for name in (ref.get("relatedCharacterIds") or ref.get("related_character_ids") or [])
        if str(name).strip()
    }
    if str(ref.get("type") or "") == "character":
        entity_name = str(ref.get("entity_name") or "").strip()
        if entity_name:
            names.add(entity_name)
    return names


def pack_reference_images_for_seedance(
    refs: list[dict[str, Any]], *, max_images: int | None = None,
    continuity_required: bool = False,
    max_keyframes: int | None = None,
    required_identity_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """必需用途优先装箱；分数只在同类候选内排序。关键帧不会被高分定妆照挤掉。"""
    from app.multiview import pack_references_by_purpose
    usable = []
    seen_inputs: set[str] = set()
    for r in refs:
        # ``purposes`` describes what an asset was generated for and is retained
        # on rejected candidates for audit.  Only the explicit selection flag is
        # authoritative for the current provider request.
        if r.get("selectedForSeedance") and not r.get("deleted"):
            key = str(
                r.get("path")
                or r.get("image_path")
                or r.get("url")
                or r.get("id")
                or ""
            )
            if key and key in seen_inputs:
                continue
            if key:
                seen_inputs.add(key)
            usable.append(r)
    if not usable:
        return []
    # 最后一道污染防线：同一逻辑 slot 若被历史/手工 meta 误标了多个 winner，
    # 仍只取 QA 最高的一张；不同时序 slot 必须全部保留。无 slot 的旧数据视为同一组。
    def _score(ref: dict[str, Any]) -> tuple[float, int]:
        value = ref.get("qualityScore")
        if value is None and isinstance(ref.get("qa"), dict):
            value = ref["qa"].get("overall")
        try:
            numeric = float(value) if value is not None else float("-inf")
        except (TypeError, ValueError):
            numeric = float("-inf")
        try:
            candidate_no = int(ref.get("candidate_no") or 1)
        except (TypeError, ValueError):
            candidate_no = 1
        return (numeric, -candidate_no)

    keyframe_winners: dict[str, dict[str, Any]] = {}
    non_keyframes: list[dict[str, Any]] = []
    for ref in usable:
        if ref.get("type") != "plot_key_frame" and not is_narrative_keyframe_slot(ref.get("slot_key")):
            non_keyframes.append(ref)
            continue
        group_key = str(ref.get("slot_key") or "__legacy_narrative_keyframe__")
        current = keyframe_winners.get(group_key)
        if current is None or _score(ref) > _score(current):
            keyframe_winners[group_key] = ref
    timeline_winners = list(keyframe_winners.values())

    def _timeline_order(ref: dict[str, Any]) -> tuple[float, int]:
        try:
            ratio = float(ref.get("keyframe_time_ratio"))
        except (TypeError, ValueError):
            ratio = 1.0
        try:
            index = int(ref.get("keyframe_index") or 999)
        except (TypeError, ValueError):
            index = 999
        return ratio, index

    declared_totals: list[int] = []
    for ref in timeline_winners:
        try:
            declared_totals.append(int(ref.get("keyframe_total")))
        except (TypeError, ValueError):
            continue
    if max_keyframes is not None:
        keyframe_limit = max(1, min(int(max_keyframes), _MAX_TIMELINE_KEYFRAMES))
    else:
        keyframe_limit = 1 if declared_totals and max(declared_totals) <= 1 else _MAX_TIMELINE_KEYFRAMES
    if len(timeline_winners) > keyframe_limit:
        master = next(
            (ref for ref in timeline_winners if ref.get("slot_key") == "narrative_keyframe"),
            None,
        )
        chosen = [master] if master is not None else []
        for ref in sorted(timeline_winners, key=_timeline_order):
            if len(chosen) >= keyframe_limit:
                break
            if ref not in chosen:
                chosen.append(ref)
        timeline_winners = chosen
    timeline_winners.sort(key=_timeline_order)
    # Keep one explicit character-library anchor per identity even when a
    # narrative keyframe also contains that person. Seedance 2.0 can bind both
    # images to one named subject when the prompt declares the mapping; the
    # clean library image is the identity truth if the scene keyframe drifts.
    usable = non_keyframes + timeline_winners
    limit = max_images if max_images is not None else max_reference_images()
    distinct_character_identities = {
        str(ref.get("entity_name") or "").strip()
        or next(
            (
                str(name).strip()
                for name in (ref.get("relatedCharacterIds") or ref.get("related_character_ids") or [])
                if str(name).strip()
            ),
            "",
        )
        for ref in usable
        if str(ref.get("type") or "") == "character"
    }
    distinct_character_identities.discard("")
    char_limit = max(
        max_character_reference_images(), len(distinct_character_identities),
    )
    return pack_references_by_purpose(
        usable,
        max_images=limit,
        continuity_required=continuity_required,
        char_limit=char_limit,
        required_identity_names=required_identity_names,
    )


def dedupe_reference_dicts(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one persisted/provider record per physical reference input."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        key = str(
            ref.get("path")
            or ref.get("image_path")
            or ref.get("url")
            or ref.get("id")
            or ""
        )
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(ref)
    return out


def _dedupe_assets(assets: list[ReferenceImageAsset]) -> list[ReferenceImageAsset]:
    out: list[ReferenceImageAsset] = []
    seen: set[str] = set()
    for asset in assets:
        key = asset.path or asset.url or asset.id
        if key in seen:
            continue
        seen.add(key)
        out.append(asset)
    return out


REFERENCE_SINGLE_INSTANCE_NOTE = (
    " Reference images bind identity/environment only; each named character "
    "appears exactly once."
)
REFERENCE_PROMPT_NOTE_MARKER = " Use the provided reference images as follows: "


def append_reference_prompt_notes_from_dicts(
    prompt_text: str,
    packed_refs: list[dict[str, Any]],
    *,
    duration_s: float | int | None = None,
) -> str:
    """Bind each provider image to stable Seedance subject labels.

    Seedance 2.0's official guidance requires explicit ``image N -> subject``
    definitions and stable reuse of those subject labels. Asset IDs alone are
    not understood by the model.

    ``duration_s``：legacy single-shot callers already have ``--dur`` embedded
    in ``prompt_text`` by ``ensure_source_excerpt_in_prompt`` before this runs,
    so ``_split_video_args`` finds and preserves it even with no explicit
    duration passed here. 分镜台 2.0.0 段落绕过了那一步（会把模型写的换行压成
    空格，见 app.media_exec.run_job._run_job 的说明），它的 prompt_text 从没
    嵌过 ``--dur``，不传就会落到 ``_split_video_args`` 的兜底默认值
    ``config.DEFAULT_VIDEO_DURATION_S``（5 秒，给旧架构最短单镜头用的）而不是
    这一段真正的时长（15 秒）——实测复现：EP1 第 3 段 duration_s=15，不传
    duration_s 时最终提交给供应商的是 ``--dur 5``。调用方在有 shot 时必须把
    ``shot.duration_s`` 传进来。
    """
    from app.compiler import _split_video_args

    if REFERENCE_PROMPT_NOTE_MARKER in prompt_text:
        return prompt_text
    prompt_body, prompt_args = _split_video_args(prompt_text, duration_s)
    lines: list[str] = []
    for idx, ref in enumerate(packed_refs, 1):
        label = {
            "character": "character",
            "scene": "scene",
            "prop": "prop",
            "style": "style",
            "previous_shot_frame": "previous shot clean frame",
            "plot_key_frame": "plot key frame",
        }.get(str(ref.get("type") or "reference"), str(ref.get("type") or "reference"))
        related = [
            str(name).strip()
            for name in (
                ref.get("relatedCharacterIds")
                or ref.get("related_character_ids")
                or []
            )
            if str(name).strip()
        ]
        entity_name = str(ref.get("entity_name") or "").strip()
        if ref.get("type") == "character" and entity_name and entity_name not in related:
            related.append(entity_name)
        subject = f"「{'、'.join(related)}」" if related else ""
        timeline = ""
        if ref.get("type") == "plot_key_frame":
            target = str(ref.get("keyframe_target_desc") or "").strip()
            beat_index = ref.get("keyframe_index") or "?"
            beat_total = ref.get("keyframe_total") or "?"
            time_ratio = ref.get("keyframe_time_ratio")
            timing = ""
            try:
                timing = f"@{round(float(time_ratio) * 100)}%"
            except (TypeError, ValueError):
                pass
            timeline = f"; beat {beat_index}/{beat_total}{timing}"
            if target:
                timeline += f"; target: {target}"
        lines.append(
            f"Reference image {idx}: use as {label}{subject}; "
            f"identity/appearance only{timeline}."
        )
    if not lines:
        return prompt_text
    note = (
        REFERENCE_PROMPT_NOTE_MARKER
        + " ".join(lines)
        + REFERENCE_SINGLE_INSTANCE_NOTE
    )
    if prompt_body.startswith("subject_definitions:\n"):
        heading, body = prompt_body.split("\n", 1)
        return (
            heading
            + "\n"
            + " ".join(lines)
            + " "
            + REFERENCE_SINGLE_INSTANCE_NOTE.strip()
            + "\n"
            + body
            + prompt_args
        )
    return prompt_body + note + prompt_args


def append_reference_prompt_notes(
    prompt_text: str,
    assets: list[ReferenceImageAsset],
    *,
    required_identity_names: list[str] | None = None,
    duration_s: float | int | None = None,
) -> str:
    # Notes and provider inputs must use the exact same packed order.
    packed_refs = pack_reference_images_for_seedance(
        [asset.public_dict() for asset in assets],
        required_identity_names=required_identity_names,
    )
    return append_reference_prompt_notes_from_dicts(
        prompt_text, packed_refs, duration_s=duration_s,
    )


def _reference_input_label(ref: dict[str, Any], role: str) -> dict[str, Any]:
    """构造一张参考图的可展示标注：谁（角色/场景）、什么用途，不含像素数据。

    观测台链路详情不能把 base64 图片原样塞进 JSON 视图（动辄 1MB+），但用户
    要看清"每张图绑的是谁"——这个标注就是那份轻量元数据，随 provider_calls.meta
    一起落库，和巨大的 request_json 图片字节完全分开存放。
    """
    ref_type = str(ref.get("type") or "").strip()
    entity_name = str(ref.get("entity_name") or "").strip()
    related = [
        str(name).strip()
        for name in (ref.get("relatedCharacterIds") or ref.get("related_character_ids") or [])
        if str(name).strip()
    ]
    if ref_type == "character" and entity_name:
        label = f"角色参考 · {entity_name}"
    elif ref_type == "scene" and entity_name:
        label = f"场景参考 · {entity_name}"
    elif ref_type == "plot_key_frame":
        who = "、".join(related) if related else entity_name
        label = f"关键帧 · {who}" if who else "关键帧（未标注人物）"
    elif entity_name:
        label = entity_name
    else:
        label = "参考图（未标注身份）"
    return {
        "role": role,
        "type": ref_type or None,
        "entity_name": entity_name or None,
        "related_character_ids": related,
        "slot_key": ref.get("slot_key"),
        "label": label,
    }


_CONTINUITY_FRAME_LABELS = {
    "first_frame": "衔接首帧（上一镜尾帧）",
    "last_frame": "衔接尾帧",
}


def build_seedance_image_inputs(meta: dict[str, Any]) -> list[tuple[str, str]]:
    mode = meta.get("mode") or REFERENCE_IMAGE_MODE
    if mode == REFERENCE_IMAGE_MODE:
        if (
            meta.get("first_frame_path")
            or meta.get("last_frame_path")
            or meta.get("video_input_url")
        ):
            raise ProviderError(
                "REFERENCE_IMAGE_MODE 不能混入 first_frame、last_frame 或 reference_video"
            )
        refs = meta.get("reference_images") or []
        if not refs:
            return empty_reference_submission(meta)
        if not reference_gallery_matches_library_policy(meta):
            raise ProviderError(
                "REFERENCE_IMAGE_MODE 只允许人物谱与场景库中的现有图片"
            )
        # 使用中的图按综合分 Top-N 装箱；截断不改 selected，高分未入选仍留在画廊。
        sequence = meta.get("keyframe_sequence") or {}
        beats = sequence.get("beats") if isinstance(sequence, dict) else None
        keyframe_limit = len(beats) if isinstance(beats, list) and beats else _MAX_TIMELINE_KEYFRAMES
        required_identities = [
            str(name).strip()
            for name in (meta.get("required_reference_characters") or [])
            if str(name).strip()
        ]
        usable = pack_reference_images_for_seedance(
            refs,
            max_keyframes=keyframe_limit,
            required_identity_names=required_identities,
        )
        if not usable:
            raise ProviderError(
                "REFERENCE_IMAGE_MODE 没有可提交的 reference_image"
            )
        covered_identities = set().union(*(
            _reference_identity_names(ref)
            for ref in usable
        ))
        missing_identities = [
            name for name in required_identities
            if name not in covered_identities
        ]
        if missing_identities:
            raise ProviderError(
                "REFERENCE_IMAGE_MODE 缺少必需人物身份参考图："
                + "、".join(missing_identities)
            )
        # 场景不像人物身份那样硬拦截（同一段可以声明多个转场场景，挤不下
        # 时该丢谁本来就该由 Seedance 张数上限的装箱优先级决定，不该整段
        # 直接失败）。但"声明过、最终没挂上"不能沉默——按用户既定方向做成
        # 可见的降级标记，写回 meta 供观测台/前端展示，不拦截生产。
        declared_scene_names = {
            name
            for ref in refs
            if str(ref.get("type") or "") == "scene"
            and ref.get("selectedForSeedance")
            and not ref.get("deleted")
            for name in (str(ref.get("entity_name") or "").strip(),)
            if name
        }
        covered_scene_names = {
            name
            for ref in usable
            if str(ref.get("type") or "") == "scene"
            for name in (str(ref.get("entity_name") or "").strip(),)
            if name
        }
        dropped_scenes = sorted(declared_scene_names - covered_scene_names)
        if dropped_scenes:
            meta["_seedance_scene_reference_degraded"] = dropped_scenes
        out: list[tuple[str, str]] = []
        labels: list[dict[str, Any]] = []
        for ref in usable:
            if ref.get("path"):
                out.append((hiagent.data_url_from_file(ref["path"]), "reference_image"))
            elif ref.get("url"):
                out.append((ref["url"], "reference_image"))
            else:
                continue
            labels.append(_reference_input_label(ref, "reference_image"))
        if not out:
            raise ProviderError(
                "REFERENCE_IMAGE_MODE 的 reference_image 文件或 URL 不可用"
            )
        meta["_seedance_image_input_labels"] = labels
        return out

    if mode == FIRST_FRAME_MODE:
        if meta.get("reference_images") or meta.get("video_input_url") or meta.get("last_frame_path"):
            raise ProviderError(
                "FIRST_FRAME_MODE 只能使用上一视频尾帧作为 first_frame"
            )
        first = str(meta.get("first_frame_path") or meta.get("first_frame_url") or "").strip()
        if not first:
            raise ProviderError("FIRST_FRAME_MODE 缺少 first_frame")

        meta["_seedance_image_input_labels"] = [
            {"role": "first_frame", "type": "continuity_frame", "entity_name": None,
             "related_character_ids": [], "slot_key": None,
             "label": _CONTINUITY_FRAME_LABELS["first_frame"]},
        ]
        if first.startswith(("data:", "http://", "https://")):
            return [(first, "first_frame")]
        path = Path(first)
        if not path.is_file():
            raise ProviderError(f"首帧文件不存在：{first}")
        return [(hiagent.data_url_from_file(first), "first_frame")]

    if mode == FIRST_LAST_FRAME_MODE:
        if meta.get("reference_images") or meta.get("video_input_url"):
            raise ProviderError(
                "FIRST_LAST_FRAME_MODE 不能混入 reference_image 或 reference_video"
            )
        first = str(meta.get("first_frame_path") or meta.get("first_frame_url") or "").strip()
        last = str(meta.get("last_frame_path") or meta.get("last_frame_url") or "").strip()
        if not first or not last:
            raise ProviderError("FIRST_LAST_FRAME_MODE 必须同时提供 first_frame 和 last_frame")

        def _resolve(value: str) -> str:
            if value.startswith(("data:", "http://", "https://")):
                return value
            path = Path(value)
            if not path.is_file():
                raise ProviderError(f"首尾帧文件不存在：{value}")
            return hiagent.data_url_from_file(value)

        meta["_seedance_image_input_labels"] = [
            {"role": role, "type": "continuity_frame", "entity_name": None,
             "related_character_ids": [], "slot_key": None,
             "label": _CONTINUITY_FRAME_LABELS[role]}
            for role in ("first_frame", "last_frame")
        ]
        return [(_resolve(first), "first_frame"), (_resolve(last), "last_frame")]

    if mode == VIDEO_INPUT_MODE:
        if (
            meta.get("reference_images")
            or meta.get("first_frame_path")
            or meta.get("last_frame_path")
            or meta.get("first_frame_url")
            or meta.get("last_frame_url")
        ):
            raise ProviderError(
                "VIDEO_INPUT_MODE 不能混入 reference_image、first_frame 或 last_frame"
            )
        return []

    raise ProviderError(f"未知视频生成模式：{mode}")


def build_seedance_video_inputs(meta: dict[str, Any]) -> list[tuple[str, str]]:
    mode = meta.get("mode") or REFERENCE_IMAGE_MODE
    if mode != VIDEO_INPUT_MODE:
        if meta.get("video_input_url"):
            raise ProviderError(f"{mode} 不能携带 reference_video")
        return []
    url = str(meta.get("video_input_url") or "").strip()
    if not url:
        raise ProviderError("VIDEO_INPUT_MODE 缺少供应商可访问的 reference_video URL")
    if url.startswith("data:"):
        raise ProviderError("reference_video 必须是 Web URL，禁止提交 data URL")
    if not url.startswith(("http://", "https://")):
        raise ProviderError("reference_video 必须是 http(s) Web URL")
    try:
        VideoInputIntent(str(meta.get("video_input_intent") or ""))
    except ValueError as exc:
        raise ProviderError("VIDEO_INPUT_MODE 缺少合法 video_input_intent") from exc
    return [(url, "reference_video")]
