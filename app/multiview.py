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
from app.portraits.card_owner import resolve_card_owner
from app.portraits.current_ref import current_portrait_ref
from app.refs import (
    _safe_name,
    character_visual_style_lock,
    effective_portrait_prompt,
    ensure_portrait_clothing_contract,
    portrait_override_appearance_anchor,
    production_appearance_anchor,
    scene_visual_style_lock,
)
from app.validators import match_scene_name

# ---------- 视角角色常量 ----------

CHARACTER_REQUIRED_VIEWS = ("front_full", "three_quarter", "profile")
CHARACTER_OPTIONAL_VIEWS = ("back_full", "face_closeup")
SCENE_REQUIRED_VIEWS = ("establishing", "reverse_angle")
SCENE_OPTIONAL_VIEWS = ("action_zone",)

# 每条视角的构图合同：是否要求全身入画，以及写进提示词的构图要求。
# 生成提示词和 QA 判据必须读同一份。真实故障：profile 的提示词要「标准左侧面
# 半身」，而 portrait_policy 无条件把 full_body_visible=False 判成硬失败——按半身
# 要求画出来的图因此必然挂。四个项目 21 个角色实测 9 条硬失败全部落在 profile 上，
# front_full 一条没有；只有模型没听话、多画了全身的那几张才侥幸通过。
CHARACTER_VIEW_FRAMING: dict[str, tuple[bool, str]] = {
    "front_full": (True, "正面全身立绘，中性姿态，双臂自然，全身完整可见"),
    "three_quarter": (False, "3/4 侧面半身或全身，清晰展示五官深度与发型轮廓"),
    "profile": (False, "标准左侧面半身，清晰展示鼻梁、下颌、耳部与侧面发型"),
    "back_full": (True, "背面全身，展示服装背面与发型背部轮廓"),
    "face_closeup": (False, "面部近景特写，五官清晰，发型完整入画"),
}
_DEFAULT_VIEW_FRAMING = (True, "全身立绘")


def character_view_requires_full_body(view_role: str | None) -> bool:
    """这条视角的生成合同要不要求全身入画。

    未知视角按「要求全身」处理：新视角没登记时宁可误报一次，也好过悄悄放行一张
    真的被腰斩的图。
    """
    return CHARACTER_VIEW_FRAMING.get(str(view_role or ""), _DEFAULT_VIEW_FRAMING)[0]

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
    framing = CHARACTER_VIEW_FRAMING.get(view_role, _DEFAULT_VIEW_FRAMING)[1]
    source = ensure_portrait_clothing_contract(
        portrait_override_appearance_anchor(appearance, portrait_prompt)
        or production_appearance_anchor(appearance)
    )
    return (
        f"{character_visual_style_lock(visual_style)}。"
        f"角色外观真值锚点：{source}。"
        "外观补充与全局画风是两个独立合同；冲突时全局画风优先，"
        "不得按外观文案关键词删除或重写内容。"
        f"生成同一角色多视角定妆照（{VIEW_ROLE_LABELS.get(view_role, view_role)}）。"
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


def scene_multiview_generation_anchor(
    scene_canonical: str,
    parent_prompt: str | None,
) -> str:
    """Continue from the exact prompt that successfully produced the parent.

    The approved canonical remains the QA authority.  A parent prompt may be
    an equivalent provider-compatible representation produced after a
    technical failure; feeding the original representation back into every
    derived view would repeat that failure and strand an otherwise usable
    primary image.
    """
    return str(parent_prompt or "").strip() or str(scene_canonical or "").strip()


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


def scene_primary_is_usable(row, views: list[dict[str, Any]]) -> bool:
    """场景可用资格只看主图文件是否存在（用户拍板 2026-09-01：有图就是可用）。

    额外视角缺失或 QA 低分不得使已有 establishing 图失效。曾经并列存在的
    ``scene_pack_is_usable``（要求 establishing+reverse_angle 齐全才算可用）已随
    同一次拍板退场：它把"只出了主图"的场景判成不可用，既让场景库显示"不可用"，
    又让出图流程把整张主图作废重来（真实事故 2026-09-01「赵国大青山山顶」堆了 8
    张候选）。判据只剩这一条，不留第二套。
    """
    del views
    if not row:
        return False
    path = row["image_path"] if "image_path" in row.keys() else None
    return bool(path and Path(path).exists())


# ---------- 视角选择（镜头级） ----------

def select_character_view_roles(shot: Any, character_name: str) -> list[str]:
    """只按结构化接触阶段选视角；画面文案不参与路由。"""
    from app.compiler import has_contact_action

    _ = character_name
    return (
        ["profile", "three_quarter"]
        if has_contact_action(shot)
        else ["front_full", "three_quarter"]
    )


def select_scene_view_roles(shot: Any) -> list[str]:
    _ = shot
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
    additional_scenes: list[dict[str, Any]] | None = None,
    keyframe_slot: str = NARRATIVE_KEYFRAME_SLOT,
) -> dict[str, Any]:
    """``scene`` 是主场景（沿用既有单场景形状，兼容全部现有调用方的读法）；
    ``additional_scenes`` 是同一段声明的第二个及以后的场景——多场景转场镜头
    （分镜台 2.0.0 一段 = 多镜，段内可以转场到另一地点）才会非空。旧调用方
    忽略这个字段没有风险：它们本来就只关心「有没有一个可用场景」。"""
    payload = {
        "episode_no": episode_no,
        "shot_id": shot_id,
        "characters": characters,
        "scene": scene,
        "additional_scenes": list(additional_scenes or []),
        "keyframe_slot": keyframe_slot,
    }
    payload["input_fingerprint"] = fingerprint_payload(payload)
    return payload


