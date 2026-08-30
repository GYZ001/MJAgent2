"""参考图使用状态的丢弃与恢复。

从 app/domain/video_ops.py 按原样搬移。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _as_body_dict,
    router,
)
from app.domain.review_wall import (
    _review_assert_reference_restore,
    _review_write_audit,
)
from fastapi import (
    Body,
    HTTPException,
)


def _set_reference_image_used(
    version_id: str, ref_id: str, *, use: bool, override_reason: str | None = None,
) -> dict:
    """素材画廊里把某张参考图标记为「废弃」或「恢复使用」。
    废弃后该图不再喂给视频模型（见 video_modes.build_seedance_image_inputs），仅留作展示。"""
    conn = get_conn()
    v = conn.execute("SELECT image_inputs FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise HTTPException(404, "视频版本不存在")
    meta = json.loads(v["image_inputs"] or "{}")
    refs = meta.get("reference_images") or []
    target = next((r for r in refs if r.get("id") == ref_id), None)
    if target is None:
        raise HTTPException(404, "参考图不存在")
    if use:
        _review_assert_reference_restore(version_id, ref_id)
    if use and target.get("rejectReason") and not (override_reason or "").strip():
        raise HTTPException(400, "恢复质检淘汰的参考图必须填写覆盖理由")
    changed = target.get("deleted") != (not use) or target.get("selectedForSeedance") != use
    target["deleted"] = not use
    target["selectedForSeedance"] = use
    if use and (override_reason or "").strip():
        target["restoreOverrideReason"] = override_reason.strip()
        target["restoredAt"] = now()
        changed = True
    meta["reference_images"] = refs
    if changed:
        meta["reference_gallery_revision"] = now()
        meta["reference_gallery_edited"] = True
        if use and str(target.get("type") or "") == "plot_key_frame":
            # 只有用户明确恢复/保留关键帧才允许跨 prompt 合同复用；
            # 编辑场景图/人物图不应让旧关键帧永久绕过升级。
            meta["reference_gallery_contract_override"] = True
    conn.execute("UPDATE shot_versions SET image_inputs=? WHERE id=?",
                 (json.dumps(meta, ensure_ascii=False), version_id))
    conn.commit()
    _review_write_audit(
        "reference.restore" if use else "reference.discard",
        "version", version_id, target_version=str(meta.get("reference_gallery_revision") or ""),
        old_state={"ref_id": ref_id, "deleted": not target.get("deleted")},
        new_state={"ref_id": ref_id, "deleted": not use}, reason=override_reason,
    )
    return {
        "version_id": version_id,
        "ref_id": ref_id,
        "deleted": not use,
        "override_reason": (override_reason or "").strip() or None,
    }

@router.delete("/versions/{version_id}/reference-images/{ref_id}")
async def discard_reference_image(version_id: str, ref_id: str):
    """废弃一张参考图：移入废弃画廊，且后续调用视频模型时不再使用它。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "reference.review",
        {"version_id": version_id, "ref_id": ref_id, "action": "discard"},
    )
    if routed is not None:
        return routed
    return _set_reference_image_used(version_id, ref_id, use=False)

@router.post("/versions/{version_id}/reference-images/{ref_id}/restore")
async def restore_reference_image(version_id: str, ref_id: str, body: dict | None = Body(None)):
    """把废弃画廊里的参考图恢复为可用（重新计入喂给视频模型的参考图）。
    若该图曾被 QA 淘汰，body.override_reason 必填，写入审计字段。"""
    from app.capabilities.dispatch import ui_route
    body = _as_body_dict(body)
    routed = await ui_route(
        "reference.review",
        {
            "version_id": version_id,
            "ref_id": ref_id,
            "action": "restore",
            "override_reason": body.get("override_reason"),
        },
    )
    if routed is not None:
        return routed
    return _set_reference_image_used(
        version_id, ref_id, use=True, override_reason=body.get("override_reason"),
    )
