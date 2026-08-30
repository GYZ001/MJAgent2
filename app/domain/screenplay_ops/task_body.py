"""剧本生成任务体：角色发现、任务体本体、录制器、恢复后录制任务、context pack。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 run_control。
"""
from __future__ import annotations

import asyncio

from app import (
    errors,
    task_registry,
)
from app.db import (
    get_conn,
    get_setting,
    now,
)
from app.domain.common import (
    _episode_source_text,
    _project_bible_or_placeholder,
)
from app.evidence import repository as evidence_repository
from app.harness.context import ContextPack
from app.harness.contracts import get_contract
from app.orchestration.engine import (
    WorkflowRecorder,
    fingerprint,
)
from app.orchestration.state_machine import StateConflict
from app.stages import (
    SCREENPLAY_SOURCE_BUDGET_CHARS,
    StageError,
)

from .run_control import (
    _assert_screenplay_run_owner,
    _project_screenplay_runtime_failure,
)


async def _screenplay_character_discovery(
    episode_id: str,
    source_text: str,
    *,
    draft_text: str = "",
) -> dict:
    """Run the required incremental cast pass for one screenplay generation."""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise StageError("新人物发现", ["剧集不存在"])
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if not project:
        raise StageError("新人物发现", ["项目不存在"])
    _assert_screenplay_run_owner(episode_id)
    if not (project["bible_json"] or "").strip():
        # 剧本允许先于完整人物谱生产，但人物身份不能因此绕过预检。先原子写入
        # 最小骨架，后续仍由既有增量流程建文字卡；bible_status 保持原值，
        # 不把这个骨架伪装成用户已完成的人物谱。
        placeholder = _project_bible_or_placeholder(project)
        conn.execute(
            "UPDATE projects SET bible_json=? "
            "WHERE id=? AND COALESCE(TRIM(bible_json), '')=''",
            (placeholder.model_dump_json(), ep["project_id"]),
        )
        conn.commit()
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
        ).fetchone()
    bible = _project_bible_or_placeholder(project)
    from app.portraits import (
        ensure_cards_for_text,
        persist_screenplay_character_resolutions,
        screenplay_identity_scope_fingerprint,
    )

    # 人物发现是剧本 stage 0，跑在叙事蓝图**之前**，拿不到蓝图那份 paratext
    # 判定，于是会把作者的话里的人名（作者笔名本身）当成出场人物立卡。
    # 这里用同一份判据先净化一次；判不出来就退回原文，绝不挡住人物发现。
    # 只净化**发现用**的文本，剧本链路的 source_text 一个字都不动——
    # 那里需要完整原文做 audit_only 来源审计，删字会让 SRC 段编号错位。
    from app.source_paratext import strip_paratext

    discovery_text = await strip_paratext(
        source_text, operation_id=f"screenplay.discovery.paratext:{episode_id}"
    )
    try:
        result = await ensure_cards_for_text(
            ep["project_id"],
            ep["episode_no"],
            discovery_text,
            bible,
            draft_text=draft_text,
            generate_portraits=False,
            write_guard=lambda: _assert_screenplay_run_owner(episode_id),
        )
    except (StageError, StateConflict):
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成剧本阶段可恢复诊断
        from app.errors import code_ref

        public = code_ref(
            exc,
            action="screenplay_character_discovery",
            context={"episode_id": episode_id, "project_id": ep["project_id"]},
        )
        raise StageError(
            "新人物发现",
            [
                f"人物身份模型暂未完成本集预检，请在剧本阶段重试（{public}）"
                "[IDENTITY_DISCOVERY_FIXED_RETRY_BUDGET]"
            ],
        ) from exc
    if result.get("errors"):
        raise StageError("新人物发现", list(result["errors"]))
    _assert_screenplay_run_owner(episode_id)
    from app.observability.tracing import current_trace

    expected_run_id = current_trace().run_id
    result["resolutions"] = persist_screenplay_character_resolutions(
        conn,
        episode_id,
        result.get("resolutions") or [],
        retire_legacy_future_identity=True,
        expected_active_run_id=expected_run_id,
        replace_identity_scope=screenplay_identity_scope_fingerprint(
            int(ep["episode_no"]), source_text
        ),
    )
    for warning in result.get("warnings") or []:
        errors.log_error(
            None,
            action="screenplay_character_discovery_warning",
            context={
                "project_id": ep["project_id"],
                "episode_id": episode_id,
                "episode_no": ep["episode_no"],
            },
            message=warning,
        )
    return result

