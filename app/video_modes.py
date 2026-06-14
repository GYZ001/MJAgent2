from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from app import config, hiagent
from app.db import get_conn, get_setting, new_id
from app.hiagent import ProviderError
from app.schemas import Bible, Shot, extract_json

FIRST_LAST_FRAME_MODE = "FIRST_LAST_FRAME_MODE"
REFERENCE_IMAGE_MODE = "REFERENCE_IMAGE_MODE"
VideoGenerationMode = Literal["FIRST_LAST_FRAME_MODE", "REFERENCE_IMAGE_MODE"]

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
    totalCount: int = 4
    reusePreviousSceneCount: int = 0
    generateNewCount: int = 4
    types: list[str] = field(default_factory=lambda: ["character", "scene", "plot_key_frame"])
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
    ruleMode: VideoGenerationMode | None = None
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

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
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


def reference_mode_enabled() -> bool:
    return bool_setting("video_generation_enable_reference_image_mode", True)


def max_reference_images() -> int:
    # Keep the default at 8, but allow deployments that have verified a 9-image limit to opt in.
    return max(1, min(int_setting("video_reference_max_images", 8), 9))


def quality_threshold() -> float:
    return float_setting("video_reference_quality_threshold", 0.75)


def fallback_failure_threshold() -> int:
    return max(1, int_setting("video_reference_fallback_failures", 2))


def selector_confidence_threshold() -> float:
    return float_setting("video_mode_selector_confidence_threshold", 0.7)


def _text_for_rules(shot: Shot) -> str:
    dialogue_text = " ".join(f"{d.speaker} {d.line} {d.emotion}" for d in shot.dialogues)
    return " ".join([
        shot.scene_setting or "",
        shot.action_desc or "",
        shot.first_frame_desc or "",
        shot.last_frame_desc or "",
        shot.transition or "",
        dialogue_text,
    ]).lower()


def _contains_any(text: str, words: list[str]) -> bool:
    return any(w.lower() in text for w in words)


def _rule_mode(shot: Shot) -> tuple[VideoGenerationMode, str, float]:
    text = _text_for_rules(shot)
    strong_words = [
        "fight", "battle", "explode", "explosion", "transform", "spell", "magic", "blast",
        "打斗", "战斗", "搏斗", "爆气", "爆炸", "法术", "施法", "变身", "快速转身", "转场",
        "过渡到", "落点", "结尾画面", "尾帧", "强控制", "冲刺", "闪现",
    ]
    light_words = [
        "dialogue", "talk", "walk", "stand", "sit", "look back", "scene continues",
        "对话", "说", "交谈", "站", "坐", "走", "回头", "场景延续", "连续出场",
        "情绪", "环境", "展示", "看向", "轻声",
    ]
    if _contains_any(text, strong_words):
        return FIRST_LAST_FRAME_MODE, "Rule matched strong action, transition, or end-state control.", 0.82
    # 连贯镜头必须走首尾帧模式：它能拿上一镜尾图作为本镜 first_frame，实现剪辑点的逐帧接续
    # （PRD §5.4 的链式衔接）。参考图模式与 first_frame 互斥，会丢掉这个确切接续，导致跳变。
    if bool(shot.continuity_from_prev):
        return FIRST_LAST_FRAME_MODE, "Scene continuity needs exact first-frame handoff from the previous shot.", 0.85
    if shot.dialogues or _contains_any(text, light_words):
        return REFERENCE_IMAGE_MODE, "Rule matched dialogue or light action without continuity.", 0.84
    return REFERENCE_IMAGE_MODE, "Default ordinary story shot favors character and scene continuity.", 0.72


def _scene_continues(prev_shot: Any | None, shot_row: Any | None, shot: Shot) -> bool:
    if not prev_shot:
        return bool(shot.continuity_from_prev)
    prev_scene = (prev_shot["scene_setting"] if hasattr(prev_shot, "keys") else prev_shot.get("scene_setting", "")) or ""
    cur_scene = (shot_row["scene_setting"] if shot_row is not None and hasattr(shot_row, "keys") else shot.scene_setting) or ""
    return bool(shot.continuity_from_prev) or prev_scene.strip() == cur_scene.strip()


