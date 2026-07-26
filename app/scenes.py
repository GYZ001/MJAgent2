"""场景图素材库工作流（跨集场景一致性的核心，与 app.refs/app.portraits 同构）。

场景圣经定稿后为每个规范场景生成 Seedream 定场图，存入 projects/<id>/scene_refs/，并登记到
scene_references（按"适用集区间"分段，ep_end=NULL 表示开区间=当前最新版）。生成镜头/关键帧时，
按 shot.scene_name 取覆盖该集的场景图，作为 scene 型参考图注入——同一场景的所有镜头、所有集
都吃同一张场景图 → 整片场景一致。

两条产生路径（完全复刻 app.portraits 的角色定妆照机制）：
  ① 初始批量：generate_scene_refs（场景圣经定稿后，适用集 1~ 至今）。
  ② 分镜阶段反应式发现：ensure_scenes_for_storyboard——剧本里出现、场景库里没有、够戏份的
     新场景 → 评估后补进 bible.scenes + 出图，适用集从首次出场那集起开放。
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.errors import ContentGenerationError, code_ref
from app.db import get_conn, new_id, now
from app.evidence.media import record_reference_asset
from app.harness import model_gateway
from app.refs import _safe_name
from app.schemas import Bible, Scene, extract_json
from app.validators import match_scene_name

SCENE_CANONICAL_MIN = 30
SCENE_CANONICAL_MAX = 80

# 初始场景图只覆盖前 N 章的场景（按钮批量出图的范围）；更靠后才出现的新场景留到分镜阶段反应式补图。
SCENE_BIBLE_CHAPTER_WINDOW = 20

# 「检查并补齐」时：某场景候选图数量超过该阈值，自动采纳最高分候选，不再继续出图。
SCENE_CANDIDATE_AUTO_ADOPT_THRESHOLD = 4


# ---------- 落盘 / 提示词 ----------

def _scene_dir(project_id: str) -> Path:
    d = config.PROJECTS_DIR / project_id / "scene_refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scene_ref_path(project_id: str, scene_name: str, ep_start: int | None = None) -> str:
    suffix = f"__ep{ep_start}" if ep_start else ""
    return str(_scene_dir(project_id) / f"{_safe_name(scene_name)}{suffix}.jpg")


def scene_ref_prompt(visual_style: str, scene_canonical: str) -> str:
    """场景定场图生成词：纯环境、无人物，作为跨集复用的场景锚点。"""
    return (
        f"{visual_style}。场景定场图（环境为主、画面中不出现任何人物）：{scene_canonical}。"
        "9:16 竖屏，构图完整的环境定场镜头，空间纵深清晰，光影与色调统一，电影质感，高清，"
        "无人物，无文字，无字幕，无水印，无 logo"
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
    """复用 stages.review_scene_image 对场景图做 QA（无人物，锚点传空）。"""
    from app.stages import review_scene_image
    name = scene["name"] if isinstance(scene, dict) else scene.name
    canonical = scene["scene_canonical"] if isinstance(scene, dict) else scene.scene_canonical
    expected = (expected_description or canonical).strip()
    try:
        return await review_scene_image(
            hiagent.encode_image_file(image_path), expected, name, [], kind="head",
            initiator_label="场景资产主图QA",
            environment_only=True,
        )
    except Exception as exc:  # noqa: BLE001 评估器失败不能伪装成通过
        return {
            "overall": 0.0,
            "issues": [f"场景一致性评估未完成：{type(exc).__name__}"],
            "qa_recovered": True,
        }


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
    """该场景是否已有一张落盘可用的场景图（已登记 scene_references 且文件还在）。
    批量出图据此跳过已生成的场景，使按钮可幂等重复点击。"""
    rows = conn.execute(
        "SELECT * FROM scene_references WHERE project_id=? AND scene_name=?",
        (project_id, name)).fetchall()
    existing = [r for r in rows if r["image_path"] and Path(r["image_path"]).exists()]
    if not existing:
        return False
    from app.multiview import (
        SCENE_REQUIRED_VIEWS,
        list_scene_views,
        pack_is_ready,
        scene_multiview_enabled,
    )
    if not scene_multiview_enabled():
        return True
    return any(
        pack_is_ready(
            row["pack_status"] if "pack_status" in row.keys() else None,
            list_scene_views(row["id"], conn=conn),
            SCENE_REQUIRED_VIEWS,
        )
        for row in existing
    )


def _scene_candidate_qa_score(artifact_id: str) -> float:
    """从 consistency_qa 评估取 0~1 分；无记录返回 -1，便于排序垫底。"""
    from app.evidence import repository as evidence_repository

    for row in evidence_repository.get_evaluations(artifact_id):
        name = str(row.get("evaluator_name") or "")
        if "consistency_qa" not in name:
            continue
        score = row.get("score")
        if isinstance(score, (int, float)):
            return float(score) / 100.0
    return -1.0


def _qa_dict_from_artifact(artifact_id: str) -> dict:
    """重建登记 scene_references 所需的 qa_json。"""
    from app.evidence import repository as evidence_repository

    for row in evidence_repository.get_evaluations(artifact_id):
        name = str(row.get("evaluator_name") or "")
        if "consistency_qa" not in name:
            continue
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


async def adopt_scene_candidate(
    project_id: str,
    scene_name: str,
    artifact_id: str,
    *,
    reason: str = "人工采纳候选",
    decided_by: str = "user",
    ensure_pack: bool = True,
) -> dict:
    """将指定候选图采纳为场景库主图（人工或自动兜底）。

    会 commit artifact（若尚未 approved）、登记 scene_references，并尽量补齐多视角包。
    多视角失败时仍保留主图（人工明确选择不应被整包 QA 抹掉）。
    """
    from app.evidence import repository as evidence_repository
    from app.harness.types import Evaluation

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

    adopt_reason = (reason or "").strip() or "人工采纳候选"
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

    qa = _qa_dict_from_artifact(artifact_id)
    qa = dict(qa)
    qa["human_adopted"] = True
    qa["adoption_reason"] = adopt_reason
    prompt = str(content.get("prompt") or scene.get("scene_prompt_override") or "").strip()
    if not prompt:
        style = (bible_data.get("world") or {}).get("visual_style_canonical") or ""
        prompt = scene_ref_prompt(style, scene.get("scene_canonical") or "")
    bible_version = project["bible_version"] or 0
    scene_id = register_initial_scene_ref(
        conn, project_id, scene_name, image_path,
        scene.get("scene_canonical") or "", prompt, qa, bible_version,
        artifact_id=artifact_id,
    )

    pack: dict | None = None
    if ensure_pack:
        from app.multiview import ensure_scene_multiview_pack, scene_multiview_enabled
        if scene_multiview_enabled():
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
            except Exception as exc:  # noqa: BLE001 主图已采纳，整包失败只记结果
                pack = {"status": "failed", "error": str(exc)}
            # 人工/自动兜底采纳：主图保留；整包未就绪不回滚主图。

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
        "pack": pack,
    }


def scene_ref_for_episode(project_id: str, name: str, episode_no: int | None) -> str | None:
    """返回覆盖该集的场景图落盘路径；未命中返回 None。"""
    if not name:
        return None
    ep = episode_no if episode_no is not None else 1
    row = get_conn().execute(
        "SELECT image_path FROM scene_references "
        "WHERE project_id=? AND scene_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY ep_start DESC LIMIT 1", (project_id, name, ep, ep)).fetchone()
    if row and row["image_path"] and Path(row["image_path"]).exists():
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
    优先 scene_references 按集分段图；未命中回退 bible.scenes 的 ref_image_path。"""
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
        if not path:
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
    only_scene: str | None = None,
    *,
    resume: bool = False,
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

    targets = [s for s in bible.scenes if (only_scene is None or s.name == only_scene)]
    if not targets:
        raise ValueError(f"场景不存在：{only_scene}")

    # 批量出图（only_scene=None）：只补还没出过图的场景，已生成的跳过 → 按钮可重复点击而不重复出图。
    # 单场景重做（only_scene 指定）：强制重出，不跳过。
    if only_scene is None or resume:
        completed: list = []
        pending: list = []
        for scene in targets:
            if scene_ref_exists(conn, project_id, scene.name):
                continue
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
            pending.append(scene)
        if completed:
            _merge_generated_scene_refs(conn, project_id, completed)
            conn.commit()
        targets = pending
        if not targets:
            return  # 当前场景库里的场景图都已就绪，无需重出

    errors: list[str] = []
    failures: list[Exception] = []
    for sc in targets:
        try:
            # 批量补齐：候选堆积超过阈值时，采纳最高分而不再继续烧图。
            if only_scene is None:
                piled = list_scene_reference_candidates(conn, project_id, sc.name)
                if len(piled) > SCENE_CANDIDATE_AUTO_ADOPT_THRESHOLD:
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
                    )
                    sc.ref_image_path = adopted["image_path"]
                    continue
            sc.ref_image_path = None
            base_prompt = ((sc.scene_prompt_override or "").strip()
                           or scene_ref_prompt(style, sc.scene_canonical))
            last_error: Exception | None = None
            critique = ""
            for attempt in range(1, 3):
                scene_id: str | None = None
                path = str(Path(scene_ref_path(project_id, sc.name)).with_name(
                    f"{_safe_name(sc.name)}__{new_id('candidate')}.jpg"
                ))
                prompt = base_prompt
                if critique:
                    prompt += f"。上一版一致性检查问题：{critique}。本版必须逐条修复。"
                try:
                    item = await _generate_scene_image(
                        prompt,
                        call_meta={
                            "asset_kind": "scene_reference",
                            "scene_name": sc.name,
                            "episode_no": 1,
                            "scene_ref_mode": "initial",
                            "attempt": attempt,
                        })
                    await _save_image_item(item, path)
                    qa = await _review_scene_ref(
                        path, sc, expected_description=base_prompt,
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
                    if artifact["status"] != "approved":
                        critique = "；".join(str(item) for item in (qa.get("issues") or []))[:400]
                        detail = f"：{critique}" if critique else ""
                        last_error = ContentGenerationError(
                            f"场景图一致性检查未通过：{sc.name}（第 {attempt} 次）{detail}"
                        )
                        continue
                    scene_id = register_initial_scene_ref(
                        conn, project_id, sc.name, path, sc.scene_canonical,
                        prompt, qa, bible_version, artifact_id=artifact["id"],
                    )
                    # 初始场景多视角资产包：反打/整包失败则禁止半包生效
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
                        )
                        if not pack_result_ok(pack):
                            raise hiagent.ProviderError(
                                f"多视角资产包未通过，禁止生效：{sc.name}"
                                f"（status={pack.get('status')}）"
                            )
                    # Publish the scene anchor only when all required angles pass.
                    sc.ref_image_path = path
                    break
                except asyncio.CancelledError:
                    if scene_id:
                        conn.execute("DELETE FROM scene_references WHERE id=?", (scene_id,))
                        conn.commit()
                    sc.ref_image_path = None
                    raise
                except hiagent.ProviderError as exc:
                    if scene_id:
                        conn.execute("DELETE FROM scene_references WHERE id=?", (scene_id,))
                        conn.commit()
                        scene_id = None
                    sc.ref_image_path = None
                    if "多视角资产包未通过" in str(exc):
                        raise
                    last_error = exc
                except Exception as exc:  # noqa: BLE001 候选失败后在有界循环内修复
                    if scene_id:
                        conn.execute("DELETE FROM scene_references WHERE id=?", (scene_id,))
                        conn.commit()
                        scene_id = None
                    sc.ref_image_path = None
                    last_error = exc
            if not sc.ref_image_path:
                raise last_error or hiagent.ProviderError(f"场景图生成失败：{sc.name}")
        except Exception as exc:  # noqa: BLE001 失败要响：逐场景记录，最后汇总抛出
            errors.append(f"{sc.name}：{exc}")
            failures.append(exc)

    # Portrait generation and reactive scene discovery can update the Bible in
    # parallel.  Only merge paths owned by this batch.
    _merge_generated_scene_refs(conn, project_id, targets)
    conn.commit()
    if errors:
        message = "部分场景图失败：" + "；".join(errors)[:600]
        if failures and all(isinstance(exc, ContentGenerationError) for exc in failures):
            raise ContentGenerationError(message)
        raise hiagent.ProviderError(message)


