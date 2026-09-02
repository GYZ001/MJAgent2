"""场景引用图/场景圣经准备的发起与后台任务体（供 recover/task_run/scene_refs 复用）。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import asyncio
import json

from app import (
    errors,
    quota,
    quota_expiry,
    task_registry,
)
from app.db import (
    get_conn,
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
        raise ValueError("场景图任务未能启动，原状态和范围凭证已保留，请重试") from exc
    return True

def _start_scene_bible_preparation(
    project_id: str,
    *,
    parent_run_id: str | None = None,
    requested_by: str = "system",
    trigger_type: str = "automatic",
) -> bool:
    """后台准备场景清单；图片生成仍必须经过独立范围确认。"""
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

# `_consume_pending_scene_regen_if_ready`（消费 db.py 里 pending_scene_regen
# 列的票据、场景清单落盘后自动续跑场景图）随 2026-08-31 架构转向一并退场：
# 唯一的票据写入方是 app/domain/bible_ops/task_run.py 的 _bible_task 场景清单
# 自动级联，那段已经删除（generate_scene_bible 退出首版流程，场景改为
# app.scenes.assess_new_scene 反应式发现），这个函数因此永远读不到 flag=1、
# 变成恒定空转的死代码——按「退场必须同时带走为它服务的机器」一并删除，不
# 留一个看起来在守护、实际再也不会触发的消费者。场景图生成现在只有两个入口：
# 用户在场景库页手动确认（start_scene_bible 里直接调用
# _start_scene_refs_generation，不经过票据）与画风切换（set_bible_visual_style
# 同理直接调用），都不依赖这张票据。

def _merge_planned_scenes(planned: list[dict], existing: list[dict]) -> list[dict]:
    """规划清单优先；已在库里、但不在清单里的场景原样保留；同名场景合并别名。

    场景圣经任务曾对 ``scenes`` 字段整体替换（"重读 bible 只覆盖 scenes 字段"）——
    它防住了并发的人物谱更新，却没防住并发的**场景**追加：映射台的反应式场景发现
    （app.scenes.ensure_scenes_for_labels → _append_scene_to_bible）在同一份 bible_json
    上追加场景。真实事故（2026-09-02《神墓》proj_facfc3964f69，人物谱版本流水 v4→v5）：
    01:07:13/01:07:20 发现刚追加「上古神魔陵园」「冬日积雪雪枫林」，01:07:21 这里
    整体回写把两条覆盖掉，本集映射随即两轮都因「未解析到 scene_reference_id」失败。
    库里已有而清单里没有的场景是别的流程核验后写进去的事实，这里没有资格丢弃它。
    """
    remaining = {
        str(scene.get("name") or "").strip(): scene
        for scene in existing
        if isinstance(scene, dict) and str(scene.get("name") or "").strip()
    }
    merged: list[dict] = []
    for scene in planned:
        prior = remaining.pop(str(scene.get("name") or "").strip(), None)
        if prior is not None:
            aliases = [*(scene.get("aliases") or []), *(prior.get("aliases") or [])]
            scene = {**scene, "aliases": list(dict.fromkeys(a for a in aliases if a))}
        merged.append(scene)
    merged.extend(remaining.values())
    return merged


_PLANNED_SCENES_CAS_ATTEMPTS = 5


def _persist_planned_scenes(conn, project_id: str, planned: list[dict], *, fallback: Bible) -> None:
    """把规划好的场景清单合并进 bible_json 回写；CAS 钉在 bible_version 上。

    与 app.scenes._commit_scene_bible_mutation 用同一把「版本号」乐观锁：读到的
    版本与写入时不一致就重读、重合并、重写，重试耗尽则失败关闭，绝不盲写。
    ``fallback`` 只在 projects 行还没有 bible_json 时兜底成形状，不改变任何决策。
    """
    for _attempt in range(_PLANNED_SCENES_CAS_ATTEMPTS):
        row = conn.execute(
            "SELECT bible_json,bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        data = json.loads(row["bible_json"]) if row and row["bible_json"] else fallback.model_dump()
        data["scenes"] = _merge_planned_scenes(planned, data.get("scenes") or [])
        Bible.model_validate(data)
        expected = int((row["bible_version"] if row else 0) or 0)
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=?,scene_refs_status='idle',"
            "scene_refs_error=NULL WHERE id=? AND COALESCE(bible_version,0)=?",
            (json.dumps(data, ensure_ascii=False), expected + 1, project_id, expected),
        )
        conn.commit()
        if cursor.rowcount == 1:
            return
    raise ValueError(
        f"场景清单落库时人物谱被并发修改，重试 {_PLANNED_SCENES_CAS_ATTEMPTS} 次仍未成功"
    )


async def _scene_bible_task(
    project_id: str,
    *,
    parent_run_id: str | None = None,
    requested_by: str = "system",
    trigger_type: str = "automatic",
) -> None:
    """生成并落库场景清单，不绕过范围确认自动生成图片。"""
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
        _persist_planned_scenes(
            conn, project_id, [s.model_dump() for s in scenes], fallback=bible,
        )
        recorder.succeed("场景设定已准备，场景图等待范围确认", conn=None)
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
            quota_expiry.assert_membership_active(conn, owner_user_id)
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
    from app.scenes import generate_scene_refs
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
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc, conn=None)
        public = errors.record_and_format(exc, action="scene_refs_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()