def _reference_plan(shot: Shot, *, is_first_shot: bool, same_scene_as_prev: bool) -> ReferenceImagePlan:
    max_refs = max_reference_images()
    complex_shot = len(shot.characters) >= 2 or len((shot.scene_setting or "") + (shot.action_desc or "")) > 90
    if is_first_shot:
        total = int_setting("video_reference_first_shot_complex_count", 8) if complex_shot else int_setting("video_reference_first_shot_default_count", 4)
        total = min(max(total, 4), max_refs)
        return ReferenceImagePlan(totalCount=total, reusePreviousSceneCount=0, generateNewCount=total,
                                  types=["character", "scene", "prop", "style", "plot_key_frame"])
    if same_scene_as_prev:
        reuse = min(int_setting("video_reference_reuse_previous_scene_max_count", 4), 4, max_refs)
        generated = 1 if not complex_shot else 2
        total = min(max(4, reuse + generated), max_refs)
        generated = max(0, total - reuse)
        return ReferenceImagePlan(totalCount=total, reusePreviousSceneCount=reuse, generateNewCount=generated,
                                  types=["scene", "previous_shot_frame", "character", "plot_key_frame"])
    total = min(8 if complex_shot else 4, max_refs)
    return ReferenceImagePlan(totalCount=total, reusePreviousSceneCount=0, generateNewCount=total,
                              types=["character", "scene", "plot_key_frame"])


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
    def select_by_rules(self, shot: Shot, *, shot_row: Any | None = None, prev_shot: Any | None = None) -> ShotVideoModeDecision:
        mode, reason, confidence = _rule_mode(shot)
        is_first = int(getattr(shot, "shot_no", 0) or 0) <= 1
        same_scene = _scene_continues(prev_shot, shot_row, shot)
        plan = _reference_plan(shot, is_first_shot=is_first, same_scene_as_prev=same_scene)
        if mode == FIRST_LAST_FRAME_MODE:
            plan = ReferenceImagePlan(totalCount=0, reusePreviousSceneCount=0, generateNewCount=0, types=[])
        return ShotVideoModeDecision(
            mode=mode,
            reason=reason,
            confidence=confidence,
            needReusePreviousScene=mode == REFERENCE_IMAGE_MODE and same_scene and not is_first,
            needGenerateNewReferences=mode == REFERENCE_IMAGE_MODE and plan.generateNewCount > 0,
            referenceImagePlan=plan,
            ruleMode=mode,
        )

    async def select(self, shot: Shot, bible: Bible, *, shot_row: Any | None = None,
                     prev_shot: Any | None = None) -> ShotVideoModeDecision:
        rule = self.select_by_rules(shot, shot_row=shot_row, prev_shot=prev_shot)
        if not reference_mode_enabled():
            rule.mode = FIRST_LAST_FRAME_MODE
            rule.reason = "Reference image mode is disabled by configuration."
            rule.defaulted = True
            rule.referenceImagePlan = ReferenceImagePlan(totalCount=0, reusePreviousSceneCount=0, generateNewCount=0, types=[])
            return rule
        configured = (get_setting("video_generation_default_mode") or "AUTO").strip().upper()
        if configured in {FIRST_LAST_FRAME_MODE, REFERENCE_IMAGE_MODE}:
            rule.mode = configured  # type: ignore[assignment]
            rule.reason = f"Forced by video_generation_default_mode={configured}."
            rule.defaulted = True
            if configured == FIRST_LAST_FRAME_MODE:
                rule.referenceImagePlan = ReferenceImagePlan(totalCount=0, reusePreviousSceneCount=0, generateNewCount=0, types=[])
            return rule

        try:
            raw = await hiagent.chat([
                {"role": "system", "content": "Return exactly one JSON object for video mode selection."},
                {"role": "user", "content": _selector_prompt(shot, bible, rule)},
            ], temperature=0, max_tokens=800)
            data = extract_json(raw)
            mode = data.get("mode")
            conf = float(data.get("confidence", 0))
            if mode not in {FIRST_LAST_FRAME_MODE, REFERENCE_IMAGE_MODE} or conf < selector_confidence_threshold():
                rule.defaulted = True
                return rule
            rule.mode = mode
            rule.reason = str(data.get("reason") or rule.reason)[:300]
            rule.confidence = max(0.0, min(1.0, conf))
            rule.needReusePreviousScene = bool(data.get("needReusePreviousScene", rule.needReusePreviousScene))
            rule.needGenerateNewReferences = bool(data.get("needGenerateNewReferences", rule.needGenerateNewReferences))
            if isinstance(data.get("referenceImagePlan"), dict) and mode == REFERENCE_IMAGE_MODE:
                plan_data = data["referenceImagePlan"]
                plan = ReferenceImagePlan(
                    totalCount=int(plan_data.get("totalCount", rule.referenceImagePlan.totalCount)),
                    reusePreviousSceneCount=int(plan_data.get("reusePreviousSceneCount", rule.referenceImagePlan.reusePreviousSceneCount)),
                    generateNewCount=int(plan_data.get("generateNewCount", rule.referenceImagePlan.generateNewCount)),
                    types=[t for t in plan_data.get("types", rule.referenceImagePlan.types) if t in REFERENCE_IMAGE_TYPES],
                    prompts=_parse_ref_prompts(
                        plan_data.get("prompts")
                        or plan_data.get("newReferenceImages")
                        or data.get("newReferenceImages")),
                )
                plan.totalCount = min(max(plan.totalCount, 1), max_reference_images())
                plan.reusePreviousSceneCount = min(max(plan.reusePreviousSceneCount, 0), plan.totalCount)
                plan.generateNewCount = min(max(plan.generateNewCount, 0), plan.totalCount - plan.reusePreviousSceneCount)
                plan.prompts = plan.prompts[:plan.generateNewCount]
                rule.referenceImagePlan = plan
            if mode == FIRST_LAST_FRAME_MODE:
                rule.referenceImagePlan = ReferenceImagePlan(totalCount=0, reusePreviousSceneCount=0, generateNewCount=0, types=[])
            rule.llmUsed = True
            return rule
        except Exception:
            rule.defaulted = True
            return rule


