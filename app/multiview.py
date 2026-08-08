"""人物/场景多视角资产包、镜头关键帧依赖与证据化 QA 支撑。

对应 PRD：人物多视角资产与关键帧一致性QA改造方案。
不引入 LoRA/FaceID；多视角图优先服务关键帧生成与 QA，不默认全部喂 Seedance。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.db import get_conn, get_setting, new_id, now
from app.refs import (
    _safe_name,
    character_visual_style_lock,
    effective_portrait_prompt,
    ensure_portrait_clothing_contract,
    portrait_override_appearance_anchor,
    production_appearance_anchor,
    scene_visual_style_lock,
)

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
    return (get_setting("watermark_qa_mode") or "reject").strip()


def fingerprint_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def view_input_fingerprint(
    *,
    view_role: str,
    prompt: str,
    anchor_text: str,
    parent_revision_id: str | None = None,
    base_view_id: str | None = None,
    seed_hint: str | None = None,
) -> str:
    """人物/场景单视角生成幂等指纹：相同版本、视角、提示词与种子不重复付费生成。"""
    return fingerprint_payload({
        "view_role": view_role,
        "prompt": prompt or "",
        "anchor_text": anchor_text or "",
        "parent_revision_id": parent_revision_id,
        "base_view_id": base_view_id,
        "seed_hint": seed_hint,
    })


def view_generation_operation_id(
    *,
    asset_kind: str,
    view_role: str,
    prompt: str,
    seed_inputs: list[str],
    fallback_identity: str,
) -> str:
    """Stable across candidate-row recreation, unique across real seed changes."""
    seed_hashes = [
        hashlib.sha256(seed.encode("utf-8")).hexdigest()
        for seed in seed_inputs
    ]
    material = json.dumps({
        "asset_kind": asset_kind,
        "view_role": view_role,
        "prompt": prompt,
        "seed_hashes": seed_hashes,
        "fallback_identity": "" if seed_hashes else fallback_identity,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    prefix = "op_character_view_" if asset_kind == "character_view" else "op_scene_view_"
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _ready_view_matches_fingerprint(view: dict[str, Any] | None, fingerprint: str) -> bool:
    """ready 视角可复用：指纹一致；旧数据 fingerprint 为空时也复用（由调用方回填）。"""
    if not view or not fingerprint:
        return False
    if view.get("status") != "ready":
        return False
    path = view.get("image_path")
    if not path or not Path(path).exists():
        return False
    existing = view.get("input_fingerprint") or None
    if existing is None:
        return True
    return existing == fingerprint


def _pending_view_can_be_reviewed(view: dict[str, Any] | None, fingerprint: str) -> bool:
    """A generated view awaiting QA should be reviewed, not regenerated at cost."""
    if not view or view.get("status") not in {PACK_STATUS_QA_PENDING, "unverified"}:
        return False
    path = view.get("image_path")
    if not path or not Path(path).exists():
        return False
    existing = view.get("input_fingerprint") or None
    return existing in {None, fingerprint}


def _backfill_view_fingerprint(conn, *, table: str, view_id: str, fingerprint: str) -> None:
    if not view_id or not fingerprint:
        return
    try:
        conn.execute(
            f"UPDATE {table} SET input_fingerprint=? WHERE id=? AND (input_fingerprint IS NULL OR input_fingerprint='')",
            (fingerprint, view_id),
        )
    except Exception:  # noqa: BLE001
        pass


def pack_result_ok(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    return result.get("status") in {PACK_STATUS_READY, "ready", "disabled"}


# ---------- 视角提示词 ----------

def character_view_prompt(
    visual_style: str,
    appearance: str,
    view_role: str,
    portrait_prompt: str | None = None,
) -> str:
    framing = {
        "front_full": "正面全身立绘，中性姿态，双臂自然，全身完整可见",
        "three_quarter": "3/4 侧面半身或全身，清晰展示五官深度与发型轮廓",
        "profile": "标准左侧面半身，清晰展示鼻梁、下颌、耳部与侧面发型",
        "back_full": "背面全身，展示服装背面与发型背部轮廓",
        "face_closeup": "面部近景特写，五官清晰，发型完整入画",
    }.get(view_role, "全身立绘")
    source = ensure_portrait_clothing_contract(
        portrait_override_appearance_anchor(appearance, portrait_prompt)
        or production_appearance_anchor(appearance)
    )
    return (
        f"{character_visual_style_lock(visual_style)}。"
        f"角色外观真值锚点：{source}。"
        "外观补充与全局画风是两个独立合同；冲突时全局画风优先，"
        "不得按外观文案关键词删除或重写内容。"
        f"生成同一角色多视角设定图（{VIEW_ROLE_LABELS.get(view_role, view_role)}）。"
        f"{framing}。纯浅米色背景，单角色。"
        "本条视角与构图要求覆盖源提示词中的视角、姿态和景别要求，但不得覆盖全局画风。"
        "同一角色、只改变观察角度，不改变稳定身份合同；结果必须满足结构化资产 QA。"
    )


def scene_view_prompt(visual_style: str, scene_canonical: str, view_role: str) -> str:
    camera = {
        "establishing": "建立镜头，完整展示空间关系与主标志物",
        "reverse_angle": "与建立视角相对的反打方向，相同几何与标志物，禁止简单复制原构图",
        "action_zone": "最常发生动作的局部区域，保留可识别标志物",
    }.get(view_role, "环境定场镜头")
    return (
        f"{scene_visual_style_lock(visual_style)}。"
        f"场景多视角定场图（{VIEW_ROLE_LABELS.get(view_role, view_role)}）：{scene_canonical}。"
        f"{camera}。9:16 竖屏，环境为主，画面中不出现任何人物。"
        "不得切换成真人实景、实拍布光或照片背景。"
        "禁止文字、字幕、水印、logo。"
    )


# ---------- 查询 ----------

def portrait_row_for_episode(
    project_id: str, name: str, episode_no: int | None, *, conn=None,
):
    if episode_no is None:
        return None
    try:
        return (conn or get_conn()).execute(
            "SELECT * FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None


def scene_row_for_episode(
    project_id: str, name: str, episode_no: int | None, *, conn=None,
):
    if episode_no is None:
        return None
    try:
        return (conn or get_conn()).execute(
            "SELECT * FROM scene_references "
            "WHERE project_id=? AND scene_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None


def project_bible_asset_names(
    project_id: str, *, conn=None,
) -> tuple[set[str], set[str]]:
    """Return the character/scene names that are managed by the project asset library."""
    try:
        row = (conn or get_conn()).execute(
            "SELECT bible_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        payload = json.loads(row["bible_json"] or "{}") if row else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    characters = {
        str(item.get("name") or "").strip()
        for item in (payload.get("characters") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    scenes = {
        str(item.get("name") or "").strip()
        for item in (payload.get("scenes") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    return characters, scenes


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
    conn=None,
) -> list[dict[str, Any]]:
    row = portrait_row_for_episode(project_id, name, episode_no, conn=conn)
    if not row:
        return []
    views = list_portrait_views(row["id"], conn=conn)
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
    conn=None,
) -> list[dict[str, Any]]:
    row = scene_row_for_episode(project_id, name, episode_no, conn=conn)
    if not row:
        return []
    views = list_scene_views(row["id"], conn=conn)
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
    """结构就绪：必需视角文件齐全即可（QA 分数不参与，PRD QA-SO #16/#20）。"""
    del pack_status
    return not missing_required_views(views, required)


def scene_pack_is_usable(row, views: list[dict[str, Any]]) -> bool:
    """下游消费资格只看必需视角文件齐全（PRD QA-SO）。"""
    if not row:
        return False
    return not missing_required_views(views, SCENE_REQUIRED_VIEWS)


def scene_primary_is_usable(row, views: list[dict[str, Any]]) -> bool:
    """主场景图可用资格只看文件是否存在（PRD QA-SO #21）。

    额外视角缺失或 QA 低分不得使已有 establishing 图失效。
    """
    del views
    if not row:
        return False
    path = row["image_path"] if "image_path" in row.keys() else None
    return bool(path and Path(path).exists())


# ---------- 视角选择（镜头级） ----------

