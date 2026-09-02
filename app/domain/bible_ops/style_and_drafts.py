"""整体画风切换（含改配报价）、引用生成缺口/进度查询与人物谱草稿存取。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import json

from app import errors
from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _as_body_dict,
    _project_or_404,
    router,
)
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
from fastapi import (
    Body,
    HTTPException,
)

from .precheck import (
    _bible_conflict_detail,
    compute_refs_precheck,
)
from .primitives import (
    _consume_payment_quote,
    _ensure_character_payment_quotes,
    _issue_scope_quote,
    _normalize_visual_style_name,
    _payment_confirm_required,
    _supports_bible_style_name,
    _validate_scope_quote,
    _visual_style_prompt_or_default,
)
from .refs_generation import (
    _refs_generation_busy,
    _start_refs_generation,
)
from .scene_assets import compute_scene_cost_precheck
from .scene_bible_prep import _start_scene_refs_generation


def _compute_style_regen_quote(project_id: str) -> dict:
    """风格切换后「人物定妆照 + 场景图」两条腿一并全量重生成的合并范围预检。

    两条腿的范围都必须在用户确认前一次性摆出来——只报其中一条腿的范围、让另一
    条腿在确认后悄悄启动，等于变相绕过范围确认。场景清单未就绪时
    ``scenes`` 为 None，合并预检里只有人物这一条腿（如实反映范围，不假装
    有场景图）。
    """
    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经")
    bible = json.loads(p["bible_json"])
    scene_bible_ready = bool(bible.get("scenes"))
    refs_quote = compute_refs_precheck(project_id, resume=False)
    scenes_quote = (
        compute_scene_cost_precheck(project_id, scenes=None, resume=False)
        if scene_bible_ready else None
    )
    total_images = int(refs_quote["image_count"]) + (
        int(scenes_quote["image_count"]) if scenes_quote else 0
    )
    computed_at = now()
    scope_fingerprint = fingerprint({
        "project_id": project_id,
        "action": "style_regen_all",
        "refs_scope_fingerprint": refs_quote["scope_fingerprint"],
        "scenes_scope_fingerprint": scenes_quote["scope_fingerprint"] if scenes_quote else None,
        "bible_version": p.get("bible_version"),
    })
    return {
        # 同 precheck.py 的同类注释：未签发前不叫 quote_id，调用方必须先经
        # _issue_scope_quote 落库才能拿到可确认的真值（本函数的两处调用方
        # 都已经这样做——见下方 set_bible_visual_style）。
        "scope_fingerprint": scope_fingerprint,
        "action": "style_regen_all",
        "project_id": project_id,
        "computed_at": computed_at,
        "quote_expires_at": computed_at + 300,
        "characters": refs_quote,
        "scenes": scenes_quote,
        "scene_bible_ready": scene_bible_ready,
        "total_image_count": total_images,
        "idempotency_hint": "同一报价重复确认只受理一次，人物与场景两条线都不会重复启动",
        "stop_policy": "确认后人物与场景两条生成线独立运行，可分别在人物谱/场景库停止；已完成的图片保留",
    }

@router.get("/bible/visual-styles")
async def bible_visual_styles_unscoped():
    """导入项目面板选画风用：项目尚未创建，没有 project_id 可传，取值与项目级
    ``GET /projects/{id}/bible/visual-styles``（precheck.py）完全一致，同一份
    ``VISUAL_STYLE_PRESETS``。"""
    return {"default": DEFAULT_VISUAL_STYLE_NAME, "items": visual_style_options()}

@router.post("/projects/{project_id}/bible/style")
async def set_bible_visual_style(project_id: str, body: dict | None = Body(None)):
    """人物谱与场景库共用的统一画风配置入口，不重新生成角色内容——场景库触发
    这次配置时不应被迫连带整份人物谱（角色外观/性格/关系）重新生成，那是
    「重新生成人物谱并更换画风」按钮的既有职责，这里不动它。

    画风未实际变化（重复确认同一风格）时直接返回 changed=False、不写库、不
    进入报价/确认流程，保证反复点击的幂等性、不重复发起生成。

    画风确有变化时走标准的「预检 → 显式确认 → 消费报价」两段式（与本文件
    其它范围确认端点同构）：第一次调用（无 confirm/quote_id）返回 409 + 合并报价
    （见 _compute_style_regen_quote，人物 + 场景两条腿的范围一次性摆出来）；
    带着 confirm=true 与 quote_id 的确认调用里，落定风格字段之后，在**同一次
    请求内**依次发起人物定妆照与场景图两条全量重生成——不是把「要不要触发
    场景图」这件事丢给前端等用户以后访问场景库页面才做，那样『两条线都要
    发起』就变成了『取决于用户接下来去了哪个页面』，不满足要求。

    场景清单未就绪（bible.scenes 为空）时，人物这条腿仍然正常发起；场景这条
    腿因为没有可生成的场景，本来就发起不了，响应里 scene_bible_ready=False，
    调用方据此给出明确的「先去准备场景清单」提示，不是静默跳过。

    不新造删除逻辑：两条腿都是 resume=false 全量重生成，复用既有的「新包完整
    后原子切换、旧包留存供下游服务」生成路径，旧素材始终留到新素材真正生成
    完成才被取代。
    """
    payload = _as_body_dict(body)
    style_name = _normalize_visual_style_name(payload.get("style_name"))
    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先在人物谱生成人物谱后再配置统一画风")
    quote_id = payload.get("quote_id")
    if payload.get("confirm") is True and quote_id:
        # 幂等重放必须先于版本冲突检查：确认成功后 bible_version 已经前进，若
        # 网络重试/重复点击带着同一个已消费的 quote_id 重新进来，此时拿它去比
        # 对「客户端仍以为的旧版本号」必然冲突——但这本是已经办成的同一件事，
        # 不该因为版本号往前走了就报错，应直接原样回放结果，不再触发生成。
        conn_probe = get_conn()
        _ensure_character_payment_quotes(conn_probe)
        existing_quote = conn_probe.execute(
            "SELECT * FROM character_payment_quotes WHERE quote_id=? AND project_id=?",
            (quote_id, project_id),
        ).fetchone()
        if existing_quote is not None and existing_quote["consumed_at"] is not None:
            return {
                "project_id": project_id,
                "style_name": style_name,
                "changed": True,
                "idempotent_replay": True,
                "quote_id": quote_id,
                "task_id": existing_quote["consumed_task_id"],
            }
    current_version = int(p.get("bible_version") or 0)
    expected_version = payload.get("expected_version")
    if expected_version is None or int(expected_version) != current_version:
        raise HTTPException(409, detail=_bible_conflict_detail(p, expected_version))
    bible_data = json.loads(p["bible_json"])
    world = bible_data.setdefault("world", {})
    old_style_prompt = world.get("visual_style_canonical")
    new_style_prompt = _visual_style_prompt_or_default(style_name)
    changed = old_style_prompt != new_style_prompt
    if not changed:
        scenes = bible_data.get("scenes") or []
        return {
            "project_id": project_id,
            "style_name": style_name,
            "changed": False,
            "bible_version": current_version,
            "scene_bible_ready": bool(scenes),
            "scenes_total": len(scenes),
        }

    quote = _compute_style_regen_quote(project_id)
    if payload.get("confirm") is not True:
        # 精确/确认合一在同一个路由里：未带 confirm 的调用必须先把报价持久化
        # （_issue_scope_quote 签发服务端 quote_id 并写入 character_payment_quotes），
        # 否则随后带着这个 quote_id 来确认时 _validate_scope_quote 查不到行，
        # 会被误判为过期报价——两次调用用的必须是同一份已签发凭证。
        raise _payment_confirm_required(_issue_scope_quote(quote))
    quote_id = payload.get("quote_id")
    quote_row = _validate_scope_quote(project_id, quote_id, quote)
    if quote_row["consumed_at"] is not None:
        return {
            "project_id": project_id,
            "style_name": style_name,
            "changed": True,
            "idempotent_replay": True,
            "quote_id": quote_id,
            "task_id": quote_row["consumed_task_id"],
        }

    world["visual_style_canonical"] = new_style_prompt
    instance, validation_errors = schema_errors(Bible, bible_data)
    if validation_errors:
        raise HTTPException(422, "；".join(validation_errors))
    conn = get_conn()
    next_version = current_version + 1
    if _supports_bible_style_name(conn):
        conn.execute(
            "UPDATE projects SET bible_json=?, bible_version=?, bible_style_name=? WHERE id=?",
            (instance.model_dump_json(), next_version, style_name, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET bible_json=?, bible_version=? WHERE id=?",
            (instance.model_dump_json(), next_version, project_id),
        )
    conn.commit()

    scene_bible_ready = bool(instance.scenes)
    refs_started = False
    refs_error: str | None = None
    try:
        # 必须显式传全部具备定妆资格的角色名单：不传时 _start_refs_generation
        # 会用「已建卡角色缺口」扫描（_established_portrait_gap_names）当默认
        # 范围，那份扫描只看已经在 character_portraits 里出现过的角色——新
        # 架构下角色只随映射台按需建卡，凡是还没被任何一集映射过的角色永远
        # 不会出现在扫描结果里，画风切换对它们就成了悄悄的无害空转，本应
        # 与 _compute_style_regen_quote 的整包报价同口径的这条腿名不副实
        # （实战撞到：换画风后 5 个角色只有已建卡的 1 个重新出图）。
        refs_started = bool(_start_refs_generation(project_id, None, resume=False, only_characters=[c.name for c in instance.characters if character_is_portrait_eligible(c)]))
    except Exception as exc:  # noqa: BLE001 风格切换已落库；这条腿独立失败，不回滚风格
        refs_error = errors.record_and_format(
            exc, action="refs_spawn_after_style_change", context={"project_id": project_id},
        )
        conn.execute(
            "UPDATE projects SET refs_status='failed',refs_error=? WHERE id=?",
            (f"画风已切换，但定妆照未能启动重新生成，可在人物谱重试。{refs_error}", project_id),
        )
        conn.commit()

    scene_refs_started = False
    scene_refs_error: str | None = None
    if scene_bible_ready:
        scene_names = [scene.name for scene in instance.scenes]
        try:
            scene_refs_started = bool(
                _start_scene_refs_generation(project_id, scene_names, resume=False)
            )
        except Exception as exc:  # noqa: BLE001 风格切换已落库；这条腿独立失败，不回滚风格
            scene_refs_error = errors.record_and_format(
                exc, action="scene_refs_spawn_after_style_change", context={"project_id": project_id},
            )
            conn.execute(
                "UPDATE projects SET scene_refs_status='failed',scene_refs_error=? WHERE id=?",
                (f"画风已切换，但场景图未能启动重新生成，可在场景库重试。{scene_refs_error}", project_id),
            )
            conn.commit()

    _consume_payment_quote(str(quote_id), task_id=f"style_regen:{project_id}", run_id=None)
    return {
        "project_id": project_id,
        "style_name": style_name,
        "changed": True,
        "bible_version": next_version,
        "scene_bible_ready": scene_bible_ready,
        "scenes_total": len(instance.scenes),
        "refs_started": refs_started,
        "refs_error": refs_error,
        "scene_refs_started": scene_refs_started,
        "scene_refs_error": scene_refs_error,
        "quote_id": quote_id,
    }

@router.get("/projects/{project_id}/refs/gaps")
async def refs_gaps(project_id: str):
    """扫描定妆缺口：按角色/视角列出缺失原因。"""
    quote = _issue_scope_quote(compute_refs_precheck(project_id, resume=True))
    return {
        "project_id": project_id,
        "missing_count": len(quote.get("scope") or []),
        "image_count": quote.get("image_count"),
        "items": quote.get("scope") or [],
        "precheck": quote,
    }

@router.get("/projects/{project_id}/refs/progress")
async def refs_progress(project_id: str):
    """定妆细粒度进度：完成/当前/缺失/失败分项。"""
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    p = _project_or_404(project_id)
    effective_refs_status = "running" if _refs_generation_busy(project_id) else p.get("refs_status")
    if not p.get("bible_json"):
        return {
            "project_id": project_id,
            "refs_status": effective_refs_status,
            "total": 0,
            "ready": 0,
            "failed": 0,
            "missing": 0,
            "deferred": 0,
            "blocked": 0,
            "items": [],
        }
    bible = json.loads(p["bible_json"])
    conn = get_conn()
    items = []
    ready = failed = missing = deferred = blocked = 0
    for c in bible.get("characters") or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        if not character_is_portrait_eligible(c):
            blocked += 1
            items.append({
                "character": name,
                "status": "blocked",
                "reason": "外观依据未通过，当前不自动定妆",
                "missing_views": [],
            })
            continue
        row = conn.execute(
            """SELECT id, pack_status FROM character_portraits
               WHERE project_id=? AND character_name=? AND ep_end IS NULL
               ORDER BY ep_start DESC LIMIT 1""",
            (project_id, name),
        ).fetchone()
        if not row:
            missing += 1
            items.append({"character": name, "status": "missing", "missing_views": list(CHARACTER_REQUIRED_VIEWS)})
            continue
        views = conn.execute(
            "SELECT view_role, status FROM character_portrait_views WHERE portrait_id=?",
            (row["id"],),
        ).fetchall()
        have = {v["view_role"] for v in views if v["status"] == "ready"}
        need = [r for r in CHARACTER_REQUIRED_VIEWS if r not in have]
        pack = row["pack_status"] or "unknown"
        if pack == "ready" and not need:
            ready += 1
            status = "ready"
        elif pack == "failed" or need:
            if pack == "failed":
                failed += 1
                status = "failed"
            else:
                missing += 1
                status = "missing"
        else:
            status = pack
        items.append({
            "character": name,
            "status": status,
            "pack_status": pack,
            "missing_views": need,
            "current": effective_refs_status == "running" and (
                p.get("refs_target") == name or not p.get("refs_target")
            ),
        })
    return {
        "project_id": project_id,
        "refs_status": effective_refs_status,
        "refs_target": p.get("refs_target"),
        "total": ready + failed + missing,
        "ready": ready,
        "failed": failed,
        "missing": missing,
        "deferred": deferred,
        "blocked": blocked,
        "items": items,
        "updated_at": now(),
    }

@router.post("/projects/{project_id}/bible/draft")
async def save_bible_draft(project_id: str, body: dict):
    """保存人物谱草稿（不定稿、不失效下游、不升版本）。"""
    p = _project_or_404(project_id)
    expected_version = body.get("expected_version")
    if expected_version is not None and int(expected_version) != int(p.get("bible_version") or 0):
        raise HTTPException(409, detail=_bible_conflict_detail(p, expected_version))
    draft = body.get("bible") if isinstance(body.get("bible"), dict) else {
        k: v for k, v in (body or {}).items()
        if k not in {"expected_version", "confirm", "impact_preview_fingerprint"}
    }
    conn = get_conn()
    # 兼容旧库：无列时写入 bible_feedback 旁路字段不可行，使用独立列迁移
    try:
        conn.execute(
            "UPDATE projects SET bible_draft_json=?, bible_draft_updated_at=? WHERE id=?",
            (json.dumps(draft, ensure_ascii=False), now(), project_id),
        )
    except Exception:
        conn.execute("ALTER TABLE projects ADD COLUMN bible_draft_json TEXT")
        conn.execute("ALTER TABLE projects ADD COLUMN bible_draft_updated_at REAL")
        conn.execute(
            "UPDATE projects SET bible_draft_json=?, bible_draft_updated_at=? WHERE id=?",
            (json.dumps(draft, ensure_ascii=False), now(), project_id),
        )
    conn.commit()
    return {
        "saved": True,
        "draft": True,
        "bible_version": int(p.get("bible_version") or 0),
        "updated_at": now(),
    }

@router.get("/projects/{project_id}/bible/draft")
async def get_bible_draft(project_id: str):
    p = _project_or_404(project_id)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT bible_draft_json, bible_draft_updated_at, bible_version FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
    except Exception:
        return {"draft": None, "bible_version": int(p.get("bible_version") or 0)}
    draft = None
    if row and row["bible_draft_json"]:
        try:
            draft = json.loads(row["bible_draft_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            draft = None
    return {
        "draft": draft,
        "updated_at": row["bible_draft_updated_at"] if row else None,
        "bible_version": int((row["bible_version"] if row else p.get("bible_version")) or 0),
    }
