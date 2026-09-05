"""连播台五个步骤（映射/分镜/确认/生成/成片）的完成判据、启动与等待。

判据只挂产物信号（CLAUDE.md「Gates and Criteria」），不复制 ``episodes.status``
白名单：
- screenplay：``episodes.screenplay_status == 'ready'``
- storyboard：``storyboard_pack_prompts_complete`` 为真且分镜状态快照
  ``confirmable`` 为真
- confirm：``episodes.status in ('confirmed','generating','done')``
- video：``rebuild_coverage_ledger(episode_id).covered_within_quota()``
- final：``final/episode.mp4`` 存在且无 ``.stale`` 标记

``stage_is_complete``/``run_stage`` 是 orchestrator.py 唯一调用的分发入口——
用 if/elif 而不是 dict 分发表，是为了让测试只需要
``monkeypatch.setattr(stages, "run_stage", stub)``/``"stage_is_complete"``
两个符号就能整体打桩，不必逐一打五个私有实现（本包子模块都用 ``from . import
stages`` 这种模块限定访问，不用 ``from .stages import name``，所以这一个
symbol 就是全部调用点的唯一绑定，不需要额外的 patch_series_ops_everywhere）。
"""
from __future__ import annotations

import asyncio

from fastapi import HTTPException

from app import task_registry
from app.db import get_conn

STAGE_SEQUENCE: tuple[str, ...] = ("screenplay", "storyboard", "confirm", "video", "final")

STAGE_LABELS: dict[str, str] = {
    "screenplay": "映射台",
    "storyboard": "分镜台",
    "confirm": "确认",
    "video": "生成台",
    "final": "成片台",
}

#: 单集任务占用一集时的登记种类 → 台名。编排器在跑每一步之前都查一遍：占用着就等，
#: 不抢、不失败——重启后平台自己会把上一轮的映射台/分镜台运行恢复起来，那正是这个
#: 连播任务的产出，等它跑完再按完成判据跳过即可。
EPISODE_BUSY_KINDS: dict[str, str] = {
    "screenplay": "映射台",
    "storyboard": "分镜台",
    "video_completion": "生成台",
}


def busy_label(episode_id: str) -> str | None:
    """这一集正被哪个台的单集任务占用；空闲返回 None。"""
    for kind, label in EPISODE_BUSY_KINDS.items():
        if task_registry.active(kind, episode_id):
            return label
    return None


def _http_error_code(exc: HTTPException) -> str | None:
    detail = exc.detail
    return detail.get("code") if isinstance(detail, dict) else None


def _http_error_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("code") or detail)
    return str(detail)


# ---------------------------------------------------------------- screenplay

