from __future__ import annotations

import asyncio
import base64
import hashlib
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

# 关键帧提示词是分镜级可复用资产的一部分。升级该版本时，未经人工
# 编辑的旧关键帧不得继续污染新视频版本。
KEYFRAME_PROMPT_CONTRACT_VERSION = "narrative_action_geometry_v11"
KEYFRAME_STRUCTURAL_FALLBACK_MODE = "omit_structurally_invalid_keyframe_slots_v1"
_KEYFRAME_LLM_PROMPT_MAX_CHARS = 1200
_DEFAULT_KEYFRAME_CANDIDATE_COUNT = 3
_SHORT_SHOT_MAX_SECONDS = 7.0
_MAX_TIMELINE_KEYFRAMES = 2
_MULTI_KEYFRAME_INVARIANCE_NOTE = (
    "MULTI-KEYFRAME IDENTITY LOCK: every timeline keyframe belongs to the same uninterrupted shot. "
    "Keep each named character's face, hairstyle, age, body build, exact clothing design/colors/accessories, "
    "and standing height/relative height ratio IDENTICAL across all keyframes. Only pose, expression, gesture, "
    "and scripted action state may change. Never redesign, resize, age, restyle, or change clothes between frames."
)


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
    prompt_contract_version: str | None = None
    keyframe_contract_fingerprint: str | None = None
    # 同一个叙事关键帧槽内的候选编号；最终画廊只会保留胜出者。
    candidate_no: int | None = None
    keyframe_index: int | None = None
    keyframe_total: int | None = None
    keyframe_time_ratio: float | None = None
    keyframe_target_desc: str | None = None

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
    # Seedance 2.0 参考图模式最多 9 张。
    return max(1, min(int_setting("video_reference_max_images", 9), 9))


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
_SCORE_ONLY_REJECT_REASONS = {
    "missing_quality_score",
    "qa_unverified_score_only",
    "quality_below_threshold_score_only",
    "cross_keyframe_identity_invariance_unverified",
}


def _is_score_only_reject_reason(reason: str | None) -> bool:
    return bool(reason) and reason in _SCORE_ONLY_REJECT_REASONS


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
    """Score-only：QA 分数不再淘汰参考图（PRD QA-SO #24）。

    阈值仅用于标记 ``rejectReason`` 供展示/排序，资产仍可选入 Seedance。
    """
    del threshold
    asset.selectedForSeedance = True
    score = asset.qualityScore
    thr = quality_threshold()
    if score is None:
        asset.rejectReason = "missing_quality_score"
    elif float(score) < thr:
        asset.rejectReason = "quality_below_threshold_score_only"
    else:
        asset.rejectReason = None
    return True


def min_generated_references() -> int:
    """参考图模式下每镜至少新生成几张关键帧参考图（防止只剩定妆照）。"""
    return max(0, int_setting("video_reference_min_generated", 1))


def reference_gen_retries() -> int:
    """QA 只评分：禁止因低分额外重生参考图（PRD QA-SO #25）。"""
    return 0


def keyframe_candidate_count() -> int:
    """每个叙事关键帧的固定采样数。

    这是同一逻辑槽位内的候选数，不是额外参考图槽位，也不是低分重试次数。
    """
    return max(1, min(int_setting("video_keyframe_candidate_count", _DEFAULT_KEYFRAME_CANDIDATE_COUNT), 5))


def supporting_keyframe_candidate_count() -> int:
    """辅助时序节拍的采样数。

    默认与决定性 master beat 一致，每个逻辑槽位都独立执行 3 选 1。
    """
    return max(1, min(int_setting("video_supporting_keyframe_candidates", 3), 3))


def estimated_keyframe_generation_count() -> int:
    """入队预算按最长镜头的两张关键帧预留；每张仍各自执行 3 选 1。"""
    return keyframe_candidate_count() + supporting_keyframe_candidate_count()


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


def role_adaptive_enabled() -> bool:
    from app.media_pipeline.retry_policy import role_adaptive_enabled as _ra
    return _ra()


def consistency_check_enabled() -> bool:
    """Phase 2：是否对整组参考图做相对一致性检查（仅扣分/展示，不触发 i2i 重生）。"""
    return bool_setting("video_reference_consistency_check", True)


def consistency_threshold() -> float:
    """一致性分数分段阈值；不再触发 i2i 重生。"""
    return float_setting("video_reference_consistency_threshold", 0.7)


def consistency_retries() -> int:
    """QA 只评分：禁止一致性漂移驱动的 i2i 重生（PRD QA-SO #26）。"""
    return 0


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
        totalCount=_MAX_TIMELINE_KEYFRAMES,
        generateNewCount=_MAX_TIMELINE_KEYFRAMES,
        types=["plot_key_frame"] * _MAX_TIMELINE_KEYFRAMES,
    )
    reason = (
        "已固定使用参考图模式；人物/场景锚点之外，0–7 秒严格生成 1 张关键帧，"
        "超过 7 秒仅在存在两个明确剧情阶段时生成 2 张。"
    )
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
    # 有项目上下文时，scene_ref_for_episode 已执行新版整包硬门禁；不得再回退到
    # bible_json 中可能仍指向历史硬失败图的兼容缓存。
    if not path and not project_id:
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


def _keyframe_character_anchors(shot: Shot, bible: Bible) -> dict[str, str]:
    """关键帧的可见人物锚点；功能性路人不能因为没有人物谱资产而被漏掉。"""
    from app.character_policy import (
        collective_role_anchor, functional_extra_anchor, is_collective_role, is_functional_extra,
    )
    from app.continuity import effective_characters_visible

    by_name = {c.name: c for c in bible.characters}
    anchors: dict[str, str] = {}
    for name in effective_characters_visible(shot):
        character = by_name.get(name)
        if character is not None:
            anchors[name] = character.appearance_canonical
        elif is_collective_role(name):
            anchors[name] = collective_role_anchor(name)
        elif is_functional_extra(name):
            anchors[name] = functional_extra_anchor(name)
        else:
            # 上游编译门禁会拦截未知具名角色；这里仍保留可见名单，
            # 防止历史分镜在图片边界被静默省略。
            anchors[name] = f'visible character "{name}"; keep one stable, distinct identity in this shot'
    return anchors


