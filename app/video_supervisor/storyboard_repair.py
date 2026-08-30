"""分镜语义修复提案的生成、校验与落地改写。"""
from __future__ import annotations

import json

from app.completion_grant import GrantValidationError, VideoCompletionGrant
from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.schemas import Shot, Storyboard
from app.video_repair_router import VideoRepairPlan

from .authority import _verify_supervisor_paid_authority
from .models import (
    ShotCoverageEntry,
    StoryboardRepairAffectedAuthority,
    StoryboardRepairProposal,
    VideoSupervisorCheckpoint,
)



def _repair_authority_ids(shots: list[Shot]) -> StoryboardRepairAffectedAuthority:
    action_ids: set[str] = set()
    event_ids: set[str] = set()
    phase_ids: set[str] = set()
    experience_ids: set[str] = set()
    for shot in shots:
        action_ids.update(
            value
            for value in [shot.primary_action_id, *(shot.supporting_action_ids or [])]
            if value
        )
        event_ids.update(value for value in (shot.event_ids or []) if value)
        phase_ids.update(value for value in (shot.action_phase_ids or []) if value)
        if shot.shot_contribution:
            experience_ids.update(
                value
                for value in shot.shot_contribution.experience_intent_ids
                if value
            )
    return StoryboardRepairAffectedAuthority(
        action_ids=sorted(action_ids),
        event_ids=sorted(event_ids),
        action_phase_ids=sorted(phase_ids),
        experience_intent_ids=sorted(experience_ids),
    )


def _candidate_storyboard_from_repair(
    board: Storyboard,
    *,
    shot_index: int,
    proposal: StoryboardRepairProposal,
) -> Storyboard:
    shots = [shot.model_copy(deep=True) for shot in board.shots]
    replacements = [shot.model_copy(deep=True) for shot in proposal.candidate_shots]
    shots[shot_index : shot_index + 1] = replacements
    # shot_no is a display/order projection. Structural insertion shifts later
    # numbers but never edits authored state, action or audience contracts.
    for index, shot in enumerate(shots, start=1):
        shot.shot_no = index
    return Storyboard(episode_no=board.episode_no, shots=shots)


