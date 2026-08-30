"""分镜生成任务体本体（逐节拍生成主循环）及素材后台准备。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 evidence 与 mutation_primitives。单个函数 _storyboard_task 426 行，
是分镜生成状态机判据与提前返回控制流的唯一权威顺序，拆分会打散终态判决执行顺序，不做拆分（移动未拆分）。
"""
from __future__ import annotations

import asyncio
import json

from app import (
    config,
    errors,
    task_registry,
)
from app.db import (
    get_conn,
    log_provider_call,
    now,
)
from app.domain.common import (
    _load_screenplay,
    _project_bible_or_placeholder,
    episode_prep_pack_payload,
)
from app.evidence import repository as evidence_repository
from app.schemas import Storyboard
from app.stages import StageError

from .evidence import _ensure_current_storyboard_shot_artifacts
from .mutation_primitives import _board_from_shot_rows


async def _prepare_storyboard_assets_background(episode_id: str) -> None:
    """Fill portrait/scene assets without blocking screenplay-to-storyboard text work."""
    from app.observability.tracing import detached_trace

    # asyncio tasks copy ContextVars when spawned. Asset discovery is an
    # independent lifecycle, so do not attribute its later provider calls to
    # the storyboard text step that happened to schedule it.
    with detached_trace():
        await _prepare_storyboard_assets_background_detached(episode_id)

async def _prepare_storyboard_assets_background_detached(episode_id: str) -> None:
    conn = get_conn()
    ep = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if not ep or not ep["screenplay_json"]:
        return
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
    ).fetchone()
    bible = _project_bible_or_placeholder(project)
    screenplay = _load_screenplay(ep)
    if screenplay is None:
        # _load_screenplay() deliberately returns None for an
        # episode_prep_pack projection (screenplay contract 6.0.0+) --
        # callers built for the legacy EpisodeScreenplay shape must not get
        # a silently-empty object (see its docstring in app.domain.common).
        # That guard must not become "skip asset prep for every prep_pack
        # episode": the storyboard stage still needs portrait/scene assets
        # resolved before it can run, so project the prep_pack payload here
        # instead of reusing _load_screenplay's legacy-only return value.
        prep_pack_payload = episode_prep_pack_payload(ep)
        if prep_pack_payload is None:
            return
        from app.production.screenplay_authority import (
            project_prep_pack_to_screenplay,
        )

        screenplay = project_prep_pack_to_screenplay(prep_pack_payload)
    try:
        from app.portraits import ensure_cards_for_screenplay

        portrait_result = await ensure_cards_for_screenplay(
            ep["project_id"],
            ep["episode_no"],
            screenplay,
            bible,
        )
        if portrait_result.get("blocking_errors"):
            raise StageError(
                "人物资产准备",
                list(portrait_result["blocking_errors"]),
            )
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
        ).fetchone()
        bible = _project_bible_or_placeholder(project)

        from app.scenes import ensure_scenes_for_storyboard

        scene_result = await ensure_scenes_for_storyboard(
            ep["project_id"],
            ep["episode_no"],
            screenplay,
            bible,
        )
        if scene_result.get("blocking_errors"):
            raise StageError(
                "场景资产准备",
                list(scene_result["blocking_errors"]),
            )
        conn.execute(
            "UPDATE episodes SET storyboard_warning=NULL WHERE id=? "
            "AND storyboard_warning LIKE '资产异步准备:%'",
            (episode_id,),
        )
        conn.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - text work remains independently recoverable
        public = errors.record_and_format(
            exc,
            action="storyboard_assets_background",
            context={
                "episode_id": episode_id,
                "project_id": ep["project_id"],
            },
        )
        conn.execute(
            "UPDATE episodes SET storyboard_warning=? WHERE id=?",
            (
                (
                    "资产异步准备: 人物或场景参考资产尚未完整就绪；"
                    "分镜文本不受影响，视频提交前会继续补齐。"
                    + public
                )[:800],
                episode_id,
            ),
        )
        conn.commit()

