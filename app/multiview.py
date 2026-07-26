"""人物/场景多视角资产包、镜头关键帧依赖与证据化 QA 支撑。

对应 PRD：人物多视角资产与关键帧一致性QA改造方案。
不引入 LoRA/FaceID；多视角图优先服务关键帧生成与 QA，不默认全部喂 Seedance。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.db import get_conn, get_setting, new_id, now
from app.refs import _safe_name, portrait_prompt

# ---------- 视角角色常量 ----------

CHARACTER_REQUIRED_VIEWS = ("front_full", "three_quarter", "profile")
CHARACTER_OPTIONAL_VIEWS = ("back_full", "face_closeup")
SCENE_REQUIRED_VIEWS = ("establishing", "reverse_angle")
SCENE_OPTIONAL_VIEWS = ("action_zone",)

VIEW_ROLE_LABELS = {
    "front_full": "正面全身",
    "three_quarter": "3/4 面",
    "profile": "侧面",
    "back_full": "背面全身",
    "face_closeup": "面部特写",
    "establishing": "建立",
    "reverse_angle": "反打",
    "action_zone": "动作区",
}

PURPOSE_KEYFRAME_SEED = "keyframe_seed"
PURPOSE_QA_ANCHOR = "qa_anchor"
PURPOSE_VIDEO_INPUT = "video_input"

NARRATIVE_KEYFRAME_SLOT = "narrative_keyframe"
ASSET_TYPE_PLOT_KEY_FRAME = "plot_key_frame"

PACK_STATUS_GENERATING = "generating"
PACK_STATUS_QA_PENDING = "qa_pending"
PACK_STATUS_READY = "ready"
PACK_STATUS_FAILED = "failed"
PACK_STATUS_LEGACY = "legacy_partial"

CHANGE_DIM_IDENTITY = {"face", "body_identity"}
CHANGE_DIM_LOOK = {"hair", "outfit", "accessory", "injury", "age_stage"}


def bool_setting(key: str, default: bool = True) -> bool:
    raw = (get_setting(key) or str(default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def float_setting(key: str, default: float) -> float:
    try:
        return float(get_setting(key) or default)
    except (TypeError, ValueError):
        return default


def character_multiview_enabled() -> bool:
    return bool_setting("character_multiview_enabled", True)


def scene_multiview_enabled() -> bool:
    return bool_setting("scene_multiview_enabled", True)


def narrative_keyframe_required() -> bool:
    return bool_setting("narrative_keyframe_required", True)


def visual_evidence_qa_enabled() -> bool:
    return bool_setting("visual_evidence_qa_enabled", True)


def video_visual_anchor_qa_enabled() -> bool:
    return bool_setting("video_visual_anchor_qa_enabled", True)


def watermark_qa_mode() -> str:
    return (get_setting("watermark_qa_mode") or "ignore_unless_occluding").strip()


def fingerprint_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


# ---------- 视角提示词 ----------

def character_view_prompt(visual_style: str, appearance: str, view_role: str) -> str:
    framing = {
        "front_full": "正面全身立绘，中性姿态，双臂自然，全身完整可见",
        "three_quarter": "3/4 侧面半身或全身，清晰展示五官深度与发型轮廓",
        "profile": "标准左侧面半身，清晰展示鼻梁、下颌、耳部与侧面发型",
        "back_full": "背面全身，展示服装背面与发型背部轮廓",
        "face_closeup": "面部近景特写，五官清晰，发型完整入画",
    }.get(view_role, "全身立绘")
    return (
        f"{visual_style}。同一角色多视角设定图（{VIEW_ROLE_LABELS.get(view_role, view_role)}）：{appearance}。"
        f"{framing}。纯浅米色背景，单角色，禁止额外人物。"
        "同一角色、只改变观察角度，不改变脸、发型、服装和体型。"
        "禁止文字、水印、logo、多余肢体。"
    )


def scene_view_prompt(visual_style: str, scene_canonical: str, view_role: str) -> str:
    camera = {
        "establishing": "建立镜头，完整展示空间关系与主标志物",
        "reverse_angle": "与建立视角相对的反打方向，相同几何与标志物，禁止简单复制原构图",
        "action_zone": "最常发生动作的局部区域，保留可识别标志物",
    }.get(view_role, "环境定场镜头")
    return (
        f"{visual_style}。场景多视角定场图（{VIEW_ROLE_LABELS.get(view_role, view_role)}）：{scene_canonical}。"
        f"{camera}。9:16 竖屏，环境为主，画面中不出现任何人物。"
        "禁止文字、字幕、水印、logo。"
    )


# ---------- 查询 ----------

def portrait_row_for_episode(project_id: str, name: str, episode_no: int | None):
    if episode_no is None:
        return None
    try:
        return get_conn().execute(
            "SELECT * FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None


def scene_row_for_episode(project_id: str, name: str, episode_no: int | None):
    if episode_no is None:
        return None
    try:
        return get_conn().execute(
            "SELECT * FROM scene_references "
            "WHERE project_id=? AND scene_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None


def list_portrait_views(portrait_id: str, *, conn=None) -> list[dict[str, Any]]:
    db = conn or get_conn()
    try:
        rows = db.execute(
            "SELECT * FROM character_portrait_views WHERE portrait_id=? ORDER BY created_at",
            (portrait_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [dict(r) for r in rows]


def list_scene_views(scene_reference_id: str, *, conn=None) -> list[dict[str, Any]]:
    db = conn or get_conn()
    try:
        rows = db.execute(
            "SELECT * FROM scene_reference_views WHERE scene_reference_id=? ORDER BY created_at",
            (scene_reference_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [dict(r) for r in rows]


def portrait_views_for_episode(
    project_id: str, name: str, episode_no: int | None, *, ready_only: bool = False,
) -> list[dict[str, Any]]:
    row = portrait_row_for_episode(project_id, name, episode_no)
    if not row:
        return []
    views = list_portrait_views(row["id"])
    if ready_only:
        views = [v for v in views if v.get("status") == "ready" and v.get("image_path")]
    for v in views:
        v["portrait_id"] = row["id"]
        v["character_name"] = name
        v["pack_status"] = row["pack_status"] if "pack_status" in row.keys() else None
        v["appearance"] = row["appearance"]
    return views


def scene_views_for_episode(
    project_id: str, name: str, episode_no: int | None, *, ready_only: bool = False,
) -> list[dict[str, Any]]:
    row = scene_row_for_episode(project_id, name, episode_no)
    if not row:
        return []
    views = list_scene_views(row["id"])
    if ready_only:
        views = [v for v in views if v.get("status") == "ready" and v.get("image_path")]
    for v in views:
        v["scene_reference_id"] = row["id"]
        v["scene_name"] = name
        v["pack_status"] = row["pack_status"] if "pack_status" in row.keys() else None
        v["scene_canonical"] = row["scene_canonical"]
    return views


def missing_required_views(views: list[dict[str, Any]], required: tuple[str, ...]) -> list[str]:
    present = {v.get("view_role") for v in views if v.get("status") == "ready" and v.get("image_path")}
    return [role for role in required if role not in present]


def pack_is_ready(pack_status: str | None, views: list[dict[str, Any]], required: tuple[str, ...]) -> bool:
    if pack_status == PACK_STATUS_READY and not missing_required_views(views, required):
        return True
    return False


# ---------- 视角选择（镜头级） ----------

def select_character_view_roles(shot: Any, character_name: str) -> list[str]:
    """按景别/朝向为角色选择 1~2 个最相关视角。"""
    size = str(getattr(shot, "shot_size", "") or "")
    action = " ".join([
        str(getattr(shot, "action_desc", "") or ""),
        str(getattr(shot, "first_frame_desc", "") or ""),
    ])
    roles: list[str] = []
    if any(k in size for k in ("特写", "近景")) or any(k in action for k in ("脸", "眼神", "表情")):
        roles.append("three_quarter")
        if "特写" in size:
            roles.append("face_closeup")
    elif any(k in action for k in ("侧", "侧身", "侧面", "回头", "耳语")):
        roles.append("profile")
        roles.append("three_quarter")
    elif any(k in action for k in ("背", "背影", "离开", "离去")):
        roles.append("back_full")
        roles.append("front_full")
    else:
        roles.append("front_full")
        roles.append("three_quarter")
    # 去重并限制 2 个；face_closeup/back_full 仅在包内存在时由调用方过滤
    out: list[str] = []
    for role in roles:
        if role not in out:
            out.append(role)
        if len(out) >= 2:
            break
    return out or ["front_full"]


def select_scene_view_roles(shot: Any) -> list[str]:
    action = " ".join([
        str(getattr(shot, "action_desc", "") or ""),
        str(getattr(shot, "camera_move", "") or ""),
    ])
    if any(k in action for k in ("反打", "对视", "对话", "回头", "转身")):
        return ["reverse_angle", "establishing"]
    return ["establishing"]


def resolve_views_for_roles(
    views: list[dict[str, Any]], roles: list[str], *, fallback_roles: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    by_role = {v.get("view_role"): v for v in views if v.get("image_path") and Path(v["image_path"]).exists()}
    picked: list[dict[str, Any]] = []
    for role in roles:
        view = by_role.get(role)
        if view and view not in picked:
            picked.append(view)
    if not picked:
        for role in fallback_roles:
            view = by_role.get(role)
            if view:
                picked.append(view)
                break
    if not picked and views:
        for v in views:
            if v.get("image_path") and Path(v["image_path"]).exists():
                picked.append(v)
                break
    return picked


# ---------- 依赖 manifest ----------

def build_reference_manifest(
    *,
    episode_no: int,
    shot_id: str,
    characters: list[dict[str, Any]],
    scene: dict[str, Any] | None,
    keyframe_slot: str = NARRATIVE_KEYFRAME_SLOT,
) -> dict[str, Any]:
    payload = {
        "episode_no": episode_no,
        "shot_id": shot_id,
        "characters": characters,
        "scene": scene,
        "keyframe_slot": keyframe_slot,
    }
    payload["input_fingerprint"] = fingerprint_payload(payload)
    return payload


def resolve_shot_asset_dependencies(
    *,
    project_id: str,
    episode_no: int,
    shot_id: str,
    shot: Any,
    scene_name: str | None = None,
) -> dict[str, Any]:
    """解析本镜人物/场景多视角依赖，供关键帧生成与 QA 冻结。"""
    characters_out: list[dict[str, Any]] = []
    for name in list(getattr(shot, "characters", None) or []):
        views = portrait_views_for_episode(project_id, name, episode_no, ready_only=False)
        row = portrait_row_for_episode(project_id, name, episode_no)
        available = [v.get("view_role") for v in views if v.get("view_role")]
        wanted = select_character_view_roles(shot, name)
        selected = resolve_views_for_roles(
            views, wanted, fallback_roles=CHARACTER_REQUIRED_VIEWS,
        )
        characters_out.append({
            "name": name,
            "look_revision_id": row["id"] if row else None,
            "pack_status": (row["pack_status"] if row and "pack_status" in row.keys() else None),
            "selected_view_ids": [v["id"] for v in selected],
            "selected_views": [
                {
                    "id": v["id"],
                    "view_role": v.get("view_role"),
                    "image_path": v.get("image_path"),
                    "purposes": [PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
                }
                for v in selected
            ],
            "available_view_roles": available,
            "missing_required": missing_required_views(views, CHARACTER_REQUIRED_VIEWS),
        })

    scene_out = None
    sname = scene_name or getattr(shot, "scene_name", None) or ""
    if sname:
        views = scene_views_for_episode(project_id, sname, episode_no, ready_only=False)
        row = scene_row_for_episode(project_id, sname, episode_no)
        wanted = select_scene_view_roles(shot)
        selected = resolve_views_for_roles(views, wanted, fallback_roles=SCENE_REQUIRED_VIEWS)
        scene_out = {
            "name": sname,
            "scene_revision_id": row["id"] if row else None,
            "pack_status": (row["pack_status"] if row and "pack_status" in row.keys() else None),
            "selected_view_ids": [v["id"] for v in selected],
            "selected_views": [
                {
                    "id": v["id"],
                    "view_role": v.get("view_role"),
                    "image_path": v.get("image_path"),
                    "purposes": [PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
                }
                for v in selected
            ],
            "available_view_roles": [v.get("view_role") for v in views if v.get("view_role")],
            "missing_required": missing_required_views(views, SCENE_REQUIRED_VIEWS),
        }

    return build_reference_manifest(
        episode_no=episode_no,
        shot_id=shot_id,
        characters=characters_out,
        scene=scene_out,
    )


def library_anchor_assets_from_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """把依赖 manifest 展开为 QA/关键帧可用的视觉锚点列表。"""
    anchors: list[dict[str, Any]] = []
    for ch in manifest.get("characters") or []:
        for view in ch.get("selected_views") or []:
            path = view.get("image_path")
            if not path or not Path(path).exists():
                continue
            anchors.append({
                "entity_type": "character",
                "entity_name": ch.get("name"),
                "library_revision_id": ch.get("look_revision_id"),
                "library_view_id": view.get("id"),
                "view_role": view.get("view_role"),
                "image_path": path,
                "purposes": list(view.get("purposes") or [PURPOSE_QA_ANCHOR, PURPOSE_KEYFRAME_SEED]),
                "type": "character",
                "source": "asset_library",
            })
    scene = manifest.get("scene") or {}
    for view in scene.get("selected_views") or []:
        path = view.get("image_path")
        if not path or not Path(path).exists():
            continue
        anchors.append({
            "entity_type": "scene",
            "entity_name": scene.get("name"),
            "library_revision_id": scene.get("scene_revision_id"),
            "library_view_id": view.get("id"),
            "view_role": view.get("view_role"),
            "image_path": path,
            "purposes": list(view.get("purposes") or [PURPOSE_QA_ANCHOR, PURPOSE_KEYFRAME_SEED]),
            "type": "scene",
            "source": "asset_library",
        })
    return anchors


def keyframe_seed_paths(manifest: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for anchor in library_anchor_assets_from_manifest(manifest):
        if PURPOSE_KEYFRAME_SEED in (anchor.get("purposes") or []):
            paths.append(anchor["image_path"])
    return paths


# ---------- 外观变化合同 ----------

def normalize_appearance_change(item: dict[str, Any]) -> dict[str, Any]:
    """扩展 screen_appearance_changes 返回合同。"""
    dims = item.get("change_dimensions") or item.get("changeDimensions") or []
    if isinstance(dims, str):
        dims = [dims]
    dims = [str(d).strip() for d in dims if str(d).strip()]
    if not dims:
        # 从 reason/new_appearance 粗推断
        text = f"{item.get('reason') or ''} {item.get('new_appearance') or ''}"
        if any(k in text for k in ("发型", "发色", "头发", "刘海")):
            dims.append("hair")
        if any(k in text for k in ("服装", "衣服", "袍", "甲", "裙", "装")):
            dims.append("outfit")
        if any(k in text for k in ("伤", "疤", "义眼", "残")):
            dims.append("injury")
        if any(k in text for k in ("老", "幼", "成年", "少年")):
            dims.append("age_stage")
        if not dims:
            dims = ["outfit"]
    persistence = str(item.get("persistence") or "persistent").strip().lower()
    if persistence not in {"persistent", "episode", "shot_only"}:
        persistence = "persistent"
    # 默认禁止 face/body_identity，除非原文明确
    reason = str(item.get("reason") or "")
    evidence = str(item.get("evidence_excerpt") or item.get("evidence") or "")
    identity_ok = any(k in (reason + evidence) for k in ("变身", "换脸", "容貌重塑", "年龄跃迁", "重生", "异化"))
    cleaned_dims = []
    for d in dims:
        if d in CHANGE_DIM_IDENTITY and not identity_ok:
            continue
        cleaned_dims.append(d)
    if not cleaned_dims:
        cleaned_dims = [d for d in dims if d not in CHANGE_DIM_IDENTITY] or ["outfit"]
    return {
        "character": item.get("character") or item.get("name") or "",
        "changed": bool(item.get("changed", True)),
        "new_appearance": (item.get("new_appearance") or "").strip(),
        "change_dimensions": cleaned_dims,
        "persistence": persistence,
        "reason": reason.strip(),
        "evidence_excerpt": evidence.strip()[:240],
    }


# ---------- 生成与落盘 ----------

def _view_path(project_id: str, kind: str, name: str, view_role: str, ep_start: int) -> str:
    root = config.PROJECTS_DIR / project_id / ("refs" if kind == "character" else "scene_refs") / "views"
    root.mkdir(parents=True, exist_ok=True)
    return str(root / f"{_safe_name(name)}__{view_role}__ep{ep_start}__{new_id('view')}.jpg")


async def _save_image_item(item: dict, dest: str) -> None:
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        import base64
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


async def _generate_image(prompt: str, *, seed_inputs: list[str] | None = None, call_meta: dict | None = None) -> dict:
    try:
        return await hiagent.generate_image(
            prompt, size=config.REF_IMAGE_SIZE, image_inputs=seed_inputs or None, call_meta=call_meta,
        )
    except hiagent.ProviderError:
        if not seed_inputs:
            raise
        return await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE, call_meta=call_meta)


def _upsert_character_view(
    conn, *, portrait_id: str, view_role: str, framing: str | None, image_path: str,
    prompt: str, qa: dict | None, artifact_id: str | None, base_view_id: str | None,
    status: str, fingerprint: str | None,
) -> str:
    existing = conn.execute(
        "SELECT id FROM character_portrait_views WHERE portrait_id=? AND view_role=?",
        (portrait_id, view_role),
    ).fetchone()
    stamp = now()
    qa_json = json.dumps(qa, ensure_ascii=False) if qa else None
    if existing:
        conn.execute(
            """UPDATE character_portrait_views SET framing=?, image_path=?, prompt=?, qa_json=?,
                      artifact_id=?, base_view_id=?, status=?, selected=1, input_fingerprint=?
               WHERE id=?""",
            (framing, image_path, prompt, qa_json, artifact_id, base_view_id, status, fingerprint, existing["id"]),
        )
        return existing["id"]
    view_id = new_id("pview")
    conn.execute(
        """INSERT INTO character_portrait_views(
               id, portrait_id, view_role, framing, image_path, prompt, qa_json,
               artifact_id, base_view_id, status, selected, input_fingerprint, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (view_id, portrait_id, view_role, framing, image_path, prompt, qa_json,
         artifact_id, base_view_id, status, 1, fingerprint, stamp),
    )
    return view_id