def _validate_storyboard_repair_proposal(
    proposal: StoryboardRepairProposal,
    *,
    board: Storyboard,
    shot_index: int,
    database_shot_id: str,
    screenplay,
    bible,
    target_duration_s: int,
    episode_id: str,
) -> tuple[Storyboard | None, list[str]]:
    errors: list[str] = []
    original = board.shots[shot_index]
    original_authority_id = (
        str(original.shot_id or "").strip() or database_shot_id
    )
    if proposal.base_shot_id != database_shot_id:
        errors.append("base_shot_id 必须精确绑定当前数据库 Shot")
    expected_count = 1 if proposal.operation == "replace" else 2
    if len(proposal.candidate_shots) != expected_count:
        errors.append(f"{proposal.operation} 操作必须输出 {expected_count} 个完整 Shot")
    candidate_ids = [str(shot.shot_id or "").strip() for shot in proposal.candidate_shots]
    if candidate_ids and candidate_ids[0] != original_authority_id:
        errors.append("候选首镜必须保留被修复 Shot 的权威 ID")
    if any(not value for value in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids):
        errors.append("候选 Shot 必须具有非空且不重复的稳定 ID")
    first_no = int(original.shot_no)
    if [shot.shot_no for shot in proposal.candidate_shots] != list(
        range(first_no, first_no + expected_count)
    ):
        errors.append("候选 Shot 的 shot_no 必须从被修复镜头开始连续排列")
    proposed_duration = sum(int(shot.duration_s or 0) for shot in proposal.candidate_shots)
    if proposed_duration != proposal.expected_total_duration_s:
        errors.append("预计时长必须等于所有候选 Shot 的时长之和")

    expected_affected = _repair_authority_ids([original, *proposal.candidate_shots])
    for field in (
        "action_ids",
        "event_ids",
        "action_phase_ids",
        "experience_intent_ids",
    ):
        proposed = list(getattr(proposal.affected_authority, field))
        expected = list(getattr(expected_affected, field))
        if len(proposed) != len(set(proposed)) or set(proposed) != set(expected):
            errors.append(
                "affected_authority 必须完整列出原镜头与候选镜头触及的 "
                f"{field}"
            )

    narrative_plan = screenplay.narrative_plan if screenplay is not None else None
    if narrative_plan is not None:
        known_actions = {item.action_id for item in narrative_plan.atomic_actions}
        known_events = {item.event_id for item in narrative_plan.events}
        known_phases = {
            phase.phase_id
            for action in narrative_plan.atomic_actions
            for phase in action.temporal_phases
        }
        known_experiences = {
            item.experience_intent_id for item in narrative_plan.experience_intents
        }
        for values, known, label in (
            (proposal.affected_authority.action_ids, known_actions, "action"),
            (proposal.affected_authority.event_ids, known_events, "event"),
            (proposal.affected_authority.action_phase_ids, known_phases, "phase"),
            (
                proposal.affected_authority.experience_intent_ids,
                known_experiences,
                "experience",
            ),
        ):
            missing = sorted(set(values) - known)
            if missing:
                errors.append(f"affected_authority 含不存在的 {label} IDs: {missing}")

    candidate_board: Storyboard | None = None
    if not errors:
        candidate_board = _candidate_storyboard_from_repair(
            board,
            shot_index=shot_index,
            proposal=proposal,
        )
        from app.validators import (
            validate_storyboard,
            validate_storyboard_continuity_contract,
            validate_storyboard_preserves_key_content,
            validate_storyboard_soundtrack,
        )

        errors.extend(validate_storyboard(
            candidate_board,
            bible,
            target_duration_s,
            narrative_authority=narrative_plan is not None,
            narrative_plan=narrative_plan,
            screenplay=screenplay,
        ))
        errors.extend(validate_storyboard_continuity_contract(
            candidate_board,
            screenplay,
        ))
        if screenplay is not None:
            errors.extend(validate_storyboard_soundtrack(
                candidate_board,
                screenplay,
                target_duration_s,
            ))
            errors.extend(validate_storyboard_preserves_key_content(
                candidate_board,
                screenplay,
            ))
        if narrative_plan is not None:
            from app.narrative import validate_storyboard_narrative

            errors.extend(validate_storyboard_narrative(
                candidate_board,
                screenplay,
                complete=True,
                expected_scope_id=episode_id,
            ))
    return candidate_board, list(dict.fromkeys(errors))