def _storyboard_pack_asset_dependencies(
    *, project_id: str, episode_no: int, shot_id: str, segment: dict[str, Any],
    conn: Any, bible: Any,
) -> dict[str, Any]:
    """分镜台 2.0.0 段的资源依赖：要不要参考图挂人物谱/场景库的卡（现查
    ``resolve_card_owner`` / ``match_scene_name``，不认前缀字符串），图本身
    按集号现查（``current_portrait_ref`` / 本文件 ``scene_row_for_episode``，
    与展示侧同源）——都不读段落自己固化的 ``portrait_id`` /
    ``scene_reference_id`` 快照。旧版 ``asset_required=bool(portrait_id)``：
    出图解耦到后台后映射那一刻新角色/场景多半还没出图，快照恒 null，于是被
    判"不需要参考图"，视频生成拿不到脸、人物每镜漂移（EP2 实证：小胖子定妆
    照生成前 1 分钟就已落库，只因快照是 null 被判不需要）。``entity:`` 前缀
    的群演查无此人，天然 ``asset_required=False``；已建卡但图还没出来时
    ``missing_required`` 现在会真正非空，``manifest_production_blockers``
    拦得住。下面 name-based 分支给旧架构按名字重挑视角，这类行没有那些
    契约字段不适用，这里仍不经过视角选择，直接信任分镜台已做的资源归属。
    ``bible`` 必填：判断"有没有卡"是所有权问题，猜不得。
    """
    if bible is None:
        raise ValueError("分镜包资产依赖解析缺少 Bible")
    resources = segment.get("resources") or {}

    def _display_name(identity_or_scene_id: str) -> str:
        return str(identity_or_scene_id).split(":", 1)[-1] if identity_or_scene_id else ""

    characters_out: list[dict[str, Any]] = []
    for entry in resources.get("characters") or []:
        identity_id = str(entry.get("identity_id") or "")
        name = _display_name(identity_id) or identity_id
        has_card = resolve_card_owner(bible, name)[0] != "none"
        current = current_portrait_ref(
            project_id, name, episode_no, visual_entity_id=identity_id, conn=conn,
        ) if has_card else None
        portrait_id = current["portrait_id"] if current else None
        image_path = current["image_path"] if current else ""
        usable = current is not None
        selected_view = {
            "id": portrait_id,
            "view_role": "front_full",
            "image_path": image_path,
            "input_fingerprint": portrait_id,
            "purposes": [PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR, PURPOSE_VIDEO_INPUT],
        } if usable else None
        characters_out.append({
            "name": name,
            "identity_id": identity_id,
            "asset_name": name,
            "role_kind": "storyboard_pack",
            "asset_required": has_card,
            "look_revision_id": portrait_id,
            "pack_status": PACK_STATUS_READY if usable else None,
            "selected_view_ids": [portrait_id] if selected_view else [],
            "selected_views": [selected_view] if selected_view else [],
            "available_view_roles": ["front_full"] if selected_view else [],
            "missing_required": [] if (selected_view or not has_card) else ["front_full"],
        })

    def _resolve_scene_entry(scene_entry: dict[str, Any]) -> dict[str, Any]:
        sname = _display_name(str(scene_entry.get("scene_id") or ""))
        scenes = getattr(bible, "scenes", None) or []
        has_card = bool(sname and match_scene_name(sname, scenes, allow_fuzzy=False))
        row = scene_row_for_episode(project_id, sname, episode_no, conn=conn) if has_card else None
        scene_reference_id = row["id"] if row else None
        image_path = str(row["image_path"] or "") if row else ""
        usable = bool(image_path) and Path(image_path).is_file()
        selected_view = {
            "id": scene_reference_id,
            "view_role": "establishing",
            "image_path": image_path,
            "input_fingerprint": scene_reference_id,
            "purposes": [PURPOSE_KEYFRAME_SEED, PURPOSE_QA_ANCHOR, PURPOSE_VIDEO_INPUT],
        } if usable else None
        return {
            "name": sname,
            "asset_required": has_card,
            "scene_revision_id": scene_reference_id,
            "pack_status": PACK_STATUS_READY if usable else None,
            "asset_usable": usable,
            "pack_usable": usable,
            "primary_usable": usable,
            "selected_view_ids": [scene_reference_id] if selected_view else [],
            "selected_views": [selected_view] if selected_view else [],
            "available_view_roles": ["establishing"] if selected_view else [],
            "missing_required": [] if (selected_view or not has_card) else ["establishing"],
        }

    # 一段可以在中途转场到第二个（甚至更多）场景——之前这里写死只取
    # scene_entries[0]，多场景转场镜实测（EP2 段2/shot_53d87e5d107d，两个
    # scene 声明）只挂上第一个，第二个连同它的参考图完全消失，没有任何可
    # 见信号。这里改成解析全部声明：第一个仍作为 ``scene``（兼容读它当单
    # 场景用的既有调用方——review-wall 门禁、QA 锚点选择等只关心"有没有
    # 可用场景"，这些语义对多场景镜头里的主场景仍然成立），第二个及以后
    # 放进 ``additional_scenes`` 交给 library_anchor_assets_from_manifest
    # 一并展开进参考图列表；受 Seedance 协议张数上限截断的降级标记见
    # build_seedance_image_inputs。
    scene_entries = resources.get("scenes") or []
    scene_outs = [_resolve_scene_entry(entry) for entry in scene_entries]
    scene_out = scene_outs[0] if scene_outs else None
    additional_scenes = scene_outs[1:]

    return build_reference_manifest(
        episode_no=episode_no,
        shot_id=shot_id,
        characters=characters_out,
        scene=scene_out,
        additional_scenes=additional_scenes,
    )


