"""分镜生成任务体的录制器、恢复后录制任务、sqlite 锁重试包装与「录制+守卫」包装。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 task_body。
"""
from __future__ import annotations

import asyncio
import sqlite3

from app import (
    config,
    task_registry,
)
from app.db import (
    get_conn,
    rows_to_dicts,
)
from app.evidence import repository as evidence_repository
from app.harness.context import ContextPack
from app.harness.contracts import get_contract
from app.orchestration.engine import (
    WorkflowRecorder,
    fingerprint,
)

from .task_body import _storyboard_task


def _new_storyboard_recorder(
    episode_id: str,
    *,
    resume: bool = False,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> WorkflowRecorder:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    checkpoints = rows_to_dicts(conn.execute(
        "SELECT shot_no, storyboard_artifact_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall())
    project = conn.execute(
        "SELECT bible_artifact_id FROM projects WHERE id=?",
        (ep["project_id"],),
    ).fetchone()
    bible_artifact_id = _storyboard_bound_bible_artifact_id(
        episode_id,
        ep,
        project["bible_artifact_id"] if project else None,
        resume=resume,
    )
    contract = get_contract("storyboard")
    return WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            ep["screenplay_artifact_id"],
            bible_artifact_id,
            ep["storyboard_outline_json"],
            checkpoints,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={
            "supervisor": True,
            "checkpoint": "supervisor_and_per_shot",
            "max_iterations_per_shot": contract.max_iterations,
            "max_inner_iterations": 4,
            "blocker_warning_candidate_allowed": False,
            "provider_retry": {
                "max_retries_per_call": config.TEXT_PROVIDER_MAX_RETRIES,
                "base_delay_s": config.TEXT_PROVIDER_RETRY_BASE_DELAY,
                "strategy": "bounded_exponential_backoff_same_request",
            },
        },
        config_snapshot={"storyboard_shot_max_tokens": config.STORYBOARD_SHOT_MAX_TOKENS},
        parent_run_id=parent_run_id,
    )

def _storyboard_bound_bible_artifact_id(
    episode_id: str,
    episode_row,
    current_artifact_id: str | None,
    *,
    resume: bool,
) -> str | None:
    """Use the checkpoint Bible on resume when its screenplay still matches."""
    if not resume:
        return current_artifact_id
    from app.storyboard_supervisor import load_latest_checkpoint

    cp = load_latest_checkpoint(episode_id)
    if cp is None:
        return current_artifact_id
    bound_screenplay = str(
        cp.input_versions.get("screenplay_artifact_id") or ""
    )
    current_screenplay = str(episode_row["screenplay_artifact_id"] or "")
    if bound_screenplay and bound_screenplay != current_screenplay:
        return current_artifact_id
    return cp.input_versions.get("bible_artifact_id") or current_artifact_id

async def _recorded_storyboard_task(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    resume: bool,
    new_activation: bool = False,
) -> None:
    recorder.start()
    try:
        conn = get_conn()
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        # 旧的最小化测试 schema 可能没有 board_text_provider 列（真实数据库总有，见
        # app/db.py 迁移）；SELECT * 拿满全部列，缺列时下面按 None 处理，不炸查询。
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
        ).fetchone()
        bible_artifact_id = _storyboard_bound_bible_artifact_id(
            episode_id,
            ep,
            project["bible_artifact_id"] if project else None,
            resume=resume,
        )
        input_ids = [
            artifact_id for artifact_id in (
                bible_artifact_id,
                ep["screenplay_artifact_id"],
            ) if artifact_id
        ]
        context = ContextPack(goal="集级 Supervisor：生成整集分镜直至通过并等待人工确认")
        if ep["screenplay_json"]:
            context.add_text(
                "screenplay", ep["screenplay_json"],
                source_artifact_id=ep["screenplay_artifact_id"], limit=24000,
            )
        from app import model_registry
        from app.harness.text_provider_scope import stage_text_provider

        resolved_text_provider = model_registry.resolve_stage_text_provider(
            dict(project).get("board_text_provider") if project else None
        )
        with stage_text_provider(resolved_text_provider):
            _step_id, supervisor_result = await recorder.step(
                "storyboard",
                lambda: _storyboard_task_with_sqlite_lock_retry(
                    episode_id,
                    resume=resume,
                    run_id=getattr(recorder, "run_id", None),
                    new_activation=new_activation,
                ),
                contract_key="storyboard",
                agent_name="storyboard_supervisor",
                input_artifact_ids=input_ids,
                context_manifest=context.manifest(),
            )
        result = conn.execute(
            "SELECT status, script_error, storyboard_artifact_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        phase = str(getattr(supervisor_result, "phase", "") or "")
        outcome = str(getattr(supervisor_result, "outcome", "") or "")
        if result and result["status"] == "confirmed":
            recorder.succeed("分镜已确认（尚未产生视频费用）", conn=None)
        elif phase == "SUCCEEDED" and outcome == "SUCCEEDED_READY_FOR_CONFIRM":
            recorder.succeed("分镜已完成，等待人工确认", conn=None)
        elif phase == "PAUSED_EXTERNAL":
            from app.orchestration.state_machine import transition_run
            transition_run(
                recorder.run_id, "RUNNING", "PAUSED_EXTERNAL",
                (result["script_error"] if result else None) or outcome or "Supervisor 已暂停",
                failure_code=(
                    "PROVIDER_UNAVAILABLE"
                    if outcome == "PAUSED_PROVIDER_UNAVAILABLE"
                    else "USER_PAUSE"
                ), conn=None,
            )
        elif phase == "WAITING_AUTHORIZATION":
            from app.orchestration.state_machine import transition_run
            transition_run(
                recorder.run_id, "RUNNING", "WAITING_AUTHORIZATION",
                (result["script_error"] if result else None) or outcome,
                failure_code="WAITING_AUTHORIZATION", conn=None,
            )
        elif phase == "WAITING_HUMAN":
            from app.orchestration.state_machine import transition_run
            wait_state = (
                "WAITING_RETRY"
                if outcome in {
                    "WAITING_RETRY_ACTIVATION_BUDGET",
                    "WAITING_RETRY_CAS_CONFLICT",
                    "WAITING_RETRY_GATE_REPAIR_EXHAUSTED",
                    "WAITING_RETRY_STORYBOARD_INCOMPLETE",
                }
                else "WAITING_HUMAN"
            )
            transition_run(
                recorder.run_id, "RUNNING", wait_state,
                (result["script_error"] if result else None) or outcome or "Supervisor 等待处理",
                failure_code=outcome or wait_state, conn=None,
            )
        elif (
            supervisor_result is None
            and result
            and result["status"] == "scripted"
            and result["storyboard_artifact_id"]
            and not result["script_error"]
        ):
            recorder.succeed("分镜已完成，等待人工确认", conn=None)
        else:
            message = (
                str(result["script_error"] or "分镜 Supervisor 未进入可恢复终态")
                if result else "分镜生成失败"
            )
            # Run 终态与页面投影必须在同一收尾路径收敛。否则 finally 只清活动
            # 指针而遗留 status=scripting，分镜台会在任务中心已经 FAILED 后仍显示运行。
            conn.execute(
                "UPDATE episodes SET status='script_failed',script_error=? "
                "WHERE id=? AND active_storyboard_run_id=?",
                (message[:800], episode_id, recorder.run_id),
            )
            conn.commit()
            recorder.fail(RuntimeError(message), conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，分镜运行等待自动续做", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:
        recorder.fail(exc, conn=None)
        raise
    finally:
        # The workflow run remains available for audit/resume lineage, but it must
        # stop acting as a write lock once this coroutine has ended. The guarded
        # comparison avoids clearing a newer run that may have started meanwhile.
        try:
            cleanup_conn = get_conn()
            cleanup_conn.execute(
                "UPDATE episodes SET active_storyboard_run_id=NULL "
                "WHERE id=? AND active_storyboard_run_id=?",
                (episode_id, recorder.run_id),
            )
            cleanup_conn.commit()
        except Exception:  # noqa: BLE001
            pass

_STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S = (0.25, 1.0, 2.0)

def _is_transient_sqlite_lock(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    error_code = getattr(exc, "sqlite_errorcode", None)
    if error_code is None:
        return False
    return (int(error_code) & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }

async def _storyboard_task_with_sqlite_lock_retry(
    episode_id: str,
    *,
    resume: bool,
    run_id: str | None,
    new_activation: bool,
):
    """Resume from the durable checkpoint after a transient SQLite writer lock."""
    for attempt in range(len(_STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S) + 1):
        try:
            return await _storyboard_task(
                episode_id,
                resume=bool(resume or attempt),
                run_id=run_id,
                new_activation=bool(new_activation and attempt == 0),
            )
        except sqlite3.OperationalError as exc:
            if (
                not _is_transient_sqlite_lock(exc)
                or attempt >= len(_STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S)
            ):
                raise
            get_conn().rollback()
            delay_s = _STORYBOARD_SQLITE_LOCK_RETRY_DELAYS_S[attempt]
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "STORYBOARD_SQLITE_LOCK_RETRY",
                    "warning",
                    "SQLite 写锁冲突，已回滚未完成事务并从安全检查点重试",
                    payload={
                        "attempt": attempt + 1,
                        "delay_s": delay_s,
                        "episode_id": episode_id,
                    },
                )
            await asyncio.sleep(delay_s)

    raise RuntimeError("unreachable")

def _storyboard_generation_is_live(ep: dict) -> bool:
    """判断活动指针是否真的对应本进程/存储中的活跃分镜任务。

    ``episodes.status='scripting'`` 是 UI 投影，不是可靠的任务存活证明。旧 Run 已经
    PARTIAL/CANCELLED/FAILED 时若仍按该字段去重，继续按钮只会返回旧 run_id，页面进入
    “正在生成”但后台没有任务。进程内注册表优先；跨重启仅 CREATED/RUNNING Run 算活跃。
    """
    if task_registry.active("storyboard", ep["id"]):
        return True
    try:
        run_id = ep["active_storyboard_run_id"]
    except (KeyError, IndexError, TypeError):
        run_id = None
    if not run_id:
        return False
    if str(run_id).startswith("starting:"):
        return True
    from app.evidence import repository
    run = repository.get_run(run_id)
    return bool(run and run.get("status") in {"CREATED", "RUNNING"})

async def _storyboard_guarded_recorded(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    resume: bool,
    new_activation: bool,
    priority: int,
) -> None:
    from app.generation_concurrency import run_with_generation_slot
    from app.orchestration.state_machine import StateConflict
    from app import quota

    conn = get_conn()
    owner_user_id = quota.owner_of_episode(conn, episode_id)
    if owner_user_id is not None:
        try:
            active = quota.count_active_workflow_runs(
                conn, owner_user_id, "storyboard", exclude_run_id=recorder.run_id,
            )
            quota.check_module_concurrency(
                conn, owner_user_id, quota.MODULE_STORYBOARD, active_count=active,
            )
            quota.assert_token_capacity(conn, owner_user_id)
        except quota.QuotaExceeded:
            try:
                recorder.cancel("账号配额已达上限，任务未启动", conn=None)
            except StateConflict:
                pass
            raise

    await run_with_generation_slot(
        "storyboard",
        lambda: _recorded_storyboard_task(
            episode_id,
            recorder,
            resume=resume,
            new_activation=new_activation,
        ),
        priority=priority,
    )