def _selector_prompt(shot: Shot, bible: Bible, rule: ShotVideoModeDecision) -> str:
    characters = ", ".join(shot.characters)
    char_anchors = {c.name: c.appearance_canonical for c in bible.characters if c.name in shot.characters}
    return json.dumps({
        "task": (
            "You are the video director. Read the shot's script and storyboard, then decide "
            "(1) which Seedance 2.0 video generation mode fits, and (2) the full reference-image plan: "
            "how many reference images this shot needs, where each one comes from (reuse a previous "
            "shot's clean frame, the character asset library, or newly generated), and the exact image "
            "prompt for every NEW reference image. Base every choice on the script content, not on fixed rules."
        ),
        "allowed_modes": [FIRST_LAST_FRAME_MODE, REFERENCE_IMAGE_MODE],
        "mode_guidance": {
            FIRST_LAST_FRAME_MODE: "Strong/precise motion, transformations, explosions, or exact end-state control, and continuity shots that must hand off the previous shot's tail frame.",
            REFERENCE_IMAGE_MODE: "Dialogue, emotion, light action, or establishing shots where character & scene consistency matter more than frame-exact motion. Requires reference images.",
        },
        "reference_sources": {
            "previous_shot_frame": "reuse a QA-passed clean frame from the previous shot (only when same scene continues)",
            "asset_library": "character locked-design portrait from the bible (one per character on screen)",
            "generate_new": "a freshly generated reference image you must write a prompt for",
        },
        "allowed_reference_types": sorted(t for t in REFERENCE_IMAGE_TYPES if t != "previous_shot_frame"),
        "rule_recommendation": asdict(rule),
        "shot": {
            "shot_no": shot.shot_no,
            "scene_setting": shot.scene_setting,
            "characters": characters,
            "character_appearance": char_anchors,
            "action_desc": shot.action_desc,
            "first_frame_desc": shot.first_frame_desc,
            "last_frame_desc": shot.last_frame_desc,
            "dialogues": [d.model_dump() if hasattr(d, "model_dump") else dict(d) for d in shot.dialogues],
            "transition": shot.transition,
            "continuity_from_prev": shot.continuity_from_prev,
        },
        "style": bible.world.visual_style_canonical,
        "constraints": {
            "max_total_reference_images": max_reference_images(),
            "totalCount = reusePreviousSceneCount + generateNewCount": True,
            "len(referenceImagePlan.prompts) must equal generateNewCount": True,
            "prompts must be in English, 9:16, no text/watermark/extra limbs": True,
        },
        "output_schema": {
            "mode": "REFERENCE_IMAGE_MODE or FIRST_LAST_FRAME_MODE",
            "reason": "short reason grounded in the script",
            "confidence": "0..1",
            "needReusePreviousScene": True,
            "needGenerateNewReferences": True,
            "referenceImagePlan": {
                "totalCount": 4,
                "reusePreviousSceneCount": 2,
                "generateNewCount": 2,
                "types": ["scene", "character", "plot_key_frame"],
                "prompts": [
                    {"type": "scene", "prompt": "Concrete English prompt for new reference image #1 derived from the script ..."},
                    {"type": "plot_key_frame", "prompt": "Concrete English prompt for new reference image #2 ..."},
                ],
            },
        },
    }, ensure_ascii=False)