async def _storyboard_task(
    episode_id: str,
    *,
    resume: bool = True,
    run_id: str | None = None,
    new_activation: bool = False,
):
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()

    def _preflight_event(event_type: str, message: str, *, payload: dict | None = None,
                         severity: str = "info") -> None:
        if not run_id:
            return
        from app.evidence import repository as _evidence_repository
        _evidence_repository.append_event(
            run_id, event_type, severity, message,
            payload={"episode_id": episode_id, "episode_no": ep["episode_no"], **(payload or {})},
        )

    try:
        if ep["screenplay_status"] != "ready" or not ep["screenplay_json"]:
            raise StageError("分镜脚本", ["请先生成并确认本集可拍剧本，再展开分镜"])
        from app.production.screenplay_authority import resolve_downstream_screenplay

        try:
            screenplay_context = resolve_downstream_screenplay(
                episode_id,
                conn=conn,
            )
        except Exception as exc:
            raise StageError("分镜脚本", [f"已发布剧本权威链无效：{exc}"]) from exc
        screenplay = screenplay_context.screenplay
        narrative_authority = screenplay_context.narrative_authority_required
        published_storyboard_baseline = False
        if (
            narrative_authority
            and ep["published_storyboard_artifact_id"]
            and ep["storyboard_completion_certificate_id"]
        ):
            try:
                from app.production.certificate import verify_completion_certificate

                baseline_certificate = verify_completion_certificate(
                    str(ep["storyboard_completion_certificate_id"]),
                    expected_kind="storyboard",
                    expected_scope_id=episode_id,
                    expected_artifact_id=str(
                        ep["published_storyboard_artifact_id"]
                    ),
                    expected_production_revision_id=str(
                        ep["storyboard_production_revision_id"] or ""
                    ),
                    allow_consumed=True,
                    allow_stale_artifact_for_revision=True,
                )
                published_storyboard_baseline = bool(
                    baseline_certificate.consumed_at is not None
                )
            except Exception:
                published_storyboard_baseline = False
        if resume and published_storyboard_baseline:
            rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                (episode_id,),
            ).fetchall()
            board = _board_from_shot_rows(rows, ep["episode_no"])
            from app.production.certificate import (
                verify_completion_certificate,
            )
            from app.narrative import storyboard_authority_projection

            published_artifact = evidence_repository.get_artifact(
                str(ep["published_storyboard_artifact_id"])
            )
            if published_artifact is None:
                raise StageError("分镜脚本", ["当前发布分镜 Artifact 已缺失"])
            published_board = Storyboard.model_validate(
                published_artifact.get("content") or {}
            )
            verify_completion_certificate(
                str(ep["storyboard_completion_certificate_id"]),
                expected_kind="storyboard",
                expected_scope_id=episode_id,
                expected_artifact_id=str(ep["published_storyboard_artifact_id"]),
                expected_production_revision_id=str(
                    ep["storyboard_production_revision_id"] or ""
                ),
                allow_consumed=True,
                allow_stale_artifact_for_revision=True,
            )
            projection_restored = bool(
                storyboard_authority_projection(board)
                != storyboard_authority_projection(published_board)
            )
            if projection_restored:
                if ep["status"] in {"confirmed", "generating", "done", "mixed"}:
                    raise StageError(
                        "分镜脚本",
                        ["已确认分镜投影与证书漂移，禁止自动覆盖，请先停止下游"],
                    )
                if len(rows) != len(published_board.shots):
                    raise StageError(
                        "分镜脚本",
                        ["当前 shots 行数与已签证 Storyboard Artifact 不一致"],
                    )
                from app.storyboard_supervisor import _write_shot_fields

                conn.execute("BEGIN IMMEDIATE")
                try:
                    for row, shot in zip(rows, published_board.shots):
                        _write_shot_fields(
                            conn,
                            str(row["id"]),
                            shot,
                            row["storyboard_artifact_id"],
                            narrative_authority=True,
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                rows = conn.execute(
                    "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
                    (episode_id,),
                ).fetchall()
                board = _board_from_shot_rows(rows, ep["episode_no"])
                _preflight_event(
                    "STORYBOARD_PUBLISHED_PROJECTION_RESTORED",
                    "已从签证 Artifact 恢复 mutable shots 正式投影",
                    payload={
                        "artifact_id": ep["published_storyboard_artifact_id"],
                        "shot_count": len(rows),
                    },
                    severity="warning",
                )

            # The immutable baseline was verified above with its exact
            # artifact, revision and evaluation set. A stale baseline may
            # seed an isolated revision, but it no longer authorizes
            # downstream work; the replacement publish issues a new
            # completion certificate.
            p = conn.execute(
                "SELECT * FROM projects WHERE id=?",
                (ep["project_id"],),
            ).fetchone()
            bible = _project_bible_or_placeholder(p)
            from app.identity_contracts import (
                canonicalize_storyboard_operational_identities,
            )

            identity_repairs = canonicalize_storyboard_operational_identities(
                board,
                bible,
                screenplay,
            )
            if not identity_repairs:
                from app.storyboard_supervisor import (
                    _repair_is_pending,
                    load_latest_checkpoint,
                    run_storyboard_supervisor,
                )

                repair_checkpoint = load_latest_checkpoint(episode_id)
                if (
                    repair_checkpoint is not None
                    and _repair_is_pending(repair_checkpoint)
                    and ep["status"] in {"scripted", "scripting"}
                ):
                    _preflight_event(
                        "STORYBOARD_PUBLISHED_REPAIR_CANDIDATE_STARTED",
                        "已基于发布分镜建立隔离修订候选，正式投影保持不变",
                        payload={
                            "artifact_id": ep["published_storyboard_artifact_id"],
                            "window_start": (
                                repair_checkpoint.last_repair or {}
                            ).get("window_start"),
                            "window_end": (
                                repair_checkpoint.last_repair or {}
                            ).get("window_end"),
                        },
                    )
                    return await run_storyboard_supervisor(
                        episode_id,
                        resume=True,
                        run_id=run_id,
                        preflight_done=True,
                        new_activation=False,
                    )
            if not identity_repairs and projection_restored:
                conn.execute(
                    "UPDATE episodes SET status='scripted',script_error=NULL,"
                    "storyboard_warning=NULL WHERE id=?",
                    (episode_id,),
                )
                conn.commit()
                from app.storyboard_supervisor import load_latest_checkpoint

                return load_latest_checkpoint(episode_id)
            if not identity_repairs or ep["status"] in {
                "confirmed", "generating", "done", "mixed",
            }:
                raise StageError(
                    "分镜脚本",
                    [
                        "当前叙事分镜已原子发布，不能在正式 shots 上原地续跑；"
                        "请创建语义修订候选并重新发布"
                    ],
                )
            old_artifact_id = str(ep["published_storyboard_artifact_id"])
            old_revision_id = str(ep["storyboard_production_revision_id"] or "")
            stamp = now()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if old_revision_id:
                    conn.execute(
                        """UPDATE production_revisions
                              SET status='superseded',updated_at=?
                            WHERE id=? AND status='published'
                              AND published_artifact_id=?""",
                        (stamp, old_revision_id, old_artifact_id),
                    )
                conn.execute(
                    """UPDATE artifacts
                          SET status='superseded',
                              stale_reason='deterministic_identity_projection_rebind'
                        WHERE id=? AND status IN ('validated','approved')""",
                    (old_artifact_id,),
                )
                episode_update = conn.execute(
                    """UPDATE episodes
                          SET storyboard_artifact_id=NULL,
                              working_storyboard_artifact_id=NULL,
                              published_storyboard_artifact_id=NULL,
                              storyboard_completion_certificate_id=NULL,
                              storyboard_production_revision_id=NULL
                        WHERE id=? AND status IN ('scripted','scripting')
                          AND published_storyboard_artifact_id=?
                          AND storyboard_completion_certificate_id=?""",
                    (
                        episode_id,
                        old_artifact_id,
                        ep["storyboard_completion_certificate_id"],
                    ),
                )
                if episode_update.rowcount != 1:
                    raise RuntimeError("分镜身份修订撤下旧发布指针发生并发冲突")
                from app.storyboard_supervisor import _write_shot_fields

                for row, shot in zip(rows, board.shots):
                    _write_shot_fields(
                        conn,
                        str(row["id"]),
                        shot,
                        None,
                        narrative_authority=True,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            _ensure_current_storyboard_shot_artifacts(
                conn,
                episode_id,
                board,
            )
            conn.commit()
            _preflight_event(
                "STORYBOARD_IDENTITY_PROJECTION_REVISION_CREATED",
                "已撤下未确认旧版并创建确定性身份修订工作投影",
                payload={
                    "old_artifact_id": old_artifact_id,
                    "old_revision_id": old_revision_id,
                    "repairs": identity_repairs,
                },
            )
            ep = conn.execute(
                "SELECT * FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            published_storyboard_baseline = False
        conn.execute("UPDATE episodes SET status='scripting', script_error=NULL, storyboard_warning=NULL WHERE id=?", (episode_id,))
        conn.commit()
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        # Text identities were hard-gated during screenplay production. Image
        # packages now start immediately but no longer serialize storyboard text.
        if not task_registry.active("storyboard_assets", episode_id):
            task_registry.spawn(
                "storyboard_assets",
                episode_id,
                _prepare_storyboard_assets_background(episode_id),
                project_id=ep["project_id"],
            )
        _preflight_event(
            "STORYBOARD_ASSETS_SCHEDULED",
            "人物与场景资产已并行准备；分镜文本立即继续",
            payload={"blocking": False},
        )

        # 恢复旧 checkpoint 时先把模型产生的引号漂移/拼接式证据收敛为授权原文中的
        # 连续片段。严格匹配不足的内容保持未解决，仍由确认门禁拦截。
        if resume and not published_storyboard_baseline:
            from app.storyboard_workspace import repair_generated_source_bindings

            evidence_repair = repair_generated_source_bindings(episode_id)
            if evidence_repair["bound"]:
                _preflight_event(
                    "STORYBOARD_SOURCE_EVIDENCE_REALIGNED",
                    f"续跑前修复源文引用：已绑定 {evidence_repair.get('bound', 0)} 条证据",
                    payload=evidence_repair,
                )
                log_provider_call(
                    "storyboard_source_evidence_repair",
                    config.MODEL_TEXT,
                    "SOURCE_EVIDENCE_REALIGNED",
                    None,
                    0,
                    meta={"episode_id": episode_id, **evidence_repair},
                )

        _preflight_event(
            "STORYBOARD_PREFLIGHT_FINISHED",
            "剧本身份合同已就绪，资产异步准备，交由分镜 Supervisor 展开生成",
        )

        # 集级 Supervisor：大纲 → 逐镜 → 整集校验 → 修复，完成后等待人工确认。
        from app.storyboard_supervisor import run_storyboard_supervisor
        return await run_storyboard_supervisor(
            episode_id,
            resume=resume,
            run_id=run_id,
            preflight_done=True,
            new_activation=new_activation,
        )
    except (StageError, Exception) as exc:  # noqa: BLE001
        # 回滚这次失败尝试自己遗留的未提交写入，必须在任何其他 conn.execute 之前做，
        # 包括紧接着的 errors.log_error() 调用——app.db.insert_error_log 自己也在
        # 这同一个 task 缓存连接（app.db.get_conn() 按 asyncio.current_task() 缓存，
        # 同一个 task 内所有 get_conn() 调用拿到同一个连接对象）上落一条 error_logs
        # 行并 conn.commit()，如果先调用 log_error 再回滚，回滚已经来不及——error_logs
        # 那次 commit 会把此刻这个连接上任何未提交的挂起写入一起提交掉，回滚这一步就
        # 成了马后炮。app.production.storyboard_pack.persist_storyboard_pack 先 DELETE
        # 本集旧 shots（ON DELETE CASCADE 一并删掉 shot_versions——已经生成好的真实
        # 视频记录）再逐段 INSERT 新 shots，整段过程故意不提交，只在函数末尾成功写完
        # 全部段落后 commit 一次，靠"从不中途提交"做到"要么整批换成新的、要么旧的
        # 原封不动"。这里如果不在最前面先回滚，随后不管是 log_error 的隐式 commit
        # 还是下面给 episodes 表写状态后的显式 conn.commit()，都会把这个还没走到
        # persist_storyboard_pack 自己那次 commit 的半成品事务提交下去：旧集已经被
        # 删，新集只写了一部分（甚至一行都没来得及写），这一集就空了——这正是"重新
        # 生成"中途失败导致已产出真实视频被连带清空的根因。回滚只丢弃这次失败尝试
        # 自己产生的未提交写入；本函数在调用 run_storyboard_supervisor 之前的每一步
        # 都已经在各自的检查点上 conn.commit() 过（例如上面的 status='scripting'
        # 那次），回滚不会波及那些已经落盘的状态，也不影响下面重新读到的 saved 计数
        # ——修复后 saved 永远只反映"最后一次真正提交成功"的那份数据。
        if conn.in_transaction:
            conn.rollback()
        rec = errors.log_error(exc, action="storyboard_generate", context={"episode_id": episode_id})
        saved = conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"]
        if run_id:
            run_row = conn.execute(
                "SELECT status FROM workflow_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            owner = conn.execute(
                "SELECT active_storyboard_run_id FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            if (
                run_row
                and (
                    run_row["status"] not in {"CREATED", "RUNNING"}
                    or not owner
                    or owner["active_storyboard_run_id"] != run_id
                )
            ):
                return
        # Supervisor 已把 WAITING_* 写为 scripted+script_error；此处只处理未捕获异常。
        # ``scripting+script_error`` 仍可能只是更早的场景包降级提示，不能据此吞掉
        # 当前异常，否则 Step 会被误记为 SUCCEEDED，Run 却在外层被判为 FAILED，
        # 同时真正异常也会被旧提示遮蔽。
        ep_now = conn.execute("SELECT status, script_error FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if ep_now and ep_now["status"] in {"scripted", "confirmed"} and ep_now["script_error"]:
            return
        if saved:
            try:
                planned = len(
                    json.loads(
                        conn.execute(
                            "SELECT storyboard_outline_json FROM episodes WHERE id=?",
                            (episode_id,),
                        ).fetchone()["storyboard_outline_json"] or "{}"
                    ).get("shots") or []
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                planned = 0
            if planned and saved >= planned:
                note = (
                    f"分镜 {saved}/{planned} 镜已完成，但发布证据校验失败："
                    f"{rec.message}（{rec.code} · {rec.error_id}）"
                )
            else:
                # 修复回滚之后，这里的 saved/rows 一定是"最后一次真正提交成功"的
                # 那份数据，不会再是这次失败尝试留下的半成品——分镜台 2.0.0
                # （episode_prep_pack）路径的持久化本身就是单事务、一次性全写，
                # 已经没有"逐镜追加、可从中间补写"的旧管线语义了（那套按镜头
                # 逐个提交的修复状态机已随 event_chain 驱动的旧分镜管线一起下线，
                # 见 app/storyboard_supervisor.py run_storyboard_supervisor 的
                # 说明）。这条提示不再暗示"接着上次断点补写"，只如实说明当前
                # 保留的是上一次成功持久化的版本，未被本轮失败改动。
                note = (
                    f"分镜生成失败，数据库中的 {saved} 个镜头是上一次成功持久化的"
                    "版本，未被本轮失败改动；可重新生成分镜。"
                    f"本轮失败原因：{rec.message}"
                    f"（{rec.code} · {rec.error_id}）"
                )
            conn.execute("UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                         (note[:800], episode_id))
        else:
            conn.execute("UPDATE episodes SET status='script_failed', script_error=? WHERE id=?",
                         (rec.public, episode_id))
        conn.commit()
        # Persist the recoverable episode projection, then preserve the
        # exception boundary so WorkflowRecorder marks both the step and run
        # as failed. Returning here would falsely record STEP_SUCCEEDED.
        raise
