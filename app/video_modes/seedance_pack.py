"""Seedance 供应商参考/视频输入打包、去重与 prompt 附注。"""
from __future__ import annotations


from pathlib import Path
from typing import Any

from app import hiagent
from app.hiagent import ProviderError
from .text_only_submission import empty_reference_submission
from app.video_plan import VideoInputIntent

from .mode_selection import (
    FIRST_FRAME_MODE,
    FIRST_LAST_FRAME_MODE,
    REFERENCE_IMAGE_MODE,
    ReferenceImageAsset,
    VIDEO_INPUT_MODE,
    _MAX_TIMELINE_KEYFRAMES,
)
from .reference_prompt import reference_gallery_matches_library_policy
from .seedance_reference_notes import (
    REFERENCE_PROMPT_NOTE_MARKER as REFERENCE_PROMPT_NOTE_MARKER,
    REFERENCE_SINGLE_INSTANCE_NOTE as REFERENCE_SINGLE_INSTANCE_NOTE,
    build_seedance_reference_prompt_notes,
)



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
    """必需用途优先装箱；分数只在同类候选内排序。关键帧不会被高分定妆照挤掉。

    去重/关键帧分组/角色配额预处理搬到 ``reference_packing_preview.py``（见该
    模块 docstring 的搬家理由），供 ``app.video_modes.prop_composite_pack``
    复用同一份而不必重新发明——超限时探测"道具会被丢弃"必须与这里用同一套
    排序，否则两边判断可能对不上。
    """
    from app.multiview import pack_references_by_purpose  # 只有本函数这一处用到，不提到模块顶层常驻（既有写法，原样保留）

    from .reference_packing_preview import prepare_usable_refs_and_limits  # 同上：只有这一处用到，不提到模块顶层常驻

    usable, limit, char_limit = prepare_usable_refs_and_limits(
        refs, max_images=max_images, max_keyframes=max_keyframes,
    )
    if not usable:
        return []
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


def append_reference_prompt_notes_from_dicts(
    prompt_text: str,
    packed_refs: list[dict[str, Any]],
    *,
    duration_s: float | int | None = None, aspect_ratio: str,
) -> str:
    """Bind each provider image to a stable ``@图片N`` Seedance subject label
    and append a Chinese purpose note after the prompt body (2026-09-03 起，
    对齐 Seedance 2.0 官方指南：用编号引用图片、说明语言与正文一致）。

    实现搬到 ``app.video_modes.seedance_reference_notes``（该模块的拆分背景、
    duration_s 与新增 aspect_ratio 语义见那边的 docstring；本文件已在
    line_count 棘轮基线里、零余量，新逻辑不能再往这加）。
    """
    return build_seedance_reference_prompt_notes(
        prompt_text, packed_refs, duration_s=duration_s, aspect_ratio=aspect_ratio,
    )


def append_reference_prompt_notes(
    prompt_text: str,
    assets: list[ReferenceImageAsset],
    *,
    required_identity_names: list[str] | None = None,
    duration_s: float | int | None = None, aspect_ratio: str,
) -> str:
    # Notes and provider inputs must use the exact same packed order.
    packed_refs = pack_reference_images_for_seedance(
        [asset.public_dict() for asset in assets],
        required_identity_names=required_identity_names,
    )
    return append_reference_prompt_notes_from_dicts(
        prompt_text, packed_refs, duration_s=duration_s, aspect_ratio=aspect_ratio,
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
    composite_labels = [
        str(name).strip() for name in (ref.get("composite_member_labels") or []) if str(name).strip()
    ]
    if ref_type == "prop" and ref.get("view_role") == "prop_composite" and composite_labels:
        label = f"道具拼图 · {'、'.join(composite_labels)}"
    elif ref_type == "character" and entity_name:
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


def _manifest_ready_prop_labels(meta: dict[str, Any]) -> set[str]:
    """本段 ``reference_manifest`` 里声明且 ready 的道具 label——装配阶段
    （``app.video_modes.reference_assemble.select_library_references``）超限
    截断掉的道具没有 ``selectedForSeedance`` 痕迹可查（它的条目从一开始就是
    ``False``，不是"声明后被撤销"），只能从这份冻结 manifest 回溯；与下面
    ``_dropped_prop_labels`` 靠 ``selectedForSeedance`` 算出的 ``declared``
    取并集，两层截断（装配阶段 / 供应商提交前的 ``ref_pack_priority``）都要
    能被同一个信号看见，不能只盯住后一层。"""
    manifest = meta.get("reference_manifest")
    props = manifest.get("props") if isinstance(manifest, dict) else None
    if not isinstance(props, list):
        return set()
    return {
        str(p.get("label") or "").strip()
        for p in props
        if isinstance(p, dict) and p.get("ready") and str(p.get("label") or "").strip()
    }


def _dropped_prop_labels(
    meta: dict[str, Any], refs: list[dict[str, Any]], usable: list[dict[str, Any]],
) -> set[str]:
    """声明过（``selectedForSeedance``）或 manifest 里 ready 却最终没有以任何
    形式送达的道具 label——与 ``dropped_scenes`` 同一可见信号取舍，props 多
    两层：被 ``app.video_modes.prop_composite_pack`` 合成进拼图的道具原条目
    会被标成 ``selectedForSeedance=False``（不再"声明"），但它的 label 仍经
    拼图条目的 ``composite_member_labels`` 算作"覆盖"，不会被误报成丢弃；装配
    阶段截断掉的道具靠 ``_manifest_ready_prop_labels`` 补上（见该函数）。"""
    declared = {
        str(ref.get("entity_name") or "").strip()
        for ref in refs
        if str(ref.get("type") or "") == "prop"
        and ref.get("selectedForSeedance")
        and not ref.get("deleted")
        and ref.get("view_role") != "prop_composite"  # 拼图自身是合成产物，不是原本声明的道具 label
        and str(ref.get("entity_name") or "").strip()
    } | _manifest_ready_prop_labels(meta)
    covered: set[str] = set()
    for ref in usable:
        if str(ref.get("type") or "") != "prop":
            continue
        if ref.get("view_role") == "prop_composite":
            covered.update(
                str(name).strip()
                for name in (ref.get("composite_member_labels") or [])
                if str(name).strip()
            )
            continue
        name = str(ref.get("entity_name") or "").strip()
        if name:
            covered.add(name)
    return declared - covered


def _record_reference_degradations(
    meta: dict[str, Any], refs: list[dict[str, Any]], usable: list[dict[str, Any]],
) -> None:
    """声明过（``selectedForSeedance``）却最终没能以任何形式送达的场景/道具
    label 写成可见信号，不拦截生产——与原 ``dropped_scenes`` 分支同一取舍，
    只是把场景与道具两条并到一处，给 ``build_seedance_image_inputs`` 腾行数。"""
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
    dropped_props = sorted(_dropped_prop_labels(meta, refs, usable))
    if dropped_props:
        meta["_seedance_prop_reference_degraded"] = dropped_props


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
        # 场景/道具都不像人物身份那样硬拦截（场景可以声明多个转场场景、道具
        # 超限会被装箱优先级挤掉，挤不下该丢谁本来就该由装箱规则决定，不该
        # 整段直接失败）。但"声明过、最终没挂上"不能沉默——按用户既定方向
        # 做成可见的降级标记，写回 meta 供观测台/前端展示，不拦截生产。
        _record_reference_degradations(meta, refs, usable)
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