async def _semantic_storyboard_repair_proposal(
    *,
    board: Storyboard,
    shot_index: int,
    database_shot_id: str,
    screenplay,
    bible,
    target_duration_s: int,
    episode_id: str,
    repair_plan: VideoRepairPlan | None,
    observed_issue_codes: list[str],
    authority_checkpoint: VideoSupervisorCheckpoint | None = None,
) -> tuple[StoryboardRepairProposal, Storyboard]:
    from app.harness import model_gateway

    current = board.shots[shot_index]
    prompt = {
        "task": (
            "As a continuity director, infer the semantic cause of this failed shot and "
            "propose either one complete replacement Shot or a two-Shot split. "
            "Do not edit any unrelated shot and do not select a repair from issue wording."
        ),
        "episode_id": episode_id,
        "database_shot_id": database_shot_id,
        "repair_evidence": {
            "structured_issue_codes": observed_issue_codes,
            "router_reason": repair_plan.reason if repair_plan else None,
            "router_issue_evidence": (
                repair_plan.model_dump(mode="json") if repair_plan else None
            ),
        },
        "current_shot_index": shot_index,
        "current_shot": current.model_dump(mode="json"),
        "previous_shot": (
            board.shots[shot_index - 1].model_dump(mode="json")
            if shot_index > 0 else None
        ),
        "next_shot": (
            board.shots[shot_index + 1].model_dump(mode="json")
            if shot_index + 1 < len(board.shots) else None
        ),
        "complete_storyboard": board.model_dump(mode="json"),
        "screenplay_authority": (
            screenplay.model_dump(mode="json") if screenplay is not None else None
        ),
        "bible": bible.model_dump(mode="json"),
        "target_duration_s": target_duration_s,
        "output_schema": StoryboardRepairProposal.model_json_schema(),
        "constraints": [
            "candidate_shots are complete Shot objects, not patches",
            "replace returns exactly one candidate; split returns exactly two",
            "the first candidate preserves the current authority shot_id",
            "affected_authority is derived from semantic graph relations",
            "preserve causal topology, action ownership, state hand-offs and audience processing",
        ],
    }
    prior = ""
    errors: list[str] = []
    for attempt in range(1, 3):
        if authority_checkpoint is not None:
            _verify_supervisor_paid_authority(
                authority_checkpoint,
                stage="semantic_storyboard_repair_model",
            )
        request = json.dumps(prompt, ensure_ascii=False)
        if errors:
            request += (
                "\nThe previous proposal failed deterministic validation. Return a full corrected JSON proposal.\n- "
                + "\n- ".join(errors[:20])
                + "\nPrevious proposal:\n"
                + prior[:16000]
            )
        # 每轮外层迭代都先重校验付费授权，再发出恰好一次模型调用：网关内部重试
        # 关掉（format/semantic 都为 0），让"付费边界前必校验"这条不变量原样保持，
        # 同时用 chat_structured 的受约束解析取代裸 extract_json。语义校验仍留在
        # 外层做——它要产出 candidate_board，并把确定性校验错误回喂下一轮。
        try:
            proposal = await model_gateway.chat_structured(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a senior film director repairing a storyboard from semantic "
                            "intent, narrative relations and audience comprehension. Output JSON only."
                        ),
                    },
                    {"role": "user", "content": request},
                ],
                model_type=StoryboardRepairProposal,
                validate=None,
                operation_id=(
                    f"semantic_storyboard_repair:{episode_id}:{database_shot_id}:{attempt}"
                ),
                temperature=0.2,
                max_tokens=16384,
                format_retry_limit=0,
                semantic_retry_limit=0,
                call_meta={
                    "stage": "video_supervisor_storyboard_repair",
                    "stage_key": "semantic_storyboard_repair_proposal",
                    "call_role": "continuity_director",
                    "episode_id": episode_id,
                    "repair_round": attempt - 1,
                    "contract_version": "storyboard-semantic-repair-proposal.v1",
                    "expected_json": True,
                },
            )
        except (
            model_gateway.StructuredFormatError,
            model_gateway.StructuredSemanticError,
            model_gateway.StructuredProviderRejection,
        ) as exc:  # untrusted model boundary: surface the real parse/schema failure
            errors = [f"JSON/Schema invalid: {exc}"]
            continue
        prior = json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False)
        candidate, errors = _validate_storyboard_repair_proposal(
            proposal,
            board=board,
            shot_index=shot_index,
            database_shot_id=database_shot_id,
            screenplay=screenplay,
            bible=bible,
            target_duration_s=target_duration_s,
            episode_id=episode_id,
        )
        if not errors and candidate is not None:
            return proposal, candidate
    raise ValueError("; ".join(errors[:20]) or "semantic repair model returned no proposal")


