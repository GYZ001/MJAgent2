"""「参考资产已更新」面板的领域逻辑与 REST 入口：只读影响分析、成组重生成、
整组原子采纳。三件事的判据与原语全部复用既有机制（``asset_drift``/
``asset_refresh_report``/``adopt``/``quota``）；两个 mutating 入口注册为
Command Bus 命令（``video.asset_refresh_regenerate``/``video.asset_refresh_
adopt``，见 ``app/capabilities/commands/video.py``），不走豁免路由——它们都
是会花钱生成视频/替换已采用版本的真实领域动作，不是协议/编排入口
（CLAUDE.md「写不出理由的端点就该老实注册成命令」同款口径，见
``app/capabilities/exemptions.py`` 模块文档）。
"""
from __future__ import annotations

from app import quota, worker
from app.db import get_conn
from app.domain.common import _episode_or_404, router
from app.domain.review_wall import _review_write_audit
from fastapi import HTTPException

from . import asset_drift as drift
from .adopt import _adopt_version_apply
from .asset_refresh_report import candidate_adopt_check, episode_asset_refresh_groups


def _account_quota(conn, project_id: str) -> dict | None:
    owner_user_id = quota.owner_of_project(conn, project_id)
    if not owner_user_id:
        return None
    limits = quota.effective_limits(conn, owner_user_id)
    addon_balance = quota.addon_video_seconds_balance(conn, owner_user_id)
    if limits.video_seconds is None:
        return {"unlimited": True, "sub_remaining": None, "addon_balance": addon_balance, "total_remaining": None}
    anchor = quota.period_anchor(conn, owner_user_id)
    used = quota.usage_for(conn, owner_user_id, "video_seconds", quota.period_index(anchor))
    sub_remaining = max(0.0, limits.video_seconds - used)
    return {
        "unlimited": False, "sub_remaining": sub_remaining, "addon_balance": addon_balance,
        "total_remaining": sub_remaining + addon_balance, "reset_at": quota.period_reset_at(anchor),
    }


@router.get("/episodes/{episode_id}/asset-refresh")
def asset_refresh_report(episode_id: str):
    """生成台/成片台「参考资产已更新」面板的数据来源：按实体成组，给出每段
    状态与额度估算；只读，不触发任何生成或采纳，GET 走通用读面权限，不需要
    登记命令。"""
    ep = _episode_or_404(episode_id)
    conn = get_conn()
    report = episode_asset_refresh_groups(conn, ep)
    account = _account_quota(conn, ep["project_id"])
    needed = report["needs_regen_seconds"]
    enough = account is None or account["unlimited"] or account["total_remaining"] >= needed
    return {
        "episode_id": episode_id,
        "groups": report["groups"],
        "quota": {
            "needs_regen_shot_count": report["needs_regen_shot_count"],
            "needs_regen_seconds": needed,
            "account": account,
            "enough": enough,
        },
    }


async def _asset_refresh_regenerate_core(episode_id: str, body: dict) -> dict:
    """供 REST 路由与 ``video.asset_refresh_regenerate`` Command Handler 共用。"""
    ep = _episode_or_404(episode_id)
    idempotency_key = str(body.get("idempotency_key") or "").strip()
    if not idempotency_key:
        raise HTTPException(422, "缺少 idempotency_key")
    entity_keys = set(body.get("entity_keys") or [])
    conn = get_conn()
    report = episode_asset_refresh_groups(conn, ep)
    target_groups = [g for g in report["groups"] if not entity_keys or g["entity_key"] in entity_keys]
    shot_ids = sorted({sid for g in target_groups for sid in g["needs_regen_shot_ids"]})
    if not shot_ids:
        return {"episode_id": episode_id, "queued": [], "errors": [], "message": "没有需要重生成的段落"}
    # 函数内导入：app.capabilities.dispatch 模块级 import app.domain 路由会
    # 与之形成循环（见该模块 docstring「曾是全后端最大强连通分量里贡献最大
    # 的两条团内边」），既有 domain 路由同款调用处都是这个理由。
    from app.capabilities.dispatch import dispatch, respond_ui

    queued: list[str] = []
    errors: list[dict] = []
    for shot_id in shot_ids:
        try:
            result = await dispatch("video.generate_shot", {
                "shot_id": shot_id, "reroll": True,
                "qualification_version": body.get("qualification_version"),
                "idempotency_key": f"{idempotency_key}:{shot_id}", "request_id": body.get("request_id"),
            }, initiator="ui")
            respond_ui(result)
            queued.append(shot_id)
        except HTTPException as exc:
            errors.append({"shot_id": shot_id, "status_code": exc.status_code, "detail": exc.detail})
    message = f"已为 {len(queued)} 段提交重生成" + (f"，{len(errors)} 段被拦住" if errors else "")
    return {"episode_id": episode_id, "queued": queued, "errors": errors, "message": message}


@router.post("/episodes/{episode_id}/asset-refresh/regenerate")
async def asset_refresh_regenerate(episode_id: str, body: dict | None = None):
    """对所选组里"需要重生成"的段逐一走现有单镜生成命令（经命令总线与全部
    闸门）；串行发起，闸门 409 原样透传且不中断其余段落。"""
    # 函数内导入：同 _asset_refresh_regenerate_core 内 dispatch 的理由——
    # app.capabilities.dispatch 模块级 import app.domain 路由会成环。
    from app.capabilities.dispatch import ui_route

    payload = dict(body) if isinstance(body, dict) else {}
    routed = await ui_route("video.asset_refresh_regenerate", {
        "episode_id": episode_id, "entity_keys": sorted(payload.get("entity_keys") or []),
        "qualification_version": payload.get("qualification_version"),
        "idempotency_key": payload.get("idempotency_key"), "request_id": payload.get("request_id"),
    })
    if routed is not None:
        return routed
    return await _asset_refresh_regenerate_core(episode_id, payload)


