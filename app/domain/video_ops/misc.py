"""混音状态查询、拼接触发与陈旧素材预览/修复。

从 app/domain/video_ops.py 按原样搬移；依赖 generate。
"""
from __future__ import annotations

import asyncio

from app import (
    worker,
)
from app.db import get_conn
from app.domain.common import (
    _episode_or_404,
    router,
)
from app.domain.review_wall import (
    _review_assert_positive_action,
    _review_sha,
    _review_upstream_snapshot,
)
from fastapi import HTTPException

from .generate import _generate_shot_core


@router.get("/episodes/{episode_id}/mix-status")
def mix_status(episode_id: str):
    """按镜号顺序返回每镜成片 URL、整体进度、已合成成品（若有）。"""
    _episode_or_404(episode_id)
    return worker.episode_mix_status(episode_id)

@router.post("/episodes/{episode_id}/concatenate")
async def concatenate(episode_id: str, body: dict | None = None):
    """把本集所有已采用的视频片段按镜号顺序拼接成一个 MP4。"""
    from app.capabilities.dispatch import ui_route
    payload = dict(body) if isinstance(body, dict) else {}
    routed = await ui_route("delivery.concatenate", {
        "episode_id": episode_id,
        "idempotency_key": payload.get("idempotency_key"),
        "request_id": payload.get("request_id"),
    })
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    try:
        return await asyncio.to_thread(worker.concatenate_episode, episode_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"ffmpeg 合成失败：{exc}")

@router.get("/episodes/{episode_id}/stale-assets-preview")
def stale_assets_preview(episode_id: str):
    """生成台：资产/分镜 stale 影响预览（只读）。"""
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    from app.domain.storyboard_ops import _shot_video_is_stale, _shot_adopted_assets_stale
    from app.video_cost_model import initial_shot_generation_cost
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    items = []
    for row in rows:
        shot = dict(row)
        stale = _shot_video_is_stale(conn, shot, ep.get("storyboard_artifact_id"))
        if not stale:
            continue
        reasons = []
        try:
            if ep.get("storyboard_artifact_id") and shot.get("storyboard_artifact_id") and (
                shot["storyboard_artifact_id"] != ep["storyboard_artifact_id"]
            ):
                reasons.append("storyboard_artifact")
        except (KeyError, TypeError):
            pass
        adopted = shot.get("adopted_version_id")
        if adopted:
            ver = conn.execute(
                "SELECT artifact_id, image_inputs FROM shot_versions WHERE id=?", (adopted,)
            ).fetchone()
            if ver and _shot_adopted_assets_stale(conn, shot, ver):
                reasons.append("asset_revision")
        if not reasons:
            reasons.append("parent_artifact")
        reason_labels = {
            "storyboard_artifact": "已确认分镜版本已变更",
            "asset_revision": "人物或场景资产版本已变更",
            "parent_artifact": "上游证据链已变更",
        }
        items.append({
            "shot_id": shot["id"],
            "shot_no": shot["shot_no"],
            "adopted_version_id": adopted,
            "reasons": reasons,
            "reason_labels": [reason_labels.get(reason, "未知陈旧原因") for reason in reasons],
            "storyboard_artifact_id": shot.get("storyboard_artifact_id"),
            "current_storyboard_artifact_id": ep.get("storyboard_artifact_id"),
            "estimated_cost_cny": initial_shot_generation_cost(
                float(shot.get("duration_s") or 0)
            ),
            "hint": "参考资产或分镜已更新，本镜采用版可能使用旧证据链",
        })
    qualification = _review_upstream_snapshot(episode_id)
    for item in items:
        asset_inputs = [
            asset for asset in qualification["assets"].get("inputs", [])
            if asset.get("shot_id") == item["shot_id"]
        ]
        item["asset_qualification"] = asset_inputs
        item["asset_soft_warnings"] = [
            warning for warning in qualification["assets"].get("soft_warnings", [])
            if warning.get("shot_id") == item["shot_id"]
        ]
        item["rule_versions"] = sorted({
            str(asset.get("rule_version")) for asset in asset_inputs if asset.get("rule_version")
        })
    preview_version = _review_sha({
        "episode_id": episode_id,
        "qualification_version": qualification["qualification_version"],
        "shots": [(item["shot_id"], item["adopted_version_id"], item["reasons"]) for item in items],
    })[:32]
    return {
        "episode_id": episode_id,
        "stale_count": len(items),
        "shots": items,
        "estimated_cost_cny": round(sum(item["estimated_cost_cny"] for item in items), 2),
        "qualification": qualification,
        "preview_version": preview_version,
        "repair_action": "POST /api/episodes/{id}/repair-stale-assets with confirm=true",
    }

@router.post("/episodes/{episode_id}/repair-stale-assets")
async def repair_stale_assets(episode_id: str, body: dict | None = None):
    """批量修复 stale 镜头：对指定/全部 stale 镜强制重抽新视频版本（保留旧采用版直至新版成功）。"""
    body = body or {}
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "video.repair_stale_assets",
        {
            "episode_id": episode_id,
            "shot_ids": body.get("shot_ids") or [],
            "confirm": body.get("confirm") is True,
            "preview_version": body.get("preview_version"),
            "qualification_version": body.get("qualification_version"),
            "idempotency_key": body.get("idempotency_key"),
        },
    )
    if routed is not None:
        return routed
    if body.get("confirm") is not True:
        raise HTTPException(409, "必须先查看 stale-assets-preview，并显式提交 confirm=true")
    preview = stale_assets_preview(episode_id)
    _review_assert_positive_action(episode_id, body.get("qualification_version"))
    if body.get("preview_version") and body.get("preview_version") != preview["preview_version"]:
        raise HTTPException(409, {
            "code": "STALE_PREVIEW_EXPIRED",
            "message": "陈旧资产范围或依赖已变化，请重新预演",
            "preview": preview,
        })
    wanted = set(body.get("shot_ids") or [])
    targets = [
        item for item in preview["shots"]
        if not wanted or item["shot_id"] in wanted
    ]
    if not targets:
        return {"queued": 0, "shot_ids": [], "message": "没有需要修复的 stale 镜头"}
    queued = []
    errors = []
    for item in targets:
        try:
            result = await _generate_shot_core(item["shot_id"], {
                "reroll": True,
                "qualification_version": preview["qualification"]["qualification_version"],
                "idempotency_key": f"{body.get('idempotency_key') or preview['preview_version']}:{item['shot_id']}",
            })
            queued.append({"shot_id": item["shot_id"], "shot_no": item["shot_no"], "result": result})
        except Exception as exc:  # noqa: BLE001
            errors.append({"shot_id": item["shot_id"], "shot_no": item["shot_no"], "error": str(exc)})
    return {
        "queued": len(queued),
        "shot_ids": [q["shot_id"] for q in queued],
        "errors": errors,
        "message": f"已为 {len(queued)} 个 stale 镜头提交重生",
        "preview_version": preview["preview_version"],
    }
