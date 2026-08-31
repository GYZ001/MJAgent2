"""角色引用图生成的任务发起、进行中判定与后台任务体、start/cancel 入口。

从 app/domain/bible_ops.py 按原样搬移；与 scene_bible_prep 各自独立，互不依赖。
"""
from __future__ import annotations

import asyncio

from app import (
    errors,
    quota,
    quota_expiry,
    task_registry,
    worker,
)
from app.db import (
    get_conn,
    get_setting,
    now,
)
from app.domain.common import (
    _project_or_404,
    _refs_task_active,
    router,
)
from app.orchestration.engine import (
    WorkflowRecorder,
    fingerprint,
)
from fastapi import HTTPException

from .precheck import compute_refs_cost_precheck
from .primitives import (
    QUOTA_MODULE_PORTRAIT,
    _consume_payment_quote,
    _issue_payment_quote,
    _normalize_character_selection,
    _payment_confirm_required,
    _refs_target_payload,
    _validate_payment_quote,
    count_active_project_scoped_workflow_runs,
)


def _new_refs_recorder(
    project_id: str,
    only_character: str | None,
    only_characters: list[str] | None,
    *,
    resume: bool,
    fresh_after: float | None,
    parent_run_id: str | None,
    requested_by: str,
    trigger_type: str,
) -> WorkflowRecorder:
    return WorkflowRecorder.create(
        workflow_type="character_references",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(
            project_id, only_character, only_characters, "character_references",
            resume, fresh_after,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        config_snapshot={
            "only_character": only_character,
            "only_characters": only_characters,
            "resume": resume,
            "fresh_after": fresh_after,
        },
        budget_limit_cny=float(get_setting("episode_cost_limit_cny") or 100),
        parent_run_id=parent_run_id,
    )

def _reserve_refs_recorder(
    conn, project_id: str, only_character: str | None, only_characters: list[str] | None, *,
    resume: bool, fresh_after: float | None, parent_run_id: str | None,
    requested_by: str, trigger_type: str,
) -> WorkflowRecorder:
    """账号维度并发准入与占位（workflow_runs 那一行）在同一个 BEGIN IMMEDIATE
    事务里完成——消除「先数后建」的 TOCTOU（CLAUDE.md「Gates and Criteria」/
    「Ownership Must Be Explicit」）。与
    scene_bible_prep._reserve_scene_refs_recorder 同一套惯例（照抄
    media_scheduler.reserve_budget 的 owns_transaction 写法，不新造第二套事务
    风格）：check 通过后才调用 _new_refs_recorder()，其内部
    WorkflowRecorder.create() 的 conn.commit() 顺带收口本事务。找不到归属账号
    （legacy-shared 兼容路径）时不拦截，与既有各处 quota 接入点一致。"""
    owner_user_id = quota.owner_of_project(conn, project_id)
    owns_transaction = not conn.in_transaction
    try:
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if owner_user_id is not None:
            quota_expiry.assert_membership_active(conn, owner_user_id)
            active = count_active_project_scoped_workflow_runs(
                conn, owner_user_id, "character_references", exclude_run_id=None,
            )
            quota.check_module_concurrency(
                conn, owner_user_id, QUOTA_MODULE_PORTRAIT, active_count=active,
            )
        recorder = _new_refs_recorder(
            project_id, only_character, only_characters,
            resume=resume, fresh_after=fresh_after, parent_run_id=parent_run_id,
            requested_by=requested_by, trigger_type=trigger_type,
        )
        if owns_transaction and conn.in_transaction:
            conn.commit()
        return recorder
    except Exception:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise

def _active_refs_run(project_id: str):
    return get_conn().execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='character_references'
             AND scope_type='project'
             AND scope_id=?
             AND status='RUNNING'
           ORDER BY updated_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()

def _refs_generation_busy(project_id: str) -> bool:
    return _refs_task_active(project_id) or _active_refs_run(project_id) is not None

def _start_refs_generation(
    project_id: str,
    only_character: str | None,
    *,
    only_characters: list[str] | None = None,
    resume: bool = False,
    fresh_after: float | None = None,
    parent_run_id: str | None = None,
) -> dict | None:
    """启动定妆照任务。

    返回可追踪的任务与 run id；已有同项目任务时返回 None。
    """
    if _refs_generation_busy(project_id):
        return None
    conn = get_conn()
    previous = conn.execute(
        "SELECT refs_status,refs_error,refs_target,refs_resume,refs_batch_started_at "
        "FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    target_payload = _refs_target_payload(only_character, only_characters)
    # ``fresh_after`` controls which ready packs resume may skip.  The operation
    # batch timestamp is separate and always durable so paid image calls can be
    # reused after a restart, including gap-only resume batches.
    previous_batch_started_at = previous["refs_batch_started_at"] if previous else None
    batch_started_at = (
        fresh_after
        if fresh_after is not None
        else previous_batch_started_at
        if resume and previous_batch_started_at is not None
        else now()
    )
    persisted_resume = 1 if resume and fresh_after is None else 0
    if target_payload is None:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=NULL, "
            "refs_resume=?, refs_batch_started_at=? WHERE id=?",
            (persisted_resume, batch_started_at, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=?, "
            "refs_resume=?, refs_batch_started_at=? WHERE id=?",
            (target_payload, persisted_resume, batch_started_at, project_id),
        )
    conn.commit()
    requested_by = "system" if resume else "user"
    trigger_type = "resume" if resume else "manual"
    recorder = None
    try:
        recorder = _reserve_refs_recorder(
            conn, project_id, only_character, only_characters,
            resume=resume, fresh_after=batch_started_at, parent_run_id=parent_run_id,
            requested_by=requested_by, trigger_type=trigger_type,
        )
        task_registry.spawn(
            "refs", project_id,
            _refs_task(
                project_id, only_character, only_characters=only_characters,
                resume=resume, fresh_after=fresh_after,
                operation_started_at=batch_started_at,
                parent_run_id=parent_run_id,
                requested_by=requested_by, trigger_type=trigger_type, recorder=recorder,
            ),
            project_id=project_id,
        )
    except quota.QuotaExceeded:
        # 配额超限时占位从未落地（_reserve_refs_recorder 的 BEGIN IMMEDIATE 事务
        # 直接回滚，没有 workflow_run 行，也没有 recorder 可以 cancel）；项目状
        # 态回滚到之前，429 detail（tier/limit/upgrade_path）原样透传给前端，不
        # 能被下面的通用处理糊成 ValueError（CLAUDE.md「拦住用户时必须给出路」）。
        if previous:
            conn.execute(
                "UPDATE projects SET refs_status=?,refs_error=?,refs_target=?,"
                "refs_resume=?,refs_batch_started_at=? WHERE id=?",
                (
                    previous["refs_status"], previous["refs_error"], previous["refs_target"],
                    previous["refs_resume"], previous["refs_batch_started_at"], project_id,
                ),
            )
            conn.commit()
        raise
    except Exception as exc:
        if previous:
            conn.execute(
                "UPDATE projects SET refs_status=?,refs_error=?,refs_target=?,"
                "refs_resume=?,refs_batch_started_at=? WHERE id=?",
                (
                    previous["refs_status"], previous["refs_error"], previous["refs_target"],
                    previous["refs_resume"], previous["refs_batch_started_at"], project_id,
                ),
            )
            conn.commit()
        if recorder is not None:
            try:
                recorder.cancel("定妆任务未能启动，项目状态已回滚", conn=None)
            except Exception:  # noqa: BLE001
                pass
        raise ValueError("定妆任务未能启动，原状态和费用凭证已保留，请重试") from exc
    return {
        "status": "accepted",
        "task_id": f"refs:{project_id}",
        "run_id": recorder.run_id,
    }

def _established_portrait_gap_names(conn, project_id: str) -> list[str]:
    """已建卡角色里「缺图或出图失败」的名单：POST /projects/{id}/refs 在没有
    显式指定 character(s) 时的补图范围来源，只补残缺、不重复出图。

    名单来源限定 ``character_portraits`` 里非作废槽位（``ep_start>=0``）——
    负数 ``ep_start`` 是 ``promote_staged_initial_portrait`` 压入的已作废历史
    定妆照槽位，纳入会把重做过定妆照的角色错误地算成多张（与
    app.portraits.card_rebind 模块 docstring 同一条纪律）。2026-08-31 架构
    转向后角色只随映射台按需建卡，这里直接查已经真正建过定妆记录的角色，
    不依赖 bible_json 快照。

    「有没有缺口」的判据与 compute_refs_cost_precheck 的 resume 分支同口径
    （当前采用版本 ``ep_end IS NULL``、pack_status、必需视角是否齐全），不
    重写第二份相似判据；已有整包且视角齐全的角色不出现在返回值里，调用方
    据此保证「已有图不重复出图、不重复烧钱」。
    """
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    rows = conn.execute(
        "SELECT DISTINCT character_name FROM character_portraits "
        "WHERE project_id=? AND ep_start>=0",
        (project_id,),
    ).fetchall()
    established = sorted({row["character_name"] for row in rows if row["character_name"]})
    gaps: list[str] = []
    for name in established:
        current = conn.execute(
            "SELECT id, pack_status FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_end IS NULL "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name),
        ).fetchone()
        if not current or current["pack_status"] not in (None, "ready"):
            gaps.append(name)
            continue
        ready_roles = {
            row["view_role"] for row in conn.execute(
                "SELECT view_role, status, image_path FROM character_portrait_views "
                "WHERE portrait_id=?", (current["id"],),
            ).fetchall()
            if row["status"] == "ready" and row["image_path"]
        }
        if any(role not in ready_roles for role in CHARACTER_REQUIRED_VIEWS):
            gaps.append(name)
    return gaps

async def _refs_task(
    project_id: str,
    only_character: str | None,
    *,
    only_characters: list[str] | None = None,
    resume: bool = False,
    fresh_after: float | None = None,
    operation_started_at: float | None = None,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
    recorder: WorkflowRecorder | None = None,
):
    from app.refs import generate_refs
    conn = get_conn()
    if recorder is None:
        # 正常生产路径（_start_refs_generation）已经预先建好 recorder；这个
        # 分支只服务测试/未来直接调用本函数的调用方。占位与账号并发准入必须
        # 同一个事务，理由与 _start_refs_generation 里的调用一致，见
        # _reserve_refs_recorder 的说明。
        try:
            recorder = _reserve_refs_recorder(
                conn, project_id, only_character, only_characters,
                resume=resume, fresh_after=fresh_after, parent_run_id=parent_run_id,
                requested_by=requested_by, trigger_type=trigger_type,
            )
        except quota.QuotaExceeded as exc:
            public = errors.record_and_format(
                exc, action="refs_generate", context={"project_id": project_id},
            )
            conn.execute(
                "UPDATE projects SET refs_status='failed', refs_error=? WHERE id=?",
                (public, project_id),
            )
            conn.commit()
            return
    try:
        recorder.start()
        # 账号维度并发准入已经在 recorder 创建时（_reserve_refs_recorder）与
        # workflow_runs 那一行的插入绑在同一个 BEGIN IMMEDIATE 事务里做过，这
        # 里不重复判（理由同 screenplay_ops.guarded 的对应注释）。
        # 新包结构完整后才使旧定妆的下游产物失效；质量评分不参与采用资格。
        # 这样技术失败或中止不会破坏当前可用链路。
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if only_characters:
            names = only_characters
        elif only_character:
            names = [only_character]
        elif p and p["bible_json"]:
            # 未显式指定角色范围时，名单来自已建卡角色的既有定妆记录，不是发现
            # 新角色——这是修补已知残缺（供应商失败/内容审核拦截/并发中断导致
            # 的缺图），不是产生新发现。名单本身已经只含缺口，「已有图不重复
            # 出图」由此保证；不额外强改调用方的 resume——它仍只表达调用方
            # 自己的语义（例如是否顺带作废旧视频等下游产物），不是本次要动的
            # 范围（2026-08-31 用户拍板）。
            names = _established_portrait_gap_names(conn, project_id)
            only_characters = names
        else:
            names = []
        await recorder.step(
            "character_references",
            lambda: generate_refs(
                project_id, only_character, only_characters=only_characters,
                resume=resume, fresh_after=fresh_after,
                operation_started_at=operation_started_at,
            ),
            agent_name="reference_asset_loop",
        )
        if not resume:
            worker.purge_character_video_artifacts(project_id, names)
        conn.execute(
            "UPDATE projects SET refs_status='ready', refs_error=NULL, refs_target=NULL, "
            "refs_batch_started_at=NULL WHERE id=?",
            (project_id,),
        )
        conn.commit()
        recorder.succeed("人物参考资产已生成且结构完整", conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，定妆任务等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:  # noqa: BLE001
        # 回滚必须在本 except 块的第一条语句做——不止要早于 errors.record_and_format()，
        # 还要早于上面这行 recorder.fail(exc)。WorkflowRecorder.fail() 内部调用
        # app.orchestration.state_machine.transition_run(..., conn=None)，后者
        # ``db = conn or get_conn()`` 取的是同一个 task 缓存连接，并且在
        # ``conn is None`` 时自己 db.commit()——跟 app.db.insert_error_log 是完全
        # 同一类隐式提交，谁先调用谁就先把此刻挂起的写入定型。这条 try 里
        # ``if not resume: worker.purge_character_video_artifacts(...)`` 对本项目
        # 命中角色的镜头逐条 DELETE shot_versions/shot_scenes/jobs、回退所属剧集
        # 状态，整段不提交，只在处理完全部镜头后 conn.commit() 一次；中途失败会把
        # 未提交的部分 DELETE 留在这个连接上。如果不把回滚提到 recorder.fail(exc)
        # 之前，这一行自己的隐式 commit 就会先把半成品定型进库——回滚只丢弃这次
        # 失败尝试自己产生的未提交写入，不影响更早已经各自 commit 过的检查点。
        if conn.in_transaction:
            conn.rollback()
        recorder.fail(exc, conn=None)
        public = errors.record_and_format(exc, action="refs_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET refs_status='failed', refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()

@router.post("/projects/{project_id}/refs")
async def start_refs(project_id: str, body: dict | None = None):
    from app.capabilities.dispatch import ui_route
    payload = body or {}
    selected_names = _normalize_character_selection(payload.get("characters"))
    routed = await ui_route(
        "portrait.generate",
        {
            "project_id": project_id,
            "character": payload.get("character"),
            "characters": selected_names,
            "resume": bool(payload.get("resume", False)),
            "confirm": payload.get("confirm") is True,
            "quote_id": payload.get("quote_id"),
            "idempotency_key": payload.get("idempotency_key") or payload.get("quote_id"),
        },
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if _refs_generation_busy(project_id) or p["refs_status"] == "running":
        raise HTTPException(409, "定妆照正在生成中")
    only = payload.get("character")
    if only and selected_names and only not in selected_names:
        raise HTTPException(422, "character 与 characters 范围不一致")
    resume = bool(payload.get("resume", False))
    quote_character = only if not selected_names else None
    quote = compute_refs_cost_precheck(
        project_id, character=quote_character, characters=selected_names, resume=resume
    )
    if payload.get("confirm") is not True:
        # 见 task_run.py 同类注释：未签发的报价不能作为 409 里的 quote_id 递
        # 出去，否则调用方按响应指引确认必然 QUOTE_STALE。
        raise _payment_confirm_required(_issue_payment_quote(quote))
    quote_id = payload.get("quote_id")
    quote_row = _validate_payment_quote(project_id, quote_id, quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "quote_id": quote_id, "task_id": quote_row["consumed_task_id"],
            "run_id": quote_row["consumed_run_id"], "precheck": quote,
        }
    generation_only = selected_names[0] if selected_names and len(selected_names) == 1 else only
    started = _start_refs_generation(
        project_id,
        generation_only,
        only_characters=selected_names,
        resume=resume,
    )
    if not started:
        raise HTTPException(409, "定妆照正在生成中")
    _consume_payment_quote(
        str(quote_id), task_id=started["task_id"], run_id=started["run_id"],
    )
    return {**started, "quote_id": quote_id, "precheck": quote}

@router.post("/projects/{project_id}/refs/cancel")
async def cancel_refs(project_id: str):
    """停止定妆照生成。已落盘的定妆照保留，状态置回空闲。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("portrait.cancel", {"project_id": project_id})
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    stopped = await task_registry.cancel_and_wait("refs", project_id)
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET refs_status='idle', refs_error=NULL, refs_target=NULL, "
        "refs_batch_started_at=NULL WHERE id=?", (project_id,))
    conn.commit()
    was_running = p["refs_status"] == "running"
    return {"stopped": stopped or was_running}
