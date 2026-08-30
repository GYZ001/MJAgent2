"""分镜视频生成模式判定：REFERENCE_IMAGE / FIRST_FRAME / FIRST_LAST_FRAME 决策数据结构与设置读取。"""
from __future__ import annotations


from dataclasses import asdict, dataclass, field
from typing import Any

from app.db import get_setting
from app.hiagent import ProviderError
from app.schemas import Bible, EpisodeScreenplay, Shot
from app.video_plan import VideoGenerationMode as VideoGenerationModeEnum




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


def _dedupe_str(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out