async def _screenplay_task(
    episode_id: str,
    *,
    preflight_result: dict | None = None,
) -> dict | None:
    """轻量分集映射包生成（screenplay 契约 6.0.0，episode_prep_pack）。

    替代原先的蓝图→场次分片→编译→修复回路（休眠保留于
    app/production/screenplay_repair.py 等，未从本调用路径引用）：资源发现/
    映射抽取（模型）→ 覆盖/资产确定性核对 → 原子发布，全部逻辑见
    app/production/prep_pack.py。2.0.0 起本模块不再产出事件链——职责收窄为
    映射台（新人物/新场景发现 + 世界书图像素材映射 + 称谓归一），事件链的
    定量职责已转交分镜台，见 app.production.prep_pack 模块 docstring 的
    2.0.0 说明。``preflight_result`` 形参保留仅为兼容旧调用签名
    （recover_screenplay_tasks 等），本流程不消费它。
    """
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    try:
        _assert_screenplay_run_owner(episode_id)
        ep_data = dict(ep)
        source_text = _episode_source_text(conn, ep)
        conn.execute(
            "UPDATE episodes SET screenplay_status=?, screenplay_error=?, screenplay_updated_at=? WHERE id=?",
            ("running", "正在发现新人物/新场景并映射世界书素材", now(), episode_id),
        )
        conn.commit()

        from app.observability.tracing import current_trace

        run_id = None
        try:
            run_id = current_trace().run_id
        except Exception:  # noqa: BLE001
            run_id = None

        from app.production.prep_pack import run_episode_prep_pack
        from app import model_registry
        from app.harness.text_provider_scope import stage_text_provider

        provider_row = conn.execute(
            "SELECT script_text_provider FROM projects WHERE id=?", (ep["project_id"],),
        ).fetchone()
        resolved_text_provider = model_registry.resolve_stage_text_provider(
            provider_row["script_text_provider"] if provider_row else None
        )
        with stage_text_provider(resolved_text_provider):
            payload = await run_episode_prep_pack(
                episode_id=episode_id,
                episode=ep_data,
                source_text=source_text,
                run_id=run_id,
            )
        return payload
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            # 进程热更/停机不是用户取消；保留 running 让新 worker 续跑。
            raise
        from app.observability.tracing import current_trace
        try:
            current_run_id = current_trace().run_id
        except Exception:  # noqa: BLE001
            current_run_id = None
        owner = conn.execute(
            "SELECT active_screenplay_run_id FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if (
            not current_run_id
            or (
                owner is not None
                and owner["active_screenplay_run_id"] == current_run_id
            )
        ):
            # 轻量流程一次性生成到原子发布，中途取消没有可续跑的工作副本
            # （不同于旧修复回路的分片 checkpoint），直接投影为 failed。
            conn.execute(
                "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, "
                "active_screenplay_run_id=NULL, screenplay_updated_at=? WHERE id=?",
                ("剧本生成已取消，可重新发起。", now(), episode_id),
            )
            conn.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        from app.observability.tracing import current_trace
        try:
            current_run_id = current_trace().run_id
        except Exception:  # noqa: BLE001
            current_run_id = None
        owner = conn.execute(
            "SELECT active_screenplay_run_id FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if (
            current_run_id
            and (
                owner is None
                or owner["active_screenplay_run_id"] != current_run_id
            )
        ):
            # 已被恢复任务替代的旧协程可能在 socket 返回后才观察到围栏；
            # 它不得覆盖新运行的剧集状态。
            raise
        public = errors.record_and_format(exc, action="screenplay_generate", context={"episode_id": episode_id})
        _project_screenplay_runtime_failure(
            episode_id,
            run_id=current_run_id,
            public_error=public,
        )
        return None

