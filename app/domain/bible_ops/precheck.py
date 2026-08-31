"""人物谱改动影响预览、引用刷新成本预检、人物谱生成前置检查与画风选项。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import json

from app import (
    worker,
)
from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _project_or_404,
    router,
)
from app.evidence import repository as evidence_repository
from app.orchestration.engine import fingerprint
from app.schemas import (
    Bible,
    character_is_portrait_eligible,
    schema_errors,
)
from app.visual_styles import (
    DEFAULT_VISUAL_STYLE_NAME,
    visual_style_options,
)
from fastapi import HTTPException
from pathlib import Path

from .primitives import (
    _issue_payment_quote,
    _normalize_character_selection,
    _normalize_visual_style_name,
    _visual_style_prompt_or_default,
)


def _purge_for_style_change(project_id: str, instance: "Bible") -> dict:
    """画风变更的连锁失效：清理全项目旧画风视频产物，并作废旧画风定妆照
    （旧定妆照/旧尾帧是比文字 prompt 更强的画风信号，残留会把新画风拉回旧画风）。"""
    purged = worker.purge_project_video_artifacts(project_id)
    refs_cleared = 0
    for c in instance.characters:
        if c.ref_image_path:
            try:
                Path(c.ref_image_path).unlink()
            except OSError:
                pass
            c.ref_image_path = None
            refs_cleared += 1
    # 画风变更 → 旧画风场景图同样是强画风信号，连带作废（落盘文件 + 分段表），并清空 bible.scenes 的图路径。
    scene_refs_cleared = 0
    for sc in getattr(instance, "scenes", None) or []:
        if sc.ref_image_path:
            try:
                Path(sc.ref_image_path).unlink()
            except OSError:
                pass
            sc.ref_image_path = None
            scene_refs_cleared += 1
    conn = get_conn()
    # 画风变更 → 旧画风的分段定妆照全部作废，重新定妆后由分镜阶段按集反应式重建分段。
    conn.execute("DELETE FROM character_portraits WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM scene_references WHERE project_id=?", (project_id,))
    conn.execute("UPDATE projects SET refs_status='idle', scene_refs_status='idle' WHERE id=?", (project_id,))
    conn.commit()
    return {**purged, "refs_cleared": refs_cleared, "scene_refs_cleared": scene_refs_cleared}

def _purge_removed_character_portraits(
    conn, project_id: str, old_bible_json: object, instance: "Bible",
) -> dict:
    """角色被移出人物谱时，它的定妆照必须一起退场。

    _resolve_portrait_id（app/production/prep_pack.py）只按 project_id +
    character_name 查 character_portraits，不校验这个名字是否还在谱里。留下的
    孤儿行会被映射器当成合法角色绑定上去，实测让整集映射停在「称谓未逐字出现在
    本集原文」这道反幻觉闸上，而且删了谱里的卡也修不好——因为闸拦的是 portrait
    命中，不是谱命中。

    调用方必须已经提交主事务：这里删磁盘文件，unlink 不可回滚，主事务失败时
    一个文件都不能动。
    """
    from app.rejected_media import purge_character_portrait

    if not old_bible_json:
        return {"characters": [], "records": 0, "files": 0}
    try:
        old_characters = json.loads(old_bible_json).get("characters") or []
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"characters": [], "records": 0, "files": 0}
    old_names = {
        str(item.get("name") or "").strip()
        for item in old_characters
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    removed = sorted(old_names - {c.name for c in instance.characters})
    if not removed:
        return {"characters": [], "records": 0, "files": 0}
    placeholders = ",".join("?" * len(removed))
    rows = conn.execute(
        f"SELECT id FROM character_portraits WHERE project_id=? AND character_name IN ({placeholders})",
        (project_id, *removed),
    ).fetchall()
    records = files = 0
    for row in rows:
        purged = purge_character_portrait(conn, str(row["id"]), commit=True)
        records += int(purged.get("records") or 0)
        files += int(purged.get("files") or 0)
    return {"characters": removed, "records": records, "files": files}

def _parse_bible_write_body(body: dict) -> tuple[dict, object, bool, str | None]:
    """拆出 bible 正文、expected_version、confirm 标志与影响预检指纹。"""
    expected_version = body.get("expected_version")
    confirm = body.get("confirm") is True
    impact_fp = body.get("impact_preview_fingerprint")
    if "bible" in body and isinstance(body.get("bible"), dict):
        bible_body = dict(body["bible"])
    else:
        skip = {
            "expected_version", "confirm", "impact_preview_fingerprint",
            "quote_id", "dry_run",
        }
        bible_body = {k: v for k, v in body.items() if k not in skip}
    if "expected_version" in bible_body:
        expected_version = bible_body.pop("expected_version", expected_version)
    if "confirm" in bible_body:
        confirm = bible_body.pop("confirm") is True or confirm
    if "impact_preview_fingerprint" in bible_body:
        impact_fp = bible_body.pop("impact_preview_fingerprint", impact_fp)
    return bible_body, expected_version, confirm, impact_fp

def _bible_conflict_detail(p: dict, expected_version) -> dict:
    server_bible = None
    if p.get("bible_json"):
        try:
            server_bible = json.loads(p["bible_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            server_bible = None
    return {
        "code": "BIBLE_VERSION_CONFLICT",
        "message": (
            f"人物谱版本冲突：当前版本 {p.get('bible_version')}，"
            f"请求基于 {expected_version}，请刷新后重试"
        ),
        "current_version": int(p.get("bible_version") or 0),
        "expected_version": expected_version,
        "server_bible": server_bible,
        "character_names": [
            c.get("name") for c in (server_bible or {}).get("characters", []) if c.get("name")
        ],
    }

def _classify_bible_changes(old_bible: dict | None, new_bible: dict) -> list[str]:
    """区分仅文字 / 角色外观 / 全局画风变更，供定稿影响预检展示。"""
    changes: list[str] = []
    old = old_bible or {}
    old_style = (old.get("world") or {}).get("visual_style_canonical")
    new_style = (new_bible.get("world") or {}).get("visual_style_canonical")
    if old_style and new_style and old_style != new_style:
        changes.append("global_style")
    old_chars = {c.get("name"): c for c in old.get("characters", []) if c.get("name")}
    new_chars = {c.get("name"): c for c in new_bible.get("characters", []) if c.get("name")}
    appearance_changed = False
    text_changed = False
    if set(old_chars) != set(new_chars):
        text_changed = True
    for name, nc in new_chars.items():
        oc = old_chars.get(name) or {}
        if (oc.get("appearance_canonical") or "") != (nc.get("appearance_canonical") or ""):
            appearance_changed = True
        for field in ("personality", "speech_style", "role", "portrait_prompt_override"):
            if (oc.get(field) or "") != (nc.get(field) or ""):
                text_changed = True
        if (oc.get("relationships") or []) != (nc.get("relationships") or []):
            text_changed = True
    if appearance_changed:
        changes.append("character_appearance")
    if text_changed and "character_appearance" not in changes:
        changes.append("text_only")
    elif text_changed:
        changes.append("text_fields")
    if not changes:
        changes.append("text_only")
    return changes

def _artifact_type_counts(artifact_ids: list[str]) -> dict[str, int]:
    if not artifact_ids:
        return {}
    conn = get_conn()
    counts: dict[str, int] = {}
    for i in range(0, len(artifact_ids), 400):
        chunk = artifact_ids[i:i + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT type, COUNT(*) AS c FROM artifacts WHERE id IN ({placeholders}) GROUP BY type",
            chunk,
        ).fetchall()
        for row in rows:
            counts[row["type"]] = counts.get(row["type"], 0) + int(row["c"])
    return counts

def compute_bible_impact_preview(
    project_id: str,
    bible_body: dict,
    *,
    expected_version=None,
) -> dict:
    """定稿前只读影响预检：不写库、不失效下游。"""
    from app.config import IMAGE_PRICE_PER_UNIT
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    p = _project_or_404(project_id)
    current_version = int(p.get("bible_version") or 0)
    if expected_version is not None and int(expected_version) != current_version:
        raise HTTPException(409, detail=_bible_conflict_detail(p, expected_version))

    instance, errors = schema_errors(Bible, bible_body)
    if errors:
        raise HTTPException(422, "；".join(errors))
    from app.validators import validate_bible
    v_errors = validate_bible(instance)
    if v_errors:
        raise HTTPException(422, "；".join(v_errors))

    old_bible = json.loads(p["bible_json"]) if p.get("bible_json") else None
    new_bible = instance.model_dump(mode="json")
    change_types = _classify_bible_changes(old_bible, new_bible)
    style_changed = "global_style" in change_types
    previous_artifact_id = p.get("bible_artifact_id")
    stale_ids = (
        evidence_repository.list_descendants(previous_artifact_id)
        if previous_artifact_id else []
    )
    by_type = _artifact_type_counts(stale_ids)
    conn = get_conn()
    stale_assets: list[dict] = []
    if stale_ids:
        for i in range(0, min(len(stale_ids), 100), 400):
            chunk = stale_ids[i:i + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT id, type, status, scope_type, scope_id FROM artifacts WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            found = {row["id"]: dict(row) for row in rows}
            stale_assets.extend(found[asset_id] for asset_id in chunk if asset_id in found)
    portraits = conn.execute(
        "SELECT COUNT(*) AS c FROM character_portraits WHERE project_id=?", (project_id,)
    ).fetchone()["c"]
    scenes = conn.execute(
        "SELECT COUNT(*) AS c FROM scene_references WHERE project_id=?", (project_id,)
    ).fetchone()["c"]
    char_count = len(instance.characters)
    views_per = len(CHARACTER_REQUIRED_VIEWS)
    rebuild_images = 0
    if style_changed:
        rebuild_images = char_count * views_per + int(scenes or 0) * 2
    elif "character_appearance" in change_types:
        old_chars = {
            c.get("name"): c for c in (old_bible or {}).get("characters", []) if c.get("name")
        }
        affected = 0
        for c in new_bible.get("characters", []):
            name = c.get("name")
            oc = old_chars.get(name) or {}
            if (oc.get("appearance_canonical") or "") != (c.get("appearance_canonical") or ""):
                affected += 1
        rebuild_images = affected * views_per
    unit = float(IMAGE_PRICE_PER_UNIT)
    estimated = round(rebuild_images * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed_at = now()
    fingerprint_payload = {
        "project_id": project_id,
        "bible_version": current_version,
        "bible_artifact_id": previous_artifact_id,
        "change_types": change_types,
        "stale_descendant_ids": stale_ids,
        "portraits": int(portraits or 0),
        "scenes": int(scenes or 0),
        "rebuild_images": rebuild_images,
    }
    preview_fp = fingerprint(fingerprint_payload)
    return {
        "project_id": project_id,
        "bible_version": current_version,
        "computed_at": computed_at,
        "fingerprint": preview_fp,
        "change_types": change_types,
        "style_changed": style_changed,
        "stale_descendant_ids": stale_ids,
        "stale_assets": stale_assets,
        "stale_assets_truncated": len(stale_ids) > 100,
        "stale_count": len(stale_ids),
        "by_artifact_type": by_type,
        "paid_assets": {
            "character_portraits": int(portraits or 0),
            "scene_references": int(scenes or 0),
        },
        "rebuild": {
            "image_count": rebuild_images,
            "unit_price_cny": unit,
            "estimated_cost_cny": estimated,
            "max_retry_budget_cny": max_retry,
            "note": "费用来自服务端口径；实际生成以任务账单为准",
        },
        "requires_reconfirm": bool(stale_ids),
        "paid_media_invalidated": bool(style_changed or stale_ids),
        "old_asset_policy": "定稿后下游证据标记失效；画风变更会作废旧定妆/场景图",
    }

def compute_refs_cost_precheck(
    project_id: str,
    *,
    character: str | None = None,
    characters: list[str] | None = None,
    resume: bool = False,
    view_role: str | None = None,
) -> dict:
    """人物定妆/单视角付费预检（只读）。"""
    from app.config import IMAGE_PRICE_PER_UNIT
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经")
    bible = json.loads(p["bible_json"])
    all_bible_characters = bible.get("characters") or []
    bible_characters = [
        c for c in all_bible_characters
        if character_is_portrait_eligible(c)
    ]
    eligible_by_name = {c.get("name"): c for c in bible_characters}
    selected_names = _normalize_character_selection(characters)
    if character and selected_names and character not in selected_names:
        raise HTTPException(422, "character 与 characters 范围不一致")
    if character:
        if character not in {c.get("name") for c in all_bible_characters}:
            raise HTTPException(404, f"角色不存在：{character}")
        if character not in eligible_by_name:
            raise HTTPException(409, f"角色尚无可靠外观依据，暂不具备定妆资格：{character}")
        bible_characters = [eligible_by_name[character]]
    elif selected_names:
        all_names = {c.get("name") for c in all_bible_characters if c.get("name")}
        missing = [name for name in selected_names if name not in all_names]
        if missing:
            raise HTTPException(404, f"角色不存在：{missing[0]}")
        ineligible = [name for name in selected_names if name not in eligible_by_name]
        if ineligible:
            raise HTTPException(409, f"角色尚无可靠外观依据，暂不具备定妆资格：{ineligible[0]}")
        bible_characters = [eligible_by_name[name] for name in selected_names]
    views_per = 1 if view_role else len(CHARACTER_REQUIRED_VIEWS)
    conn = get_conn()
    missing_roles: list[dict] = []
    image_count = 0
    if view_role:
        image_count = 1
        missing_roles.append({
            "character": character, "view_role": view_role, "reason": "单视角重做",
        })
    elif resume:
        for c in bible_characters:
            name = c.get("name")
            row = conn.execute(
                """SELECT id, pack_status FROM character_portraits
                   WHERE project_id=? AND character_name=? AND ep_end IS NULL
                   ORDER BY ep_start DESC LIMIT 1""",
                (project_id, name),
            ).fetchone()
            if not row or row["pack_status"] not in (None, "ready"):
                image_count += views_per
                missing_roles.append({
                    "character": name, "views": list(CHARACTER_REQUIRED_VIEWS),
                    "reason": "缺包或未通过",
                })
                continue
            view_rows = conn.execute(
                "SELECT view_role, status, image_path FROM character_portrait_views WHERE portrait_id=?",
                (row["id"],),
            ).fetchall()
            have = {
                v["view_role"] for v in view_rows
                if v["status"] == "ready" and v["image_path"]
            }
            need = [r for r in CHARACTER_REQUIRED_VIEWS if r not in have]
            if need:
                image_count += len(need)
                missing_roles.append({
                    "character": name, "views": need, "reason": "缺失视角",
                })
    else:
        image_count = len(bible_characters) * views_per
        for c in bible_characters:
            missing_roles.append({
                "character": c.get("name"),
                "views": list(CHARACTER_REQUIRED_VIEWS) if not view_role else [view_role],
                "reason": "整包生成",
            })
    unit = float(IMAGE_PRICE_PER_UNIT)
    estimated = round(image_count * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed_at = now()
    scope_fingerprint = fingerprint({
        "project_id": project_id,
        "character": character,
        "characters": selected_names,
        "resume": resume,
        "view_role": view_role,
        "image_count": image_count,
        "unit": unit,
        "bible_version": p.get("bible_version"),
    })
    return {
        "quote_id": scope_fingerprint,
        "scope_fingerprint": scope_fingerprint,
        "computed_at": computed_at,
        "quote_expires_at": computed_at + 300,
        "project_id": project_id,
        "action": (
            "regenerate_view" if view_role
            else ("resume_missing" if resume else ("regenerate_pack" if character else "generate_all"))
        ),
        "character": character,
        "characters": selected_names,
        "view_role": view_role,
        "character_count": len(bible_characters),
        "views_per_character": views_per,
        "image_count": image_count,
        "unit_price_cny": unit,
        "estimated_cost_cny": estimated,
        "max_retry_budget_cny": max_retry,
        "budget_cap_cny": max_retry,
        "scope": missing_roles,
        "old_asset_policy": (
            "已落盘且可读取的视角保留；技术失败不替换当前采用包"
            if resume else
            "使用最新角色设定与全局画风生成；新包三视角文件齐全并可读取后替换旧包，质量评分只作提示"
        ),
        "idempotency_hint": "同一 quote_id 重复确认不会扩大范围；服务端仍做最终校验",
        "stop_policy": "可停止；已扣费步骤不退款，已完成成品保留",
    }

@router.post("/projects/{project_id}/bible/impact-preview")
async def bible_impact_preview(project_id: str, body: dict):
    """定稿人物谱前的只读影响预检。"""
    bible_body, expected_version, _, _ = _parse_bible_write_body(body or {})
    return compute_bible_impact_preview(
        project_id, bible_body, expected_version=expected_version,
    )

@router.post("/projects/{project_id}/refs/precheck")
async def refs_cost_precheck(project_id: str, body: dict | None = None):
    """定妆照/造型包付费预检。"""
    payload = body or {}
    return _issue_payment_quote(compute_refs_cost_precheck(
        project_id,
        character=payload.get("character"),
        characters=_normalize_character_selection(payload.get("characters")),
        resume=bool(payload.get("resume", False)),
        view_role=payload.get("view_role"),
    ))

def _compute_bible_generate_precheck(project_id: str, *, style_name: str | None = None) -> dict:
    """POST /bible 只判定世界观（不点名角色）的真实成本预检：只有请求画风与
    当前画风不同才触发角色批量重出定妆照（判据须与 _bible_task 的
    style_changed 同口径），首次生成或画风未变时无图片费用，不报假价。"""
    from app.config import IMAGE_PRICE_PER_UNIT
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    style_name = _normalize_visual_style_name(style_name)
    p = _project_or_404(project_id)
    unit = float(IMAGE_PRICE_PER_UNIT)
    views_per = len(CHARACTER_REQUIRED_VIEWS)
    bible = json.loads(p["bible_json"]) if p.get("bible_json") else None
    current_style = ((bible or {}).get("world") or {}).get("visual_style_canonical")
    style_changing = bool(current_style) and _visual_style_prompt_or_default(style_name) != current_style
    chars = (bible or {}).get("characters") or [] if style_changing else []
    char_count = len(chars)
    names = [c.get("name") for c in chars if c.get("name")]
    if style_changing:
        estimate_note = "画风将发生变化，按当前人物谱角色数重新生成全部角色定妆照"
    elif bible:
        estimate_note = "本次只判定世界观，画风未变化，不会重新生成角色定妆照，无图片费用"
    else:
        estimate_note = "首次生成只判定年代/题材/画风（世界观），本身不产生角色或图片，无费用；角色改在映射台按需生成"
    image_count = char_count * views_per
    estimated = round(image_count * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed_at = now()
    scope_fingerprint = fingerprint({
        "project_id": project_id,
        "action": "generate_bible_and_refs",
        "character_count": char_count,
        "image_count": image_count,
        "unit": unit,
        "bible_version": p.get("bible_version"),
        "style_name": style_name,
    })
    return {
        "quote_id": scope_fingerprint,
        "scope_fingerprint": scope_fingerprint,
        "computed_at": computed_at,
        "quote_expires_at": computed_at + 300,
        "project_id": project_id,
        "action": "generate_bible_and_refs",
        "style_name": style_name,
        "character_count": char_count,
        "character_names": names,
        "views_per_character": views_per,
        "image_count": image_count,
        "unit_price_cny": unit,
        "estimated_cost_cny": estimated,
        "max_retry_budget_cny": max_retry,
        "budget_cap_cny": max_retry,
        "estimated_duration_min": [max(3, char_count), max(8, char_count * 3)],
        "estimate_note": estimate_note,
        "old_asset_policy": "停止后保留已落盘成品；未开始项可稍后补齐",
        "stop_policy": "可按阶段停止谱写或定妆；已扣费步骤不退款",
        "scope": [
            {"character": n or f"角色{i+1}", "views": list(CHARACTER_REQUIRED_VIEWS), "reason": "首次/重生"}
            for i, n in enumerate(names or [None] * char_count)
        ],
    }

@router.post("/projects/{project_id}/bible/generate-precheck")
async def bible_generate_precheck(project_id: str, body: dict | None = None):
    """签发首次生成人物谱+定妆的服务端费用凭证。"""
    payload = body or {}
    return _issue_payment_quote(_compute_bible_generate_precheck(
        project_id, style_name=payload.get("style_name"),
    ))

@router.get("/projects/{project_id}/bible/visual-styles")
async def bible_visual_styles(project_id: str):
    _project_or_404(project_id)
    return {
        "default": DEFAULT_VISUAL_STYLE_NAME,
        "items": visual_style_options(),
    }