def resolve_shot_asset_dependencies(
    *,
    project_id: str,
    episode_no: int,
    shot_id: str,
    shot: Any,
    conn: Any,
    scene_name: str | None = None,
    ready_only: bool = True,
    bible=None,
    screenplay=None,
) -> dict[str, Any]:
    """解析本镜人物/场景多视角依赖，供关键帧生成与 QA 冻结。

    生产路径默认 ready_only=True：非 ready 视角不得进入依赖与后续生成。

    ``conn`` 必填、不留默认值：分镜包分支（见下方短路）对 conn 是硬依赖
    （直接按 ID 查 character_portraits / scene_references，无条件
    ``conn.execute(...)``），name-based 旧分支下面也已经把 conn 当成真正
    要用的连接而不是可选缓存。曾经的 ``conn=None`` 默认值就是 ERR-
    20260826-99049d 事故的根因：调用方漏传时不会在调用点报错，而是拖到
    三层深处才炸出一个无法定位的 AttributeError。这里刻意不回退到
    ``get_conn()``——那个函数按 asyncio task 缓存连接，拿到的可能不是调用
    方正处在事务里的那个连接，会读到不一致状态，而且会把「少传一个参数」
    这个缺陷永久藏起来，不再有任何信号。漏传现在必须在调用那一刻就是
    TypeError。
    """
    # 分镜台 2.0.0 段落分支不做 ready_only 门禁（见下方短路说明），该形参只
    # 供下面 name-based 旧分支使用。
    segment = getattr(shot, "storyboard_pack_segment", None)
    if segment is not None:
        return _storyboard_pack_asset_dependencies(
            project_id=project_id, episode_no=episode_no, shot_id=shot_id,
            segment=segment, conn=conn, bible=bible,
        )
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
        usable = scene_primary_is_usable(row, all_views)
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
            "primary_usable": usable,
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
            # 缺口只报"有没有主图"，不报缺侧视角——口径与分镜包分支一致。
            "missing_required": [] if usable else ["establishing"],
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
    scenes = [manifest.get("scene") or {}, *(manifest.get("additional_scenes") or [])]
    for scene in scenes:
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
    scenes = [manifest.get("scene") or {}, *(manifest.get("additional_scenes") or [])]
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        name = str(scene.get("name") or "")
        for view in scene.get("selected_views") or []:
            role = str(view.get("view_role") or "")
            fp = str(view.get("input_fingerprint") or "")
            if name and role and fp:
                out[("scene", name, role)] = fp
    return out


