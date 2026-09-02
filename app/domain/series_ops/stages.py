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


def _http_error_code(exc: HTTPException) -> str | None:
    detail = exc.detail
    return detail.get("code") if isinstance(detail, dict) else None


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


def _stalled_video_reason(episode_id: str) -> str:
    conn = get_conn()
    ep = conn.execute(
        "SELECT active_video_run_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    run_id = ep["active_video_run_id"] if ep else None
    if not run_id or str(run_id).startswith("starting:"):
        return ""
    run = conn.execute(
        "SELECT status, failure_message FROM workflow_runs WHERE id=?", (run_id,)
    ).fetchone()
    if not run:
        return ""
    return f"：{run['failure_message'] or run['status']}"


async def _run_video(episode_id: str, run_id: str) -> None:
    from app.domain.video_ops import _complete_episode_core

    if not task_registry.active("video_completion", episode_id):
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
