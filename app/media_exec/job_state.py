"""job/version 状态写入与供应商轮询节奏策略（拆分自 ``run_job.py``）。

``_set_job``/``_set_version`` 是全仓统一的两个状态转移出口（CLAUDE.md「配套参
数必须一起传递」的落地点之一：调用方不得绕过这两个函数直接拼 SQL 改
``jobs``/``shot_versions``）。``_provider_wait_policy`` 决定下一次轮询的退避节
奏，``_provider_submitted_at``/``_prior_task_poll_failure_messages`` 服务于轮询
重试的可观测性。``_recover_paid_video_task``/``_paid_video_attempt_count`` 处理
已付费视频任务的恢复计数。``_video_model_rejection_guidance`` 把供应商拒绝原
因翻译成面向用户的修复建议。``_video_image_inputs_from_meta`` 从 job 元数据解
析视频输入图，取不到时抛 ``VideoInputRepairRequired``（``.fences``）而不是静默
兜底（CLAUDE.md「不得兜底填充」）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from app import config, hiagent, video_modes
from app.db import get_conn, now
from app.harness.hiagent_input_image_privacy import (
    INPUT_IMAGE_PRIVACY_CODE, INPUT_IMAGE_PRIVACY_REJECTED_KIND,
)
from app.hiagent import ProviderError
from app.orchestration.media_runs import mark_media_job_state
from app.visual_styles import VISUAL_STYLE_PRESETS

from .common import LeaseLost
from .enqueue import _row_value
from .fences import VideoInputRepairRequired


def _set_job(
    job_id: str,
    status: str,
    error: str | None = None,
    *,
    lease_owner: str | None = None,
) -> bool:
    conn = get_conn()
    # waiting_human 是死路：既不是「进行中」也不是「已交付」，必须释放
    # video_slot_active，否则 _begin_video_preflight_job 的镜头级独占锁永远
    # 不会被清空，重新生成会在指纹比对之前就被短路成假的"reused"（见
    # CLAUDE.md「Gates and Criteria」）。
    terminal = status in {
        "succeeded", "failed", "cancelled", "abandoned", "waiting_human",
    }
    if terminal:
        cursor = conn.execute(
            "UPDATE jobs SET status=?, error=?, updated_at=?, video_slot_active=0, "
            "lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=?" + (" AND lease_owner=?" if lease_owner else ""),
            (status, error, now(), job_id, *([lease_owner] if lease_owner else [])),
        )
    else:
        cursor = conn.execute(
            "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?"
            + (" AND lease_owner=?" if lease_owner else ""),
            (status, error, now(), job_id, *([lease_owner] if lease_owner else [])),
        )
    if cursor.rowcount != 1:
        conn.rollback()
        return False
    if terminal:
        conn.execute(
            """UPDATE shot_versions
                  SET video_slot_active=0
                WHERE id=(SELECT version_id FROM jobs WHERE id=?)""",
            (job_id,),
        )
    conn.commit()
    row = conn.execute("SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row:
        mark_media_job_state(row["run_id"], row["step_run_id"], status, error)
    return True


def _set_version(version_id: str, **fields) -> bool:
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in fields)
    cancellation_guard = (
        """ AND NOT EXISTS (
                SELECT 1 FROM jobs j
                 WHERE j.version_id=shot_versions.id
                   AND j.cancellation_requested=1
            )"""
        if "status" in fields
        else ""
    )
    cursor = conn.execute(
        f"UPDATE shot_versions SET {cols} WHERE id=?{cancellation_guard}",
        (*fields.values(), version_id),
    )
    conn.commit()
    return cursor.rowcount == 1


def _video_model_rejection_guidance(
    meta: dict[str, Any],
    exc: ProviderError,
) -> tuple[str, str] | None:
    """把结构化供应商结论转成落地文案，绝不从错误正文猜测分类。

    以下两类失败在结构化层面已经确定"本镜不会再自动付费重试"（模型/提示词
    服务明确拒绝；或供应商执行失败且分类结论要求转人工），若不在这里接管，
    会落到 app/errors.py 的 provider 分类兜底提示——那句"可稍后重试"对这类
    镜头是假话（CLAUDE.md「User-Facing Behavior」：界面承诺必须与实际行为
    一致）。因此这里必须：
    (a) 逐字转述供应商原文（不改写、不替它重新分类）；
    (b) 明确告知系统已停止对本镜的自动付费重试；
    (c) 给出具体出路，不能把用户晾在原地。
    """
    provider_text = (exc.raw or "").strip()
    if exc.failure.category is hiagent.ProviderFailureCategory.MODEL_REJECTION:
        if exc.failure.kind == hiagent.ProviderFailureKind.PROMPT_PROVIDER_REJECTED.value:
            return (
                "VIDEO_PROMPT_PROVIDER_REJECTED",
                "AI 视频提示词服务明确拒绝了当前内容；系统未改写内容、未切换生成方式，"
                "也未向视频服务提交本镜。请更换获准的提示词模型或人工调整内容后再继续。",
            )
        if exc.failure.kind == INPUT_IMAGE_PRIVACY_REJECTED_KIND:
            return (
                "VIDEO_INPUT_IMAGE_PRIVACY_REJECTED",
                "视频供应商判定本镜输入图疑似真人肖像，按隐私政策拒收"
                f"（供应商错误码 {INPUT_IMAGE_PRIVACY_CODE}）。真人摄影风/精修真人风越接近"
                "真实人像越容易触发这类判定，同一画风原样重试大概率复现同样的拒绝，"
                "系统已停止对本镜的自动付费重试。请到项目设置改用非真人画风（"
                + "、".join(preset.name for preset in VISUAL_STYLE_PRESETS if not preset.photographic)
                + "）后重新生成定妆照与本镜；若需继续保留当前摄影类画风，"
                "可仅保留图片产出、不生成视频。",
            )
        mode = str(meta.get("mode") or meta.get("planned_mode") or "")
        quote = f"供应商原文：{provider_text}。" if provider_text else ""
        if exc.failure.kind == PROVIDER_CONTENT_REJECTED_KIND:
            return (
                "VIDEO_PROVIDER_CONTENT_REJECTED",
                f"视频供应商对本镜连续 {CONTENT_REJECTION_MIN_TASKS} 次独立任务给出了完全相同的"
                f"拒绝结果，判定为真实的模型拒绝。{quote}"
                "系统已停止对本镜的自动重试，并把本镜按「跳过」处理：本集成片照常合成、"
                "不含本镜。需要补上时请编辑本镜提示词后手工重抽。",
            )
        return (
            "VIDEO_PROVIDER_MODEL_REJECTED",
            f"当前视频模型明确拒绝了本次输入，系统已保持 {mode or '原计划模式'} "
            f"失败且没有改写内容或切换生成方式。{quote}"
            "系统已停止对本镜的自动付费重试，转人工处理。"
            "请编辑本镜提示词后重抽，或切换视频供应商。",
        )
    if (
        exc.failure.category is hiagent.ProviderFailureCategory.TECHNICAL
        and exc.failure.kind == hiagent.ProviderFailureKind.EXECUTION_FAILED.value
        and provider_text
    ):
        return (
            exc.failure.reason_code,
            f"视频供应商执行失败，供应商原文：{provider_text}。"
            "系统已停止对本镜的自动付费重试，转人工处理。"
            "请在页面核对供应商任务状态，或编辑本镜提示词后重抽、或切换视频供应商。",
        )
    return None


# 预检撞 uq_versions_active_video_shot（旧版本仍占槽）时的等待预算：槽位随旧版本终态
# 释放，通常几秒到几分钟（服务重启后"历史任务完成→隔离"的提交可能晚几分钟），
# 默认 2 次×15 秒等不到（2026-09-05 我欲封天 4 镜因此转人工）。
ACTIVE_SLOT_WAIT_MAX_RETRIES = 12
ACTIVE_SLOT_WAIT_STEP_S = 30.0
ACTIVE_SLOT_WAIT_CAP_S = 60.0


def is_active_slot_collision(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.IntegrityError) and "shot_versions" in str(exc)


def preflight_retry_budget(
    exc: BaseException, job_max_retries: Any, retry_count: int,
) -> tuple[int, float]:
    """(本次故障的重试上限, 下一次的延迟秒数)。槽位冲突走独立的等待预算，不占
    结构化故障那 2 次的名额；其它故障维持 VIDEO_PREFLIGHT_* 的指数退避。"""
    if is_active_slot_collision(exc):
        return ACTIVE_SLOT_WAIT_MAX_RETRIES, min(
            ACTIVE_SLOT_WAIT_STEP_S * (retry_count + 1), ACTIVE_SLOT_WAIT_CAP_S,
        )
    configured = int(config.VIDEO_PREFLIGHT_MAX_RETRIES)
    limit = min(int(job_max_retries or configured), configured)
    return limit, float(config.VIDEO_PREFLIGHT_RETRY_BASE_DELAY) * (2 ** retry_count)


PROVIDER_CONTENT_REJECTED_KIND = "provider_content_rejected"
# 同一镜头、不同供应商任务、字节级相同的终态失败达到这个数才判定为真实拒绝。
# 与 config.VIDEO_JOB_MAX_RETRIES=3 配合：第 1、2 次换新任务重试，第 3 次相同即停。
CONTENT_REJECTION_MIN_TASKS = 3


def _prior_shot_terminal_failure_records(conn, shot_id: str) -> list[tuple[str, str]]:
    """按时间顺序返回同一镜头此前全部 TASK_FAILED 轮询的 (task_id, error 原文)。
    ``poll_video_task`` 落库的 meta 带 ``shot_id``/``task_id``（见 app/seedance.py
    call_meta），本次失败那条在这里被调用前已经提交，所以结果天然含"本次"。
    """
    rows = conn.execute(
        """SELECT error, meta FROM provider_calls
           WHERE kind='video_poll' AND status='TASK_FAILED' AND meta LIKE ?
           ORDER BY ts""",
        (f'%"shot_id": "{shot_id}"%',),
    ).fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        out.append((str(meta.get("task_id") or ""), str(row["error"] or "")))
    return out


_REQUEST_ID_RE = re.compile(r"(request[ _-]?id|requestid|trace[ _-]?id|logid)\s*[:=：]?\s*[0-9a-zA-Z\-]+", re.I)


def normalize_vendor_failure_text(message: str) -> str:
    """去掉供应商失败原文里每次请求都不同的追踪标识（``Request id: 0217…``），只留
    对内容的判语。这是结构归一化，不是按关键词猜分类：不去掉它，跨任务的同一句
    拒绝永远比不相等（实测火山每条失败都带唯一 Request id）。"""
    return " ".join(_REQUEST_ID_RE.sub("", message or "").split()).strip(" .。;；,，")


def rejection_repeated_across_tasks(
    records: list[tuple[str, str]], *, min_tasks: int = CONTENT_REJECTION_MIN_TASKS,
) -> bool:
    """纯结构判据：最近一次终态失败的原文（归一化后），在同一镜头至少 ``min_tasks``
    个**不同**供应商任务上逐字出现，判定为供应商对这段内容的确定性拒绝。
    只看"独立任务 × 相同结果"这个结构，不看 message 里写了什么词——同一任务被
    反复轮询出的相同文本只算一个任务（``task_id`` 去重），说明不了任何事。
    """
    if not records:
        return False
    latest_task, latest_msg = records[-1]
    latest = normalize_vendor_failure_text(latest_msg)
    if not latest or not latest_task:
        return False
    tasks = {task for task, msg in records if task and normalize_vendor_failure_text(msg) == latest}
    return len(tasks) >= max(2, min_tasks)


def settle_terminal_poll_failure(
    conn, job_id: str, owner: str, *, shot_id: str, failure: Any,
) -> Any:
    """供应商已报告任务终态失败：这个任务不会再变，绝不能再轮询它。
    - 跨独立任务重复出现相同失败 → 真实的模型拒绝（外部终态，本镜跳过，成片不含本镜）；
    - 否则保持技术故障、可换**新任务**重试：把 ``provider_poll_required`` 清零，
      错误处理器才会走 ``_schedule_job_retry`` 而不是 ``_defer_provider_poll``
      去复轮一个死任务（2026-09-05 我欲封天第 21 集第 3 镜：一个任务 2 小时被轮询
      109 次、刷出 82 条报错，而换新任务的第 2 次一次就过）。
    """
    if failure.category is hiagent.ProviderFailureCategory.TECHNICAL:
        history = _prior_shot_terminal_failure_records(conn, shot_id)
        if rejection_repeated_across_tasks(history):
            failure = hiagent.ProviderFailure.model_rejection(PROVIDER_CONTENT_REJECTED_KIND)
        elif failure.retryable:
            conn.execute(
                """UPDATE jobs SET provider_poll_required=0, provider_result_adoptable=0,
                          updated_at=?
                    WHERE id=? AND lease_owner=?""",
                (now(), job_id, owner),
            )
            conn.commit()
    return failure


def _prior_task_poll_failure_messages(conn, task_id: str) -> list[str]:
    """按时间顺序返回同一供应商任务此前全部 TASK_FAILED 轮询的 error 原文。

    ``log_provider_call`` 在 ``poll_video_task`` 内部同步提交（见
    app/db.py ``_log_provider_call_inner``），本次失败对应的那条
    ``video_poll``/``TASK_FAILED`` 记录在这里被调用前已经落库，因此结果里
    天然包含"本次"，调用方不需要再单独拼接当前消息。

    只用于给 ``hiagent.has_repeated_terminal_poll_failure`` 提供历史序列；
    本函数是这里唯一做数据库 I/O 的部分，纯判断逻辑留在 hiagent 那边。
    """
    rows = conn.execute(
        """SELECT error FROM provider_calls
           WHERE kind='video_poll' AND status='TASK_FAILED' AND meta LIKE ?
           ORDER BY ts""",
        (f"%{task_id}%",),
    ).fetchall()
    return [str(row["error"] or "") for row in rows]


def _provider_submitted_at(
    conn,
    job,
    task_id: str,
    *,
    lease_owner: str | None = None,
) -> float:
    """返回 provider 首次接受当前视频 task 的时间，并为旧任务补齐持久字段。

    轮询预算必须基于这个绝对时间，不能在 worker 重启后重新开始计时。
    """
    persisted = _row_value(job, "provider_submitted_at")
    if persisted:
        return float(persisted)
    operation_id = _row_value(job, "provider_operation_id")
    provider_call = conn.execute(
        """SELECT MIN(ts) AS submitted_at FROM provider_calls
           WHERE kind='video_create' AND status='OK'
             AND (operation_id=? OR meta LIKE ?)""",
        (operation_id, f"%{task_id}%"),
    ).fetchone()
    submitted_at = (
        float(provider_call["submitted_at"])
        if provider_call and provider_call["submitted_at"] is not None
        else float(_row_value(job, "attempt_started_at") or time.time())
    )
    updated = conn.execute(
        "UPDATE jobs SET provider_submitted_at=? WHERE id=?"
        + (
            " AND status='running' AND lease_owner=? AND cancellation_requested=0"
            if lease_owner is not None
            else ""
        ),
        (submitted_at, job["id"], *([lease_owner] if lease_owner is not None else [])),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise LeaseLost(
            f"provider submission timestamp lost lease: {job['id']} / {lease_owner}"
        )
    conn.commit()
    return submitted_at


def _provider_wait_policy(
    task_id: str,
    result: dict[str, Any],
    meta: dict[str, Any],
    *,
    duration_s: float,
    provider_submitted_at: float,
    stamp: float | None = None,
) -> dict[str, Any]:
    """Choose queue-aware polling and timeout behavior for the active provider."""
    current = time.time() if stamp is None else float(stamp)
    provider_age = max(0.0, current - float(provider_submitted_at))
    policy = {
        "elapsed_s": provider_age,
        "timeout_s": float(config.VIDEO_PROVIDER_MAX_WAIT),
        "poll_delay_s": None,
        "scope": "供应商任务",
        "meta_changed": False,
        "stage_progress": None,
    }
    from app import video_providers

    adapter = video_providers.adapter_for_task_id(task_id)
    if adapter is None:
        return policy
    return adapter.apply_wait_policy(
        task_id,
        result,
        meta,
        policy,
        duration_s=duration_s,
        current=current,
    )


def _recover_paid_video_task(conn, operation_id: str | None) -> tuple[str, float] | None:
    """Recover a provider handle accepted before the local job commit."""
    if not operation_id:
        return None
    rows = conn.execute(
        """SELECT ts, response_json FROM provider_calls
           WHERE kind='video_create' AND status='OK' AND operation_id=?
             AND response_json IS NOT NULL
           ORDER BY id DESC""",
        (operation_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["response_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        task_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
        if task_id:
            return task_id, float(row["ts"])
    return None


def _paid_video_attempt_count(conn, version_id: str) -> int:
    prefix = f"video-create-{version_id}"
    row = conn.execute(
        """SELECT COUNT(DISTINCT operation_id) AS count
           FROM provider_calls
           WHERE kind='video_create' AND status='OK'
             AND response_json IS NOT NULL
             AND (operation_id=? OR operation_id LIKE ?)""",
        (prefix, f"{prefix}-%"),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _video_image_inputs_from_meta(meta: dict) -> list[tuple[str, str]]:
    try:
        return video_modes.build_seedance_image_inputs(meta)
    except ProviderError as exc:
        if meta.get("mode") in {
            video_modes.REFERENCE_IMAGE_MODE,
            video_modes.FIRST_FRAME_MODE,
            video_modes.FIRST_LAST_FRAME_MODE,
        }:
            raise VideoInputRepairRequired(str(exc)) from exc
        raise

__all__ = [name for name in globals() if not name.startswith("__")]
