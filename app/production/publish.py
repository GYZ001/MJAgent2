"""原子发布：Working → Published，消费完成凭证。"""
from __future__ import annotations

import json
from typing import Any

from app import config
from app.db import get_conn, log_provider_call, now
from app.production.certificate import (
    assert_publish_has_certificate,
    consume_completion_certificate,
    issue_completion_certificate,
    verify_completion_certificate,
)
from app.production.metrics import record_certificate_issued
from app.production.patch import load_screenplay_from_artifact
from app.production.revision import get_production_revision, set_published_artifact
from app.production.structured_issues import blocker_count, must_fix_count
from app.validators import ending_hook_grounding_report


def _screenplay_qa_authority_evidence(
    conn,
    *,
    evaluation_ids: list[str],
    artifact_id: str,
    qa_profile_version: str,
) -> list[dict[str, Any]]:
    if not evaluation_ids:
        return []
    marks = ",".join("?" for _ in evaluation_ids)
    rows = conn.execute(
        f"""SELECT artifact_id,evaluator_type,evaluator_name,evaluator_version,
                   status,hard_gate_passed,evidence_json,evaluation_role,
                   runtime_blocking
              FROM evaluations WHERE id IN ({marks})""",
        evaluation_ids,
    ).fetchall()
    evidence: list[dict[str, Any]] = []
    for row in rows:
        score_only = (
            row["evaluation_role"] == "score_only"
            and not bool(row["runtime_blocking"])
        )
        legacy_runtime_gate = (
            row["evaluation_role"] == "runtime_gate"
            and bool(row["runtime_blocking"])
        )
        if (
            row["artifact_id"] != artifact_id
            or row["evaluator_type"] != "deterministic"
            or row["evaluator_name"] != "screenplay_production_qa"
            or row["evaluator_version"] != qa_profile_version
            or row["status"] != "passed"
            or not bool(row["hard_gate_passed"])
            or not (score_only or legacy_runtime_gate)
        ):
            continue
        try:
            evidence.append(json.loads(row["evidence_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence.append({})
    return evidence


def publish_screenplay(
    *,
    episode_id: str,
    revision_id: str,
    artifact_id: str,
    artifact_hash: str,
    evaluation_ids: list[str],
    input_fingerprint: str = "",
    contract_version: str = "",
    qa_profile_version: str = "",
    clear_downstream: bool = True,
) -> dict[str, Any]:
    """签发凭证并原子发布剧本到页面投影。内部修复不得调用此函数的下游清空以外路径。"""
    rev = get_production_revision(revision_id)
    if rev is None:
        raise ValueError("production revision 不存在")
    if rev.working_artifact_id != artifact_id:
        raise ValueError("只能发布当前 working Artifact")

    script = load_screenplay_from_artifact(artifact_id)
    conn = get_conn()
    from app.production.screenplay_authority import (
        screenplay_contract_requires_narrative,
    )

    effective_contract_version = contract_version or rev.contract_version
    if (
        screenplay_contract_requires_narrative(effective_contract_version)
        and script.narrative_plan is None
    ):
        raise ValueError("当前剧本合同要求 narrative_plan，禁止降级发布 legacy 剧本")
    if script.narrative_plan is not None:
        from app.production.screenplay_authority import (
            screenplay_authority_fingerprint,
        )

        authority_fingerprint = screenplay_authority_fingerprint(
            episode_id,
            conn=conn,
            contract_version=effective_contract_version,
            qa_profile_version=qa_profile_version or rev.qa_profile_version,
        )
        if (input_fingerprint or rev.input_fingerprint) != authority_fingerprint:
            raise ValueError("剧本 revision 未绑定当前原文/Bible/人物决议/改编约束指纹")
        if not evaluation_ids:
            raise ValueError("叙事剧本发布缺少当前 QA Evaluation")
        authority_evidence = _screenplay_qa_authority_evidence(
            conn,
            evaluation_ids=evaluation_ids,
            artifact_id=artifact_id,
            qa_profile_version=qa_profile_version or rev.qa_profile_version,
        )
        if len(authority_evidence) != 1 or authority_evidence[0].get(
            "authority_input_fingerprint"
        ) != authority_fingerprint:
            raise ValueError("剧本质量评分未精确绑定当前权威输入指纹")

    artifact = conn.execute(
        "SELECT status,type FROM artifacts WHERE id=?",
        (artifact_id,),
    ).fetchone()
    if artifact is None:
        raise ValueError("待发布 working Artifact 不存在")
    if artifact["type"] != "screenplay_document":
        raise ValueError("只能发布完整 screenplay_document，禁止发布 IR/scene shard")
    from app.evidence import repository as evidence_repository
    from app.production.screenplay_authority import (
        assert_screenplay_matches_validated_v7_source,
    )

    artifact_record = evidence_repository.get_artifact(
        artifact_id,
        conn=conn,
    )
    if artifact_record is None:
        raise ValueError("待发布 working Artifact 不存在")
    assert_screenplay_matches_validated_v7_source(
        episode_id=episode_id,
        artifact=artifact_record,
        screenplay=script,
        conn=conn,
    )
    lineage = evidence_repository.get_lineage(artifact_id)
    ancestor_types = {
        str(item.get("type") or "")
        for item in lineage.get("ancestors") or []
    }
    if (
        ancestor_types.intersection({
            "screenplay_envelope", "screenplay_scene_shard",
            "screenplay_scene_shard_plan",
        })
        and "screenplay_generation_ir_merged" not in ancestor_types
    ):
        raise ValueError("Scene Shard 生成的完整 Document 缺少 merged IR 血缘")
    merged_ancestors = [
        item for item in lineage.get("ancestors") or []
        if item.get("type") == "screenplay_generation_ir_merged"
    ]
    if merged_ancestors:
        by_id = {
            str(item.get("id") or ""): item
            for item in lineage.get("ancestors") or []
        }
        merged = merged_ancestors[0]
        direct_parents = [
            by_id[parent_id]
            for parent_id in merged.get("parent_artifact_ids") or []
            if parent_id in by_id
        ]
        direct_types = {str(item.get("type") or "") for item in direct_parents}
        required_types = {
            "screenplay_narrative_blueprint",
            "screenplay_identity_registry",
            "screenplay_envelope",
            "screenplay_scene_shard",
        }
        missing_lineage = sorted(required_types - direct_types)
        if missing_lineage:
            raise ValueError(
                "merged IR 缺少直接父级：" + "、".join(missing_lineage)
            )
        if any(
            item.get("type") in required_types
            and item.get("status") not in {"validated", "approved"}
            for item in direct_parents
        ):
            raise ValueError("merged IR 只能引用 validated 上游 Artifact")
    projection_json = script.model_dump_json()
    if conn.in_transaction:
        raise RuntimeError("剧本发布前存在未收口事务")
    cleanup_outbox_id: str | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        current_revision = conn.execute(
            """SELECT status,working_artifact_id,input_fingerprint,
                      contract_version,qa_profile_version
                 FROM production_revisions WHERE id=?""",
            (revision_id,),
        ).fetchone()
        if (
            current_revision is None
            or current_revision["status"] != "active"
            or current_revision["working_artifact_id"] != artifact_id
        ):
            raise ValueError("待发布 production revision 已失效")
        current_artifact = conn.execute(
            "SELECT status,type FROM artifacts WHERE id=?",
            (artifact_id,),
        ).fetchone()
        if current_artifact is None or current_artifact["type"] != "screenplay_document":
            raise ValueError("待发布 working Artifact 已失效")
        original_status = str(current_artifact["status"] or "")
        if original_status not in {"candidate", "working", "validated", "approved"}:
            raise ValueError("待发布 working Artifact 状态不可用")
        if original_status in {"candidate", "working"}:
            cursor = conn.execute(
                "UPDATE artifacts SET status='validated' WHERE id=? AND status=?",
                (artifact_id, original_status),
            )
            if cursor.rowcount != 1:
                raise ValueError("待发布 working Artifact 状态发生冲突")

        cert = issue_completion_certificate(
            kind="screenplay",
            scope_id=episode_id,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            input_fingerprint=input_fingerprint or rev.input_fingerprint,
            contract_version=effective_contract_version,
            qa_profile_version=qa_profile_version or rev.qa_profile_version,
            evaluation_ids=evaluation_ids,
            blockers=0,
            must_fix_issues=0,
            production_revision_id=revision_id,
            conn=conn,
            commit=False,
        )
        verify_completion_certificate(
            cert,
            expected_artifact_id=artifact_id,
            expected_artifact_hash=artifact_hash,
            expected_input_fingerprint=input_fingerprint or rev.input_fingerprint or None,
            expected_contract_version=effective_contract_version or None,
            conn=conn,
        )
        assert_publish_has_certificate(
            kind="screenplay", episode_id=episode_id, certificate_id=cert.certificate_id,
        )

        previous = conn.execute(
            "SELECT screenplay_artifact_id,project_id,episode_no FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if previous is None:
            raise ValueError("待发布剧集不存在")
        previous_artifact_id = previous["screenplay_artifact_id"]
        # 在发布事务中先切断下游权威指针。旧镜头/文件的物理清理必须在
        # 新剧本提交后执行；否则发布后半段失败会同时丢失旧下游和新发布。
        if clear_downstream:
            from app.artifacts import stage_episode_artifact_cleanup

            cleanup = stage_episode_artifact_cleanup(conn, episode_id)
            cleanup_outbox_id = str(cleanup["outbox_id"])
            conn.execute(
                """UPDATE episodes SET storyboard_outline_json=NULL, storyboard_artifact_id=NULL,
                          storyboard_warning=NULL, published_storyboard_artifact_id=NULL,
                          working_storyboard_artifact_id=NULL, active_storyboard_run_id=NULL,
                          storyboard_production_revision_id=NULL,
                          storyboard_completion_certificate_id=NULL,
                          active_video_run_id=NULL, video_control_json=NULL,
                          delivery_artifact_id=NULL, delivery_status='not_ready'
                    WHERE id=?""",
                (episode_id,),
            )
            from app.storyboard_authority import (
                clear_storyboard_outline_authority,
            )

            clear_storyboard_outline_authority(
                episode_id,
                conn=conn,
            )

        # Downstream retirement can legitimately normalize mutable episode
        # authority fields (notably storyboard-owned duration).  Recompute only
        # after those writes while holding the SQLite writer lock.  The QA,
        # revision and certificate must all bind this exact post-cleanup value;
        # otherwise rolling back is safer than publishing a certificate that
        # is invalid the instant it commits.
        if script.narrative_plan is not None:
            post_cleanup_fingerprint = screenplay_authority_fingerprint(
                episode_id,
                conn=conn,
                contract_version=effective_contract_version,
                qa_profile_version=(
                    qa_profile_version or current_revision["qa_profile_version"]
                ),
            )
            expected_fingerprint = str(
                input_fingerprint or current_revision["input_fingerprint"] or ""
            )
            if (
                post_cleanup_fingerprint != expected_fingerprint
                or str(current_revision["input_fingerprint"] or "")
                != post_cleanup_fingerprint
                or str(current_revision["contract_version"] or "")
                != effective_contract_version
                or str(current_revision["qa_profile_version"] or "")
                != (qa_profile_version or rev.qa_profile_version)
            ):
                raise ValueError(
                    "剧本发布事务中权威输入已变化，必须重新 QA 与签证",
                )
            locked_evidence = _screenplay_qa_authority_evidence(
                conn,
                evaluation_ids=evaluation_ids,
                artifact_id=artifact_id,
                qa_profile_version=(
                    qa_profile_version or current_revision["qa_profile_version"]
                ),
            )
            if (
                len(locked_evidence) != 1
                or locked_evidence[0].get("authority_input_fingerprint")
                != post_cleanup_fingerprint
            ):
                raise ValueError("剧本质量评分未绑定发布事务的最终权威指纹")

        conn.execute(
            "UPDATE artifacts SET status='approved', trust_level='T2' WHERE id=?",
            (artifact_id,),
        )
        episode_cursor = conn.execute(
            "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', "
            "screenplay_error=NULL, screenplay_updated_at=?, screenplay_artifact_id=?, "
            "published_screenplay_artifact_id=?, status='planned', script_error=NULL "
            "WHERE id=?",
            (projection_json, now(), artifact_id, artifact_id, episode_id),
        )
        if episode_cursor.rowcount != 1:
            raise ValueError("剧本发布 episode 更新发生冲突")
        # 跨集叙事承接：把本集真实结尾钩子写回 episodes.cliffhanger，并
        # 镜像写入下一集 episodes.hook，供 prev_ending 查询与 prompt 承接文案使用。
        # ending_hook 已在生成/校验链路里经过溯源判定（编造内容会被清空为
        # ""）；此处仍在写入权威锚点前用同一判据复核一次——不同调用路径可能
        # 携带未经这条门禁清洗过的 legacy Artifact，复核不通过就按空钩子处理，
        # 绝不把可疑内容写进 episodes.cliffhanger / 下一集 episodes.hook。
        #
        # 这次复核如果判定不通过，必须留下可查证据——以前是直接清空、不留
        # 痕迹，数据上无法区分"原文真的没钩子"和"被误杀"（app/stages.py 里
        # 两处生成期清空同理，已一并修复）。
        #
        # 注意：log_provider_call() 内部会 conn.commit()——这个函数正处在
        # `BEGIN IMMEDIATE` 开启的原子发布事务里（本函数末尾统一
        # conn.commit()，任何异常都要整体 conn.rollback()）。这里如果直接调用
        # log_provider_call()，它的内部 commit 会把事务提前收口，后续
        # consume_completion_certificate 等步骤一旦失败，rollback 就晚了——
        # 已提交的部分（包括这条 UPDATE episodes 尚未来得及执行的当前状态）
        # 回不去，发布的原子性被破坏。因此这里只记录判定结果，真正的
        # log_provider_call() 调用推迟到事务成功提交之后（见本函数下方
        # "事务已提交，此时才能安全记录观测证据" 处）。
        ending_hook_value = (script.ending_hook or "").strip()
        ending_hook_rejection: dict[str, Any] | None = None
        if ending_hook_value:
            recheck_report = ending_hook_grounding_report(
                ending_hook_value, script.full_script_text, events=script.events,
            )
            if not recheck_report["grounded"]:
                ending_hook_value = ""
                ending_hook_rejection = recheck_report
        conn.execute(
            "UPDATE episodes SET cliffhanger=? WHERE id=?",
            (ending_hook_value, episode_id),
        )
        conn.execute(
            "UPDATE episodes SET hook=? WHERE project_id=? AND episode_no=?",
            (ending_hook_value, previous["project_id"], previous["episode_no"] + 1),
        )
        set_published_artifact(
            revision_id,
            artifact_id,
            certificate_id=cert.certificate_id,
            conn=conn,
            commit=False,
        )
        # Keep the run lease through the authority transition. The revision
        # publish guard verifies the exact owner above; only then may this same
        # transaction release the episode for a later run.
        conn.execute(
            "UPDATE episodes SET active_screenplay_run_id=NULL WHERE id=?",
            (episode_id,),
        )
        consume_completion_certificate(cert.certificate_id, conn=conn, commit=False)
        conn.execute("DELETE FROM screenplay_drafts WHERE episode_id=?", (episode_id,))
        if previous_artifact_id and previous_artifact_id != artifact_id:
            evidence_repository.invalidate_descendants(
                previous_artifact_id,
                f"上游剧本已由 {artifact_id} 替代",
                exclude_ids={artifact_id},
                conn=conn,
                commit=False,
            )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    # 事务已提交，此时才能安全记录观测证据：log_provider_call() 自带
    # conn.commit()，必须放在原子发布事务收口之后调用，否则会把事务提前
    # 收口、破坏发布失败时的整体回滚（见上方判定处的注释）。事务已经成功
    # 提交意味着 episodes.cliffhanger/hook 写入的就是这个被清空的值，此时
    # 记录的证据与实际落库结果一致。
    if ending_hook_rejection is not None:
        log_provider_call(
            "ending_hook_grounding_rejected",
            config.MODEL_TEXT,
            "REJECTED",
            None,
            0,
            meta={
                "episode_id": episode_id,
                "source": "screenplay_publish_recheck",
                "hook_text": ending_hook_rejection["hook_text"],
                "tier": ending_hook_rejection["tier"],
                "layer1_coverage": ending_hook_rejection["layer1_coverage"],
                "best_event_id": ending_hook_rejection["best_event_id"],
                "best_event_coverage": ending_hook_rejection["best_event_coverage"],
                "window": ending_hook_rejection["window"],
            },
        )
    try:
        record_certificate_issued(
            kind="screenplay",
            episode_id=episode_id,
            certificate_id=cert.certificate_id,
        )
    except Exception:
        pass
    downstream_cleanup_pending = False
    if cleanup_outbox_id:
        try:
            from app.artifacts import flush_media_cleanup_outbox

            downstream_cleanup_pending = not flush_media_cleanup_outbox(
                cleanup_outbox_id
            )
        except Exception:  # noqa: BLE001 - startup recovery owns the durable row
            downstream_cleanup_pending = True
    result = {
        "episode_id": episode_id,
        "artifact_id": artifact_id,
        "certificate_id": cert.certificate_id,
        "status": "ready",
    }
    if downstream_cleanup_pending:
        result["downstream_cleanup_pending"] = True
    return result


def publish_storyboard(
    *,
    episode_id: str,
    revision_id: str,
    artifact_id: str,
    artifact_hash: str,
    evaluation_ids: list[str],
    shots_payload: list[dict[str, Any]],
    outline_json: str | None = None,
    input_fingerprint: str = "",
    contract_version: str = "",
    qa_profile_version: str = "",
) -> dict[str, Any]:
    """整集分镜原子发布到正式 shots 表。"""
    rev = get_production_revision(revision_id)
    if rev is None:
        raise ValueError("production revision 不存在")
    if rev.working_artifact_id != artifact_id:
        raise ValueError("只能发布当前 working Artifact")
    planned_total = 0
    if outline_json:
        try:
            planned_total = len(json.loads(outline_json).get("shots") or [])
        except (TypeError, ValueError, json.JSONDecodeError):
            planned_total = 0
    if not shots_payload:
        raise ValueError("没有任何分镜产物可发布")
    if planned_total and len(shots_payload) != planned_total:
        raise ValueError(f"分镜数量与计划不同：已完成 {len(shots_payload)}/{planned_total} 镜")
    if not bool(shots_payload[-1].get("is_final")):
        raise ValueError("最终镜未标记收束，禁止发布未结束的分镜")

    conn = get_conn()
    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if episode_row is None:
        raise ValueError("待发布分镜所属剧集不存在")
    from app.evidence import repository as evidence_repository
    from app.schemas import Storyboard

    board = Storyboard.model_validate({
        "episode_no": int(episode_row["episode_no"]),
        "shots": shots_payload,
    })
    board_artifact = evidence_repository.get_artifact(artifact_id)
    if (
        board_artifact is None
        or board_artifact.get("type") not in {"storyboard", "storyboard_document"}
        or board_artifact.get("scope_type") != "episode"
        or board_artifact.get("scope_id") != episode_id
        or board_artifact.get("status")
        in {"stale", "rejected", "superseded", "needs_revision"}
    ):
        raise ValueError("待发布分镜 Artifact 类型、范围或状态无效")
    try:
        artifact_board = Storyboard.model_validate(board_artifact.get("content"))
    except Exception as exc:  # noqa: BLE001 - immutable artifact boundary
        raise ValueError(f"待发布分镜 Artifact 内容无法解析：{exc}") from exc
    if artifact_board.model_dump(mode="json") != board.model_dump(mode="json"):
        raise ValueError("待发布 shots_payload 与完成凭证绑定的 Artifact 内容不一致")

    if conn.in_transaction:
        raise RuntimeError("分镜发布前存在未收口事务")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current_revision = conn.execute(
            """SELECT episode_id,kind,status,working_artifact_id,published_artifact_id,
                      input_fingerprint,contract_version,qa_profile_version
                 FROM production_revisions WHERE id=?""",
            (revision_id,),
        ).fetchone()
        if (
            current_revision is None
            or current_revision["episode_id"] != episode_id
            or current_revision["kind"] != "storyboard"
            or current_revision["working_artifact_id"] != artifact_id
        ):
            raise ValueError("待发布 storyboard production revision 已失效")
        effective_input_fingerprint = (
            input_fingerprint or current_revision["input_fingerprint"]
        )
        effective_contract_version = (
            contract_version or current_revision["contract_version"]
        )
        effective_qa_profile_version = (
            qa_profile_version or current_revision["qa_profile_version"]
        )
        if (
            input_fingerprint
            and current_revision["input_fingerprint"]
            and input_fingerprint != current_revision["input_fingerprint"]
        ):
            raise ValueError("发布 input_fingerprint 与 production revision 不匹配")
        if (
            contract_version
            and current_revision["contract_version"]
            and contract_version != current_revision["contract_version"]
        ):
            raise ValueError("发布 contract_version 与 production revision 不匹配")
        if (
            qa_profile_version
            and current_revision["qa_profile_version"]
            and qa_profile_version != current_revision["qa_profile_version"]
        ):
            raise ValueError("发布 qa_profile_version 与 production revision 不匹配")

        current_episode = conn.execute(
            "SELECT * FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if current_episode is None:
            raise ValueError("待发布分镜所属剧集不存在")
        from app.storyboard_workspace import (
            assert_storyboard_source_bindings_complete,
        )

        assert_storyboard_source_bindings_complete(
            episode_id,
            conn=conn,
        )
        narrative_authority = False
        if current_episode["screenplay_json"]:
            from app.production.screenplay_authority import (
                resolve_downstream_screenplay,
            )

            screenplay_context = resolve_downstream_screenplay(
                episode_id,
                conn=conn,
            )
            narrative_authority = screenplay_context.narrative_authority_required
        if narrative_authority and not outline_json:
            raise ValueError("叙事分镜发布缺少已版本化 storyboard outline")

        if current_revision["status"] == "published":
            if current_revision["published_artifact_id"] != artifact_id:
                raise ValueError("已发布 storyboard production revision 权威漂移")
            if (
                current_episode["storyboard_artifact_id"] != artifact_id
                or current_episode["published_storyboard_artifact_id"] != artifact_id
                or current_episode["working_storyboard_artifact_id"] != artifact_id
                or current_episode["storyboard_production_revision_id"] != revision_id
                or not current_episode["storyboard_completion_certificate_id"]
            ):
                raise ValueError("已发布 storyboard episode 权威链漂移")
            published_artifact = conn.execute(
                "SELECT status FROM artifacts WHERE id=?",
                (artifact_id,),
            ).fetchone()
            if published_artifact is None or published_artifact["status"] != "approved":
                raise ValueError("已发布 storyboard Artifact 状态漂移")
            cert = verify_completion_certificate(
                str(current_episode["storyboard_completion_certificate_id"]),
                expected_artifact_id=artifact_id,
                expected_artifact_hash=artifact_hash,
                expected_input_fingerprint=effective_input_fingerprint or None,
                expected_contract_version=effective_contract_version or None,
                expected_qa_profile_version=effective_qa_profile_version or None,
                expected_kind="storyboard",
                expected_scope_id=episode_id,
                expected_production_revision_id=revision_id,
                allow_consumed=True,
                conn=conn,
            )
            if (
                cert.consumed_at is None
                or cert.evaluation_ids != list(evaluation_ids)
                or cert.input_fingerprint != effective_input_fingerprint
                or cert.contract_version != effective_contract_version
                or cert.qa_profile_version != effective_qa_profile_version
            ):
                raise ValueError("已发布 storyboard 完成凭证与重试请求不匹配")
            if outline_json:
                from app.storyboard_authority import (
                    assert_storyboard_matches_outline_authority,
                    outline_fingerprint,
                    resolve_storyboard_outline_authority,
                )

                outline_authority = resolve_storyboard_outline_authority(
                    episode_id,
                    conn=conn,
                )
                if outline_fingerprint(outline_json) != outline_authority.fingerprint:
                    raise ValueError(
                        "已发布 outline JSON 不是当前权威 revision/fingerprint"
                    )
                assert_storyboard_matches_outline_authority(
                    outline_authority,
                    board,
                )
            published_shots = conn.execute(
                "SELECT shot_no,duration_s FROM shots "
                "WHERE episode_id=? ORDER BY shot_no",
                (episode_id,),
            ).fetchall()
            if len(published_shots) != len(board.shots) or any(
                int(row["shot_no"]) != int(shot.shot_no)
                or int(row["duration_s"] or 0) != int(shot.duration_s or 0)
                for row, shot in zip(published_shots, board.shots)
            ):
                raise ValueError("已发布 shots 投影与重试请求不匹配")
            conn.commit()
            return {
                "episode_id": episode_id,
                "artifact_id": artifact_id,
                "certificate_id": cert.certificate_id,
                "shot_count": len(shots_payload),
                "status": "scripted",
            }
        if current_revision["status"] != "active":
            raise ValueError("待发布 storyboard production revision 已失效")

        outline_authority = None
        if narrative_authority:
            from app.storyboard_authority import (
                assert_storyboard_matches_outline_authority,
                outline_fingerprint,
                resolve_storyboard_outline_authority,
            )

            outline_authority = resolve_storyboard_outline_authority(
                episode_id,
                conn=conn,
            )
            if outline_fingerprint(outline_json) != outline_authority.fingerprint:
                raise ValueError(
                    "待发布 outline JSON 不是当前权威 revision/fingerprint"
                )
            assert_storyboard_matches_outline_authority(
                outline_authority,
                board,
            )
        elif outline_json:
            from app.storyboard_authority import (
                assert_storyboard_matches_outline_authority,
                persist_storyboard_outline_authority,
            )

            outline_authority = persist_storyboard_outline_authority(
                episode_id,
                outline_json,
                conn=conn,
                commit=False,
            )
            assert_storyboard_matches_outline_authority(
                outline_authority,
                board,
            )

        cert = issue_completion_certificate(
            kind="storyboard",
            scope_id=episode_id,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            input_fingerprint=effective_input_fingerprint,
            contract_version=effective_contract_version,
            qa_profile_version=effective_qa_profile_version,
            evaluation_ids=evaluation_ids,
            blockers=0,
            must_fix_issues=0,
            production_revision_id=revision_id,
            conn=conn,
            commit=False,
        )
        verify_completion_certificate(
            cert,
            expected_artifact_id=artifact_id,
            expected_artifact_hash=artifact_hash,
            expected_input_fingerprint=effective_input_fingerprint or None,
            expected_contract_version=effective_contract_version or None,
            expected_qa_profile_version=effective_qa_profile_version or None,
            expected_kind="storyboard",
            expected_scope_id=episode_id,
            expected_production_revision_id=revision_id,
            conn=conn,
        )
        assert_publish_has_certificate(
            kind="storyboard",
            episode_id=episode_id,
            certificate_id=cert.certificate_id,
        )
        artifact_cursor = conn.execute(
            """UPDATE artifacts
                  SET status='approved',trust_level='T2',
                      approved_at=COALESCE(approved_at,?)
                WHERE id=?""",
            (now(), artifact_id),
        )
        if artifact_cursor.rowcount != 1:
            raise ValueError("待发布 storyboard Artifact 批准发生冲突")

        # 正式投影、outline authority、证书和发布指针共用一个事务。
        shot_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        if len(shot_rows) != len(board.shots):
            raise ValueError("正式 shots 行数与待发布 Storyboard Artifact 不一致")
        from app.storyboard_supervisor import _write_shot_fields

        for row, shot in zip(shot_rows, board.shots):
            _write_shot_fields(
                conn,
                str(row["id"]),
                shot,
                row["storyboard_artifact_id"],
                narrative_authority=narrative_authority,
            )

        # 冷观众审读/一次观看校准（narrative_review/narrative_calibration）已
        # 整体下线（用户拍板）：这里曾经按是否拿到已验证的审读+校准结果分两
        # 支写 narrative_status，但两个局部变量在删除前就从未被真正赋值过
        # （历史遗留——没有任何调用方补上产出它们的那一步），narrative_
        # authority=True 分支已经是永远不可达的死分支，删除不改变任何当前
        # 可达路径的行为。
        episode_cursor = conn.execute(
            """UPDATE episodes
                  SET status='scripted', script_error=NULL, storyboard_warning=NULL,
                      storyboard_artifact_id=?
                WHERE id=?""",
            (artifact_id, episode_id),
        )
        if episode_cursor.rowcount != 1:
            raise ValueError("分镜发布 episode 更新发生冲突")
        set_published_artifact(
            revision_id,
            artifact_id,
            certificate_id=cert.certificate_id,
            conn=conn,
            commit=False,
        )
        consume_completion_certificate(
            cert.certificate_id,
            conn=conn,
            commit=False,
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {
        "episode_id": episode_id,
        "artifact_id": artifact_id,
        "certificate_id": cert.certificate_id,
        "shot_count": len(shots_payload),
        "status": "scripted",
    }


def can_issue_certificate(issues: list) -> bool:
    """QA 是只读门禁：任何 blocker / must-fix 都必须先由 Repair 生成新候选。"""
    return blocker_count(issues) == 0 and must_fix_count(issues) == 0