def _upsert_scene_view(
    conn, *, scene_reference_id: str, view_role: str, camera_axis: str | None, image_path: str,
    prompt: str, qa: dict | None, artifact_id: str | None, base_view_id: str | None,
    status: str, fingerprint: str | None,
) -> str:
    existing = conn.execute(
        "SELECT id FROM scene_reference_views WHERE scene_reference_id=? AND view_role=?",
        (scene_reference_id, view_role),
    ).fetchone()
    stamp = now()
    qa_json = json.dumps(qa, ensure_ascii=False) if qa else None
    if existing:
        conn.execute(
            """UPDATE scene_reference_views SET camera_axis=?, image_path=?, prompt=?, qa_json=?,
                      artifact_id=?, base_view_id=?, status=?, selected=1, input_fingerprint=?
               WHERE id=?""",
            (camera_axis, image_path, prompt, qa_json, artifact_id, base_view_id, status, fingerprint, existing["id"]),
        )
        return existing["id"]
    view_id = new_id("sview")
    conn.execute(
        """INSERT INTO scene_reference_views(
               id, scene_reference_id, view_role, camera_axis, image_path, prompt, qa_json,
               artifact_id, base_view_id, status, selected, input_fingerprint, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (view_id, scene_reference_id, view_role, camera_axis, image_path, prompt, qa_json,
         artifact_id, base_view_id, status, 1, fingerprint, stamp),
    )
    return view_id


def _set_portrait_pack_fields(conn, portrait_id: str, **fields: Any) -> None:
    cols = []
    vals: list[Any] = []
    for key, value in fields.items():
        cols.append(f"{key}=?")
        vals.append(value)
    if not cols:
        return
    vals.append(portrait_id)
    try:
        conn.execute(f"UPDATE character_portraits SET {', '.join(cols)} WHERE id=?", vals)
    except Exception:  # noqa: BLE001
        pass


def _set_scene_pack_fields(conn, scene_id: str, **fields: Any) -> None:
    cols = []
    vals: list[Any] = []
    for key, value in fields.items():
        cols.append(f"{key}=?")
        vals.append(value)
    if not cols:
        return
    vals.append(scene_id)
    try:
        conn.execute(f"UPDATE scene_references SET {', '.join(cols)} WHERE id=?", vals)
    except Exception:  # noqa: BLE001
        pass


async def review_character_view(image_path: str, appearance: str, view_role: str) -> dict[str, Any]:
    from app.stages import review_portrait_image
    try:
        qa = await review_portrait_image(hiagent.encode_image_file(image_path), appearance)
        qa["view_role"] = view_role
        return qa
    except Exception as exc:  # noqa: BLE001
        return {
            "overall": None,
            "status": "unverified",
            "issues": [f"视角 QA 未完成：{type(exc).__name__}"],
            "qa_recovered": True,
            "view_role": view_role,
        }


async def review_scene_view(image_path: str, scene_canonical: str, view_role: str) -> dict[str, Any]:
    from app.stages import review_scene_image
    try:
        qa = await review_scene_image(
            hiagent.encode_image_file(image_path), scene_canonical, "场景定场", [], kind="head",
        )
        qa["view_role"] = view_role
        return qa
    except Exception as exc:  # noqa: BLE001
        return {
            "overall": None,
            "status": "unverified",
            "issues": [f"场景视角 QA 未完成：{type(exc).__name__}"],
            "qa_recovered": True,
            "view_role": view_role,
        }


async def review_character_pack_consistency(views: list[dict[str, Any]], appearance: str) -> dict[str, Any]:
    """整包跨视角 QA：同一角色脸/发型/服装一致。"""
    frames: list[str] = []
    roles: list[str] = []
    for v in views:
        path = v.get("image_path")
        if not path or not Path(path).exists():
            continue
        try:
            frames.append(hiagent.encode_image_file(path))
            roles.append(str(v.get("view_role") or ""))
        except OSError:
            continue
    if len(frames) < 2:
        return {"overall": 1.0 if frames else None, "status": "ready" if frames else "unverified", "issues": []}
    expectation = (
        "你是角色多视角一致性评审。以下图片是同一角色不同观察角度的设定图，顺序："
        + ", ".join(f"{i+1}:{r}" for i, r in enumerate(roles))
        + f"。外观锚点：{appearance}。"
        "检查：同一角色脸部特征、发型、服装、体型在各视角一致，只允许角度不同。"
        '输出 JSON：{"overall":0~1,"face_consistency":0~1,"outfit_consistency":0~1,'
        '"hair_consistency":0~1,"issues":[str],"hard_failures":[str]}'
    )
    try:
        raw = await hiagent.vlm_check(frames, expectation, call_meta={"initiator_label": "人物多视角整包QA"})
        from app.schemas import extract_json
        data = extract_json(raw)
        for key in ("overall", "face_consistency", "outfit_consistency", "hair_consistency"):
            try:
                data[key] = max(0.0, min(1.0, float(data.get(key, 0))))
            except (TypeError, ValueError):
                data[key] = 0.0
        data["status"] = "ready" if float(data.get("overall") or 0) >= 0.75 and not data.get("hard_failures") else "failed"
        if not isinstance(data.get("issues"), list):
            data["issues"] = []
        return data
    except Exception as exc:  # noqa: BLE001
        return {
            "overall": None,
            "status": "unverified",
            "issues": [f"整包 QA 未完成：{type(exc).__name__}"],
            "qa_recovered": True,
        }


async def review_scene_pack_consistency(views: list[dict[str, Any]], scene_canonical: str) -> dict[str, Any]:
    frames: list[str] = []
    roles: list[str] = []
    for v in views:
        path = v.get("image_path")
        if not path or not Path(path).exists():
            continue
        try:
            frames.append(hiagent.encode_image_file(path))
            roles.append(str(v.get("view_role") or ""))
        except OSError:
            continue
    if len(frames) < 2:
        return {"overall": 1.0 if frames else None, "status": "ready" if frames else "unverified", "issues": []}
    expectation = (
        "你是场景多视角一致性评审。以下是同一场景不同机位的无人定场图，顺序："
        + ", ".join(f"{i+1}:{r}" for i, r in enumerate(roles))
        + f"。场景锚点：{scene_canonical}。"
        "检查门窗、主陈设、标志物、光线方向在各视角不自相矛盾；允许构图不同。"
        '输出 JSON：{"overall":0~1,"geometry_consistency":0~1,"landmark_consistency":0~1,'
        '"lighting_consistency":0~1,"issues":[str],"hard_failures":[str]}'
    )
    try:
        raw = await hiagent.vlm_check(frames, expectation, call_meta={"initiator_label": "场景多视角整包QA"})
        from app.schemas import extract_json
        data = extract_json(raw)
        for key in ("overall", "geometry_consistency", "landmark_consistency", "lighting_consistency"):
            try:
                data[key] = max(0.0, min(1.0, float(data.get(key, 0))))
            except (TypeError, ValueError):
                data[key] = 0.0
        data["status"] = "ready" if float(data.get("overall") or 0) >= 0.75 and not data.get("hard_failures") else "failed"
        if not isinstance(data.get("issues"), list):
            data["issues"] = []
        return data
    except Exception as exc:  # noqa: BLE001
        return {
            "overall": None,
            "status": "unverified",
            "issues": [f"场景整包 QA 未完成：{type(exc).__name__}"],
            "qa_recovered": True,
        }


def _view_passed(qa: dict[str, Any] | None) -> bool:
    if not qa:
        return False
    if qa.get("status") == "unverified" or qa.get("overall") is None:
        return False
    if qa.get("qa_recovered"):
        return False
    try:
        return float(qa.get("overall") or 0) >= 0.6
    except (TypeError, ValueError):
        return False


async def ensure_character_multiview_pack(
    *,
    project_id: str,
    portrait_id: str,
    character_name: str,
    appearance: str,
    visual_style: str,
    ep_start: int,
    base_portrait_id: str | None = None,
    optional_views: list[str] | None = None,
) -> dict[str, Any]:
    """生成/补齐人物必需多视角包；失败时 pack_status=failed，不半包生效。"""
    if not character_multiview_enabled():
        return {"status": "disabled", "portrait_id": portrait_id}
    conn = get_conn()
    _set_portrait_pack_fields(conn, portrait_id, pack_status=PACK_STATUS_GENERATING)
    conn.commit()

    existing_views = {v["view_role"]: v for v in list_portrait_views(portrait_id, conn=conn)}
    base_views: dict[str, dict] = {}
    if base_portrait_id:
        base_views = {v["view_role"]: v for v in list_portrait_views(base_portrait_id, conn=conn)}

    roles = list(CHARACTER_REQUIRED_VIEWS) + [r for r in (optional_views or []) if r in CHARACTER_OPTIONAL_VIEWS]
    # 1) front_full 优先
    front = existing_views.get("front_full")
    if not front or not front.get("image_path") or not Path(front["image_path"]).exists():
        # 若父记录已有 image_path，直接登记为 front_full
        parent = conn.execute("SELECT * FROM character_portraits WHERE id=?", (portrait_id,)).fetchone()
        if parent and parent["image_path"] and Path(parent["image_path"]).exists():
            qa = await review_character_view(parent["image_path"], appearance, "front_full")
            status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
            _upsert_character_view(
                conn, portrait_id=portrait_id, view_role="front_full", framing="full_body",
                image_path=parent["image_path"], prompt=parent["prompt"] or "",
                qa=qa, artifact_id=parent["artifact_id"] if "artifact_id" in parent.keys() else None,
                base_view_id=(base_views.get("front_full") or {}).get("id"),
                status=status, fingerprint=None,
            )
            conn.commit()
        else:
            prompt = character_view_prompt(visual_style, appearance, "front_full")
            seed = None
            base_front = base_views.get("front_full") or {}
            if base_front.get("image_path") and Path(base_front["image_path"]).exists():
                seed = [hiagent.data_url_from_file(base_front["image_path"])]
            path = _view_path(project_id, "character", character_name, "front_full", ep_start)
            item = await _generate_image(
                prompt, seed_inputs=seed,
                call_meta={"asset_kind": "character_view", "view_role": "front_full", "character_name": character_name},
            )
            await _save_image_item(item, path)
            qa = await review_character_view(path, appearance, "front_full")
            status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
            _upsert_character_view(
                conn, portrait_id=portrait_id, view_role="front_full", framing="full_body",
                image_path=path, prompt=prompt, qa=qa, artifact_id=None,
                base_view_id=base_front.get("id"), status=status, fingerprint=None,
            )
            # 镜像到父表 image_path
            conn.execute("UPDATE character_portraits SET image_path=? WHERE id=?", (path, portrait_id))
            conn.commit()
            if status != "ready":
                _set_portrait_pack_fields(conn, portrait_id, pack_status=PACK_STATUS_FAILED,
                                         group_qa_json=json.dumps(qa, ensure_ascii=False))
                conn.commit()
                return {"status": "failed", "portrait_id": portrait_id, "failed_view": "front_full", "qa": qa}

    existing_views = {v["view_role"]: v for v in list_portrait_views(portrait_id, conn=conn)}
    front = existing_views.get("front_full") or {}
    if not _view_passed(json.loads(front["qa_json"]) if front.get("qa_json") else front.get("qa") or {"overall": 1.0}):
        # 若已有 ready 状态则放行
        if front.get("status") != "ready":
            _set_portrait_pack_fields(conn, portrait_id, pack_status=PACK_STATUS_FAILED)
            conn.commit()
            return {"status": "failed", "portrait_id": portrait_id, "failed_view": "front_full"}

    front_seed = []
    if front.get("image_path") and Path(front["image_path"]).exists():
        front_seed = [hiagent.data_url_from_file(front["image_path"])]

    async def _gen_side(view_role: str) -> dict[str, Any]:
        cur = existing_views.get(view_role)
        if cur and cur.get("status") == "ready" and cur.get("image_path") and Path(cur["image_path"]).exists():
            return {"view_role": view_role, "status": "ready", "id": cur["id"]}
        prompt = character_view_prompt(visual_style, appearance, view_role)
        seeds = list(front_seed)
        base = base_views.get(view_role) or {}
        if base.get("image_path") and Path(base["image_path"]).exists():
            seeds.append(hiagent.data_url_from_file(base["image_path"]))
        path = _view_path(project_id, "character", character_name, view_role, ep_start)
        item = await _generate_image(
            prompt, seed_inputs=seeds or None,
            call_meta={"asset_kind": "character_view", "view_role": view_role, "character_name": character_name},
        )
        await _save_image_item(item, path)
        qa = await review_character_view(path, appearance, view_role)
        status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
        view_id = _upsert_character_view(
            conn, portrait_id=portrait_id, view_role=view_role,
            framing="half_or_full" if view_role != "face_closeup" else "closeup",
            image_path=path, prompt=prompt, qa=qa, artifact_id=None,
            base_view_id=base.get("id"), status=status, fingerprint=None,
        )
        conn.commit()
        return {"view_role": view_role, "status": status, "id": view_id, "qa": qa}

    side_roles = [r for r in roles if r != "front_full"]
    side_results = await asyncio.gather(*[_gen_side(r) for r in side_roles])
    failed = [r for r in side_results if r.get("status") != "ready"]
    if failed:
        _set_portrait_pack_fields(
            conn, portrait_id, pack_status=PACK_STATUS_FAILED,
            group_qa_json=json.dumps({"failed_views": failed}, ensure_ascii=False),
        )
        conn.commit()
        return {"status": "failed", "portrait_id": portrait_id, "failed_views": failed}

    views = list_portrait_views(portrait_id, conn=conn)
    required_views = [v for v in views if v.get("view_role") in CHARACTER_REQUIRED_VIEWS]
    group_qa = await review_character_pack_consistency(required_views, appearance)
    if group_qa.get("status") != "ready":
        _set_portrait_pack_fields(
            conn, portrait_id, pack_status=PACK_STATUS_FAILED,
            group_qa_json=json.dumps(group_qa, ensure_ascii=False),
        )
        conn.commit()
        return {"status": "failed", "portrait_id": portrait_id, "group_qa": group_qa}

    # 镜像 front_full 到父表
    front_ready = next((v for v in views if v.get("view_role") == "front_full"), None)
    fields = {
        "pack_status": PACK_STATUS_READY,
        "group_qa_json": json.dumps(group_qa, ensure_ascii=False),
    }
    if front_ready and front_ready.get("image_path"):
        fields["image_path"] = front_ready["image_path"]
    _set_portrait_pack_fields(conn, portrait_id, **fields)
    conn.commit()
    return {"status": "ready", "portrait_id": portrait_id, "group_qa": group_qa, "views": views}


async def ensure_scene_multiview_pack(
    *,
    project_id: str,
    scene_reference_id: str,
    scene_name: str,
    scene_canonical: str,
    visual_style: str,
    ep_start: int,
    base_scene_id: str | None = None,
) -> dict[str, Any]:
    if not scene_multiview_enabled():
        return {"status": "disabled", "scene_reference_id": scene_reference_id}
    conn = get_conn()
    _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_GENERATING)
    conn.commit()

    existing_views = {v["view_role"]: v for v in list_scene_views(scene_reference_id, conn=conn)}
    base_views = {v["view_role"]: v for v in list_scene_views(base_scene_id, conn=conn)} if base_scene_id else {}

    # establishing
    est = existing_views.get("establishing")
    if not est or not est.get("image_path") or not Path(est["image_path"]).exists():
        parent = conn.execute("SELECT * FROM scene_references WHERE id=?", (scene_reference_id,)).fetchone()
        if parent and parent["image_path"] and Path(parent["image_path"]).exists():
            qa = await review_scene_view(parent["image_path"], scene_canonical, "establishing")
            status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
            _upsert_scene_view(
                conn, scene_reference_id=scene_reference_id, view_role="establishing",
                camera_axis="establishing", image_path=parent["image_path"],
                prompt=parent["prompt"] or "", qa=qa,
                artifact_id=parent["artifact_id"] if "artifact_id" in parent.keys() else None,
                base_view_id=(base_views.get("establishing") or {}).get("id"),
                status=status, fingerprint=None,
            )
            conn.commit()
        else:
            prompt = scene_view_prompt(visual_style, scene_canonical, "establishing")
            path = _view_path(project_id, "scene", scene_name, "establishing", ep_start)
            item = await _generate_image(
                prompt, call_meta={"asset_kind": "scene_view", "view_role": "establishing", "scene_name": scene_name},
            )
            await _save_image_item(item, path)
            qa = await review_scene_view(path, scene_canonical, "establishing")
            status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
            _upsert_scene_view(
                conn, scene_reference_id=scene_reference_id, view_role="establishing",
                camera_axis="establishing", image_path=path, prompt=prompt, qa=qa, artifact_id=None,
                base_view_id=(base_views.get("establishing") or {}).get("id"),
                status=status, fingerprint=None,
            )
            conn.execute("UPDATE scene_references SET image_path=? WHERE id=?", (path, scene_reference_id))
            conn.commit()
            if status != "ready":
                _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_FAILED,
                                      group_qa_json=json.dumps(qa, ensure_ascii=False))
                conn.commit()
                return {"status": "failed", "scene_reference_id": scene_reference_id, "failed_view": "establishing"}

    existing_views = {v["view_role"]: v for v in list_scene_views(scene_reference_id, conn=conn)}
    est = existing_views.get("establishing") or {}
    if est.get("status") != "ready":
        _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_FAILED)
        conn.commit()
        return {"status": "failed", "scene_reference_id": scene_reference_id, "failed_view": "establishing"}

    # reverse_angle
    rev = existing_views.get("reverse_angle")
    if not rev or rev.get("status") != "ready" or not rev.get("image_path"):
        prompt = scene_view_prompt(visual_style, scene_canonical, "reverse_angle")
        seeds = []
        if est.get("image_path") and Path(est["image_path"]).exists():
            seeds.append(hiagent.data_url_from_file(est["image_path"]))
        path = _view_path(project_id, "scene", scene_name, "reverse_angle", ep_start)
        item = await _generate_image(
            prompt, seed_inputs=seeds or None,
            call_meta={"asset_kind": "scene_view", "view_role": "reverse_angle", "scene_name": scene_name},
        )
        await _save_image_item(item, path)
        qa = await review_scene_view(path, scene_canonical, "reverse_angle")
        status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
        _upsert_scene_view(
            conn, scene_reference_id=scene_reference_id, view_role="reverse_angle",
            camera_axis="reverse", image_path=path, prompt=prompt, qa=qa, artifact_id=None,
            base_view_id=(base_views.get("reverse_angle") or {}).get("id"),
            status=status, fingerprint=None,
        )
        conn.commit()
        if status != "ready":
            _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_FAILED,
                                  group_qa_json=json.dumps(qa, ensure_ascii=False))
            conn.commit()
            return {"status": "failed", "scene_reference_id": scene_reference_id, "failed_view": "reverse_angle"}

    views = list_scene_views(scene_reference_id, conn=conn)
    required_views = [v for v in views if v.get("view_role") in SCENE_REQUIRED_VIEWS]
    group_qa = await review_scene_pack_consistency(required_views, scene_canonical)
    if group_qa.get("status") != "ready":
        _set_scene_pack_fields(
            conn, scene_reference_id, pack_status=PACK_STATUS_FAILED,
            group_qa_json=json.dumps(group_qa, ensure_ascii=False),
        )
        conn.commit()
        return {"status": "failed", "scene_reference_id": scene_reference_id, "group_qa": group_qa}

    est_ready = next((v for v in views if v.get("view_role") == "establishing"), None)
    fields = {
        "pack_status": PACK_STATUS_READY,
        "group_qa_json": json.dumps(group_qa, ensure_ascii=False),
    }
    if est_ready and est_ready.get("image_path"):
        fields["image_path"] = est_ready["image_path"]
    _set_scene_pack_fields(conn, scene_reference_id, **fields)
    conn.commit()
    return {"status": "ready", "scene_reference_id": scene_reference_id, "group_qa": group_qa, "views": views}


async def complete_legacy_character_pack(
    project_id: str, character_name: str, episode_no: int, visual_style: str,
) -> dict[str, Any] | None:
    """对本集涉及的 legacy_partial 人物补齐缺失视角。"""
    row = portrait_row_for_episode(project_id, character_name, episode_no)
    if not row:
        return None
    pack_status = row["pack_status"] if "pack_status" in row.keys() else None
    views = list_portrait_views(row["id"])
    missing = missing_required_views(views, CHARACTER_REQUIRED_VIEWS)
    if pack_status == PACK_STATUS_READY and not missing:
        return {"status": "ready", "portrait_id": row["id"]}
    return await ensure_character_multiview_pack(
        project_id=project_id,
        portrait_id=row["id"],
        character_name=character_name,
        appearance=row["appearance"] or "",
        visual_style=visual_style,
        ep_start=row["ep_start"],
        base_portrait_id=row["base_portrait_id"],
    )


async def complete_legacy_scene_pack(
    project_id: str, scene_name: str, episode_no: int, visual_style: str,
) -> dict[str, Any] | None:
    row = scene_row_for_episode(project_id, scene_name, episode_no)
    if not row:
        return None
    pack_status = row["pack_status"] if "pack_status" in row.keys() else None
    views = list_scene_views(row["id"])
    missing = missing_required_views(views, SCENE_REQUIRED_VIEWS)
    if pack_status == PACK_STATUS_READY and not missing:
        return {"status": "ready", "scene_reference_id": row["id"]}
    return await ensure_scene_multiview_pack(
        project_id=project_id,
        scene_reference_id=row["id"],
        scene_name=scene_name,
        scene_canonical=row["scene_canonical"] or "",
        visual_style=visual_style,
        ep_start=row["ep_start"],
        base_scene_id=row["base_scene_id"],
    )


# ---------- 证据化关键帧 QA ----------

KEYFRAME_SCORE_WEIGHTS = {
    "action_match": 0.25,
    "body_proportion": 0.20,
    "face_identity": 0.20,
    "outfit_match": 0.15,
    "hair_match": 0.10,
    "scene_match": 0.10,
}

KEYFRAME_HARD_FAILURES = {
    "wrong_identity", "duplicate_character", "severe_anatomy",
    "wrong_outfit", "action_missing", "subject_occlusion",
}


def compute_weighted_overall(scores: dict[str, Any], weights: dict[str, float]) -> float | None:
    """只对适用维度（非 None / 非 N/A）归一化。"""
    usable: list[tuple[float, float]] = []
    for key, weight in weights.items():
        val = scores.get(key)
        if val is None:
            continue
        if isinstance(val, str) and val.upper() in {"N/A", "NA", "NONE"}:
            continue
        try:
            usable.append((float(val), float(weight)))
        except (TypeError, ValueError):
            continue
    if not usable:
        return None
    total_w = sum(w for _, w in usable)
    if total_w <= 0:
        return None
    return round(sum(score * (w / total_w) for score, w in usable), 3)


def keyframe_gate_passed(qa: dict[str, Any]) -> bool:
    if qa.get("status") == "unverified" or qa.get("overall") is None:
        return False
    if qa.get("qa_recovered"):
        return False
    hard = {str(x).strip() for x in (qa.get("hard_failures") or []) if str(x).strip()}
    if hard & KEYFRAME_HARD_FAILURES:
        return False
    overall_thr = float_setting("keyframe_qa_overall_threshold", 0.80)
    action_thr = float_setting("keyframe_qa_action_threshold", 0.70)
    body_thr = float_setting("keyframe_qa_body_threshold", 0.72)
    id_thr = float_setting("keyframe_qa_identity_threshold", 0.75)
    try:
        if float(qa.get("overall") or 0) < overall_thr:
            return False
        if float(qa.get("action_match") or 0) < action_thr:
            return False
        if float(qa.get("body_proportion") or 0) < body_thr:
            return False
        for key in ("face_identity", "outfit_match", "hair_match"):
            val = qa.get(key)
            if val is None or (isinstance(val, str) and val.upper() in {"N/A", "NA"}):
                continue
            if float(val) < id_thr:
                return False
    except (TypeError, ValueError):
        return False
    return True


def build_image_manifest(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, entry in enumerate(entries, 1):
        item = {"index": i, "role": entry.get("role")}
        if entry.get("entity"):
            item["entity"] = entry["entity"]
        if entry.get("view"):
            item["view"] = entry["view"]
        out.append(item)
    return out


async def review_keyframe_with_evidence(
    candidate_b64: str,
    *,
    shot: Any,
    bible: Any,
    visual_anchors: list[dict[str, Any]],
    ref_type: str = ASSET_TYPE_PLOT_KEY_FRAME,
) -> dict[str, Any]:
    """关键帧证据化 QA：候选图 + 人物/场景真值图对照。"""
    if not visual_evidence_qa_enabled():
        # 回退：无证据时仍打分，但不伪装满分
        from app.video_modes import review_reference_image
        qa = await review_reference_image(candidate_b64, shot=shot, bible=bible, ref_type=ref_type)
        qa.setdefault("status", "scored")
        return qa

    frames = [candidate_b64]
    manifest_entries = [{"role": "candidate_keyframe"}]
    for anchor in visual_anchors:
        path = anchor.get("image_path") or anchor.get("path")
        if not path or not Path(path).exists():
            continue
        try:
            frames.append(hiagent.encode_image_file(path))
        except OSError:
            continue
        role = "character_anchor" if anchor.get("entity_type") == "character" or anchor.get("type") == "character" else "scene_anchor"
        if anchor.get("type") == "previous_shot_frame":
            role = "continuity_anchor"
        manifest_entries.append({
            "role": role,
            "entity": anchor.get("entity_name") or anchor.get("name"),
            "view": anchor.get("view_role"),
        })

    image_manifest = build_image_manifest(manifest_entries)
    by_name = {c.name: c for c in getattr(bible, "characters", []) or []}
    anchors_txt = []
    for name in getattr(shot, "characters", []) or []:
        if name in by_name:
            anchors_txt.append(f"{name}: {by_name[name].appearance_canonical}")

    wm_mode = watermark_qa_mode()
    wm_note = (
        "水印/Logo 本身不作为评分主项，也不单独构成 hard failure；"
        "仅当遮挡脸、发型、衣服、手部动作接触区或关键场景标志物时，"
        "在对应主维度扣分，并标记 hard_failures 含 subject_occlusion。"
        if wm_mode == "ignore_unless_occluding"
        else "检查画面干净度，水印可计入问题。"
    )
    expectation = {
        "task": "Evidence-based narrative keyframe QA for Seedance.",
        "image_manifest": image_manifest,
        "shot": {
            "scene": getattr(shot, "scene_setting", ""),
            "action": getattr(shot, "action_desc", ""),
            "first_frame": getattr(shot, "first_frame_desc", ""),
            "characters": anchors_txt,
            "style": getattr(getattr(bible, "world", None), "visual_style_canonical", ""),
        },
        "dimensions": {
            "action_match": "姿态、朝向、手部/道具接触、人物间空间互动",
            "body_proportion": "头身比、肢体长度、身体完整性、无异常融合",
            "face_identity": "与人物锚点脸部特征一致；脸不可见时返回 null 或 N/A",
            "outfit_match": "款式颜色层次配饰与本集造型一致",
            "hair_match": "发型长度发色刘海轮廓一致",
            "scene_match": "几何标志物机位方向状态光线合理",
        },
        "watermark_policy": wm_note,
        "hard_failures_enum": sorted(KEYFRAME_HARD_FAILURES),
        "output_schema": {
            "action_match": 0.0,
            "body_proportion": 0.0,
            "face_identity": 0.0,
            "outfit_match": 0.0,
            "hair_match": 0.0,
            "scene_match": 0.0,
            "overall": 0.0,
            "hard_failures": [],
            "issues": [],
            "status": "scored",
        },
    }
    try:
        raw = await hiagent.vlm_check(
            frames, json.dumps(expectation, ensure_ascii=False),
            call_meta={
                "initiator_label": "关键帧证据化质检",
                "shot_no": getattr(shot, "shot_no", None),
                "anchor_count": len(frames) - 1,
            },
        )
        from app.schemas import extract_json
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "unverified",
            "overall": None,
            "action_match": None,
            "body_proportion": None,
            "face_identity": None,
            "outfit_match": None,
            "hair_match": None,
            "scene_match": None,
            "hard_failures": [],
            "issues": [f"关键帧 QA 未完成：{type(exc).__name__}"],
            "qa_recovered": True,
            "image_manifest": image_manifest,
        }

    score_keys = list(KEYFRAME_SCORE_WEIGHTS.keys())
    missing_required = False
    for key in score_keys:
        val = data.get(key)
        if val is None or (isinstance(val, str) and val.upper() in {"N/A", "NA"}):
            # face 等允许 N/A
            if key == "face_identity":
                data[key] = None
                continue
            # 其它缺失 → unverified
            if key in {"action_match", "body_proportion"} and val is None:
                missing_required = True
            data[key] = None
            continue
        try:
            data[key] = max(0.0, min(1.0, float(val)))
        except (TypeError, ValueError):
            data[key] = None
            if key in {"action_match", "body_proportion"}:
                missing_required = True

    if missing_required:
        data["status"] = "unverified"
        data["overall"] = None
        data["qa_recovered"] = True
        data["issues"] = list(data.get("issues") or []) + ["缺少必需评分数"]
        data["image_manifest"] = image_manifest
        return data

    overall = compute_weighted_overall(data, KEYFRAME_SCORE_WEIGHTS)
    data["overall"] = overall
    if not isinstance(data.get("issues"), list):
        data["issues"] = [str(data.get("issues"))] if data.get("issues") else []
    if not isinstance(data.get("hard_failures"), list):
        data["hard_failures"] = []
    # 水印降级：从 hard_failures 移除纯水印项
    if watermark_qa_mode() == "ignore_unless_occluding":
        cleaned = []
        for item in data["hard_failures"]:
            s = str(item).lower()
            if "watermark" in s or "水印" in s or s == "logo":
                if "occlusion" in s or "遮挡" in s or "subject_occlusion" in s:
                    cleaned.append("subject_occlusion")
                continue
            cleaned.append(str(item))
        data["hard_failures"] = cleaned
    data["status"] = "scored"
    data["image_manifest"] = image_manifest
    data["passed"] = keyframe_gate_passed(data)
    return data


# ---------- Seedance 确定性装箱 ----------

def purpose_list(ref: dict[str, Any]) -> list[str]:
    raw = ref.get("purposes") or ref.get("purposes_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, list) and raw:
        return [str(p) for p in raw]
    # 兼容旧数据
    if ref.get("selectedForSeedance") and not ref.get("deleted"):
        return [PURPOSE_VIDEO_INPUT]
    return []


def ref_pack_priority(ref: dict[str, Any]) -> tuple[int, float]:
    """必需用途优先；同类内按分数。数值越小越优先。"""
    rtype = str(ref.get("type") or "")
    purposes = set(purpose_list(ref))
    slot = str(ref.get("slot_key") or "")
    if rtype == "previous_shot_frame" or "previous_shot" in str(ref.get("source") or ""):
        tier = 0
    elif rtype == ASSET_TYPE_PLOT_KEY_FRAME or slot == NARRATIVE_KEYFRAME_SLOT or "keyframe" in purposes:
        tier = 1
    elif rtype == "scene":
        tier = 2
    elif rtype == "character":
        tier = 3
    elif rtype in {"prop", "style"}:
        tier = 4
    else:
        tier = 5
    try:
        score = float(ref.get("qualityScore") if ref.get("qualityScore") is not None
                      else (ref.get("qa") or {}).get("overall") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return (tier, -score)


def pack_references_by_purpose(
    refs: list[dict[str, Any]],
    *,
    max_images: int,
    continuity_required: bool = False,
    char_limit: int = 1,
) -> list[dict[str, Any]]:
    """必需用途优先装箱：关键帧不会被高分定妆照挤掉。"""
    usable = []
    for r in refs:
        if r.get("deleted"):
            continue
        purposes = purpose_list(r)
        # selectedForSeedance 仍代表 video_input 意愿
        if PURPOSE_VIDEO_INPUT in purposes or r.get("selectedForSeedance"):
            usable.append(r)
    if not usable:
        return []

    ordered = sorted(usable, key=ref_pack_priority)
    packed: list[dict[str, Any]] = []
    char_count = 0

    def _is_char_bearing(ref: dict[str, Any]) -> bool:
        rtype = str(ref.get("type") or "")
        if rtype in {"character", ASSET_TYPE_PLOT_KEY_FRAME, "previous_shot_frame"}:
            return True
        return bool(ref.get("relatedCharacterIds"))

    # 先确保必需项
    required_types = []
    if continuity_required:
        required_types.append("previous_shot_frame")
    required_types.append(ASSET_TYPE_PLOT_KEY_FRAME)

    for need in required_types:
        for ref in ordered:
            if ref in packed:
                continue
            if str(ref.get("type") or "") != need and not (
                need == ASSET_TYPE_PLOT_KEY_FRAME and str(ref.get("slot_key") or "") == NARRATIVE_KEYFRAME_SLOT
            ):
                continue
            if len(packed) >= max_images:
                break
            if _is_char_bearing(ref) and need != "previous_shot_frame":
                # 关键帧本身计人物图，但必需，不受 char_limit 拦截
                packed.append(ref)
                if need != ASSET_TYPE_PLOT_KEY_FRAME:
                    char_count += 1
                else:
                    char_count += 1
            else:
                packed.append(ref)
            break

    for ref in ordered:
        if ref in packed:
            continue
        if len(packed) >= max_images:
            break
        if _is_char_bearing(ref):
            # 额外人物图受上限；关键帧已计入
            if char_count >= char_limit and str(ref.get("type") or "") != ASSET_TYPE_PLOT_KEY_FRAME:
                # 若关键帧已占 1 个人物名额，额外 character 图默认不加（除非 char_limit>1）
                if str(ref.get("type") or "") == "character" and char_count >= max(char_limit, 1):
                    continue
            char_count += 1
        packed.append(ref)
    return packed


def enrich_ref_dict_metadata(ref: dict[str, Any], **extra: Any) -> dict[str, Any]:
    out = dict(ref)
    out.update({k: v for k, v in extra.items() if v is not None})
    if "purposes" in out and isinstance(out["purposes"], list):
        out["purposes_json"] = json.dumps(out["purposes"], ensure_ascii=False)
    return out


def gallery_fingerprint_material(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    material = []
    for ref in refs:
        material.append({
            "id": ref.get("id"),
            "type": ref.get("type"),
            "source": ref.get("source"),
            "path": ref.get("path") or ref.get("image_path"),
            "selected": bool(ref.get("selectedForSeedance", True)),
            "deleted": bool(ref.get("deleted")),
            "library_revision_id": ref.get("library_revision_id"),
            "library_view_id": ref.get("library_view_id"),
            "view_role": ref.get("view_role"),
            "purposes": purpose_list(ref),
            "qa_status": (ref.get("qa") or {}).get("status"),
            "qa_overall": (ref.get("qa") or {}).get("overall"),
        })
    return material


def is_plot_key_frame(ref: dict[str, Any] | Any) -> bool:
    if isinstance(ref, dict):
        return (
            str(ref.get("type") or "") == ASSET_TYPE_PLOT_KEY_FRAME
            or str(ref.get("slot_key") or "") == NARRATIVE_KEYFRAME_SLOT
        )
    return (
        getattr(ref, "type", None) == ASSET_TYPE_PLOT_KEY_FRAME
        or getattr(ref, "slot_key", None) == NARRATIVE_KEYFRAME_SLOT
    )