def decision_to_dict(decision: ShotVideoModeDecision) -> dict[str, Any]:
    data = asdict(decision)
    return data


def dict_to_decision(data: dict[str, Any]) -> ShotVideoModeDecision:
    plan_data = data.get("referenceImagePlan") or {}
    return ShotVideoModeDecision(
        mode=data.get("mode") if data.get("mode") in {FIRST_LAST_FRAME_MODE, REFERENCE_IMAGE_MODE} else REFERENCE_IMAGE_MODE,
        reason=str(data.get("reason") or ""),
        confidence=float(data.get("confidence", 0)),
        needReusePreviousScene=bool(data.get("needReusePreviousScene")),
        needGenerateNewReferences=bool(data.get("needGenerateNewReferences")),
        referenceImagePlan=ReferenceImagePlan(
            totalCount=int(plan_data.get("totalCount", 0)),
            reusePreviousSceneCount=int(plan_data.get("reusePreviousSceneCount", 0)),
            generateNewCount=int(plan_data.get("generateNewCount", 0)),
            types=list(plan_data.get("types") or []),
            prompts=_parse_ref_prompts(plan_data.get("prompts")),
        ),
        ruleMode=data.get("ruleMode"),
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
                     quality_score: float | None = None, qa: dict[str, Any] | None = None) -> ReferenceImageAsset:
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
    )


def _scene_qa_ok(scene_row: Any, threshold: float) -> tuple[bool, float | None, dict[str, Any] | None, str | None]:
    qa = json.loads(scene_row["qa_json"]) if scene_row["qa_json"] else {}
    score = qa.get("overall")
    if score is None:
        return False, None, qa, "missing_quality_score"
    if float(score) < threshold:
        return False, float(score), qa, "quality_below_threshold"
    issues = " ".join(str(x) for x in qa.get("issues") or [])
    banned = ["watermark", "subtitle", "text", "blur", "collapse", "deformed", "字幕", "水印", "模糊", "崩", "畸形", "错误文字"]
    if _contains_any(issues.lower(), banned):
        return False, float(score), qa, "quality_issue_blocks_reuse"
    return True, float(score), qa, None