def _keyframe_contract(shot: Shot, bible: Bible) -> dict[str, Any]:
    from app.compiler import keyframe_visual_contract

    return keyframe_visual_contract(shot, bible)


def is_narrative_keyframe_slot(slot_key: str | None) -> bool:
    """识别决定性 master 槽与同镜的其他时序节拍槽。"""
    from app.multiview import NARRATIVE_KEYFRAME_SLOT

    value = str(slot_key or "")
    return value == NARRATIVE_KEYFRAME_SLOT or value.startswith(f"{NARRATIVE_KEYFRAME_SLOT}_")


def narrative_keyframe_slot_index(slot_key: str | None) -> int | None:
    from app.multiview import NARRATIVE_KEYFRAME_SLOT

    value = str(slot_key or "")
    if value == NARRATIVE_KEYFRAME_SLOT:
        return None  # master beat 的时序位置由冻结 beat metadata 决定
    prefix = f"{NARRATIVE_KEYFRAME_SLOT}_"
    if not value.startswith(prefix):
        return None
    try:
        return int(value[len(prefix):])
    except ValueError:
        return None


def timeline_keyframe_plan(shot: Shot) -> dict[str, Any]:
    """决定本镜最终需要 1 张还是 2 张关键帧。

    0–7 秒是不可绕过的单关键帧硬限制。更长镜头也不会因为“有空余图片
    名额”自动加图，只有脚本明确包含状态迁移、动作后的独立反应，或清晰的
    顺序动作语义时才使用第二张时间路标。
    """
    try:
        duration_s = max(0.0, float(shot.duration_s or 0.0))
    except (TypeError, ValueError):
        duration_s = 0.0
    if duration_s <= _SHORT_SHOT_MAX_SECONDS:
        return {
            "count": 1,
            "duration_s": duration_s,
            "threshold_s": _SHORT_SHOT_MAX_SECONDS,
            "reason": "duration_at_most_7_seconds",
            "signals": [],
        }

    def _normalized(value: str | None) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)

    def _different(left: str | None, right: str | None) -> bool:
        a, b = _normalized(left), _normalized(right)
        return bool(a and b and a != b and a not in b and b not in a)

    signals: list[str] = []
    if _different(shot.state_in, shot.state_out):
        signals.append("explicit_state_transition")
    if shot.primary_action and shot.emotion_beat and _different(shot.primary_action, shot.emotion_beat):
        signals.append("action_then_reaction")
    sequence_text = "；".join(
        part for part in (shot.primary_action, shot.action_desc, shot.state_out) if str(part or "").strip()
    )
    if any(marker in sequence_text for marker in (
        "随后", "然后", "接着", "继而", "最后", "之后", "读完", "说完", "转身离开", "起身离开",
    )):
        signals.append("explicit_sequential_action")

    return {
        "count": 2 if signals else 1,
        "duration_s": duration_s,
        "threshold_s": _SHORT_SHOT_MAX_SECONDS,
        "reason": "two_distinct_visual_stages" if signals else "long_but_single_visual_stage",
        "signals": list(dict.fromkeys(signals)),
    }