# ---------- 分镜阶段反应式发现新场景（对照 portraits.ensure_character_card 的新角色路径） ----------

async def assess_new_scene(label: str, context: str, *, style: str,
                           known_names: list[str], ep_label: str) -> dict:
    """判断剧本里出现、场景库里没有的地点是否值得【单独建场景并出图】，并产出场景字段。
    返回 {important, reason, name, scene_canonical, location_kind}。"""
    known = "、".join(known_names) or "（无）"
    prompt = f"""任务：判断漫剧里出现的地点「{label}」是否值得【加入场景图素材库并单独出一张场景定场图】（用作跨集复用的环境锚点）。

全片画风（场景锚点必须与之一致）：{style}
已有规范场景（若「{label}」其实是这些场景的同一地点/别称，则 important=false）：
{known}

本场景相关剧本上下文（{ep_label}）：
{context[:4000]}

判定口径：
- important=true 仅当：「{label}」是【真正的新地点】，且【反复出现 / 有戏份 / 画面感强】，值得稳定其环境外观。
- important=false：一次性过场、只被提及、或其实是已有场景的同一地点。
- name：稳定的场景短标签（4~10 字），不要与已有场景重名。
- scene_canonical 是"固定场景锚点串"：30~60 字，须含 地点/室内外/光线时段/标志陈设/氛围色调；只写视觉可见的环境信息，不写人物、不写剧情动作。必须贴合画风「{style}」，是 CG/动画/漫画类非真人渲染场景，严禁真人实拍/实景照片描述。

只输出一个 JSON 对象：
{{"important": true/false, "reason": "一句话依据", "name": str, "scene_canonical": str, "location_kind": "室内|室外|其他"}}"""
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
        "reason": (obj.get("reason") or "").strip(),
        "name": name,
        "scene_canonical": canonical,
        "location_kind": (obj.get("location_kind") or "其他").strip() or "其他",
    }