def reusable_previous_assets(conn: Any, *, prev_shot: Any | None, limit: int, threshold: float) -> list[ReferenceImageAsset]:
    if not prev_shot or limit <= 0:
        return []
    action = ((prev_shot["action_desc"] if hasattr(prev_shot, "keys") else prev_shot.get("action_desc", "")) or "").lower()
    polluted = ["爆炸", "打斗", "爆气", "法术", "强特效", "explosion", "fight", "spell", "blast"]
    if _contains_any(action, polluted):
        # Only VLM/scene QA-passed frames below will be reused.
        pass
    shot_id = prev_shot["id"] if hasattr(prev_shot, "keys") else prev_shot.get("id")
    rows = conn.execute(
        """SELECT id, shot_id, kind, image_path, qa_json
           FROM shot_scenes
           WHERE shot_id=? AND status='succeeded' AND image_path IS NOT NULL
           ORDER BY CASE kind WHEN 'head' THEN 0 WHEN 'tail' THEN 1 ELSE 2 END, version_no DESC""",
        (shot_id,),
    ).fetchall()
    assets: list[ReferenceImageAsset] = []
    for row in rows:
        if len(assets) >= limit:
            break
        ok, score, qa, reject = _scene_qa_ok(row, threshold)
        if not ok:
            continue
        if not Path(row["image_path"]).exists():
            continue
        assets.append(_asset_from_path(
            path=row["image_path"],
            ref_type="scene" if row["kind"] == "head" else "previous_shot_frame",
            source="previous_shot",
            shot_id=row["shot_id"],
            scene_id=row["id"],
            quality_score=score,
            qa=qa,
        ))
    return assets


def character_reference_assets(bible: Bible, character_names: list[str], *, limit: int) -> list[ReferenceImageAsset]:
    assets: list[ReferenceImageAsset] = []
    by_name = {c.name: c for c in bible.characters}
    for name in character_names:
        if len(assets) >= limit:
            break
        c = by_name.get(name)
        path = getattr(c, "ref_image_path", None) if c else None
        if not path or not Path(path).exists():
            continue
        try:
            assets.append(_asset_from_path(
                path=path,
                ref_type="character",
                source="asset_library",
                related_character_ids=[name],
                quality_score=1.0,
                qa={"overall": 1.0, "issues": []},
            ))
        except OSError:
            continue
    return assets


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
    raw = await hiagent.vlm_check([image_b64], json.dumps(expectation, ensure_ascii=False))
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


async def _generate_one_reference(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                  ref_type: str, index: int, content_override: str | None = None) -> ReferenceImageAsset:
    dest = reference_image_path(project_id, episode_no, shot.shot_no, ref_type, index)
    prompt = reference_generation_prompt(shot, bible, ref_type, index, content_override=content_override)
    item = await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE)
    if item.get("url"):
        await hiagent.download(item["url"], str(dest))
    elif item.get("b64_json"):
        dest.write_bytes(base64.b64decode(item["b64_json"]))
    else:
        raise ProviderError(f"Reference image response missing url/b64_json: {list(item.keys())}")
    qa = await review_reference_image(hiagent.encode_image_file(str(dest)), shot=shot, bible=bible, ref_type=ref_type)
    asset = _asset_from_path(
        path=str(dest),
        ref_type=ref_type,
        source="seedream_generated",
        quality_score=float(qa.get("overall", 0)),
        qa=qa,
        related_character_ids=list(shot.characters) if ref_type in {"character", "plot_key_frame"} else [],
    )
    if asset.qualityScore is None or asset.qualityScore < quality_threshold():
        asset.rejectReason = "quality_below_threshold"
    return asset


