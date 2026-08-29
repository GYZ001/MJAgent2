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
from typing import Any, Callable

from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.harness import model_gateway
from app.refs import visual_style_lock
from app.db import get_setting, new_id
from app.errors import code_ref
from app.hiagent import ProviderError
from app.schemas import Bible, EpisodeScreenplay, Shot, extract_json
from app.video_plan import (
    VideoGenerationMode as VideoGenerationModeEnum,
    VideoInputIntent,
)

REFERENCE_IMAGE_MODE = "REFERENCE_IMAGE_MODE"
FIRST_FRAME_MODE = "FIRST_FRAME_MODE"
FIRST_LAST_FRAME_MODE = "FIRST_LAST_FRAME_MODE"
VIDEO_INPUT_MODE = "VIDEO_INPUT_MODE"
VideoGenerationMode = VideoGenerationModeEnum

REFERENCE_INPUT_POLICY_VERSION = "library_assets_only_v1"

REFERENCE_IMAGE_TYPES = {
    "character",
    "scene",
    "prop",
    "style",
    "previous_shot_frame",
    "plot_key_frame",
}

# 关键帧提示词是分镜级可复用资产的一部分。升级该版本时，未经人工
KEYFRAME_PROMPT_CONTRACT_VERSION = "narrative_action_geometry_v18"
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


def _screenplay_call_kwargs(
    screenplay: EpisodeScreenplay | None,
) -> dict[str, EpisodeScreenplay]:
    """Avoid changing legacy monkeypatch/callable signatures for a null context."""
    return {"screenplay": screenplay} if screenplay is not None else {}


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
    videoInputIntent: str | None = None
    dependsOnShotId: str | None = None
    requiredAssets: list[dict[str, Any]] = field(default_factory=list)
    fallbackOrder: list[str] = field(default_factory=list)
    episodeVideoPlanId: str | None = None
    shotPlanId: str | None = None
    planRevision: int | None = None
    sourceStoryboardRevisionId: str | None = None
    capabilitySnapshotId: str | None = None
    inputRevisionFingerprints: dict[str, str] = field(default_factory=dict)
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


# 冗余软惩罚上限：VLM 图片质检已下线后，qualityScore 只承载"同镜含人物图过多
# 时的排序优先级"，不再是质量分。
_MAX_REDUNDANCY_PENALTY = 0.15


def _reference_runtime_blocking(asset: ReferenceImageAsset) -> bool:
    """Read the persisted typed gate; rejectReason prose has no authority."""
    return (asset.qa or {}).get("runtime_blocking") is True




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
    """生成台不再创建剧情关键帧或静态边界帧。"""
    return 0


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


def max_character_reference_images() -> int:
    """喂给视频模型时偏好的「含人物参考图」上限（装箱偏好 / 冗余惩罚参考，不再硬剔除）。
    多张不同尺度含人物图易触发分身/前景巨人；超额者扣分并在装箱时让位给更高分图。"""
    return max(1, int_setting("video_reference_max_character_images", 1))


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
    """Legacy selector facade; production mode choices come from the episode plan."""

    async def select(self, shot: Shot, bible: Bible, *, shot_row: Any | None = None,
                     prev_shot: Any | None = None) -> ShotVideoModeDecision:
        if shot_row is not None:
            try:
                shot_id = shot_row["id"]
            except (KeyError, TypeError):
                shot_id = None
            if shot_id:
                from app.video_plan import get_shot_plan
                planned = get_shot_plan(str(shot_id))
                if planned is not None:
                    return dict_to_decision(planned.model_dump(mode="json"))
        if int(shot.shot_no or 0) == 1:
            return default_reference_decision()
        raise ProviderError("第 2 镜起必须先发布整集 EpisodeVideoGenerationPlan")