def narrative_keyframe_beats(shot: Shot, count: int) -> list[dict[str, Any]]:
    """把一镜剧情拆成有序、不同的静止节拍，不发明新事件。

    opening/state_in → 起势 → 动作进展 → 决定性时刻 → 反应 → state_out/closing。
    ``narrative_keyframe`` 旧槽名专门留给决定性 master beat，兼容恢复链路。
    """
    from app.compiler import narrative_keyframe_target
    from app.multiview import NARRATIVE_KEYFRAME_SLOT

    count = max(1, min(int(count), _MAX_TIMELINE_KEYFRAMES))
    start = (shot.first_frame_desc or shot.state_in or shot.action_desc or shot.scene_setting).strip()
    action = (shot.primary_action or shot.action_desc or start).strip()
    decisive = (narrative_keyframe_target(shot) or action).strip()
    reaction = (shot.emotion_beat or shot.state_out or shot.last_frame_desc or decisive).strip()
    ending = (shot.last_frame_desc or shot.state_out or reaction).strip()
    if count == 1:
        ratios = [0.64]
    else:
        ratios = [i / (count - 1) for i in range(count)]

    # 决定性定格默认放在 64%；但当该定格按镜头合同必须展示剧情文字时，
    # 时间点必须落在 required_text 的可见窗口内。否则后续会同时生成
    # “必须画出指定文字”与“当前时刻禁止文字”两份相反合同。
    decisive_ratio = 0.64
    decisive_ratio_adjusted = False
    required_text = getattr(shot, "required_text", None)
    if required_text is not None and _keyframe_contract(shot, None).get("required_text_expected"):
        try:
            duration_s = float(shot.duration_s or 0.0)
            appear_at = float(required_text.appear_start_s or 0.0)
            stable_raw = required_text.stable_until_s
            stable_until = float(stable_raw) if stable_raw is not None else duration_s
            if duration_s > 0:
                window_start = max(0.0, min(1.0, appear_at / duration_s))
                window_end = max(window_start, min(1.0, stable_until / duration_s))
                decisive_ratio = min(max(decisive_ratio, window_start), window_end)
                decisive_ratio_adjusted = decisive_ratio != 0.64
        except (TypeError, ValueError):
            pass

    beats: list[dict[str, Any]] = []
    decisive_position = min(range(len(ratios)), key=lambda i: abs(ratios[i] - decisive_ratio))
    if decisive_ratio_adjusted:
        ratios[decisive_position] = decisive_ratio
    for zero_index, ratio in enumerate(ratios):
        beat_index = zero_index + 1
        if zero_index == decisive_position:
            phase = "decisive"
            target = decisive
            source = "decisive_action"
            slot_key = NARRATIVE_KEYFRAME_SLOT
        elif ratio <= 0.01:
            phase = "opening"
            target = start
            source = "first_frame_desc_or_state_in"
            slot_key = f"{NARRATIVE_KEYFRAME_SLOT}_{beat_index:02d}"
        elif ratio < 0.25:
            phase = "setup"
            target = (
                f"时间轴 {round(ratio * 100)}% 的起势与准备：{start}；"
                f"主动作尚未完成，即将开始：{action}"
            )
            source = "derived_setup"
            slot_key = f"{NARRATIVE_KEYFRAME_SLOT}_{beat_index:02d}"
        elif ratio < 0.45:
            phase = "onset"
            target = (
                f"时间轴 {round(ratio * 100)}% ：主动作刚刚开始并推进至本时刻，"
                f"尚未到达决定性结果：{action}"
            )
            source = "derived_action_onset"
            slot_key = f"{NARRATIVE_KEYFRAME_SLOT}_{beat_index:02d}"
        elif ratio < 0.64:
            phase = "progress"
            target = (
                f"时间轴 {round(ratio * 100)}% ：主动作正在清晰推进至本阶段，"
                f"接近但不得提前混入决定性时刻：{action}"
            )
            source = "derived_action_progress"
            slot_key = f"{NARRATIVE_KEYFRAME_SLOT}_{beat_index:02d}"
        elif ratio < 0.9:
            phase = "reaction"
            target = (
                f"时间轴 {round(ratio * 100)}% ：决定性动作之后、推进至本时刻的"
                f"即时反应与局势变化：{reaction}"
            )
            source = "emotion_or_state_out"
            slot_key = f"{NARRATIVE_KEYFRAME_SLOT}_{beat_index:02d}"
        else:
            phase = "closing"
            target = ending
            source = "last_frame_desc_or_state_out"
            slot_key = f"{NARRATIVE_KEYFRAME_SLOT}_{beat_index:02d}"
        beats.append({
            "slot_key": slot_key,
            "beat_index": beat_index,
            "beat_total": count,
            "time_ratio": round(ratio, 4),
            "time_s": round(float(shot.duration_s or 0) * ratio, 3),
            "phase": phase,
            "target_desc": target,
            "target_source": source,
            "prompt_intent": (
                f"Timeline beat {beat_index}/{count} at {round(ratio * 100)}% ({phase}). "
                f"Freeze only this instant: {target}. Do not show another timeline beat, montage, or split screen."
            ),
        })
    return beats


def _shot_for_keyframe_beat(shot: Shot, beat: dict[str, Any] | None) -> Shot:
    if not beat:
        return shot
    from app.compiler import explicit_height_difference_evidence, has_contact_action

    target = str(beat.get("target_desc") or shot.action_desc).strip()
    update: dict[str, Any] = {
        "action_desc": target,
        "primary_action": target,
        "first_frame_desc": target,
        "last_frame_desc": target,
        "state_in": target,
        "state_out": target,
    }
    height_evidence = explicit_height_difference_evidence(shot)
    if height_evidence:
        update["spatial_anchor"] = "；".join(
            part for part in (
                (shot.spatial_anchor or "").strip(),
                "原镜头明示身高差证据：" + "；".join(height_evidence),
            )
            if part
        )
    required_text = shot.required_text
    if required_text is not None:
        try:
            beat_time = float(beat.get("time_s") or 0.0)
            appear_at = float(required_text.appear_start_s or 0.0)
            stable_until = (
                float(required_text.stable_until_s)
                if required_text.stable_until_s is not None
                else None
            )
            text_visible = appear_at <= beat_time and (stable_until is None or beat_time <= stable_until)
        except (TypeError, ValueError):
            text_visible = False
        update["required_text"] = (
            required_text.model_copy(update={"appear_start_s": 0.0, "stable_until_s": None})
            if text_visible
            else None
        )
    # 与视频提示词合同对齐：原镜头只要属于真实接触互动，所有时序帧都共用
    # 侧面轴线。不能让起势帧因“尚未接触”又回到正面，否则九图会互相冲突。
    if has_contact_action(shot):
        update["camera_angle"] = "侧面视角"
        update["risk_tags"] = _dedupe_str([*(shot.risk_tags or []), "timeline_contact_side_axis"])
    return shot.model_copy(deep=True, update=update)


def keyframe_contract_fingerprint(shot: Shot, bible: Bible) -> str:
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
        "geometry": _keyframe_contract(shot, bible),
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
        "character_anchors": _keyframe_character_anchors(shot, bible),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _keyframe_contract_instructions(shot: Shot, bible: Bible) -> list[str]:
    """最终发给图片模型的硬约束；必须放在 LLM 文案之后，以覆盖冲突描述。"""
    contract = _keyframe_contract(shot, bible)
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


def reference_generation_prompt(shot: Shot, bible: Bible, ref_type: str, index: int,
                                *, content_override: str | None = None) -> str:
    anchors = _keyframe_character_anchors(shot, bible)
    anchor_text = "; ".join(f"{name}: {appearance}" for name, appearance in anchors.items())
    # content_override：LLM 按剧本写的内容提示词。它只能补充美术细节，不能覆盖下方
    # 确定性构图合同。最终合同在截断后追加，避免第二人物/接触点被截掉。
    if content_override:
        body = content_override.strip()[:_KEYFRAME_LLM_PROMPT_MAX_CHARS]
    else:
        contract = _keyframe_contract(shot, bible)
        body = (
            f"Create one clean 9:16 anime-drama reference image for Seedance. "
            f"Reference type: {ref_type}. Shot {shot.shot_no}. Scene: {shot.scene_setting}. "
            f"Single narrative keyframe target: {contract['target_keyframe_desc']}."
        )
    common = (
        f"{body} Characters: {anchor_text or '(no visible character)'}. "
        f"Episode style: {bible.world.visual_style_canonical}. "
    )
    if ref_type != "plot_key_frame":
        return (
            common
            + "No text, no subtitles, no watermark, no logo, no extra limbs, no motion blur. 9:16 portrait. "
            "The image must be suitable as a Seedance 2.0 reference image."
        )
    mandatory = " ".join(_keyframe_contract_instructions(shot, bible))
    return f"{common}{mandatory} Policy version: {KEYFRAME_PROMPT_CONTRACT_VERSION}."


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
    " Reference images lock identity, outfit, style, and environment only—not pose, framing, camera, or physical "
    "height. Each character image is one separate named identity; never merge, swap, omit, or duplicate identities. "
    "Ignore crop-size differences and follow the mandatory action/geometry contract."
)


