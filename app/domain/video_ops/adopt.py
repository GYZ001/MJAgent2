"""镜头版本采纳与采纳取消。

从 app/domain/video_ops.py 按原样搬移。
"""
from __future__ import annotations

import json

from app import worker
from app.auth.principal import current_actor_name
from app.db import (
    get_conn,
    new_id,
    now,
)
from app.domain.common import router
from app.domain.review_wall import (
    _review_assert_shot_positive,
    _review_write_audit,
)
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation
from fastapi import HTTPException
from pathlib import Path


def _assert_version_adoptable(version, body: dict) -> bool:
    """返回 human_override。系统代采只接受 succeeded；人工采纳还接受 waiting_human（闸门拦下、
    交人判断的版本），前提是视频文件真实存在——人工采纳是最高优先级，但采纳一个不存在的文件没有意义。"""
    human_override = bool(body.get("human_override"))
    if not version:
        raise HTTPException(409, "该版本不存在或未成功")
    if version["status"] == "succeeded":
        return human_override
    if human_override and version["status"] == "waiting_human" and version["video_path"] and Path(version["video_path"]).is_file():
        return True
    raise HTTPException(409, "该版本不存在或未成功")


def _quality_issues_overridden(technical: dict, human_override: bool) -> list[str]:
    """技术门禁未过时：人工采纳可越过 category=quality 的判定（字幕闸门等主观/视觉判定），返回被越过的
    issue code 供审计；文件级问题（不存在、容器坏、时长异常，category 非 quality）谁都不能越过。"""
    if technical.get("passed"):
        return []
    issues = [i for i in (technical.get("issues") or []) if isinstance(i, dict)]
    if human_override and issues and all(str(i.get("category") or "") == "quality" for i in issues):
        return [str(i.get("code") or "") for i in issues]
    raise HTTPException(409, "视频技术门禁未通过，不能人工采用" if not human_override else "视频文件本身不可用（不存在、容器损坏或时长异常），不能采纳")


def _settle_waiting_human(conn, version, overridden: list[str]) -> None:
    """人工采纳了「等待人工」的版本：这就是那个人工处理，把版本落成 succeeded，合成才认它。"""
    if version["status"] == "waiting_human" and overridden:
        conn.execute("UPDATE shot_versions SET status='succeeded', error=NULL WHERE id=?", (version["id"],))