def _manifest_scenes_asset_required(manifest: dict[str, Any] | None) -> dict[str, bool]:
    scenes = [
        (manifest or {}).get("scene") or {},
        *((manifest or {}).get("additional_scenes") or []),
    ]
    return {
        str(scene.get("name") or ""): bool(scene.get("asset_required", True))
        for scene in scenes
        if isinstance(scene, dict)
    }


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
        and _manifest_scenes_asset_required(frozen) == _manifest_scenes_asset_required(current)
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
    # 主场景 + 多场景转场镜头的第二个及以后（见 build_reference_manifest 的
    # additional_scenes 说明）：同一套检查逐个套用，任何一个没挂上参考图都
    # 要在这里冒出来——这是"可见不拦截"的降级信号通道，assert_manifest_
    # allows_production 只把它们记进 warnings，从不用它们挡生产。
    scenes = [manifest.get("scene"), *(manifest.get("additional_scenes") or [])]
    for scene in scenes:
        if not (isinstance(scene, dict) and scene.get("name")):
            continue
        name = scene.get("name")
        if not scene.get("scene_revision_id"):
            if scene.get("asset_required", True):
                blockers.append(f"场景「{name}」缺少本集场景版本")
        elif not (scene.get("selected_view_ids") or scene.get("selected_views")):
            # 有图就是可用（用户拍板 2026-09-01）：场景不再因 pack_status 未 ready
            # 或缺侧视角而被判成拦路项——那会让"主图明明在、生成也拿得到"的场景
            # 挡住整集付费生成。
            blockers.append(f"场景「{name}」没有可用的场景图")
    return blockers


