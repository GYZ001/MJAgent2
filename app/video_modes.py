from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from app import config, hiagent
from app.db import get_conn, get_setting, new_id
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


def quality_floor() -> float:
    """兜底图质量地板：生成图全不达标时，最佳一版仍低于此分则不喂模型——此时定妆照/场景锚点已能锁身份与环境，
    一张带水印/畸形的脏图当参考反而拖累成片。介于地板与阈值之间才作兜底喂入。"""
    return float_setting("video_reference_quality_floor", 0.4)


def min_generated_references() -> int:
    """参考图模式下每镜至少新生成几张关键帧参考图（防止只剩定妆照）。"""
    return max(0, int_setting("video_reference_min_generated", 1))


def reference_gen_retries() -> int:
    """单张参考图 QA 不达标时的额外重试次数。"""
    return max(0, int_setting("video_reference_gen_retries", 2))


def reference_prompt_async() -> bool:
    """是否为每张新参考图用独立 LLM 调用并发生成提示词（防止一次性写多张时偷懒）。"""
    return bool_setting("video_reference_prompt_async", True)


def consistency_check_enabled() -> bool:
    """Phase 2：是否对整组参考图做相对一致性检查（点名漂移图并 i2i 重生/剔除）。"""
    return bool_setting("video_reference_consistency_check", True)


def consistency_threshold() -> float:
    """候选参考图与锚点（定妆照/上镜尾帧）的一致性达标线，低于此判为漂移。"""
    return float_setting("video_reference_consistency_threshold", 0.7)


def consistency_retries() -> int:
    """漂移图从锚点 i2i 重生的最大次数；仍漂移则从喂给 Seedance 的集合里剔除。"""
    return max(0, int_setting("video_reference_consistency_retries", 1))


def max_character_reference_images() -> int:
    """喂给视频模型时，「含同一角色」的参考图最多几张。
    根因防穿模/分身：多张不同尺度的含人物图（尤其纯背景全身定妆照与入戏关键帧并存）是 Seedance
    把同一角色画两遍/生成前景巨人的结构性触发器。默认 1（场景/环境图不受此限）。"""
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
    async def select(self, shot: Shot, bible: Bible, *, shot_row: Any | None = None,
                     prev_shot: Any | None = None) -> ShotVideoModeDecision:
        """视频生成已固定为参考图模式；不再调用 LLM 做模式选择。"""
        return default_reference_decision()


def default_reference_decision() -> ShotVideoModeDecision:
    plan = ReferenceImagePlan()
    return ShotVideoModeDecision(
        mode=REFERENCE_IMAGE_MODE,
        reason="已固定使用参考图模式生成视频。",
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
    # 只有通过 VLM/场景 QA 的干净帧才会被复用（见下方 _scene_qa_ok），不再用关键词预筛。
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


def character_reference_assets(bible: Bible, character_names: list[str], *, limit: int,
                               project_id: str | None = None,
                               episode_no: int | None = None) -> list[ReferenceImageAsset]:
    assets: list[ReferenceImageAsset] = []
    by_name = {c.name: c for c in bible.characters}
    for name in character_names:
        if len(assets) >= limit:
            break
        c = by_name.get(name)
        # 按集号选用人物谱分段定妆照（覆盖该集的版本），未命中回退到初始 ref_image_path。
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
                quality_score=1.0,
                qa={"overall": 1.0, "issues": []},
            ))
        except OSError:
            continue
    return assets


