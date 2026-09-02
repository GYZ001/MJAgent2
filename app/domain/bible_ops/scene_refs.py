"""单场景引用图缺口/进度/预检与场景圣经发起、取消的路由实现。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import json

from app import (
    task_registry,
)
from app.db import (
    get_conn,
    now,
    rows_to_dicts,
)
from app.domain.common import (
    _as_body_dict,
    _project_or_404,
    _scene_assets_task_active,
    router,
)
from app.evidence import repository as evidence_repository
from app.schemas import (
    Bible,
    schema_errors,
)
from app.validators import validate_scene_bible
from fastapi import HTTPException

from .primitives import (
    _SCENE_CANONICAL_LENGTH_MESSAGE,
    _consume_payment_quote,
    _issue_scope_quote,
    _parse_json_value,
    _payment_confirm_required,
    _scene_canonical_length_ok,
    _validate_scope_quote,
)
from .scene_assets import (
    _normalize_scene_selection,
    compute_scene_cost_precheck,
    scan_scene_asset_gaps,
)
from .scene_bible_prep import (
    _decode_scene_target,
    _start_scene_refs_generation,
)


@router.get("/projects/{project_id}/scene-refs/gaps")
async def scene_refs_gaps(project_id: str):
    return scan_scene_asset_gaps(project_id)

@router.post("/projects/{project_id}/scene-refs/precheck")
async def scene_refs_precheck(project_id: str, body: dict | None = None):
    payload = _as_body_dict(body)
    return _issue_scope_quote(compute_scene_cost_precheck(
        project_id,
        scenes=_normalize_scene_selection(payload.get("scenes")),
        resume=bool(payload.get("resume", False)),
        view_role=payload.get("view_role"),
        scene_reference_id=payload.get("scene_reference_id"),
        action=payload.get("action"),
    ))

def _scene_refs_progress_payload(project_id: str) -> dict:
    p = _project_or_404(project_id)
    gaps = scan_scene_asset_gaps(project_id)
    target = _decode_scene_target(p.get("scene_refs_target"))
    all_scenes = (json.loads(p["bible_json"]).get("scenes") or []) if p.get("bible_json") else []
    target_names = set(target if isinstance(target, list) else ([target] if isinstance(target, str) else []))
    progress_scenes = [scene for scene in all_scenes if not target_names or scene.get("name") in target_names]
    total = len(progress_scenes)
    problematic = {item["scene"]: item for item in gaps["items"]}
    items = []
    ready = failed = missing = unverified = 0
    for scene in progress_scenes:
        name = scene.get("name")
        gap = problematic.get(name)
        if not gap:
            status = "ready"; ready += 1
        elif gap["category"] == "missing":
            status = "missing"; missing += 1
        elif gap["category"] == "hard_failure":
            status = "failed"; failed += 1
        else:
            status = "unverified"; unverified += 1
        items.append({"scene": name, "status": status, **({"detail": gap} if gap else {})})
    run = next((item for item in evidence_repository.list_runs(project_id=project_id, limit=50)
                if item.get("workflow_type") in {"scene_references", "scene_view_redo"}), None)
    run_id = (run or {}).get("id")
    steps = evidence_repository.get_steps(run_id) if run_id else []
    active_step = next((step for step in reversed(steps)
                        if step.get("status") in {"queued", "running", "waiting"}), None)
    latest_step = active_step or (steps[-1] if steps else None)
    latest_call = None
    if run_id:
        conn = get_conn()
        calls = rows_to_dicts(conn.execute(
            "SELECT * FROM provider_calls WHERE run_id=? ORDER BY id", (run_id,),
        ).fetchall())
        latest_call = calls[-1] if calls else None
    call_meta = _parse_json_value((latest_call or {}).get("meta"), {})
    if not isinstance(call_meta, dict):
        call_meta = {}
    fallback_scene = target[0] if isinstance(target, list) and target else (target if isinstance(target, str) else None)
    configured = (run or {}).get("config_snapshot") or {}
    return {
        "project_id": project_id, "total": total, "ready": ready, "failed": failed,
        "missing": missing, "unverified": unverified, "remaining": max(0, total - ready),
        "refs_status": p.get("scene_refs_status"), "refs_target": target,
        "run_id": run_id,
        "phase": (latest_step or {}).get("step_name") or (latest_call or {}).get("kind") or p.get("scene_refs_status"),
        "current_scene": call_meta.get("scene_name") or configured.get("scene_name") or fallback_scene,
        "current_view": call_meta.get("view_role") or configured.get("view_role"),
        "attempt": int((latest_call or {}).get("attempt_no") or 0),
        "items": items, "updated_at": now(),
    }

@router.get("/projects/{project_id}/scene-refs/progress")
async def scene_refs_progress(project_id: str):
    return _scene_refs_progress_payload(project_id)

@router.post("/projects/{project_id}/scene-bible/preview")
async def preview_scene_bible(project_id: str):
    """只生成可编辑的场景清单与真实视角报价；不出图、不替换资产。"""
    from app.stages import generate_scene_bible

    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经")
    bible = Bible.model_validate(json.loads(p["bible_json"]))
    from app.scenes import SCENE_BIBLE_CHAPTER_WINDOW
    chapters = rows_to_dicts(get_conn().execute(
        "SELECT * FROM chapters WHERE project_id=? ORDER BY idx LIMIT ?",
        (project_id, SCENE_BIBLE_CHAPTER_WINDOW),
    ).fetchall())
    scenes = await generate_scene_bible(chapters, bible, project_id=project_id)
    scene_payloads = [scene.model_dump(mode="json") for scene in scenes]
    quote = _issue_scope_quote(compute_scene_cost_precheck(
        project_id,
        scenes=[scene["name"] for scene in scene_payloads],
        action="generate_bible_and_refs",
        scene_payloads=scene_payloads,
    ))
    # 生成侧的 AgentLoop 修不好时会带着残留问题把草稿交回来（warning/baseline
    # 分支，见 app/stages.py:_run_with_agent_loop），generate_scene_bible 只返回
    # scenes，残留问题就此消失。这里不拦——清单是可编辑的，拦死了用户反而没路走；
    # 但必须把这份清单当前过不了哪几道门原样说出来，否则用户点确认时才被自己的
    # 提交端点以 422 拒收，而界面从头到尾没提示过哪一条超标。
    # 真实故障 ERR-20260828-4f4f19（《罗刹海市》）：12 个场景里 3 个锚点 81 字。
    return {
        "project_id": project_id,
        "scenes": scene_payloads,
        "precheck": quote,
        "generates_images": False,
        "blocking_errors": validate_scene_bible(scenes),
    }

@router.post("/projects/{project_id}/scene-bible/precheck")
async def scene_bible_precheck(project_id: str, body: dict | None = None):
    payload = _as_body_dict(body)
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise HTTPException(422, "必须提交已确认的场景清单")
    names = [str(item.get("name") or "").strip() for item in scenes if isinstance(item, dict)]
    if len(names) != len(scenes) or not all(names) or len(names) != len(set(names)):
        raise HTTPException(422, "场景名称不能为空或重复")
    if any(
        not _scene_canonical_length_ok(str(item.get("scene_canonical") or "").strip())
        for item in scenes
    ):
        raise HTTPException(422, _SCENE_CANONICAL_LENGTH_MESSAGE)
    project = _project_or_404(project_id)
    candidate_bible = json.loads(project["bible_json"] or '{}')
    candidate_bible["scenes"] = scenes
    instance, validation_errors = schema_errors(Bible, candidate_bible)
    if validation_errors:
        raise HTTPException(422, "；".join(validation_errors))
    normalized_scenes = [scene.model_dump(mode="json") for scene in instance.scenes]
    return _issue_scope_quote(compute_scene_cost_precheck(
        project_id, scenes=names, action="generate_bible_and_refs", scene_payloads=normalized_scenes,
    ))

@router.post("/projects/{project_id}/scene-bible", status_code=202)
async def start_scene_bible(project_id: str, body: dict | None = None):
    """（重新）生成场景圣经并触发场景图批量出图。人物谱必须先就绪。"""
    from app.capabilities.dispatch import ui_route
    payload = _as_body_dict(body)
    # 带服务端报价的正式确认直接进入本路由的报价/幂等校验；旧能力入口仍走 Command Bus。
    formal_request = any(
        key in payload
        for key in ("scenes", "confirm", "quote_id", "idempotency_key", "request_id")
    )
    if not formal_request:
        routed = await ui_route("scene.generate_bible", {"project_id": project_id})
        if routed is not None:
            return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if _scene_assets_task_active(project_id):
        raise HTTPException(409, "场景图正在生成中")
    confirmed_scenes = payload.get("scenes")
    if not isinstance(confirmed_scenes, list) or not confirmed_scenes:
        raise HTTPException(409, detail={
            "code": "SCENE_PREVIEW_REQUIRED",
            "message": "必须先预览并确认场景清单，再执行范围确认",
        })
    names = [str(item.get("name") or "").strip() for item in confirmed_scenes if isinstance(item, dict)]
    if not names or len(names) != len(set(names)):
        raise HTTPException(422, "场景清单名称不能为空或重复")
    if any(
        not _scene_canonical_length_ok(str(item.get("scene_canonical") or "").strip())
        for item in confirmed_scenes
    ):
        raise HTTPException(422, _SCENE_CANONICAL_LENGTH_MESSAGE)
    candidate_bible = json.loads(p["bible_json"] or '{}')
    candidate_bible["scenes"] = confirmed_scenes
    bible_instance, validation_errors = schema_errors(Bible, candidate_bible)
    if validation_errors:
        raise HTTPException(422, "；".join(validation_errors))
    confirmed_scenes = [scene.model_dump(mode="json") for scene in bible_instance.scenes]
    quote = compute_scene_cost_precheck(
        project_id, scenes=names, action="generate_bible_and_refs", scene_payloads=confirmed_scenes,
    )
    if payload.get("confirm") is not True:
        # 见 task_run.py 同类注释：未签发的报价不能作为 409 里的 quote_id 递
        # 出去，否则调用方按响应指引确认必然 QUOTE_STALE。
        raise _payment_confirm_required(_issue_scope_quote(quote))
    quote_row = _validate_scope_quote(project_id, payload.get("quote_id"), quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "quote_id": payload.get("quote_id"), "task_id": quote_row["consumed_task_id"],
            "run_id": quote_row["consumed_run_id"],
        }
    conn = get_conn()
    current = json.loads(p["bible_json"])
    current["scenes"] = confirmed_scenes
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps(current, ensure_ascii=False), project_id),
    )
    conn.commit()
    if not _start_scene_refs_generation(project_id, names):
        raise HTTPException(409, "场景图正在生成中")
    task_id = f"scene_refs:{project_id}"
    _consume_payment_quote(str(payload.get("quote_id")), task_id=task_id, run_id=None)
    return {"status": "accepted", "task_id": task_id, "quote_id": payload.get("quote_id"), "precheck": quote}

@router.post("/projects/{project_id}/scene-refs", status_code=202)
async def start_scene_refs(project_id: str, body: dict | None = None):
    """（重新）生成场景图。需先有场景圣经（bible.scenes 非空）。可带 only 单场景重做。"""
    from app.capabilities.dispatch import ui_route
    payload = _as_body_dict(body)
    formal_request = any(
        key in payload
        for key in ("scenes", "resume", "confirm", "quote_id", "idempotency_key", "request_id")
    )
    if not formal_request:
        routed = await ui_route(
            "scene.generate_refs",
            {"project_id": project_id, "scene_name": payload.get("scene")},
        )
        if routed is not None:
            return routed
    p = _project_or_404(project_id)
    if not p["bible_json"] or not json.loads(p["bible_json"]).get("scenes"):
        raise HTTPException(409, "还没有场景圣经，请先生成场景清单")
    if _scene_assets_task_active(project_id):
        raise HTTPException(409, "场景图正在生成中")
    selected = _normalize_scene_selection(payload.get("scenes"))
    only = payload.get("scene")
    if only and selected and only not in selected:
        raise HTTPException(422, "scene 与 scenes 范围不一致")
    if only and not selected:
        selected = [str(only)]
    resume = bool(payload.get("resume", not bool(only)))
    quote = compute_scene_cost_precheck(project_id, scenes=selected, resume=resume)
    if payload.get("confirm") is not True:
        # 见 task_run.py 同类注释：未签发的报价不能作为 409 里的 quote_id 递
        # 出去，否则调用方按响应指引确认必然 QUOTE_STALE。
        raise _payment_confirm_required(_issue_scope_quote(quote))
    quote_row = _validate_scope_quote(project_id, payload.get("quote_id"), quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "quote_id": payload.get("quote_id"), "task_id": quote_row["consumed_task_id"],
            "run_id": quote_row["consumed_run_id"], "precheck": quote,
        }
    targets: str | list[str] | None = selected if selected else None
    if not _start_scene_refs_generation(project_id, targets, resume=resume):
        raise HTTPException(409, "场景图正在生成中")
    task_id = f"scene_refs:{project_id}"
    _consume_payment_quote(str(payload.get("quote_id")), task_id=task_id, run_id=None)
    return {"status": "accepted", "task_id": task_id, "quote_id": payload.get("quote_id"), "precheck": quote}

@router.post("/projects/{project_id}/scene-refs/cancel")
async def cancel_scene_refs(project_id: str):
    """停止场景图生成。已落盘的场景图保留，状态置回空闲。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("scene.cancel_refs", {"project_id": project_id})
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    stopped_bible = await task_registry.cancel_and_wait("scene_bible", project_id)
    stopped_refs = await task_registry.cancel_and_wait("scene_refs", project_id)
    stopped = stopped_bible or stopped_refs
    final_progress = _scene_refs_progress_payload(project_id)
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET scene_refs_status='idle',scene_refs_error=NULL,"
        "scene_refs_target=NULL,scene_refs_batch_started_at=NULL WHERE id=?",
        (project_id,))
    conn.commit()
    was_running = p["scene_refs_status"] == "running"
    final_progress["refs_status"] = "idle"
    return {
        "stopped": stopped or was_running,
        "partial_results_preserved": True,
        "progress": final_progress,
    }