def scan_episode_reference_asset_gaps(
    *,
    project_id: str,
    episode_no: int,
    shots: list[tuple[str, Any]],
    conn: Any,
    bible=None,
    screenplay=None,
) -> dict[str, Any]:
    """Read-only production preflight for the reusable assets used by an episode.

    ``conn`` 是必填形参，不给默认值：调用方（整集生成/补齐到全片预检）都是
    在已经拿到 conn 的路径上跑的，分镜包分支对 conn 是硬依赖（见
    ``resolve_shot_asset_dependencies`` 里的说明），留一个 None 默认值只会
    邀请下一个调用方重新踩中同一个三层深的 AttributeError。
    """
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
            conn=conn,
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
        scenes = [manifest.get("scene") or {}, *(manifest.get("additional_scenes") or [])]
        for scene in scenes:
            if not (isinstance(scene, dict) and scene.get("asset_required", True)):
                continue
            if (
                not scene.get("scene_revision_id")
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
    """把依赖 manifest 展开为 QA/关键帧可用的视觉锚点列表。

    ``scene`` 是主场景；``additional_scenes``（多场景转场镜头才非空，见
    ``_storyboard_pack_asset_dependencies``）用同样的形状展开，让转场到的
    第二个及以后的场景也能进最终的参考图列表，而不是在这里就被漏掉。
    """
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
    scenes = [manifest.get("scene") or {}, *(manifest.get("additional_scenes") or [])]
    for scene in scenes:
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
    """保留模型的结构化变化声明；不从正文词汇反推维度或权限。"""
    dims = item.get("change_dimensions") or item.get("changeDimensions") or []
    if isinstance(dims, str):
        dims = [dims]
    dims = [str(d).strip() for d in dims if str(d).strip()]
    persistence = str(item.get("persistence") or "persistent").strip().lower()
    if persistence not in {"persistent", "episode", "shot_only"}:
        persistence = "persistent"
    reason = str(item.get("reason") or "")
    evidence = str(item.get("evidence_excerpt") or item.get("evidence") or "")
    return {
        "character": item.get("character") or item.get("name") or "",
        "changed": bool(item.get("changed", True)),
        "new_appearance": (item.get("new_appearance") or "").strip(),
        "change_dimensions": list(dict.fromkeys(dims)),
        "identity_change_authorized": item.get("identity_change_authorized") is True,
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
    """生成/补齐人物必需多视角包；技术产物（文件）存在即 ready，不半包生效。

    VLM 图片质检已下线：本函数不再对生成结果做一致性/身份评分，只要每个必需视角的
    图片文件成功落盘（技术产物存在）即视为该视角就绪。``primary_qa`` 形参不再承载
    评分数据（调用方现在恒传 ``{}``），只用其"是否为 None"标记"父图是本次流水线刚
    生成的候选，可直接复用为 front_full"（见下方 ``use_parent_primary``）。
    """
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
    # ``primary_qa is not None``（而非真值判断）刻意保留：调用方（refs.py/portraits.py）
    # 传入空字典 ``{}`` 表示"这就是本次流水线里刚生成的候选，直接复用为 front_full"，
    # 与 prompt 字节级是否相同无关——后者在初始定妆流程里因锚点重排版几乎必然不匹配。
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
            # 技术产物存在即 ready：父图（parent["image_path"]）已经落盘，不再跑 VLM 评分。
            _upsert_character_view(
                conn, portrait_id=portrait_id, view_role="front_full", framing="full_body",
                image_path=parent["image_path"], prompt=parent["prompt"] or front_prompt,
                qa=None, artifact_id=parent["artifact_id"] if "artifact_id" in parent.keys() else None,
                base_view_id=base_front.get("id"),
                status="ready", fingerprint=front_fp,
            )
            conn.commit()
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
            # 技术产物存在即 ready：图片已成功落盘（否则 _save_image_item 早已抛出），不再跑 VLM 评分。
            gen_fp = view_input_fingerprint(
                view_role="front_full", prompt=front_prompt, anchor_text=effective_prompt,
                parent_revision_id=portrait_id, base_view_id=base_front.get("id"),
                seed_hint=base_front.get("image_path"),
            )
            _upsert_character_view(
                conn, portrait_id=portrait_id, view_role="front_full", framing="full_body",
                image_path=path, prompt=front_prompt, qa=None, artifact_id=None,
                base_view_id=base_front.get("id"), status="ready", fingerprint=gen_fp,
            )
            # 镜像到父表 image_path
            conn.execute("UPDATE character_portraits SET image_path=? WHERE id=?", (path, portrait_id))
            conn.commit()
    elif front and not front.get("input_fingerprint"):
        _backfill_view_fingerprint(
            conn, table="character_portrait_views", view_id=front["id"], fingerprint=front_fp,
        )
        conn.commit()

    existing_views = {v["view_role"]: v for v in list_portrait_views(portrait_id, conn=conn)}
    front = existing_views.get("front_full") or {}
    if front.get("status") != "ready" or not front.get("image_path"):
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
            # 历史遗留的 qa_pending/unverified 行：技术产物（文件）已存在且指纹匹配，
            # VLM 质检已下线，直接晋升为 ready，不再重新生成。
            if cur:
                conn.execute(
                    "UPDATE character_portrait_views SET status='ready', input_fingerprint=? WHERE id=?",
                    (fp, cur["id"]),
                )
                conn.commit()
            return {"view_role": view_role, "status": "ready", "id": cur["id"], "reused": True}
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
        # 技术产物存在即 ready：图片已成功落盘，不再等待 VLM 评审。
        view_id = _upsert_character_view(
            conn, portrait_id=portrait_id, view_role=view_role,
            framing="half_or_full" if view_role != "face_closeup" else "closeup",
            image_path=path, prompt=prompt, qa=None, artifact_id=None,
            base_view_id=base.get("id"), status="ready", fingerprint=fp,
        )
        conn.commit()
        return {"view_role": view_role, "status": "ready", "id": view_id}

    side_roles = [r for r in roles if r != "front_full"]
    side_results = await asyncio.gather(*[_gen_side(r) for r in side_roles])
    failed = [r for r in side_results if r.get("status") != "ready"]
    if failed:
        _set_portrait_pack_fields(conn, portrait_id, pack_status=PACK_STATUS_FAILED)
        conn.commit()
        return {"status": "failed", "portrait_id": portrait_id, "failed_views": failed}

    # 仅结构缺失（必需视角文件不存在）可判失败；VLM 一致性质检已下线，不再据此拦截整包。
    views = list_portrait_views(portrait_id, conn=conn)
    present_roles = {
        v.get("view_role") for v in views
        if v.get("image_path") and Path(v["image_path"]).exists()
    }
    missing_roles = [role for role in CHARACTER_REQUIRED_VIEWS if role not in present_roles]
    if missing_roles:
        _set_portrait_pack_fields(conn, portrait_id, pack_status=PACK_STATUS_FAILED)
        conn.commit()
        return {"status": "failed", "portrait_id": portrait_id, "failed_views": missing_roles}

    # 镜像 front_full 到父表
    front_ready = next((v for v in views if v.get("view_role") == "front_full"), None)
    fields: dict[str, Any] = {"pack_status": PACK_STATUS_READY}
    if front_ready and front_ready.get("image_path"):
        fields["image_path"] = front_ready["image_path"]
    _set_portrait_pack_fields(conn, portrait_id, **fields)
    conn.commit()
    return {"status": "ready", "portrait_id": portrait_id, "views": views}


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
    """生成/补齐场景必需多视角包；技术产物（文件）存在即 ready，不半包生效。

    VLM 图片质检已下线：本函数不再对生成结果做环境一致性评分，只要每个必需视角的
    图片文件成功落盘即视为该视角就绪。``primary_qa`` 形参不再承载评分数据（调用方
    现在恒传 ``{}``），只用其"是否为 None"标记父图可直接复用为 establishing。
    """
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
    generation_anchor = scene_multiview_generation_anchor(
        scene_canonical,
        parent["prompt"] if parent else None,
    )
    est_prompt = scene_view_prompt(visual_style, generation_anchor, "establishing")
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
            # 技术产物存在即 ready：父图已经落盘，不再跑 VLM 评分。
            _upsert_scene_view(
                conn, scene_reference_id=scene_reference_id, view_role="establishing",
                camera_axis="establishing", image_path=parent["image_path"],
                prompt=parent["prompt"] or est_prompt, qa=None,
                artifact_id=parent["artifact_id"] if "artifact_id" in parent.keys() else None,
                base_view_id=base_est.get("id"),
                status="ready", fingerprint=est_fp,
            )
            conn.commit()
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
            # 技术产物存在即 ready：图片已成功落盘，不再跑 VLM 评分。
            gen_fp = view_input_fingerprint(
                view_role="establishing", prompt=est_prompt, anchor_text=scene_canonical,
                parent_revision_id=scene_reference_id, base_view_id=base_est.get("id"),
                seed_hint=base_est.get("image_path"),
            )
            _upsert_scene_view(
                conn, scene_reference_id=scene_reference_id, view_role="establishing",
                camera_axis="establishing", image_path=path, prompt=est_prompt, qa=None, artifact_id=None,
                base_view_id=base_est.get("id"),
                status="ready", fingerprint=gen_fp,
            )
            conn.execute("UPDATE scene_references SET image_path=? WHERE id=?", (path, scene_reference_id))
            conn.commit()
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
    rev_prompt = scene_view_prompt(visual_style, generation_anchor, "reverse_angle")
    base_rev = base_views.get("reverse_angle") or {}
    rev_fp = view_input_fingerprint(
        view_role="reverse_angle",
        prompt=rev_prompt,
        anchor_text=scene_canonical,
        parent_revision_id=scene_reference_id,
        base_view_id=base_rev.get("id"),
        seed_hint=est.get("image_path"),
    )
    if _pending_view_can_be_reviewed(rev, rev_fp):
        # 历史遗留的 qa_pending/unverified 行：文件已存在且指纹匹配，直接晋升为 ready。
        conn.execute(
            "UPDATE scene_reference_views SET status='ready', input_fingerprint=? WHERE id=?",
            (rev_fp, rev["id"]),
        )
        conn.commit()
    elif not _ready_view_matches_fingerprint(rev, rev_fp):
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
        # 技术产物存在即 ready：图片已成功落盘，不再等待 VLM 评审。
        _upsert_scene_view(
            conn, scene_reference_id=scene_reference_id, view_role="reverse_angle",
            camera_axis="reverse", image_path=path, prompt=rev_prompt, qa=None, artifact_id=None,
            base_view_id=base_rev.get("id"),
            status="ready", fingerprint=rev_fp,
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
        action_prompt = scene_view_prompt(visual_style, generation_anchor, "action_zone")
        action_fp = view_input_fingerprint(
            view_role="action_zone", prompt=action_prompt, anchor_text=scene_canonical,
            parent_revision_id=scene_reference_id,
            seed_hint=(existing_views.get("establishing") or {}).get("image_path"),
        )
        if _pending_view_can_be_reviewed(action, action_fp):
            # 历史遗留的 qa_pending/unverified 行：文件已存在且指纹匹配，直接晋升为 ready。
            conn.execute(
                "UPDATE scene_reference_views SET status='ready', input_fingerprint=? WHERE id=?",
                (action_fp, action["id"]),
            )
            conn.commit()
        elif not _ready_view_matches_fingerprint(action, action_fp):
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
            # 技术产物存在即 ready：图片已成功落盘，不再等待 VLM 评审。
            _upsert_scene_view(
                conn, scene_reference_id=scene_reference_id, view_role="action_zone",
                camera_axis="action", image_path=path, prompt=action_prompt, qa=None, artifact_id=None,
                base_view_id=None, status="ready", fingerprint=action_fp,
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
    # VLM 图片质检已下线：不再逐视角评审、也不再做整包一致性评审。技术产物（文件）
    # 存在即视为该视角就绪；把仍停留在旧状态（qa_pending/unverified/failed）但文件
    # 已实际落盘的历史行统一晋升为 ready，避免残留状态把已完成的包挡在"未就绪"上。
    for view in required_views:
        if view.get("status") != "ready":
            conn.execute(
                "UPDATE scene_reference_views SET status='ready' WHERE id=?",
                (view["id"],),
            )
            view["status"] = "ready"
    conn.commit()

    views = list_scene_views(scene_reference_id, conn=conn)
    est_ready = next((v for v in views if v.get("view_role") == "establishing"), None)
    fields: dict[str, Any] = {"pack_status": PACK_STATUS_READY}
    if est_ready and est_ready.get("image_path"):
        fields["image_path"] = est_ready["image_path"]
    _set_scene_pack_fields(conn, scene_reference_id, **fields)
    conn.commit()
    return {"status": "ready", "scene_reference_id": scene_reference_id, "views": views}


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
    required_identity_names: list[str] | None = None,
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

    required_names = list(dict.fromkeys(
        str(name).strip()
        for name in (required_identity_names or [])
        if str(name).strip()
    ))

    def _identity_names(ref: dict[str, Any]) -> set[str]:
        names = {
            str(name).strip()
            for name in (
                ref.get("relatedCharacterIds")
                or ref.get("related_character_ids")
                or []
            )
            if str(name).strip()
        }
        entity_name = str(ref.get("entity_name") or "").strip()
        if entity_name:
            names.add(entity_name)
        return names

    required_refs: list[dict[str, Any]] = []
    for name in required_names:
        match = min(
            (
                ref
                for ref in usable
                if str(ref.get("type") or "") == "character"
                and name in _identity_names(ref)
            ),
            key=ref_pack_priority,
            default=None,
        )
        if match is not None and match not in required_refs:
            required_refs.append(match)

    character_limit = max(
        0,
        int(char_limit),
        len(required_names),
    )
    characters_seen = len(required_refs)
    eligible: list[dict[str, Any]] = list(required_refs)
    for ref in sorted(usable, key=ref_pack_priority):
        if ref in required_refs:
            continue
        if str(ref.get("type") or "") == "character":
            if characters_seen >= character_limit:
                continue
            characters_seen += 1
        eligible.append(ref)
    # continuity_required 保留 API 语义；有真实尾帧时它始终是最高优先级。
    _ = continuity_required
    return eligible[:limit]


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
    # 技术产物存在即可用：VLM 图片质检已下线，图片成功落盘即可替换旧视角。
    if not Path(path).exists():
        return {"status": "failed", "view_role": view_role, "preserved_previous": True}

    candidate = dict(existing.get(view_role) or {})
    candidate.update({
        "view_role": view_role, "image_path": path, "prompt": prompt,
        "status": "ready", "input_fingerprint": fp,
    })
    candidate_views = [candidate if v.get("view_role") == view_role else v for v in existing.values()]
    if view_role not in existing:
        candidate_views.append(candidate)
    missing = missing_required_views(candidate_views, CHARACTER_REQUIRED_VIEWS)
    if missing:
        _discard_rejected_candidate(path)
        return {
            "status": "failed", "view_role": view_role, "missing_required": missing,
            "preserved_previous": True,
        }

    view_id = _upsert_character_view(
        conn, portrait_id=portrait_id, view_role=view_role,
        framing="closeup" if view_role == "face_closeup" else ("full_body" if view_role.endswith("full") else "half_or_full"),
        image_path=path, prompt=prompt, qa=None, artifact_id=None,
        base_view_id=(existing.get(view_role) or {}).get("id"),
        status="ready", fingerprint=fp,
    )
    if view_role == "front_full":
        conn.execute("UPDATE character_portraits SET image_path=? WHERE id=?", (path, portrait_id))
    _set_portrait_pack_fields(conn, portrait_id, pack_status=PACK_STATUS_READY)
    conn.commit()
    return {"status": "ready", "view_role": view_role, "view_id": view_id}


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
    # 技术产物存在即可用：VLM 图片质检已下线，图片成功落盘即可替换旧视角。
    if not Path(path).exists():
        return {"status": "failed", "view_role": view_role, "preserved_previous": True}

    candidate = dict(existing.get(view_role) or {})
    candidate.update({
        "view_role": view_role, "image_path": path, "prompt": prompt,
        "status": "ready", "input_fingerprint": fp,
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
    missing = missing_required_views(candidate_views, tuple(required_roles))
    if missing:
        _discard_rejected_candidate(path)
        return {
            "status": "failed", "view_role": view_role, "missing_required": missing,
            "preserved_previous": True,
        }

    view_id = _upsert_scene_view(
        conn, scene_reference_id=scene_reference_id, view_role=view_role,
        camera_axis="establishing" if view_role == "establishing" else (
            "reverse" if view_role == "reverse_angle" else "action"),
        image_path=path, prompt=prompt, qa=None, artifact_id=None,
        base_view_id=(existing.get(view_role) or {}).get("id"),
        status="ready", fingerprint=fp,
    )
    if view_role == "establishing":
        conn.execute("UPDATE scene_references SET image_path=? WHERE id=?", (path, scene_reference_id))
    _set_scene_pack_fields(conn, scene_reference_id, pack_status=PACK_STATUS_READY)
    conn.commit()
    return {"status": "ready", "view_role": view_role, "view_id": view_id}


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