def _assert_adopt_request(payload: dict) -> tuple[str, dict, str]:
    entity_key = str(payload.get("entity_key") or "").strip()
    versions = payload.get("versions") or {}
    reason = str(payload.get("reason") or "").strip()
    if not entity_key or not isinstance(versions, dict) or not versions:
        raise HTTPException(422, "缺少 entity_key 或 versions")
    if len(reason) < 4:
        raise HTTPException(422, "请填写有效的采用理由（至少 4 个字）")
    return entity_key, versions, reason


def _assert_group_covered(conn, ep, entity_key: str, versions: dict) -> None:
    """整组采用前的闸门：本组若还有「需要重生成」的段，或还有「已有候选」的段
    没被本次 ``versions`` 覆盖，一律拒绝——只靠前端禁用按钮挡不住直连 API 的
    调用，必须在这里也拦住（CLAUDE.md「不做部分采用」）。entity_key 当前不
    在任何分组里（已无变化/从未变化）时不做这道检查，保留既有行为：那种情况
    没有"本组"可供完整性比较。"""
    report = episode_asset_refresh_groups(conn, ep)
    group = next((g for g in report["groups"] if g["entity_key"] == entity_key), None)
    if group is None:
        return
    if group["needs_regen_shot_ids"]:
        raise HTTPException(409, {
            "code": "ASSET_REFRESH_ADOPT_INCOMPLETE",
            "message": f"本组还有 {len(group['needs_regen_shot_ids'])} 段尚未重生成，不能整组采用",
            "needs_regen_shot_ids": group["needs_regen_shot_ids"],
        })
    has_candidate_ids = {m["shot_id"] for m in group["members"] if m["status"] == "has_candidate"}
    missing = sorted(has_candidate_ids - set(versions))
    if missing:
        raise HTTPException(409, {
            "code": "ASSET_REFRESH_ADOPT_INCOMPLETE",
            "message": f"本组还有 {len(missing)} 段未选定候选版本，不能整组采用",
            "missing_shot_ids": missing,
        })


def _asset_refresh_adopt_core(episode_id: str, body: dict) -> dict:
    """供 REST 路由与 ``video.asset_refresh_adopt`` Command Handler 共用：整
    组原子采纳——逐段核验版本归属本镜、已成功、技术校验可用、其冻结参考已
    是当前最新；任何一段不满足就整组拒绝（409），全部满足才在一个事务里一
    次性替换、一次性失效交付权威，不做部分采用。"""
    ep = _episode_or_404(episode_id)
    entity_key, versions, reason = _assert_adopt_request(body)
    conn = get_conn()
    project_id, episode_no = ep["project_id"], ep["episode_no"]
    _assert_group_covered(conn, ep, entity_key, versions)
    bible, screenplay = drift.episode_bible_and_screenplay(conn, ep, project_id)
    for shot_id, version_id in versions.items():
        shot_row = conn.execute("SELECT * FROM shots WHERE id=? AND episode_id=?", (shot_id, episode_id)).fetchone()
        if not shot_row:
            raise HTTPException(409, {"code": "ASSET_REFRESH_ADOPT_REJECTED", "shot_id": shot_id, "message": "镜头不属于本集"})
        ok, msg = candidate_adopt_check(
            conn, shot_row, version_id, project_id=project_id, episode_no=episode_no, bible=bible, screenplay=screenplay,
        )
        if not ok:
            raise HTTPException(409, {"code": "ASSET_REFRESH_ADOPT_REJECTED", "shot_id": shot_id, "message": msg})
    applied: list[tuple[str, str, dict]] = []
    try:
        for shot_id, version_id in versions.items():
            result = _adopt_version_apply(shot_id, {
                "version_id": version_id, "reason": reason, "human_override": True, "playback_rate": 1.0,
                "qualification_version": body.get("qualification_version"),
            }, conn=conn)
            applied.append((shot_id, version_id, result))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    for shot_id, version_id, result in applied:
        _review_write_audit(
            "video_version.adopt", "shot", shot_id, target_version=version_id,
            old_state=result["_old_state"], new_state=result["_new_state"], reason=reason,
            idempotency_key=body.get("idempotency_key"), request_id=body.get("request_id"),
        )
    worker.invalidate_episode_final(episode_id)
    return {
        "episode_id": episode_id, "entity_key": entity_key,
        "adopted": [{"shot_id": s, "version_id": v} for s, v, _ in applied],
    }


@router.post("/episodes/{episode_id}/asset-refresh/adopt")
async def asset_refresh_adopt(episode_id: str, body: dict | None = None):
    """整组原子采纳的 REST 入口，见 ``_asset_refresh_adopt_core`` 文档。"""
    # 函数内导入：同上一个路由函数，同一条理由。
    from app.capabilities.dispatch import ui_route

    payload = dict(body) if isinstance(body, dict) else {}
    routed = await ui_route("video.asset_refresh_adopt", {
        "episode_id": episode_id, "entity_key": payload.get("entity_key"),
        "versions": payload.get("versions") or {}, "reason": payload.get("reason"),
        "qualification_version": payload.get("qualification_version"),
        "idempotency_key": payload.get("idempotency_key"), "request_id": payload.get("request_id"),
    })
    if routed is not None:
        return routed
    return _asset_refresh_adopt_core(episode_id, payload)
