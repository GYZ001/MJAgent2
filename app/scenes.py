"""场景图素材库工作流（跨集场景一致性的核心，与 app.refs/app.portraits 同构）。

场景圣经定稿后为每个规范场景生成 Seedream 定场图，存入 projects/<id>/scene_refs/，并登记到
scene_references（按"适用集区间"分段，ep_end=NULL 表示开区间=当前最新版）。生成镜头/关键帧时，
按 shot.scene_name 取覆盖该集的场景图，作为 scene 型参考图注入——同一场景的所有镜头、所有集
都吃同一张场景图 → 整片场景一致。

两条产生路径（完全复刻 app.portraits 的角色定妆照机制）：
  ① 初始批量：generate_scene_refs（场景圣经定稿后，适用集 1~ 至今）。
  ② 分镜阶段反应式发现：ensure_scenes_for_storyboard——剧本里出现、场景库里没有、够戏份的
     新场景 → 评估后补进 bible.scenes，适用集从首次出场那集起开放。出图不在这里
     内联完成，挪到映射台发布后统一触发的后台任务（见
     app/domain/screenplay_ops/background_portraits.py）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app import config, generation_concurrency, hiagent, quota
from app.atomic_io import atomic_write_bytes
from app.errors import ContentGenerationError, code_ref
from app.db import get_conn, new_id, now
from app.evidence import repository as evidence_repository
from app.evidence.media import record_reference_asset
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.refs import _safe_name, scene_visual_style_lock
from app.scene_contract import split_legacy_scene_setting
from app.schemas import Bible, Scene, extract_json
from app.validators import match_scene_name

SCENE_CANONICAL_MIN = 30
SCENE_CANONICAL_MAX = 80

# 初始场景图只覆盖前 N 章的场景（按钮批量出图的范围）；更靠后才出现的新场景留到分镜阶段反应式补图。
SCENE_BIBLE_CHAPTER_WINDOW = 20

# 「检查并补齐」时：某场景候选图数量超过该阈值，自动采纳最高分候选，不再继续出图。
SCENE_CANDIDATE_AUTO_ADOPT_THRESHOLD = 4

_reactive_scene_locks: dict[tuple[str, str], asyncio.Lock] = {}
_reactive_scene_locks_guard = asyncio.Lock()
_reactive_bible_locks: dict[str, asyncio.Lock] = {}
_reactive_bible_locks_guard = asyncio.Lock()


async def _reactive_scene_lock(project_id: str, name: str) -> asyncio.Lock:
    async with _reactive_scene_locks_guard:
        key = (project_id, name)
        lock = _reactive_scene_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _reactive_scene_locks[key] = lock
        return lock


def _exact_known_scene_name(value: str, scenes: list[Scene]) -> str | None:
    """Match canonical discovery names without collapsing interior/exterior."""

    def identity_key(label: str) -> str:
        _time, location = split_legacy_scene_setting(label)
        return re.sub(r"[\s，,。.：:；;/、|]+", "", location.strip())

    target = identity_key(value)
    if not target:
        return None
    for scene in scenes:
        labels = [scene.name, *(scene.aliases or [])]
        if any(identity_key(str(label or "")) == target for label in labels):
            return scene.name
    return None


async def _reactive_bible_lock(project_id: str) -> asyncio.Lock:
    async with _reactive_bible_locks_guard:
        lock = _reactive_bible_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            _reactive_bible_locks[project_id] = lock
        return lock


class SceneCandidateReviewRequired(Exception):
    """图片候选已经落盘，但当前没有可自动采纳的新版硬门禁证据。

    这不是生图或供应商失败，上层应将任务收口为 PARTIAL/待人工处理，
    不得再包装成“大模型/外部服务调用失败”。
    """


class SceneAssetQualityError(ContentGenerationError):
    """A scene asset exists or was evaluated, but its typed QA contract failed."""


def _scene_failures_are_quality_only(failures: list[Exception]) -> bool:
    """Classify by exception type; error prose never participates in routing."""
    return bool(failures) and all(
        isinstance(exc, (SceneCandidateReviewRequired, SceneAssetQualityError))
        for exc in failures
    )


# ---------- 落盘 / 提示词 ----------

def normalize_scene_prompt(*segments: str) -> str:
    """规范标点并仅移除完全重复片段，保留语义性强调。

    原属 app.scene_policy（已随 VLM 图片质检整体下线）；这是纯文本提示词拼接工具，
    与质检无关，随其唯一调用方 app.scenes 一起保留。
    """
    seen: set[str] = set()
    parts: list[str] = []
    for segment in segments:
        for raw in re.split(r"[。；;\n]+", str(segment or "")):
            part = re.sub(r"[，,]{2,}", "，", raw.strip(" ，,。；;"))
            key = re.sub(r"\s+", "", part).lower()
            if not part or key in seen:
                continue
            seen.add(key)
            parts.append(part)
    return "。".join(parts) + ("。" if parts else "")


def _scene_dir(project_id: str) -> Path:
    d = config.PROJECTS_DIR / project_id / "scene_refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scene_ref_path(project_id: str, scene_name: str, ep_start: int | None = None) -> str:
    suffix = f"__ep{ep_start}" if ep_start else ""
    return str(_scene_dir(project_id) / f"{_safe_name(scene_name)}{suffix}.jpg")


def environment_only_scene_canonical(scene_canonical: str) -> str:
    """Return the approved scene contract without vocabulary-based rewriting."""
    return str(scene_canonical or "").strip()


def scene_generation_canonical(scene_name: str, scene_canonical: str) -> str:
    """Use the approved canonical as generation authority, independent of its name."""
    _ = scene_name
    return environment_only_scene_canonical(scene_canonical)


def scene_name_visual_constraints(scene_name: str, scene_canonical: str = "") -> str:
    """Legacy entrypoint kept empty; visual semantics belong to the scene contract."""
    _ = (scene_name, scene_canonical)
    return ""


def scene_hard_gate_retry_prompt(
    base_prompt: str,
    qa: dict | None,
    *,
    scene_name: str = "",
    scene_canonical: str = "",
    visual_style: str = "",
) -> str | None:
    """Build one bounded corrective retry only for deterministic hard failures."""
    result = dict(qa or {})
    hard = [str(item).strip() for item in (result.get("hard_failures") or []) if str(item).strip()]
    if result.get("status") != "failed" and not hard:
        return None
    issues = [str(item).strip() for item in (result.get("issues") or []) if str(item).strip()]
    details = list(dict.fromkeys([*hard, *issues]))[:6]
    if not details:
        return None
    if scene_name or scene_canonical or visual_style:
        issue_text = "；".join(details)
        return normalize_scene_prompt(
            "编辑输入候选图：依据结构化 QA 事实修复候选图，不得原样复制失败区域",
            f"唯一地点：{scene_name or '原场景'}",
            f"目标环境：{scene_generation_canonical(scene_name, scene_canonical)}",
            f"必须保持画风：{visual_style}" if visual_style else "",
            "必须修复的观察事实：" + issue_text[:700],
            "纯环境、无人、无剧情文字；供应商固定角落标识无需重绘，禁止新增其他文字或 Logo",
        )
    return normalize_scene_prompt(
        base_prompt,
        "上一候选未通过确定性场景硬门禁，本次仅允许修复以下明确问题："
        + "；".join(details)[:900],
        "必须逐条纠正上述问题，同时完整保留原地点、空间结构、材质状态、画风、纯环境和无文字要求",
    )


def scene_ref_prompt(
    visual_style: str,
    scene_canonical: str,
    *,
    scene_name: str = "",
) -> str:
    """场景定场图生成词：纯环境、无人物，作为跨集复用的场景锚点。"""
    location_identity = (
        f"规范地点名称：{scene_name.strip()}。"
        "地点名是独立且最高优先级的场景语义输入：必须逐项识别名称中的建筑功能、"
        "空间类型和状态限定词，并把每一项都转化为无需文字即可识别的可见环境证据。"
        "即使后续场景描述没有重复某个名称限定词，也不得忽略或替换它；"
        "必须呈现与该地点名称一致的建筑功能、陈设、材质状态和空间类型，不得替换成其他地点。"
        if scene_name.strip()
        else ""
    )
    style_constraint = scene_visual_style_lock(visual_style) if visual_style.strip() else ""
    generation_canonical = scene_generation_canonical(scene_name, scene_canonical)
    functional_constraints = scene_name_visual_constraints(scene_name, generation_canonical)
    return normalize_scene_prompt(
        style_constraint,
        f"场景定场图（纯环境、画面中不出现任何人物）："
        f"{location_identity}{generation_canonical}",
        functional_constraints,
        "9:16 竖屏，构图完整的环境定场镜头，空间纵深清晰，光影与色调统一，电影质感，高清",
        "画面必须无人物；不得生成任何文字、字幕、招牌字、角标、水印或 logo",
        f"再次确认：地点是「{scene_name.strip()}」，画风是「{visual_style.strip()}」"
        if scene_name.strip() and visual_style.strip()
        else "",
    )


async def _provider_visual_scene_retry_prompt(
    visual_style: str,
    scene_canonical: str,
) -> str:
    """Re-express an approved scene contract after a technical image failure.

    This is a bounded representation fallback, not a vocabulary filter: the
    text model receives the complete approved contract and must preserve every
    visible fact while replacing proper-name and narrative phrasing with an
    equivalent visual description.  The generated image is still evaluated
    against the original scene contract.
    """
    raw = await model_gateway.chat(
        [{
            "role": "user",
            "content": f"""任务：把已批准的场景合同改写成纯视觉环境生图描述。

已批准场景合同：
{scene_canonical}

要求：
- 保留原合同中全部可见的空间类型、建筑功能、时代、时段、光线、材质、陈设、状态与氛围事实，不得增删或改换地点。
- 只改写表达方式；用可见建筑与陈设表达地点功能，不输出地点专名、组织名、人物、剧情事件、台词或政策说明。
- 只描述纯环境，不出现人物，不生成可读文字。

只输出 JSON：{{"visual_environment": "同一场景的完整纯视觉描述"}}""",
        }],
        temperature=0.1,
        max_tokens=600,
        call_meta={
            "stage": "scene_provider_visual_retry",
            "asset_kind": "scene_reference",
        },
    )
    payload = extract_json(raw)
    visual_environment = str(
        payload.get("visual_environment") or ""
    ).strip()
    if len(visual_environment) < SCENE_CANONICAL_MIN:
        raise hiagent.ProviderError("场景视觉改写结果过短，无法用于技术重试")
    return normalize_scene_prompt(
        scene_visual_style_lock(visual_style) if visual_style.strip() else "",
        "场景定场图（纯环境、画面中不出现任何人物）："
        + visual_environment,
        "9:16 竖屏，构图完整的环境定场镜头，空间纵深清晰，光影与色调统一，电影质感，高清",
        "画面必须无人物；不得生成任何文字、字幕、招牌字、角标、水印或 logo",
    )


def _restore_approved_scene_bible(conn, project_id: str, bible_data: dict) -> bool:
    """Restore scenes lost by an older concurrent full-Bible write.

    The immutable approved scene-bible artifact is the recovery source.  Current
    entries win by name, preserving manual edits and reactively added scenes.
    """
    try:
        row = conn.execute(
            """SELECT content_json FROM artifacts
               WHERE type='scene_bible' AND scope_type='project' AND scope_id=?
                 AND status='approved'
               ORDER BY version DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001 legacy/minimal test schemas may not have artifacts
        return False
    if not row or not row["content_json"]:
        return False
    try:
        approved = json.loads(row["content_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    current = bible_data.setdefault("scenes", [])
    known = {item.get("name") for item in current}
    missing = [item for item in approved.get("scenes", []) if item.get("name") not in known]
    if not missing:
        return False
    current.extend(missing)
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps(bible_data, ensure_ascii=False), project_id),
    )
    conn.commit()
    return True