def _new_screenplay_recorder(
    episode_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> WorkflowRecorder:
    from app import hiagent
    from app.production.screenplay_authority import SCREENPLAY_QA_PROFILE_VERSION
    from app.stages import (
        SCREENPLAY_BASELINE_PROMPT_VERSION,
        SCREENPLAY_STRUCTURAL_BOOTSTRAP_ITERATIONS,
    )

    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError(f"episode not found: {episode_id}")
    project = conn.execute(
        "SELECT bible_version FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    source_text = _episode_source_text(conn, ep)
    active_text_provider = hiagent.active_provider("text")
    active_text_model = hiagent.active_model("text", provider=active_text_provider)
    create_kwargs = dict(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            episode_id,
            ep["source_chapters"],
            source_text,
            project["bible_version"] if project else 0,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={
            "contract": f"screenplay@{get_contract('screenplay').version}",
            "max_iterations": SCREENPLAY_STRUCTURAL_BOOTSTRAP_ITERATIONS,
            "stall_rounds": 2,
            "min_quality_gain": 0.03,
            "baseline_only": True,
            "repair_activation_patch_limit": 12,
            "repair_activation_pass_limit": 32,
        },
        config_snapshot={
            "pipeline_version": "screenplay-compact-ir-pipeline-5.0.0",
            "prompt_version": SCREENPLAY_BASELINE_PROMPT_VERSION,
            "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
            "provider": active_text_provider,
            "model": active_text_model,
            "text_generation_concurrency": (
                get_setting("text_generation_concurrency")
                or "10"
            ),
            "duration_policy": "content_derived_unbounded",
            "blueprint_budget_lineage_fingerprint": fingerprint(
                episode_id,
                ep["source_chapters"],
                source_text,
                project["bible_version"] if project else 0,
            ),
            "blueprint_retry_grant_id": "",
            "blueprint_retry_receipts_hash": "",
        },
        parent_run_id=parent_run_id,
    )
    return _reserve_screenplay_concurrency_slot(conn, episode_id, create_kwargs)


def _reserve_screenplay_concurrency_slot(
    conn, episode_id: str, create_kwargs: dict,
) -> WorkflowRecorder:
    """账号维度并发准入与占位（workflow_runs 行本身）在同一个 BEGIN IMMEDIATE
    事务里完成——消除「先数后建」的 TOCTOU（CLAUDE.md「Gates and Criteria」/
    「Ownership Must Be Explicit」）。照抄 ``media_scheduler.reserve_budget`` 的
    ``owns_transaction`` 惯例，不新造第二套事务风格：``WorkflowRecorder.create()``
    内部的 ``conn.commit()`` 顺带收口本事务，通过后才允许 workflow_runs 出现
    这一行，因此准入判定读到的 ``active_count`` 永远不含"正在被别的并发请求
    创建、尚未提交"的幽灵行——同一账号同一模块的两个并发请求不可能都读到
    "还没到上限"再各自建一行，把上限撑破。"""
    from app import quota

    owner_user_id = quota.owner_of_episode(conn, episode_id)
    owns_transaction = not conn.in_transaction
    try:
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if owner_user_id is not None:
            active = quota.count_active_workflow_runs(
                conn, owner_user_id, "screenplay", exclude_run_id=None,
            )
            quota.check_module_concurrency(
                conn, owner_user_id, quota.MODULE_SCREENPLAY, active_count=active,
            )
        recorder = WorkflowRecorder.create(**create_kwargs)
        if owns_transaction and conn.in_transaction:
            conn.commit()
        return recorder
    except Exception:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise

def _screenplay_context_pack(episode_id: str) -> tuple[list[str], dict]:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    source_text = _episode_source_text(conn, ep)
    bible_artifact = evidence_repository.latest_artifact(
        "character_bible", "project", ep["project_id"]
    )
    mapping_artifact = evidence_repository.latest_artifact(
        "episode_mapping", "project", ep["project_id"]
    )
    from app.production.revision import resolve_screenplay_resume_eligibility

    eligibility = resolve_screenplay_resume_eligibility(
        episode_id,
        conn=conn,
    )
    # Published episode pointers remain populated while an incompatible
    # screenplay is rebuilt. They describe release history, not the input
    # authority of the new Baseline. Only a resolver-approved finalize path
    # may expose a working Document as this step's patch/revalidation input.
    working_artifact_id = (
        eligibility.working_artifact_id
        if eligibility.mode == "finalize"
        else None
    )
    input_ids = [
        artifact_id
        for artifact_id in (
            bible_artifact["id"] if bible_artifact else None,
            mapping_artifact["id"] if mapping_artifact else None,
            working_artifact_id,
        )
        if artifact_id
    ]
    pack = ContextPack(
        goal=f"生成第 {ep['episode_no']} 集可拍剧本",
        metadata={
            "episode_id": episode_id,
            "episode_no": ep["episode_no"],
            "contract_version": get_contract("screenplay").version,
        },
    )
    pack.add_text(
        "source_text",
        source_text,
        limit=SCREENPLAY_SOURCE_BUDGET_CHARS,
        truncation_strategy="head_with_truncation_notice",
    )
    bible_json = project["bible_json"] or "{}"
    pack.add_text(
        "character_bible",
        bible_json,
        limit=max(len(bible_json), 1),
        source_artifact_id=bible_artifact["id"] if bible_artifact else None,
        truncation_strategy="none",
    )
    return list(dict.fromkeys(input_ids)), pack.manifest()

async def _recorded_screenplay_task(
    episode_id: str,
    recorder: WorkflowRecorder,
) -> dict | None:
    async def operation(preflight: dict) -> dict:
        generated = await _screenplay_task(episode_id, preflight_result=preflight)
        if generated is None:
            row = get_conn().execute(
                "SELECT screenplay_error FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            raise RuntimeError(row["screenplay_error"] if row else "剧本任务未产生结果")
        return generated

    try:
        recorder.start()
        discovery_conn = get_conn()
        discovery_episode = discovery_conn.execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        discovery_source = _episode_source_text(
            discovery_conn,
            discovery_episode,
        )
        # 轻量 episode_prep_pack 流程（screenplay 契约 6.0.0）不做全章人物预检：
        # 出场角色/场景在 app/production/prep_pack.py 内对 character_portraits /
        # scene_references 做确定性解析，不需要旧重型改编管线的姓名决议预检。
        # preflight 保留为空 dict 只为兼容 operation() 的签名。
        preflight: dict = {}
        evidence_repository.append_event(
            recorder.run_id,
            "CHARACTER_DISCOVERY_SKIPPED",
            "info",
            "轻量分集准备流程使用确定性资产映射，跳过全章人物预检模型调用",
            payload={"episode_id": episode_id},
        )
        _assert_screenplay_run_owner(episode_id, run_id=recorder.run_id)
        # Discovery may advance bible_version. Refresh the persisted fingerprint and
        # context pack before the screenplay step so evidence describes the inputs
        # actually used by generation.
        fingerprint_ep = get_conn().execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        fingerprint_project = get_conn().execute(
            "SELECT bible_version FROM projects WHERE id=?", (fingerprint_ep["project_id"],)
        ).fetchone()
        get_conn().execute(
            "UPDATE workflow_runs SET input_fingerprint=?, updated_at=? WHERE id=?",
            (
                fingerprint(
                    episode_id,
                    fingerprint_ep["source_chapters"],
                    discovery_source,
                    fingerprint_project["bible_version"] if fingerprint_project else 0,
                ),
                now(),
                recorder.run_id,
            ),
        )
        get_conn().commit()
        input_artifact_ids, context_manifest = _screenplay_context_pack(episode_id)
        _, script = await recorder.step(
            "screenplay_document",
            lambda: operation(preflight),
            contract_key="screenplay",
            agent_name="screenplay_agent_loop",
            input_artifact_ids=input_artifact_ids,
            context_manifest=context_manifest,
        )
        row = get_conn().execute(
            "SELECT screenplay_status, screenplay_error FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not row:
            raise RuntimeError("剧本任务完成后剧集记录不存在")
        if row["screenplay_status"] == "ready":
            recorder.succeed("剧本已通过完成凭证并发布", conn=None)
        elif row["screenplay_status"] == "repairing":
            recorder.partial(row["screenplay_error"] or "剧本自动修复中/等待续跑", conn=None)
        else:
            recorder.succeed("剧本任务结束", conn=None)
        return script
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，剧本运行等待自动续跑", conn=None)
        else:
            recorder.cancel("剧本生成已取消", conn=None)
        raise
    except StateConflict:
        # 旧运行已被新的恢复运行围栏；不再回写剧集，也不把这种协调竞态报成内容失败。
        return None
    except Exception as exc:  # noqa: BLE001 -- failure is persisted for Run Center
        from app.production.screenplay_repair import ScreenplayNarrativeGateError

        if isinstance(exc, ScreenplayNarrativeGateError):
            errors.log_error(
                exc,
                action="screenplay_repair",
                context={"episode_id": episode_id, "phase": "narrative_gate"},
            )
            try:
                recorder.fail(exc, conn=None)
            except StateConflict:
                pass
            return None
        row = get_conn().execute(
            "SELECT screenplay_status, screenplay_error,active_screenplay_run_id "
            "FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not row or row["active_screenplay_run_id"] != recorder.run_id:
            # A remote cancellation or a replacement run owns the terminal
            # projection now.  The stale worker may only observe the conflict.
            return None
        if row and row["screenplay_status"] == "running":
            public = errors.record_and_format(
                exc,
                action="screenplay_generate",
                context={"episode_id": episode_id, "phase": "character_discovery"},
            )
            get_conn().execute(
                "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
                (public, now(), episode_id),
            )
            get_conn().commit()
        elif row and row["screenplay_status"] == "repairing":
            if str(row["screenplay_error"] or "").startswith("WAITING_INPUT"):
                try:
                    recorder.partial(row["screenplay_error"], conn=None)
                except StateConflict:
                    pass
                return None
            public = errors.record_and_format(
                exc,
                action="screenplay_repair",
                context={"episode_id": episode_id},
            )
            get_conn().execute(
                "UPDATE episodes SET screenplay_error=?, screenplay_updated_at=? WHERE id=?",
                (
                    f"剧本后续阶段已暂停，工作副本已保留，可继续流程。{public}",
                    now(),
                    episode_id,
                ),
            )
            get_conn().commit()
        try:
            recorder.fail(exc, conn=None)
        except StateConflict:
            return None
        return None
