from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.harness import model_gateway
from app.db import get_setting, new_id
from app.errors import code_ref
from app.hiagent import ProviderError
from app.schemas import Bible, Shot, extract_json

REFERENCE_IMAGE_MODE = "REFERENCE_IMAGE_MODE"
VideoGenerationMode = Literal["REFERENCE_IMAGE_MODE"]

REFERENCE_IMAGE_TYPES = {
    "character",
    "scene",
    "prop",
    "style",
    "previous_shot_frame",
    "plot_key_frame",
}


@dataclass
class ReferenceImagePlan:
    totalCount: int = 1
    reusePreviousSceneCount: int = 0
    generateNewCount: int = 1
    types: list[str] = field(default_factory=lambda: ["plot_key_frame"])
    # 模型按剧本/分镜为每张「新生成」参考图给出的提示词，元素形如 {"type": str, "prompt": str}。
    # 为空时回退到 reference_generation_prompt 的模板提示词。
    prompts: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ShotVideoModeDecision:
    mode: VideoGenerationMode
    reason: str
    confidence: float
    needReusePreviousScene: bool = False
    needGenerateNewReferences: bool = False
    referenceImagePlan: ReferenceImagePlan = field(default_factory=ReferenceImagePlan)
    llmUsed: bool = False
    defaulted: bool = False


@dataclass
class ReferenceImageAsset:
    id: str
    url: str
    type: str
    source: str
    path: str | None = None
    shotId: str | None = None
    episodeId: str | None = None
    sceneId: str | None = None
    relatedCharacterIds: list[str] = field(default_factory=list)
    qualityScore: float | None = None
    selectedForSeedance: bool = False
    rejectReason: str | None = None
    qa: dict[str, Any] | None = None
    deleted: bool = False  # 用户在素材画廊里手动废弃 → 不再喂给模型
    # 多视角 / 用途 / 库资产溯源
    entity_type: str | None = None
    entity_name: str | None = None
    library_revision_id: str | None = None
    library_view_id: str | None = None
    view_role: str | None = None
    purposes: list[str] = field(default_factory=list)
    required: bool = False
    slot_key: str | None = None
    dependency_manifest: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # data URL 只用于当次供应商调用。图片已经落盘后，持久化路径即可；
        # 把 base64 写进 shot_versions.image_inputs 会令每次重抽成倍膨胀数据库。
        if data.get("path"):
            data.pop("url", None)
        return data