def _merge_generated_scene_refs(conn, project_id: str, generated_scenes) -> None:
    """Merge accepted scene paths without overwriting concurrent Bible changes."""
    accepted = {
        item.name: item.ref_image_path for item in generated_scenes if item.ref_image_path
    }
    if not accepted:
        return
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return
    latest = json.loads(row["bible_json"])
    for item in latest.get("scenes", []):
        if item.get("name") in accepted:
            item["ref_image_path"] = accepted[item["name"]]
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps(latest, ensure_ascii=False), project_id),
    )


async def _save_image_item(item: dict, dest: str) -> None:
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


def same_scene_anchor(conn, project_id: str, name: str) -> str | None:
    """该场景【自己】已落盘的最新一张图路径，作为同名场景跨集/重出时的 i2i 锚点；无则 None。

    只在同名场景内部参考——绝不跨场景。Seedream 的参考图是【全图 i2i】，会把锚点的构图与陈设整体带过来：
    同一地点（如某场景跨集演化、或重出微调）拿自己的旧图当锚点能保持一致；但拿【别的场景】的图当锚点
    会把那个场景的石碑/围栏等带进来导致撞图，是错的。"""
    rows = conn.execute(
        "SELECT image_path FROM scene_references WHERE project_id=? AND scene_name=? "
        "ORDER BY ep_start DESC, id DESC", (project_id, name)).fetchall()
    for r in rows:
        if r["image_path"] and Path(r["image_path"]).exists():
            return r["image_path"]
    return None


async def _generate_scene_image(prompt: str, anchor_url: str | None = None, *,
                                call_meta: dict | None = None) -> dict:
    """出一张场景图。anchor_url 仅用于【同场景】的 i2i 锚点（由 same_scene_anchor 取该场景自己的旧图），
    绝不传别的场景的图。带参考图失败则回退纯文生图（与 generate_image 文档约定一致）。"""
    if anchor_url:
        try:
            return await hiagent.generate_image(
                prompt, size=config.REF_IMAGE_SIZE, image_inputs=[anchor_url], call_meta=call_meta)
        except Exception:  # noqa: BLE001 带参考图失败 → 不带重试
            pass
    return await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE, call_meta=call_meta)


async def _review_scene_ref(
    image_path: str,
    scene: "Scene | dict",
    *,
    expected_description: str | None = None,
) -> dict:
    """VLM 图片质检已下线：场景图是否可用只看文件是否存在（技术校验），产品决定人自己看。

    保留空字典返回值和函数签名，使全部既有调用方（生图流程/候选复验）无需改动即可继续运行；
    调用方把 ``{}`` 传给 ``record_reference_asset(qa=...)`` 时 ``qa`` 为假值，不会再产生任何
    model_evaluation 记录。
    """
    del image_path, scene, expected_description
    return {}


# ---------- scene_references 分段表读写（对照 app.portraits） ----------

def register_initial_scene_ref(conn, project_id: str, name: str, image_path: str,
                               scene_canonical: str, prompt: str, qa: dict, bible_version: int,
                               artifact_id: str | None = None) -> str:
    """初次出图后登记场景图（适用集 1~ 至今）。覆盖式：先清掉该场景全部旧分段。"""
    conn.execute("DELETE FROM scene_references WHERE project_id=? AND scene_name=?", (project_id, name))
    scene_id = new_id("scene")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scene_references)").fetchall()}
    if "pack_status" in cols:
        conn.execute(
            "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, scene_canonical, "
            "prompt, image_path, qa_json, base_scene_id, bible_version, artifact_id, pack_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scene_id, project_id, name, 1, None, scene_canonical, prompt, image_path,
             json.dumps(qa, ensure_ascii=False), None, bible_version, artifact_id, "legacy_partial", now()))
    else:
        conn.execute(
            "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, scene_canonical, "
            "prompt, image_path, qa_json, base_scene_id, bible_version, artifact_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scene_id, project_id, name, 1, None, scene_canonical, prompt, image_path,
             json.dumps(qa, ensure_ascii=False), None, bible_version, artifact_id, now()))
    conn.commit()
    return scene_id


def scene_ref_exists(conn, project_id: str, name: str) -> bool:
    """Whether a complete current scene pack exists."""
    from app.multiview import (
        list_scene_views,
        scene_multiview_enabled,
        scene_pack_is_usable,
    )

    rows = conn.execute(
        "SELECT * FROM scene_references WHERE project_id=? AND scene_name=?",
        (project_id, name)).fetchall()
    for row in rows:
        if not (row["image_path"] and Path(row["image_path"]).exists()):
            continue
        if not scene_multiview_enabled():
            return True
        if scene_pack_is_usable(row, list_scene_views(row["id"], conn=conn)):
            return True
    return False


def _scene_gate_evaluations(artifact_id: str) -> list[dict]:
    """返回可作为场景候选当前门禁结论的评估（时间正序）。"""
    from app.evidence import repository as evidence_repository

    out: list[dict] = []
    for row in evidence_repository.get_evaluations(artifact_id):
        name = str(row.get("evaluator_name") or "")
        evidence = row.get("evidence") or {}
        qa = evidence.get("qa") if isinstance(evidence, dict) else None
        if "consistency_qa" in name or name == "scene_candidate_human_hard_gate_review":
            out.append(row)
        elif isinstance(qa, dict) and qa.get("policy_version"):
            out.append(row)
    return out


def _latest_scene_gate_evaluation(artifact_id: str) -> dict | None:
    rows = _scene_gate_evaluations(artifact_id)
    return rows[-1] if rows else None


def _scene_candidate_qa_score(artifact_id: str) -> float:
    """从 consistency_qa 评估取 0~1 分；无记录返回 -1，便于排序垫底。"""
    row = _latest_scene_gate_evaluation(artifact_id)
    score = row.get("score") if row else None
    if isinstance(score, (int, float)):
        return float(score) / 100.0
    return -1.0


def _qa_dict_from_artifact(artifact_id: str) -> dict:
    """重建登记 scene_references 所需的 qa_json。"""
    row = _latest_scene_gate_evaluation(artifact_id)
    if row:
        evidence = row.get("evidence") or {}
        if isinstance(evidence, dict):
            qa = evidence.get("qa")
            if isinstance(qa, dict):
                return dict(qa)
        score = row.get("score")
        if isinstance(score, (int, float)):
            issues = []
            for item in (row.get("issues") or []):
                if isinstance(item, dict) and item.get("message"):
                    issues.append(str(item["message"]))
                elif isinstance(item, str):
                    issues.append(item)
            return {"overall": float(score) / 100.0, "issues": issues}
    return {"overall": 0.0, "issues": ["人工采纳（无 QA 记录）"], "human_adopted": True}


def scene_candidate_gate(artifact_id: str, *, require_current_policy: bool = True) -> dict:
    """候选的当前评分结论；任何 QA 状态都不改变文件的采用资格。"""
    row = _latest_scene_gate_evaluation(artifact_id)
    if not row:
        return {
            "state": "unverified", "verified": False, "hard_failures": [],
            "warnings": [], "uncertainties": ["尚未执行新版场景 QA"], "evaluation": None,
            "runtime_blocking": False, "production_eligible": True,
        }
    evidence = row.get("evidence") or {}
    qa = evidence.get("qa") if isinstance(evidence, dict) else None
    qa = dict(qa) if isinstance(qa, dict) else {}
    hard = [str(item) for item in (qa.get("hard_failures") or []) if str(item).strip()]
    for issue in row.get("issues") or []:
        if (isinstance(issue, dict) and issue.get("severity") == "blocker"
                and issue.get("code") == "SCENE_HARD_GATE" and issue.get("message")):
            hard.append(str(issue["message"]))
    warnings = [str(item) for item in (qa.get("warnings") or qa.get("issues") or []) if str(item).strip()]
    uncertainties = [str(item) for item in (qa.get("uncertainties") or []) if str(item).strip()]
    current_policy = bool(qa.get("policy_version"))
    explicit_unverified = qa.get("status") in {"unverified", "pending"} or bool(row.get("recovered"))
    hard_failed = bool(hard) or qa.get("status") == "failed"
    verified = (
        not hard_failed and not explicit_unverified and row.get("hard_gate_passed") in {True, 1}
        and (current_policy or not require_current_policy)
    )
    state = "hard_failed" if hard_failed else ("passed" if verified else "unverified")
    if not current_policy and require_current_policy:
        uncertainties.append("缺少新版硬门禁策略版本证据")
    return {
        "state": state, "verified": verified,
        "hard_failures": list(dict.fromkeys(hard)),
        "warnings": list(dict.fromkeys(warnings)),
        "uncertainties": list(dict.fromkeys(uncertainties)),
        "qa": qa, "evaluation": row,
        "runtime_blocking": False,
        "production_eligible": True,
    }


def list_scene_reference_candidates(conn, project_id: str, scene_name: str) -> list[dict]:
    """列出某场景全部 scene_reference 候选产物（含已采纳/已替代），按创建时间升序。"""
    from app.db import rows_to_dicts

    rows = rows_to_dicts(conn.execute(
        """SELECT * FROM artifacts
           WHERE type='scene_reference' AND scope_type='reference_asset'
             AND scope_id LIKE ? AND status != 'stale'
           ORDER BY created_at, version""",
        (f"{project_id}:%",),
    ).fetchall())
    out: list[dict] = []
    for row in rows:
        try:
            content = json.loads(row.get("content_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            content = {}
        if str(content.get("scene_name") or "").strip() != scene_name:
            continue
        path = row.get("file_path") or ""
        if not path or not Path(path).exists():
            continue
        out.append({
            "artifact_id": row["id"],
            "status": row["status"],
            "file_path": path,
            "content": content,
            "created_at": row.get("created_at") or "",
            "qa_score": _scene_candidate_qa_score(row["id"]),
            "scope_id": row.get("scope_id"),
        })
    return out


def pick_best_scene_candidate(conn, project_id: str, scene_name: str) -> dict | None:
    """按 QA 分选最高分候选；同分取较新创建。"""
    candidates = list_scene_reference_candidates(conn, project_id, scene_name)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["qa_score"], item["created_at"]))