async def _generate_image_with_seed_fallback(prompt: str, seed_inputs: list[str] | None, *,
                                             call_meta: dict | None = None) -> dict[str, Any]:
    """带 i2i 种子生成参考图；仅在图片输入明确不可用时才降级为纯文生图。

    429/5xx/读超时等瞬时错误必须保留人物/场景种子交给上层重试，不能静默产出
    一张失去身份锚点的“成功”关键帧。上传写超时可去种子快速降级，因为原样重传
    大图只会继续占满上行。
    """
    try:
        return await hiagent.generate_image(
            prompt, size=config.REF_IMAGE_SIZE, image_inputs=seed_inputs or None, call_meta=call_meta)
    except ProviderError as exc:
        if not seed_inputs:
            raise
        detail = f"{exc} {getattr(exc, 'raw', '')}".lower()
        unsupported_markers = (
            "unsupported image", "image input is not supported", "reference image is not supported",
            "does not support image", "unsupported_image", "invalid image input", "image edit not supported",
            "不支持图片输入", "不支持参考图", "不支持图生图",
        )
        seedless_fallback_allowed = (
            getattr(exc, "timeout_phase", None) == "write"
            or any(marker in detail for marker in unsupported_markers)
        )
        if not seedless_fallback_allowed:
            raise
        return await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE, call_meta=call_meta)


