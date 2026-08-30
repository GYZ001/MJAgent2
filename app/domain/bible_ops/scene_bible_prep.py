"""场景引用图/场景圣经准备的发起与后台任务体（供 recover/task_run/scene_refs 复用）。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import asyncio
import json

from app import (
    errors,
    quota,
    task_registry,
)
from app.db import (
    get_conn,
    get_setting,
    now,
    rows_to_dicts,
)
from app.domain.common import (
    _scene_assets_task_active,
    _scene_refs_task_active,
)
from app.orchestration.engine import (
    WorkflowRecorder,
    fingerprint,
)
from app.schemas import Bible

from .primitives import (
    QUOTA_MODULE_SCENE_REF,
    _parse_json_value,
    count_active_project_scoped_workflow_runs,
)


def _start_scene_refs_generation(
    project_id: str,
    only_scene: str | list[str] | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
) -> bool:
    """启动场景图素材库生成任务。已有同项目任务在跑则返回 False。"""
    if _scene_refs_task_active(project_id):
        return False
    conn = get_conn()
    project_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    batch_column_supported = "scene_refs_batch_started_at" in project_columns
    previous = conn.execute(
        "SELECT scene_refs_status,scene_refs_error,scene_refs_target,"
        + ("scene_refs_batch_started_at" if batch_column_supported else "NULL AS scene_refs_batch_started_at")
        + " FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    batch_started_at = (
        previous["scene_refs_batch_started_at"]
        if resume and previous and previous["scene_refs_batch_started_at"] is not None
        else now()
    )
    target_payload = (
        json.dumps(only_scene, ensure_ascii=False)
        if isinstance(only_scene, list) else only_scene
    )
    if batch_column_supported:
        conn.execute(
            "UPDATE projects SET scene_refs_status='running',scene_refs_error=NULL,"
            "scene_refs_target=?,scene_refs_batch_started_at=? WHERE id=?",
            (target_payload, batch_started_at, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET scene_refs_status='running',scene_refs_error=NULL,"
            "scene_refs_target=? WHERE id=?",
            (target_payload, project_id),
        )
    conn.commit()
    try:
        task_registry.spawn(
            "scene_refs", project_id,
            _scene_refs_task(
                project_id, only_scene, resume=resume, parent_run_id=parent_run_id,
                operation_started_at=batch_started_at,
                requested_by="system" if resume else "user",
                trigger_type="resume" if resume else "manual",
            ),
            project_id=project_id,
        )
    except Exception as exc:
        if previous:
            if batch_column_supported:
                conn.execute(
                    "UPDATE projects SET scene_refs_status=?,scene_refs_error=?,scene_refs_target=?,"
                    "scene_refs_batch_started_at=? WHERE id=?",
                    (
                        previous["scene_refs_status"], previous["scene_refs_error"],
                        previous["scene_refs_target"], previous["scene_refs_batch_started_at"],
                        project_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE projects SET scene_refs_status=?,scene_refs_error=?,scene_refs_target=? "
                    "WHERE id=?",
                    (
                        previous["scene_refs_status"], previous["scene_refs_error"],
                        previous["scene_refs_target"], project_id,
                    ),
                )
            conn.commit()
        raise ValueError("场景图任务未能启动，原状态和费用凭证已保留，请重试") from exc
    return True

def _start_scene_bible_preparation(
    project_id: str,
    *,
    parent_run_id: str | None = None,
    requested_by: str = "system",
    trigger_type: str = "automatic",
) -> bool:
    """后台准备免费场景清单；图片生成仍必须经过独立费用确认。"""
    if _scene_assets_task_active(project_id):
        return False
    conn = get_conn()
    previous = conn.execute(
        "SELECT scene_refs_status,scene_refs_error FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    conn.execute(
        "UPDATE projects SET scene_refs_status='running',scene_refs_error=NULL WHERE id=?",
        (project_id,),
    )
    conn.commit()
    try:
        task_registry.spawn(
            "scene_bible",
            project_id,
            _scene_bible_task(
                project_id,
                parent_run_id=parent_run_id,
                requested_by=requested_by,
                trigger_type=trigger_type,
            ),
            project_id=project_id,
        )
    except Exception as exc:
        if previous:
            conn.execute(
                "UPDATE projects SET scene_refs_status=?,scene_refs_error=? WHERE id=?",
                (previous["scene_refs_status"], previous["scene_refs_error"], project_id),
            )
            conn.commit()
        raise ValueError("场景设定任务未能启动，原状态已保留，请重试") from exc
    return True

def _decode_scene_target(value: str | list[str] | None) -> str | list[str] | None:
    if not isinstance(value, str):
        return value
    parsed = _parse_json_value(value)
    if isinstance(parsed, list):
        names = [str(item).strip() for item in parsed if str(item).strip()]
        return list(dict.fromkeys(names)) or None
    return value.strip() or None

def _consume_pending_scene_regen_if_ready(project_id: str, scenes_ready: bool) -> None:
    """消费「风格确认后场景图自动续跑」这张票据（见 db.py 里 pending_scene_regen
    列的注释）：人物谱谱写/重谱成功时 _bible_task 写下这张票据，场景清单一旦真的
    就绪（这里，_scene_bible_task 成功落盘之后）就自动继续生成场景图——不必等
    用户之后碰巧访问场景库页面。

    用一次原子 UPDATE ... WHERE pending_scene_regen=1 消费：rowcount=0 说明没有
    待消费的票据（普通场景清单刷新、或已被消费过），直接跳过，不会重复触发。
    """
    conn = get_conn()
    project_columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "pending_scene_regen" not in project_columns or not scenes_ready:
        return
    cursor = conn.execute(
        "UPDATE projects SET pending_scene_regen=0 WHERE id=? AND pending_scene_regen=1",
        (project_id,),
    )
    conn.commit()
    if cursor.rowcount < 1:
        return
    try:
        started = _start_scene_refs_generation(project_id, None, resume=False)
    except Exception as exc:  # noqa: BLE001 场景清单仍然算成功，只是场景图续跑失败
        public = errors.record_and_format(
            exc, action="scene_refs_spawn_after_style_regen", context={"project_id": project_id},
        )
        conn.execute(
            "UPDATE projects SET scene_refs_status='failed',scene_refs_error=? WHERE id=?",
            (f"场景清单已就绪，但按新画风继续生成场景图未能启动，可在场景库重试。{public}", project_id),
        )
        conn.commit()
        return
    if not started:
        # 场景图任务已在跑（比如用户自己手动触发过）：票据的目的已经达成，不是失败。
        return

async def _scene_bible_task(
    project_id: str,
    *,
    parent_run_id: str | None = None,
    requested_by: str = "system",
    trigger_type: str = "automatic",
) -> None:
    """生成并落库场景清单，不绕过费用确认自动生成图片。"""
    from app.stages import generate_scene_bible
    conn = get_conn()
    recorder = WorkflowRecorder.create(
        workflow_type="scene_bible",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, "scene_bible"),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={"max_iterations": 4, "warning_blocks_downstream": True},
        parent_run_id=parent_run_id,
    )
    try:
        recorder.start()
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if not p or not p["bible_json"]:
            raise ValueError("人物谱不存在，不能生成场景 Bible")
        bible = Bible.model_validate(json.loads(p["bible_json"]))
        # 初始场景清单只取前 N 章：避免一上来就铺满全片场景；更靠后的新场景留到分镜阶段反应式补图。
        from app.scenes import SCENE_BIBLE_CHAPTER_WINDOW
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx LIMIT ?",
            (project_id, SCENE_BIBLE_CHAPTER_WINDOW)).fetchall())
        _, scenes = await recorder.step(
            "scene_bible",
            lambda: generate_scene_bible(chapters, bible, project_id=project_id),
            contract_key="scene_bible",
            agent_name="scene_bible",
        )
        # 重读 bible（人物谱可能已被并发流程更新），只覆盖 scenes 字段后回写。
        p2 = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        data = json.loads(p2["bible_json"]) if p2 and p2["bible_json"] else bible.model_dump()
        data["scenes"] = [s.model_dump() for s in scenes]
        conn.execute(
            "UPDATE projects SET bible_json=?,scene_refs_status='idle',scene_refs_error=NULL "
            "WHERE id=?",
            (json.dumps(data, ensure_ascii=False), project_id),
        )
        conn.commit()
        recorder.succeed("场景设定已准备，场景图等待费用确认", conn=None)
        _consume_pending_scene_regen_if_ready(project_id, bool(scenes))
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，场景设定任务等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:  # noqa: BLE001 场景设定失败不阻断人物谱主流程
        recorder.fail(exc, conn=None)
        public = errors.record_and_format(exc, action="scene_bible_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()

def _reserve_scene_refs_recorder(
    conn, project_id: str, only_scene: str | list[str] | None, *,
    requested_by: str, trigger_type: str, parent_run_id: str | None,
) -> WorkflowRecorder:
    """账号维度并发准入与占位（workflow_runs 那一行）在同一个 BEGIN IMMEDIATE
    事务里完成——消除「先数后建」的 TOCTOU（CLAUDE.md「Gates and Criteria」/
    「Ownership Must Be Explicit」）。照抄 media_scheduler.reserve_budget 的
    owns_transaction 惯例：check 通过后才调用 WorkflowRecorder.create()，它内部
    的 conn.commit() 顺带收口本事务，不新造第二套事务风格。找不到归属账号
    （legacy-shared 兼容路径）时不拦截，与既有各处 quota 接入点一致。"""
    owner_user_id = quota.owner_of_project(conn, project_id)
    owns_transaction = not conn.in_transaction
    try:
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if owner_user_id is not None:
            active = count_active_project_scoped_workflow_runs(
                conn, owner_user_id, "scene_references", exclude_run_id=None,
            )
            quota.check_module_concurrency(
                conn, owner_user_id, QUOTA_MODULE_SCENE_REF, active_count=active,
            )
        recorder = WorkflowRecorder.create(
            workflow_type="scene_references",
            scope_type="project",
            scope_id=project_id,
            input_fingerprint=fingerprint(project_id, only_scene, "scene_references"),
            requested_by=requested_by,
            trigger_type=trigger_type,
            budget_limit_cny=float(get_setting("episode_cost_limit_cny") or 100),
            parent_run_id=parent_run_id,
        )
        if owns_transaction and conn.in_transaction:
            conn.commit()
        return recorder
    except Exception:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise

async def _scene_refs_task(
    project_id: str,
    only_scene: str | list[str] | None,
    *,
    resume: bool = False,
    operation_started_at: float | None = None,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
):
    from app.scenes import SceneCandidateReviewRequired, generate_scene_refs
    conn = get_conn()
    try:
        recorder = _reserve_scene_refs_recorder(
            conn, project_id, only_scene,
            requested_by=requested_by, trigger_type=trigger_type,
            parent_run_id=parent_run_id,
        )
    except quota.QuotaExceeded as exc:
        # 账号并发准入未通过：没有 workflow_run 行被创建（占位与判定同一个
        # BEGIN IMMEDIATE 事务，见 _reserve_scene_refs_recorder），因此这里没有
        # recorder 可以 fail()——直接把配额错误落到项目状态上，与下面
        # generate_scene_refs 真正失败时的落地字段一致，前端读同一处即可。
        public = errors.record_and_format(
            exc, action="scene_refs_generate", context={"project_id": project_id},
        )
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()
        return
    try:
        recorder.start()
        await recorder.step(
            "scene_references",
            lambda: generate_scene_refs(
                project_id,
                only_scene,
                resume=resume,
                operation_started_at=operation_started_at,
            ),
            agent_name="reference_asset_loop",
        )
        conn.execute(
            "UPDATE projects SET scene_refs_status='ready',scene_refs_error=NULL,"
            "scene_refs_batch_started_at=NULL WHERE id=?",
            (project_id,),
        )
        conn.commit()
        recorder.succeed("场景参考资产已生成并通过证据门禁", conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，人物单视角重做等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except SceneCandidateReviewRequired as exc:
        message = str(exc)[:1200]
        recorder.partial(message, conn=None)
        conn.execute(
            "UPDATE projects SET scene_refs_status='warning', scene_refs_error=? WHERE id=?",
            (message, project_id),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc, conn=None)
        public = errors.record_and_format(exc, action="scene_refs_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()
