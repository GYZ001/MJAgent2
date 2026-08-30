"""场景素材缺口扫描与场景刷新成本预检的共享判据。

从 app/domain/bible_ops.py 按原样搬移；被 scene_refs / scene_edit / auto_change / view_redo 依赖。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    now,
    rows_to_dicts,
)
from app.domain.common import _project_or_404
from app.orchestration.engine import fingerprint
from fastapi import HTTPException

from .primitives import _parse_json_value


def _normalize_scene_selection(value) -> list[str] | None:
    if value in (None, ""):
        return None
    raw = value
    if isinstance(value, str):
        parsed = _parse_json_value(value)
        raw = parsed if isinstance(parsed, list) else value.split(",")
    if not isinstance(raw, list):
        raise HTTPException(422, "scenes 必须是场景名数组")
    names = [str(item).strip() for item in raw if str(item).strip()]
    return list(dict.fromkeys(names)) or None

def _scene_required_roles(scene: dict) -> list[str]:
    from app.multiview import SCENE_REQUIRED_VIEWS
    roles = list(SCENE_REQUIRED_VIEWS)
    requested = scene.get("required_views") or []
    if isinstance(requested, str):
        requested = [requested]
    if scene.get("action_zone_required") or "action_zone" in requested:
        roles.append("action_zone")
    return list(dict.fromkeys(roles))

def _scene_current_row(conn, project_id: str, scene_name: str):
    return conn.execute(
        "SELECT * FROM scene_references WHERE project_id=? AND scene_name=? "
        "ORDER BY (ep_end IS NULL) DESC, ep_start DESC, created_at DESC LIMIT 1",
        (project_id, scene_name),
    ).fetchone()

def _scene_asset_state(pack_status: str | None, *, has_image: bool) -> str:
    """场景素材技术状态：missing/generating/passed/warning/failed/unverified。

    VLM 图片质检已下线（原 app.scene_policy.scene_asset_state 已随之删除）；这里只挂
    pack_status 这一产物信号，不再读取 QA gate。调用方只在 ``primary_usable`` 为假
    （主图技术上不可用）时才会走到这里，所以这个函数不需要处理"主图可用但整包 QA
    有警告"的降级分支——那种情况在调用侧已经被直接跳过。
    """
    if not has_image:
        return "missing"
    if pack_status in {"generating", "qa_pending", "running"}:
        return "generating"
    if pack_status == "failed":
        return "failed"
    if pack_status in {None, "legacy_partial"}:
        return "unverified"
    return "passed" if pack_status == "ready" else "unverified"

def scan_scene_asset_gaps(project_id: str) -> dict:
    """只读扫描；不会创建任务、调用供应商或写账单。"""
    from app.multiview import scene_primary_is_usable

    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        return {"project_id": project_id, "total": 0, "items": [], "counts": {}}
    bible = json.loads(p["bible_json"])
    conn = get_conn()
    items: list[dict] = []
    counts = {"missing": 0, "hard_failure": 0, "warning": 0, "interrupted": 0, "unverified": 0}
    for scene in bible.get("scenes") or []:
        name = str(scene.get("name") or "")
        required = _scene_required_roles(scene)
        row = _scene_current_row(conn, project_id, name)
        if not row:
            item = {"scene": name, "category": "missing", "reason": "尚无场景视角包", "views": required}
            counts["missing"] += 1
            items.append(item)
            continue
        views = rows_to_dicts(conn.execute(
            "SELECT view_role,status,image_path,qa_json FROM scene_reference_views WHERE scene_reference_id=?",
            (row["id"],),
        ).fetchall())
        ready_roles = {v["view_role"] for v in views if v.get("status") == "ready" and v.get("image_path")}
        missing_roles = [role for role in required if role not in ready_roles]
        has_image = bool(row["image_path"])
        primary_usable = scene_primary_is_usable(row, views)
        # The gap scanner serves the video-production path, not the internal
        # multi-view QA dashboard.  Once the establishing image is usable,
        # optional reverse/action views are not a user blocking gap.
        if primary_usable:
            continue
        pack_status = row["pack_status"] if "pack_status" in row.keys() else None
        state = _scene_asset_state(pack_status, has_image=has_image)
        if missing_roles:
            category, reason, repair = "missing", "缺少必需视角", missing_roles
        elif state == "failed":
            category, reason, repair = "hard_failure", "整包生成未通过技术校验", required
        elif state == "unverified":
            category, reason, repair = "unverified", "尚未完成生成", required
        elif p.get("scene_refs_status") == "failed":
            category, reason, repair = "interrupted", "最近一次场景任务中断或失败", []
        else:
            continue
        counts[category] += 1
        items.append({
            "scene": name,
            "scene_reference_id": row["id"],
            "category": category,
            "reason": reason,
            "views": list(dict.fromkeys(repair)),
            "hard_failures": [],
            "warnings": [],
            "pack_status": pack_status,
        })
    return {"project_id": project_id, "total": len(items), "items": items, "counts": counts, "read_only": True}

def compute_scene_cost_precheck(
    project_id: str,
    *,
    scenes: list[str] | None = None,
    resume: bool = False,
    view_role: str | None = None,
    scene_reference_id: str | None = None,
    action: str | None = None,
    scene_payloads: list[dict] | None = None,
) -> dict:
    """所有场景图片付费入口共用的服务端范围/费用预检。"""
    from app.config import IMAGE_PRICE_PER_UNIT

    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成人物谱")
    bible = json.loads(p["bible_json"])
    source_scenes = scene_payloads if scene_payloads is not None else list(bible.get("scenes") or [])
    selected = _normalize_scene_selection(scenes)
    if selected:
        by_name = {str(item.get("name") or ""): item for item in source_scenes}
        missing = [name for name in selected if name not in by_name]
        if missing:
            raise HTTPException(404, f"场景不存在：{missing[0]}")
        source_scenes = [by_name[name] for name in selected]
    scope: list[dict] = []
    if view_role:
        if len(source_scenes) != 1:
            raise HTTPException(422, "单视角预检必须明确一个场景")
        scene = source_scenes[0]
        scope.append({
            "scene": scene.get("name"), "scene_reference_id": scene_reference_id,
            "views": [view_role], "view_role": view_role, "reason": "单视角重做",
        })
    elif resume:
        gaps = scan_scene_asset_gaps(project_id)
        allowed = set(selected or [str(s.get("name") or "") for s in source_scenes])
        source_by_name = {str(scene.get("name") or ""): scene for scene in source_scenes}
        for item in gaps["items"]:
            if item["scene"] not in allowed or item["category"] == "warning":
                continue
            # 当前补齐实现以临时完整包复验后原子切换，报价必须覆盖完整合同视角；
            # 只修一个视角请走详情内“单视角重做”入口。
            contract_views = _scene_required_roles(source_by_name.get(item["scene"], {}))
            scope.append({
                "scene": item["scene"],
                "scene_reference_id": item.get("scene_reference_id"),
                "views": contract_views,
                "suggested_failed_views": item.get("views") or [],
                "reason": f"{item.get('reason')}；整包文件齐全并可读取后原子切换",
                "category": item.get("category"),
            })
    else:
        for scene in source_scenes:
            scope.append({
                "scene": scene.get("name"),
                "views": _scene_required_roles(scene),
                "reason": "首次生成" if action == "generate_bible_and_refs" else "整包重生",
            })
    image_count = sum(len(item.get("views") or []) for item in scope)
    unit = float(IMAGE_PRICE_PER_UNIT)
    estimated = round(image_count * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed = now()
    scope_fp = fingerprint({
        "project_id": project_id,
        "action": action or ("regenerate_view" if view_role else ("resume_missing" if resume else "regenerate_pack")),
        "scope": scope,
        "unit": unit,
        "bible_version": p.get("bible_version"),
    })
    return {
        "quote_id": scope_fp,
        "scope_fingerprint": scope_fp,
        "computed_at": computed,
        "quote_expires_at": computed + 300,
        "project_id": project_id,
        "action": action or ("regenerate_view" if view_role else ("resume_missing" if resume else "regenerate_pack")),
        "scene_count": len(scope),
        "actual_view_count": image_count,
        "views_per_scene": max((len(item.get("views") or []) for item in scope), default=0),
        "image_count": image_count,
        "unit_price_cny": unit,
        "estimated_cost_cny": estimated,
        "max_retry_budget_cny": max_retry,
        "budget_cap_cny": max_retry,
        "max_retries": 2,
        "estimated_duration_min": [max(1, image_count), max(3, image_count * 3)],
        "scope": scope,
        "old_asset_policy": "新包文件齐全并可读取后原子切换；质量评分只作提示，切换前旧采用包继续服务下游",
        "idempotency_hint": "同一有效报价重复确认只受理一个任务；范围或价格扩大必须重新确认",
        "stop_policy": "可停止；已开始步骤可能计费，结构完整并落盘的资产保留",
    }