async def review_reference_consistency(*, candidates: list[ReferenceImageAsset],
                                       anchors: list[ReferenceImageAsset],
                                       shot: Shot, bible: Bible) -> dict[str, Any]:
    """相对一致性检查 Agent（Phase 2）：把锚点图（定妆照/上镜尾帧=真值）与候选新参考图【一起】喂给 VLM，
    逐张给候选打「与锚点及同镜其他帧的一致性」分并点名漂移维度
    （服饰/发型/长相/体型/身高比例/画风/环境）。

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
    if not cand_pairs:
        return {"candidates": [], "overall": 1.0, "failed": False}

    char_txt = "; ".join(f"{c.name}: {c.appearance_canonical}"
                         for c in bible.characters if c.name in shot.characters)
    geometry = _keyframe_contract(shot, bible)
    k, n = len(anchor_b64), len(cand_pairs)
    expectation = (
        f"You are a reference-image CONSISTENCY reviewer for ONE anime-drama shot. I send {k + n} images "
        f"in order. The FIRST {k} are ANCHOR images = ground truth for each character's face/hairstyle/outfit/build "
        f"and for the scene environment/lighting. The NEXT {n} are CANDIDATE reference images for the SAME "
        f"shot, numbered 1..{n} in the order sent (after the anchors). For EACH candidate, judge whether the "
        "SAME character(s) keep an IDENTICAL face, hairstyle, body build, clothing design/colors/accessories, "
        "standing height and relative height ratio. Compare candidates directly with EACH OTHER as well as with "
        "the anchors: different crops or camera distance must never be mistaken for a height change. Also verify "
        "that art style / lighting / environment stay consistent. Pose, expression, gesture and camera framing are "
        "ALLOWED to differ — do NOT penalize those. "
        f"Character appearance reference (text): {char_txt or '(none)'}. "
        f"Scripted relative-height policy: {geometry.get('relative_height_policy') or 'natural fixed scale'}. "
        f"Explicit height evidence: {geometry.get('height_difference_evidence') or []}. "
        f"Art style: {bible.world.visual_style_canonical}. "
        'Output exactly one JSON object: {"candidates":[{"n":<1-based int>,"consistency":<0..1>,'
        '"drift":[<any of "costume","hair","face","body_build","height_ratio","style","environment">],'
        '"issues":[<short strings>]}],"overall":<0..1>}. consistency=1 means the protected character '
        "attributes are identical. Report even a subtle costume, face, build, or height-ratio change explicitly."
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
    from app.continuity import effective_characters_visible

    prompt = reference_generation_prompt(shot, bible, ref_type, index, content_override=content_override)
    if seed_inputs:
        prompt += _SEED_USAGE_NOTE
    if extra_instruction:
        prompt += " " + extra_instruction.strip()
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
    if skip_inline_qa:
        qa = {"overall": 1.0, "deferred_batch_qa": True, "issues": []}
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
        related_character_ids=(
            effective_characters_visible(shot) if ref_type in {"character", "plot_key_frame"} else []
        ),
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


async def write_reference_prompt(shot: Shot, bible: Bible, ref_type: str, *, intent: str | None = None) -> str:
    """为【单张】新参考图独立写一条详尽的 Seedream 英文提示词（一图一次 LLM 调用）。
    逐图独立调用 + 上游并发，避免一次性写多张时模型偷懒只给空泛短提示。失败返回空串（上游回退模板）。"""
    anchors = _keyframe_character_anchors(shot, bible)
    contract = _keyframe_contract(shot, bible)
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
) -> list[str]:
    """一镜一次返回全部槽位提示词合同（P1）。缺项/重复时仅对异常槽回退单图调用。"""
    anchors = _keyframe_character_anchors(shot, bible)
    planned = []
    slot_shots: list[Shot] = []
    for i, (slot_key, ref_type) in enumerate(slots):
        beat = beats[i] if beats and i < len(beats) else None
        slot_shot = _shot_for_keyframe_beat(shot, beat)
        slot_contract = _keyframe_contract(slot_shot, bible)
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
            "visible_characters": list(_keyframe_contract(shot, bible).get("visible_characters") or []),
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
        prompts[i] = await write_reference_prompt(slot_shots[i], bible, ref_type, intent=intent)
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
                ) or prompts[i]
    return prompts


async def _generate_reference_keep_best(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                        ref_type: str, index: int, content_override: str | None,
                                        retries: int, seed_inputs: list[str] | None = None,
                                        extra_instruction: str | None = None,
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
                seed_inputs=seed_inputs, extra_instruction=extra_instruction,
                skip_inline_qa=skip_inline_qa)
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
    # 单候选已经在证据化关键帧 QA 中与同一批人物/场景锚点比较过；
    # 组一致性只有在多张候选可能彼此打架时才提供新增信息。
    if len(candidates) < 2 or not anchors:
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


async def _enforce_timeline_keyframe_invariance(
    *,
    selected: list[ReferenceImageAsset],
    shot: Shot,
    bible: Bible,
    rejection_details: list[dict[str, Any]] | None = None,
    rejected_out: list[ReferenceImageAsset] | None = None,
) -> tuple[list[ReferenceImageAsset], set[str]]:
    """双关键帧人物不变量硬检查；无法证明一致时安全降级为单关键帧。"""
    timeline = sorted(
        [
            asset for asset in selected
            if asset.type == "plot_key_frame" and asset.selectedForSeedance and not asset.deleted
        ],
        key=lambda asset: (
            asset.keyframe_time_ratio if asset.keyframe_time_ratio is not None else 1.0,
            asset.keyframe_index if asset.keyframe_index is not None else 999,
        ),
    )[:_MAX_TIMELINE_KEYFRAMES]
    if len(timeline) <= 1:
        return selected, set()
    anchors = [
        asset for asset in selected
        if asset.source in {"asset_library", "previous_shot"} and not asset.deleted
    ]
    report = await review_reference_consistency(
        candidates=timeline, anchors=anchors, shot=shot, bible=bible,
    )
    scores = _consistency_scores(report)
    _annotate_consistency(timeline, scores)
    protected_drift = {
        "costume", "clothing", "outfit", "hair", "hairstyle", "face", "identity",
        "body_build", "build", "body", "height", "height_ratio", "relative_height",
    }

    def _verified(asset: ReferenceImageAsset) -> bool:
        info = scores.get(asset.id) or {}
        if report.get("failed") or info.get("check_failed") or info.get("consistency") is None:
            return False
        drift = {str(item).strip().lower() for item in (info.get("drift") or [])}
        return not (drift & protected_drift) and float(info["consistency"]) >= 0.97

    if all(_verified(asset) for asset in timeline):
        return selected, set()

    # 决定性 master 优先保留；第二张无法通过不变量复核时不冒险喂给视频模型。
    keeper = next((asset for asset in timeline if asset.slot_key == "narrative_keyframe"), None)
    if keeper is None:
        keeper = max(timeline, key=lambda asset: asset.qualityScore or 0.0)
    dropped_slots: set[str] = set()
    dropped_asset_ids: set[int] = set()
    for asset in timeline:
        if asset is keeper:
            continue
        asset.selectedForSeedance = False
        asset.deleted = False
        asset.rejectReason = "cross_keyframe_identity_invariance_unverified"
        asset.purposes = [purpose for purpose in (asset.purposes or []) if purpose != "video_input"]
        dropped_asset_ids.add(id(asset))
        if asset.slot_key:
            dropped_slots.add(str(asset.slot_key))
        if rejected_out is not None and asset not in rejected_out:
            rejected_out.append(asset)
        if rejection_details is not None:
            rejection_details.append({
                "type": asset.type,
                "source": asset.source,
                "reason": asset.rejectReason,
                "slot_key": asset.slot_key,
                "drift": (scores.get(asset.id) or {}).get("drift") or [],
                "consistency": (scores.get(asset.id) or {}).get("consistency"),
            })
    return [asset for asset in selected if id(asset) not in dropped_asset_ids], dropped_slots


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
        keyframe_runtime_blocking_failures,
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
    current_keyframe_fingerprint = keyframe_contract_fingerprint(shot, bible)
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
    # 只有 Bible 具名角色才有可复用定妆资产。功能性路人保留文字锚点，但不得
    # 抢占 refs_as_image_inputs 的前 N 个名额，导致后面主角定妆种子被截掉。
    identity_character_names = [name for name in visible_character_names if name in bible_character_names]

    # 冻结依赖 manifest：worker 重启复用；若本集人物/场景版本已变则判 stale 并重建
    reuse_frozen = False
    frozen_manifest = existing_meta.get("reference_manifest")
    if not prompt_contract_changed and existing_meta.get("reference_manifest_frozen") and isinstance(frozen_manifest, dict):
        current_probe = resolve_shot_asset_dependencies(
            project_id=project_id, episode_no=episode_no, shot_id=shot_id, shot=shot,
            scene_name=scene_name or None,
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
            scene_name=scene_name or None,
        )
        existing_meta["reference_manifest"] = manifest
        existing_meta["reference_manifest_frozen"] = True

    # 兼容保留门禁报告 API，但阻塞项只写入告警，不终止付费链路。
    manifest_warnings = assert_manifest_allows_production(manifest)
    if manifest_warnings:
        existing_meta["asset_manifest_gate_retry_exhausted"] = True
        existing_meta["asset_manifest_warnings"] = list(manifest_warnings)

    # 只有 action_continuation 才把上一镜尾帧作为强制参考图和剪辑点连贯锚点。
    forced: list[ReferenceImageAsset] = []
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    needs_tail = uses_previous_tail_frame(derive_continuity_mode(shot, prev=prev_shot))
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
                    and (asset.rejectReason is None or _is_score_only_reject_reason(asset.rejectReason))
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
            written = await write_reference_prompt(beat_shot, bible, ref_type, intent=brief)
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

    visual_anchors = library_anchor_assets_from_manifest(manifest)

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
        beat_shot = _shot_for_keyframe_beat(shot, beat_by_slot.get(slot_key))
        if ref_type == "plot_key_frame" or is_narrative_keyframe_slot(slot_key):
            qa = await review_keyframe_with_evidence(
                payload,
                shot=beat_shot,
                bible=bible,
                visual_anchors=visual_anchors,
                ref_type=ref_type,
            )
        else:
            qa = await review_reference_image(payload, shot=shot, bible=bible, ref_type=ref_type)
            qa.setdefault("status", "scored")
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
            recompose_asset_score(asset)
            asset.rejectReason = None
            candidate_statuses[(slot_key, candidate_no)] = "scored"
        asset.selectedForSeedance = False
        asset.purposes = [PURPOSE_QA_ANCHOR]
        _checkpoint_candidates(slot_key, "qa_pending")
        _publish_progress()

    # 双关键帧不能各自只看单图分数：在删除败选图之前，把两个槽的全部候选
    # 放在同一组里比较身份、服装、体型和身高比例，让一致性分参与各槽择优。
    if len(temporal_beats) > 1:
        joint_candidates = [
            asset
            for slot_key in sorted(active_candidate_slots)
            for _candidate_no, asset in candidate_pool.get(slot_key, [])
        ]
        if joint_candidates:
            joint_report = await review_reference_consistency(
                candidates=joint_candidates,
                anchors=video_anchor_assets,
                shot=shot,
                bible=bible,
            )
            joint_scores = _consistency_scores(joint_report)
            _annotate_consistency(joint_candidates, joint_scores)
            for slot_key in active_candidate_slots:
                _checkpoint_candidates(slot_key, "qa_pending")
            _publish_progress()

    def _numeric_qa_score(asset: ReferenceImageAsset) -> float | None:
        value = (asset.qa or {}).get("overall")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    eligible_by_slot: dict[str, list[tuple[int, ReferenceImageAsset]]] = {}
    for slot_key in active_candidate_slots:
        pairs = candidate_pool.get(slot_key, [])
        # 所有已落盘、技术可读的候选都可进入最终择优。结构/几何 QA 只决定
        # 风险标记和排序；三张均有问题时仍保留其中最佳一张。
        eligible_by_slot[slot_key] = list(pairs)
        structural = [
            {
                "candidate_no": candidate_no,
                "hard_failures": sorted(keyframe_runtime_blocking_failures(asset.qa or {})),
            }
            for candidate_no, asset in pairs
            if keyframe_runtime_blocking_failures(asset.qa or {})
        ]
        if structural and len(structural) == len(pairs):
            slot_state.setdefault(slot_key, {})["gate_retry_exhausted"] = True
            slot_state[slot_key]["gate_warnings"] = structural
    existing_meta["reference_slots"] = slot_state

    all_cleanup_errors: list[str] = []
    for slot_key in sorted(active_candidate_slots):
        slot_cleanup_errors: list[str] = []
        all_pairs = candidate_pool.get(slot_key, [])
        pairs = eligible_by_slot.get(slot_key, all_pairs)
        if not all_pairs:
            _checkpoint_candidates(slot_key, "technical_failed")
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
            passed = keyframe_gate_passed(winner.qa or {}) if (
                winner.type == "plot_key_frame" or is_narrative_keyframe_slot(slot_key)
            ) else apply_keep_gate(winner)
            structural_warnings = keyframe_runtime_blocking_failures(winner.qa or {})
            if passed and not structural_warnings:
                winner_status = "passed"
                winner.rejectReason = None
            else:
                winner_status = "scored_warning"
                winner.rejectReason = "quality_below_threshold_score_only"
                if structural_warnings:
                    winner.qa = {
                        **(winner.qa or {}),
                        "gate_retry_exhausted": True,
                        "runtime_blocking": False,
                    }
        if winner not in selected:
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
        rejection_details=rejection_details, rejected_out=rejected_out)
    video_candidates, invariant_dropped_slots = await _enforce_timeline_keyframe_invariance(
        selected=video_candidates,
        shot=shot,
        bible=bible,
        rejection_details=rejection_details,
        rejected_out=rejected_out,
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


async def assemble_continuity_tail(
    *, conn: Any, project_id: str, episode_no: int, episode_id: str, shot_id: str,
    shot: Shot, bible: Bible, meta: dict[str, Any], prev_shot: Any | None,
    rejection_details: list[dict[str, Any]] | None = None,
    rejected_out: list[ReferenceImageAsset] | None = None,
) -> list[ReferenceImageAsset]:
    """尾帧到达后：装配连续性参考并做最终组门禁；不重跑已通过静态槽位。"""
    from app.multiview import PURPOSE_VIDEO_INPUT, purpose_list

    refs = list(meta.get("reference_images") or [])
    selected: list[ReferenceImageAsset] = []
    for ref in refs:
        path = ref.get("path") or ref.get("image_path")
        url = str(ref.get("url") or "")
        if path and not Path(path).is_file():
            continue
        if not path and not url.startswith("data:image/"):
            continue
        if ref.get("type") == "previous_shot_frame":
            continue  # 用最新尾帧替换
        if path:
            asset = _asset_from_path(
                path=path,
                ref_type=ref.get("type") or "plot_key_frame",
                source=ref.get("source") or "pipeline",
                shot_id=ref.get("shotId") or shot_id,
                episode_id=ref.get("episodeId") or episode_id,
                quality_score=ref.get("qualityScore"),
                qa=ref.get("qa"),
                related_character_ids=list(ref.get("relatedCharacterIds") or []),
                entity_type=ref.get("entity_type"),
                entity_name=ref.get("entity_name"),
                library_revision_id=ref.get("library_revision_id"),
                library_view_id=ref.get("library_view_id"),
                view_role=ref.get("view_role"),
                purposes=purpose_list(ref),
                required=bool(ref.get("required")),
                slot_key=ref.get("slot_key"),
            )
        else:
            asset = ReferenceImageAsset(
                id=ref.get("id") or new_id("ref"),
                url=url,
                type=ref.get("type") or "plot_key_frame",
                source=ref.get("source") or "pipeline",
                shotId=ref.get("shotId") or shot_id,
                episodeId=ref.get("episodeId") or episode_id,
                relatedCharacterIds=list(ref.get("relatedCharacterIds") or []),
                qualityScore=ref.get("qualityScore"),
                qa=ref.get("qa"),
                entity_type=ref.get("entity_type"),
                entity_name=ref.get("entity_name"),
                library_revision_id=ref.get("library_revision_id"),
                library_view_id=ref.get("library_view_id"),
                view_role=ref.get("view_role"),
                purposes=purpose_list(ref),
                required=bool(ref.get("required")),
                slot_key=ref.get("slot_key"),
            )
        asset.id = ref.get("id") or asset.id
        asset.selectedForSeedance = bool(ref.get("selectedForSeedance"))
        asset.deleted = bool(ref.get("deleted"))
        asset.rejectReason = ref.get("rejectReason")
        asset.dependency_manifest = ref.get("dependency_manifest")
        asset.prompt_contract_version = ref.get("prompt_contract_version")
        asset.keyframe_contract_fingerprint = ref.get("keyframe_contract_fingerprint")
        asset.candidate_no = ref.get("candidate_no")
        asset.keyframe_index = ref.get("keyframe_index")
        asset.keyframe_total = ref.get("keyframe_total")
        asset.keyframe_time_ratio = ref.get("keyframe_time_ratio")
        asset.keyframe_target_desc = ref.get("keyframe_target_desc")

        # 静态参考图可能在等待上一镜尾帧期间被人工废弃，QA 淘汰图也会
        # 保留 video_input 用途供审计。两者都只能留在废弃画廊，不能参与
        # 连续性重装配，否则后续门禁可能把高分旧候选重新标成 selected。
        stale_video_candidate = (
            PURPOSE_VIDEO_INPUT in asset.purposes and not asset.selectedForSeedance
        )
        structural_reject = asset.rejectReason and not _is_score_only_reject_reason(asset.rejectReason)
        if asset.deleted or structural_reject or stale_video_candidate:
            asset.selectedForSeedance = False
            if rejected_out is not None and asset not in rejected_out:
                rejected_out.append(asset)
            continue
        selected.append(asset)

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
    # 时序关键帧虽然含人，但它们是不同剧情时刻，不是多张重复定妆照；
    # 不得消耗 character 配额或因帧数增加而被逐张扣分。
    char_refs = [a for a in assets if a.type == "character"]
    if len(char_refs) <= 1:
        for a in assets:
            if a.qualityScore is None and (a.qa or {}).get("overall") is None:
                continue
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
    distinct_identities = {
        str(asset.entity_name or "").strip()
        or next((str(name).strip() for name in asset.relatedCharacterIds if str(name).strip()), "")
        for asset in char_refs
    }
    distinct_identities.discard("")
    # One anchor per distinct named identity is required evidence, not
    # redundant imagery. Only extra views beyond that baseline are penalized.
    prefer = max(max_character_reference_images(), len(distinct_identities))
    penalties: dict[int, float] = {}
    for i, a in enumerate(ranked):
        if i < prefer:
            penalties[id(a)] = 0.0
        else:
            excess = i - prefer + 1
            penalties[id(a)] = min(_MAX_REDUNDANCY_PENALTY, 0.05 * excess)

    for a in assets:
        if a.qualityScore is None and (a.qa or {}).get("overall") is None:
            # QA 不可用时保留 None，不要把“未评分”伪造成 0 分。
            continue
        recompose_asset_score(a, redundancy_penalty=penalties.get(id(a), 0.0))


def _finalize_reference_selection(
    assets: list[ReferenceImageAsset],
    *,
    rejected_out: list[ReferenceImageAsset] | None = None,
    rejection_details: list[dict[str, Any]] | None = None,
) -> list[ReferenceImageAsset]:
    """Score-only：全部技术有效参考图保留；按分数排序，低分只记展示标记（PRD QA-SO #24）。"""
    del rejected_out, rejection_details
    if not assets:
        return []
    _apply_redundancy_penalties(assets)
    for asset in assets:
        apply_keep_gate(asset)
    return sorted(assets, key=lambda a: a.qualityScore or 0.0, reverse=True)


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