def _scene_candidate_context(project_id: str, scene_name: str, artifact_id: str) -> tuple[dict, dict, dict]:
    """校验候选归属并返回 project/scene/artifact，供复验与人工复核共用。"""
    from app.evidence import repository as evidence_repository

    conn = get_conn()
    project_row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project_row or not project_row["bible_json"]:
        raise ValueError("项目不存在或还没有角色圣经")
    project = dict(project_row)
    bible_data = json.loads(project["bible_json"])
    scene = next(
        (item for item in bible_data.get("scenes", []) if item.get("name") == scene_name),
        None,
    )
    if scene is None:
        raise ValueError(f"场景不存在：{scene_name}")
    artifact = evidence_repository.get_artifact(artifact_id)
    if not artifact:
        raise KeyError(f"候选不存在：{artifact_id}")
    if artifact.get("type") != "scene_reference" or artifact.get("scope_type") != "reference_asset":
        raise ValueError("不是场景参考图候选")
    if artifact.get("status") == "stale":
        raise ValueError("候选已过期，不能处理")
    try:
        content = artifact.get("content")
        if content is None and artifact.get("content_json"):
            content = json.loads(artifact["content_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        content = {}
    content = content if isinstance(content, dict) else {}
    if str(content.get("scene_name") or "").strip() != scene_name:
        raise ValueError(f"候选不属于场景「{scene_name}」")
    if not str(artifact.get("scope_id") or "").startswith(f"{project_id}:"):
        raise ValueError("候选不属于当前项目")
    path = artifact.get("file_path") or ""
    if not path or not Path(path).exists():
        raise ValueError("候选图片文件不存在")
    artifact["content"] = content
    return project, scene, artifact


async def adopt_scene_candidate(
    project_id: str,
    scene_name: str,
    artifact_id: str,
    *,
    reason: str = "",
    decided_by: str = "user",
    ensure_pack: bool = False,
) -> dict:
    """将指定候选图采纳为场景库主图（人工或自动兜底）。

    多视角模式下，已有当前整包时会先用候选定场图与其余当前视角做整包 QA，
    通过后才在同一事务中切换；失败时当前整包完全不变。
    """
    from app.evidence import repository as evidence_repository
    from app.harness.types import Evaluation

    if not (reason or "").strip():
        raise ValueError("采纳候选必须填写采纳理由")

    conn = get_conn()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not project["bible_json"]:
        raise ValueError("项目不存在或还没有角色圣经")
    bible_data = json.loads(project["bible_json"])
    scene = next(
        (item for item in bible_data.get("scenes", []) if item.get("name") == scene_name),
        None,
    )
    if scene is None:
        raise ValueError(f"场景不存在：{scene_name}")

    artifact = evidence_repository.get_artifact(artifact_id)
    if not artifact:
        raise KeyError(f"候选不存在：{artifact_id}")
    if artifact.get("type") != "scene_reference" or artifact.get("scope_type") != "reference_asset":
        raise ValueError("不是场景参考图候选")
    if artifact.get("status") == "stale":
        raise ValueError("候选已过期，不能采纳")
    # VLM 图片质检已下线：候选是否可采纳只看文件是否存在（技术校验），不再由 QA 打分或拦截。
    # scene_candidate_gate 对没有历史 QA 证据的候选返回空 hard_failures/warnings，这里原样透传，
    # 不再用 normalize_scene_image_qa 重新归一化（该函数已随 app.scene_policy 一并下线）。
    current_gate = scene_candidate_gate(artifact_id, require_current_policy=False)
    latest_qa = dict(current_gate.get("qa") or {})
    warnings = list(dict.fromkeys([
        *(current_gate.get("warnings") or []),
        *(current_gate.get("hard_failures") or []),
        *(current_gate.get("uncertainties") or []),
    ]))
    from app.multiview import scene_multiview_enabled
    multiview_enabled = scene_multiview_enabled()
    try:
        content = artifact.get("content")
        if content is None and artifact.get("content_json"):
            content = json.loads(artifact["content_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        content = {}
    if not isinstance(content, dict):
        content = {}
    if str(content.get("scene_name") or "").strip() != scene_name:
        raise ValueError(f"候选不属于场景「{scene_name}」")
    scope_id = str(artifact.get("scope_id") or "")
    if not scope_id.startswith(f"{project_id}:"):
        raise ValueError("候选不属于当前项目")

    image_path = artifact.get("file_path") or ""
    if not image_path or not Path(image_path).exists():
        raise ValueError("候选图片文件不存在")

    adopt_reason = (reason or "").strip()
    qa = _qa_dict_from_artifact(artifact_id)
    qa = dict(qa)
    if multiview_enabled:
        qa.update(latest_qa)
    qa["human_adopted"] = True
    qa["adoption_reason"] = adopt_reason
    prompt = str(content.get("prompt") or scene.get("scene_prompt_override") or "").strip()
    if not prompt:
        style = (bible_data.get("world") or {}).get("visual_style_canonical") or ""
        prompt = scene_ref_prompt(
            style,
            scene.get("scene_canonical") or "",
            scene_name=scene_name,
        )
    pack: dict | None = None
    bible_version = project["bible_version"] or 0
    scene_id: str
    current = conn.execute(
        "SELECT * FROM scene_references WHERE project_id=? AND scene_name=? AND ep_end IS NULL "
        "ORDER BY ep_start DESC,created_at DESC LIMIT 1",
        (project_id, scene_name),
    ).fetchone()

    if multiview_enabled and current is not None and ensure_pack:
        from app.multiview import ensure_scene_multiview_pack, pack_result_ok

        minimum = conn.execute(
            "SELECT MIN(ep_start) AS value FROM scene_references "
            "WHERE project_id=? AND scene_name=? AND ep_start<=0",
            (project_id, scene_name),
        ).fetchone()
        candidate_start = int(
            minimum["value"] if minimum and minimum["value"] is not None else 0
        ) - 1
        scene_id = new_id("scene")
        conn.execute(
            "INSERT INTO scene_references(id,project_id,scene_name,ep_start,ep_end,scene_canonical,"
            "prompt,image_path,qa_json,base_scene_id,bible_version,artifact_id,pack_status,change_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scene_id, project_id, scene_name, candidate_start, 0,
             scene.get("scene_canonical") or "", prompt, image_path,
             json.dumps(qa, ensure_ascii=False), current["id"], bible_version, artifact_id,
             "generating", json.dumps({"change_type": "candidate_pack_validation"}, ensure_ascii=False), now()),
        )
        conn.commit()
        style = (bible_data.get("world") or {}).get("visual_style_canonical") or ""
        try:
            pack = await ensure_scene_multiview_pack(
                project_id=project_id,
                scene_reference_id=scene_id,
                scene_name=scene_name,
                scene_canonical=scene.get("scene_canonical") or "",
                visual_style=style,
                ep_start=int(current["ep_start"] or 1),
                primary_qa=qa,
                optional_views=[role for role in (scene.get("required_views") or []) if role == "action_zone"],
            )
            if not pack_result_ok(pack):
                warnings.append(
                    f"候选整包补齐重试耗尽（status={pack.get('status')}），已采用当前可用产物"
                )
        except asyncio.CancelledError:
            from app.rejected_media import purge_scene_reference
            purge_scene_reference(conn, scene_id)
            raise
        except Exception as exc:
            # 主候选文件已技术落盘；侧视角补齐失败只记录风险，不能撤销本次采用。
            pack = {"status": "retry_exhausted_fallback", "error": str(exc)[:300]}
            warnings.append(f"候选整包补齐未完成，已采用当前可用产物：{exc}")
        minimum = conn.execute(
            "SELECT MIN(ep_start) AS value FROM scene_references "
            "WHERE project_id=? AND scene_name=? AND ep_start<=0 AND id<>?",
            (project_id, scene_name, scene_id),
        ).fetchone()
        history_start = int(
            minimum["value"] if minimum and minimum["value"] is not None else 0
        ) - 1
        adopted_start = int(current["ep_start"] or 1)
        conn.execute(
            "UPDATE scene_references SET ep_start=?,ep_end=0 WHERE id=?",
            (history_start, current["id"]),
        )
        fallback_pack = not pack_result_ok(pack)
        conn.execute(
            "UPDATE scene_references SET ep_start=?,ep_end=NULL,pack_status=?,change_json=? WHERE id=?",
            (adopted_start, "partial_fallback" if fallback_pack else "ready", json.dumps({
                "change_type": "candidate_adoption", "adoption_reason": adopt_reason,
                "decided_by": str(decided_by or "user"), "previous_version_id": current["id"],
                "adopted_at": now(), "gate_retry_exhausted": fallback_pack,
                "quality_warnings": warnings,
            }, ensure_ascii=False), scene_id),
        )
    elif multiview_enabled and current is not None and not ensure_pack:
        from app.multiview import (
            SCENE_REQUIRED_VIEWS,
            clone_scene_views,
            list_scene_views,
            missing_required_views,
        )

        current_views = list_scene_views(current["id"], conn=conn)
        try:
            current_group = json.loads(current["group_qa_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            current_group = {}
        required_roles = tuple(current_group.get("required_views") or SCENE_REQUIRED_VIEWS)
        candidate_view = {
            "view_role": "establishing",
            "image_path": image_path,
            "prompt": prompt,
            "qa_json": json.dumps(qa, ensure_ascii=False),
            "status": "ready",
            "artifact_id": artifact_id,
        }
        candidate_views = [
            candidate_view if view.get("view_role") == "establishing" else dict(view)
            for view in current_views
        ]
        if not any(view.get("view_role") == "establishing" for view in current_views):
            candidate_views.append(candidate_view)
        missing = missing_required_views(candidate_views, required_roles)
        if missing:
            warnings.append(
                "当前整包缺少必需视角，已采用现有视图并记录风险：" + "、".join(missing)
            )
        # VLM 图片质检已下线：整包一致性不再评分，采纳只看必需视角文件是否齐全。
        group_qa = {
            "status": "partial_fallback" if missing else "ready",
            "required_views": list(required_roles),
        }

        # 先克隆当前包到负数历史槽，再原子更新当前包。这样既不改变下游引用 ID，
        # 也保留了可回滚的完整旧版本（含全部视角与证据）。
        minimum = conn.execute(
            "SELECT MIN(ep_start) AS value FROM scene_references "
            "WHERE project_id=? AND scene_name=? AND ep_start<=0",
            (project_id, scene_name),
        ).fetchone()
        history_start = int(minimum["value"] if minimum and minimum["value"] is not None else 0) - 1
        history_id = new_id("scene")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(scene_references)").fetchall()}
        history_columns = [
            "id", "project_id", "scene_name", "ep_start", "ep_end", "scene_canonical", "prompt",
            "image_path", "qa_json", "base_scene_id", "bible_version", "artifact_id", "created_at",
        ]
        optional_columns = [
            "pack_status", "group_qa_json", "state_canonical", "input_fingerprint", "change_json",
        ]
        history_columns.extend(column for column in optional_columns if column in cols)
        history_values = {
            column: current[column] if column in current.keys() else None for column in history_columns
        }
        history_values.update({
            "id": history_id, "ep_start": history_start, "ep_end": 0,
            "base_scene_id": current["id"], "created_at": now(),
        })
        placeholders = ",".join("?" for _ in history_columns)
        conn.execute(
            f"INSERT INTO scene_references({','.join(history_columns)}) VALUES({placeholders})",
            tuple(history_values[column] for column in history_columns),
        )
        clone_scene_views(conn, source_scene_id=current["id"], dest_scene_id=history_id)
        change = {}
        if "change_json" in current.keys() and current["change_json"]:
            try:
                change = json.loads(current["change_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                change = {}
        change.update({
            "change_type": "candidate_adoption", "adoption_reason": adopt_reason,
            "decided_by": str(decided_by or "user"), "previous_version_id": history_id,
            "adopted_at": now(), "gate_retry_exhausted": bool(missing),
            "quality_warnings": warnings,
        })
        assignments = [
            "image_path=?", "prompt=?", "qa_json=?", "artifact_id=?", "bible_version=?",
            "pack_status=?", "group_qa_json=?",
        ]
        values: list[object] = [
            image_path, prompt, json.dumps(qa, ensure_ascii=False), artifact_id, bible_version,
            "partial_fallback" if missing else "ready",
            json.dumps(group_qa, ensure_ascii=False),
        ]
        if "change_json" in cols:
            assignments.append("change_json=?")
            values.append(json.dumps(change, ensure_ascii=False))
        values.append(current["id"])
        conn.execute(
            f"UPDATE scene_references SET {','.join(assignments)} WHERE id=?", tuple(values),
        )
        establishing = next(
            (view for view in current_views if view.get("view_role") == "establishing"), None,
        )
        if establishing:
            conn.execute(
                "UPDATE scene_reference_views SET image_path=?,prompt=?,qa_json=?,artifact_id=?,"
                "status='ready',input_fingerprint=? WHERE id=?",
                (image_path, prompt, json.dumps(qa, ensure_ascii=False), artifact_id,
                 f"candidate:{artifact_id}", establishing["id"]),
            )
        scene_id = current["id"]
        pack = {"status": group_qa.get("status"), "group_qa": group_qa, "history_id": history_id}
    else:
        scene_id = register_initial_scene_ref(
            conn, project_id, scene_name, image_path,
            scene.get("scene_canonical") or "", prompt, qa, bible_version,
            artifact_id=artifact_id,
        )
        if ensure_pack and multiview_enabled:
            from app.multiview import ensure_scene_multiview_pack
            style = (bible_data.get("world") or {}).get("visual_style_canonical") or ""
            try:
                pack = await ensure_scene_multiview_pack(
                    project_id=project_id,
                    scene_reference_id=scene_id,
                    scene_name=scene_name,
                    scene_canonical=scene.get("scene_canonical") or "",
                    visual_style=style,
                    ep_start=1,
                    primary_qa=qa,
                )
            except Exception as exc:  # noqa: BLE001 初次登记的整包失败留作候选，不供下游消费
                pack = {"status": "failed", "error": str(exc)}

    if artifact.get("status") != "approved":
        evidence_repository.commit_artifact(
            None,
            artifact_id,
            [Evaluation(
                evaluator_type="human",
                evaluator_name=str(decided_by or "user"),
                evaluator_version="1.0.0",
                status="passed",
                hard_gate_passed=True,
                score=100,
                evidence={"decision": "adopt", "reason": adopt_reason, "scene_name": scene_name},
            )],
        )

    class _Adopted:
        def __init__(self, name: str, path: str):
            self.name = name
            self.ref_image_path = path

    _merge_generated_scene_refs(conn, project_id, [_Adopted(scene_name, image_path)])
    conn.commit()
    return {
        "adopted": True,
        "scene_name": scene_name,
        "artifact_id": artifact_id,
        "scene_reference_id": scene_id,
        "image_path": image_path,
        "reason": adopt_reason,
        "qa_overall": qa.get("overall"),
        "soft_warnings": warnings,
        "gate_retry_exhausted": bool(current_gate.get("hard_failures") or current_gate.get("uncertainties")),
        "pack": pack,
    }


def scene_ref_for_episode(project_id: str, name: str, episode_no: int | None) -> str | None:
    """返回覆盖该集的场景图落盘路径；未命中返回 None。"""
    if not name:
        return None
    ep = episode_no if episode_no is not None else 1
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM scene_references "
        "WHERE project_id=? AND scene_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY ep_start DESC LIMIT 1", (project_id, name, ep, ep)).fetchone()
    if row:
        from app.multiview import list_scene_views, scene_multiview_enabled, scene_pack_is_usable
        views = list_scene_views(row["id"], conn=conn)
        usable = (
            not scene_multiview_enabled()
            or scene_pack_is_usable(row, views)
        )
    else:
        usable = False
    if usable and row["image_path"] and Path(row["image_path"]).exists():
        return row["image_path"]
    return None


def scene_views_for_episode(project_id: str, name: str, episode_no: int | None, *, ready_only: bool = False):
    """本集有效场景多视角包；供新链路使用。"""
    from app.multiview import scene_views_for_episode as _views
    return _views(project_id, name, episode_no, ready_only=ready_only)


def scene_ref_qa_for_episode(project_id: str, name: str, episode_no: int | None) -> dict | None:
    if not name:
        return None
    ep = episode_no if episode_no is not None else 1
    row = get_conn().execute(
        "SELECT qa_json FROM scene_references "
        "WHERE project_id=? AND scene_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY ep_start DESC LIMIT 1", (project_id, name, ep, ep)).fetchone()
    if row and row["qa_json"]:
        try:
            return json.loads(row["qa_json"])
        except (TypeError, ValueError):
            return None
    return None


def scene_refs_as_image_inputs(bible: Bible, scene_names: list[str], limit: int,
                               *, project_id: str | None = None,
                               episode_no: int | None = None) -> list[tuple[str, str]]:
    """规范场景名 →(data_url, "reference_image") 列表，最多 limit 张。
    有项目上下文时只接受通过新版门禁的分段包；无项目上下文的旧调用才回退 Bible 缓存。"""
    out: list[tuple[str, str]] = []
    by_name = {s.name: s for s in (getattr(bible, "scenes", None) or [])}
    seen: set[str] = set()
    for name in scene_names:
        if len(out) >= max(limit, 0):
            break
        if not name or name in seen:
            continue
        seen.add(name)
        path = scene_ref_for_episode(project_id, name, episode_no) if project_id else None
        if not path and not project_id:
            sc = by_name.get(name)
            path = getattr(sc, "ref_image_path", None) if sc else None
        if path and Path(path).exists():
            try:
                out.append((hiagent.data_url_from_file(path), "reference_image"))
            except OSError:
                continue
    return out


# ---------- 初始批量出图 ----------

async def generate_scene_refs(
    project_id: str,
    only_scene: str | list[str] | None = None,
    *,
    resume: bool = False,
    operation_started_at: float | None = None,
) -> None:
    """为项目全部（或指定）场景生成定场图，写回 bible_json 的 scenes[*].ref_image_path。"""
    conn = get_conn()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not project["bible_json"]:
        raise ValueError("项目不存在或还没有角色圣经")
    bible_data = json.loads(project["bible_json"])
    _restore_approved_scene_bible(conn, project_id, bible_data)
    bible = Bible.model_validate(bible_data)
    if not bible.scenes:
        raise ValueError("还没有场景圣经，请先生成场景清单")
    style = bible.world.visual_style_canonical
    bible_version = project["bible_version"] or 0

    selected_names = (
        {str(name).strip() for name in only_scene if str(name).strip()}
        if isinstance(only_scene, list)
        else ({only_scene} if only_scene else None)
    )
    targets = [s for s in bible.scenes if (selected_names is None or s.name in selected_names)]
    if not targets:
        raise ValueError(f"场景不存在：{only_scene}")

    # 批量出图（only_scene=None）：只补还没出过图的场景，已生成的跳过 → 按钮可重复点击而不重复出图。
    # 单场景重做（only_scene 指定）：强制重出，不跳过。
    if only_scene is None or resume:
        completed: list = []
        pending: list = []
        for scene in targets:
            if resume:
                from app.multiview import (
                    complete_legacy_scene_pack,
                    pack_result_ok,
                    scene_multiview_enabled,
                    scene_row_for_episode,
                )
                row = scene_row_for_episode(project_id, scene.name, 1)
                if row and row["image_path"] and Path(row["image_path"]).exists():
                    try:
                        pack = (
                            await complete_legacy_scene_pack(project_id, scene.name, 1, style)
                            if scene_multiview_enabled()
                            else {"status": "disabled"}
                        )
                    except Exception:  # noqa: BLE001 - full regeneration retries below
                        pack = None
                    if pack_result_ok(pack):
                        refreshed = scene_row_for_episode(project_id, scene.name, 1)
                        if (refreshed and refreshed["image_path"]
                                and Path(refreshed["image_path"]).exists()):
                            scene.ref_image_path = refreshed["image_path"]
                            completed.append(scene)
                            continue
            if scene_ref_exists(conn, project_id, scene.name):
                continue
            pending.append(scene)
        if completed:
            _merge_generated_scene_refs(conn, project_id, completed)
            conn.commit()
        targets = pending
        if not targets:
            return  # 当前场景库里的场景图都已就绪，无需重出

    # 各场景互相独立（互不引用彼此的定场图作为参考图），故按场景分批并发；
    # 并发池上限见 generation_concurrency.scene_reference_batch_semaphore
    # ——与人物定妆照各自独立，互不挤占彼此的槽位。场景内部（主图→各视角）
    # 的先后依赖完全保留在 ensure_scene_multiview_pack 内部，未受影响。
    # 上限按账号档位推导（app.quota.TIER_TABLE），不是固定常量：free 档批量点
    # 「全部生成」也只会跑到 1 个并发，不会绕过账号并发限制。
    bible_merge_lock = asyncio.Lock()
    owner_user_id = quota.owner_of_project(conn, project_id)
    semaphore = generation_concurrency.scene_reference_batch_semaphore(conn, owner_user_id)

    async def _bounded(sc) -> None:
        async with semaphore:
            await _generate_one_scene_reference(
                project_id, sc, style, project, bible_version, only_scene,
                operation_started_at, bible_merge_lock,
            )

    results = await asyncio.gather(
        *(_bounded(sc) for sc in targets), return_exceptions=True,
    )
    errors: list[str] = []
    failures: list[Exception] = []
    for sc, result in zip(targets, results):
        if result is None:
            continue
        if isinstance(result, Exception):
            errors.append(f"{sc.name}：{result}")
            failures.append(result)
        elif isinstance(result, BaseException):
            # CancelledError (or any other non-Exception BaseException): the
            # whole batch is being torn down, not "this scene failed" --
            # propagate instead of quietly recording it as a content failure.
            raise result

    # Portrait generation and reactive scene discovery can update the Bible in
    # parallel.  Only merge paths owned by this batch.
    _merge_generated_scene_refs(conn, project_id, targets)
    conn.commit()
    # 单个场景的有界重试耗尽不会回滚其他已生成场景，也不再
    # 终止后续分镜/视频。缺失的场景使用 scene_canonical 文本锨点保底。
    return {
        "generated": [sc.name for sc in targets if sc.ref_image_path],
        "gate_retry_exhausted": bool(errors),
        "warnings": errors,
    }


async def _generate_one_scene_reference(
    project_id: str,
    sc,
    style: str,
    project,
    bible_version: int,
    only_scene,
    operation_started_at: float | None,
    bible_merge_lock: asyncio.Lock,
) -> None:
    """Run one scene's full reference-image pipeline; raises on failure.

    Spawned as its own ``asyncio.Task`` by ``asyncio.gather`` in
    ``generate_scene_refs``.  ``get_conn()`` keys connections by
    ``asyncio.current_task()`` (see ``app.db``), so calling it fresh here --
    never inheriting the caller's ``conn`` via closure -- gives this scene
    its own isolated SQLite connection, isolated from concurrent siblings.
    """
    conn = get_conn()
    pending_state = (sc.pending_state_canonical or "").strip()
    pending_ep_start = sc.pending_state_ep_start
    if pending_state and pending_ep_start:
        current_state_row = _open_scene_ref(conn, project_id, sc.name)
        if current_state_row and int(current_state_row["ep_start"] or 1) < int(pending_ep_start):
            evolved = await _refresh_scene_on_state_change(
                project_id, sc.name, int(pending_ep_start), pending_state, style, bible_version,
                change_meta={
                    "change_type": "approved_scene_state_change",
                    "reason": "待审场景状态变化批准后付费重绘",
                    "persistence": "persistent",
                },
            )
            if not evolved:
                raise SceneAssetQualityError(f"场景状态变化版本未能创建：{sc.name}")
            sc.scene_canonical = pending_state
            sc.pending_state_canonical = None
            sc.pending_state_ep_start = None
            sc.ref_image_path = evolved["image_path"]
            # ``bible_merge_lock`` serializes this read-modify-write of the
            # whole bible_json blob against concurrent sibling scenes in the
            # same batch -- two unlocked writers here would silently lose one
            # another's merge (last write wins on the whole blob).
            async with bible_merge_lock:
                _merge_generated_scene_refs(conn, project_id, [sc])
                latest_project = conn.execute(
                    "SELECT bible_json FROM projects WHERE id=?", (project_id,),
                ).fetchone()
                latest_bible = json.loads(latest_project["bible_json"] or "{}")
                for item in latest_bible.get("scenes", []):
                    if item.get("name") == sc.name:
                        item["scene_canonical"] = pending_state
                        item["pending_state_canonical"] = None
                        item["pending_state_ep_start"] = None
                        break
                conn.execute(
                    "UPDATE projects SET bible_json=? WHERE id=?",
                    (json.dumps(latest_bible, ensure_ascii=False), project_id),
                )
                conn.commit()
            return
    # 批量补齐：候选堆积超过阈值时，采纳最高分而不再继续烧图。
    if only_scene is None or isinstance(only_scene, list):
        piled = list_scene_reference_candidates(conn, project_id, sc.name)
        if len(piled) > SCENE_CANDIDATE_AUTO_ADOPT_THRESHOLD:
            # VLM 图片质检已下线：候选列表已保证文件存在，不再有"重新质检以改善
            # 排序"这一步——qa_score 现在恒为占位值，直接按其排序即退化为按最新
            # 创建时间挑选，这是符合直觉的确定性兜底（较新的候选通常是较新一次
            # 补图尝试的结果）。
            best = max(piled, key=lambda item: (item["qa_score"], item["created_at"]))
            adopted = await adopt_scene_candidate(
                project_id,
                sc.name,
                best["artifact_id"],
                reason=(
                    f"检查并补齐：候选已达 {len(piled)} 张（阈值 "
                    f"{SCENE_CANDIDATE_AUTO_ADOPT_THRESHOLD}），自动采纳最高分 "
                    f"{best['qa_score']:.2f}"
                ),
                decided_by="scene_refs_auto_adopt",
                ensure_pack=True,
            )
            sc.ref_image_path = adopted["image_path"]
            return
    sc.ref_image_path = None
    base_prompt = (
        (sc.scene_prompt_override or "").strip()
        or scene_ref_prompt(style, sc.scene_canonical, scene_name=sc.name)
    )
    last_error: Exception | None = None
    retry_prompt: str | None = None
    retry_seed: str | None = None
    # 分数和审美提示仍只评分；仅确定性硬门禁失败时允许一次有界纠偏重生。
    for attempt in range(1, 3):
        scene_id: str | None = None
        path = str(Path(scene_ref_path(project_id, sc.name)).with_name(
            f"{_safe_name(sc.name)}__{new_id('candidate')}.jpg"
        ))
        prompt = retry_prompt or base_prompt
        try:
            call_meta = {
                "asset_kind": "scene_reference",
                "scene_name": sc.name,
                "episode_no": 1,
                "scene_ref_mode": "initial",
                "attempt": attempt,
            }
            if operation_started_at is not None:
                operation_material = (
                    f"{project_id}:{operation_started_at}:{sc.name}:"
                    f"scene_reference:{attempt}"
                )
                call_meta.update({
                    "operation_id": "op_scene_reference_" + hashlib.sha256(
                        operation_material.encode("utf-8")
                    ).hexdigest()[:32],
                    "reuse_successful_operation": True,
                })
            item = await _generate_scene_image(
                prompt,
                retry_seed,
                call_meta=call_meta,
            )
            await _save_image_item(item, path)
            # ``record_reference_asset`` 会物理删除硬失败候选。先在
            # 内存中保留一次纠偏编辑所需的输入，不让失败文件落盘滞留。
            candidate_seed = hiagent.data_url_from_file(path)
            qa = await _review_scene_ref(
                path, sc, expected_description=prompt,
            )
            artifact = record_reference_asset(
                asset_type="scene_reference",
                scope_id=f"{project_id}:{sc.name}:1",
                file_path=path,
                content={
                    "scene_name": sc.name,
                    "canonical": sc.scene_canonical,
                    "prompt": prompt,
                    "attempt": attempt,
                },
                parent_artifact_ids=(
                    [project["bible_artifact_id"]] if project["bible_artifact_id"] else []
                ),
                qa=qa,
            )
            if artifact["status"] not in {"approved", "validated"}:
                last_error = SceneAssetQualityError(
                    f"场景图技术校验未通过：{sc.name}"
                )
                retry_prompt = scene_hard_gate_retry_prompt(
                    base_prompt,
                    qa,
                    scene_name=sc.name,
                    scene_canonical=sc.scene_canonical,
                    visual_style=style,
                )
                if attempt >= 2 or not retry_prompt:
                    break
                retry_seed = candidate_seed
                continue
            from app.multiview import scene_multiview_enabled
            old_current = _open_scene_ref(conn, project_id, sc.name)
            is_atomic_replacement = bool(scene_multiview_enabled() and old_current)
            if is_atomic_replacement:
                # 新包先占用负数候选槽，完整 QA 期间不改变当前版本及下游引用。
                minimum = conn.execute(
                    "SELECT MIN(ep_start) AS value FROM scene_references "
                    "WHERE project_id=? AND scene_name=? AND ep_start<=0",
                    (project_id, sc.name),
                ).fetchone()
                candidate_start = int(
                    minimum["value"] if minimum and minimum["value"] is not None else 0
                ) - 1
                scene_id = new_id("scene")
                cols = {row[1] for row in conn.execute(
                    "PRAGMA table_info(scene_references)"
                ).fetchall()}
                if "pack_status" in cols:
                    conn.execute(
                        "INSERT INTO scene_references(id,project_id,scene_name,ep_start,ep_end,"
                        "scene_canonical,prompt,image_path,qa_json,base_scene_id,bible_version,artifact_id,"
                        "pack_status,change_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (scene_id, project_id, sc.name, candidate_start, 0, sc.scene_canonical,
                         prompt, path, json.dumps(qa, ensure_ascii=False), old_current["id"],
                         bible_version, artifact["id"], "generating",
                         json.dumps({"change_type": "pack_regeneration_candidate",
                                     "candidate_created_at": now()}, ensure_ascii=False), now()),
                    )
                else:
                    conn.execute(
                        "INSERT INTO scene_references(id,project_id,scene_name,ep_start,ep_end,"
                        "scene_canonical,prompt,image_path,qa_json,base_scene_id,bible_version,artifact_id,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (scene_id, project_id, sc.name, candidate_start, 0, sc.scene_canonical,
                         prompt, path, json.dumps(qa, ensure_ascii=False), old_current["id"],
                         bible_version, artifact["id"], now()),
                    )
                conn.commit()
            else:
                scene_id = register_initial_scene_ref(
                    conn, project_id, sc.name, path, sc.scene_canonical,
                    prompt, qa, bible_version, artifact_id=artifact["id"],
                )
            # The candidate is publishable only after its required views exist.
            from app.multiview import (
                ensure_scene_multiview_pack, scene_multiview_enabled, pack_result_ok,
            )
            if scene_multiview_enabled():
                pack = await ensure_scene_multiview_pack(
                    project_id=project_id,
                    scene_reference_id=scene_id,
                    scene_name=sc.name,
                    scene_canonical=sc.scene_canonical,
                    visual_style=style,
                    ep_start=1,
                    primary_qa=qa,
                    optional_views=[role for role in (sc.required_views or []) if role == "action_zone"],
                )
                if not pack_result_ok(pack):
                    raise SceneAssetQualityError(
                        f"场景多视角资产包结构不完整：{sc.name}"
                    )
            if is_atomic_replacement and old_current:
                # 完整包已通过：先把旧当前版本移入新的历史槽，再把候选切为当前。
                minimum = conn.execute(
                    "SELECT MIN(ep_start) AS value FROM scene_references "
                    "WHERE project_id=? AND scene_name=? AND ep_start<=0 AND id<>?",
                    (project_id, sc.name, scene_id),
                ).fetchone()
                history_start = int(
                    minimum["value"] if minimum and minimum["value"] is not None else 0
                ) - 1
                adopted_start = int(old_current["ep_start"] or 1)
                conn.execute(
                    "UPDATE scene_references SET ep_start=?,ep_end=0 WHERE id=?",
                    (history_start, old_current["id"]),
                )
                adoption_change = {
                    "change_type": "pack_regeneration", "previous_version_id": old_current["id"],
                    "adoption_reason": "付费整包重生完成",
                    "adopted_at": now(), "gate_retry_exhausted": False,
                }
                if "change_json" in cols:
                    conn.execute(
                        "UPDATE scene_references SET ep_start=?,ep_end=NULL,pack_status=?,"
                        "change_json=? WHERE id=?",
                        (adopted_start, "ready",
                         json.dumps(adoption_change, ensure_ascii=False), scene_id),
                    )
                else:
                    conn.execute(
                        "UPDATE scene_references SET ep_start=?,ep_end=NULL WHERE id=?",
                        (adopted_start, scene_id),
                    )
                conn.commit()
            sc.ref_image_path = path
            break
        except asyncio.CancelledError:
            if scene_id:
                from app.rejected_media import purge_scene_reference
                purge_scene_reference(conn, scene_id)
            sc.ref_image_path = None
            raise
        except hiagent.ProviderError as exc:
            if scene_id:
                from app.rejected_media import purge_scene_reference
                purge_scene_reference(conn, scene_id)
                scene_id = None
            sc.ref_image_path = None
            last_error = exc
        except Exception as exc:  # noqa: BLE001 候选失败后在有界循环内修复
            if scene_id:
                from app.rejected_media import purge_scene_reference
                purge_scene_reference(conn, scene_id)
                scene_id = None
            sc.ref_image_path = None
            last_error = exc
    if not sc.ref_image_path:
        raise last_error or hiagent.ProviderError(f"场景图生成失败：{sc.name}")


# ---------- 分镜阶段反应式发现新场景（对照 portraits.ensure_character_card 的新角色路径） ----------

async def assess_new_scene(label: str, spatial_context: str, *, style: str,
                           known_names: list[str], ep_label: str) -> dict:
    """把已确认剧本场次解析为新场景或已有场景别名，并产出自动建库字段。"""
    from app.visual_styles import is_photographic_style_prompt
    known = "、".join(known_names) or "（无）"
    scene_canonical_style_rule = (
        f"必须贴合画风「{style}」，是照片级摄影质感的实景环境描述，允许并鼓励真实材质、"
        "自然光影与摄影级细节。"
        if is_photographic_style_prompt(style)
        else f"必须贴合画风「{style}」，是 CG/动画/漫画类非真人渲染场景，严禁真人实拍/实景照片描述。"
    )
    prompt = f"""任务：把已确认剧本场次地点「{label}」解析成可用于分镜的规范场景。

全片画风（场景锚点必须与之一致）：{style}
已有规范场景（若「{label}」其实是这些场景的同一地点/别称，则 important=false，并在 existing_scene_name 返回下列某个完整名称）：
{known}

本场景的空间信息（{ep_label}）：
{spatial_context[:1000]}

判定口径：
- 这是已确认剧本中真实开拍的场次，不得因一次性过场而省略场景。
- important=true：它是已有列表之外的真正新地点，必须自动加入场景库并生成场景图。
- important=false：仅当它确实是已有场景的别名/简称；existing_scene_name 必须返回已有列表中的完整名称。
- name：稳定的场景短标签（4~10 字），不要与已有场景重名。
- scene_canonical 是"固定场景锚点串"：30~60 字，须含 地点/室内外/光线时段/标志陈设/氛围色调；只写视觉可见的环境信息，不写人物、不写剧情动作。{scene_canonical_style_rule}

只输出一个 JSON 对象：
{{"important": true/false, "existing_scene_name": "已有规范场景完整名称或空字符串", "reason": "一句话依据", "name": str, "scene_canonical": str, "location_kind": "室内|室外|其他"}}"""
    raw = await model_gateway.chat(
        [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=600,
        call_meta={"stage": "assess_new_scene", "scene_label": label},
    )
    obj = extract_json(raw)
    important = bool(obj.get("important"))
    name = (obj.get("name") or "").strip() or label.strip()
    canonical = (obj.get("scene_canonical") or "").strip()
    if len(canonical) > SCENE_CANONICAL_MAX:
        canonical = canonical[:SCENE_CANONICAL_MAX]
    if important and len(canonical) < SCENE_CANONICAL_MIN:
        important = False  # 锚点太稀薄不足以稳定定场 → 不入库
    return {
        "important": important,
        "existing_scene_name": (obj.get("existing_scene_name") or "").strip(),
        "reason": (obj.get("reason") or "").strip(),
        "name": name,
        "scene_canonical": canonical,
        "location_kind": (obj.get("location_kind") or "其他").strip() or "其他",
    }


def _commit_scene_bible_mutation(
    conn,
    project_id: str,
    data: dict,
    *,
    operation: str,
    scene_name: str,
) -> bool:
    row = conn.execute(
        "SELECT bible_version,bible_artifact_id FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    if not row:
        return False
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id=project_id,
        status="approved",
        trust_level="T2",
        content=data,
        parent_artifact_ids=[row["bible_artifact_id"]] if row["bible_artifact_id"] else [],
        contract_version="character-bible-1.0.0",
        prompt_version="reactive-scene-bible-1.0.0",
        model_snapshot={"operation": operation, "scene_name": scene_name},
    ))
    expected_version = int(row["bible_version"] or 0)
    cursor = conn.execute(
        "UPDATE projects SET bible_json=?,bible_version=?,bible_artifact_id=? "
        "WHERE id=? AND COALESCE(bible_version,0)=?",
        (
            json.dumps(data, ensure_ascii=False),
            expected_version + 1,
            artifact["id"],
            project_id,
            expected_version,
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def _append_scene_to_bible(conn, project_id: str, scene: dict) -> bool:
    """把 AI 已确认的新场景追加进 bible，并推进版本；内部处理不产生人工待审。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    data = json.loads(row["bible_json"])
    if scene.get("name") in {s.get("name") for s in data.get("scenes", [])}:
        return False
    normalized = Scene.model_validate(scene).model_dump(mode="json")
    data.setdefault("scenes", []).append(normalized)
    Bible.model_validate(data)
    return _commit_scene_bible_mutation(
        conn,
        project_id,
        data,
        operation="incremental_scene_add",
        scene_name=str(scene.get("name") or ""),
    )


def _append_scene_alias(conn, project_id: str, scene_name: str, alias: str) -> bool:
    alias = (alias or "").strip()
    if not alias:
        return False
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    data = json.loads(row["bible_json"])
    for scene in data.get("scenes", []):
        if scene.get("name") != scene_name:
            continue
        aliases = [str(item).strip() for item in (scene.get("aliases") or []) if str(item).strip()]
        if alias in aliases:
            return False
        aliases.append(alias)
        scene["aliases"] = aliases
        Bible.model_validate(data)
        return _commit_scene_bible_mutation(
            conn,
            project_id,
            data,
            operation="incremental_scene_alias",
            scene_name=scene_name,
        )
    return False


async def _generate_and_register_scene(project_id: str, name: str, scene_canonical: str,
                                       style: str, *, ep_start: int, bible_version: int) -> str | None:
    """为新场景出一张定场图并登记到 scene_references（适用集 ep_start~ 至今）。出图失败返回 None。"""
    base_prompt = scene_ref_prompt(style, scene_canonical, scene_name=name)
    conn = get_conn()
    # 同场景参考：若该场景已有更早分段的图（同一地点跨集演化），以它做 i2i 锚点保持一致；全新场景则为 None → 纯文生图。
    prior = same_scene_anchor(conn, project_id, name)
    anchor_url = hiagent.data_url_from_file(prior) if prior else None
    project = conn.execute(
        "SELECT bible_artifact_id FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    prior_row = conn.execute(
        "SELECT artifact_id FROM scene_references WHERE project_id=? AND scene_name=? ORDER BY ep_start DESC LIMIT 1",
        (project_id, name),
    ).fetchone()
    parent_ids = [
        artifact_id for artifact_id in (
            prior_row["artifact_id"] if prior_row else None,
            project["bible_artifact_id"] if project else None,
        ) if artifact_id
    ]
    dest = ""
    prompt = base_prompt
    qa: dict = {}
    artifact = None
    retry_prompt: str | None = None
    # Score-only：不因 QA 带 critique 重生。供应商技术失败时允许一次
    # 纯视觉等义改写重试，结果仍按原始批准场景合同做 QA。
    for attempt in range(1, 3):
        prompt = retry_prompt or base_prompt
        dest = str(Path(scene_ref_path(project_id, name, ep_start)).with_name(
            f"{_safe_name(name)}__ep{ep_start}__{new_id('candidate')}.jpg"
        ))
        try:
            item = await _generate_scene_image(
                prompt,
                anchor_url,
                call_meta={
                    "asset_kind": "scene_reference",
                    "scene_name": name,
                    "episode_no": ep_start,
                    "scene_ref_mode": "reactive",
                    "attempt": attempt,
                })
            await _save_image_item(item, dest)
            qa = await _review_scene_ref(dest, {"name": name, "scene_canonical": scene_canonical})
            artifact = record_reference_asset(
                asset_type="scene_reference",
                scope_id=f"{project_id}:{name}:{ep_start}",
                file_path=dest,
                content={"scene_name": name, "canonical": scene_canonical,
                         "prompt": prompt, "episode_start": ep_start, "attempt": attempt},
                parent_artifact_ids=parent_ids,
                qa=qa,
            )
            if artifact["status"] in {"approved", "validated"}:
                break
            return None
        except Exception:  # noqa: BLE001 技术失败不伪装成 QA 问题
            if attempt == 1:
                try:
                    retry_prompt = await _provider_visual_scene_retry_prompt(
                        style,
                        scene_canonical,
                    )
                except Exception:  # noqa: BLE001 改写失败时不扩大重试
                    retry_prompt = None
                    break
            continue
    if not artifact or artifact["status"] not in {"approved", "validated"}:
        return None
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, scene_canonical, "
        "prompt, image_path, qa_json, base_scene_id, bible_version, artifact_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("scene"), project_id, name, ep_start, None, scene_canonical, prompt, dest,
         json.dumps(qa, ensure_ascii=False), None, bible_version, artifact["id"], now()))
    conn.commit()
    return dest


def _collect_scene_labels(screenplay) -> list[str]:
    """从剧本场次结构/节拍里收集出现过的地点标签。"""
    labels: list[str] = []
    seen: set[str] = set()

    def _add(v: str) -> None:
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            labels.append(v)

    for sc in getattr(screenplay, "scene_outline", None) or []:
        _add(getattr(sc, "scene_heading", ""))
    for b in getattr(screenplay, "beats", None) or []:
        _add(getattr(b, "location", ""))
    return labels


def _queue_scene_auto_change(
    conn, project_id: str, *, kind: str, scene_name: str, episode_no: int,
    reason: str, payload: dict,
) -> dict:
    """保留内部场景发现审计；场景由 AI 自动处理，不再形成用户待审队列。"""
    row = conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    try:
        items = json.loads(row["bible_auto_changes_json"] or "[]") if row else []
    except (TypeError, ValueError, json.JSONDecodeError):
        items = []
    fingerprint = f"{kind}:{scene_name}:{episode_no}"
    existing = next((item for item in items if item.get("fingerprint") == fingerprint), None)
    if existing:
        if existing.get("status") != "auto_applied":
            existing["status"] = "processing"
            existing["reason"] = reason or existing.get("reason") or ""
            existing["payload"] = payload or existing.get("payload") or {}
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(items, ensure_ascii=False), project_id),
            )
            conn.commit()
        return existing
    item = {
        "id": new_id("scene_change"), "fingerprint": fingerprint,
        "kind": kind, "status": "processing", "scene": scene_name,
        "ep_start": episode_no, "reason": reason, "payload": payload,
        "decided_by": "ai_scene_preflight", "created_at": now(),
    }
    items.append(item)
    conn.execute(
        "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
        (json.dumps(items, ensure_ascii=False), project_id),
    )
    conn.commit()
    return item


def _mark_scene_auto_change(
    conn,
    project_id: str,
    change_id: str,
    *,
    status: str,
    reason: str,
    image_path: str | None = None,
) -> None:
    row = conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    try:
        items = json.loads(row["bible_auto_changes_json"] or "[]") if row else []
    except (TypeError, ValueError, json.JSONDecodeError):
        items = []
    for item in items:
        if item.get("id") != change_id:
            continue
        item["status"] = status
        item["decided_by"] = "ai_scene_preflight"
        item["decided_at"] = now()
        item["decision_reason"] = reason
        if image_path:
            item.setdefault("payload", {})["image_path"] = image_path
        break
    conn.execute(
        "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
        (json.dumps(items, ensure_ascii=False), project_id),
    )
    conn.commit()


async def _ensure_reactive_scene_image(
    project_id: str,
    scene: Scene,
    *,
    episode_no: int,
    style: str,
    bible_version: int,
) -> dict:
    """等待本集场景主图落盘；并发/恢复时复用同一场景版本，不借用其它地点。"""
    current_path = scene_ref_for_episode(project_id, scene.name, episode_no)
    if current_path:
        return {"image_path": current_path, "reused": True}
    lock = await _reactive_scene_lock(project_id, scene.name)
    async with lock:
        current_path = scene_ref_for_episode(project_id, scene.name, episode_no)
        if current_path:
            return {"image_path": current_path, "reused": True}
        conn = get_conn()
        broken = conn.execute(
            "SELECT * FROM scene_references WHERE project_id=? AND scene_name=? "
            "AND ep_start=? ORDER BY created_at DESC LIMIT 1",
            (project_id, scene.name, episode_no),
        ).fetchone()
        if broken:
            broken_path = str(broken["image_path"] or "")
            if broken_path and Path(broken_path).is_file():
                from app.multiview import (
                    complete_legacy_scene_pack,
                    pack_result_ok,
                    scene_multiview_enabled,
                )

                if scene_multiview_enabled():
                    try:
                        pack = await complete_legacy_scene_pack(
                            project_id,
                            scene.name,
                            episode_no,
                            style,
                        )
                        if not pack_result_ok(pack):
                            raise SceneAssetQualityError(
                                f"场景多视角资产包结构不完整：{scene.name}"
                            )
                    except Exception as exc:
                        conn.execute(
                            """UPDATE scene_references
                                  SET pack_status='failed',group_qa_json=?
                                WHERE id=?""",
                            (
                                json.dumps(
                                    {
                                        "status": "failed",
                                        "error": str(exc),
                                        "recovery": "resume_missing_views",
                                    },
                                    ensure_ascii=False,
                                ),
                                broken["id"],
                            ),
                        )
                        conn.commit()
                        raise
                    _update_bible_scene_canonical(
                        conn,
                        project_id,
                        scene.name,
                        scene.scene_canonical,
                        broken_path,
                    )
                    return {
                        "image_path": broken_path,
                        "reused": True,
                        "resumed_pack": True,
                        "pack_status": "ready",
                    }
                return {
                    "image_path": broken_path,
                    "reused": True,
                    "pack_status": str(broken["pack_status"] or ""),
                }
            conn.execute(
                "DELETE FROM scene_references WHERE id=?",
                (broken["id"],),
            )
            conn.commit()
        image_path = await _generate_and_register_scene(
            project_id,
            scene.name,
            scene.scene_canonical,
            style,
            ep_start=episode_no,
            bible_version=bible_version,
        )
        if not image_path or not Path(image_path).is_file():
            raise hiagent.ProviderError(f"场景图未能落盘：{scene.name}")
        _update_bible_scene_canonical(
            conn, project_id, scene.name, scene.scene_canonical, image_path,
        )
        try:
            from app.multiview import (
                complete_legacy_scene_pack,
                pack_result_ok,
                scene_multiview_enabled,
            )
            if scene_multiview_enabled():
                pack = await complete_legacy_scene_pack(
                    project_id, scene.name, episode_no, style,
                )
                if not pack_result_ok(pack):
                    raise SceneAssetQualityError(
                        f"场景多视角资产包结构不完整：{scene.name}"
                    )
        except Exception as exc:
            failed = _open_scene_ref(conn, project_id, scene.name)
            if failed:
                conn.execute(
                    """UPDATE scene_references
                          SET pack_status='failed',group_qa_json=?
                        WHERE id=?""",
                    (
                        json.dumps(
                            {
                                "status": "failed",
                                "error": str(exc),
                                "recovery": "resume_missing_views",
                            },
                            ensure_ascii=False,
                        ),
                        failed["id"],
                    ),
                )
                conn.commit()
            raise
        return {"image_path": image_path, "reused": False, "pack_status": "ready"}


async def ensure_scenes_for_storyboard(project_id: str, episode_no: int, screenplay, bible) -> dict:
    """分镜前反应式维护本集场景：AI 自动建库，再交给分镜。不内联等图落盘——分镜
    产出的是文本（场景锚点 + 镜头描述），图只在发起付费视频时才真的需要，那道闸
    已经独立落地（单镜 _assert_shot_generation_gate，整集
    scan_episode_reference_asset_gaps）。出图统一由映射台发布后触发的后台任务
    补齐，见 app/domain/screenplay_ops/background_portraits.py。"""
    scenes = list(getattr(bible, "scenes", None) or [])
    style = bible.world.visual_style_canonical
    conn = get_conn()

    labels = _collect_scene_labels(screenplay)
    summary_by_heading = {
        (getattr(scene, "scene_heading", "") or "").strip(): (
            getattr(scene, "summary", "") or ""
        )
        for scene in (getattr(screenplay, "scene_outline", None) or [])
    }
    unmatched = [
        lb for lb in labels
        if not match_scene_name(lb, scenes, allow_fuzzy=False)
    ]

    added: list[dict] = []
    evolved: list[dict] = []
    errors: list[str] = []
    blocking_errors: list[str] = []
    for label in unmatched:
        _scene_time, location = split_legacy_scene_setting(label)
        spatial_context = location or label
        try:
            verdict = await assess_new_scene(
                label, spatial_context, style=style,
                known_names=[s.name for s in scenes],
                ep_label=f"第 {episode_no} 集")
        except Exception as exc:  # noqa: BLE001
            message = f"{label}：场景识别失败" + code_ref(
                exc, action="assess_new_scene",
                context={"project_id": project_id, "scene": label, "episode_no": episode_no},
            )
            errors.append(message)
            blocking_errors.append(message)
            continue
        if not verdict["important"]:
            existing_name = verdict.get("existing_scene_name") or ""
            if existing_name not in {scene.name for scene in scenes}:
                message = f"{label}：AI 未能解析为新场景或已有场景别名"
                errors.append(message)
                blocking_errors.append(message)
                continue
            bible_lock = await _reactive_bible_lock(project_id)
            async with bible_lock:
                _append_scene_alias(conn, project_id, existing_name, label)
            project_row = conn.execute(
                "SELECT bible_json FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            scenes = Bible.model_validate(json.loads(project_row["bible_json"])).scenes
            continue
        name = verdict["name"]
        if _exact_known_scene_name(name, scenes):
            continue
        scene_payload = {
            "name": name,
            "scene_canonical": verdict["scene_canonical"],
            "location_kind": verdict["location_kind"],
            "first_episode": episode_no,
            "discovery_sources": [spatial_context[:500]],
            "aliases": [label] if label != name else [],
        }
        queued = _queue_scene_auto_change(
            conn, project_id, kind="scene_discovery", scene_name=name, episode_no=episode_no,
            reason=verdict["reason"], payload={
                "scene": scene_payload,
                "source_episode": episode_no, "source_episode_label": f"第 {episode_no} 集",
                "evidence_fragments": [spatial_context[:500]],
                "duplicate_candidates": [s.name for s in scenes if name in s.name or s.name in name],
            },
        )
        try:
            bible_lock = await _reactive_bible_lock(project_id)
            async with bible_lock:
                appended = _append_scene_to_bible(conn, project_id, scene_payload)
            project_row = conn.execute(
                "SELECT bible_json FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            scenes = Bible.model_validate(json.loads(project_row["bible_json"])).scenes
            if not appended and not match_scene_name(label, scenes, allow_fuzzy=False):
                raise ValueError("scene bible commit failed")
            added.append({
                "name": name, "reason": verdict["reason"], "change_id": queued["id"],
                "has_image": False, "auto_applied": True,
            })
        except Exception as exc:  # noqa: BLE001
            message = f"{name}：自动加入场景库失败" + code_ref(
                exc, action="auto_apply_scene_discovery",
                context={"project_id": project_id, "scene": name, "episode_no": episode_no},
            )
            _mark_scene_auto_change(
                conn, project_id, queued["id"], status="auto_apply_failed", reason=message,
            )
            errors.append(message)
            blocking_errors.append(message)

    # 场景卡片只需入库映射；分镜不再内联等图（用户明确口径：分镜不像视频
    # 生成那样往外发，不能因为图片没生成就报错）。出图交给后台补图任务
    # （见 app/domain/screenplay_ops/background_portraits.py），缺图的安全网
    # 挪到真正需要图的地方——发起付费视频时：整集走
    # scan_episode_reference_asset_gaps，单镜走 _assert_shot_generation_gate
    # （commit 2441f6f，先于本次解耦落地）。这里仍然拦"场景压根没建库成功"
    # ——那不是缺图，是这个场景本身还不存在，分镜没有可用的锚点。
    project_row = conn.execute(
        "SELECT bible_json,bible_version FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    current_bible = Bible.model_validate(json.loads(project_row["bible_json"]))
    scenes = list(current_bible.scenes)
    relevant_names: list[str] = []
    for label in labels:
        matched = match_scene_name(label, scenes, allow_fuzzy=False)
        if not matched:
            message = f"{label}：相关场景仍未完成自动建库"
            if message not in blocking_errors:
                blocking_errors.append(message)
            continue
        if matched not in relevant_names:
            relevant_names.append(matched)
    change_rows = conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    try:
        all_changes = json.loads(change_rows["bible_auto_changes_json"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        all_changes = []
    for name in relevant_names:
        for change in all_changes:
            if change.get("kind") == "scene_discovery" and change.get("scene") == name:
                _mark_scene_auto_change(
                    conn,
                    project_id,
                    str(change.get("id") or ""),
                    status="auto_applied",
                    reason="AI 已自动采纳场景，场景图交由后台生成",
                )

    # ② 已入库场景的永久状态演进（损毁/重建等）
    try:
        known_entries = _known_scene_change_entries(
            conn, project_id, episode_no, screenplay, scenes, summary_by_heading,
        )
        if known_entries:
            changes = await screen_scene_state_changes(known_entries, f"第 {episode_no} 集")
            for name, meta in changes.items():
                try:
                    queued = _queue_scene_auto_change(
                        conn, project_id, kind="scene_state_change", scene_name=name,
                        episode_no=episode_no, reason=meta.get("reason") or "场景永久状态变化",
                        payload={
                            "scene_name": name, "new_scene_canonical": meta["new_scene_canonical"],
                            "source_episode": episode_no,
                            "evidence_fragments": [str(meta.get("evidence_excerpt") or meta.get("reason") or "")],
                        },
                    )
                    project_state = conn.execute(
                        "SELECT bible_version FROM projects WHERE id=?", (project_id,),
                    ).fetchone()
                    refreshed = await _refresh_scene_on_state_change(
                        project_id,
                        name,
                        episode_no,
                        meta["new_scene_canonical"],
                        current_bible.world.visual_style_canonical,
                        int(project_state["bible_version"] or 0),
                        change_meta={
                            "change_dimensions": meta.get("change_dimensions") or [],
                            "persistence": meta.get("persistence") or "persistent",
                            "reason": meta.get("reason") or "",
                            "evidence_excerpt": meta.get("evidence_excerpt") or "",
                        },
                    )
                    if refreshed:
                        _mark_scene_auto_change(
                            conn, project_id, queued["id"], status="auto_applied",
                            reason="AI 已自动采纳场景状态变化并完成场景图更新",
                            image_path=refreshed.get("image_path"),
                        )
                    evolved.append({
                        "name": name, "change_id": queued["id"], "reason": meta.get("reason"),
                        "auto_applied": bool(refreshed), **(refreshed or {}),
                    })
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"{name}@第{episode_no}集场景演进失败"
                        + code_ref(exc, action="refresh_scene_state",
                                   context={"project_id": project_id, "scene": name, "episode_no": episode_no})
                    )
    except Exception as exc:  # noqa: BLE001 演进探测失败不阻断分镜
        errors.append("场景状态演进探测失败" + code_ref(exc, action="screen_scene_state_changes",
                                                    context={"project_id": project_id, "episode_no": episode_no}))

    return {
        "checked": len(unmatched),
        "added": added,
        "evolved": evolved,
        "errors": errors,
        "blocking_errors": blocking_errors,
        "ready_scenes": relevant_names,
    }


async def ensure_scenes_for_labels(project_id: str, episode_no: int, labels: list[str]) -> dict:
    """反应式场景发现，供没有编译剧本对象的调用方使用（如 episode_prep_pack 的资产
    映射，app/production/prep_pack.py）：对给定的原始场景提及标签逐个做 新场景/
    已有场景别名 判定，新场景则建库。出场景参考图不在本函数内联完成，见下方
    尾段说明。

    是 ``ensure_scenes_for_storyboard`` 的①部分（发现→建库）在“只有一串标签、
    没有 screenplay 场景结构”时的等价复用：调用同一批 assess_new_scene /
    _append_scene_to_bible / _append_scene_alias，不重复其判定逻辑。不含该函数的
    ②已入库场景状态演进探测（损毁/重建等）——那仍是分镜前维护职责，本函数的调用方
    （映射台）不需要，也拿不到②所需的 screenplay/summary_by_heading 输入。
    """
    conn = get_conn()
    project = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not (project["bible_json"] or "").strip():
        return {
            "added": [],
            "errors": [f"{label}：人物谱尚未初始化，无法建场景库" for label in labels],
            "resolved_names": {},
        }
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    scenes = list(bible.scenes)
    style = bible.world.visual_style_canonical

    unmatched = [
        label for label in labels
        if not match_scene_name(label, scenes, allow_fuzzy=False)
    ]
    added: list[dict] = []
    errors: list[str] = []
    # 本次调用内对每个 label 已经拿到、且经过代码核验（existing_name 必须真的
    # 在 {scene.name for scene in scenes} 里；新场景必须真的建库成功）的裁决
    # 结果——第32轮真实 EP7 回归 ERR-20260824-6ecfbe 根因：旧实现算完这份裁决
    # 就扔了，转头在下面用 match_scene_name(label, scenes) 重新反查一遍
    # resolved_names；当同一个 label 字符串因跨集历史原因已经被写成两个不同
    # 场景各自的别名时（真实数据："洞府" 同时是 南峰山脚洞府/洞府修行石室 的
    # 别名，_append_scene_alias 本身没有跨场景排他约束，属于合法的历史累积
    # 结果，不是数据损坏），match_scene_name 的唯一胜者要求（len(winners)==1）
    # 必然打平返回 None——哪怕模型这一次的裁决清清楚楚、可核验、两次调用
    # （round 30/32）结论完全一致。裁决已经代码核验过，就是这个 label 本次
    # 调用的权威结果，不该再喂给一个对历史别名冲突免疫力为零的通用反查函数
    # 重新赌一次。
    direct_resolutions: dict[str, str] = {}
    for label in unmatched:
        _scene_time, location = split_legacy_scene_setting(label)
        spatial_context = location or label
        try:
            verdict = await assess_new_scene(
                label, spatial_context, style=style,
                known_names=[s.name for s in scenes],
                ep_label=f"第 {episode_no} 集")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}：场景识别失败" + code_ref(
                exc, action="assess_new_scene",
                context={"project_id": project_id, "scene": label, "episode_no": episode_no},
            ))
            continue
        if not verdict["important"]:
            existing_name = verdict.get("existing_scene_name") or ""
            if existing_name not in {scene.name for scene in scenes}:
                errors.append(f"{label}：AI 未能解析为新场景或已有场景别名")
                continue
            bible_lock = await _reactive_bible_lock(project_id)
            async with bible_lock:
                _append_scene_alias(conn, project_id, existing_name, label)
            project_row = conn.execute(
                "SELECT bible_json FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            scenes = Bible.model_validate(json.loads(project_row["bible_json"])).scenes
            direct_resolutions[label] = existing_name
            continue
        name = verdict["name"]
        if _exact_known_scene_name(name, scenes):
            direct_resolutions[label] = name
            continue
        scene_payload = {
            "name": name,
            "scene_canonical": verdict["scene_canonical"],
            "location_kind": verdict["location_kind"],
            "first_episode": episode_no,
            "discovery_sources": [spatial_context[:500]],
            "aliases": [label] if label != name else [],
        }
        queued = _queue_scene_auto_change(
            conn, project_id, kind="scene_discovery", scene_name=name, episode_no=episode_no,
            reason=verdict["reason"], payload={
                "scene": scene_payload,
                "source_episode": episode_no, "source_episode_label": f"第 {episode_no} 集",
                "evidence_fragments": [spatial_context[:500]],
                "duplicate_candidates": [s.name for s in scenes if name in s.name or s.name in name],
            },
        )
        try:
            bible_lock = await _reactive_bible_lock(project_id)
            async with bible_lock:
                appended = _append_scene_to_bible(conn, project_id, scene_payload)
            project_row = conn.execute(
                "SELECT bible_json FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            scenes = Bible.model_validate(json.loads(project_row["bible_json"])).scenes
            if not appended and not match_scene_name(label, scenes, allow_fuzzy=False):
                raise ValueError("scene bible commit failed")
            added.append({
                "name": name, "reason": verdict["reason"], "change_id": queued["id"],
                "has_image": False,
            })
            direct_resolutions[label] = name
        except Exception as exc:  # noqa: BLE001
            message = f"{name}：自动加入场景库失败" + code_ref(
                exc, action="auto_apply_scene_discovery",
                context={"project_id": project_id, "scene": name, "episode_no": episode_no},
            )
            _mark_scene_auto_change(
                conn, project_id, queued["id"], status="auto_apply_failed", reason=message,
            )
            errors.append(message)

    # 场景卡片只需入库映射；出图从映射台解耦到后台，不在这里内联等图落盘
    # （定妆照同一轮已落地，见 app/domain/screenplay_ops/background_portraits.py
    # ::start_background_portraits 与其调用方 task_body.py::_screenplay_task
    # 的 finally——prep_pack 成功/失败/用户取消都会触发后台补图，只排除进程
    # 热更/停机）。缺场景图会在分镜/发起付费视频时被参考图就绪校验拦住，不会
    # 静默流到生成台。
    project_row = conn.execute(
        "SELECT bible_json,bible_version FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    current_bible = Bible.model_validate(json.loads(project_row["bible_json"]))
    scenes = list(current_bible.scenes)
    resolved_names: dict[str, str] = {}
    relevant_names: list[str] = []
    for label in labels:
        # 本次调用内刚做出、已核验的裁决优先（见上方 direct_resolutions 定义
        # 处的完整说明）；只有本来就不需要走 assess_new_scene 的 label（调用
        # 一开始就已经能裸精确/别名匹配）才落到 match_scene_name 反查。
        matched = direct_resolutions.get(label) or match_scene_name(
            label, scenes, allow_fuzzy=False,
        )
        if not matched:
            continue  # 未解析成功已在上面记过 errors
        resolved_names[label] = matched
        if matched not in relevant_names:
            relevant_names.append(matched)
    change_rows = conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    try:
        all_changes = json.loads(change_rows["bible_auto_changes_json"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        all_changes = []
    for name in relevant_names:
        for change in all_changes:
            if change.get("kind") == "scene_discovery" and change.get("scene") == name:
                _mark_scene_auto_change(
                    conn,
                    project_id,
                    str(change.get("id") or ""),
                    status="auto_applied",
                    reason="AI 已自动采纳场景，场景图交由后台生成",
                )

    return {
        "added": added,
        "errors": errors,
        "resolved_names": resolved_names,
    }


def _open_scene_ref(conn, project_id: str, name: str):
    return conn.execute(
        "SELECT * FROM scene_references WHERE project_id=? AND scene_name=? AND ep_end IS NULL "
        "ORDER BY ep_start DESC LIMIT 1",
        (project_id, name),
    ).fetchone()


def _known_scene_change_entries(conn, project_id, episode_no, screenplay, scenes, summary_by_heading) -> list[dict]:
    """为本集已映射到库内的场景收集状态演进探测条目。"""
    entries: list[dict] = []
    labels = _collect_scene_labels(screenplay)
    by_name = {s.name: s for s in scenes}
    for label in labels:
        name = match_scene_name(label, scenes, allow_fuzzy=False)
        if not name and label in by_name:
            name = label
        if not name or name not in by_name:
            continue
        cur = _open_scene_ref(conn, project_id, name)
        if not cur or cur["ep_start"] >= episode_no:
            continue
        context = summary_by_heading.get(label, "") or ""
        for b in getattr(screenplay, "beats", None) or []:
            if (getattr(b, "location", "") or "").strip() == label:
                context += "\n" + (getattr(b, "action", "") or getattr(b, "summary", "") or "")
        if not context.strip():
            continue
        entries.append({
            "name": name,
            "current_canonical": cur["scene_canonical"] or by_name[name].scene_canonical,
            "fragments": [context.strip()[:2000]],
        })
    return entries


class _SceneStateChangeItem(BaseModel):
    """一个已入库场景在本集是否发生需整包演进的永久状态变化。字段可留空/空列表，
    但结构由 schema 在生成层约束，不再事后从自由文本里抠。"""

    model_config = ConfigDict(extra="forbid")
    name: str
    changed: bool
    persistence: str = ""
    change_dimensions: list[str] = Field(default_factory=list)
    new_scene_canonical: str = ""
    reason: str = ""
    evidence_excerpt: str = ""


class _SceneStateChangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[_SceneStateChangeItem] = Field(default_factory=list)


async def screen_scene_state_changes(entries: list[dict], ep_label: str) -> dict[str, dict]:
    """判断已有场景是否发生永久损毁/重建等需整包演进的状态变化。"""
    if not entries:
        return {}
    payload = []
    for item in entries:
        payload.append({
            "name": item["name"],
            "current_canonical": item["current_canonical"],
            "evidence": "\n".join(item.get("fragments") or [])[:1800],
        })
    prompt = f"""任务：判断漫剧场景是否发生【永久状态变化】，需要生成新的场景多视角资产包。

范围（{ep_label}）：
{json.dumps(payload, ensure_ascii=False)}

只在下列情况标记 changed=true：
- 建筑/空间永久损毁、坍塌、烧毁、炸毁
- 明确重建、改建、装修后长期固定的新陈设
- 永久性标志物增减导致环境真值改变

不要标记：
- 普通昼夜、天气、临时烟雾/灯光/道具
- 只影响单镜构图的临时布置

输出 JSON 对象，根字段为 items：
{{"items":[{{"name":str,"changed":bool,"persistence":"persistent|episode|shot_only",
 "change_dimensions":["damage"|"rebuild"|"layout"|"decor"],
 "new_scene_canonical":str,"reason":str,"evidence_excerpt":str}}]}}
shot_only / 未永久变化请 changed=false。new_scene_canonical 须 30~80 字，只写视觉环境。"""
    response = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_SceneStateChangeResponse,
        validate=None,
        operation_id="screen_scene_state_changes:" + hashlib.sha256(
            f"{ep_label}:{json.dumps(payload, ensure_ascii=False)}".encode("utf-8")
        ).hexdigest(),
        temperature=0.2,
        max_tokens=1600,
        call_meta={
            "stage": "screen_scene_state_changes",
            "stage_key": "screen_scene_state_changes",
            "expected_json": True,
        },
    )
    out: dict[str, dict] = {}
    for item in response.items:
        if not item.changed:
            continue
        name = item.name.strip()
        if not name:
            continue
        persistence = (item.persistence or "persistent").strip().lower()
        if persistence == "shot_only":
            continue
        if persistence not in {"persistent", "episode"}:
            persistence = "persistent"
        dims = [str(d).strip() for d in item.change_dimensions if str(d).strip()]
        canonical = item.new_scene_canonical.strip()
        if len(canonical) < SCENE_CANONICAL_MIN:
            continue
        if len(canonical) > SCENE_CANONICAL_MAX:
            canonical = canonical[:SCENE_CANONICAL_MAX]
        out[name] = {
            "name": name,
            "changed": True,
            "persistence": persistence,
            "change_dimensions": dims or ["layout"],
            "new_scene_canonical": canonical,
            "reason": item.reason.strip(),
            "evidence_excerpt": item.evidence_excerpt.strip(),
        }
    return out


def _update_bible_scene_canonical(conn, project_id: str, name: str, canonical: str,
                                  ref_image_path: str | None = None) -> None:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return
    data = json.loads(row["bible_json"])
    for sc in data.get("scenes", []):
        if sc.get("name") == name:
            sc["scene_canonical"] = canonical
            if ref_image_path:
                sc["ref_image_path"] = ref_image_path
            break
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(data, ensure_ascii=False), project_id))
    conn.commit()


async def _refresh_scene_on_state_change(
    project_id: str, name: str, episode_no: int,
    new_canonical: str, style: str, bible_version: int,
    *, change_meta: dict | None = None,
) -> dict | None:
    """永久场景状态变化：临时生成完整多视角包，整包 QA 通过后原子切换。"""
    conn = get_conn()
    cur = _open_scene_ref(conn, project_id, name)
    if not cur or cur["ep_start"] >= episode_no:
        return None

    base_prompt = scene_ref_prompt(style, new_canonical, scene_name=name)
    prior = cur["image_path"] if cur["image_path"] and Path(cur["image_path"]).exists() else None
    anchor_url = hiagent.data_url_from_file(prior) if prior else None
    dest = str(Path(scene_ref_path(project_id, name, episode_no)).with_name(
        f"{_safe_name(name)}__ep{episode_no}__{new_id('candidate')}.jpg"
    ))
    item = await _generate_scene_image(
        base_prompt, anchor_url,
        call_meta={"asset_kind": "scene_reference", "scene_name": name,
                   "episode_no": episode_no, "scene_ref_mode": "state_evolve"},
    )
    await _save_image_item(item, dest)
    qa = await _review_scene_ref(dest, {"name": name, "scene_canonical": new_canonical})
    # Score-only：演进主图技术落盘即可，QA 不通过不阻断（PRD QA-SO #21）。
    if not Path(dest).exists() or Path(dest).stat().st_size <= 0:
        raise hiagent.ProviderError(f"场景状态演进主图未落盘：{name}")

    cols = {row[1] for row in conn.execute("PRAGMA table_info(scene_references)").fetchall()}
    new_scene_id = new_id("scene")
    change_json = json.dumps(change_meta or {}, ensure_ascii=False) if change_meta else None
    if "pack_status" in cols:
        conn.execute(
            """INSERT INTO scene_references(
                   id, project_id, scene_name, ep_start, ep_end, scene_canonical, prompt, image_path,
                   qa_json, base_scene_id, bible_version, artifact_id, pack_status, state_canonical,
                   change_json, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_scene_id, project_id, name, episode_no, episode_no, new_canonical, base_prompt, dest,
             json.dumps(qa, ensure_ascii=False), cur["id"], bible_version, None, "generating",
             new_canonical, change_json, now()),
        )
    else:
        conn.execute(
            """INSERT INTO scene_references(
                   id, project_id, scene_name, ep_start, ep_end, scene_canonical, prompt, image_path,
                   qa_json, base_scene_id, bible_version, artifact_id, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_scene_id, project_id, name, episode_no, None, new_canonical, base_prompt, dest,
             json.dumps(qa, ensure_ascii=False), cur["id"], bible_version, None, now()),
        )
    conn.commit()

    pack_status = "ready"
    if "pack_status" in cols:
        from app.multiview import ensure_scene_multiview_pack, pack_result_ok
        pack = await ensure_scene_multiview_pack(
            project_id=project_id,
            scene_reference_id=new_scene_id,
            scene_name=name,
            scene_canonical=new_canonical,
            visual_style=style,
            ep_start=episode_no,
            base_scene_id=cur["id"],
            primary_qa=qa,
        )
        pack_status = pack.get("status") or "failed"
        if not pack_result_ok(pack):
            from app.rejected_media import purge_scene_reference
            purge_scene_reference(conn, new_scene_id)
            raise SceneAssetQualityError(
                f"场景多视角资产包未通过，无法切换版本：{name}（waiting_asset_review）"
            )
        conn.execute("UPDATE scene_references SET ep_end=? WHERE id=?", (episode_no - 1, cur["id"]))
        persistence = (change_meta or {}).get("persistence") or "persistent"
        new_ep_end = episode_no if persistence == "episode" else None
        conn.execute(
            "UPDATE scene_references SET ep_end=?, pack_status=?, state_canonical=? WHERE id=?",
            (new_ep_end, "ready", new_canonical, new_scene_id),
        )
        if persistence == "episode":
            from app.multiview import clone_scene_views, PACK_STATUS_READY as READY
            reuse_id = new_id("scene")
            group_qa = cur["group_qa_json"] if "group_qa_json" in cur.keys() else None
            conn.execute(
                """INSERT INTO scene_references(
                       id, project_id, scene_name, ep_start, ep_end, scene_canonical, prompt, image_path,
                       qa_json, base_scene_id, bible_version, artifact_id, pack_status, group_qa_json, created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (reuse_id, project_id, name, episode_no + 1, None, cur["scene_canonical"], cur["prompt"],
                 cur["image_path"], cur["qa_json"], cur["id"], bible_version,
                 cur["artifact_id"] if "artifact_id" in cur.keys() else None,
                 READY, group_qa, now()),
            )
            clone_scene_views(conn, source_scene_id=cur["id"], dest_scene_id=reuse_id)
        conn.commit()
    else:
        conn.execute("UPDATE scene_references SET ep_end=? WHERE id=?", (episode_no - 1, cur["id"]))
        conn.commit()

    return {"ep_start": episode_no, "image_path": dest, "pack_status": pack_status,
            "scene_reference_id": new_scene_id}