async def build_reference_assets(*, conn: Any, project_id: str, episode_no: int, episode_id: str,
                                 shot_id: str, shot: Shot, bible: Bible,
                                 decision: ShotVideoModeDecision, prev_shot: Any | None = None,
                                 rejection_details: list[dict[str, Any]] | None = None) -> list[ReferenceImageAsset]:
    plan = decision.referenceImagePlan
    threshold = quality_threshold()
    max_refs = max_reference_images()
    selected: list[ReferenceImageAsset] = []

    selected.extend(character_reference_assets(bible, shot.characters, limit=min(len(shot.characters), plan.totalCount)))
    remaining_reuse = max(0, plan.reusePreviousSceneCount)
    if remaining_reuse:
        selected.extend(reusable_previous_assets(conn, prev_shot=prev_shot, limit=remaining_reuse, threshold=threshold))

    selected = _dedupe_assets(selected)[:max_refs]
    generated_needed = max(0, min(plan.totalCount, max_refs) - len(selected))
    generated_needed = min(generated_needed, plan.generateNewCount if plan.generateNewCount > 0 else generated_needed)
    type_cycle = [t for t in plan.types if t in REFERENCE_IMAGE_TYPES and t not in {"previous_shot_frame"}] or ["plot_key_frame"]
    # 逐图规格：优先用模型按剧本写好的 (type, prompt)；不足时用类型轮换 + 模板提示词补齐。
    model_specs = [p for p in (plan.prompts or []) if p.get("prompt")]
    specs: list[tuple[str, str | None]] = []
    for i in range(generated_needed):
        if i < len(model_specs):
            spec = model_specs[i]
            specs.append((spec.get("type") or type_cycle[i % len(type_cycle)], spec.get("prompt")))
        else:
            specs.append((type_cycle[i % len(type_cycle)], None))
    rejected: list[ReferenceImageAsset] = []
    for i, (ref_type, content_override) in enumerate(specs):
        try:
            asset = await _generate_one_reference(
                project_id=project_id, episode_no=episode_no, shot=shot, bible=bible,
                ref_type=ref_type, index=i + 1, content_override=content_override,
            )
            if asset.rejectReason:
                rejected.append(asset)
                if rejection_details is not None:
                    rejection_details.append({
                        "type": ref_type, "source": "seedream_generated",
                        "reason": asset.rejectReason,
                        "quality_score": asset.qualityScore,
                        "qa": asset.qa,
                    })
            else:
                selected.append(asset)
        except Exception as exc:
            reason = str(exc)[:240]
            rejected.append(ReferenceImageAsset(
                id=new_id("ref"), url="", type=ref_type, source="seedream_generated",
                rejectReason=reason,
            ))
            if rejection_details is not None:
                rejection_details.append({
                    "type": ref_type, "source": "seedream_generated",
                    "reason": reason,
                })
        if len(selected) >= max_refs or len(selected) >= plan.totalCount:
            break

    selected = _dedupe_assets(selected)[:min(plan.totalCount, max_refs)]
    for asset in selected:
        asset.selectedForSeedance = True
        asset.shotId = asset.shotId or shot_id
        asset.episodeId = asset.episodeId or episode_id
    # Rejected assets are not returned to Seedance, but callers may store them under reference_rejections.
    return selected


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
    note = " Use the provided reference images as follows: " + " ".join(lines)
    return prompt_text + note


def build_seedance_image_inputs(meta: dict[str, Any]) -> list[tuple[str, str]]:
    mode = meta.get("mode") or FIRST_LAST_FRAME_MODE
    if mode == REFERENCE_IMAGE_MODE:
        if meta.get("first_frame_path") or meta.get("last_frame_path"):
            raise ProviderError("REFERENCE_IMAGE_MODE must not pass first_frame or last_frame.")
        refs = meta.get("reference_images") or []
        if not refs:
            raise ProviderError("REFERENCE_IMAGE_MODE requires at least one quality-approved reference image.")
        if len(refs) > max_reference_images():
            raise ProviderError("REFERENCE_IMAGE_MODE reference image count exceeds configured limit.")
        out: list[tuple[str, str]] = []
        for ref in refs:
            if not ref.get("selectedForSeedance"):
                continue
            if ref.get("path"):
                out.append((hiagent.data_url_from_file(ref["path"]), "reference_image"))
            elif ref.get("url"):
                out.append((ref["url"], "reference_image"))
        if not out:
            raise ProviderError("REFERENCE_IMAGE_MODE has no selected reference images.")
        return out

    if meta.get("reference_images"):
        raise ProviderError("FIRST_LAST_FRAME_MODE must not pass reference images.")
    first_path = meta.get("first_frame_path")
    last_path = meta.get("last_frame_path")
    if not first_path:
        raise ProviderError("Video first frame is missing; regenerate keyframes before video generation.")
    out = [(hiagent.data_url_from_file(first_path), "first_frame")]
    if last_path:
        out.append((hiagent.data_url_from_file(last_path), "last_frame"))
    return out


def is_character_consistency_failure(qa: dict[str, Any]) -> bool:
    issues = " ".join(str(x) for x in qa.get("issues") or []).lower()
    if any(w in issues for w in ["character", "costume", "hair", "face", "角色", "服装", "发型", "五官", "一致"]):
        return True
    try:
        return float(qa.get("character_match", 1)) < quality_threshold()
    except (TypeError, ValueError):
        return False