def _append_scene_to_bible(conn, project_id: str, scene: dict) -> bool:
    """把新场景追加进 bible_json.scenes（按 name 去重，重读再写以免覆盖并发编辑）。返回是否新增。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    data = json.loads(row["bible_json"])
    if scene.get("name") in {s.get("name") for s in data.get("scenes", [])}:
        return False
    data.setdefault("scenes", []).append(scene)
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(data, ensure_ascii=False), project_id))
    conn.commit()
    return True


async def _generate_and_register_scene(project_id: str, name: str, scene_canonical: str,
                                       style: str, *, ep_start: int, bible_version: int) -> str | None:
    """为新场景出一张定场图并登记到 scene_references（适用集 ep_start~ 至今）。出图失败返回 None。"""
    base_prompt = scene_ref_prompt(style, scene_canonical)
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
    critique = ""
    for attempt in range(1, 3):
        prompt = base_prompt
        if critique:
            prompt += f"。上一版一致性检查问题：{critique}。本版必须逐条修复。"
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
            if artifact["status"] == "approved":
                break
            critique = "；".join(str(item) for item in (qa.get("issues") or []))[:400]
        except Exception:  # noqa: BLE001 出图失败仍允许一次有界重试
            continue
    if not artifact or artifact["status"] != "approved":
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


async def ensure_scenes_for_storyboard(project_id: str, episode_no: int, screenplay, bible) -> dict:
    """剧本就绪后（分镜展开前）反应式维护场景库：
      ① 新场景发现：剧本地点未入库 → 评估后补进 bible.scenes + 出图；
      ② 已有场景永久状态演进：损毁/重建等 → 整包多视角原子切换。
    逐项吞错——单场景失败不阻断分镜。返回 {checked, added, evolved, errors}。"""
    scenes = list(getattr(bible, "scenes", None) or [])
    style = bible.world.visual_style_canonical
    conn = get_conn()
    proj = conn.execute("SELECT bible_version FROM projects WHERE id=?", (project_id,)).fetchone()
    bible_version = (proj["bible_version"] if proj else 0) or 0

    labels = _collect_scene_labels(screenplay)
    summary_by_heading = {
        (getattr(sc, "scene_heading", "") or "").strip(): (getattr(sc, "summary", "") or "")
        for sc in (getattr(screenplay, "scene_outline", None) or [])
    }
    unmatched = [lb for lb in labels if not match_scene_name(lb, scenes)]

    added: list[dict] = []
    evolved: list[dict] = []
    errors: list[str] = []
    for label in unmatched:
        context = f"{label}：{summary_by_heading.get(label, '')}".strip()
        try:
            verdict = await assess_new_scene(
                label, context, style=style, known_names=[s.name for s in scenes],
                ep_label=f"第 {episode_no} 集")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}：评估失败"
                          + code_ref(exc, action="assess_new_scene",
                                     context={"project_id": project_id, "scene": label, "episode_no": episode_no}))
            continue
        if not verdict["important"]:
            continue
        name = verdict["name"]
        if match_scene_name(name, scenes) or name in {s.name for s in scenes}:
            continue
        new_scene = {"name": name, "scene_canonical": verdict["scene_canonical"],
                     "location_kind": verdict["location_kind"], "ref_image_path": None,
                     "scene_prompt_override": None}
        try:
            path = await _generate_and_register_scene(
                project_id, name, verdict["scene_canonical"], style,
                ep_start=episode_no, bible_version=bible_version)
            new_scene["ref_image_path"] = path
        except Exception as exc:  # noqa: BLE001 出图失败不阻断，文字锚点仍入库
            errors.append(f"{name}@第{episode_no}集出图失败"
                          + code_ref(exc, action="generate_scene_image",
                                     context={"project_id": project_id, "scene": name, "episode_no": episode_no}))
        if _append_scene_to_bible(conn, project_id, new_scene):
            scenes.append(Scene.model_validate(new_scene))
            added.append({"name": name, "reason": verdict["reason"], "has_image": bool(new_scene["ref_image_path"])})

    # ② 已入库场景的永久状态演进（损毁/重建等）
    try:
        known_entries = _known_scene_change_entries(
            conn, project_id, episode_no, screenplay, scenes, summary_by_heading,
        )
        if known_entries:
            changes = await screen_scene_state_changes(known_entries, f"第 {episode_no} 集")
            for name, meta in changes.items():
                try:
                    result = await _refresh_scene_on_state_change(
                        project_id, name, episode_no,
                        meta["new_scene_canonical"], style, bible_version,
                        change_meta=meta,
                    )
                    if result:
                        evolved.append({"name": name, **result, "reason": meta.get("reason")})
                        _update_bible_scene_canonical(conn, project_id, name, meta["new_scene_canonical"],
                                                      result.get("image_path"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"{name}@第{episode_no}集场景演进失败"
                        + code_ref(exc, action="refresh_scene_state",
                                   context={"project_id": project_id, "scene": name, "episode_no": episode_no})
                    )
    except Exception as exc:  # noqa: BLE001 演进探测失败不阻断分镜
        errors.append("场景状态演进探测失败" + code_ref(exc, action="screen_scene_state_changes",
                                                    context={"project_id": project_id, "episode_no": episode_no}))

    return {"checked": len(unmatched), "added": added, "evolved": evolved, "errors": errors}


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
        name = match_scene_name(label, scenes)
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
    raw = await model_gateway.chat(
        [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=1600,
        call_meta={"stage": "screen_scene_state_changes"},
    )
    data = _extract_scene_change_items(raw)
    if not isinstance(data, list):
        return {}
    out: dict[str, dict] = {}
    for item in data:
        if not isinstance(item, dict) or not item.get("changed"):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        persistence = str(item.get("persistence") or "persistent").strip().lower()
        if persistence == "shot_only":
            continue
        if persistence not in {"persistent", "episode"}:
            persistence = "persistent"
        dims = item.get("change_dimensions") or []
        if isinstance(dims, str):
            dims = [dims]
        dims = [str(d).strip() for d in dims if str(d).strip()]
        canonical = (item.get("new_scene_canonical") or "").strip()
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
            "reason": (item.get("reason") or "").strip(),
            "evidence_excerpt": (item.get("evidence_excerpt") or "").strip(),
        }
    return out


def _extract_scene_change_items(raw: str) -> list[dict]:
    """同时解析规范对象和旧版根数组，避免多场景决策被静默截断。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    starts = [pos for pos in (text.find("["), text.find("{")) if pos >= 0]
    data = None
    if starts:
        try:
            data, _ = json.JSONDecoder().raw_decode(text[min(starts):])
        except (TypeError, ValueError, json.JSONDecodeError):
            data = None
    if data is None:
        data = extract_json(raw)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        items = data.get("items") or data.get("scenes")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        if "name" in data and "changed" in data:
            return [data]
    return []


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

    base_prompt = scene_ref_prompt(style, new_canonical)
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
    if float(qa.get("overall") or 0) < 0.6 and not qa.get("qa_recovered"):
        raise hiagent.ProviderError(f"场景状态演进主图 QA 未通过：{name}")

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
            conn.execute("DELETE FROM scene_references WHERE id=?", (new_scene_id,))
            conn.commit()
            raise hiagent.ProviderError(
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
