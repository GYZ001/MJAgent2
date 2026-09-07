"""墙钟收口判据：仍在推进就有界续期，真卡住才收口（2026-09-06 第 14 轮 6 集结构性失败）。

第 14 轮 27 集视频台几乎同时开工，每集墙钟 240 分钟固定从本集开工算起。B 库实测单镜
「派发 → 视频落盘」p50 89 分钟、p90 184 分钟、最长 248 分钟，而最快的一镜只要 4.2 分钟
——差值全是排队：500 多个镜头共用同一条供应商队列（并发不设上限，由机器水位与供应商信号
自适应），一集的墙钟于是被别的集的排队吃光。结果 6 集正好卡在 240 分钟被收口，而它们缺的
那几镜在收口后 5-9 分钟就落盘了，被当作「历史任务」隔离丢弃：付了钱、片子合格、集子判失败。

判据因此不能挂在「开工至今过了多久」（会被别的集的正常活动改动），要挂在「这一集自己还能不能
往前走」：本集仍有在途作业、且整条流水线最近确实有作业到达终态，就说明只是在排队，按
``DEADLINE_EXTENSION_S`` 续一段再看；整条流水线 ``DEADLINE_STALL_GRACE_S`` 内一个作业都没
到终态，才是真卡住，照旧收口。续期次数有上限，卡死的集仍然会终止。
"""
from __future__ import annotations

from typing import Any

from app.db import get_conn, now

from .constants import (
    DEADLINE_EXTENSION_S,
    DEADLINE_STALL_GRACE_S,
    MAX_DEADLINE_EXTENSIONS,
)
from .models import VideoSupervisorCheckpoint

TERMINAL_VIDEO_JOB_STATUSES = ("succeeded", "failed", "cancelled")


def pipeline_last_terminal_at(conn: Any = None) -> float | None:
    """整条流水线最近一次视频作业到达终态的时刻；一条都没有返回 None。"""
    row = (conn or get_conn()).execute(
        "SELECT MAX(updated_at) AS ts FROM jobs WHERE kind='video' AND status IN "
        f"({','.join('?' * len(TERMINAL_VIDEO_JOB_STATUSES))})",
        TERMINAL_VIDEO_JOB_STATUSES,
    ).fetchone()
    return float(row["ts"]) if row and row["ts"] is not None else None


def episode_has_pending_work(cp: VideoSupervisorCheckpoint) -> bool:
    """本集还有镜头没拿到采用版本——没有的话本来就该走正常收敛，不需要续期。"""
    return any(
        not (state or {}).get("adopted_version_id")
        for state in (cp.shot_state or {}).values()
    )


def effective_deadline(cp: VideoSupervisorCheckpoint) -> float | None:
    """本集当前生效的截止时刻：授权墙钟与已批准的续期取较晚者。"""
    stamps = [x for x in (cp.deadline_at, cp.deadline_extended_until) if x]
    return max(stamps) if stamps else None


def resolve_deadline(cp: VideoSupervisorCheckpoint, *, conn: Any = None) -> str:
    """返回 ``"within"``（未到期）/ ``"extended"``（到期但在排队，已续期）/ ``"closeout"``。

    ``extended`` 分支就地把续期写进 ``cp``，调用方照常继续跑；每次续期都记一次
    ``deadline_extensions``，到 ``MAX_DEADLINE_EXTENSIONS`` 为止。
    """
    deadline = effective_deadline(cp)
    if not deadline or now() < deadline:
        return "within"
    if cp.deadline_extensions >= MAX_DEADLINE_EXTENSIONS or not episode_has_pending_work(cp):
        return "closeout"
    last_terminal = pipeline_last_terminal_at(conn)
    if last_terminal is None or now() - last_terminal > DEADLINE_STALL_GRACE_S:
        return "closeout"
    cp.deadline_extensions += 1
    cp.deadline_extended_until = now() + DEADLINE_EXTENSION_S
    return "extended"