def _adopt_version_core(shot_id: str, body: dict) -> dict:
    """人工采用视频版本的领域逻辑，供 REST 路由与 ``video.adopt_version`` Command Handler 共用。"""
    from app.video_playback import normalize_playback_rate

    version_id = body.get("version_id")
    try:
        playback_rate = normalize_playback_rate(body.get("playback_rate"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _review_assert_shot_positive(shot_id, body.get("qualification_version"))
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=? AND shot_id=?", (version_id, shot_id)).fetchone()
    human_override = _assert_version_adoptable(v, body)
    from app.evidence import media as media_evidence

    try:
        artifact = media_evidence.record_video_candidate(version_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"候选证据创建失败：{exc}") from exc
    technical = json.loads(v["technical_validation_json"] or "{}")
    if not technical:
        refreshed = conn.execute(
            "SELECT technical_validation_json FROM shot_versions WHERE id=?", (version_id,)
        ).fetchone()
        technical = json.loads(refreshed["technical_validation_json"] or "{}")
    overridden = _quality_issues_overridden(technical, human_override)
    qa = json.loads(v["qa_json"] or "{}")
    observed_state_out = qa.get("observed_state_out")
    if observed_state_out:
        media_evidence.persist_candidate_observed_state_out(
            version_id,
            str(observed_state_out),
        )
    reason = str(body.get("reason") or "").strip()
    if len(reason) < 4:
        raise HTTPException(422, "请填写有效的采用理由（至少 4 个字，说明质量、成本或版本比较）")
    evidence_repository.commit_artifact(
        None,
        artifact["id"],
        [Evaluation(
            evaluator_type="human", evaluator_name=current_actor_name(),
            evaluator_version="1.0.0", status="passed", hard_gate_passed=True,
            score=100, evidence={
                "decision": "adopt", "reason": reason, "playback_rate": playback_rate, "overrode_quality_issues": overridden,
            },
        )],
    )
    shot = conn.execute("SELECT episode_id, adopted_version_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    previous_rate = float(v["playback_rate"] or 1.0)
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    _settle_waiting_human(conn, v, overridden)
    conn.execute(
        "UPDATE shot_versions SET adoption_reason=?, playback_rate=? WHERE id=?",
        (reason + (f"（人工越过质量判定：{'、'.join(overridden)}）" if overridden else ""), playback_rate, version_id),
    )
    conn.execute(
        """INSERT INTO gate_decisions(
               id, artifact_id, gate_key, decision, decided_by, reason, created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            new_id("gate"), artifact["id"], "video_adoption", "approve",
            current_actor_name(), reason, now(),
        ),
    )
    from app.video_plan import reconcile_adopted_revision
    reconcile_result = reconcile_adopted_revision(
        shot_id, version_id, conn=conn,
    )
    adoption_changed = bool(
        shot and (
            shot["adopted_version_id"] != version_id
            or abs(previous_rate - playback_rate) > 0.0001
        )
    )
    if adoption_changed:
        from app.artifacts import invalidate_episode_delivery_authority

        invalidate_episode_delivery_authority(conn, shot["episode_id"])
    conn.commit()
    _review_write_audit(
        "video_version.adopt", "shot", shot_id, target_version=version_id,
        old_state={
            "adopted_version_id": shot["adopted_version_id"] if shot else None,
            "playback_rate": previous_rate,
        },
        new_state={"adopted_version_id": version_id, "playback_rate": playback_rate}, reason=reason,
        idempotency_key=body.get("idempotency_key"), request_id=body.get("request_id"),
    )
    if adoption_changed:
        worker.invalidate_episode_final(shot["episode_id"])
    return {
        "adopted": version_id,
        "artifact_id": artifact["id"],
        "reason": reason,
        "playback_rate": playback_rate,
        "video_plan_reconcile": reconcile_result,
    }

@router.post("/shots/{shot_id}/adopt")
async def adopt_version(shot_id: str, body: dict):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch(
        "video.adopt_version",
        {
            "shot_id": shot_id, "version_id": body.get("version_id"), "reason": body.get("reason"),
            "playback_rate": body.get("playback_rate", 1.0), "human_override": True,  # REST 只有人在用：人工采纳最高优先级
            "qualification_version": body.get("qualification_version"),
            "idempotency_key": body.get("idempotency_key"), "request_id": body.get("request_id"),
        },
        initiator="ui",
    )
    return respond_ui(result)

def _cancel_shot_adoption_core(shot_id: str) -> dict:
    """保留真实模型候选，只取消本镜采纳关系；后续合成不得使用图片代替。"""
    conn = get_conn()
    shot = conn.execute(
        "SELECT id,episode_id,adopted_version_id FROM shots WHERE id=?", (shot_id,),
    ).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    previous = shot["adopted_version_id"]
    if not previous:
        return {"shot_id": shot_id, "previous_adopted_version_id": None, "adopted_version_id": None}
    conn.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=?", (shot_id,))
    from app.video_plan import reconcile_adopted_revision
    reconcile_result = reconcile_adopted_revision(
        shot_id, "__unadopted__", conn=conn,
    )
    from app.artifacts import invalidate_episode_delivery_authority

    invalidate_episode_delivery_authority(conn, shot["episode_id"])
    conn.commit()
    worker.invalidate_episode_final(shot["episode_id"])
    _review_write_audit(
        "video_version.cancel_adoption",
        "shot",
        shot_id,
        target_version=previous,
        old_state={"adopted_version_id": previous},
        new_state={"adopted_version_id": None},
        reason="用户取消采纳；保留真实模型候选，成片禁止使用图片或静音片段代替",
    )
    return {
        "shot_id": shot_id,
        "previous_adopted_version_id": previous,
        "adopted_version_id": None,
        "video_plan_reconcile": reconcile_result,
    }

@router.post("/shots/{shot_id}/adoption/cancel")
async def cancel_shot_adoption(shot_id: str):
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("video.cancel_adoption", {"shot_id": shot_id})
    if routed is not None:
        return routed
    return _cancel_shot_adoption_core(shot_id)