async def _amend_storyboard(
    entry: ShotCoverageEntry,
    *,
    grant: VideoCompletionGrant,
    plan: VideoRepairPlan | None = None,
    run_id: str | None = None,
) -> bool:
    """L5 只创建分镜修改草稿，不改写已确认分镜。

    ``allow_storyboard_edit`` 授予的是“提议草稿”权限，不是绕过人工重新
    确认的权限。草稿产生后 Supervisor 转 WAITING_HUMAN，且视频流水线
    保持暂停；只有分镜台完成并发布新终态后才能重新授权。
    """
    if not grant.allow_storyboard_edit:
        return False
    conn = get_conn()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (entry.shot_id,)).fetchone()
    if not row:
        return False
    episode_id = str(row["episode_id"])
    try:
        authority_checkpoint = VideoSupervisorCheckpoint(
            episode_id=episode_id,
            grant_id=grant.grant_id,
            storyboard_artifact_id=grant.storyboard_artifact_id,
            episode_video_plan_id=grant.episode_video_plan_id,
            episode_video_plan_revision=grant.episode_video_plan_revision,
            video_plan_release_hash=grant.video_plan_release_hash,
            capability_snapshot_id=grant.capability_snapshot_id,
        )
        _verify_supervisor_paid_authority(
            authority_checkpoint,
            stage="semantic_storyboard_repair",
        )
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        project = conn.execute(
            "SELECT bible_json FROM projects WHERE id=?", (episode["project_id"],)
        ).fetchone()
        if not project or not str(project["bible_json"] or "").strip():
            raise ValueError("当前项目缺少可验证的 Bible，不能生成分镜修复候选")
        from app.schemas import Bible
        from app.production.screenplay_authority import resolve_downstream_screenplay
        from app.domain.storyboard_ops import _board_from_shot_rows

        bible = Bible.model_validate_json(project["bible_json"])
        try:
            screenplay = resolve_downstream_screenplay(
                episode_id,
                conn=conn,
            ).screenplay
        except ValueError:
            screenplay = None
        rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        from app.storyboard_authority import (
            resolve_storyboard_outline_authority,
        )

        outline_authority = resolve_storyboard_outline_authority(
            episode_id,
            conn=conn,
            verify_shots=True,
        )
        board = _board_from_shot_rows(rows, int(episode["episode_no"] or 1))
        shot_index = next(
            index for index, item in enumerate(rows) if item["id"] == entry.shot_id
        )
        proposal, candidate_board = await _semantic_storyboard_repair_proposal(
            board=board,
            shot_index=shot_index,
            database_shot_id=entry.shot_id,
            screenplay=screenplay,
            bible=bible,
            target_duration_s=outline_authority.authoritative_duration_s,
            episode_id=episode_id,
            repair_plan=plan,
            observed_issue_codes=list(
                (plan.issue_codes if plan else entry.last_issue_codes) or []
            ),
            authority_checkpoint=authority_checkpoint,
        )
    except GrantValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - model/validator failure pauses for human
        if run_id:
            evidence_repository.append_event(
                run_id,
                "VIDEO_STORYBOARD_PROPOSAL_REJECTED",
                "warning",
                "AI 分镜语义修复候选未通过，已转人工",
                payload={"shot_id": entry.shot_id, "error": str(exc)[:2000]},
            )
        return False
    try:
        art = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard_repair_proposal",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T1",
            content={
                "proposed_by": "video_supervisor_semantic_director",
                "base_storyboard_artifact_id": grant.storyboard_artifact_id,
                "proposal": proposal.model_dump(mode="json"),
                "candidate_storyboard": candidate_board.model_dump(mode="json"),
                "requires_manual_confirmation": True,
            },
            parent_artifact_ids=[grant.storyboard_artifact_id] if grant.storyboard_artifact_id else [],
            contract_version="storyboard-semantic-repair-proposal.v1",
        ))
        conn.execute(
            "UPDATE episodes SET working_storyboard_artifact_id=? WHERE id=?",
            (art["id"], episode_id),
        )
        conn.commit()
        try:
            from app.video_control import request_control
            request_control(episode_id, "pause")
        except Exception:  # noqa: BLE001
            pass
        if run_id:
            evidence_repository.append_event(
                run_id, "VIDEO_STORYBOARD_DRAFT_CREATED", "warning",
                f"第 {entry.shot_no} 镜 L5 修改草稿待重新确认",
                payload={
                    "shot_id": entry.shot_id,
                    "artifact_id": art["id"],
                    "operation": proposal.operation,
                    "affected_authority": proposal.affected_authority.model_dump(
                        mode="json"
                    ),
                },
            )
        return True
    except Exception:  # noqa: BLE001
        return False


def _try_auto_crop(entry: ShotCoverageEntry, *, run_id: str | None) -> bool:
    if not entry.best_version_id:
        return False
    from app.video_crop import try_auto_crop_shot_version
    result = try_auto_crop_shot_version(entry.best_version_id)
    if not result or not result.get("ok"):
        return False
    if run_id:
        evidence_repository.append_event(
            run_id, "VIDEO_REPAIR_PLAN_SELECTED", "info",
            f"第 {entry.shot_no} 镜自动裁切",
            payload={"shot_no": entry.shot_no, "version_id": result.get("version_id")},
        )
    return True