def _keyframe_identity_names(refs: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for ref in refs:
        if ref.get("deleted") or not ref.get("selectedForSeedance"):
            continue
        is_keyframe = (
            ref.get("type") == "plot_key_frame"
            or is_narrative_keyframe_slot(ref.get("slot_key"))
        )
        if is_keyframe:
            names.update(_reference_identity_names(ref))
    return names


def _suppress_character_anchors_covered_by_keyframes(refs: list[dict[str, Any]]) -> None:
    """关键帧已承载某人物时，不再把该人物定妆照作为第二张主体图发送。

    Seedance 的多图接口在供应商边界只接收通用 ``reference_image``，没有实体 ID
    可声明两张图是同一个人。同时发送定妆照和含该人物的剧情关键帧会诱发分身。
    定妆照仍保留为关键帧生成/QA 证据，只从本次视频实际输入中移除。
    """
    carried_identities = _keyframe_identity_names(refs)
    if not carried_identities:
        return
    for ref in refs:
        if (
            ref.get("deleted")
            or not ref.get("selectedForSeedance")
            or ref.get("type") != "character"
            or not (_reference_identity_names(ref) & carried_identities)
        ):
            continue
        ref["selectedForSeedance"] = False
        ref["purposes"] = [
            purpose for purpose in (ref.get("purposes") or [])
            if purpose != "video_input"
        ]
        ref["required"] = False
        ref["rejectReason"] = None
        ref["selection_reason"] = "身份已由剧情关键帧承载，避免同一人物双重注入"


def pack_reference_images_for_seedance(
    refs: list[dict[str, Any]], *, max_images: int | None = None,
    continuity_required: bool = False,
    max_keyframes: int | None = None,
) -> list[dict[str, Any]]:
    """必需用途优先装箱；分数只在同类候选内排序。关键帧不会被高分定妆照挤掉。"""
    from app.multiview import pack_references_by_purpose
    usable = []
    for r in refs:
        # ``purposes`` describes what an asset was generated for and is retained
        # on rejected candidates for audit.  Only the explicit selection flag is
        # authoritative for the current provider request.
        if r.get("selectedForSeedance") and not r.get("deleted"):
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
    # 供应商只看到一组无实体 ID 的 reference_image。剧情关键帧已经包含某人物时，
    # 再发送同一人物的全身定妆照会被解释成第二个画面主体；装箱时确定性去掉该冗余锚点。
    timeline_identities = set().union(
        *(_reference_identity_names(ref) for ref in timeline_winners),
    ) if timeline_winners else set()
    if timeline_identities:
        non_keyframes = [
            ref for ref in non_keyframes
            if not (
                ref.get("type") == "character"
                and (_reference_identity_names(ref) & timeline_identities)
            )
        ]
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
    # 注释必须与 build_seedance_image_inputs 使用同一装箱结果，否则 Reference N 会指向错图。
    packed_refs = pack_reference_images_for_seedance([asset.public_dict() for asset in assets])
    timeline_count = sum(1 for ref in packed_refs if ref.get("type") == "plot_key_frame")
    for idx, ref in enumerate(packed_refs, 1):
        label = {
            "character": "character",
            "scene": "scene",
            "prop": "prop",
            "style": "style",
            "previous_shot_frame": "previous shot clean frame",
            "plot_key_frame": "plot key frame",
        }.get(str(ref.get("type") or "reference"), str(ref.get("type") or "reference"))
        source = str(ref.get("source") or "pipeline").replace("_", " ")
        related = [str(name) for name in (ref.get("relatedCharacterIds") or []) if str(name)]
        chars = f"; related characters: {', '.join(related)}" if related else ""
        timeline = ""
        if ref.get("type") == "plot_key_frame":
            qa_beat = (ref.get("qa") or {}).get("keyframe_beat") if isinstance(ref.get("qa"), dict) else {}
            beat_index = ref.get("keyframe_index") or (qa_beat or {}).get("beat_index")
            beat_total = ref.get("keyframe_total") or (qa_beat or {}).get("beat_total") or timeline_count
            time_ratio = ref.get("keyframe_time_ratio")
            if time_ratio is None:
                time_ratio = (qa_beat or {}).get("time_ratio")
            target = ref.get("keyframe_target_desc") or (qa_beat or {}).get("target_desc")
            timing = ""
            try:
                timing = f", at {round(float(time_ratio) * 100)}% of the shot"
            except (TypeError, ValueError):
                pass
            timeline = (
                f"; chronological timeline beat {beat_index or '?'} of {beat_total or timeline_count}{timing}"
                + (f"; freeze only: {target}" if target else "")
            )
        lines.append(f"Reference image {idx}: use as {label}; source: {source}{chars}{timeline}.")
    if not lines:
        return prompt_text
    sequence_note = ""
    if timeline_count > 1:
        sequence_note = (
            " Treat the plot key frames as chronological waypoints of ONE continuous shot and interpolate through "
            "them in the numbered order. Do not show them simultaneously; no montage, split screen, collage, "
            "hard cuts, duplicated actors, or frozen slideshow. "
            + _MULTI_KEYFRAME_INVARIANCE_NOTE
        )
    note = (
        " Use the provided reference images as follows: "
        + " ".join(lines)
        + sequence_note
        + REFERENCE_SINGLE_INSTANCE_NOTE
    )
    return prompt_text + note


def build_seedance_image_inputs(meta: dict[str, Any]) -> list[tuple[str, str]]:
    mode = meta.get("mode") or REFERENCE_IMAGE_MODE
    if mode == REFERENCE_IMAGE_MODE:
        if meta.get("first_frame_path") or meta.get("last_frame_path"):
            raise ProviderError("REFERENCE_IMAGE_MODE must not pass first_frame or last_frame.")
        refs = meta.get("reference_images") or []
        if not refs:
            # 参考图/关键帧重试耗尽后的最终降级：Seedance 支持纯文本 content。
            # 返回空列表表示继续提交，而不是把已经发生的付费工作判失败。
            return []
        # 同步更新冻结 meta，使“视频实际输入”界面与真正发送给供应商的图片一致。
        _suppress_character_anchors_covered_by_keyframes(refs)
        # 使用中的图按综合分 Top-N 装箱；截断不改 selected，高分未入选仍留在画廊。
        sequence = meta.get("keyframe_sequence") or {}
        beats = sequence.get("beats") if isinstance(sequence, dict) else None
        keyframe_limit = len(beats) if isinstance(beats, list) and beats else _MAX_TIMELINE_KEYFRAMES
        usable = pack_reference_images_for_seedance(refs, max_keyframes=keyframe_limit)
        if not usable:
            return []
        out: list[tuple[str, str]] = []
        for ref in usable:
            if ref.get("path"):
                out.append((hiagent.data_url_from_file(ref["path"]), "reference_image"))
            elif ref.get("url"):
                out.append((ref["url"], "reference_image"))
        if not out:
            return []
        return out

    raise ProviderError("视频生成已固定为参考图模式，不再支持首尾帧输入。")