def scene_reference_assets(bible: Bible, scene_name: str, *, project_id: str | None = None,
                           episode_no: int | None = None) -> list[ReferenceImageAsset]:
    """该镜场景的场景库图 →[ReferenceImageAsset]（ref_type="scene"，环境真值锚点）。
    同一规范场景的所有镜头、所有集都取同一张图（按集分段），保证场景跨镜/跨集一致。"""
    if not scene_name:
        return []
    from app.scenes import scene_ref_for_episode, scene_ref_qa_for_episode
    path = scene_ref_for_episode(project_id, scene_name, episode_no) if project_id else None
    if not path:
        by_name = {s.name: s for s in (getattr(bible, "scenes", None) or [])}
        sc = by_name.get(scene_name)
        path = getattr(sc, "ref_image_path", None) if sc else None
    if not path or not Path(path).exists():
        return []
    qa = scene_ref_qa_for_episode(project_id, scene_name, episode_no) if project_id else None
    score = float(qa.get("overall", 1.0)) if isinstance(qa, dict) and qa.get("overall") is not None else 1.0
    try:
        return [_asset_from_path(path=path, ref_type="scene", source="asset_library",
                                 quality_score=score, qa=qa or {"overall": score, "issues": []})]
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
    VLM 异常或 JSON 解析失败时保守返回「全部达标」，避免误删好图。
    返回 {"candidates": [{"asset_id", "consistency", "drift": [...], "issues": [...]}], "overall"}。"""
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
        return {"candidates": [], "overall": 1.0}

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
    except Exception:  # noqa: BLE001 VLM/解析失败保守放行，不误删
        return {"candidates": [{"asset_id": c.id, "consistency": 1.0, "drift": [], "issues": []}
                               for c, _ in cand_pairs], "overall": 1.0}

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
    for c, _ in cand_pairs:  # 模型漏报的候选默认达标，不误删
        if c.id not in covered:
            out.append({"asset_id": c.id, "consistency": 1.0, "drift": [], "issues": []})
    try:
        overall = max(0.0, min(1.0, float(data.get("overall"))))
    except (TypeError, ValueError):
        overall = round(sum(o["consistency"] for o in out) / len(out), 3) if out else 1.0
    return {"candidates": out, "overall": overall}


async def _generate_one_reference(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                  ref_type: str, index: int, content_override: str | None = None,
                                  seed_inputs: list[str] | None = None,
                                  extra_instruction: str | None = None) -> ReferenceImageAsset:
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
    """取上一镜「尾帧」用作参考图（接上镜的连续性锚点）：
    优先上一镜已过审尾关键帧；没有则从上一镜采用成片里抽最后一帧。"""
    if prev_shot is None:
        return None

    def _g(key: str) -> Any:
        if hasattr(prev_shot, "keys"):
            return prev_shot[key] if key in prev_shot.keys() else None
        return prev_shot.get(key)

    prev_id = _g("id")
    tail_id = _g("approved_tail_scene_id")
    if tail_id:
        row = conn.execute(
            "SELECT image_path FROM shot_scenes WHERE id=? AND status='succeeded'", (tail_id,)).fetchone()
        if row and row["image_path"] and Path(row["image_path"]).exists():
            return _asset_from_path(
                path=row["image_path"], ref_type="previous_shot_frame", source="previous_shot",
                shot_id=prev_id, quality_score=1.0, qa={"overall": 1.0, "issues": ["forced_continuity"]})
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
        raw = await hiagent.chat([
            {"role": "system", "content": "Return exactly one JSON object with a single 'prompt' string field. English only."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], temperature=0.3, max_tokens=500,
            call_meta={"initiator_label": "参考图提示词生成", "reference_type": ref_type, "shot_no": shot.shot_no})
        data = extract_json(raw)
        return str(data.get("prompt") or "").strip()[:600]
    except Exception:
        return ""


async def _generate_reference_keep_best(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                        ref_type: str, index: int, content_override: str | None,
                                        retries: int, seed_inputs: list[str] | None = None) -> tuple[ReferenceImageAsset | None, list[ReferenceImageAsset], list[dict[str, Any]]]:
    """生成单张参考图，QA 不达标则重试；最终返回过审资产，或（全部不达标时）保留分数最高的一版兜底，
    保证镜头至少有图，而不是因为 QA 卡掉直接没有参考图。但兜底图有质量地板（quality_floor）：
    最佳一版仍低于地板则一律不喂（返回 None，全部进废弃画廊），改由定妆照/场景锚点撑住——脏图当参考反而拖累成片。
    返回 (chosen_or_none, discarded_assets, rejection_details)：discarded_assets 是质检未通过、
    未被选用的参考图（带各自图片文件），供评审墙「废弃照片画廊」展示。"""
    rejections: list[dict[str, Any]] = []
    attempts: list[ReferenceImageAsset] = []  # 全部质检未通过的资产（含图片），用于废弃画廊
    best: ReferenceImageAsset | None = None
    for attempt in range(retries + 1):
        # 每次尝试写到不同文件名，避免后一次（更差）覆盖前一次的最佳图，导致 best.path 指向劣质图。
        attempt_index = index * 100 + attempt
        try:
            asset = await _generate_one_reference(
                project_id=project_id, episode_no=episode_no, shot=shot, bible=bible,
                ref_type=ref_type, index=attempt_index, content_override=content_override,
                seed_inputs=seed_inputs)
        except Exception as exc:
            rejections.append({"type": ref_type, "source": "seedream_generated",
                               "reason": "参考图生成异常" + code_ref(
                                   exc, action="generate_reference_image",
                                   context={"project_id": project_id, "episode_no": episode_no,
                                            "shot_id": getattr(shot, "id", None), "ref_type": ref_type})})
            continue
        if not asset.rejectReason:
            # 通过 QA：选它；本次之前生成的不达标图作为废弃图一并返回。
            return asset, attempts, rejections
        rejections.append({"type": ref_type, "source": "seedream_generated",
                           "reason": asset.rejectReason, "quality_score": asset.qualityScore, "qa": asset.qa})
        attempts.append(asset)
        if best is None or (asset.qualityScore or 0) > (best.qualityScore or 0):
            best = asset
    # 全部不达标：最佳一版若仍低于质量地板，直接不喂（全部进废弃画廊），只靠定妆照/场景锚点。
    if best is not None and (best.qualityScore or 0) < quality_floor():
        return None, list(attempts), rejections
    # 否则保留分数最高的一版兜底，其余进废弃画廊。
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
        if info:
            a.qa = {**(a.qa or {}), "consistency": info["consistency"], "drift": info["drift"]}


def _record_consistency_rejection(rejection_details: list[dict[str, Any]] | None,
                                  rejected_out: list[ReferenceImageAsset] | None,
                                  asset: ReferenceImageAsset, *, reason: str,
                                  drift: list[str] | None = None, consistency: float | None = None) -> None:
    asset.selectedForSeedance = False
    asset.rejectReason = reason
    if rejected_out is not None and asset not in rejected_out:
        rejected_out.append(asset)
    if rejection_details is not None:
        rejection_details.append({"type": asset.type, "source": asset.source, "reason": reason,
                                  "drift": drift or [], "consistency": consistency})


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
    """Phase 2 主流程：以锚点（定妆照/上镜尾帧/复用历史帧）为真值，检查生成的候选参考图是否风格/服饰漂移；
    漂移的从锚点 i2i 重生，仍漂移则从喂给 Seedance 的集合里剔除（进废弃画廊）。无锚点时跳过（避免误删）。"""
    if not consistency_check_enabled():
        return selected
    candidates = [a for a in selected if a.source == "seedream_generated"]
    anchors = [a for a in selected if a.source in {"asset_library", "previous_shot"}]
    if not candidates or not anchors:
        return selected
    seeds = _dedupe_str([a.url for a in anchors if a.url])
    threshold = consistency_threshold()

    current = list(candidates)
    scores = _consistency_scores(await review_reference_consistency(
        candidates=current, anchors=anchors, shot=shot, bible=bible))
    _annotate_consistency(current, scores)

    for attempt in range(consistency_retries()):
        drifted = [c for c in current if scores.get(c.id, {}).get("consistency", 1.0) < threshold]
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
            _record_consistency_rejection(rejection_details, rejected_out, cand,
                                          reason="consistency_drift", drift=drift,
                                          consistency=scores.get(cand.id, {}).get("consistency"))
            current = [new_asset if c is cand else c for c in current]
            changed = True
        if not changed:
            break
        scores = _consistency_scores(await review_reference_consistency(
            candidates=current, anchors=anchors, shot=shot, bible=bible))
        _annotate_consistency(current, scores)

    kept: list[ReferenceImageAsset] = []
    for c in current:
        cs = scores.get(c.id, {}).get("consistency", 1.0)
        if cs < threshold:
            _record_consistency_rejection(rejection_details, rejected_out, c,
                                          reason="consistency_drift_unfixable",
                                          drift=scores.get(c.id, {}).get("drift"), consistency=cs)
            continue
        kept.append(c)

    rebuilt = [a for a in selected if a.source != "seedream_generated"] + kept
    if not rebuilt:  # 兜底：极端情况全被剔 → 留一致性最高的一版，保证每镜仍有参考图
        best = max(current, key=lambda c: scores.get(c.id, {}).get("consistency", 0.0))
        if best in (rejected_out or []):
            rejected_out.remove(best)
        best.selectedForSeedance = True
        best.rejectReason = None
        rebuilt = [best]
    return rebuilt


async def build_reference_assets(*, conn: Any, project_id: str, episode_no: int, episode_id: str,
                                 shot_id: str, shot: Shot, bible: Bible,
                                 decision: ShotVideoModeDecision, prev_shot: Any | None = None,
                                 rejection_details: list[dict[str, Any]] | None = None,
                                 rejected_out: list[ReferenceImageAsset] | None = None) -> list[ReferenceImageAsset]:
    plan = decision.referenceImagePlan
    threshold = quality_threshold()
    max_refs = max_reference_images()

    # 接上镜（continuity_from_prev）：强制把上一镜尾帧作为参考图注入，作为剪辑点连贯锚点。
    # 不受 plan.reusePreviousSceneCount 计数与 QA 阈值限制；放在最前、确保不被裁掉。
    forced: list[ReferenceImageAsset] = []
    if shot.continuity_from_prev:
        prev = prev_shot
        if prev is None and int(getattr(shot, "shot_no", 0) or 0) > 1:
            prev = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
                (episode_id, int(shot.shot_no) - 1)).fetchone()
        if prev is not None:
            ref_dir = reference_image_path(project_id, episode_no, shot.shot_no, "previous_shot_frame", 0).parent
            tail = previous_tail_reference_asset(conn, prev, dest_dir=ref_dir)
            if tail:
                forced.append(tail)

    # 期望新生成的关键帧参考图数量：模型计划值与「每镜最少生成数」取大者（仅参考图模式），保证每镜都有生成图。
    min_gen = min_generated_references() if decision.mode == REFERENCE_IMAGE_MODE else 0
    want_gen = max(int(plan.generateNewCount or 0), min_gen)
    # 先给强制连贯帧 + 生成位预留名额，剩余名额才给定妆照/复用帧，避免它们把生成位挤掉（否则只剩定妆照）。
    reserve_for_gen = min(want_gen, max(0, max_refs - len(forced)))
    non_gen_budget = max(0, max_refs - len(forced) - reserve_for_gen)

    selected: list[ReferenceImageAsset] = list(forced)
    # 场景库图（环境锚点）：同一规范场景跨镜/跨集复用同一张图，与定妆照同档优先注入。
    scene_assets = scene_reference_assets(bible, getattr(shot, "scene_name", "") or "",
                                          project_id=project_id, episode_no=episode_no)
    selected.extend(scene_assets[:max(0, non_gen_budget)])
    char_budget = max(0, non_gen_budget - len(scene_assets[:non_gen_budget]))
    selected.extend(character_reference_assets(bible, shot.characters, limit=min(len(shot.characters), char_budget),
                                               project_id=project_id, episode_no=episode_no))
    selected = _dedupe_assets(selected)
    remaining_reuse = max(0, plan.reusePreviousSceneCount)
    room_for_reuse = max(0, max_refs - len(selected) - reserve_for_gen)
    if remaining_reuse and room_for_reuse:
        selected.extend(reusable_previous_assets(
            conn, prev_shot=prev_shot, limit=min(remaining_reuse, room_for_reuse), threshold=threshold))

    selected = _dedupe_assets(selected)[:max_refs]
    room = max(0, max_refs - len(selected))
    generated_needed = min(want_gen, room)

    type_cycle = [t for t in plan.types if t in REFERENCE_IMAGE_TYPES and t not in {"previous_shot_frame"}] or ["plot_key_frame"]
    # 逐图规格：优先用模型按剧本写好的 (type, prompt)；不足时用类型轮换补齐（prompt 留空，下面逐图异步补写）。
    model_specs = [p for p in (plan.prompts or []) if p.get("prompt")]
    specs: list[tuple[str, str | None]] = []
    for i in range(generated_needed):
        if i < len(model_specs):
            spec = model_specs[i]
            specs.append((spec.get("type") or type_cycle[i % len(type_cycle)], spec.get("prompt")))
        else:
            specs.append((type_cycle[i % len(type_cycle)], None))

    # 逐图异步写提示词：每张图各起一次独立 LLM 调用并发生成详尽提示词（防止一次性写多张时偷懒）。
    # 模型计划里若已带该图的简述，作为 intent 喂进去引导，但仍逐图单独成稿；调用失败才回退到原简述。
    if specs and reference_prompt_async():
        async def _resolve(ref_type: str, brief: str | None) -> str | None:
            written = await write_reference_prompt(shot, bible, ref_type, intent=brief)
            return written or brief or None
        resolved = await asyncio.gather(*[_resolve(t, o) for t, o in specs])
        specs = [(specs[i][0], resolved[i]) for i in range(len(specs))]

    # i2i 种子（根因修复）：新生成的参考图不再裸跑文生图，而是以稳定锚点做图生图，姿态/动作仍走文字。
    #   - 定妆照：锁长相/发型/服饰，喂给「含人物」的图（character / plot_key_frame）。
    #   - 上一镜尾帧（continuity 镜的 forced 帧）：锁环境/光线/构图衔接，喂给本镜所有新图。
    # 二者同源于实际喂给 Seedance 的锚点，保证「同分镜多张一致」「与上一镜衔接」。
    portrait_seeds = _portrait_seed_inputs(bible, shot.characters, project_id=project_id, episode_no=episode_no)
    # 环境种子：上一镜尾帧（衔接）+ 本镜场景库图（锁定环境/陈设/光线），喂给本镜所有新图。
    env_seeds = [a.url for a in forced if a.type == "previous_shot_frame" and a.url]
    env_seeds += [a.url for a in scene_assets if a.url]

    def _seeds_for(ref_type: str) -> list[str]:
        seeds = (portrait_seeds + env_seeds) if ref_type in {"character", "plot_key_frame"} else list(env_seeds)
        return _dedupe_str(seeds)

    # 并发生成所有参考图（每张带 QA 重试 + 全部不达标时保留最佳一版兜底）。
    if specs:
        results = await asyncio.gather(*[
            _generate_reference_keep_best(
                project_id=project_id, episode_no=episode_no, shot=shot, bible=bible,
                ref_type=t, index=i + 1, content_override=o, retries=reference_gen_retries(),
                seed_inputs=_seeds_for(t))
            for i, (t, o) in enumerate(specs)
        ])
        for asset, discarded, rej in results:
            if rejection_details is not None:
                rejection_details.extend(rej)
            if asset is not None:
                selected.append(asset)
            if rejected_out is not None:
                rejected_out.extend(discarded)

    # Phase 2：整组相对一致性检查——点名漂移的生成图，从锚点 i2i 重生；仍漂移则剔除（不喂 Seedance，进废弃画廊）。
    selected = await _enforce_reference_consistency(
        selected=selected, shot=shot, bible=bible, project_id=project_id, episode_no=episode_no,
        rejection_details=rejection_details, rejected_out=rejected_out)

    selected = _dedupe_assets(selected)[:max_refs]
    # 根因防穿模/分身：限制「含同一角色」的参考图数量。多张不同尺度的含人物图（尤其纯背景全身定妆照
    # 与入戏关键帧并存）是 Seedance 把同一角色画两遍/生成前景巨人的结构性触发器。被压下的图（多为裸
    # 定妆照）仍作为 i2i 种子参与了参考图生成，身份不丢；它们进废弃画廊、不喂模型，用户可手动启用。
    selected, suppressed = _limit_character_references(selected, limit=max_character_reference_images())
    if rejected_out is not None:
        rejected_out.extend(suppressed)
    for asset in selected:
        asset.selectedForSeedance = True
        asset.shotId = asset.shotId or shot_id
        asset.episodeId = asset.episodeId or episode_id
    # 质检未通过、未被选用的参考图（rejected_out）不喂给 Seedance，仅供评审墙废弃画廊展示。
    if rejected_out is not None:
        for asset in rejected_out:
            asset.selectedForSeedance = False
            asset.shotId = asset.shotId or shot_id
            asset.episodeId = asset.episodeId or episode_id
    return selected


def _is_character_bearing(asset: ReferenceImageAsset) -> bool:
    """该参考图里是否含人物（会参与构图、可能被模型当成额外主体复制）。纯场景/环境图返回 False。"""
    return asset.type in {"character", "plot_key_frame", "previous_shot_frame"} or bool(asset.relatedCharacterIds)


def _limit_character_references(selected: list[ReferenceImageAsset], *, limit: int
                                ) -> tuple[list[ReferenceImageAsset], list[ReferenceImageAsset]]:
    """限制喂给 Seedance 的「含人物参考图」数量，避免同一角色被画两遍/前景巨人/穿模。
    优先级：上一镜尾帧（连贯锚点）> 过审的入戏生成关键帧 > 干净裸定妆照 > 兜底（低于阈值）的生成关键帧。
    干净生成关键帧仍排在定妆照前（定妆照直接喂视频最易变前景巨人）；但当生成图本身只是兜底废图时，
    宁可让位给干净定妆照——脏兜底图压过满分定妆照得不偿失。超出名额的移出喂模型集合（仍作为 i2i 种子，身份不丢），
    进废弃画廊供用户手动启用。返回 (kept, suppressed)。"""
    char_refs = [a for a in selected if _is_character_bearing(a)]
    if len(char_refs) <= limit:
        return selected, []

    def _priority(a: ReferenceImageAsset) -> int:
        if a.type == "previous_shot_frame":
            return 0   # 连贯尾帧优先保留
        if a.source == "seedream_generated":
            # 过审的入戏关键帧最优；兜底（带 rejectReason、低于阈值）的生成图让位给干净定妆照
            return 1 if not a.rejectReason else 3
        return 2       # 干净裸定妆照（直接喂视频较易变前景巨人，故排在过审生成图后）

    keep_ids = {id(a) for a in sorted(char_refs, key=_priority)[:limit]}
    kept: list[ReferenceImageAsset] = []
    suppressed: list[ReferenceImageAsset] = []
    for a in selected:
        if _is_character_bearing(a) and id(a) not in keep_ids:
            a.selectedForSeedance = False
            a.rejectReason = "duplicate_character_suppressed"
            suppressed.append(a)
        else:
            kept.append(a)
    return kept, suppressed


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
        # 只取「选中且未被废弃」的参考图喂给模型：质检未通过或用户在素材画廊里删除的图都排除在外。
        usable = [r for r in refs if r.get("selectedForSeedance") and not r.get("deleted")]
        if len(usable) > max_reference_images():
            raise ProviderError("REFERENCE_IMAGE_MODE reference image count exceeds configured limit.")
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


def is_character_consistency_failure(qa: dict[str, Any]) -> bool:
    issues = " ".join(str(x) for x in qa.get("issues") or []).lower()
    if any(w in issues for w in ["character", "costume", "hair", "face", "角色", "服装", "发型", "五官", "一致"]):
        return True
    try:
        return float(qa.get("character_match", 1)) < quality_threshold()
    except (TypeError, ValueError):
        return False