def default_reference_decision() -> ShotVideoModeDecision:
    plan = ReferenceImagePlan(
        totalCount=0,
        generateNewCount=0,
        types=[],
    )
    return ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE,
        reason="场景首镜无可用上一视频尾帧，只使用人物谱与场景库现有图片。",
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
    try:
        mode = VideoGenerationModeEnum(str(data.get("mode") or REFERENCE_IMAGE_MODE))
    except ValueError:
        mode = VideoGenerationModeEnum.REFERENCE_IMAGE_MODE
    required_assets = data.get("requiredAssets")
    if required_assets is None:
        required_assets = data.get("required_assets")
    fallback_order = data.get("fallbackOrder")
    if fallback_order is None:
        fallback_order = data.get("fallback_order")
    intent = data.get("videoInputIntent")
    if intent is None:
        intent = data.get("video_input_intent")
    depends_on = data.get("dependsOnShotId")
    if depends_on is None:
        depends_on = data.get("depends_on_shot_id")
    return ShotVideoModeDecision(
        mode=mode,
        reason=str(data.get("reason") or default_reference_decision().reason),
        confidence=float(data.get("confidence", 1.0)),
        needReusePreviousScene=bool(data.get("needReusePreviousScene")),
        needGenerateNewReferences=(
            bool(data.get("needGenerateNewReferences"))
            if "needGenerateNewReferences" in data
            else mode == VideoGenerationModeEnum.REFERENCE_IMAGE_MODE
        ),
        referenceImagePlan=ReferenceImagePlan(
            totalCount=total,
            reusePreviousSceneCount=reuse,
            generateNewCount=generate,
            types=types,
            prompts=_parse_ref_prompts(plan_data.get("prompts")),
        ),
        videoInputIntent=str(intent) if intent else None,
        dependsOnShotId=str(depends_on) if depends_on else None,
        requiredAssets=list(required_assets or []),
        fallbackOrder=[
            str(item.value if isinstance(item, VideoGenerationModeEnum) else item)
            for item in (fallback_order or [])
        ],
        episodeVideoPlanId=data.get("episodeVideoPlanId") or data.get("episode_video_plan_id"),
        shotPlanId=data.get("shotPlanId") or data.get("shot_plan_id"),
        planRevision=data.get("planRevision") or data.get("plan_revision"),
        sourceStoryboardRevisionId=(
            data.get("sourceStoryboardRevisionId")
            or data.get("source_storyboard_revision_id")
        ),
        capabilitySnapshotId=(
            data.get("capabilitySnapshotId")
            or data.get("capability_snapshot_id")
        ),
        inputRevisionFingerprints=dict(
            data.get("inputRevisionFingerprints")
            or data.get("input_revision_fingerprints")
            or {}
        ),
        llmUsed=bool(data.get("llmUsed")),
        defaulted=bool(data.get("defaulted")),
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


def required_visual_anchor_names(manifest: dict[str, Any]) -> set[str]:
    """Return only identities whose typed contract requires a truth image."""
    return {
        str(character.get("name") or "").strip()
        for character in (manifest.get("characters") or [])
        if character.get("asset_required", True)
        and str(character.get("name") or "").strip()
    }


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
    from app.compiler import narrative_keyframe_target, shot_contact_phase
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
    declared_contact_phase = shot_contact_phase(shot)
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
        if declared_contact_phase == "established":
            beat_contact_phase = (
                "none" if phase == "opening"
                else "approach" if phase in {"onset", "progress"}
                else "established"
            )
        elif declared_contact_phase == "separated":
            beat_contact_phase = (
                "separated" if phase in {"decisive", "reaction", "closing"}
                else "established"
            )
        elif declared_contact_phase == "approach":
            beat_contact_phase = "none" if phase == "opening" else "approach"
        else:
            beat_contact_phase = "none"
        beats.append({
            "slot_key": slot_key,
            "beat_index": beat_index,
            "beat_total": count,
            "time_ratio": round(ratio, 4),
            "time_s": round(float(shot.duration_s or 0) * ratio, 3),
            "phase": phase,
            "contact_phase": beat_contact_phase,
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
    phase = str(beat.get("phase") or "").strip()
    if phase:
        inherited_tags = [
            tag for tag in (shot.risk_tags or [])
            if not str(tag).startswith("contact_phase:")
        ]
        beat_contact_phase = str(beat.get("contact_phase") or "none")
        update["risk_tags"] = _dedupe_str([
            *inherited_tags,
            f"timeline_keyframe_phase:{phase}",
            *(
                [f"contact_phase:{beat_contact_phase}"]
                if beat_contact_phase != "none" else []
            ),
        ])
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
        update["risk_tags"] = _dedupe_str([
            *(update.get("risk_tags") or shot.risk_tags or []),
            "timeline_contact_side_axis",
        ])
    return shot.model_copy(deep=True, update=update)


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


def _dedupe_str(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def _enforce_reference_consistency(*, selected: list[ReferenceImageAsset], shot: Shot, bible: Bible,
                                         project_id: str, episode_no: int,
                                         rejection_details: list[dict[str, Any]] | None = None,
                                         rejected_out: list[ReferenceImageAsset] | None = None,
                                         screenplay: EpisodeScreenplay | None = None,
                                         ) -> list[ReferenceImageAsset]:
    """VLM 参考图一致性质检已下线：不再跨候选比对锚点、不再触发漂移重生。

    技术产物存在即可用——已生成的候选原样放行，去留交给人工在成片里判断。
    保留全部形参与返回类型不变，使调用方无需改动；``rejection_details``/
    ``rejected_out`` 不再被写入（没有一致性检查就没有可报告的一致性拒绝理由）。
    """
    del project_id, episode_no, rejection_details, rejected_out, shot, bible, screenplay
    return selected


async def _enforce_timeline_keyframe_invariance(
    *,
    selected: list[ReferenceImageAsset],
    shot: Shot,
    bible: Bible,
    rejection_details: list[dict[str, Any]] | None = None,
    rejected_out: list[ReferenceImageAsset] | None = None,
    screenplay: EpisodeScreenplay | None = None,
) -> tuple[list[ReferenceImageAsset], set[str]]:
    """VLM 跨关键帧一致性核查已下线：不再对双关键帧做"无法证明一致就降级为单帧"处理。

    这曾是一条真实的安全阀（识别不到一致性证据时主动砍掉第二张关键帧，避免把
    可能撞脸/换装的画面喂给视频模型）；VLM 判定本身被判定为不可靠后，这条阀门
    没有替代判据可用，只能整体让位给人工在成片里复核——多关键帧之间的身份漂移
    风险由此变为需要人工留意的已知限制，不再有自动化兜底。
    """
    del shot, bible, rejection_details, rejected_out, screenplay
    return selected, set()



async def _build_library_reference_assets(
    *,
    conn: Any,
    project_id: str,
    episode_no: int,
    episode_id: str,
    shot_id: str,
    shot: Shot,
    bible: Bible,
    on_progress: Callable[
        [list[ReferenceImageAsset], list[ReferenceImageAsset]], None
    ] | None = None,
    existing_meta: dict[str, Any] | None = None,
    screenplay: EpisodeScreenplay | None = None,
) -> list[ReferenceImageAsset]:
    """Resolve existing character/scene-library images without generating media."""
    from app.continuity import effective_characters_visible
    from app.multiview import (
        PURPOSE_QA_ANCHOR,
        PURPOSE_VIDEO_INPUT,
        assert_manifest_allows_production,
        library_anchor_assets_from_manifest,
        resolve_shot_asset_dependencies,
    )

    meta = existing_meta if existing_meta is not None else {}
    scene_name = str(getattr(shot, "scene_name", "") or "").strip()
    visible_names = effective_characters_visible(shot)
    bible_names = {character.name for character in bible.characters}
    if screenplay is not None and screenplay.narrative_plan is not None:
        from app.identity_contracts import narrative_identity_resolver

        resolver = narrative_identity_resolver(bible, screenplay)
        identity_names = list(dict.fromkeys(
            identity.asset_name
            for identity in (
                resolver.resolve(name, usage="visual") for name in visible_names
            )
            if identity.allows_asset
        ))
    else:
        identity_names = [name for name in visible_names if name in bible_names]

    manifest = resolve_shot_asset_dependencies(
        project_id=project_id,
        episode_no=episode_no,
        shot_id=shot_id,
        shot=shot,
        scene_name=scene_name or None,
        conn=conn,
        bible=bible,
        screenplay=screenplay,
    )
    warnings = assert_manifest_allows_production(manifest)
    if warnings:
        meta["asset_manifest_gate_retry_exhausted"] = True
        meta["asset_manifest_warnings"] = list(warnings)

    assets: list[ReferenceImageAsset] = []
    for anchor in library_anchor_assets_from_manifest(manifest):
        entity_type = str(anchor.get("entity_type") or anchor.get("type") or "")
        if entity_type not in {"character", "scene"}:
            continue
        path = str(anchor.get("image_path") or "").strip()
        if not path or not Path(path).is_file():
            continue
        try:
            assets.append(_asset_from_path(
                path=path,
                ref_type=entity_type,
                source="asset_library",
                related_character_ids=(
                    [str(anchor.get("entity_name"))]
                    if entity_type == "character" and anchor.get("entity_name")
                    else None
                ),
                qa={"status": "library", "overall": None, "issues": []},
                entity_type=entity_type,
                entity_name=anchor.get("entity_name"),
                library_revision_id=anchor.get("library_revision_id"),
                library_view_id=anchor.get("library_view_id"),
                view_role=anchor.get("view_role"),
                purposes=[PURPOSE_QA_ANCHOR],
            ))
        except OSError:
            continue

    if not any(asset.entity_type == "character" for asset in assets):
        assets.extend(character_reference_assets(
            bible,
            identity_names,
            limit=max(1, len(identity_names)),
            project_id=project_id,
            episode_no=episode_no,
        ))
    if not any(asset.entity_type == "scene" for asset in assets):
        assets.extend(scene_reference_assets(
            bible,
            scene_name,
            project_id=project_id,
            episode_no=episode_no,
        ))
    assets = [
        asset for asset in _dedupe_assets(assets)
        if (asset.entity_type or asset.type) in {"character", "scene"}
        and asset.source == "asset_library"
    ]

    role_priority = {
        "front_full": 0,
        "three_quarter": 1,
        "profile": 2,
        "side_full": 2,
        "action_zone": 0,
        "establishing": 1,
        "reverse_angle": 2,
    }

    def _rank(asset: ReferenceImageAsset) -> tuple[int, int, str]:
        kind = asset.entity_type or asset.type
        kind_rank = 0 if kind == "character" else 1
        return (
            kind_rank,
            role_priority.get(str(asset.view_role or ""), 9),
            asset.path or asset.id,
        )

    selected: list[ReferenceImageAsset] = []
    selected_names: set[str] = set()
    for asset in sorted(assets, key=_rank):
        if len(selected) >= max_reference_images():
            break
        if (asset.entity_type or asset.type) != "character":
            continue
        name = str(asset.entity_name or "").strip()
        if identity_names and name not in identity_names:
            continue
        key = name or "|".join(asset.relatedCharacterIds)
        if key in selected_names:
            continue
        selected_names.add(key)
        selected.append(asset)
    scene_asset = next(
        (
            asset for asset in sorted(assets, key=_rank)
            if (asset.entity_type or asset.type) == "scene"
        ),
        None,
    )
    if scene_asset is not None and len(selected) < max_reference_images():
        selected.append(scene_asset)

    selected_ids = {id(asset) for asset in selected}
    for asset in assets:
        asset.shotId = shot_id
        asset.episodeId = episode_id
        asset.required = id(asset) in selected_ids
        asset.selectedForSeedance = id(asset) in selected_ids
        asset.purposes = _dedupe_str([
            *(asset.purposes or []),
            PURPOSE_QA_ANCHOR,
            *([PURPOSE_VIDEO_INPUT] if id(asset) in selected_ids else []),
        ])

    meta.update({
        "reference_input_policy_version": REFERENCE_INPUT_POLICY_VERSION,
        "reference_manifest": manifest,
        "reference_manifest_frozen": True,
        "reference_slots": {},
        "keyframe_sequence": {"beats": [], "beat_count": 0},
        "narrative_keyframe_missing": False,
    })
    for key in (
        "keyframe_fallback_mode",
        "keyframe_structural_fallback_slots",
        "keyframe_contract_fingerprint",
    ):
        meta.pop(key, None)
    if on_progress is not None:
        on_progress(list(assets), [])
    return assets if selected else []


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
                                 existing_meta: dict[str, Any] | None = None,
                                 screenplay: EpisodeScreenplay | None = None) -> list[ReferenceImageAsset]:
    return await _build_library_reference_assets(
        conn=conn,
        project_id=project_id,
        episode_no=episode_no,
        episode_id=episode_id,
        shot_id=shot_id,
        shot=shot,
        bible=bible,
        on_progress=on_progress,
        existing_meta=existing_meta,
        screenplay=screenplay,
    )


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


async def assemble_continuity_tail(
    *, conn: Any, project_id: str, episode_no: int, episode_id: str, shot_id: str,
    shot: Shot, bible: Bible, meta: dict[str, Any], prev_shot: Any | None,
    rejection_details: list[dict[str, Any]] | None = None,
    rejected_out: list[ReferenceImageAsset] | None = None,
    screenplay: EpisodeScreenplay | None = None,
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
        structural_reject = _reference_runtime_blocking(asset)
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
        screenplay=screenplay,
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
    """对超额含人物图按类型优先级降低排序位置；不直接踢出，只影响装箱时的先后顺序。

    VLM 图片质检已下线：``qualityScore`` 不再是质量分（生成成功即恒为 1.0），这里
    把它复用成"冗余优先级"排序键——超出配额的图片分数被扣低，装箱阶段自然让位
    给配额内的图片，行为与原来"综合分里叠一层冗余惩罚"等价，只是不再有 QA 分量。
    """
    # 时序关键帧虽然含人，但它们是不同剧情时刻，不是多张重复定妆照；
    # 不得消耗 character 配额或因帧数增加而被逐张扣分。
    char_refs = [a for a in assets if a.type == "character"]
    if len(char_refs) <= 1:
        return

    def _rank_key(a: ReferenceImageAsset) -> tuple:
        # 与旧优先级对齐：尾帧 > 生成图 > 定妆照/其他
        if a.type == "previous_shot_frame":
            pri = 0
        elif a.source == "seedream_generated":
            pri = 1
        else:
            pri = 2
        return pri

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
    for i, a in enumerate(ranked):
        if i < prefer:
            continue
        excess = i - prefer + 1
        penalty = min(_MAX_REDUNDANCY_PENALTY, 0.05 * excess)
        a.qualityScore = max(0.0, (a.qualityScore if a.qualityScore is not None else 1.0) - penalty)


def _finalize_reference_selection(
    assets: list[ReferenceImageAsset],
    *,
    rejected_out: list[ReferenceImageAsset] | None = None,
    rejection_details: list[dict[str, Any]] | None = None,
) -> list[ReferenceImageAsset]:
    """技术产物存在即可用：全部技术有效参考图保留，按冗余优先级排序装箱顺序。"""
    del rejected_out, rejection_details
    if not assets:
        return []
    _apply_redundancy_penalties(assets)
    for asset in assets:
        asset.selectedForSeedance = True
        asset.rejectReason = None
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
            raise ProviderError(
                "REFERENCE_IMAGE_MODE 缺少通过门禁的 reference_image，禁止纯文本提交"
            )
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
