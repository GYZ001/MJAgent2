"""已发布人物谱/单角色卡的编辑与修订提交、单角色立绘提示词编辑。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    new_id,
    now,
)
from app.domain.common import (
    _project_or_404,
    router,
)
from app.evidence import repository as evidence_repository
from app.harness.types import (
    Evaluation,
    EvidenceArtifact,
)
from app.schemas import (
    Bible,
    schema_errors,
)
from fastapi import HTTPException

from .precheck import (
    _artifact_type_counts,
    _bible_conflict_detail,
    _parse_bible_write_body,
    _purge_for_style_change,
    _purge_removed_character_portraits,
    compute_bible_impact_preview,
)


@router.put("/projects/{project_id}/bible")
async def edit_bible(project_id: str, body: dict):
    from app.capabilities.dispatch import ui_route

    bible_body, expected_version, confirm, impact_fp = _parse_bible_write_body(body or {})

    routed = await ui_route(
        "bible.update",
        {
            "project_id": project_id,
            "bible": bible_body,
            "expected_version": expected_version,
            "confirm": confirm,
            "impact_preview_fingerprint": impact_fp,
        },
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if expected_version is None:
        raise HTTPException(
            409,
            detail={
                "code": "EXPECTED_VERSION_REQUIRED",
                "message": "定稿人物谱必须携带 expected_version，以防止并发覆盖",
                "current_version": int(p.get("bible_version") or 0),
            },
        )
    if int(expected_version) != int(p.get("bible_version") or 0):
        raise HTTPException(409, detail=_bible_conflict_detail(p, expected_version))
    if not confirm:
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_CONFIRM_REQUIRED",
                "message": "必须先完成定稿影响预检并显式确认（confirm=true）",
            },
        )
    try:
        preview = compute_bible_impact_preview(
            project_id, bible_body, expected_version=expected_version,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_PREVIEW_UNAVAILABLE",
                "message": f"定稿影响预检失败，已阻止正式定稿：{exc}",
            },
        ) from exc
    if not impact_fp or impact_fp != preview.get("fingerprint"):
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_PREVIEW_STALE",
                "message": "影响预检已过期或缺失，请重新预检后再定稿",
                "preview": preview,
            },
        )

    instance, errors = schema_errors(Bible, bible_body)
    if errors:
        raise HTTPException(422, "；".join(errors))
    from app.validators import validate_bible
    errors = validate_bible(instance)
    if errors:
        raise HTTPException(422, "；".join(errors))
    old_style = None
    if p["bible_json"]:
        old_style = (json.loads(p["bible_json"]).get("world") or {}).get("visual_style_canonical")
    style_changed = bool(old_style) and instance.world.visual_style_canonical != old_style
    purge_info = _purge_for_style_change(project_id, instance) if style_changed else None
    conn = get_conn()
    previous_artifact_id = p.get("bible_artifact_id")
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id=project_id,
        status="validated",
        trust_level="T4",
        content=instance.model_dump(mode="json"),
        parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
        contract_version="character-bible-1.0.0",
    ))
    artifact = evidence_repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="human",
        evaluator_name="bible_editor",
        evaluator_version="1.0.0",
        status="passed",
        hard_gate_passed=True,
        score=100,
        evidence={"decision": "manual_edit", "style_changed": style_changed},
    )])
    stale_ids = evidence_repository.invalidate_descendants(
        previous_artifact_id,
        "人物谱已人工修订，需要重新复验下游产物",
        exclude_ids={artifact["id"]},
    ) if previous_artifact_id else []
    conn.execute(
        "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_artifact_id=?, "
        "bible_status='ready', bible_error=NULL WHERE id=?",
        (instance.model_dump_json(), artifact["id"], project_id),
    )
    conn.execute(
        "INSERT INTO gate_decisions(id, artifact_id, gate_key, decision, decided_by, reason, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (new_id("gate"), artifact["id"], "character_bible", "approve", "bible_editor", "人工修订并定稿", now()),
    )
    conn.commit()
    # 主事务落定之后才动被删角色的定妆照：删文件不可回滚，写库失败时必须一个文件都没碰。
    purged_portraits = _purge_removed_character_portraits(
        conn, project_id, p["bible_json"], instance,
    )
    return {
        "bible_version_bumped": True,
        "style_changed": style_changed,
        "purged": purge_info,
        "purged_removed_character_portraits": purged_portraits,
        "artifact_id": artifact["id"],
        "bible_version": int(p.get("bible_version") or 0) + 1,
        "impact": {
            "stale_descendant_ids": stale_ids,
            "requires_reconfirm": bool(stale_ids),
            "paid_media_invalidated": bool(style_changed or stale_ids),
            "by_artifact_type": _artifact_type_counts(stale_ids),
            "change_types": preview.get("change_types"),
            "rebuild": preview.get("rebuild"),
        },
    }

def _commit_bible_revision(project_id: str, p: dict, instance: "Bible", *, reason: str) -> dict:
    previous_artifact_id = p.get("bible_artifact_id")
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id=project_id,
        status="validated",
        trust_level="T4",
        content=instance.model_dump(mode="json"),
        parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
        contract_version="character-bible-1.0.0",
    ))
    artifact = evidence_repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="human",
        evaluator_name="bible_editor",
        evaluator_version="1.0.0",
        status="passed",
        hard_gate_passed=True,
        score=100,
        evidence={"decision": "manual_edit", "reason": reason},
    )])
    stale_ids = evidence_repository.invalidate_descendants(
        previous_artifact_id,
        "人物谱已人工修订，需要重新复验下游产物",
        exclude_ids={artifact["id"]},
    ) if previous_artifact_id else []
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_artifact_id=?, "
        "bible_status='ready', bible_error=NULL WHERE id=?",
        (instance.model_dump_json(), artifact["id"], project_id),
    )
    conn.execute(
        "INSERT INTO gate_decisions(id, artifact_id, gate_key, decision, decided_by, reason, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (new_id("gate"), artifact["id"], "character_bible", "approve", "bible_editor", reason, now()),
    )
    conn.commit()
    return {
        "artifact_id": artifact["id"],
        "stale_descendant_ids": stale_ids,
        "by_artifact_type": _artifact_type_counts(stale_ids),
        "bible_version": int(p.get("bible_version") or 0) + 1,
    }

@router.put("/projects/{project_id}/characters/{character_name}")
async def edit_character(project_id: str, character_name: str, body: dict):
    """角色级保存：只替换指定角色对象，并按 bible_version 做乐观并发控制。"""
    payload = body or {}
    expected_version = payload.get("expected_version")
    if expected_version is None:
        expected_version = (payload.get("character") or {}).get("expected_version") if isinstance(payload.get("character"), dict) else None
    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经")
    if expected_version is None:
        raise HTTPException(
            409,
            detail={
                "code": "EXPECTED_VERSION_REQUIRED",
                "message": "保存角色必须携带 expected_version，以防止并发覆盖",
                "current_version": int(p.get("bible_version") or 0),
            },
        )
    if int(expected_version) != int(p.get("bible_version") or 0):
        raise HTTPException(409, detail=_bible_conflict_detail(p, expected_version))

    character_body = payload.get("character")
    if not isinstance(character_body, dict):
        raise HTTPException(422, "character 必须是角色对象")
    character_body = dict(character_body)
    character_body.setdefault("name", character_name)
    if character_body.get("name") != character_name:
        raise HTTPException(422, "角色 name 与路径 character_name 不一致")

    next_bible = json.loads(p["bible_json"])
    target_idx = next(
        (idx for idx, item in enumerate(next_bible.get("characters", [])) if item.get("name") == character_name),
        None,
    )
    if target_idx is None:
        raise HTTPException(404, f"角色不存在：{character_name}")
    next_bible["characters"][target_idx] = character_body

    instance, errors = schema_errors(Bible, next_bible)
    if errors:
        raise HTTPException(422, "；".join(errors))
    from app.validators import validate_bible
    errors = validate_bible(instance)
    if errors:
        raise HTTPException(422, "；".join(errors))

    try:
        preview = compute_bible_impact_preview(
            project_id, next_bible, expected_version=expected_version,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_PREVIEW_UNAVAILABLE",
                "message": f"定稿影响预检失败，已阻止正式定稿：{exc}",
            },
        ) from exc
    impact_fp = payload.get("impact_preview_fingerprint")
    if payload.get("confirm") is not True or not impact_fp:
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_CONFIRM_REQUIRED",
                "message": "任何角色定稿变更都必须先完成影响预检并显式确认",
                "preview": preview,
            },
        )
    if impact_fp != preview.get("fingerprint"):
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_PREVIEW_STALE",
                "message": "影响预检已过期或缺失，请重新预检后再保存",
                "preview": preview,
            },
        )

    revision = _commit_bible_revision(project_id, p, instance, reason=f"人工保存角色：{character_name}")
    return {
        "saved": True,
        "character": character_name,
        "bible_version": revision["bible_version"],
        "artifact_id": revision["artifact_id"],
        "impact": {
            "change_types": preview.get("change_types"),
            "stale_descendant_ids": revision["stale_descendant_ids"],
            "by_artifact_type": revision["by_artifact_type"],
            "rebuild": preview.get("rebuild"),
        },
    }

@router.put("/projects/{project_id}/characters/{character_name}/portrait")
async def edit_portrait_prompt(project_id: str, character_name: str, body: dict):
    """更新单个角色的画像描述（定妆照生成词）。传空字符串/null 恢复为默认合成描述。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "portrait.update_prompt",
        {"project_id": project_id, "character": character_name, "prompt": (body.get("portrait_prompt") or "")},
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    prompt_text = (body.get("portrait_prompt") or "").strip()
    if prompt_text and not 10 <= len(prompt_text) <= 400:
        raise HTTPException(422, f"画像描述长度 {len(prompt_text)} 字，要求 10~400 字（留空则恢复默认）")
    bible = json.loads(p["bible_json"])
    target = next((c for c in bible.get("characters", []) if c.get("name") == character_name), None)
    if target is None:
        raise HTTPException(404, f"角色不存在：{character_name}")
    target["portrait_prompt_override"] = prompt_text or None
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(bible, ensure_ascii=False), project_id))
    conn.commit()
    return {"saved": True, "reset_to_default": not prompt_text}