def bool_setting(key: str, default: bool) -> bool:
    value = (get_setting(key) or str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


def int_setting(key: str, default: int) -> int:
    try:
        return int(get_setting(key) or default)
    except (TypeError, ValueError):
        return default


def float_setting(key: str, default: float) -> float:
    try:
        return float(get_setting(key) or default)
    except (TypeError, ValueError):
        return default


def max_reference_images() -> int:
    # Keep the default at 8, but allow deployments that have verified a 9-image limit to opt in.
    return max(1, min(int_setting("video_reference_max_images", 8), 9))


def quality_threshold() -> float:
    """综合 QA 分门禁：≥此分必须留在「使用中」。默认 0.8。"""
    return float_setting("video_reference_quality_threshold", 0.8)


def quality_floor() -> float:
    """兜底图质量地板：生成图全不达标时，最佳一版仍低于此分则不喂模型——此时定妆照/场景锚点已能锁身份与环境，
    一张带水印/畸形的脏图当参考反而拖累成片。介于地板与阈值之间才作兜底喂入。"""
    return float_setting("video_reference_quality_floor", 0.4)


# 综合分权重：绝对质检与相对一致性；冗余只做软惩罚，不单独硬剔。
_SCORE_W_ABS = 0.55
_SCORE_W_CONS = 0.35
_SCORE_W_SUM = _SCORE_W_ABS + _SCORE_W_CONS  # 0.90
_MAX_REDUNDANCY_PENALTY = 0.15
_HARD_FAILURE_MULTIPLIER = 0.3


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def compose_reference_score(*, absolute_quality: float, consistency: float = 1.0,
                            redundancy_penalty: float = 0.0,
                            hard_failures: list[Any] | None = None) -> dict[str, Any]:
    """把绝对质检、相对一致性、人物冗余软惩罚与硬伤乘数合成一张 overall。

    overall = hard_multiplier * absolute * (w_abs + w_cons * consistency) / (w_abs + w_cons)
              - redundancy_penalty

    consistency=1 时 overall==absolute（未测一致性不抬分）；consistency 下降只降分不抬分。
    """
    abs_q = _clamp01(absolute_quality)
    cons = _clamp01(consistency)
    hard_list = [x for x in (hard_failures or []) if str(x).strip()]
    hard_mult = _HARD_FAILURE_MULTIPLIER if hard_list else 1.0
    # 一致性作为绝对分的折扣因子，避免「未测一致性默认 1.0」把低绝对分抬过门禁
    weighted = abs_q * (_SCORE_W_ABS + _SCORE_W_CONS * cons) / _SCORE_W_SUM
    penalty = _clamp01(min(_MAX_REDUNDANCY_PENALTY, max(0.0, float(redundancy_penalty or 0.0))))
    overall = _clamp01(hard_mult * weighted - penalty)
    return {
        "overall": round(overall, 3),
        "absolute_quality": round(abs_q, 3),
        "consistency": round(cons, 3),
        "redundancy_penalty": round(penalty, 3),
        "hard_multiplier": hard_mult,
        "hard_failures": hard_list,
    }


def _absolute_quality_of(asset: ReferenceImageAsset) -> float:
    qa = asset.qa or {}
    if qa.get("absolute_quality") is not None:
        try:
            return _clamp01(float(qa["absolute_quality"]))
        except (TypeError, ValueError):
            pass
    if asset.qualityScore is not None and qa.get("consistency") is None and qa.get("redundancy_penalty") is None:
        # 尚未合成过：qualityScore 即绝对分
        try:
            return _clamp01(float(asset.qualityScore))
        except (TypeError, ValueError):
            pass
    if qa.get("overall") is not None and qa.get("absolute_quality") is None and qa.get("consistency") is None:
        try:
            return _clamp01(float(qa["overall"]))
        except (TypeError, ValueError):
            pass
    try:
        return _clamp01(float(asset.qualityScore or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _consistency_of(asset: ReferenceImageAsset) -> float:
    qa = asset.qa or {}
    if qa.get("consistency") is not None:
        try:
            return _clamp01(float(qa["consistency"]))
        except (TypeError, ValueError):
            return 1.0
    return 1.0


def _hard_failures_of(asset: ReferenceImageAsset) -> list[Any]:
    qa = asset.qa or {}
    raw = qa.get("hard_failures")
    if isinstance(raw, list) and raw:
        items = [str(x) for x in raw if str(x).strip()]
    else:
        issues = qa.get("issues") or []
        if not isinstance(issues, list):
            return []
        # 仅把明显硬伤词视为一票否决因子；普通 issues 不压 hard_multiplier
        markers = ("broken", "畸形", "extra limb", "多余肢体", "severe_anatomy",
                   "wrong_identity", "duplicate_character", "action_missing")
        items = [x for x in issues if any(m in str(x).lower() for m in markers)]
    from app.multiview import watermark_qa_mode
    if watermark_qa_mode() == "ignore_unless_occluding":
        cleaned = []
        for item in items:
            s = str(item).lower()
            if "watermark" in s or "水印" in s or s in {"logo", "字幕", "subtitle"}:
                if "occlusion" in s or "遮挡" in s or "subject_occlusion" in s:
                    cleaned.append("subject_occlusion")
                continue
            cleaned.append(item)
        return cleaned
    return items


def recompose_asset_score(asset: ReferenceImageAsset, *, consistency: float | None = None,
                          redundancy_penalty: float | None = None) -> float:
    """按最新维度重写 asset.qualityScore / qa.overall，返回综合分。"""
    abs_q = _absolute_quality_of(asset)
    cons = _consistency_of(asset) if consistency is None else _clamp01(consistency)
    penalty = 0.0 if redundancy_penalty is None else float(redundancy_penalty)
    if redundancy_penalty is None and asset.qa and asset.qa.get("redundancy_penalty") is not None:
        try:
            penalty = float(asset.qa["redundancy_penalty"])
        except (TypeError, ValueError):
            penalty = 0.0
    composed = compose_reference_score(
        absolute_quality=abs_q, consistency=cons, redundancy_penalty=penalty,
        hard_failures=_hard_failures_of(asset))
    prev = dict(asset.qa or {})
    # 保留非评分字段（issues / drift / batch 标记等）
    asset.qa = {**prev, **composed}
    asset.qualityScore = composed["overall"]
    return composed["overall"]


def apply_keep_gate(asset: ReferenceImageAsset, *, threshold: float | None = None) -> bool:
    """统一门禁：综合分 ≥ 阈值则留下，否则仅标 quality_below_threshold。返回是否留下。"""
    thr = quality_threshold() if threshold is None else float(threshold)
    score = asset.qualityScore
    if score is None:
        asset.selectedForSeedance = False
        asset.rejectReason = "missing_quality_score"
        return False
    if float(score) < thr:
        asset.selectedForSeedance = False
        asset.rejectReason = "quality_below_threshold"
        return False
    asset.selectedForSeedance = True
    asset.rejectReason = None
    return True


def min_generated_references() -> int:
    """参考图模式下每镜至少新生成几张关键帧参考图（防止只剩定妆照）。"""
    return max(0, int_setting("video_reference_min_generated", 1))


def reference_gen_retries() -> int:
    """单张参考图 QA 不达标时的额外重试次数。"""
    return max(0, int_setting("video_reference_gen_retries", 2))


def reference_prompt_async() -> bool:
    """是否为每张新参考图用独立 LLM 调用并发生成提示词（防止一次性写多张时偷懒）。"""
    # 批量合同开启时默认走一镜一次；仍可用 video_reference_prompt_async 强制逐图
    from app.media_pipeline.retry_policy import batch_prompt_enabled
    if batch_prompt_enabled() and not bool_setting("video_reference_force_per_image_prompt", False):
        return False
    return bool_setting("video_reference_prompt_async", True)


def batch_prompt_enabled() -> bool:
    from app.media_pipeline.retry_policy import batch_prompt_enabled as _bp
    return _bp()


def batch_qa_enabled() -> bool:
    from app.media_pipeline.retry_policy import batch_qa_enabled as _bq
    return _bq()


def role_adaptive_enabled() -> bool:
    from app.media_pipeline.retry_policy import role_adaptive_enabled as _ra
    return _ra()


def consistency_check_enabled() -> bool:
    """Phase 2：是否对整组参考图做相对一致性检查（扣分进综合分，低分可触发 i2i 重生提分）。"""
    return bool_setting("video_reference_consistency_check", True)


def consistency_threshold() -> float:
    """仅触发 i2i 重生尝试的内部线；不再单独决定废弃（废弃只看综合分门禁）。"""
    return float_setting("video_reference_consistency_threshold", 0.7)


def consistency_retries() -> int:
    """漂移图从锚点 i2i 重生的最大次数；仍漂移则只靠综合分门禁决定去留。"""
    return max(0, int_setting("video_reference_consistency_retries", 1))


def max_character_reference_images() -> int:
    """喂给视频模型时偏好的「含人物参考图」上限（装箱偏好 / 冗余惩罚参考，不再硬剔除）。
    多张不同尺度含人物图易触发分身/前景巨人；超额者扣分并在装箱时让位给更高分图。"""
    return max(1, int_setting("video_reference_max_character_images", 1))


def _contains_any(text: str, words: list[str]) -> bool:
    return any(w.lower() in text for w in words)


def _parse_ref_prompts(raw: Any) -> list[dict[str, str]]:
    """归一化模型给出的「新参考图」提示词列表。每项保留合法的 type 与非空 prompt。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or item.get("text") or "").strip()
        if not prompt:
            continue
        ref_type = str(item.get("type") or "plot_key_frame").strip()
        if ref_type not in REFERENCE_IMAGE_TYPES or ref_type == "previous_shot_frame":
            ref_type = "plot_key_frame"
        out.append({"type": ref_type, "prompt": prompt[:600]})
    return out


class ShotVideoModeSelector:
    """兼容壳：模式已锁死 REFERENCE_IMAGE_MODE，select 直接返回 default_reference_decision。"""

    async def select(self, shot: Shot, bible: Bible, *, shot_row: Any | None = None,
                     prev_shot: Any | None = None) -> ShotVideoModeDecision:
        return default_reference_decision()


def default_reference_decision() -> ShotVideoModeDecision:
    from app.multiview import narrative_keyframe_required
    plan = ReferenceImagePlan(
        totalCount=1,
        generateNewCount=1,
        types=["plot_key_frame"],
    )
    reason = "已固定使用参考图模式；每镜生成 1 张必需叙事关键帧。"
    if not narrative_keyframe_required():
        reason = "已固定使用参考图模式生成视频。"
    return ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE,
        reason=reason,
        confidence=1.0,
        needGenerateNewReferences=plan.generateNewCount > 0,
        referenceImagePlan=plan,
        defaulted=True,
    )


def decision_to_dict(decision: ShotVideoModeDecision) -> dict[str, Any]:
    data = asdict(decision)
    return data


def dict_to_decision(data: dict[str, Any]) -> ShotVideoModeDecision:
    plan_data = data.get("referenceImagePlan") or {}
    default_plan = ReferenceImagePlan()
    total = int(plan_data.get("totalCount", default_plan.totalCount) or default_plan.totalCount)
    generate = int(plan_data.get("generateNewCount", default_plan.generateNewCount) or default_plan.generateNewCount)
    reuse = int(plan_data.get("reusePreviousSceneCount", default_plan.reusePreviousSceneCount) or 0)
    types = list(plan_data.get("types") or default_plan.types)
    return ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE,
        reason=str(data.get("reason") or default_reference_decision().reason),
        confidence=float(data.get("confidence", 1.0)),
        needReusePreviousScene=bool(data.get("needReusePreviousScene")),
        needGenerateNewReferences=True,
        referenceImagePlan=ReferenceImagePlan(
            totalCount=total,
            reusePreviousSceneCount=reuse,
            generateNewCount=generate,
            types=types,
            prompts=_parse_ref_prompts(plan_data.get("prompts")),
        ),
        llmUsed=bool(data.get("llmUsed")),
        defaulted=True,
    )


def _safe_ref_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "ref"


def reference_image_path(project_id: str, episode_no: int, shot_no: int, ref_type: str, index: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "shots" / str(shot_no) / "references"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{index:02d}_{_safe_ref_name(ref_type)}.jpg"


def _asset_from_path(*, path: str, ref_type: str, source: str, shot_id: str | None = None,
                     episode_id: str | None = None, scene_id: str | None = None,
                     related_character_ids: list[str] | None = None,
                     quality_score: float | None = None, qa: dict[str, Any] | None = None,
                     entity_type: str | None = None, entity_name: str | None = None,
                     library_revision_id: str | None = None, library_view_id: str | None = None,
                     view_role: str | None = None, purposes: list[str] | None = None,
                     required: bool = False, slot_key: str | None = None) -> ReferenceImageAsset:
    return ReferenceImageAsset(
        id=new_id("ref"),
        url=hiagent.data_url_from_file(path),
        path=path,
        type=ref_type,
        source=source,
        shotId=shot_id,
        episodeId=episode_id,
        sceneId=scene_id,
        relatedCharacterIds=related_character_ids or [],
        qualityScore=quality_score,
        qa=qa,
        entity_type=entity_type,
        entity_name=entity_name,
        library_revision_id=library_revision_id,
        library_view_id=library_view_id,
        view_role=view_role,
        purposes=list(purposes or []),
        required=required,
        slot_key=slot_key,
    )


def reusable_previous_assets(conn: Any, *, prev_shot: Any | None, limit: int, threshold: float) -> list[ReferenceImageAsset]:
    """旧关键帧候选不再进入参考图集合。连续性只复用上一镜实际采用视频的尾帧。"""
    return []


def character_reference_assets(bible: Bible, character_names: list[str], *, limit: int,
                               project_id: str | None = None,
                               episode_no: int | None = None) -> list[ReferenceImageAsset]:
    """人物库图作为 keyframe_seed + qa_anchor；默认不直接 video_input。"""
    from app.multiview import (
        PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR, portrait_views_for_episode,
        character_multiview_enabled,
    )
    assets: list[ReferenceImageAsset] = []
    by_name = {c.name: c for c in bible.characters}
    for name in character_names:
        if len(assets) >= limit:
            break
        c = by_name.get(name)
        views = []
        if c is not None and project_id is not None and character_multiview_enabled():
            views = portrait_views_for_episode(project_id, name, episode_no, ready_only=True)
        if views:
            # 优先 front_full，其次任意 ready 视角
            preferred = next((v for v in views if v.get("view_role") == "front_full"), views[0])
            path = preferred.get("image_path")
            qa = None
            if preferred.get("qa_json"):
                try:
                    qa = json.loads(preferred["qa_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    qa = None
            score = None
            if isinstance(qa, dict) and qa.get("overall") is not None:
                try:
                    score = float(qa["overall"])
                except (TypeError, ValueError):
                    score = None
            if not path or not Path(path).exists():
                continue
            try:
                assets.append(_asset_from_path(
                    path=path,
                    ref_type="character",
                    source="asset_library",
                    related_character_ids=[name],
                    quality_score=score,
                    qa=qa or {"status": "unverified", "overall": None, "issues": ["人物库图缺少 QA"]},
                    entity_type="character",
                    entity_name=name,
                    library_revision_id=preferred.get("portrait_id"),
                    library_view_id=preferred.get("id"),
                    view_role=preferred.get("view_role"),
                    purposes=[PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
                ))
            except OSError:
                continue
            continue
        # 回退单图
        path = None
        if c is not None and project_id is not None:
            from app.portraits import portrait_for_episode
            path = portrait_for_episode(project_id, name, episode_no)
        if not path:
            path = getattr(c, "ref_image_path", None) if c else None
        if not path or not Path(path).exists():
            continue
        try:
            assets.append(_asset_from_path(
                path=path,
                ref_type="character",
                source="asset_library",
                related_character_ids=[name],
                quality_score=None,
                qa={"status": "unverified", "overall": None, "issues": ["旧单图无分项 QA"]},
                entity_type="character",
                entity_name=name,
                view_role="front_full",
                purposes=[PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
            ))
        except OSError:
            continue
    return assets


def scene_reference_assets(bible: Bible, scene_name: str, *, project_id: str | None = None,
                           episode_no: int | None = None) -> list[ReferenceImageAsset]:
    """该镜场景的场景库图 →[ReferenceImageAsset]（环境真值锚点；默认 keyframe_seed+qa_anchor）。"""
    from app.multiview import (
        PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR, scene_views_for_episode, scene_multiview_enabled,
    )
    if not scene_name:
        return []
    if project_id and scene_multiview_enabled():
        views = scene_views_for_episode(project_id, scene_name, episode_no, ready_only=True)
        if views:
            preferred = next((v for v in views if v.get("view_role") == "establishing"), views[0])
            path = preferred.get("image_path")
            qa = None
            if preferred.get("qa_json"):
                try:
                    qa = json.loads(preferred["qa_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    qa = None
            score = float(qa["overall"]) if isinstance(qa, dict) and qa.get("overall") is not None else None
            if path and Path(path).exists():
                try:
                    return [_asset_from_path(
                        path=path, ref_type="scene", source="asset_library",
                        quality_score=score, qa=qa or {"status": "unverified", "overall": None},
                        entity_type="scene", entity_name=scene_name,
                        library_revision_id=preferred.get("scene_reference_id"),
                        library_view_id=preferred.get("id"),
                        view_role=preferred.get("view_role"),
                        purposes=[PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
                    )]
                except OSError:
                    pass
    from app.scenes import scene_ref_for_episode, scene_ref_qa_for_episode
    path = scene_ref_for_episode(project_id, scene_name, episode_no) if project_id else None
    if not path:
        by_name = {s.name: s for s in (getattr(bible, "scenes", None) or [])}
        sc = by_name.get(scene_name)
        path = getattr(sc, "ref_image_path", None) if sc else None
    if not path or not Path(path).exists():
        return []
    qa = scene_ref_qa_for_episode(project_id, scene_name, episode_no) if project_id else None
    score = float(qa.get("overall")) if isinstance(qa, dict) and qa.get("overall") is not None else None
    try:
        return [_asset_from_path(
            path=path, ref_type="scene", source="asset_library",
            quality_score=score, qa=qa or {"status": "unverified", "overall": None},
            entity_type="scene", entity_name=scene_name, view_role="establishing",
            purposes=[PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
        )]
    except OSError:
        return []


def reference_generation_prompt(shot: Shot, bible: Bible, ref_type: str, index: int,
                                *, content_override: str | None = None) -> str:
    anchors = []
    by_name = {c.name: c for c in bible.characters}
    for name in shot.characters:
        if name in by_name:
            anchors.append(f"{name}: {by_name[name].appearance_canonical}")
    # content_override：模型按剧本为这张参考图写的内容提示词。提供时以它为主体，
    # 仍统一补上角色锚点 / 画风 / 负面约束，保证可作为 Seedance 参考图。
    if content_override:
        body = content_override.strip()
    else:
        body = (
            f"Create one clean 9:16 anime-drama reference image for Seedance. "
            f"Reference type: {ref_type}. Shot {shot.shot_no}. Scene: {shot.scene_setting}. "
            f"Action: {shot.action_desc}. First frame idea: {shot.first_frame_desc}. "
            f"Last frame idea: {shot.last_frame_desc}."
        )
    return (
        f"{body} Characters: {'; '.join(anchors)}. "
        f"Episode style: {bible.world.visual_style_canonical}. "
        "No text, no subtitles, no watermark, no logo, no extra limbs, no motion blur. 9:16 portrait. "
        "The image must be suitable as a Seedance 2.0 reference image."
    )


async def review_reference_image(image_b64: str, *, shot: Shot, bible: Bible, ref_type: str) -> dict[str, Any]:
    anchors = []
    by_name = {c.name: c for c in bible.characters}
    for name in shot.characters:
        if name in by_name:
            anchors.append(f"{name}: {by_name[name].appearance_canonical}")
    expectation = {
        "task": "Quality check one Seedance reference image.",
        "ref_type": ref_type,
        "shot": {
            "scene": shot.scene_setting,
            "action": shot.action_desc,
            "characters": anchors,
            "style": bible.world.visual_style_canonical,
        },
        "checks": [
            "character consistency", "clothing consistency", "hair consistency", "core props",
            "scene match", "no broken anatomy", "no wrong text", "no watermark",
            "suitable as Seedance reference image",
        ],
        "output_schema": {
            "character_match": 0.0,
            "costume_match": 0.0,
            "hair_match": 0.0,
            "prop_match": 0.0,
            "scene_match": 0.0,
            "clean_frame": 0.0,
            "seedance_reference_fit": 0.0,
            "overall": 0.0,
            "issues": [],
        },
    }
    raw = await hiagent.vlm_check(
        [image_b64], json.dumps(expectation, ensure_ascii=False),
        call_meta={
            "initiator_label": "参考图单图质检",
            "reference_type": ref_type,
            "shot_no": shot.shot_no,
            "scene_setting": shot.scene_setting,
        })
    data = extract_json(raw)
    keys = ["character_match", "costume_match", "hair_match", "prop_match", "scene_match", "clean_frame", "seedance_reference_fit"]
    for key in keys + ["overall"]:
        try:
            data[key] = max(0.0, min(1.0, float(data.get(key, 0))))
        except (TypeError, ValueError):
            data[key] = 0.0
    if not data.get("overall"):
        data["overall"] = round(sum(float(data.get(k, 0)) for k in keys) / len(keys), 3)
    if not isinstance(data.get("issues"), list):
        data["issues"] = [str(data.get("issues"))]
    return data


# i2i 种子使用守则：参考图只锁「身份/服饰/环境」，姿态构图一律走文字——否则图生图会照搬
# 种子的站姿/构图，导致同镜多张雷同、且照搬定妆照站姿（见 worker.py:355 关键帧系统的同款教训）。
_SEED_USAGE_NOTE = (
    " IMPORTANT: the provided reference images are identity/style anchors ONLY — use them to keep each "
    "character's face, hairstyle and outfit identical and the scene's environment/lighting consistent. "
    "Do NOT copy their pose, framing or composition; strictly follow THIS prompt's described pose, "
    "action, expression and camera."
)


async def _generate_image_with_seed_fallback(prompt: str, seed_inputs: list[str] | None, *,
                                             call_meta: dict | None = None) -> dict[str, Any]:
    """带 i2i 种子生成参考图；若网关不支持参考图（ProviderError）则去掉种子重试一次（对齐 worker._generate_one_scene）。"""
    try:
        return await hiagent.generate_image(
            prompt, size=config.REF_IMAGE_SIZE, image_inputs=seed_inputs or None, call_meta=call_meta)
    except ProviderError:
        if not seed_inputs:
            raise
        return await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE, call_meta=call_meta)


async def review_reference_consistency(*, candidates: list[ReferenceImageAsset],
                                       anchors: list[ReferenceImageAsset],
                                       shot: Shot, bible: Bible) -> dict[str, Any]:
    """相对一致性检查 Agent（Phase 2）：把锚点图（定妆照/上镜尾帧=真值）与候选新参考图【一起】喂给 VLM，
    逐张给候选打「与锚点的一致性」分并点名漂移维度（服饰/发型/长相/画风/环境）。

    与逐图绝对质检 review_reference_image 的本质区别：它做组内相对比较，能抓到「同分镜两张互相打架」
    「和上一镜没关系」这类单图质检结构上看不见的问题。姿态/表情/机位允许不同，不扣分。
    VLM 异常或 JSON 解析失败时返回 failed=True 且不伪造满分；调用方应跳过一致性重生，
    仅保留已有绝对质检结果，避免「检查失败 = 完美一致」的静默放行。
    返回 {"candidates": [{"asset_id", "consistency", "drift": [...], "issues": [...]}], "overall", "failed"?}。"""
    anchor_b64: list[str] = []
    for a in anchors:
        if a.path and Path(a.path).exists():
            try:
                anchor_b64.append(hiagent.encode_image_file(a.path))
            except OSError:
                continue
    cand_pairs: list[tuple[ReferenceImageAsset, str]] = []
    for c in candidates:
        if c.path and Path(c.path).exists():
            try:
                cand_pairs.append((c, hiagent.encode_image_file(c.path)))
            except OSError:
                continue
    if not cand_pairs or not anchor_b64:
        return {"candidates": [], "overall": 1.0, "failed": False}

    char_txt = "; ".join(f"{c.name}: {c.appearance_canonical}"
                         for c in bible.characters if c.name in shot.characters)
    k, n = len(anchor_b64), len(cand_pairs)
    expectation = (
        f"You are a reference-image CONSISTENCY reviewer for ONE anime-drama shot. I send {k + n} images "
        f"in order. The FIRST {k} are ANCHOR images = ground truth for each character's face/hairstyle/outfit "
        f"and for the scene environment/lighting. The NEXT {n} are CANDIDATE reference images for the SAME "
        f"shot, numbered 1..{n} in the order sent (after the anchors). For EACH candidate, judge whether the "
        "SAME character(s) keep an IDENTICAL face, hairstyle and outfit, and whether the art style / lighting "
        "/ environment stay consistent with the anchors. Pose, expression, gesture and camera framing are "
        "ALLOWED to differ — do NOT penalize those. "
        f"Character appearance reference (text): {char_txt or '(none)'}. "
        f"Art style: {bible.world.visual_style_canonical}. "
        'Output exactly one JSON object: {"candidates":[{"n":<1-based int>,"consistency":<0..1>,'
        '"drift":[<any of "costume","hair","face","style","environment">],"issues":[<short strings>]}],'
        '"overall":<0..1>}. consistency=1 means perfectly consistent with the anchors; below 0.7 means a '
        "clear outfit/hair/face/style change that would make the generated video inconsistent."
    )
    frames = anchor_b64 + [b for _, b in cand_pairs]
    try:
        raw = await hiagent.vlm_check(
            frames, expectation,
            call_meta={
                "initiator_label": "参考图一致性质检",
                "shot_no": shot.shot_no,
                "candidate_count": len(cand_pairs),
                "anchor_count": len(anchor_b64),
            })
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 VLM/解析失败不可观测地伪造成满分
        return {
            "candidates": [
                {
                    "asset_id": c.id,
                    "consistency": None,
                    "drift": [],
                    "issues": [f"consistency_check_unavailable:{type(exc).__name__}"],
                    "check_failed": True,
                }
                for c, _ in cand_pairs
            ],
            "overall": None,
            "failed": True,
        }

    out: list[dict[str, Any]] = []
    reported = data.get("candidates") if isinstance(data, dict) else None
    if isinstance(reported, list):
        for item in reported:
            if not isinstance(item, dict):
                continue
            try:
                pos = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            if not (1 <= pos <= n):
                continue
            cand = cand_pairs[pos - 1][0]
            try:
                cs = max(0.0, min(1.0, float(item.get("consistency", 1.0))))
            except (TypeError, ValueError):
                cs = 1.0
            drift = [str(x).strip() for x in (item.get("drift") or []) if str(x).strip()]
            issues = [str(x).strip() for x in (item.get("issues") or []) if str(x).strip()]
            out.append({"asset_id": cand.id, "consistency": cs, "drift": drift, "issues": issues})
    covered = {o["asset_id"] for o in out}
    for c, _ in cand_pairs:  # 模型漏报的候选 → unverified，不得伪装满分
        if c.id not in covered:
            out.append({
                "asset_id": c.id,
                "consistency": None,
                "drift": [],
                "issues": ["consistency_unreported"],
                "check_failed": True,
            })
    try:
        vals = [o["consistency"] for o in out if o.get("consistency") is not None]
        overall = max(0.0, min(1.0, float(data.get("overall")))) if vals else None
        if overall is None and vals:
            overall = round(sum(vals) / len(vals), 3)
    except (TypeError, ValueError):
        vals = [o["consistency"] for o in out if o.get("consistency") is not None]
        overall = round(sum(vals) / len(vals), 3) if vals else None
    failed = any(o.get("check_failed") or o.get("consistency") is None for o in out)
    return {"candidates": out, "overall": overall, "failed": failed}


async def _generate_one_reference(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                  ref_type: str, index: int, content_override: str | None = None,
                                  seed_inputs: list[str] | None = None,
                                  extra_instruction: str | None = None,
                                  skip_inline_qa: bool = False) -> ReferenceImageAsset:
    dest = reference_image_path(project_id, episode_no, shot.shot_no, ref_type, index)
    prompt = reference_generation_prompt(shot, bible, ref_type, index, content_override=content_override)
    if seed_inputs:
        prompt += _SEED_USAGE_NOTE
    if extra_instruction:
        prompt += " " + extra_instruction.strip()
    item = await _generate_image_with_seed_fallback(
        prompt,
        seed_inputs,
        call_meta={
            "asset_kind": "reference_image",
            "episode_no": episode_no,
            "shot_no": shot.shot_no,
            "reference_type": ref_type,
            "reference_index": index,
        })
    if item.get("url"):
        await hiagent.download(item["url"], str(dest))
    elif item.get("b64_json"):
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise ProviderError(f"Reference image response missing url/b64_json: {list(item.keys())}")
    if skip_inline_qa:
        qa = {"overall": 1.0, "deferred_batch_qa": True, "issues": []}
        asset = _asset_from_path(
            path=str(dest),
            ref_type=ref_type,
            source="seedream_generated",
            quality_score=1.0,
            qa=qa,
            related_character_ids=list(shot.characters) if ref_type in {"character", "plot_key_frame"} else [],
        )
        return asset
    qa = await review_reference_image(hiagent.encode_image_file(str(dest)), shot=shot, bible=bible, ref_type=ref_type)
    abs_score = float(qa.get("overall", 0))
    qa = {**qa, "absolute_quality": abs_score}
    asset = _asset_from_path(
        path=str(dest),
        ref_type=ref_type,
        source="seedream_generated",
        quality_score=abs_score,
        qa=qa,
        related_character_ids=list(shot.characters) if ref_type in {"character", "plot_key_frame"} else [],
    )
    recompose_asset_score(asset)
    apply_keep_gate(asset)
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


def previous_tail_reference_asset(conn: Any, prev_shot: Any, *, dest_dir: Path) -> ReferenceImageAsset | None:
    """从上一镜实际采用成片抽尾帧，作为连续镜的参考图锚点。"""
    if prev_shot is None:
        return None

    def _g(key: str) -> Any:
        if hasattr(prev_shot, "keys"):
            return prev_shot[key] if key in prev_shot.keys() else None
        return prev_shot.get(key)

    prev_id = _g("id")
    adopted = _g("adopted_version_id")
    if adopted:
        v = conn.execute(
            "SELECT video_path FROM shot_versions WHERE id=? AND status='succeeded'", (adopted,)).fetchone()
        if v and v["video_path"] and Path(v["video_path"]).exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / "00_previous_tail.jpg"
            if _extract_last_frame(v["video_path"], dest):
                return _asset_from_path(
                    path=str(dest), ref_type="previous_shot_frame", source="previous_shot",
                    shot_id=prev_id, quality_score=1.0, qa={"overall": 1.0, "issues": ["forced_continuity"]})
    return None


async def write_reference_prompt(shot: Shot, bible: Bible, ref_type: str, *, intent: str | None = None) -> str:
    """为【单张】新参考图独立写一条详尽的 Seedream 英文提示词（一图一次 LLM 调用）。
    逐图独立调用 + 上游并发，避免一次性写多张时模型偷懒只给空泛短提示。失败返回空串（上游回退模板）。"""
    anchors = {c.name: c.appearance_canonical for c in bible.characters if c.name in shot.characters}
    payload = {
        "task": (
            "Write ONE detailed English image-generation prompt for a single Seedance reference image. "
            "It must be concrete and faithful to this shot's script so it can anchor character & scene "
            "consistency. Describe subject(s), pose/expression, key props, framing, lighting and background. "
            "Do NOT write multiple images, do NOT be lazy or generic."
        ),
        "reference_type": ref_type,
        "intent": intent or "",
        "shot": {
            "scene_setting": shot.scene_setting,
            "characters": list(shot.characters),
            "character_appearance": anchors,
            "action_desc": shot.action_desc,
            "first_frame_desc": shot.first_frame_desc,
            "last_frame_desc": shot.last_frame_desc,
            "dialogues": [d.model_dump() if hasattr(d, "model_dump") else dict(d) for d in shot.dialogues],
        },
        "style": bible.world.visual_style_canonical,
        "constraints": [
            "English only", "9:16 portrait", "no text/subtitle/watermark/logo",
            "no extra limbs, no motion blur", "single coherent still image",
            "keep character face/hair/clothing exactly as character_appearance",
        ],
        "output_schema": {"prompt": "the full English image prompt, one paragraph"},
    }
    try:
        raw = await model_gateway.chat([
            {"role": "system", "content": "Return exactly one JSON object with a single 'prompt' string field. English only."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], temperature=0.3, max_tokens=500,
            call_meta={"initiator_label": "参考图提示词生成", "reference_type": ref_type, "shot_no": shot.shot_no})
        data = extract_json(raw)
        return str(data.get("prompt") or "").strip()[:600]
    except Exception:
        return ""


_SLOT_ROLE_CYCLE = [
    ("narrative_keyframe", "plot_key_frame"),
]


async def write_reference_prompt_batch(
    shot: Shot, bible: Bible, slots: list[tuple[str, str]], *, intents: list[str | None] | None = None,
) -> list[str]:
    """一镜一次返回全部槽位提示词合同（P1）。缺项/重复时仅对异常槽回退单图调用。"""
    anchors = {c.name: c.appearance_canonical for c in bible.characters if c.name in shot.characters}
    planned = []
    for i, (slot_key, ref_type) in enumerate(slots):
        planned.append({
            "slot": slot_key,
            "type": ref_type,
            "intent": (intents[i] if intents and i < len(intents) else None) or "",
        })
    payload = {
        "task": (
            "Write DISTINCT detailed English image-generation prompts for EACH planned Seedance "
            "reference slot in one JSON. Cover identity/action, environment, action key frame and "
            "emotion/composition without near-duplicate text. Do NOT invent a previous-shot tail frame."
        ),
        "slots": planned,
        "shot": {
            "scene_setting": shot.scene_setting,
            "characters": list(shot.characters),
            "character_appearance": anchors,
            "action_desc": shot.action_desc,
            "first_frame_desc": shot.first_frame_desc,
            "last_frame_desc": shot.last_frame_desc,
        },
        "style": bible.world.visual_style_canonical,
        "constraints": [
            "English only", "9:16 portrait", "no text/subtitle/watermark",
            "each slot must be visually distinct", "no spoilers for later shots",
        ],
        "output_schema": {
            "slots": [{"slot": "identity_action", "type": "character", "prompt": "..."}],
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
            prompt = str(item.get("prompt") or "").strip()[:600]
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
        prompts[i] = await write_reference_prompt(shot, bible, ref_type, intent=intent)
    # 近重复检测：若两槽文本高度相似，重写后者
    for i in range(len(prompts)):
        for j in range(i):
            a, b = prompts[i], prompts[j]
            if a and b and a.lower()[:80] == b.lower()[:80]:
                prompts[i] = await write_reference_prompt(
                    shot, bible, slots[i][1], intent=f"distinct from slot {slots[j][0]}"
                ) or prompts[i]
    return prompts


async def review_reference_images_batch(
    items: list[tuple[str, str, str]], *, shot: Shot, bible: Bible,
) -> dict[str, Any]:
    """一次多图结构化评估（P1）。items: [(slot, ref_type, image_b64), ...]。

    返回 {"items":[{slot, overall, ..., hard_failures, repair_instruction}], "group_consistency": float}
    缺项或置信不足时由调用方对异常图回退单图 VLM。
    """
    if not items:
        return {"items": [], "group_consistency": 1.0}
    anchors = []
    by_name = {c.name: c for c in bible.characters}
    for name in shot.characters:
        if name in by_name:
            anchors.append(f"{name}: {by_name[name].appearance_canonical}")
    expectation = {
        "task": "Quality-check multiple Seedance reference images in one response.",
        "shot": {
            "scene": shot.scene_setting,
            "action": shot.action_desc,
            "characters": anchors,
            "style": bible.world.visual_style_canonical,
        },
        "image_order": [{"index": i, "slot": s, "ref_type": t} for i, (s, t, _) in enumerate(items)],
        "output_schema": {
            "items": [{
                "slot": "action_key",
                "technical_quality": 0.0,
                "prompt_alignment": 0.0,
                "identity_consistency": 0.0,
                "scene_consistency": 0.0,
                "overall": 0.0,
                "hard_failures": [],
                "repair_instruction": None,
            }],
            "group_consistency": 0.0,
        },
    }
    images = [b64 for _, _, b64 in items]
    try:
        raw = await hiagent.vlm_check(
            images, json.dumps(expectation, ensure_ascii=False),
            call_meta={
                "initiator_label": "参考图批量质检",
                "shot_no": shot.shot_no,
                "image_count": len(images),
            })
        data = extract_json(raw)
    except Exception:
        return {"items": [], "group_consistency": None, "failed": True}

    out_items = []
    by_slot = {str(it.get("slot") or ""): it for it in (data.get("items") or []) if isinstance(it, dict)}
    for slot, ref_type, _ in items:
        it = by_slot.get(slot) or {}
        if not it:
            out_items.append({"slot": slot, "missing": True})
            continue
        for key in ("technical_quality", "prompt_alignment", "identity_consistency",
                    "scene_consistency", "overall"):
            try:
                it[key] = max(0.0, min(1.0, float(it.get(key, 0))))
            except (TypeError, ValueError):
                it[key] = 0.0
        if not it.get("overall"):
            vals = [it.get(k, 0) for k in (
                "technical_quality", "prompt_alignment", "identity_consistency", "scene_consistency"
            )]
            it["overall"] = round(sum(float(v) for v in vals) / max(len(vals), 1), 3)
        it["slot"] = slot
        it["ref_type"] = ref_type
        if not isinstance(it.get("hard_failures"), list):
            it["hard_failures"] = []
        out_items.append(it)
    try:
        group = max(0.0, min(1.0, float(data.get("group_consistency", 0.8))))
    except (TypeError, ValueError):
        group = 0.8
    return {"items": out_items, "group_consistency": group, "failed": False}


async def _generate_reference_keep_best(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                        ref_type: str, index: int, content_override: str | None,
                                        retries: int, seed_inputs: list[str] | None = None,
                                        skip_inline_qa: bool = False) -> tuple[ReferenceImageAsset | None, list[ReferenceImageAsset], list[dict[str, Any]]]:
    """生成单张参考图，QA 不达标则重试；最终返回过审资产，或（全部不达标时）保留分数最高的一版兜底。
    skip_inline_qa=True 时生成后不跑单图 VLM（交给批量 QA）。"""
    rejections: list[dict[str, Any]] = []
    attempts: list[ReferenceImageAsset] = []
    best: ReferenceImageAsset | None = None
    for attempt in range(retries + 1):
        attempt_index = index * 100 + attempt
        try:
            asset = await _generate_one_reference(
                project_id=project_id, episode_no=episode_no, shot=shot, bible=bible,
                ref_type=ref_type, index=attempt_index, content_override=content_override,
                seed_inputs=seed_inputs, skip_inline_qa=skip_inline_qa)
        except Exception as exc:
            rejections.append({"type": ref_type, "source": "seedream_generated",
                               "reason": "参考图生成异常" + code_ref(
                                   exc, action="generate_reference_image",
                                   context={"project_id": project_id, "episode_no": episode_no,
                                            "shot_id": getattr(shot, "id", None), "ref_type": ref_type})})
            continue
        if skip_inline_qa:
            return asset, attempts, rejections
        if not asset.rejectReason:
            return asset, attempts, rejections
        rejections.append({"type": ref_type, "source": "seedream_generated",
                           "reason": asset.rejectReason, "quality_score": asset.qualityScore, "qa": asset.qa})
        attempts.append(asset)
        if best is None or (asset.qualityScore or 0) > (best.qualityScore or 0):
            best = asset
    if best is not None and (best.qualityScore or 0) < quality_floor():
        return None, list(attempts), rejections
    discarded = [a for a in attempts if a is not best]
    return best, discarded, rejections


def _portrait_seed_inputs(bible: Bible, character_names: list[str], *, project_id: str | None,
                          episode_no: int | None, limit: int = 2) -> list[str]:
    """出场角色定妆照的 data URL，作为新参考图的 i2i 种子（锁长相/发型/服饰，姿态仍走文字）。
    用 refs.refs_as_image_inputs 走「按集分段定妆照」选版，与喂给 Seedance 的人物锚点同源。"""
    from app.refs import refs_as_image_inputs
    return [url for url, _ in refs_as_image_inputs(
        bible, list(character_names), max(limit, 0), project_id=project_id, episode_no=episode_no)]


def _dedupe_str(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _consistency_scores(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把 review_reference_consistency 的报告整理成 {asset_id: {consistency, drift, issues}}。"""
    out: dict[str, dict[str, Any]] = {}
    for c in (report or {}).get("candidates", []) or []:
        aid = c.get("asset_id")
        if not aid:
            continue
        if c.get("check_failed") or (report or {}).get("failed"):
            out[aid] = {
                "consistency": None,
                "drift": [str(x) for x in (c.get("drift") or []) if str(x).strip()],
                "issues": [str(x) for x in (c.get("issues") or []) if str(x).strip()],
                "check_failed": True,
            }
            continue
        try:
            cs = max(0.0, min(1.0, float(c.get("consistency", 1.0))))
        except (TypeError, ValueError):
            cs = 1.0
        out[aid] = {
            "consistency": cs,
            "drift": [str(x) for x in (c.get("drift") or []) if str(x).strip()],
            "issues": [str(x) for x in (c.get("issues") or []) if str(x).strip()],
        }
    return out


def _annotate_consistency(assets: list[ReferenceImageAsset], scores: dict[str, dict[str, Any]]) -> None:
    for a in assets:
        info = scores.get(a.id)
        if not info:
            continue
        if info.get("check_failed") or info.get("consistency") is None:
            a.qa = {
                **(a.qa or {}),
                "consistency_check_failed": True,
                "drift": info.get("drift") or [],
                "issues": info.get("issues") or [],
            }
            continue
        a.qa = {**(a.qa or {}), "consistency": info["consistency"], "drift": info["drift"]}
        recompose_asset_score(a, consistency=info["consistency"])


def _mark_below_threshold(rejection_details: list[dict[str, Any]] | None,
                          rejected_out: list[ReferenceImageAsset] | None,
                          asset: ReferenceImageAsset, *, consistency: float | None = None,
                          drift: list[str] | None = None) -> None:
    """综合分不达标时统一记 quality_below_threshold（不再暴露 consistency_drift 等内部代号）。"""
    apply_keep_gate(asset)
    if asset.selectedForSeedance:
        return
    if rejected_out is not None and asset not in rejected_out:
        rejected_out.append(asset)
    if rejection_details is not None:
        rejection_details.append({
            "type": asset.type, "source": asset.source,
            "reason": asset.rejectReason or "quality_below_threshold",
            "drift": drift or [], "consistency": consistency,
            "quality_score": asset.qualityScore,
        })


async def _regenerate_for_consistency(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                      ref_type: str, index: int, seeds: list[str],
                                      drift: list[str]) -> ReferenceImageAsset | None:
    """漂移图从锚点 i2i 重生：强约束「服饰/发型/长相/画风/环境与锚点完全一致，只改姿态」。"""
    note = ("Regenerate to FIX consistency versus the reference anchors"
            + (": " + ", ".join(drift) if drift else "")
            + ". Keep each character's face, hairstyle and outfit, the art style and the environment EXACTLY "
              "identical to the reference images; only adapt pose and expression to this shot.")
    try:
        asset = await _generate_one_reference(
            project_id=project_id, episode_no=episode_no, shot=shot, bible=bible,
            ref_type=ref_type, index=index, content_override=None,
            seed_inputs=seeds or None, extra_instruction=note)
    except Exception:  # noqa: BLE001 单张重生失败不拖垮整镜
        return None
    return asset


async def _enforce_reference_consistency(*, selected: list[ReferenceImageAsset], shot: Shot, bible: Bible,
                                         project_id: str, episode_no: int,
                                         rejection_details: list[dict[str, Any]] | None = None,
                                         rejected_out: list[ReferenceImageAsset] | None = None
                                         ) -> list[ReferenceImageAsset]:
    """Phase 2：以锚点为真值检查候选一致性；低一致性格分并可选 i2i 重生提分。

    不再因 consistency_drift 硬剔：去留只由综合分门禁决定。无锚点时跳过。
    """
    if not consistency_check_enabled():
        return selected
    candidates = [a for a in selected if a.source == "seedream_generated"]
    anchors = [a for a in selected if a.source in {"asset_library", "previous_shot"}]
    if not candidates or not anchors:
        return selected
    seeds = _dedupe_str([a.url for a in anchors if a.url])
    regen_line = consistency_threshold()

    current = list(candidates)
    extras_kept: list[ReferenceImageAsset] = []
    report = await review_reference_consistency(
        candidates=current, anchors=anchors, shot=shot, bible=bible)
    scores = _consistency_scores(report)
    _annotate_consistency(current, scores)
    if report.get("failed"):
        # 检查失败时不伪造满分、不触发漂移重生；保留既有绝对质检结果。
        return selected

    for attempt in range(consistency_retries()):
        drifted = [
            c for c in current
            if (scores.get(c.id, {}).get("consistency") is not None
                and scores.get(c.id, {}).get("consistency", 1.0) < regen_line)
        ]
        if not drifted:
            break
        changed = False
        for i, cand in enumerate(drifted):
            drift = scores.get(cand.id, {}).get("drift") or []
            new_asset = await _regenerate_for_consistency(
                project_id=project_id, episode_no=episode_no, shot=shot, bible=bible,
                ref_type=cand.type, index=9000 + attempt * 100 + i, seeds=seeds, drift=drift)
            if new_asset is None:
                continue
            # 原图：按已扣一致性重算；≥门禁则仍留下，否则进废弃（理由仅为分数不足）
            recompose_asset_score(cand, consistency=scores.get(cand.id, {}).get("consistency", 1.0))
            if not apply_keep_gate(cand):
                _mark_below_threshold(
                    rejection_details, rejected_out, cand,
                    consistency=scores.get(cand.id, {}).get("consistency"),
                    drift=drift)
            elif cand not in extras_kept:
                extras_kept.append(cand)
            current = [new_asset if c is cand else c for c in current]
            changed = True
        if not changed:
            break
        report = await review_reference_consistency(
            candidates=current, anchors=anchors, shot=shot, bible=bible)
        scores = _consistency_scores(report)
        _annotate_consistency(current, scores)
        if report.get("failed"):
            break

    # 最终候选全部按一致性重算分数；不因一致性硬剔；检查失败时保持绝对质检分
    for c in current:
        info = scores.get(c.id) or {}
        if info.get("check_failed") or info.get("consistency") is None:
            continue
        recompose_asset_score(c, consistency=info.get("consistency", _consistency_of(c)))

    rebuilt = [a for a in selected if a.source != "seedream_generated"] + current
    for extra in extras_kept:
        if extra not in rebuilt and (extra.qualityScore or 0) >= quality_threshold():
            rebuilt.append(extra)
    if not any(a.source == "seedream_generated" for a in rebuilt) and current:
        # 极端：生成图全低于门禁时仍留综合分最高的一张作兜底候选（后续 gate/floor 再裁）
        best = max(current, key=lambda c: c.qualityScore or 0.0)
        if best not in rebuilt:
            rebuilt.append(best)
    return rebuilt


async def build_reference_assets(*, conn: Any, project_id: str, episode_no: int, episode_id: str,
                                 shot_id: str, shot: Shot, bible: Bible,
                                 decision: ShotVideoModeDecision, prev_shot: Any | None = None,
                                 rejection_details: list[dict[str, Any]] | None = None,
                                 rejected_out: list[ReferenceImageAsset] | None = None,
                                 on_progress: Callable[
                                     [list[ReferenceImageAsset], list[ReferenceImageAsset]], None
                                 ] | None = None,
                                 allow_missing_continuity_tail: bool = False,
                                 job_id: str | None = None,
                                 existing_meta: dict[str, Any] | None = None) -> list[ReferenceImageAsset]:
    from app.multiview import (
        PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR, PURPOSE_VIDEO_INPUT,
        NARRATIVE_KEYFRAME_SLOT, resolve_shot_asset_dependencies, keyframe_seed_paths,
        library_anchor_assets_from_manifest, review_keyframe_with_evidence, keyframe_gate_passed,
        narrative_keyframe_required, complete_legacy_character_pack, complete_legacy_scene_pack,
    )
    plan = decision.referenceImagePlan
    max_refs = max_reference_images()
    existing_meta = existing_meta or {}
    slot_state: dict[str, Any] = dict(existing_meta.get("reference_slots") or {})

    # 冻结依赖 manifest：已冻结则复用，避免 worker 重启后重新选最新人物图
    frozen_manifest = existing_meta.get("reference_manifest")
    if existing_meta.get("reference_manifest_frozen") and isinstance(frozen_manifest, dict):
        manifest = frozen_manifest
    else:
        # 进入本集生产前按需补齐 legacy_partial 缺失视角
        style = bible.world.visual_style_canonical
        for name in list(shot.characters or []):
            try:
                await complete_legacy_character_pack(project_id, name, episode_no, style)
            except Exception:  # noqa: BLE001
                pass
        scene_name = getattr(shot, "scene_name", "") or ""
        if scene_name:
            try:
                await complete_legacy_scene_pack(project_id, scene_name, episode_no, style)
            except Exception:  # noqa: BLE001
                pass
        manifest = resolve_shot_asset_dependencies(
            project_id=project_id, episode_no=episode_no, shot_id=shot_id, shot=shot,
            scene_name=scene_name or None,
        )
        existing_meta["reference_manifest"] = manifest
        existing_meta["reference_manifest_frozen"] = True

    # 只有 action_continuation 才把上一镜尾帧作为强制参考图和剪辑点连贯锚点。
    forced: list[ReferenceImageAsset] = []
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    needs_tail = uses_previous_tail_frame(derive_continuity_mode(shot))
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
        bible, shot.characters, limit=max(1, len(shot.characters)),
        project_id=project_id, episode_no=episode_no,
    )
    if not any(a.entity_type == "character" for a in evidence_assets):
        evidence_assets.extend(char_assets)

    selected: list[ReferenceImageAsset] = list(forced)
    # 证据锚点暂不计入 selectedForSeedance；稍后合并进画廊
    reserve_for_gen = min(want_gen, max(0, max_refs - len(forced)))
    generated_needed = reserve_for_gen

    def _publish_progress() -> None:
        if on_progress is None:
            return
        gallery = _dedupe_assets(list(selected) + list(evidence_assets))
        for asset in gallery:
            asset.shotId = asset.shotId or shot_id
            asset.episodeId = asset.episodeId or episode_id
            # 仅 video_input 用途默认选中
            if PURPOSE_VIDEO_INPUT in (asset.purposes or []) or asset.type == "previous_shot_frame":
                asset.selectedForSeedance = not asset.deleted and asset.rejectReason is None
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
    specs: list[tuple[str, str, str | None]] = []  # slot_key, ref_type, prompt
    passed_slots = {
        k: v for k, v in slot_state.items()
        if isinstance(v, dict) and v.get("status") == "passed" and v.get("path")
    }
    for i in range(generated_needed):
        role = _SLOT_ROLE_CYCLE[i % len(_SLOT_ROLE_CYCLE)]
        slot_key = role[0] if i < len(_SLOT_ROLE_CYCLE) else f"extra_{i}"
        if slot_key in passed_slots:
            prev = passed_slots[slot_key]
            path = prev.get("path")
            if path and Path(path).exists():
                asset = _asset_from_path(
                    path=path,
                    ref_type=prev.get("type") or role[1],
                    source="seedream_generated",
                    quality_score=float(prev.get("quality_score") or 0.0) if prev.get("quality_score") is not None else None,
                    qa=prev.get("qa") or {"overall": None, "status": "unverified", "resumed": True},
                    purposes=[PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR],
                    required=True,
                    slot_key=slot_key,
                    entity_type="shot",
                )
                asset.dependency_manifest = manifest
                selected.append(asset)
                continue
        if i < len(model_specs):
            spec = model_specs[i]
            specs.append((slot_key, spec.get("type") or type_cycle[i % len(type_cycle)], spec.get("prompt")))
        else:
            specs.append((slot_key, type_cycle[i % len(type_cycle)], None))

    if specs and batch_prompt_enabled():
        prompts = await write_reference_prompt_batch(
            shot, bible, [(s, t) for s, t, _ in specs],
            intents=[o for _, _, o in specs],
        )
        specs = [(specs[i][0], specs[i][1], prompts[i] or specs[i][2]) for i in range(len(specs))]
        for slot_key, ref_type, prompt in specs:
            slot_state[slot_key] = {
                **(slot_state.get(slot_key) or {}),
                "status": "prompt_ready", "type": ref_type, "prompt": prompt,
            }
        if existing_meta is not None:
            existing_meta["reference_slots"] = slot_state
    elif specs and reference_prompt_async():
        async def _resolve(ref_type: str, brief: str | None) -> str | None:
            written = await write_reference_prompt(shot, bible, ref_type, intent=brief)
            return written or brief or None
        resolved = await asyncio.gather(*[_resolve(t, o) for _, t, o in specs])
        specs = [(specs[i][0], specs[i][1], resolved[i]) for i in range(len(specs))]

    # 关键帧种子：优先用本镜选中的人物/场景视角
    seed_paths = keyframe_seed_paths(manifest)
    portrait_seeds = []
    for p in seed_paths:
        try:
            portrait_seeds.append(hiagent.data_url_from_file(p))
        except OSError:
            continue
    if not portrait_seeds:
        portrait_seeds = _portrait_seed_inputs(bible, shot.characters, project_id=project_id, episode_no=episode_no)
    env_seeds = [a.url for a in forced if a.type == "previous_shot_frame" and a.url]
    env_seeds += [a.url for a in evidence_assets if a.type == "scene" and a.url]

    def _seeds_for(ref_type: str) -> list[str]:
        seeds = (portrait_seeds + env_seeds) if ref_type in {"character", "plot_key_frame"} else list(env_seeds)
        return _dedupe_str(seeds)

    visual_anchors = library_anchor_assets_from_manifest(manifest)

    if specs:
        async def _run_one(slot_key: str, ref_type: str, override: str | None, index: int):
            asset, discarded, rej = await _generate_reference_keep_best(
                project_id=project_id, episode_no=episode_no, shot=shot, bible=bible,
                ref_type=ref_type, index=index, content_override=override,
                retries=reference_gen_retries(), seed_inputs=_seeds_for(ref_type),
                skip_inline_qa=True)  # 统一走证据化 QA
            return slot_key, asset, discarded, rej

        wrapped = [
            asyncio.create_task(_run_one(slot_key, ref_type, override, i + 1))
            for i, (slot_key, ref_type, override) in enumerate(specs)
        ]
        pending_qa: list[tuple[str, str, ReferenceImageAsset]] = []
        for completed in asyncio.as_completed(wrapped):
            slot_key, asset, discarded, rej = await completed
            if rejection_details is not None:
                rejection_details.extend(rej)
            if asset is not None:
                asset.slot_key = slot_key
                asset.required = slot_key == NARRATIVE_KEYFRAME_SLOT or asset.type == "plot_key_frame"
                asset.entity_type = "shot"
                asset.purposes = [PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR]
                asset.dependency_manifest = manifest
                selected.append(asset)
                slot_state[slot_key] = {
                    "status": "qa_pending",
                    "type": asset.type,
                    "path": asset.path,
                    "quality_score": asset.qualityScore,
                    "qa": asset.qa,
                }
                pending_qa.append((slot_key, asset.type, asset))
            if rejected_out is not None:
                rejected_out.extend(discarded)
            if existing_meta is not None:
                existing_meta["reference_slots"] = slot_state
            _publish_progress()

        if pending_qa:
            for slot_key, ref_type, asset in pending_qa:
                if not asset.path or not Path(asset.path).exists():
                    asset.qa = {"status": "unverified", "overall": None, "issues": ["关键帧文件缺失"]}
                    asset.qualityScore = None
                    asset.selectedForSeedance = False
                    asset.rejectReason = "unverified"
                    if rejected_out is not None and asset not in rejected_out:
                        rejected_out.append(asset)
                    if asset in selected:
                        selected.remove(asset)
                    continue
                try:
                    b64 = hiagent.encode_image_file(asset.path)
                except OSError:
                    asset.qa = {"status": "unverified", "overall": None, "issues": ["关键帧无法读取"]}
                    asset.rejectReason = "unverified"
                    continue
                if ref_type == "plot_key_frame" or slot_key == NARRATIVE_KEYFRAME_SLOT:
                    qa = await review_keyframe_with_evidence(
                        b64, shot=shot, bible=bible, visual_anchors=visual_anchors, ref_type=ref_type,
                    )
                else:
                    qa = await review_reference_image(b64, shot=shot, bible=bible, ref_type=ref_type)
                    qa.setdefault("status", "scored")
                asset.qa = qa
                if qa.get("status") == "unverified" or qa.get("overall") is None:
                    asset.qualityScore = None
                    asset.selectedForSeedance = False
                    asset.rejectReason = "unverified"
                    if rejected_out is not None and asset not in rejected_out:
                        rejected_out.append(asset)
                    if asset in selected:
                        selected.remove(asset)
                    slot_state[slot_key] = {
                        **(slot_state.get(slot_key) or {}),
                        "status": "unverified", "qa": qa,
                    }
                    continue
                overall = float(qa.get("overall") or 0)
                asset.qualityScore = overall
                if "absolute_quality" not in qa:
                    qa["absolute_quality"] = overall
                    asset.qa = qa
                recompose_asset_score(asset)
                passed = keyframe_gate_passed(qa) if (
                    ref_type == "plot_key_frame" or slot_key == NARRATIVE_KEYFRAME_SLOT
                ) else apply_keep_gate(asset)
                if not passed:
                    asset.selectedForSeedance = False
                    asset.rejectReason = asset.rejectReason or "quality_below_threshold"
                    if rejected_out is not None and asset not in rejected_out:
                        rejected_out.append(asset)
                    if asset in selected:
                        selected.remove(asset)
                    slot_state[slot_key] = {
                        **(slot_state.get(slot_key) or {}),
                        "status": "rejected", "qa": asset.qa, "quality_score": asset.qualityScore,
                    }
                else:
                    asset.selectedForSeedance = True
                    asset.rejectReason = None
                    if PURPOSE_VIDEO_INPUT not in asset.purposes:
                        asset.purposes = list(asset.purposes or []) + [PURPOSE_VIDEO_INPUT]
                    slot_state[slot_key] = {
                        **(slot_state.get(slot_key) or {}),
                        "status": "passed", "qa": asset.qa, "quality_score": asset.qualityScore,
                        "path": asset.path,
                    }
            if existing_meta is not None:
                existing_meta["reference_slots"] = slot_state
            _publish_progress()

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
        rejection_details=rejection_details, rejected_out=rejected_out)

    video_candidates = _dedupe_assets(video_candidates)
    video_candidates = _finalize_reference_selection(
        video_candidates, rejected_out=rejected_out, rejection_details=rejection_details)

    # 必需关键帧门禁：未验证/缺失时默认阻止（由调用方检查 reference_group_gate）
    has_keyframe = any(
        (a.type == "plot_key_frame" or a.slot_key == NARRATIVE_KEYFRAME_SLOT)
        and not a.deleted and a.qa and a.qa.get("status") != "unverified"
        and a.qa.get("overall") is not None
        for a in video_candidates
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
    _publish_progress()
    return gallery


async def assemble_continuity_tail(
    *, conn: Any, project_id: str, episode_no: int, episode_id: str, shot_id: str,
    shot: Shot, bible: Bible, meta: dict[str, Any], prev_shot: Any | None,
    rejection_details: list[dict[str, Any]] | None = None,
    rejected_out: list[ReferenceImageAsset] | None = None,
) -> list[ReferenceImageAsset]:
    """尾帧到达后：装配连续性参考并做最终组门禁；不重跑已通过静态槽位。"""
    refs = list(meta.get("reference_images") or [])
    selected: list[ReferenceImageAsset] = []
    for ref in refs:
        path = ref.get("path") or ref.get("image_path")
        if not path or not Path(path).exists():
            continue
        if ref.get("type") == "previous_shot_frame":
            continue  # 用最新尾帧替换
        selected.append(_asset_from_path(
            path=path,
            ref_type=ref.get("type") or "plot_key_frame",
            source=ref.get("source") or "pipeline",
            quality_score=ref.get("qualityScore"),
            qa=ref.get("qa"),
            related_character_ids=list(ref.get("relatedCharacterIds") or []),
        ))
        selected[-1].selectedForSeedance = bool(ref.get("selectedForSeedance", True))
        selected[-1].id = ref.get("id") or selected[-1].id

    prev = prev_shot
    if prev is None and int(getattr(shot, "shot_no", 0) or 0) > 1:
        prev = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
            (episode_id, int(shot.shot_no) - 1)).fetchone()
    if prev is not None:
        ref_dir = reference_image_path(project_id, episode_no, shot.shot_no, "previous_shot_frame", 0).parent
        tail = previous_tail_reference_asset(conn, prev, dest_dir=ref_dir)
        if tail:
            selected = [tail] + [a for a in selected if a.type != "previous_shot_frame"]

    selected = await _enforce_reference_consistency(
        selected=selected, shot=shot, bible=bible, project_id=project_id, episode_no=episode_no,
        rejection_details=rejection_details, rejected_out=rejected_out,
    )
    selected = _dedupe_assets(selected)
    selected = _finalize_reference_selection(
        selected, rejected_out=rejected_out, rejection_details=rejection_details)
    for asset in selected:
        asset.selectedForSeedance = True
        asset.shotId = shot_id
        asset.episodeId = episode_id
    return selected


def _is_character_bearing(asset: ReferenceImageAsset) -> bool:
    """该参考图里是否含人物（会参与构图、可能被模型当成额外主体复制）。纯场景/环境图返回 False。"""
    return asset.type in {"character", "plot_key_frame", "previous_shot_frame"} or bool(asset.relatedCharacterIds)


def _is_character_bearing_ref(ref: dict[str, Any]) -> bool:
    return (ref.get("type") in {"character", "plot_key_frame", "previous_shot_frame"}
            or bool(ref.get("relatedCharacterIds")))


def _apply_redundancy_penalties(assets: list[ReferenceImageAsset]) -> None:
    """对超额含人物图施加 0～0.15 软惩罚并写入综合分；不直接踢出。"""
    char_refs = [a for a in assets if _is_character_bearing(a)]
    if len(char_refs) <= 1:
        for a in assets:
            recompose_asset_score(a, redundancy_penalty=0.0)
        return

    def _rank_key(a: ReferenceImageAsset) -> tuple:
        # 与旧优先级对齐：尾帧 > 过审生成 > 定妆照 > 兜底生成；同分看当前分
        if a.type == "previous_shot_frame":
            pri = 0
        elif a.source == "seedream_generated":
            pri = 1 if not a.rejectReason else 3
        else:
            pri = 2
        return (pri, -(a.qualityScore or 0.0))

    ranked = sorted(char_refs, key=_rank_key)
    prefer = max_character_reference_images()
    penalties: dict[int, float] = {}
    for i, a in enumerate(ranked):
        if i < prefer:
            penalties[id(a)] = 0.0
        else:
            excess = i - prefer + 1
            penalties[id(a)] = min(_MAX_REDUNDANCY_PENALTY, 0.05 * excess)

    for a in assets:
        recompose_asset_score(a, redundancy_penalty=penalties.get(id(a), 0.0))


def _finalize_reference_selection(
    assets: list[ReferenceImageAsset],
    *,
    rejected_out: list[ReferenceImageAsset] | None = None,
    rejection_details: list[dict[str, Any]] | None = None,
) -> list[ReferenceImageAsset]:
    """冗余软惩罚 → 综合分门禁：≥阈值一律留下；低于则进废弃（仅 quality_below_threshold）。"""
    if not assets:
        return []
    _apply_redundancy_penalties(assets)
    kept: list[ReferenceImageAsset] = []
    for asset in assets:
        if apply_keep_gate(asset):
            kept.append(asset)
            continue
        # 地板以上的生成兜底：若本镜尚无任何留下的图，允许暂留最高分一张
        if rejected_out is not None and asset not in rejected_out:
            rejected_out.append(asset)
        if rejection_details is not None:
            rejection_details.append({
                "type": asset.type, "source": asset.source,
                "reason": asset.rejectReason or "quality_below_threshold",
                "quality_score": asset.qualityScore,
            })
    if kept:
        return kept
    # 极端兜底：全部低于门禁时，若最佳仍 ≥ quality_floor，留下一张以免无参考
    ranked = sorted(assets, key=lambda a: a.qualityScore or 0.0, reverse=True)
    best = ranked[0] if ranked else None
    if best is not None and (best.qualityScore or 0) >= quality_floor():
        best.selectedForSeedance = True
        # 仍保留 rejectReason 作为「兜底」标记，前端可显示分数
        if rejected_out is not None and best in rejected_out:
            rejected_out.remove(best)
        return [best]
    return []


def pack_reference_images_for_seedance(
    refs: list[dict[str, Any]], *, max_images: int | None = None,
    continuity_required: bool = False,
) -> list[dict[str, Any]]:
    """必需用途优先装箱；分数只在同类候选内排序。关键帧不会被高分定妆照挤掉。"""
    from app.multiview import pack_references_by_purpose, PURPOSE_VIDEO_INPUT, purpose_list
    usable = []
    for r in refs:
        if r.get("deleted"):
            continue
        purposes = purpose_list(r)
        if PURPOSE_VIDEO_INPUT in purposes or r.get("selectedForSeedance"):
            usable.append(r)
    if not usable:
        return []
    limit = max_images if max_images is not None else max_reference_images()
    char_limit = max_character_reference_images()
    return pack_references_by_purpose(
        usable, max_images=limit, continuity_required=continuity_required, char_limit=char_limit,
    )


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


# 反分身/单实例约束（真正发给 Seedance 视频的 prompt 用）：参考图只锁「身份+环境」，绝不能被当成额外主体
# 再画一遍。不加这句时，满屏全身定妆照常被模型原样贴进画面 → 前景巨人 + 脚本里的小人 = 同一角色两份/穿模。
REFERENCE_SINGLE_INSTANCE_NOTE = (
    " 重要：以上参考图仅用于锁定每个角色的长相/发型/服装与场景的环境/光线；"
    "每个角色在整个画面里只能出现一次，严禁把参考图里的人物当作额外的前景或背景对象再画一遍，"
    "不要分身/复制/双重同一角色，不要出现一个贴满画面的巨大人物剪影遮挡主体，不要人物与人物穿模重叠。"
)


def append_reference_prompt_notes(prompt_text: str, assets: list[ReferenceImageAsset]) -> str:
    lines = []
    for idx, asset in enumerate(assets, 1):
        label = {
            "character": "character",
            "scene": "scene",
            "prop": "prop",
            "style": "style",
            "previous_shot_frame": "previous shot clean frame",
            "plot_key_frame": "plot key frame",
        }.get(asset.type, asset.type)
        source = asset.source.replace("_", " ")
        chars = f"; related characters: {', '.join(asset.relatedCharacterIds)}" if asset.relatedCharacterIds else ""
        lines.append(f"Reference image {idx}: use as {label}; source: {source}{chars}.")
    if not lines:
        return prompt_text
    note = " Use the provided reference images as follows: " + " ".join(lines) + REFERENCE_SINGLE_INSTANCE_NOTE
    return prompt_text + note


def build_seedance_image_inputs(meta: dict[str, Any]) -> list[tuple[str, str]]:
    mode = meta.get("mode") or REFERENCE_IMAGE_MODE
    if mode == REFERENCE_IMAGE_MODE:
        if meta.get("first_frame_path") or meta.get("last_frame_path"):
            raise ProviderError("REFERENCE_IMAGE_MODE must not pass first_frame or last_frame.")
        refs = meta.get("reference_images") or []
        if not refs:
            raise ProviderError("REFERENCE_IMAGE_MODE requires at least one quality-approved reference image.")
        # 使用中的图按综合分 Top-N 装箱；截断不改 selected，高分未入选仍留在画廊。
        usable = pack_reference_images_for_seedance(refs)
        if not usable:
            raise ProviderError("REFERENCE_IMAGE_MODE has no selected reference images.")
        out: list[tuple[str, str]] = []
        for ref in usable:
            if ref.get("path"):
                out.append((hiagent.data_url_from_file(ref["path"]), "reference_image"))
            elif ref.get("url"):
                out.append((ref["url"], "reference_image"))
        if not out:
            raise ProviderError("REFERENCE_IMAGE_MODE has no selected reference images.")
        return out

    raise ProviderError("视频生成已固定为参考图模式，不再支持首尾帧输入。")