def screenplay_complete(conn, episode_id: str) -> bool:
    row = conn.execute(
        "SELECT screenplay_status FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    return bool(row) and row["screenplay_status"] == "ready"


async def _run_screenplay(episode_id: str) -> None:
    from app.capabilities.direct import enter_handler
    from app.domain.screenplay_ops import start_screenplay

    with enter_handler():
        await start_screenplay(episode_id, body={})
    while task_registry.active("screenplay", episode_id):
        await asyncio.sleep(5)


# ----------------------------------------------------------------- storyboard

def storyboard_complete(conn, episode_id: str) -> bool:
    from app.domain.common import storyboard_pack_prompts_complete
    from app.domain.storyboard_ops import storyboard_status

    if not storyboard_pack_prompts_complete(conn, episode_id):
        return False
    return bool(storyboard_status(episode_id).get("confirmable"))


async def _run_storyboard(episode_id: str) -> None:
    from app.capabilities.direct import enter_handler
    from app.domain.storyboard_ops import start_storyboard, storyboard_start_preflight

    preflight = await asyncio.to_thread(storyboard_start_preflight, episode_id, None)
    token = preflight.get("preview_token")
    with enter_handler():
        await start_storyboard(episode_id, body={"preflight_token": token})
    while task_registry.active("storyboard", episode_id):
        await asyncio.sleep(5)


# --------------------------------------------------------------------- confirm

def confirm_complete(conn, episode_id: str) -> bool:
    row = conn.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return bool(row) and row["status"] in {"confirmed", "generating", "done"}


async def _run_confirm(episode_id: str) -> None:
    from app.domain.video_ops import confirm_episode_core, create_storyboard_confirmation_preview

    def _do() -> None:
        # get_conn 是线程局部的：预览签发与确认落地必须在同一个工作线程里
        # 依次做，才能保证 confirm_episode_core 内部 BEGIN IMMEDIATE 的事务
        # 不会与事件循环线程上其它任务共用同一个连接对象（照
        # capabilities/handlers/storyboard.py::confirm 的同款注释）。
        preview = create_storyboard_confirmation_preview(episode_id)
        confirm_episode_core(
            episode_id,
            preview_token=preview["preview_token"],
            decided_by="series_film",
        )

    await asyncio.to_thread(_do)


# ----------------------------------------------------------------------- video

def video_complete(conn, episode_id: str) -> bool:
    from app.video_supervisor import rebuild_coverage_ledger

    _ = conn
    return rebuild_coverage_ledger(episode_id).covered_within_quota()


def _active_video_run(conn, episode_id: str) -> dict | None:
    ep = conn.execute(
        "SELECT active_video_run_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    run_id = ep["active_video_run_id"] if ep else None
    if not run_id or str(run_id).startswith("starting:"):
        return None
    run = conn.execute(
        "SELECT id, status, failure_message FROM workflow_runs WHERE id=?", (run_id,)
    ).fetchone()
    return dict(run) if run else None


def _stalled_video_reason(episode_id: str) -> str:
    conn = get_conn()
    run = _active_video_run(conn, episode_id)
    if not run:
        return ""
    reason = f"：{run['failure_message'] or run['status']}"
    # 运行级结论只说「需人工」，不说是哪一镜、供应商说了什么；把待人工镜头的原话带上，
    # 用户在连播台就能看到出路（CLAUDE.md「拦住用户时必须给出出路」）。
    blocked = conn.execute(
        """SELECT s.shot_no, j.error FROM jobs j JOIN shots s ON s.id=j.shot_id
            WHERE j.episode_id=? AND j.kind='video' AND j.status='waiting_human'
            ORDER BY s.shot_no LIMIT 3""",
        (episode_id,),
    ).fetchall()
    if blocked:
        reason += "；待人工处理：" + "；".join(
            f"第{row['shot_no']}镜 {str(row['error'] or '').strip()[:200]}" for row in blocked
        )
    return reason


# 补齐 Supervisor 停在这些 checkpoint 阶段时都不能静默发起新的 fresh 尝试：
# WAITING_AUTHORIZATION/WAITING_HUMAN/WAITING_RETRY 需要人工在生成台处理，
# PAUSED_EXTERNAL 这里特指「不是服务重启」的暂停（用户手动点了暂停，见
# app/video_supervisor/run_loop.py 的 action=="pause" 分支）——它和 workflow_runs
# 记录的字面 status='PAUSED_EXTERNAL'（唯一来源是 recorder.pause_external()，只在
# task_registry.shutdown_in_progress() 时触发）是两回事，见下面 _kick_video_completion
# 的分支顺序：先判 workflow_runs.status，命中才自动唤醒；命中不了才落到这里按
# checkpoint phase 停下来讲人话。
_VIDEO_WAIT_PHASES = {
    "WAITING_AUTHORIZATION", "WAITING_HUMAN", "WAITING_RETRY", "PAUSED_EXTERNAL",
}

# outcome 大多是 app.completion_grant.GrantValidationError.code（如
# GRANT_EXPIRED/GRANT_REVOKED），本身已是可读的英文短语，直接透出即可；只有
# STORYBOARD_REPAIR_PROPOSAL_NOT_AUTHORIZED（见 run_loop.py 第 404 行）没有配套
# 人话，专门补这一条。不新造一整套阶段文案——阶段名复用
# app.video_supervisor.constants.phase_label，不重复 _PHASE_LABELS。
#: 等待补充授权里能由连播台自己续上的原因：旧授权绑定的分镜/资格已过时或已用尽。
_REAUTHORIZABLE_OUTCOMES = {"UPSTREAM_VERSION_CHANGED", "GRANT_EXPIRED", "GRANT_CONSUMED"}


def _can_reauthorize(cp) -> bool:
    return cp.phase == "WAITING_AUTHORIZATION" and (cp.outcome or "") in _REAUTHORIZABLE_OUTCOMES


_VIDEO_WAIT_OUTCOME_DETAILS: dict[str, str] = {
    "STORYBOARD_REPAIR_PROPOSAL_NOT_AUTHORIZED": (
        "AI 提议修改分镜以补齐镜头，需要你在生成台批准或自行修分镜"
    ),
}


def _video_wait_message(detail: str, phase: str | None) -> str:
    from app.video_supervisor import phase_label

    label = phase_label(phase) if phase else "等待处理"
    return (
        f"生成台正在等待处理（{label}）：{detail}。"
        "请到生成台处理后，再回连播台点「继续」"
    )


def _checkpoint_wait_message(cp) -> str:
    outcome = cp.outcome or ""
    detail = _VIDEO_WAIT_OUTCOME_DETAILS.get(outcome) or outcome or "需要人工处理"
    return _video_wait_message(detail, cp.phase)


async def _resume_paused_video(episode_id: str, run_id: str, cp) -> str | None:
    """PAUSED_EXTERNAL 且是服务重启导致时尝试唤醒原运行；返回 None 表示已唤醒，
    返回值非 None 时是给用户看的等待文案（唤醒失败或缺少可续跑的授权）。"""
    from app.domain.video_ops import _complete_episode_core

    if not cp.grant_id:
        return _video_wait_message(
            "生成台因服务重启暂停，但缺少可续跑的补齐授权，需要人工在生成台重新发起",
            cp.phase,
        )
    try:
        await _complete_episode_core(episode_id, {
            "mode": "resume",
            "completion_grant_id": cp.grant_id,
            "idempotency_key": f"{run_id}:video-resume:{episode_id}",
        })
        return None
    except HTTPException as exc:
        return _video_wait_message(
            f"服务重启后自动恢复未成功：{_http_error_message(exc)}", cp.phase,
        )


async def _kick_video_completion(episode_id: str, run_id: str) -> None:
    from app.domain.video_ops import _complete_episode_core
    from app.video_supervisor import load_latest_checkpoint

    run = _active_video_run(get_conn(), episode_id)
    cp = load_latest_checkpoint(episode_id) if run else None
    matched_cp = cp if (cp and run and cp.run_id == run["id"]) else None

    if matched_cp and run["status"] == "PAUSED_EXTERNAL":
        wait_message = await _resume_paused_video(episode_id, run_id, matched_cp)
        if wait_message is None:
            return
        raise RuntimeError(wait_message)

    if matched_cp and matched_cp.phase in _VIDEO_WAIT_PHASES and not _can_reauthorize(matched_cp):
        raise RuntimeError(_checkpoint_wait_message(matched_cp))

    # 走到这里要么没有在等的运行，要么是「旧授权因分镜重做/过期/用尽而失效」——那是流程
    # 自己造成的状态变化，连播台自己重新发起一次 fresh 补齐（按当前发布版分镜签新授权），
    # 不把人晾到生成台点确认（2026-09-05 产品复盘：我欲封天第 10 集卡在 UPSTREAM_VERSION_CHANGED）。
    try:
        await _complete_episode_core(episode_id, {
            "mode": "fresh",
            "allow_fallback_adopt": True,
            "allow_storyboard_edit": False,
            "idempotency_key": f"{run_id}:video:{episode_id}",
        })
    except HTTPException as exc:
        if _http_error_code(exc) != "VIDEO_COMPLETION_ALREADY_ACTIVE":
            raise


async def _run_video(episode_id: str, run_id: str) -> None:
    if not task_registry.active("video_completion", episode_id):
        await _kick_video_completion(episode_id, run_id)
    while task_registry.active("video_completion", episode_id):
        await asyncio.sleep(8)
    if not video_complete(get_conn(), episode_id):
        raise RuntimeError(f"生成台未能补齐全部镜头{_stalled_video_reason(episode_id)}")


# ----------------------------------------------------------------------- final

def final_complete(conn, episode_id: str) -> bool:
    from app.media_exec.concat import _final_video_path

    _ = conn
    row = get_conn().execute(
        "SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if not row:
        return False
    path = _final_video_path(row["project_id"], row["episode_no"])
    return path.is_file() and not path.with_suffix(".stale").is_file()


async def _run_final(episode_id: str) -> None:
    from app import worker

    await asyncio.to_thread(worker.concatenate_episode, episode_id)


# -------------------------------------------------------------------- dispatch

def stage_is_complete(stage: str, conn, episode_id: str) -> bool:
    if stage == "screenplay":
        return screenplay_complete(conn, episode_id)
    if stage == "storyboard":
        return storyboard_complete(conn, episode_id)
    if stage == "confirm":
        return confirm_complete(conn, episode_id)
    if stage == "video":
        return video_complete(conn, episode_id)
    if stage == "final":
        return final_complete(conn, episode_id)
    raise ValueError(f"未知步骤：{stage}")


async def run_stage(stage: str, episode_id: str, run_id: str) -> None:
    if stage == "screenplay":
        await _run_screenplay(episode_id)
    elif stage == "storyboard":
        await _run_storyboard(episode_id)
    elif stage == "confirm":
        await _run_confirm(episode_id)
    elif stage == "video":
        await _run_video(episode_id, run_id)
    elif stage == "final":
        await _run_final(episode_id)
    else:
        raise ValueError(f"未知步骤：{stage}")