def select_character_view_roles(shot: Any, character_name: str) -> list[str]:
    """按景别/朝向为角色选择 1~2 个最相关视角。"""
    from app.compiler import has_contact_action

    size = str(getattr(shot, "shot_size", "") or "")
    action = " ".join([
        str(getattr(shot, "primary_action", "") or ""),
        str(getattr(shot, "action_desc", "") or ""),
        str(getattr(shot, "first_frame_desc", "") or ""),
        str(getattr(shot, "last_frame_desc", "") or ""),
        str(getattr(shot, "state_in", "") or ""),
        str(getattr(shot, "state_out", "") or ""),
        str(getattr(shot, "camera_angle", "") or ""),
    ])
    roles: list[str] = []
    # 接触镜的侧面种子优先级高于特写/近景；否则正面定妆图会强烈诱导
    # 图生图退化成“正面站桩 + 手部悬空”。
    if has_contact_action(shot) or any(k in action.lower() for k in ("侧面", "侧视", "侧拍", "profile", "side view")):
        roles.append("profile")
        roles.append("three_quarter")
    elif any(k in size for k in ("特写", "近景")) or any(k in action for k in ("脸", "眼神", "表情")):
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
    ready_only: bool = True,
    conn=None,
    bible=None,
    screenplay=None,
) -> dict[str, Any]:
    """解析本镜人物/场景多视角依赖，供关键帧生成与 QA 冻结。

    生产路径默认 ready_only=True：非 ready 视角不得进入依赖与后续生成。
    """
    from app.continuity import effective_characters_visible

    managed_characters, managed_scenes = project_bible_asset_names(project_id, conn=conn)
    identity_resolver = None
    if screenplay is not None and getattr(screenplay, "narrative_plan", None) is not None:
        if bible is None:
            raise ValueError("narrative 资产依赖解析缺少 Bible")
        from app.identity_contracts import narrative_identity_resolver

        identity_resolver = narrative_identity_resolver(bible, screenplay)
    else:
        from app.character_policy import is_collective_role

    characters_out: list[dict[str, Any]] = []
    for name in effective_characters_visible(shot):
        if identity_resolver is not None:
            identity = identity_resolver.resolve(name, usage="visual")
            asset_name = identity.asset_name
            asset_required = identity.requires_asset
            asset_allowed = identity.allows_asset
            role_kind = identity.visual_policy
        else:
            identity = None
            asset_name = name
            asset_required = name in managed_characters
            asset_allowed = True
            role_kind = "collective" if is_collective_role(name) else "legacy"
        if not asset_allowed or (
            identity_resolver is None
            and name not in managed_characters
            and is_collective_role(name)
        ):
            characters_out.append({
                "name": name,
                "identity_id": identity.identity_id if identity is not None else name,
                "asset_name": asset_name,
                "role_kind": role_kind,
                "asset_required": False,
                "look_revision_id": None,
                "pack_status": None,
                "selected_view_ids": [],
                "selected_views": [],
                "available_view_roles": [],
                "missing_required": [],
            })
            continue
        # 缺视角检测需要看全部视角状态；选中只允许 ready
        all_views = portrait_views_for_episode(
            project_id, asset_name, episode_no, ready_only=False, conn=conn,
        )
        views = [v for v in all_views if v.get("status") == "ready" and v.get("image_path")] if ready_only else all_views
        row = portrait_row_for_episode(project_id, asset_name, episode_no, conn=conn)
        available = [v.get("view_role") for v in views if v.get("view_role")]
        wanted = select_character_view_roles(shot, name)
        selected = resolve_views_for_roles(
            views, wanted, fallback_roles=CHARACTER_REQUIRED_VIEWS,
        )
        if len(selected) > 1:
            identity_role_priority = {
                "front_full": 0,
                "three_quarter": 1,
                "face_closeup": 2,
                "profile": 3,
            }
            selected = [min(
                selected,
                key=lambda view: identity_role_priority.get(
                    str(view.get("view_role") or ""), 9,
                ),
            )]
        characters_out.append({
            "name": name,
            "identity_id": identity.identity_id if identity is not None else name,
            "asset_name": asset_name,
            "role_kind": role_kind,
            # Storyboards may contain one-off extras that intentionally have no
            # reusable library identity. Keep them auditable without requiring
            # a canonical multiview pack.
            "asset_required": asset_required,
            "look_revision_id": row["id"] if row else None,
            "pack_status": (row["pack_status"] if row and "pack_status" in row.keys() else None),
            "selected_view_ids": [v["id"] for v in selected],
            "selected_views": [
                {
                    "id": v["id"],
                    "view_role": v.get("view_role"),
                    "image_path": v.get("image_path"),
                    "input_fingerprint": v.get("input_fingerprint"),
                    "purposes": [PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
                }
                for v in selected
            ],
            "available_view_roles": available,
            "missing_required": missing_required_views(all_views, CHARACTER_REQUIRED_VIEWS),
        })

    scene_out = None
    sname = scene_name or getattr(shot, "scene_name", None) or ""
    if sname:
        all_views = scene_views_for_episode(
            project_id, sname, episode_no, ready_only=False, conn=conn,
        )
        row = scene_row_for_episode(project_id, sname, episode_no, conn=conn)
        pack_usable = scene_pack_is_usable(row, all_views)
        primary_usable = scene_primary_is_usable(row, all_views)
        usable = pack_usable or primary_usable
        views = (
            [v for v in all_views if v.get("status") == "ready" and v.get("image_path")]
            if ready_only and usable else ([] if ready_only else all_views)
        )
        wanted = select_scene_view_roles(shot)
        selected = resolve_views_for_roles(views, wanted, fallback_roles=SCENE_REQUIRED_VIEWS)
        scene_out = {
            "name": sname,
            "asset_required": sname in managed_scenes,
            "scene_revision_id": row["id"] if row else None,
            "pack_status": (row["pack_status"] if row and "pack_status" in row.keys() else None),
            "asset_usable": usable,
            "pack_usable": pack_usable,
            "primary_usable": primary_usable,
            "selected_view_ids": [v["id"] for v in selected],
            "selected_views": [
                {
                    "id": v["id"],
                    "view_role": v.get("view_role"),
                    "image_path": v.get("image_path"),
                    "input_fingerprint": v.get("input_fingerprint"),
                    "purposes": [PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR],
                }
                for v in selected
            ],
            "available_view_roles": [v.get("view_role") for v in views if v.get("view_role")],
            "missing_required": missing_required_views(all_views, SCENE_REQUIRED_VIEWS),
        }

    return build_reference_manifest(
        episode_no=episode_no,
        shot_id=shot_id,
        characters=characters_out,
        scene=scene_out,
    )


def manifest_asset_revision_ids(manifest: dict[str, Any] | None) -> dict[str, str | None]:
    """提取冻结依赖中的人物/场景版本 ID，供 stale 比较。"""
    out: dict[str, str | None] = {}
    if not isinstance(manifest, dict):
        return out
    for ch in manifest.get("characters") or []:
        name = ch.get("name")
        if name:
            out[f"character:{name}"] = ch.get("look_revision_id")
    scene = manifest.get("scene") or {}
    if isinstance(scene, dict) and scene.get("name"):
        out[f"scene:{scene['name']}"] = scene.get("scene_revision_id")
    return out


def manifest_asset_view_fingerprints(
    manifest: dict[str, Any] | None,
) -> dict[tuple[str, str, str], str]:
    """提取已冻结视角的内容版本；旧 manifest 缺字段时保持向后兼容。"""
    out: dict[tuple[str, str, str], str] = {}
    if not isinstance(manifest, dict):
        return out
    for ch in manifest.get("characters") or []:
        name = str(ch.get("name") or "")
        for view in ch.get("selected_views") or []:
            role = str(view.get("view_role") or "")
            fp = str(view.get("input_fingerprint") or "")
            if name and role and fp:
                out[("character", name, role)] = fp
    scene = manifest.get("scene") or {}
    if isinstance(scene, dict):
        name = str(scene.get("name") or "")
        for view in scene.get("selected_views") or []:
            role = str(view.get("view_role") or "")
            fp = str(view.get("input_fingerprint") or "")
            if name and role and fp:
                out[("scene", name, role)] = fp
    return out


def manifest_revisions_match(frozen: dict[str, Any] | None, current: dict[str, Any] | None) -> bool:
    return (
        manifest_asset_revision_ids(frozen) == manifest_asset_revision_ids(current)
        and manifest_asset_view_fingerprints(frozen) == manifest_asset_view_fingerprints(current)
        and {
            str(ch.get("name") or ""): bool(ch.get("asset_required", True))
            for ch in ((frozen or {}).get("characters") or [])
        } == {
            str(ch.get("name") or ""): bool(ch.get("asset_required", True))
            for ch in ((current or {}).get("characters") or [])
        }
        and bool(((frozen or {}).get("scene") or {}).get("asset_required", True))
        == bool(((current or {}).get("scene") or {}).get("asset_required", True))
    )


def manifest_production_blockers(manifest: dict[str, Any] | None) -> list[str]:
    """不完整/非 ready 多视角包阻断关键帧与视频生产的原因列表。"""
    if not isinstance(manifest, dict):
        return ["依赖 manifest 缺失"]
    blockers: list[str] = []
    for ch in manifest.get("characters") or []:
        name = ch.get("name") or "?"
        if not ch.get("look_revision_id"):
            # Old manifests have no asset_required and intentionally remain
            # strict. New manifests may explicitly mark a one-off extra as
            # text-driven rather than library-managed.
            if ch.get("asset_required", True):
                blockers.append(f"人物「{name}」缺少本集造型版本")
            continue
        pack = ch.get("pack_status")
        missing = [str(x) for x in (ch.get("missing_required") or []) if x]
        if pack and pack != PACK_STATUS_READY:
            blockers.append(f"人物「{name}」多视角包状态为 {pack}")
        elif missing:
            blockers.append(f"人物「{name}」缺少必需视角：{','.join(missing)}")
        elif not (ch.get("selected_view_ids") or ch.get("selected_views")):
            blockers.append(f"人物「{name}」无可用 ready 视角")
    scene = manifest.get("scene")
    if isinstance(scene, dict) and scene.get("name"):
        name = scene.get("name")
        if not scene.get("scene_revision_id"):
            if scene.get("asset_required", True):
                blockers.append(f"场景「{name}」缺少本集场景版本")
        else:
            pack = scene.get("pack_status")
            missing = [str(x) for x in (scene.get("missing_required") or []) if x]
            if pack and pack != PACK_STATUS_READY:
                blockers.append(f"场景「{name}」多视角包状态为 {pack}")
            elif missing:
                blockers.append(f"场景「{name}」缺少必需视角：{','.join(missing)}")
            elif not (scene.get("selected_view_ids") or scene.get("selected_views")):
                blockers.append(f"场景「{name}」无可用 ready 视角")
    return blockers


def scan_episode_reference_asset_gaps(
    *,
    project_id: str,
    episode_no: int,
    shots: list[tuple[str, Any]],
    bible=None,
    screenplay=None,
) -> dict[str, Any]:
    """Read-only production preflight for the reusable assets used by an episode."""
    missing_characters: set[str] = set()
    missing_scenes: set[str] = set()
    blockers: list[str] = []
    for shot_id, shot in shots:
        manifest = resolve_shot_asset_dependencies(
            project_id=project_id,
            episode_no=episode_no,
            shot_id=shot_id,
            shot=shot,
            scene_name=(getattr(shot, "scene_name", None) or None),
            bible=bible,
            screenplay=screenplay,
        )
        shot_blockers = manifest_production_blockers(manifest)
        if shot_blockers:
            shot_no = int(getattr(shot, "shot_no", 0) or 0)
            blockers.extend(f"镜头 {shot_no}: {reason}" for reason in shot_blockers)
        for character in manifest.get("characters") or []:
            if not character.get("asset_required", True):
                continue
            if (
                not character.get("look_revision_id")
                or character.get("pack_status") not in {None, PACK_STATUS_READY}
                or bool(character.get("missing_required"))
                or not (character.get("selected_view_ids") or character.get("selected_views"))
            ):
                name = str(character.get("name") or "").strip()
                if name:
                    missing_characters.add(name)
        scene = manifest.get("scene") or {}
        if isinstance(scene, dict) and scene.get("asset_required", True):
            if (
                not scene.get("scene_revision_id")
                or scene.get("pack_status") not in {None, PACK_STATUS_READY}
                or bool(scene.get("missing_required"))
                or not (scene.get("selected_view_ids") or scene.get("selected_views"))
            ):
                name = str(scene.get("name") or "").strip()
                if name:
                    missing_scenes.add(name)
    return {
        "characters": sorted(missing_characters),
        "scenes": sorted(missing_scenes),
        "blockers": blockers,
    }


def assert_manifest_allows_production(manifest: dict[str, Any] | None) -> list[str]:
    """返回资产缺口供重试和风险记录，但绝不阻断下游生产。

    人物/场景包的补齐仍会在调用前尝试；尝试耗尽后，视频链使用当前已有视图、
    叙事关键帧或纯文本提示继续。函数名保留以兼容现有调用方。
    """
    if not character_multiview_enabled() and not scene_multiview_enabled():
        return []
    return manifest_production_blockers(manifest)


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
    """Use one strong identity image per person and one scene image.

    Seedance/Seedream can interpret multiple angles of the same person as
    different subjects. If any visible individual lacks an identity image,
    omit all partial character seeds so the available person's gender/face
    cannot bleed into the unanchored role. The deterministic text contract and
    QA gate still enforce the full cast.
    """
    characters = [
        item for item in (manifest.get("characters") or [])
        if str(item.get("role_kind") or "") not in {"collective", "functional"}
    ]
    role_priority = {
        "front_full": 0,
        "three_quarter": 1,
        "face_closeup": 2,
        "profile": 3,
    }
    character_paths: list[str] = []
    complete_character_coverage = bool(characters)
    for character in characters:
        views = [
            view for view in (character.get("selected_views") or [])
            if view.get("image_path") and Path(str(view["image_path"])).is_file()
        ]
        if not views:
            complete_character_coverage = False
            break
        preferred = min(
            views,
            key=lambda view: role_priority.get(str(view.get("view_role") or ""), 9),
        )
        character_paths.append(str(preferred["image_path"]))
    if not complete_character_coverage:
        character_paths = []

    scene_paths: list[str] = []
    scene = manifest.get("scene") or {}
    scene_views = [
        view for view in (scene.get("selected_views") or [])
        if view.get("image_path") and Path(str(view["image_path"])).is_file()
    ]
    if scene_views:
        preferred_scene = next(
            (view for view in scene_views if view.get("view_role") == "establishing"),
            scene_views[0],
        )
        scene_paths.append(str(preferred_scene["image_path"]))
    return list(dict.fromkeys([*character_paths, *scene_paths]))


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


def _discard_rejected_candidate(path: str) -> None:
    """尽力清理尚未登记入库的 QA 失败候选，不让清理异常覆盖真实 QA 结果。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


async def _save_image_item(item: dict, dest: str) -> None:
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        import base64
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


async def _generate_image(prompt: str, *, seed_inputs: list[str] | None = None, call_meta: dict | None = None) -> dict:
    return await hiagent.generate_image(
        prompt,
        size=config.REF_IMAGE_SIZE,
        image_inputs=seed_inputs or None,
        call_meta=call_meta,
    )


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
            initiator_label="场景多视角主图QA",
            environment_only=True,
        )
        from app.scene_policy import normalize_scene_image_qa
        qa["view_role"] = view_role
        return normalize_scene_image_qa(qa, environment_only=True)
    except Exception as exc:  # noqa: BLE001
        return {
            "overall": None,
            "status": "unverified",
            "issues": [f"场景视角 QA 未完成：{type(exc).__name__}"],
            "qa_recovered": True,
            "view_role": view_role,
        }


def _qa_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if value is None or value == "":
        return []
    return [str(value)]


async def review_character_pack_consistency(views: list[dict[str, Any]], appearance: str) -> dict[str, Any]:
    """一次完成逐视角质量与整包一致性 QA。"""
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
    if len(frames) != len(views) or len(frames) < 2:
        return {
            "overall": None,
            "status": "unverified",
            "issues": ["多视角素材不完整，无法完成整包 QA"],
        }
    expectation = (
        "你是角色多视角一致性评审。以下图片是同一角色不同观察角度的设定图，顺序："
        + ", ".join(f"{i+1}:{r}" for i, r in enumerate(roles))
        + f"。外观锚点：{appearance}。"
        "先逐张检查对应观察角度、稳定身份特征、主体完整性与技术缺陷；"
        "再检查同一角色脸部特征、发型、服装、体型在各视角一致，只允许观察角度不同。"
        "各视角之间发生性别、核心身份、脸/发型/服装/体型漂移，或缺失视角、多余人物、畸形、"
        "遮挡人物主体的文字/水印属于 hard_failures。供应商角落水印若不遮挡人物，"
        "只属于 issues 软警告，不得写入 hard_failures。表情、眼神、笑容、爱慕/妩媚/虚荣/戏谑等气质，"
        "以及普通展示站姿只属于 issues 软警告，不得写入 hard_failures，也不得拉低 overall；"
        "定妆多视角允许使用统一的中性表情。三张图若彼此一致，仅与文字锚点在视觉年龄、"
        "服装款式、发饰或审美装饰上有差异，也只属于 issues，不得判为 hard_failures。"
        '输出 JSON：{"overall":0~1,"face_consistency":0~1,"outfit_consistency":0~1,'
        '"hair_consistency":0~1,"body_consistency":0~1,'
        '"identity_consistent":bool|null,'
        '"views":[{"view_role":str,"identity_match":0~1,"presentation_match":0~1,'
        '"clean_frame":0~1,"overall":0~1,"person_count":int|null,'
        '"stable_identity_matches":bool|null,"full_body_visible":bool|null,'
        '"crop_severity":"none|minor|major","anatomy_valid":bool|null,'
        '"watermark_detected":bool|null,"watermark_occluding":bool|null,'
        '"forbidden_text_detected":bool|null,"forbidden_text_is_provider_mark":bool|null,'
        '"issues":[str],"hard_failures":[str]}],"issues":[str],"hard_failures":[str]}'
    )
    try:
        raw = await hiagent.vlm_check(frames, expectation, call_meta={"initiator_label": "人物多视角整包QA"})
        from app.schemas import extract_json
        from app.portrait_policy import normalize_portrait_seed_qa, unique_messages
        data = extract_json(raw)
        reported_group_hard = _qa_string_list(data.get("hard_failures"))
        for key in ("overall", "face_consistency", "outfit_consistency", "hair_consistency", "body_consistency"):
            try:
                data[key] = max(0.0, min(1.0, float(data.get(key, 0))))
            except (TypeError, ValueError):
                data[key] = 0.0
        group_hard: list[str] = []
        if data.get("identity_consistent") is False:
            group_hard.append("结构化整包观察确认角色身份跨视角不一致")
        data["hard_failures"] = group_hard
        data["issues"] = unique_messages([
            *_qa_string_list(data.get("issues")),
            *reported_group_hard,
        ])
        reported = {
            str(item.get("view_role") or ""): item
            for item in (data.get("views") or []) if isinstance(item, dict)
        }
        normalized_views = []
        for role in roles:
            item = dict(reported.get(role) or {})
            item["view_role"] = role
            normalized_views.append(normalize_portrait_seed_qa(item))
        data["views"] = normalized_views
        data["issues"] = unique_messages([
            *data["issues"],
            *(
                f"{item['view_role']}：{message}"
                for item in normalized_views
                for message in item.get("issues", [])
            ),
        ])
        passed = (
            float(data.get("overall") or 0) >= 0.75
            and not data["hard_failures"]
            and all(item["status"] in {"ready", "warning"} for item in normalized_views)
        )
        data["status"] = "warning" if passed and data["issues"] else ("ready" if passed else "failed")
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
    if len(frames) != len(views) or len(frames) < 2:
        return {
            "overall": None,
            "status": "unverified",
            "issues": ["场景多视角素材不完整，无法完成整包 QA"],
        }
    expectation = (
        "你是场景多视角一致性评审。以下是同一场景不同机位的无人定场图，顺序："
        + ", ".join(f"{i+1}:{r}" for i, r in enumerate(roles))
        + f"。场景锚点：{scene_canonical}。"
        "先逐张识别实际视角角色并检查场景锚点和画面结构；"
        "再检查相机轴线是否真正变化、门窗/主陈设/标志物的左右前后关系及空间覆盖。"
        "相似度/SSIM 只能作为证据，不能单独决定通过；对称空间也必须给出轴线和标志物依据。"
        "views 必须与输入顺序一一对应，view_role 只能原样输出上述英文枚举，"
        "不得加 shot、机位描述或其他后缀。"
        '输出 JSON：{"overall":0~1,"geometry_consistency":0~1,"landmark_consistency":0~1,'
        '"lighting_consistency":0~1,"views":[{"view_role":str,"overall":0~1,'
        '"view_role_matches":bool,"camera_axis_valid":bool,"landmark_relation_valid":bool,'
        '"space_coverage_valid":bool,"issues":[str],"hard_failures":[str]}],'
        '"issues":[str],"hard_failures":[str],"uncertainties":[str]}'
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
        raw_views = [dict(item) for item in (data.get("views") or []) if isinstance(item, dict)]
        unused = set(range(len(raw_views)))
        normalized_views = []
        for role_index, role in enumerate(roles):
            matched_index = next(
                (index for index in unused if str(raw_views[index].get("view_role") or "").strip() == role),
                None,
            )
            if matched_index is None:
                role_token = role.replace("_", " ").lower()
                matched_index = next(
                    (
                        index for index in unused
                        if role_token in str(raw_views[index].get("view_role") or "")
                        .replace("_", " ").lower()
                    ),
                    None,
                )
            # The contract also defines positional correspondence.  This
            # recovers otherwise valid VLM output such as
            # "establishing shot (left viewpoint)" without fabricating facts.
            if matched_index is None and role_index in unused:
                matched_index = role_index
            item = dict(raw_views[matched_index]) if matched_index is not None else {}
            if matched_index is not None:
                unused.discard(matched_index)
            item["view_role"] = role
            try:
                item["overall"] = max(0.0, min(1.0, float(item.get("overall"))))
            except (TypeError, ValueError):
                item["overall"] = None
            item["issues"] = _qa_string_list(item.get("issues"))
            item["hard_failures"] = _qa_string_list(item.get("hard_failures"))
            item["status"] = (
                "ready" if item["overall"] is not None and item["overall"] >= 0.6
                and not item["hard_failures"] else "failed"
            )
            normalized_views.append(item)
        data["views"] = normalized_views
        data["issues"] = _qa_string_list(data.get("issues"))
        data["hard_failures"] = _qa_string_list(data.get("hard_failures"))
        from app.scene_policy import normalize_scene_pack_qa
        return normalize_scene_pack_qa(
            data,
            required_roles=[*SCENE_REQUIRED_VIEWS, *(["action_zone"] if "action_zone" in roles else [])],
            actual_roles=roles,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "overall": None,
            "status": "unverified",
            "issues": [f"场景整包 QA 未完成：{type(exc).__name__}"],
            "qa_recovered": True,
        }


def _view_passed(qa: dict[str, Any] | None) -> bool:
    """Score-only：单视图 QA 永不因低分判失败（PRD QA-SO）。缺少 QA 视为 unscored 但仍通过结构路径。"""
    del qa
    return True


def _apply_pack_view_qa(
    conn,
    *,
    table: str,
    parent_column: str,
    parent_id: str,
    views: list[dict[str, Any]],
    group_qa: dict[str, Any],
) -> list[str]:
    """落回整包 QA 评分；视角 ready 只看文件是否存在（PRD QA-SO #16/#20）。"""
    reported = {
        str(item.get("view_role") or ""): dict(item)
        for item in (group_qa.get("views") or []) if isinstance(item, dict)
    }
    failed: list[str] = []
    for view in views:
        role = str(view.get("view_role") or "")
        qa = reported.get(role)
        if qa is None:
            qa = {
                "view_role": role,
                "overall": group_qa.get("overall"),
                "issues": _qa_string_list(group_qa.get("issues")),
                "hard_failures": _qa_string_list(group_qa.get("hard_failures")),
                "status": group_qa.get("status"),
            }
        qa = {
            **qa,
            "evaluation_role": "score_only",
            "runtime_blocking": False,
            "retry_eligible": False,
        }
        path = view.get("image_path")
        has_file = bool(path and Path(str(path)).exists())
        status = "ready" if has_file else "failed"
        if status != "ready":
            failed.append(role)
        conn.execute(
            f"UPDATE {table} SET qa_json=?, status=? WHERE {parent_column}=? AND view_role=?",
            (json.dumps(qa, ensure_ascii=False), status, parent_id, role),
        )
    return failed


async def ensure_character_multiview_pack(
    *,
    project_id: str,
    portrait_id: str,
    character_name: str,
    appearance: str,
    visual_style: str,
    portrait_prompt: str | None = None,
    ep_start: int,
    base_portrait_id: str | None = None,
    optional_views: list[str] | None = None,
    primary_qa: dict[str, Any] | None = None,
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
    # 1) front_full 优先（含 fingerprint 幂等）
    front = existing_views.get("front_full")
    parent = conn.execute("SELECT * FROM character_portraits WHERE id=?", (portrait_id,)).fetchone()
    base_front = base_views.get("front_full") or {}
    effective_prompt = effective_portrait_prompt(
        visual_style, appearance, portrait_prompt,
    )
    front_prompt = character_view_prompt(
        visual_style, appearance, "front_full", effective_prompt,
    )
    parent_prompt_matches = bool(
        parent
        and (parent["prompt"] or "").strip()
        and (parent["prompt"] or "").strip() == effective_prompt
    )
    use_parent_primary = bool(
        parent
        and parent["image_path"]
        and Path(parent["image_path"]).exists()
        and (parent_prompt_matches or primary_qa is not None)
    )
    front_prompt_for_fp = effective_prompt if use_parent_primary else front_prompt
    front_fp = view_input_fingerprint(
        view_role="front_full",
        prompt=front_prompt_for_fp,
        anchor_text=effective_prompt,
        parent_revision_id=portrait_id,
        base_view_id=base_front.get("id"),
        seed_hint=base_front.get("image_path"),
    )
    front_is_authoritative = bool(
        front
        and front.get("status") == "ready"
        and front.get("image_path")
        and Path(str(front["image_path"])).exists()
    )
    if not (
        _ready_view_matches_fingerprint(front, front_fp)
        or front_is_authoritative
    ):
        if use_parent_primary:
            qa = dict(primary_qa) if primary_qa else await review_character_view(
                parent["image_path"], effective_prompt, "front_full",
            )
            qa.setdefault("view_role", "front_full")
            status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
            _upsert_character_view(
                conn, portrait_id=portrait_id, view_role="front_full", framing="full_body",
                image_path=parent["image_path"], prompt=parent["prompt"] or front_prompt,
                qa=qa, artifact_id=parent["artifact_id"] if "artifact_id" in parent.keys() else None,
                base_view_id=base_front.get("id"),
                status=status, fingerprint=front_fp,
            )
            conn.commit()
            if status != "ready":
                _set_portrait_pack_fields(conn, portrait_id, pack_status=PACK_STATUS_FAILED,
                                         group_qa_json=json.dumps(qa, ensure_ascii=False))
                conn.commit()
                return {"status": "failed", "portrait_id": portrait_id, "failed_view": "front_full", "qa": qa}
        else:
            seed = None
            if base_front.get("image_path") and Path(base_front["image_path"]).exists():
                seed = [hiagent.data_url_from_file(base_front["image_path"])]
            path = _view_path(project_id, "character", character_name, "front_full", ep_start)
            item = await _generate_image(
                front_prompt, seed_inputs=seed,
                call_meta={
                    "asset_kind": "character_view",
                    "view_role": "front_full",
                    "character_name": character_name,
                    "operation_id": "op_character_view_" + hashlib.sha256(
                        f"{portrait_id}:front_full:{front_fp}".encode("utf-8")
                    ).hexdigest()[:32],
                    "reuse_successful_operation": True,
                },
            )
            await _save_image_item(item, path)
            qa = await review_character_view(path, effective_prompt, "front_full")
            status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
            gen_fp = view_input_fingerprint(
                view_role="front_full", prompt=front_prompt, anchor_text=effective_prompt,
                parent_revision_id=portrait_id, base_view_id=base_front.get("id"),
                seed_hint=base_front.get("image_path"),
            )
            _upsert_character_view(
                conn, portrait_id=portrait_id, view_role="front_full", framing="full_body",
                image_path=path, prompt=front_prompt, qa=qa, artifact_id=None,
                base_view_id=base_front.get("id"), status=status, fingerprint=gen_fp,
            )
            # 镜像到父表 image_path
            conn.execute("UPDATE character_portraits SET image_path=? WHERE id=?", (path, portrait_id))
            conn.commit()
            if status != "ready":
                _set_portrait_pack_fields(conn, portrait_id, pack_status=PACK_STATUS_FAILED,
                                         group_qa_json=json.dumps(qa, ensure_ascii=False))
                conn.commit()
                return {"status": "failed", "portrait_id": portrait_id, "failed_view": "front_full", "qa": qa}
    elif front and not front.get("input_fingerprint"):
        _backfill_view_fingerprint(
            conn, table="character_portrait_views", view_id=front["id"], fingerprint=front_fp,
        )
        conn.commit()

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
        prompt = character_view_prompt(
            visual_style, appearance, view_role, effective_prompt,
        )
        base = base_views.get(view_role) or {}
        fp = view_input_fingerprint(
            view_role=view_role,
            prompt=prompt,
            anchor_text=effective_prompt,
            parent_revision_id=portrait_id,
            base_view_id=base.get("id"),
            seed_hint=front.get("image_path") or base.get("image_path"),
        )
        cur = existing_views.get(view_role)
        if _ready_view_matches_fingerprint(cur, fp):
            if cur and not cur.get("input_fingerprint"):
                _backfill_view_fingerprint(
                    conn, table="character_portrait_views", view_id=cur["id"], fingerprint=fp,
                )
                conn.commit()
            return {"view_role": view_role, "status": "ready", "id": cur["id"], "reused": True}
        if _pending_view_can_be_reviewed(cur, fp):
            if cur and not cur.get("input_fingerprint"):
                _backfill_view_fingerprint(
                    conn, table="character_portrait_views", view_id=cur["id"], fingerprint=fp,
                )
                conn.commit()
            return {
                "view_role": view_role,
                "status": PACK_STATUS_QA_PENDING,
                "id": cur["id"],
                "reused": True,
            }
        seeds = list(front_seed)
        if base.get("image_path") and Path(base["image_path"]).exists():
            seeds.append(hiagent.data_url_from_file(base["image_path"]))
        path = _view_path(project_id, "character", character_name, view_role, ep_start)
        item = await _generate_image(
            prompt, seed_inputs=seeds or None,
            call_meta={
                "asset_kind": "character_view",
                "view_role": view_role,
                "character_name": character_name,
                    "operation_id": view_generation_operation_id(
                        asset_kind="character_view",
                        view_role=view_role,
                        prompt=prompt,
                        seed_inputs=seeds,
                        fallback_identity=f"{portrait_id}:{fp}",
                    ),
                "reuse_successful_operation": True,
            },
        )
        await _save_image_item(item, path)
        view_id = _upsert_character_view(
            conn, portrait_id=portrait_id, view_role=view_role,
            framing="half_or_full" if view_role != "face_closeup" else "closeup",
            image_path=path, prompt=prompt, qa=None, artifact_id=None,
            base_view_id=base.get("id"), status=PACK_STATUS_QA_PENDING, fingerprint=fp,
        )
        conn.commit()
        return {"view_role": view_role, "status": PACK_STATUS_QA_PENDING, "id": view_id}

    side_roles = [r for r in roles if r != "front_full"]
    side_results = await asyncio.gather(*[_gen_side(r) for r in side_roles])
    failed = [r for r in side_results if r.get("status") not in {"ready", PACK_STATUS_QA_PENDING}]
    if failed:
        _set_portrait_pack_fields(
            conn, portrait_id, pack_status=PACK_STATUS_FAILED,
            group_qa_json=json.dumps({"failed_views": failed}, ensure_ascii=False),
        )
        conn.commit()
        return {"status": "failed", "portrait_id": portrait_id, "failed_views": failed}

    views = list_portrait_views(portrait_id, conn=conn)
    required_views = [v for v in views if v.get("view_role") in CHARACTER_REQUIRED_VIEWS]
    group_qa = await review_character_pack_consistency(required_views, effective_prompt)
    group_qa = {
        **group_qa,
        "evaluation_role": "score_only",
        "runtime_blocking": False,
        "retry_eligible": False,
    }
    failed_roles = _apply_pack_view_qa(
        conn,
        table="character_portrait_views",
        parent_column="portrait_id",
        parent_id=portrait_id,
        views=required_views,
        group_qa=group_qa,
    )
    # 仅结构缺失可失败；QA 低分/hard_failures 不阻断整包生效。
    if failed_roles:
        _set_portrait_pack_fields(
            conn, portrait_id, pack_status=PACK_STATUS_FAILED,
            group_qa_json=json.dumps(group_qa, ensure_ascii=False),
        )
        conn.commit()
        return {
            "status": "failed", "portrait_id": portrait_id,
            "group_qa": group_qa, "failed_views": failed_roles,
        }

    views = list_portrait_views(portrait_id, conn=conn)
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
    primary_qa: dict[str, Any] | None = None,
    optional_views: list[str] | None = None,
) -> dict[str, Any]:
    if not scene_multiview_enabled():
        return {"status": "disabled", "scene_reference_id": scene_reference_id}
    conn = get_conn()
    _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_GENERATING)
    conn.commit()

    existing_views = {v["view_role"]: v for v in list_scene_views(scene_reference_id, conn=conn)}
    base_views = {v["view_role"]: v for v in list_scene_views(base_scene_id, conn=conn)} if base_scene_id else {}

    # establishing（含 fingerprint 幂等）
    est = existing_views.get("establishing")
    parent = conn.execute("SELECT * FROM scene_references WHERE id=?", (scene_reference_id,)).fetchone()
    base_est = base_views.get("establishing") or {}
    est_prompt = scene_view_prompt(visual_style, scene_canonical, "establishing")
    est_prompt_for_fp = (parent["prompt"] if parent and parent["prompt"] else est_prompt)
    est_fp = view_input_fingerprint(
        view_role="establishing",
        prompt=est_prompt_for_fp,
        anchor_text=scene_canonical,
        parent_revision_id=scene_reference_id,
        base_view_id=base_est.get("id"),
        seed_hint=base_est.get("image_path"),
    )
    if not _ready_view_matches_fingerprint(est, est_fp):
        if parent and parent["image_path"] and Path(parent["image_path"]).exists():
            qa = dict(primary_qa) if primary_qa else await review_scene_view(
                parent["image_path"], scene_canonical, "establishing",
            )
            qa.setdefault("view_role", "establishing")
            status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
            _upsert_scene_view(
                conn, scene_reference_id=scene_reference_id, view_role="establishing",
                camera_axis="establishing", image_path=parent["image_path"],
                prompt=parent["prompt"] or est_prompt, qa=qa,
                artifact_id=parent["artifact_id"] if "artifact_id" in parent.keys() else None,
                base_view_id=base_est.get("id"),
                status=status, fingerprint=est_fp,
            )
            conn.commit()
            if status != "ready":
                _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_FAILED,
                                      group_qa_json=json.dumps(qa, ensure_ascii=False))
                conn.commit()
                return {"status": "failed", "scene_reference_id": scene_reference_id, "failed_view": "establishing"}
        else:
            path = _view_path(project_id, "scene", scene_name, "establishing", ep_start)
            item = await _generate_image(
                est_prompt,
                call_meta={
                    "asset_kind": "scene_view",
                    "view_role": "establishing",
                    "scene_name": scene_name,
                    "operation_id": "op_scene_view_" + hashlib.sha256(
                        f"{scene_reference_id}:establishing:{est_fp}".encode("utf-8")
                    ).hexdigest()[:32],
                    "reuse_successful_operation": True,
                },
            )
            await _save_image_item(item, path)
            qa = await review_scene_view(path, scene_canonical, "establishing")
            status = "ready" if _view_passed(qa) else ("unverified" if qa.get("status") == "unverified" else "failed")
            gen_fp = view_input_fingerprint(
                view_role="establishing", prompt=est_prompt, anchor_text=scene_canonical,
                parent_revision_id=scene_reference_id, base_view_id=base_est.get("id"),
                seed_hint=base_est.get("image_path"),
            )
            _upsert_scene_view(
                conn, scene_reference_id=scene_reference_id, view_role="establishing",
                camera_axis="establishing", image_path=path, prompt=est_prompt, qa=qa, artifact_id=None,
                base_view_id=base_est.get("id"),
                status=status, fingerprint=gen_fp,
            )
            conn.execute("UPDATE scene_references SET image_path=? WHERE id=?", (path, scene_reference_id))
            conn.commit()
            if status != "ready":
                _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_FAILED,
                                      group_qa_json=json.dumps(qa, ensure_ascii=False))
                conn.commit()
                return {"status": "failed", "scene_reference_id": scene_reference_id, "failed_view": "establishing"}
    elif est and not est.get("input_fingerprint"):
        _backfill_view_fingerprint(
            conn, table="scene_reference_views", view_id=est["id"], fingerprint=est_fp,
        )
        conn.commit()

    existing_views = {v["view_role"]: v for v in list_scene_views(scene_reference_id, conn=conn)}
    est = existing_views.get("establishing") or {}
    if est.get("status") != "ready":
        _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_FAILED)
        conn.commit()
        return {"status": "failed", "scene_reference_id": scene_reference_id, "failed_view": "establishing"}

    # reverse_angle（含 fingerprint 幂等）
    rev = existing_views.get("reverse_angle")
    rev_prompt = scene_view_prompt(visual_style, scene_canonical, "reverse_angle")
    base_rev = base_views.get("reverse_angle") or {}
    rev_fp = view_input_fingerprint(
        view_role="reverse_angle",
        prompt=rev_prompt,
        anchor_text=scene_canonical,
        parent_revision_id=scene_reference_id,
        base_view_id=base_rev.get("id"),
        seed_hint=est.get("image_path"),
    )
    if not _ready_view_matches_fingerprint(rev, rev_fp) and not _pending_view_can_be_reviewed(rev, rev_fp):
        seeds = []
        if est.get("image_path") and Path(est["image_path"]).exists():
            seeds.append(hiagent.data_url_from_file(est["image_path"]))
        path = _view_path(project_id, "scene", scene_name, "reverse_angle", ep_start)
        item = await _generate_image(
            rev_prompt, seed_inputs=seeds or None,
            call_meta={
                "asset_kind": "scene_view",
                "view_role": "reverse_angle",
                "scene_name": scene_name,
                "operation_id": view_generation_operation_id(
                    asset_kind="scene_view",
                    view_role="reverse_angle",
                    prompt=rev_prompt,
                    seed_inputs=seeds,
                    fallback_identity=f"{scene_reference_id}:{rev_fp}",
                ),
                "reuse_successful_operation": True,
            },
        )
        await _save_image_item(item, path)
        _upsert_scene_view(
            conn, scene_reference_id=scene_reference_id, view_role="reverse_angle",
            camera_axis="reverse", image_path=path, prompt=rev_prompt, qa=None, artifact_id=None,
            base_view_id=base_rev.get("id"),
            status=PACK_STATUS_QA_PENDING, fingerprint=rev_fp,
        )
        conn.commit()
    elif rev and not rev.get("input_fingerprint"):
        _backfill_view_fingerprint(
            conn, table="scene_reference_views", view_id=rev["id"], fingerprint=rev_fp,
        )
        conn.commit()

    requested_optional = [role for role in (optional_views or []) if role in SCENE_OPTIONAL_VIEWS]
    if "action_zone" in requested_optional:
        existing_views = {v["view_role"]: v for v in list_scene_views(scene_reference_id, conn=conn)}
        action = existing_views.get("action_zone")
        action_prompt = scene_view_prompt(visual_style, scene_canonical, "action_zone")
        action_fp = view_input_fingerprint(
            view_role="action_zone", prompt=action_prompt, anchor_text=scene_canonical,
            parent_revision_id=scene_reference_id,
            seed_hint=(existing_views.get("establishing") or {}).get("image_path"),
        )
        if not _ready_view_matches_fingerprint(action, action_fp) and not _pending_view_can_be_reviewed(action, action_fp):
            seeds = []
            anchor = existing_views.get("establishing") or {}
            if anchor.get("image_path") and Path(anchor["image_path"]).exists():
                seeds.append(hiagent.data_url_from_file(anchor["image_path"]))
            path = _view_path(project_id, "scene", scene_name, "action_zone", ep_start)
            item = await _generate_image(
                action_prompt, seed_inputs=seeds or None,
                call_meta={
                    "asset_kind": "scene_view",
                    "view_role": "action_zone",
                    "scene_name": scene_name,
                    "operation_id": view_generation_operation_id(
                        asset_kind="scene_view",
                        view_role="action_zone",
                        prompt=action_prompt,
                        seed_inputs=seeds,
                        fallback_identity=f"{scene_reference_id}:{action_fp}",
                    ),
                    "reuse_successful_operation": True,
                },
            )
            await _save_image_item(item, path)
            _upsert_scene_view(
                conn, scene_reference_id=scene_reference_id, view_role="action_zone",
                camera_axis="action", image_path=path, prompt=action_prompt, qa=None, artifact_id=None,
                base_view_id=None, status=PACK_STATUS_QA_PENDING, fingerprint=action_fp,
            )
            conn.commit()

    views = list_scene_views(scene_reference_id, conn=conn)
    required_roles = (*SCENE_REQUIRED_VIEWS, *requested_optional)
    required_views = [v for v in views if v.get("view_role") in required_roles]
    # At this point qa_pending is a real on-disk image that the loop below must
    # review.  Only a physically absent role is missing; using
    # missing_required_views here used to abort before QA because that helper
    # intentionally counts only ready views for downstream consumption.
    present_roles = {
        view.get("view_role") for view in views
        if view.get("image_path") and Path(view["image_path"]).exists()
    }
    missing = [role for role in required_roles if role not in present_roles]
    if missing:
        group_qa = {
            "status": "failed", "hard_failures": [f"缺少必需视角：{role}" for role in missing],
            "missing_required": missing, "required_views": list(required_roles),
        }
        _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_FAILED,
                               group_qa_json=json.dumps(group_qa, ensure_ascii=False))
        conn.commit()
        return {"status": "failed", "scene_reference_id": scene_reference_id, "group_qa": group_qa, "failed_views": missing}
    single_failed: list[str] = []
    for view in required_views:
        try:
            existing_qa = json.loads(view.get("qa_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            existing_qa = {}
        if existing_qa.get("policy_version") and existing_qa.get("hard_gate_passed") is True:
            continue
        qa = await review_scene_view(view["image_path"], scene_canonical, view["view_role"])
        view_status = "ready" if _view_passed(qa) else (
            "unverified" if qa.get("status") == "unverified" else "failed"
        )
        conn.execute(
            "UPDATE scene_reference_views SET qa_json=?,status=? WHERE id=?",
            (json.dumps(qa, ensure_ascii=False), view_status, view["id"]),
        )
        view["qa_json"] = json.dumps(qa, ensure_ascii=False)
        view["status"] = view_status
        if view_status != "ready":
            single_failed.append(view["view_role"])
    if single_failed:
        group_qa = {
            "status": "failed", "hard_failures": [f"{role} 文件不可用" for role in single_failed],
            "failed_views": single_failed, "required_views": list(required_roles),
            "evaluation_role": "score_only", "runtime_blocking": False,
        }
        _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_FAILED,
                               group_qa_json=json.dumps(group_qa, ensure_ascii=False))
        conn.commit()
        return {"status": "failed", "scene_reference_id": scene_reference_id, "group_qa": group_qa, "failed_views": single_failed}
    conn.commit()
    group_qa = await review_scene_pack_consistency(required_views, scene_canonical)
    group_qa = {
        **group_qa,
        "evaluation_role": "score_only",
        "runtime_blocking": False,
        "retry_eligible": False,
    }
    failed_roles = _apply_pack_view_qa(
        conn,
        table="scene_reference_views",
        parent_column="scene_reference_id",
        parent_id=scene_reference_id,
        views=required_views,
        group_qa=group_qa,
    )
    # 仅结构缺失可失败；QA 分数不阻断场景整包。
    if failed_roles:
        _set_scene_pack_fields(
            conn, scene_reference_id, pack_status=PACK_STATUS_FAILED,
            group_qa_json=json.dumps(group_qa, ensure_ascii=False),
        )
        conn.commit()
        return {
            "status": "failed", "scene_reference_id": scene_reference_id,
            "group_qa": group_qa, "failed_views": failed_roles,
        }

    views = list_scene_views(scene_reference_id, conn=conn)
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


def keyframe_runtime_blocking_failures(qa: dict[str, Any]) -> set[str]:
    if qa.get("runtime_blocking") is not True:
        return set()
    facts = {
        str(item).strip()
        for item in (qa.get("blocking_facts") or [])
        if str(item).strip()
    }
    return facts or {"typed_runtime_gate_failed"}


def keyframe_gate_passed(qa: dict[str, Any]) -> bool:
    """Use the typed identity contract, never a failure-code allow/deny list."""
    if "identity_contract_passed" in qa:
        return qa.get("identity_contract_passed") is True
    # Compatibility for test fixtures and historical scored rows. New v17
    # keyframes always carry ``identity_contract_passed``.
    return bool(
        qa.get("status") == "scored"
        and not qa.get("qa_recovered")
        and qa.get("face_identity") is not None
    )


async def review_keyframe_geometry_guard(
    candidate_b64: str,
    *,
    contract: dict[str, Any],
    visible_characters: list[str],
) -> dict[str, Any]:
    """独立复核多人关键帧的身高、体型与强透视，避免通用 QA 自证通过。"""
    policy = str(contract.get("relative_height_policy") or "single_subject")
    expectation = {
        "task": "Strict independent human-scale geometry audit. Inspect only visible facts in this candidate image.",
        "characters": visible_characters,
        "relative_height_policy": policy,
        "explicit_height_evidence": list(contract.get("height_difference_evidence") or []),
        "instructions": [
            "First identify each named person's posture: standing, sitting, kneeling, leaning, or unclear.",
            "Estimate apparent full-body height for each person from head top to supporting foot/ground point; "
            "report max_height_div_min_height even if a foot is cropped, using a conservative lower bound.",
            "Judge whether the people occupy the same interaction/depth plane and whether forced perspective is "
            "making one a giant or miniature.",
            "A teenager rendered with child/toddler body scale, oversized head, narrow child shoulders, or a head "
            "below the other standing teenager/adult's shoulder is a childlike_body_scale_mismatch.",
            "For equal_scale, two upright co-present teens/adults must use approximately the same canonical skeleton "
            "scale. If apparent standing-height ratio exceeds 1.25 without an explicit seated/kneeling/depth reason, FAIL.",
            "Do not infer that a large height gap is acceptable merely because the text calls one character a boy "
            "and the other a girl. Do not repeat another QA score; make an independent visual measurement.",
        ],
        "output_schema": {
            "postures": [{"character": "name", "posture": "standing|sitting|kneeling|leaning|unclear"}],
            "same_depth_plane": True,
            "max_height_div_min_height": 1.0,
            "childlike_body_scale_mismatch": False,
            "forced_perspective_scale_mismatch": False,
            "scripted_height_relation_match": True,
            "confidence": 1.0,
            "verdict": "pass|fail",
            "issues": [],
        },
        "rule_version": "keyframe_geometry_guard_v1",
    }
    try:
        raw = await hiagent.vlm_check(
            [candidate_b64],
            json.dumps(expectation, ensure_ascii=False),
            call_meta={
                "initiator_label": "关键帧身高体型硬复核",
                "character_count": len(visible_characters),
                "relative_height_policy": policy,
            },
        )
        from app.schemas import extract_json
        data = extract_json(raw)
        ratio = float(data.get("max_height_div_min_height"))
        confidence = float(data.get("confidence"))
        postures = data.get("postures")
        childlike = data.get("childlike_body_scale_mismatch")
        forced = data.get("forced_perspective_scale_mismatch")
        relation = data.get("scripted_height_relation_match")
        verdict = str(data.get("verdict") or "").strip().lower()
        if not (0.0 < ratio < 10.0) or not (0.0 <= confidence <= 1.0):
            raise ValueError("invalid geometry measurements")
        if not isinstance(postures, list) or len(postures) < min(2, len(visible_characters)):
            raise ValueError("missing posture observations")
        if not isinstance(childlike, bool) or not isinstance(forced, bool) or not isinstance(relation, bool):
            raise ValueError("missing geometry booleans")
        if verdict not in {"pass", "fail"}:
            raise ValueError("missing geometry verdict")
        same_depth = data.get("same_depth_plane")
        ratio_failure = (
            policy == "equal_scale"
            and ratio > 1.25
            and same_depth is not False
        )
        passed = (
            verdict == "pass"
            and confidence >= 0.75
            and not childlike
            and not forced
            and relation
            and not ratio_failure
        )
        return {
            "status": "verified",
            "passed": passed,
            "postures": postures,
            "same_depth_plane": same_depth,
            "max_height_div_min_height": ratio,
            "childlike_body_scale_mismatch": childlike,
            "forced_perspective_scale_mismatch": forced,
            "scripted_height_relation_match": relation,
            "confidence": confidence,
            "verdict": verdict,
            "issues": [str(item) for item in (data.get("issues") or []) if str(item).strip()],
            "rule_version": "keyframe_geometry_guard_v1",
        }
    except Exception as exc:  # noqa: BLE001 独立硬复核失败时不得伪装成通过
        return {
            "status": "unverified",
            "passed": False,
            "issues": [f"身高体型硬复核未完成：{type(exc).__name__}"],
            "rule_version": "keyframe_geometry_guard_v1",
        }


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
    screenplay=None,
) -> dict[str, Any]:
    """关键帧证据化 QA：候选图 + 人物/场景真值图对照。"""
    if not visual_evidence_qa_enabled():
        # 回退：无证据时仍打分，但不伪装满分
        from app.video_modes import review_reference_image
        qa = await review_reference_image(
            candidate_b64,
            shot=shot,
            bible=bible,
            ref_type=ref_type,
            screenplay=screenplay,
        )
        qa.setdefault("status", "scored")
        qa["identity_contract_passed"] = False
        qa["identity_contract_unverified_reason"] = "visual_evidence_qa_disabled"
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
    from app.compiler import keyframe_visual_contract

    contract = keyframe_visual_contract(shot, bible, screenplay=screenplay)
    target_contact_phase = str(contract.get("target_contact_phase") or "none")
    contact_phase_required = target_contact_phase in {"approach", "established", "separated"}
    by_name = {c.name: c for c in getattr(bible, "characters", []) or []}
    anchors_txt = []
    if screenplay is not None and getattr(screenplay, "narrative_plan", None) is not None:
        from app.identity_contracts import narrative_identity_resolver

        resolver = narrative_identity_resolver(bible, screenplay)
        anchors_txt.extend(
            f"{name}: {resolver.visual_anchor(str(name))}"
            for name in contract.get("visible_characters") or []
        )
    else:
        from app.character_policy import (
            collective_role_anchor,
            functional_extra_anchor,
            is_collective_role,
            is_functional_extra,
            typed_functional_identity_names,
        )

        declared_functional_names = typed_functional_identity_names(
            screenplay,
        )
        for name in contract.get("visible_characters") or []:
            if name in by_name:
                anchors_txt.append(f"{name}: {by_name[name].appearance_canonical}")
            elif is_collective_role(str(name)):
                anchors_txt.append(f"{name}: {collective_role_anchor(str(name))}")
            elif (
                is_functional_extra(str(name))
                or str(name) in declared_functional_names
            ):
                functional_anchor = functional_extra_anchor(
                    str(name),
                    declared_functional_names=declared_functional_names,
                )
                anchors_txt.append(f"{name}: {functional_anchor}")

    geometry_requirements = [
        f"唯一目标定格：{contract.get('target_keyframe_desc') or getattr(shot, 'action_desc', '')}",
        f"计划机位：{contract.get('camera_angle') or '平视'}",
    ]
    required_text = getattr(shot, "required_text", None)
    required_text_expected = bool(contract.get("required_text_expected"))
    required_text_payload = (
        required_text.model_dump()
        if required_text_expected and required_text is not None and hasattr(required_text, "model_dump")
        else None
    )
    if contract.get("collective_presence_forbidden"):
        geometry_requirements.append("目标定格明示人群已离场/在画外/仅属回忆：画面中不得出现人群，不得把画外声变成可见人物")
    elif contract.get("collective_visible_roles"):
        geometry_requirements.append(
            "可见名单含叙事群体："
            + "、".join(str(name) for name in contract.get("collective_visible_roles") or [])
            + "；必须按目标的人数和主次表现为群体，不得缩成一人，不得复制具名角色长相"
        )
    elif contract.get("collective_presence_required"):
        geometry_requirements.append("目标定格明示需要人群/众人：必须按目标主次与数量画出匿名群体，不得缺失、也不得替代或复制焦点角色")
    elif contract.get("anonymous_background_allowed"):
        geometry_requirements.append("环境语义允许匿名背景人群，但本定格不强制入画；若出现，不得抢占焦点或复制具名角色")
    else:
        geometry_requirements.append("不得添加名单外的可辨识焦点人物")
    if required_text_expected:
        geometry_requirements.append(
            "唯一允许的画面文字为「"
            + str(required_text_payload.get("exact_text") or "").strip()
            + "」，位于"
            + str(required_text_payload.get("surface") or "指定表面")
            + "；其他文字/乱码均不允许"
        )
    else:
        geometry_requirements.append("该目标定格时刻不应出现画面文字、字幕或乱码")
    if contract.get("contact_camera_required"):
        geometry_requirements.append(
            "本镜必须从互动轴侧面拍摄，互动区清晰无遮挡，禁止正面站桩；"
            "人物身体/脸可为身份辨识自然转成 3/4 角度"
        )
    if contract.get("established_contact_required"):
        geometry_requirements.append(
            "目标定格中接触已成立：接触点必须真实连接、清晰可见，禁止手部悬空或留缝"
        )
    elif contract.get("target_contact_phase") == "separated":
        geometry_requirements.append("目标是松开/分离后的状态：必须保留已分开的空隙与收回动作，不得重新连接")
    elif target_contact_phase == "approach":
        geometry_requirements.append("目标尚未建立接触：保留接近/未命中的距离，不得凭空改成已碰触")
    elif contract.get("contact_axis_inherited"):
        geometry_requirements.append(
            "该时序帧继承接触镜的侧面轴线，但当前目标未规定接触阶段；"
            "只评侧面机位，不得凭空添加或删除肢体接触"
        )
    if contract.get("relative_height_policy") == "equal_scale":
        geometry_requirements.append(
            "本镜无剧情身高差：同框青少年/成人的站直基准身高、头身比和骨架尺度必须一致。"
            "若当前两人均站立且剧情未写弯腰/跪/坐，双脚必须位于同一地面与景深平面，"
            "头顶、肩线、髋线与眼线应在自然小误差内齐平；“抬头/仰望/低头”只是头颈与视线动作，"
            "不是身高差证据。禁止儿童化、前景巨人、后景小人或强透视尺度差"
        )
    elif contract.get("relative_height_policy") == "preserve_explicit_difference":
        height_evidence = "；".join(
            str(item).strip() for item in (contract.get("height_difference_evidence") or []) if str(item).strip()
        )
        geometry_requirements.append(
            "仅保留剧情/人物锚点明示的身高差"
            + (f"（原文证据：{height_evidence}）" if height_evidence else "")
            + "，不得用强透视夸大"
        )
    scene_canonical = str(contract.get("scene_canonical") or "").strip()
    scene_landmarks = [
        str(item).strip() for item in (contract.get("scene_landmarks") or []) if str(item).strip()
    ]
    if scene_canonical or scene_landmarks:
        geometry_requirements.append(
            "固定场景几何必须匹配场景圣经；石碑、门、桌台、屏幕等永久地标不得缺失、复制、变形或换位。"
            + (f"场景锚点：{scene_canonical}" if scene_canonical else "")
            + (f"；显式地标：{'、'.join(scene_landmarks)}" if scene_landmarks else "")
        )
    geometry_requirements.append(
        "人物保持角色参考中的自然头身比；参考图裁切大小不代表真实头部大小，禁止大头化、幼态化或身体缩小"
    )

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
            "scene_canonical": contract.get("scene_canonical"),
            "scene_landmarks": contract.get("scene_landmarks"),
            "action": getattr(shot, "action_desc", ""),
            "target_keyframe_desc": contract.get("target_keyframe_desc"),
            "target_source": contract.get("target_source"),
            "target_contact_phase": contract.get("target_contact_phase"),
            "camera_angle": contract.get("camera_angle"),
            "visible_characters": list(contract.get("visible_characters") or []),
            "individual_visible_characters": list(contract.get("individual_visible_characters") or []),
            "identity_verification": dict(contract.get("identity_verification") or {}),
            "collective_visible_roles": list(contract.get("collective_visible_roles") or []),
            "anonymous_background_allowed": bool(contract.get("anonymous_background_allowed")),
            "collective_presence_required": bool(contract.get("collective_presence_required")),
            "collective_presence_forbidden": bool(contract.get("collective_presence_forbidden")),
            "required_text_expected": required_text_expected,
            "required_text": required_text_payload,
            "characters": anchors_txt,
            "style": getattr(getattr(bible, "world", None), "visual_style_canonical", ""),
        },
        "geometry_requirements": geometry_requirements,
        "dimensions": {
            "action_match": "唯一目标定格的姿态、朝向、手部/道具接触、人物间空间互动；不得改画首帧或中性摆拍",
            "body_proportion": "头身比、肢体长度、身体完整性，以及同框人物相对身高、眼线、体型尺度和透视深度",
            "side_view_match": "互动轴侧面机位是否清楚展示互动区且非正面站桩；非接触/趋近镜可返回 N/A",
            "contact_visibility": "已建立接触时，接触点是否真实连接、清晰可见且未遮挡；否则返回 N/A",
            "contact_phase_match": "是否严格匹配 target_contact_phase：established 必须连接，approach 必须留缝，separated 必须显示松开后的空隙",
            "relative_height_match": "是否符合 geometry_requirements 中的同高或明示身高差，且没有强透视夸大",
            "collective_presence_match": "有叙事群体时，是否按目标数量/主次/动作以群体出现，而非缩成单人；无群体返回 N/A",
            "required_text_match": "required_text_expected=true 时检查字面、承载面与样式；false 时目标帧禁字并返回 N/A",
            "style_match": "是否严格保持统一非真人 CG/动画/漫画画风，未切换成真人实拍、照片写实或 live-action 质感",
            "face_identity": "与人物锚点脸部特征一致；脸不可见时返回 null 或 N/A",
            "outfit_match": "款式颜色层次配饰与本集造型一致",
            "hair_match": "发型长度发色刘海轮廓一致",
            "scene_match": "几何标志物机位方向状态光线合理",
        },
        "watermark_policy": wm_note,
        "output_schema": {
            "action_match": 0.0,
            "body_proportion": 0.0,
            "side_view_match": 0.0 if contract.get("contact_camera_required") else "N/A",
            "contact_visibility": 0.0 if contract.get("established_contact_required") else "N/A",
            "contact_phase_match": 0.0 if contact_phase_required else "N/A",
            "relative_height_match": (
                0.0 if contract.get("relative_height_policy") != "single_subject" else "N/A"
            ),
            "required_text_match": 0.0 if required_text_expected else "N/A",
            "collective_presence_match": (
                0.0 if contract.get("collective_presence_required") or contract.get("collective_presence_forbidden")
                else "N/A"
            ),
            "style_match": 0.0,
            "face_identity": 0.0,
            "outfit_match": 0.0,
            "hair_match": 0.0,
            "scene_match": 0.0,
            "identity_contract": {
                "characters": [
                    {
                        "name": str(name),
                        "present": True,
                        "gender_match": True,
                        "identity_match": (
                            True
                            if (
                                (contract.get("identity_verification") or {})
                                .get(str(name), {})
                                .get("mode")
                                == "visual_anchor"
                            )
                            else "N/A"
                        ),
                        "text_contract_match": (
                            True
                            if (
                                (contract.get("identity_verification") or {})
                                .get(str(name), {})
                                .get("mode")
                                == "text_contract"
                            )
                            else "N/A"
                        ),
                        "outfit_match": True,
                        "instance_count": 1,
                    }
                    for name in (
                        contract.get("individual_visible_characters") or []
                    )
                ],
                "unexpected_recognizable_people": 0,
            },
            "anatomy_valid": None,
            "watermark_occluding": None,
            "photoreal_detected": None,
            "live_action_detected": None,
            "overall": 0.0,
            "hard_failures": [],
            "issues": [],
            "status": "scored",
        },
        "rule_version": "keyframe_geometry_qa_v3",
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
            "side_view_match": None,
            "contact_visibility": None,
            "contact_phase_match": None,
            "relative_height_match": None,
            "required_text_match": None,
            "collective_presence_match": None,
            "style_match": None,
            "face_identity": None,
            "outfit_match": None,
            "hair_match": None,
            "scene_match": None,
            "identity_contract_passed": False,
            "runtime_blocking": True,
            "blocking_facts": ["qa_unverified"],
            "hard_failures": [],
            "issues": [f"关键帧 QA 未完成：{type(exc).__name__}"],
            "qa_recovered": True,
            "image_manifest": image_manifest,
            "rule_version": "keyframe_geometry_qa_v3",
        }

    expected_identities = [
        str(name).strip()
        for name in (contract.get("individual_visible_characters") or [])
        if str(name).strip()
    ]
    identity_verification = dict(contract.get("identity_verification") or {})
    visual_anchor_identities = {
        name
        for name in expected_identities
        if (
            (identity_verification.get(name) or {}).get("mode")
            == "visual_anchor"
        )
    }
    available_identity_anchors = {
        str(item.get("entity") or "").strip()
        for item in manifest_entries
        if item.get("role") == "character_anchor"
        and str(item.get("entity") or "").strip()
    }
    identity_anchors_complete = visual_anchor_identities.issubset(
        available_identity_anchors
    )
    identity_payload = data.get("identity_contract")
    identity_rows = (
        identity_payload.get("characters")
        if isinstance(identity_payload, dict)
        and isinstance(identity_payload.get("characters"), list)
        else []
    )
    identity_by_name = {
        str(item.get("name") or "").strip(): item
        for item in identity_rows
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    try:
        unexpected_people = int(
            identity_payload.get("unexpected_recognizable_people", -1)
        ) if isinstance(identity_payload, dict) else -1
    except (TypeError, ValueError):
        unexpected_people = -1
    identity_contract_complete = (
        isinstance(identity_payload, dict)
        and identity_anchors_complete
        and set(identity_by_name) == set(expected_identities)
        and unexpected_people == 0
    )
    identity_contract_passed = identity_contract_complete and all(
        item.get("present") is True
        and item.get("gender_match") is True
        and item.get("outfit_match") is True
        and item.get("instance_count") == 1
        and (
            item.get("identity_match") is True
            if name in visual_anchor_identities
            else item.get("text_contract_match") is True
        )
        for name, item in identity_by_name.items()
    )
    data["identity_contract_passed"] = identity_contract_passed
    if not identity_contract_complete:
        identity_issue = "身份合同或人物真值锚点不完整"
        data["issues"] = [
            *list(data.get("issues") or []),
            identity_issue,
        ]

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
            if key in {"action_match", "body_proportion"}:
                missing_required = True
            data[key] = None
            continue
        try:
            data[key] = max(0.0, min(1.0, float(val)))
        except (TypeError, ValueError):
            data[key] = None
            if key in {"action_match", "body_proportion"}:
                missing_required = True

    for key in (
        "side_view_match", "contact_visibility", "contact_phase_match", "relative_height_match",
        "required_text_match", "collective_presence_match", "style_match",
    ):
        val = data.get(key)
        if val is None or (isinstance(val, str) and val.upper() in {"N/A", "NA", "NONE"}):
            data[key] = None
            continue
        try:
            data[key] = max(0.0, min(1.0, float(val)))
        except (TypeError, ValueError):
            data[key] = None

    required_diagnostics: list[str] = []
    if contract.get("contact_camera_required"):
        required_diagnostics.append("side_view_match")
    if contract.get("established_contact_required"):
        required_diagnostics.append("contact_visibility")
    if contact_phase_required:
        required_diagnostics.append("contact_phase_match")
    if contract.get("relative_height_policy") != "single_subject":
        required_diagnostics.append("relative_height_match")
    if required_text_expected:
        required_diagnostics.append("required_text_match")
    if contract.get("collective_presence_required") or contract.get("collective_presence_forbidden"):
        required_diagnostics.append("collective_presence_match")
    missing_diagnostics = [key for key in required_diagnostics if data.get(key) is None]

    if missing_required or missing_diagnostics:
        data["status"] = "unverified"
        data["overall"] = None
        data["qa_recovered"] = True
        issue = "缺少必需评分数"
        if missing_diagnostics:
            issue += "：" + ",".join(missing_diagnostics)
        data["issues"] = list(data.get("issues") or []) + [issue]
        data["hard_failures"] = list(data.get("hard_failures") or [])
        data["runtime_blocking"] = True
        data["blocking_facts"] = [
            *(f"{key}_missing" for key in missing_diagnostics),
            *(("required_score_missing",) if missing_required else ()),
        ]
        data["image_manifest"] = image_manifest
        data["rule_version"] = "keyframe_geometry_qa_v3"
        data["geometry_requirements"] = geometry_requirements
        data["target_keyframe_desc"] = contract.get("target_keyframe_desc")
        return data

    # 通用 QA 若声称多人身高通过，再交给一个只看骨架尺度/体型/强透视的
    # 独立审计。它不读取通用 QA 分数，专门拦截“少年被画成儿童、另一人像巨人”。
    if (
        contract.get("relative_height_policy") != "single_subject"
        and data.get("relative_height_match") is not None
        and float(data["relative_height_match"]) >= 0.7
    ):
        guard = await review_keyframe_geometry_guard(
            candidate_b64,
            contract=contract,
            visible_characters=[str(name) for name in (contract.get("visible_characters") or [])],
        )
        data["geometry_guard"] = guard
        hard_failures = [str(item) for item in (data.get("hard_failures") or [])]
        issues = [str(item) for item in (data.get("issues") or [])]
        if guard.get("status") != "verified":
            if "geometry_guard_unverified" not in hard_failures:
                hard_failures.append("geometry_guard_unverified")
            issues.extend(str(item) for item in (guard.get("issues") or []) if str(item) not in issues)
        elif not guard.get("passed"):
            data["relative_height_match"] = min(float(data["relative_height_match"]), 0.2)
            if "relative_scale_mismatch" not in hard_failures:
                hard_failures.append("relative_scale_mismatch")
            guard_issue = "独立身高体型复核失败：人物儿童化、身高比例异常或存在强透视"
            if guard_issue not in issues:
                issues.append(guard_issue)
            issues.extend(str(item) for item in (guard.get("issues") or []) if str(item) not in issues)
        data["hard_failures"] = hard_failures
        data["issues"] = issues

    overall = compute_weighted_overall(data, KEYFRAME_SCORE_WEIGHTS)
    data["overall"] = overall
    if not isinstance(data.get("issues"), list):
        data["issues"] = [str(data.get("issues"))] if data.get("issues") else []
    if not isinstance(data.get("hard_failures"), list):
        data["hard_failures"] = []
    reported_hard = [str(item) for item in data.get("hard_failures") or []]
    data["issues"] = list(dict.fromkeys([*data["issues"], *reported_hard]))
    blocking_facts: list[str] = []
    if not identity_contract_passed:
        blocking_facts.append("identity_contract_failed")
    for score_key in ["action_match", "body_proportion", *required_diagnostics, "style_match"]:
        score = data.get(score_key)
        if score is not None and float(score) < 0.7:
            blocking_facts.append(f"{score_key}_below_contract")
    if data.get("anatomy_valid") is False:
        blocking_facts.append("anatomy_contract_failed")
    if data.get("watermark_occluding") is True:
        blocking_facts.append("watermark_occludes_subject")
    if data.get("photoreal_detected") is True:
        blocking_facts.append("photoreal_medium_detected")
    if data.get("live_action_detected") is True:
        blocking_facts.append("live_action_medium_detected")
    data["blocking_facts"] = list(dict.fromkeys(blocking_facts))
    data["runtime_blocking"] = bool(data["blocking_facts"])
    data["hard_failures"] = list(data["blocking_facts"])
    data["status"] = "scored"
    data["image_manifest"] = image_manifest
    data["rule_version"] = "keyframe_geometry_qa_v3"
    data["geometry_requirements"] = geometry_requirements
    data["target_keyframe_desc"] = contract.get("target_keyframe_desc")
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


def _is_narrative_keyframe_slot(slot: str) -> bool:
    """识别旧版单关键帧槽与新版时序槽。"""
    value = (slot or "").strip()
    return value == NARRATIVE_KEYFRAME_SLOT or value.startswith(f"{NARRATIVE_KEYFRAME_SLOT}_")


def _ref_quality(ref: dict[str, Any]) -> float:
    try:
        value = ref.get("qualityScore")
        if value is None and isinstance(ref.get("qa"), dict):
            value = ref["qa"].get("overall")
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _optional_number(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _keyframe_sequence_key(ref: dict[str, Any]) -> tuple[float, float, float, str, str]:
    """关键帧只按冻结的剧情时序排列，不允许 QA 分数改变播放顺序。"""
    qa = ref.get("qa") if isinstance(ref.get("qa"), dict) else {}
    beat = ref.get("beat") or ref.get("keyframe_beat") or qa.get("keyframe_beat") or {}
    if not isinstance(beat, dict):
        beat = {}
    beat_index = _optional_number(
        ref.get("beat_index")
        if ref.get("beat_index") is not None
        else (ref.get("keyframe_index") if ref.get("keyframe_index") is not None else beat.get("beat_index"))
    )
    time_ratio = _optional_number(
        ref.get("time_ratio")
        if ref.get("time_ratio") is not None
        else (
            ref.get("keyframe_time_ratio")
            if ref.get("keyframe_time_ratio") is not None
            else beat.get("time_ratio")
        )
    )
    slot = str(ref.get("slot_key") or "")
    suffix: float | None = None
    if _is_narrative_keyframe_slot(slot):
        tail = slot[len(NARRATIVE_KEYFRAME_SLOT):].strip("_-")
        # 新槽位默认为 narrative_keyframe_01；兼容 _01_opening。
        first_token = tail.replace("-", "_").split("_", 1)[0] if tail else ""
        if first_token.isdigit():
            suffix = float(int(first_token))
    infinity = float("inf")
    return (
        beat_index if beat_index is not None else infinity,
        time_ratio if time_ratio is not None else infinity,
        suffix if suffix is not None else infinity,
        slot,
        str(ref.get("id") or ref.get("path") or ref.get("image_path") or ""),
    )


def ref_pack_priority(ref: dict[str, Any]) -> tuple[Any, ...]:
    """装箱稳定排序：连续帧、剧情帧、道具、场景、人物、风格。"""
    rtype = str(ref.get("type") or "")
    purposes = set(purpose_list(ref))
    slot = str(ref.get("slot_key") or "")
    if rtype == "previous_shot_frame" or "previous_shot" in str(ref.get("source") or ""):
        tier = 0
    elif rtype == ASSET_TYPE_PLOT_KEY_FRAME or _is_narrative_keyframe_slot(slot) or "keyframe" in purposes:
        tier = 1
    elif rtype == "prop":
        tier = 2
    elif rtype == "scene":
        tier = 3
    elif rtype == "character":
        tier = 4
    elif rtype == "style":
        tier = 5
    else:
        tier = 6
    if tier == 1:
        return (tier, *_keyframe_sequence_key(ref))
    return (tier, -_ref_quality(ref), str(ref.get("id") or ""))


def pack_references_by_purpose(
    refs: list[dict[str, Any]],
    *,
    max_images: int,
    continuity_required: bool = False,
    char_limit: int = 1,
) -> list[dict[str, Any]]:
    """按连续性价值装箱，确保稀缺槽位优先给动作与道具真值。

    上一镜尾帧与至少一张剧情关键帧是结构必需项；当 ``max_images``
    容得下两者时不可被人物/场景图挤掉。人物配额只统计 character
    资产，plot keyframe 即使含人物也不消耗该配额。
    """
    limit = max(0, int(max_images))
    if limit == 0:
        return []
    usable = []
    for r in refs:
        # 用途是资产血缘/装箱优先级，不是当前选择状态。QA 淘汰图会保留
        # video_input 用途供审计，因此必须以 selectedForSeedance 为准。
        if r.get("selectedForSeedance") and not r.get("deleted"):
            usable.append(r)
    if not usable:
        return []

    character_limit = max(0, int(char_limit))
    characters_seen = 0
    eligible: list[dict[str, Any]] = []
    for ref in sorted(usable, key=ref_pack_priority):
        if str(ref.get("type") or "") == "character":
            if characters_seen >= character_limit:
                continue
            characters_seen += 1
        eligible.append(ref)
    # continuity_required 保留 API 语义；有真实尾帧时它始终是最高优先级。
    _ = continuity_required
    return eligible[:limit]


def enrich_ref_dict_metadata(ref: dict[str, Any], **extra: Any) -> dict[str, Any]:
    out = dict(ref)
    out.update({k: v for k, v in extra.items() if v is not None})
    if "purposes" in out and isinstance(out["purposes"], list):
        out["purposes_json"] = json.dumps(out["purposes"], ensure_ascii=False)
    return out


def gallery_fingerprint_material(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    material = []
    for ref in refs:
        qa = ref.get("qa") if isinstance(ref.get("qa"), dict) else {}
        frozen_beat = ref.get("beat") or ref.get("keyframe_beat") or qa.get("keyframe_beat") or {}
        if not isinstance(frozen_beat, dict):
            frozen_beat = {}
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
            "slot_key": ref.get("slot_key"),
            "candidate_no": ref.get("candidate_no"),
            "beat_index": (
                ref.get("beat_index")
                if ref.get("beat_index") is not None
                else (
                    ref.get("keyframe_index")
                    if ref.get("keyframe_index") is not None
                    else frozen_beat.get("beat_index")
                )
            ),
            "beat_count": (
                ref.get("beat_count")
                if ref.get("beat_count") is not None
                else (
                    ref.get("keyframe_total")
                    if ref.get("keyframe_total") is not None
                    else frozen_beat.get("beat_total")
                )
            ),
            "time_ratio": (
                ref.get("time_ratio")
                if ref.get("time_ratio") is not None
                else (
                    ref.get("keyframe_time_ratio")
                    if ref.get("keyframe_time_ratio") is not None
                    else frozen_beat.get("time_ratio")
                )
            ),
            "time_s": (
                ref.get("time_s")
                if ref.get("time_s") is not None
                else (ref.get("keyframe_time_s") or frozen_beat.get("time_s"))
            ),
            "beat_role": ref.get("beat_role") or frozen_beat.get("phase"),
            "target_desc": (
                ref.get("target_desc")
                or ref.get("keyframe_target_desc")
                or ref.get("target_keyframe_desc")
                or frozen_beat.get("target_desc")
            ),
            "beat": frozen_beat or None,
            "keyframe_contract_fingerprint": ref.get("keyframe_contract_fingerprint"),
            "keyframe_sequence_fingerprint": ref.get("keyframe_sequence_fingerprint"),
            "purposes": purpose_list(ref),
            "qa_status": (ref.get("qa") or {}).get("status"),
            "qa_overall": (ref.get("qa") or {}).get("overall"),
        })
    return material


def is_plot_key_frame(ref: dict[str, Any] | Any) -> bool:
    if isinstance(ref, dict):
        return (
            str(ref.get("type") or "") == ASSET_TYPE_PLOT_KEY_FRAME
            or _is_narrative_keyframe_slot(str(ref.get("slot_key") or ""))
        )
    return (
        getattr(ref, "type", None) == ASSET_TYPE_PLOT_KEY_FRAME
        or _is_narrative_keyframe_slot(str(getattr(ref, "slot_key", None) or ""))
    )


# ---------- 零付费整包绑定 / 单视角重做 ----------

def clone_portrait_views(
    conn, *, source_portrait_id: str, dest_portrait_id: str,
) -> int:
    """把源造型包的全部视角零付费绑定到目标段（复用同一 image_path，不重新生成）。"""
    views = list_portrait_views(source_portrait_id, conn=conn)
    stamp = now()
    count = 0
    for v in views:
        view_id = new_id("pview")
        conn.execute(
            """INSERT INTO character_portrait_views(
                   id, portrait_id, view_role, framing, image_path, prompt, qa_json,
                   artifact_id, base_view_id, status, selected, input_fingerprint, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                view_id, dest_portrait_id, v.get("view_role"), v.get("framing"),
                v.get("image_path"), v.get("prompt"), v.get("qa_json"),
                v.get("artifact_id"), v.get("id"), v.get("status") or "ready",
                1, v.get("input_fingerprint"), stamp,
            ),
        )
        count += 1
    return count


def clone_scene_views(
    conn, *, source_scene_id: str, dest_scene_id: str,
) -> int:
    views = list_scene_views(source_scene_id, conn=conn)
    stamp = now()
    count = 0
    for v in views:
        view_id = new_id("sview")
        conn.execute(
            """INSERT INTO scene_reference_views(
                   id, scene_reference_id, view_role, camera_axis, image_path, prompt, qa_json,
                   artifact_id, base_view_id, status, selected, input_fingerprint, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                view_id, dest_scene_id, v.get("view_role"), v.get("camera_axis"),
                v.get("image_path"), v.get("prompt"), v.get("qa_json"),
                v.get("artifact_id"), v.get("id"), v.get("status") or "ready",
                1, v.get("input_fingerprint"), stamp,
            ),
        )
        count += 1
    return count


def bind_ready_portrait_reuse(
    conn, *,
    project_id: str,
    character_name: str,
    source_portrait_id: str,
    ep_start: int,
    bible_version: int,
) -> str:
    """仅本集造型结束后：把完整旧包零付费重新绑定为新开区间段（pack_status=ready）。"""
    src = conn.execute(
        "SELECT * FROM character_portraits WHERE id=?", (source_portrait_id,)
    ).fetchone()
    if not src:
        raise hiagent.ProviderError(f"无法复用旧造型包：{source_portrait_id}")
    reuse_id = new_id("portrait")
    group_qa = src["group_qa_json"] if "group_qa_json" in src.keys() else None
    conn.execute(
        """INSERT INTO character_portraits(
               id, project_id, character_name, ep_start, ep_end, appearance, prompt, image_path,
               base_portrait_id, bible_version, artifact_id, pack_status, group_qa_json, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            reuse_id, project_id, character_name, ep_start, None,
            src["appearance"], src["prompt"], src["image_path"],
            source_portrait_id, bible_version,
            src["artifact_id"] if "artifact_id" in src.keys() else None,
            PACK_STATUS_READY, group_qa, now(),
        ),
    )
    clone_portrait_views(conn, source_portrait_id=source_portrait_id, dest_portrait_id=reuse_id)
    return reuse_id


async def regenerate_character_view(
    *,
    project_id: str,
    portrait_id: str,
    view_role: str,
    visual_style: str | None = None,
) -> dict[str, Any]:
    """人物谱单视角重做：只重生成指定视角，再跑整包一致性；失败不切换其它视角。"""
    if view_role not in CHARACTER_REQUIRED_VIEWS + CHARACTER_OPTIONAL_VIEWS:
        raise hiagent.ProviderError(f"未知人物视角：{view_role}")
    conn = get_conn()
    row = conn.execute("SELECT * FROM character_portraits WHERE id=?", (portrait_id,)).fetchone()
    if not row or row["project_id"] != project_id:
        raise hiagent.ProviderError("造型版本不存在")
    proj = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    try:
        bible = json.loads(proj["bible_json"] or "{}") if proj else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        bible = {}
    style = visual_style or (bible.get("world") or {}).get("visual_style_canonical") or ""
    appearance = row["appearance"] or ""
    character = next(
        (
            item for item in bible.get("characters", [])
            if item.get("name") == row["character_name"]
        ),
        {},
    )
    latest_prompt = (
        effective_portrait_prompt(
            style,
            character.get("appearance_canonical") or appearance,
            (character.get("portrait_prompt_override") or "").strip() or None,
        )
        or (row["prompt"] or "").strip()
    )
    existing = {v["view_role"]: v for v in list_portrait_views(portrait_id, conn=conn)}
    front = existing.get("front_full") or {}
    seeds = []
    if view_role != "front_full" and front.get("image_path") and Path(front["image_path"]).exists():
        seeds.append(hiagent.data_url_from_file(front["image_path"]))
    prompt = character_view_prompt(style, appearance, view_role, latest_prompt)
    path = _view_path(project_id, "character", row["character_name"], view_role, row["ep_start"])
    fp = view_input_fingerprint(
        view_role=view_role, prompt=prompt, anchor_text=latest_prompt,
        parent_revision_id=portrait_id,
        seed_hint=f"{front.get('image_path') or ''}|redo:{Path(path).name}",
    )
    item = await _generate_image(
        prompt, seed_inputs=seeds or None,
        call_meta={"asset_kind": "character_view_redo", "view_role": view_role,
                   "character_name": row["character_name"]},
    )
    await _save_image_item(item, path)
    qa = await review_character_view(path, latest_prompt, view_role)
    qa = {
        **qa,
        "evaluation_role": "score_only",
        "runtime_blocking": False,
        "retry_eligible": False,
    }
    # Score-only：技术有效图即可替换；QA 低分不丢弃候选（PRD QA-SO #17）。
    if not Path(path).exists():
        return {"status": "failed", "view_role": view_role, "qa": qa, "preserved_previous": True}

    candidate = dict(existing.get(view_role) or {})
    candidate.update({
        "view_role": view_role, "image_path": path, "prompt": prompt,
        "qa_json": json.dumps(qa, ensure_ascii=False), "status": "ready",
        "input_fingerprint": fp,
    })
    candidate_views = [candidate if v.get("view_role") == view_role else v for v in existing.values()]
    if view_role not in existing:
        candidate_views.append(candidate)
    required = [v for v in candidate_views if v.get("view_role") in CHARACTER_REQUIRED_VIEWS]
    missing = missing_required_views(candidate_views, CHARACTER_REQUIRED_VIEWS)
    if missing:
        _discard_rejected_candidate(path)
        return {
            "status": "failed", "view_role": view_role, "missing_required": missing,
            "preserved_previous": True,
        }

    group_qa = await review_character_pack_consistency(required, latest_prompt)
    group_qa = {
        **group_qa,
        "evaluation_role": "score_only",
        "runtime_blocking": False,
        "retry_eligible": False,
    }

    view_id = _upsert_character_view(
        conn, portrait_id=portrait_id, view_role=view_role,
        framing="closeup" if view_role == "face_closeup" else ("full_body" if view_role.endswith("full") else "half_or_full"),
        image_path=path, prompt=prompt, qa=qa, artifact_id=None,
        base_view_id=(existing.get(view_role) or {}).get("id"),
        status="ready", fingerprint=fp,
    )
    if view_role == "front_full":
        conn.execute("UPDATE character_portraits SET image_path=? WHERE id=?", (path, portrait_id))
    _set_portrait_pack_fields(
        conn, portrait_id, pack_status=PACK_STATUS_READY,
        group_qa_json=json.dumps(group_qa, ensure_ascii=False),
    )
    conn.commit()
    return {"status": "ready", "view_role": view_role, "view_id": view_id, "group_qa": group_qa}


async def regenerate_scene_view(
    *,
    project_id: str,
    scene_reference_id: str,
    view_role: str,
    visual_style: str | None = None,
) -> dict[str, Any]:
    """场景库单视角重做。"""
    if view_role not in SCENE_REQUIRED_VIEWS + SCENE_OPTIONAL_VIEWS:
        raise hiagent.ProviderError(f"未知场景视角：{view_role}")
    conn = get_conn()
    row = conn.execute("SELECT * FROM scene_references WHERE id=?", (scene_reference_id,)).fetchone()
    if not row or row["project_id"] != project_id:
        raise hiagent.ProviderError("场景版本不存在")
    style = visual_style or ""
    if not style:
        proj = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        try:
            style = (json.loads(proj["bible_json"] or "{}").get("world") or {}).get("visual_style_canonical") or ""
        except (TypeError, ValueError, json.JSONDecodeError):
            style = ""
    canonical = row["scene_canonical"] or ""
    if "state_canonical" in row.keys() and row["state_canonical"]:
        canonical = row["state_canonical"]
    existing = {v["view_role"]: v for v in list_scene_views(scene_reference_id, conn=conn)}
    est = existing.get("establishing") or {}
    seeds = []
    if view_role != "establishing" and est.get("image_path") and Path(est["image_path"]).exists():
        seeds.append(hiagent.data_url_from_file(est["image_path"]))
    prompt = scene_view_prompt(style, canonical, view_role)
    path = _view_path(project_id, "scene", row["scene_name"], view_role, row["ep_start"])
    fp = view_input_fingerprint(
        view_role=view_role, prompt=prompt, anchor_text=canonical,
        parent_revision_id=scene_reference_id,
        seed_hint=f"{est.get('image_path') or ''}|redo:{Path(path).name}",
    )
    item = await _generate_image(
        prompt, seed_inputs=seeds or None,
        call_meta={"asset_kind": "scene_view_redo", "view_role": view_role, "scene_name": row["scene_name"]},
    )
    await _save_image_item(item, path)
    qa = await review_scene_view(path, canonical, view_role)
    qa = {
        **qa,
        "evaluation_role": "score_only",
        "runtime_blocking": False,
        "retry_eligible": False,
    }
    # Score-only：技术有效图即可替换；QA 低分不丢弃候选（PRD QA-SO #17/#21）。
    if not Path(path).exists():
        return {"status": "failed", "view_role": view_role, "qa": qa, "preserved_previous": True}

    candidate = dict(existing.get(view_role) or {})
    candidate.update({
        "view_role": view_role, "image_path": path, "prompt": prompt,
        "qa_json": json.dumps(qa, ensure_ascii=False), "status": "ready",
        "input_fingerprint": fp,
    })
    candidate_views = [candidate if v.get("view_role") == view_role else v for v in existing.values()]
    if view_role not in existing:
        candidate_views.append(candidate)
    previous_group = {}
    try:
        previous_group = json.loads(row["group_qa_json"] or "{}") if "group_qa_json" in row.keys() else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        previous_group = {}
    required_roles = list(previous_group.get("required_views") or SCENE_REQUIRED_VIEWS)
    if view_role == "action_zone" and "action_zone" not in required_roles:
        required_roles.append("action_zone")
    required = [v for v in candidate_views if v.get("view_role") in required_roles]
    missing = missing_required_views(candidate_views, tuple(required_roles))
    if missing:
        _discard_rejected_candidate(path)
        return {
            "status": "failed", "view_role": view_role, "missing_required": missing,
            "preserved_previous": True,
        }

    group_qa = await review_scene_pack_consistency(required, canonical)
    group_qa = {
        **group_qa,
        "evaluation_role": "score_only",
        "runtime_blocking": False,
        "retry_eligible": False,
    }

    view_id = _upsert_scene_view(
        conn, scene_reference_id=scene_reference_id, view_role=view_role,
        camera_axis="establishing" if view_role == "establishing" else (
            "reverse" if view_role == "reverse_angle" else "action"),
        image_path=path, prompt=prompt, qa=qa, artifact_id=None,
        base_view_id=(existing.get(view_role) or {}).get("id"),
        status="ready", fingerprint=fp,
    )
    if view_role == "establishing":
        conn.execute("UPDATE scene_references SET image_path=? WHERE id=?", (path, scene_reference_id))
    _set_scene_pack_fields(
        conn, scene_reference_id, pack_status=PACK_STATUS_READY,
        group_qa_json=json.dumps(group_qa, ensure_ascii=False),
    )
    conn.commit()
    return {"status": "ready", "view_role": view_role, "view_id": view_id, "group_qa": group_qa}


# ---------- 高风险视频抽帧 ----------

HIGH_RISK_QA_TAGS = frozenset({
    "duration_gt5_needs_review",
    "identity_risk",
    "occlusion_risk",
    "crowd_risk",
    "action_complex",
    "multi_character",
    "high_risk_qa",
    "complex_action",
    "occlusion",
})


def shot_needs_high_risk_frame_sample(shot: Any) -> bool:
    """高风险镜头：duration>5 或 risk_tags 命中时，视频 QA 抽五帧。"""
    tags = {str(t).strip() for t in (getattr(shot, "risk_tags", None) or []) if str(t).strip()}
    if tags & HIGH_RISK_QA_TAGS:
        return True
    try:
        if int(getattr(shot, "duration_s", 0) or 0) > 5:
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(shot, dict):
        try:
            raw = shot.get("risk_tags")
            if isinstance(raw, str):
                raw = json.loads(raw)
            tags = {str(t).strip() for t in (raw or []) if str(t).strip()}
            if tags & HIGH_RISK_QA_TAGS:
                return True
            if int(shot.get("duration_s") or 0) > 5:
                return True
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return False


def video_qa_sample_positions(*, high_risk: bool = False) -> tuple[float, ...]:
    """返回 0~1 相对时间点。普通三帧；高风险五帧（0/25/50/75/95%）。"""
    if high_risk:
        return (0.0, 0.25, 0.50, 0.75, 0.95)
    return (0.0, 0.50, 0.97)
