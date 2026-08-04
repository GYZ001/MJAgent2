"""Isolated cold-audience review and intent comparator.

The cold reader never receives source text, truth propositions, target deltas,
future reservations or director objectives.  Its free recall is persisted
before a separate comparator is allowed to see the intended audience paths.
"""
from __future__ import annotations

import json
from typing import Any

from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.narrative import (
    AUDIENCE_PERCEPTUAL_SURFACE_VERSION,
    NARRATIVE_CONTRACT_VERSION,
    _contains_forbidden_contract_key,
    audience_perceptual_surface,
    audience_perceptual_surface_hash,
    index_narrative_plan,
    validate_blind_review,
)
from app.schemas import (
    BlindAudienceObservation,
    EpisodeScreenplay,
    NarrativeReviewReport,
    Storyboard,
    extract_json,
)

BLIND_READER_PROMPT_VERSION = "blind-audience.v2"
COMPARATOR_PROMPT_VERSION = "narrative-comparator.v1"
BLIND_PERCEPTUAL_INPUT_ARTIFACT_TYPE = "blind_audience_perceptual_input"

_BLIND_FORBIDDEN_CONTRACT_KEYS = {
    "narrative_plan",
    "source_evidence",
    "propositions",
    "director_objective",
    "target_deltas",
    "assimilation_tasks",
    "reserved_future_event_ids",
    "planned_state_in_fact_ids",
    "planned_delta_add_fact_ids",
    "planned_delta_remove_fact_ids",
    "planned_state_out_fact_ids",
    "audience_state_paths",
}

_BLIND_SYSTEM = (
    "你是一名第一次观看成片分镜的普通观众，不是编剧、导演或审稿人。"
    "你只能依据输入中实际可见可听的内容作答。先完成自由复述并冻结，"
    "之后再记录中性追问观察；不得猜创作者想表达什么。只输出 JSON。"
)

_COMPARATOR_SYSTEM = (
    "你是叙事意图与冷观众观察的隔离比较器。故事真值和导演目标只用于比较，"
    "不得改写已经冻结的 spontaneous_recall，不得把追问答案算入首轮理解。"
    "逐 audience_prior_id、逐 target_delta_id 给出结果；只输出 JSON。"
)


class NarrativeReviewError(RuntimeError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("；".join(self.errors[:6]))


def _resolve_review_screenplay_authority(
    *,
    episode_id: str,
    supplied_screenplay: EpisodeScreenplay,
    supplied_artifact_id: str | None = None,
) -> tuple[EpisodeScreenplay, str]:
    """Resolve the published screenplay and reject caller-side drift.

    Blind review must never derive its perceptual projection from a mutable
    request object or a merely-current artifact pointer.  The resolver checks
    the published artifact, projection, consumed certificate, exact QA gate and
    current source fingerprint before we compare the supplied value.
    """
    try:
        from app.production.screenplay_authority import (
            resolve_current_screenplay_authority,
        )

        authority = resolve_current_screenplay_authority(
            episode_id,
            require_narrative=True,
        )
    except Exception as exc:  # noqa: BLE001 - immutable authority boundary
        raise NarrativeReviewError([
            f"[REVIEW_SCREENPLAY_AUTHORITY_INVALID] 冷观众审读剧本权威链无效：{exc}"
        ]) from exc

    requested_artifact_id = str(supplied_artifact_id or "").strip()
    if requested_artifact_id and requested_artifact_id != authority.artifact_id:
        raise NarrativeReviewError([
            "[REVIEW_INPUT_SCREENPLAY_STALE] 冷观众审读使用的剧本 Artifact "
            "已不是当前已发布权威版"
        ])
    if (
        supplied_screenplay.model_dump(mode="json")
        != authority.screenplay.model_dump(mode="json")
    ):
        raise NarrativeReviewError([
            "[REVIEW_INPUT_SCREENPLAY_DRIFT] 调用方传入剧本与当前已发布权威版不一致"
        ])
    return authority.screenplay, authority.artifact_id


def verify_persisted_narrative_review(
    *,
    episode_id: str,
    screenplay: EpisodeScreenplay,
    board: Storyboard,
    report: NarrativeReviewReport,
    artifact_ids: list[str],
) -> str:
    """Verify an immutable cold-review chain before carrying its gate forward.

    A caller cannot turn a hand-built ``decision='pass'`` object into a release
    gate.  The report, frozen observations and exact reviewed board projection
    must all exist, be current, and already own a passed comparator evaluation.
    """
    screenplay, _screenplay_artifact_id = _resolve_review_screenplay_authority(
        episode_id=episode_id,
        supplied_screenplay=screenplay,
    )
    ids = list(dict.fromkeys(str(item) for item in artifact_ids if str(item)))
    artifacts = {
        artifact_id: evidence_repository.get_artifact(artifact_id)
        for artifact_id in ids
    }
    errors: list[str] = []
    if any(item is None for item in artifacts.values()):
        errors.append("[NARRATIVE_REVIEW_ARTIFACT_MISSING] 审读证据链含不存在的 Artifact")
    usable = {
        artifact_id: item
        for artifact_id, item in artifacts.items()
        if item is not None
        and item.get("status") not in {"stale", "rejected", "superseded", "needs_revision"}
    }
    review_inputs = [
        (artifact_id, item) for artifact_id, item in usable.items()
        if item.get("type") == "storyboard_review_input"
    ]
    perceptual_input_rows = [
        (artifact_id, item) for artifact_id, item in usable.items()
        if item.get("type") == BLIND_PERCEPTUAL_INPUT_ARTIFACT_TYPE
    ]
    reports = [
        (artifact_id, item) for artifact_id, item in usable.items()
        if item.get("type") == "narrative_review_report"
    ]
    observation_rows = [
        (artifact_id, item) for artifact_id, item in usable.items()
        if item.get("type") == "blind_audience_observation"
    ]
    if len(review_inputs) != 1:
        errors.append("[NARRATIVE_REVIEW_INPUT_INVALID] 必须且只能有一个当前 storyboard_review_input")
    if len(reports) != 1:
        errors.append("[NARRATIVE_REVIEW_REPORT_INVALID] 必须且只能有一个有效 narrative_review_report")

    plan = screenplay.narrative_plan
    if plan is None:
        errors.append("[NARRATIVE_PLAN_MISSING] 冷观众证据链缺少剧本叙事合同")
        expected_priors = []
    else:
        expected_priors = list(plan.audience_priors)
    if len(perceptual_input_rows) != len(expected_priors):
        errors.append(
            "[BLIND_PERCEPTUAL_INPUT_COVERAGE_INVALID] "
            "每个 audience prior 必须且只能有一个精确感知输入 Artifact"
        )

    current_parents = _current_review_input_parent_artifact_ids(
        episode_id,
        board,
        None,
    )
    if review_inputs:
        input_id, review_input = review_inputs[0]
        if review_input.get("scope_type") != "episode" or review_input.get("scope_id") != episode_id:
            errors.append("[NARRATIVE_REVIEW_INPUT_SCOPE_INVALID] 审读输入不属于当前集")
        if review_input.get("content") != board.model_dump(mode="json"):
            errors.append("[NARRATIVE_REVIEW_BOARD_DRIFT] 审读输入与待发布分镜内容不一致")
        if list(review_input.get("parent_artifact_ids") or []) != current_parents:
            errors.append("[NARRATIVE_REVIEW_LINEAGE_DRIFT] 审读输入未绑定当前剧本和逐镜证据")
    else:
        input_id = ""
    base_review_parents = (
        [current_parents[0], input_id]
        if current_parents and input_id
        else []
    )

    perceptual_by_prior: dict[str, tuple[str, dict[str, Any]]] = {}
    expected_prior_by_id = {
        prior.audience_prior_id: (ordinal, prior)
        for ordinal, prior in enumerate(expected_priors, start=1)
    }
    for perceptual_artifact_id, artifact in perceptual_input_rows:
        content = artifact.get("content")
        if not isinstance(content, dict):
            errors.append("[BLIND_PERCEPTUAL_INPUT_INVALID] 感知输入 Artifact 内容不是对象")
            continue
        prior_id = str(content.get("audience_prior_id") or "")
        if not prior_id or prior_id not in expected_prior_by_id:
            errors.append(
                f"[BLIND_PERCEPTUAL_PRIOR_INVALID] 感知输入引用未知 audience prior：{prior_id}"
            )
            continue
        if prior_id in perceptual_by_prior:
            errors.append(
                f"[BLIND_PERCEPTUAL_PRIOR_DUPLICATE] audience prior {prior_id} 有多个感知输入"
            )
            continue
        ordinal, prior = expected_prior_by_id[prior_id]
        try:
            expected_content = _blind_perceptual_input_content(
                prior=prior,
                screenplay=screenplay,
                board=board,
                observation_id=f"BAO-{episode_id}-{ordinal}",
            )
        except NarrativeReviewError as exc:
            errors.extend(exc.errors)
            continue
        if artifact.get("scope_type") != "episode" or artifact.get("scope_id") != episode_id:
            errors.append("[BLIND_PERCEPTUAL_INPUT_SCOPE_INVALID] 感知输入不属于当前集")
        if artifact.get("contract_version") != AUDIENCE_PERCEPTUAL_SURFACE_VERSION:
            errors.append("[BLIND_PERCEPTUAL_SERIALIZER_VERSION_DRIFT] 感知序列化版本已变化")
        if artifact.get("prompt_version") != BLIND_READER_PROMPT_VERSION:
            errors.append("[BLIND_PERCEPTUAL_PROMPT_VERSION_DRIFT] 冷观众 prompt 版本已变化")
        if list(artifact.get("parent_artifact_ids") or []) != base_review_parents:
            errors.append("[BLIND_PERCEPTUAL_INPUT_LINEAGE_INVALID] 感知输入未继承当前剧本与分镜输入")
        model_payload = content.get("model_prompt_payload")
        if not isinstance(model_payload, dict):
            errors.append("[BLIND_PERCEPTUAL_PROMPT_MISSING] 感知输入没有精确模型 payload")
        elif content.get("prompt_payload_hash") != audience_perceptual_surface_hash(model_payload):
            errors.append("[BLIND_PERCEPTUAL_PROMPT_HASH_INVALID] 冷观众 payload hash 与内容不一致")
        surface = model_payload.get("input") if isinstance(model_payload, dict) else None
        if not isinstance(surface, dict):
            errors.append("[BLIND_PERCEPTUAL_SURFACE_MISSING] 冷观众 payload 缺少感知表面")
        elif content.get("perceptual_surface_hash") != audience_perceptual_surface_hash(surface):
            errors.append("[BLIND_PERCEPTUAL_SURFACE_HASH_INVALID] 感知表面 hash 与内容不一致")
        if content != expected_content:
            errors.append(
                f"[BLIND_PERCEPTUAL_INPUT_DRIFT] prior={prior_id} 的精确感知输入已与当前分镜不同"
            )
        perceptual_by_prior[prior_id] = (perceptual_artifact_id, content)

    conn = evidence_repository.get_conn()
    observation_by_prior: dict[str, tuple[str, BlindAudienceObservation]] = {}
    for observation_artifact_id, artifact in observation_rows:
        try:
            observation = BlindAudienceObservation.model_validate(artifact.get("content"))
        except Exception as exc:  # noqa: BLE001 - persisted contract boundary
            errors.append(f"[NARRATIVE_REVIEW_OBSERVATION_INVALID] {exc}")
            continue
        prior_id = observation.audience_prior_id
        if prior_id in observation_by_prior:
            errors.append(
                f"[NARRATIVE_REVIEW_OBSERVATION_DUPLICATE] prior={prior_id} 有多个冻结观察"
            )
            continue
        if prior_id not in expected_prior_by_id:
            errors.append(
                f"[NARRATIVE_REVIEW_OBSERVATION_PRIOR_INVALID] 冻结观察引用未知 prior={prior_id}"
            )
            continue
        perceptual_row = perceptual_by_prior.get(prior_id)
        if perceptual_row is None:
            errors.append(
                f"[NARRATIVE_REVIEW_OBSERVATION_INPUT_MISSING] prior={prior_id} 缺少精确感知输入"
            )
            continue
        perceptual_artifact_id, perceptual_content = perceptual_row
        if artifact.get("scope_type") != "episode" or artifact.get("scope_id") != episode_id:
            errors.append(
                "[NARRATIVE_REVIEW_OBSERVATION_SCOPE_INVALID] 冷观众观察不属于当前集"
            )
        if artifact.get("prompt_version") != BLIND_READER_PROMPT_VERSION:
            errors.append("[NARRATIVE_REVIEW_OBSERVATION_PROMPT_DRIFT] 冻结观察 prompt 版本已变化")
        expected_observation_parents = [*base_review_parents, perceptual_artifact_id]
        if list(artifact.get("parent_artifact_ids") or []) != expected_observation_parents:
            errors.append(
                "[NARRATIVE_REVIEW_OBSERVATION_LINEAGE_INVALID] "
                "冷观众观察未精确继承当前剧本、分镜输入与逐先验感知 payload"
            )
        if observation.observation_id != perceptual_content.get("observation_id"):
            errors.append("[NARRATIVE_REVIEW_OBSERVATION_ID_DRIFT] 冻结观察与感知输入 ID 不一致")
        visible_handles = {
            handle
            for shot in (
                perceptual_content.get("model_prompt_payload", {})
                .get("input", {})
                .get("ordered_storyboard_as_seen", [])
            )
            for handle in shot.get("observable_evidence_handles") or []
        }
        if not set(observation.supporting_evidence_ids).issubset(visible_handles):
            errors.append("[BLIND_EVIDENCE_NOT_VISIBLE] 冻结观察引用了精确 payload 中不存在的证据")
        isolation_gate = conn.execute(
            """SELECT evaluator_version,evidence_json FROM evaluations
                 WHERE artifact_id=?
                   AND evaluator_name='blind_review_isolation_gate'
                   AND evaluator_version=?
                   AND evaluation_role='runtime_gate'
                   AND runtime_blocking=1
                   AND status='passed' AND hard_gate_passed=1
                 LIMIT 1""",
            (observation_artifact_id, BLIND_READER_PROMPT_VERSION),
        ).fetchone()
        if isolation_gate is None:
            errors.append(
                "[NARRATIVE_REVIEW_ISOLATION_GATE_MISSING] "
                "冷观众观察缺少同 Artifact 的隔离 runtime gate"
            )
        else:
            try:
                gate_evidence = json.loads(isolation_gate["evidence_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                gate_evidence = {}
            expected_gate_evidence = {
                "audience_prior_id": prior_id,
                "target_free_payload": True,
                "perceptual_input_artifact_id": perceptual_artifact_id,
                "serializer_version": AUDIENCE_PERCEPTUAL_SURFACE_VERSION,
                "prompt_version": BLIND_READER_PROMPT_VERSION,
                "perceptual_surface_hash": perceptual_content.get("perceptual_surface_hash"),
                "prompt_payload_hash": perceptual_content.get("prompt_payload_hash"),
            }
            if gate_evidence != expected_gate_evidence:
                errors.append("[NARRATIVE_REVIEW_ISOLATION_EVIDENCE_DRIFT] 隔离门未绑定精确 payload hash")
        if _contains_forbidden_contract_key(
            observation.spontaneous_recall,
            {"target_deltas", "target_delta_id", "director_objective", "withheld_propositions"},
        ):
            errors.append("[BLIND_REVIEW_TARGET_LEAK] 冻结自由复述泄漏了导演目标")
        observation_by_prior[prior_id] = (observation_artifact_id, observation)

    if set(observation_by_prior) != set(expected_prior_by_id):
        errors.append("[NARRATIVE_REVIEW_OBSERVATION_COVERAGE_INVALID] 冻结观察未逐 prior 完整覆盖")
    observations = [
        observation_by_prior[prior.audience_prior_id][1]
        for prior in expected_priors
        if prior.audience_prior_id in observation_by_prior
    ]

    if reports:
        report_id, report_artifact = reports[0]
        try:
            persisted_report = NarrativeReviewReport.model_validate(report_artifact.get("content"))
        except Exception as exc:  # noqa: BLE001 - persisted contract boundary
            errors.append(f"[NARRATIVE_REVIEW_REPORT_INVALID] {exc}")
            persisted_report = None
        if persisted_report != report:
            errors.append("[NARRATIVE_REVIEW_REPORT_DRIFT] 调用方报告与已冻结报告不一致")
        expected_report_parents = list(dict.fromkeys([
            *base_review_parents,
            *[
                artifact_id
                for prior in expected_priors
                for artifact_id in (
                    perceptual_by_prior.get(prior.audience_prior_id, ("", {}))[0],
                    observation_by_prior.get(prior.audience_prior_id, ("", None))[0],
                )
                if artifact_id
            ],
        ]))
        if list(report_artifact.get("parent_artifact_ids") or []) != expected_report_parents:
            errors.append(
                "[NARRATIVE_REVIEW_REPORT_LINEAGE_MISSING] "
                "报告未精确继承当前剧本、分镜输入、逐先验 payload 和全部冻结观察"
            )
        gate = conn.execute(
            """SELECT 1 FROM evaluations
                 WHERE artifact_id=? AND evaluator_name='narrative_blind_comparator'
                   AND evaluator_version=?
                   AND evaluation_role='runtime_gate' AND status='passed'
                   AND runtime_blocking=1 AND hard_gate_passed=1
                 LIMIT 1""",
            (report_id, COMPARATOR_PROMPT_VERSION),
        ).fetchone()
        if gate is None:
            errors.append("[NARRATIVE_REVIEW_GATE_MISSING] 冻结报告没有已通过的 comparator runtime gate")
    else:
        report_id = ""

    errors.extend(validate_blind_review(screenplay, observations, report))
    if report.decision != "pass":
        errors.append("[NARRATIVE_REVIEW_NOT_PASSED] 冷观众审读结论不是 pass")
    if errors:
        raise NarrativeReviewError(list(dict.fromkeys(errors)))
    return report_id


def verify_review_chain_for_storyboard_artifact(
    *,
    episode_id: str,
    screenplay: EpisodeScreenplay,
    storyboard_artifact: dict[str, Any],
) -> str:
    """Verify the exact review ancestry carried by one storyboard artifact."""
    if (
        storyboard_artifact.get("scope_type") != "episode"
        or storyboard_artifact.get("scope_id") != episode_id
        or storyboard_artifact.get("status")
        in {"stale", "rejected", "superseded", "needs_revision"}
    ):
        raise NarrativeReviewError([
            "[NARRATIVE_STORYBOARD_ARTIFACT_INVALID] 分镜 Artifact 范围或状态无效"
        ])
    try:
        board = Storyboard.model_validate(storyboard_artifact.get("content"))
    except Exception as exc:  # noqa: BLE001 - immutable artifact boundary
        raise NarrativeReviewError([
            f"[NARRATIVE_STORYBOARD_ARTIFACT_INVALID] 分镜 Artifact 内容无法解析：{exc}"
        ]) from exc
    parent_ids = list(storyboard_artifact.get("parent_artifact_ids") or [])
    parent_artifacts = [
        evidence_repository.get_artifact(parent_id)
        for parent_id in parent_ids
    ]
    report_artifacts = [
        item
        for item in parent_artifacts
        if item is not None and item.get("type") == "narrative_review_report"
    ]
    if len(report_artifacts) != 1:
        raise NarrativeReviewError([
            "[NARRATIVE_REVIEW_REPORT_INVALID] 分镜 Artifact 必须有唯一直接冷观众报告父证据"
        ])
    try:
        report = NarrativeReviewReport.model_validate(report_artifacts[0].get("content"))
    except Exception as exc:  # noqa: BLE001 - immutable report boundary
        raise NarrativeReviewError([
            f"[NARRATIVE_REVIEW_REPORT_INVALID] {exc}"
        ]) from exc
    return verify_persisted_narrative_review(
        episode_id=episode_id,
        screenplay=screenplay,
        board=board,
        report=report,
        artifact_ids=parent_ids,
    )


def invalidate_episode_narrative_review(
    conn,
    episode_id: str,
    reason: str,
    *,
    upstream_artifact_ids: list[str] | tuple[str, ...] | set[str] = (),
    exclude_artifact_ids: set[str] | None = None,
    commit: bool = False,
) -> list[str]:
    """Invalidate review evidence derived from changed storyboard evidence.

    The traversal follows immutable artifact parent links, so every current or
    future review-stage artifact is covered without maintaining a list of
    narrative artifact types.  The episode pointer is included as a safety
    root for legacy reports created before shot-level lineage was complete.
    """
    episode = conn.execute(
        "SELECT narrative_review_artifact_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        return []

    roots = {
        str(artifact_id)
        for artifact_id in upstream_artifact_ids
        if str(artifact_id or "").strip()
    }
    review_artifact_id = episode["narrative_review_artifact_id"]
    excluded = {str(item) for item in (exclude_artifact_ids or set())}
    rows = conn.execute(
        "SELECT id,status,parent_artifact_ids_json FROM artifacts"
    ).fetchall()
    children: dict[str, list[str]] = {}
    status_by_id: dict[str, str] = {}
    for row in rows:
        artifact_id = str(row["id"])
        status_by_id[artifact_id] = str(row["status"] or "")
        try:
            parents = json.loads(row["parent_artifact_ids_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            parents = []
        if not isinstance(parents, list):
            parents = []
        for parent_id in parents:
            normalized = str(parent_id or "").strip()
            if normalized:
                children.setdefault(normalized, []).append(artifact_id)

    stale_candidates: set[str] = set()
    pending = [child for root in roots for child in children.get(root, [])]
    if review_artifact_id:
        normalized_review_id = str(review_artifact_id)
        stale_candidates.add(normalized_review_id)
        pending.extend(children.get(normalized_review_id, []))
    while pending:
        artifact_id = pending.pop()
        if artifact_id in stale_candidates or artifact_id in excluded:
            continue
        stale_candidates.add(artifact_id)
        pending.extend(children.get(artifact_id, []))

    changed = sorted(
        artifact_id
        for artifact_id in stale_candidates
        if artifact_id not in excluded
        and status_by_id.get(artifact_id) not in {None, "stale", "rejected"}
    )
    if changed:
        conn.executemany(
            "UPDATE artifacts SET status='stale',stale_reason=? "
            "WHERE id=? AND status NOT IN ('stale','rejected')",
            [(reason, artifact_id) for artifact_id in changed],
        )
    conn.execute(
        "UPDATE episodes SET narrative_status='needs_review', "
        "narrative_review_artifact_id=NULL, "
        "narrative_calibration_artifact_id=NULL WHERE id=?",
        (episode_id,),
    )
    if commit:
        conn.commit()
    return changed


def _current_review_input_parent_artifact_ids(
    episode_id: str,
    board: Storyboard,
    screenplay_artifact_id: str | None,
) -> list[str]:
    """Resolve the exact screenplay and shot evidence reviewed by this run."""
    conn = evidence_repository.get_conn()
    episode = conn.execute(
        "SELECT screenplay_artifact_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise NarrativeReviewError([
            f"[REVIEW_INPUT_EPISODE_MISSING] 冷观众审读集 {episode_id} 不存在"
        ])
    current_screenplay_id = str(episode["screenplay_artifact_id"] or "").strip()
    requested_screenplay_id = str(screenplay_artifact_id or "").strip()
    if requested_screenplay_id and requested_screenplay_id != current_screenplay_id:
        raise NarrativeReviewError([
            "[REVIEW_INPUT_SCREENPLAY_STALE] 冷观众审读使用的剧本已不是本集当前版本"
        ])
    if not current_screenplay_id:
        raise NarrativeReviewError([
            "[REVIEW_INPUT_SCREENPLAY_MISSING] 冷观众审读缺少当前剧本证据"
        ])

    screenplay_artifact = conn.execute(
        "SELECT status FROM artifacts WHERE id=?",
        (current_screenplay_id,),
    ).fetchone()
    if (
        screenplay_artifact is None
        or screenplay_artifact["status"] in {"stale", "rejected", "superseded"}
    ):
        raise NarrativeReviewError([
            "[REVIEW_INPUT_SCREENPLAY_INVALID] 冷观众审读的当前剧本证据不可用"
        ])

    rows = conn.execute(
        "SELECT shot_no,storyboard_artifact_id FROM shots "
        "WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    if len(rows) != len(board.shots):
        raise NarrativeReviewError([
            "[REVIEW_INPUT_SHOT_COUNT_DRIFT] 冷观众审读的镜头证据数与当前分镜不一致"
        ])

    parent_ids = [current_screenplay_id]
    for row, shot in zip(rows, board.shots):
        shot_no = int(row["shot_no"])
        if shot_no != int(shot.shot_no):
            raise NarrativeReviewError([
                "[REVIEW_INPUT_SHOT_ORDER_DRIFT] 冷观众审读的镜头顺序与当前分镜不一致"
            ])
        artifact_id = str(row["storyboard_artifact_id"] or "").strip()
        if not artifact_id:
            raise NarrativeReviewError([
                f"[REVIEW_INPUT_SHOT_EVIDENCE_MISSING] 第 {shot_no} 镜缺少当前证据"
            ])
        artifact = conn.execute(
            "SELECT status FROM artifacts WHERE id=?",
            (artifact_id,),
        ).fetchone()
        if (
            artifact is None
            or artifact["status"] in {"stale", "rejected", "superseded"}
        ):
            raise NarrativeReviewError([
                f"[REVIEW_INPUT_SHOT_EVIDENCE_INVALID] 第 {shot_no} 镜当前证据不可用"
            ])
        parent_ids.append(artifact_id)
    return list(dict.fromkeys(parent_ids))


async def _structured_call(
    *,
    system: str,
    prompt: dict[str, Any],
    model_cls,
    call_role: str,
    episode_id: str,
    max_attempts: int = 3,
):
    errors: list[str] = []
    prior_raw = ""
    for attempt in range(1, max_attempts + 1):
        request = json.dumps(prompt, ensure_ascii=False)
        if errors:
            request += (
                "\n上一份 JSON 未通过结构/隔离校验，请只修正这些问题并重发完整 JSON：\n- "
                + "\n- ".join(errors[:12])
                + "\n上一份候选：\n"
                + prior_raw[:12000]
            )
        raw = await model_gateway.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": request}],
            temperature=0.2,
            max_tokens=16384,
            call_meta={
                "stage": "narrative_blind_review",
                "stage_key": call_role,
                "call_role": call_role,
                "episode_id": episode_id,
                "repair_round": attempt - 1,
                "contract_version": NARRATIVE_CONTRACT_VERSION,
            },
        )
        prior_raw = raw
        try:
            instance = model_cls.model_validate(extract_json(raw))
        except Exception as exc:  # noqa: BLE001 - model output boundary
            errors = [f"JSON/Schema 无效：{exc}"]
            continue
        return instance
    raise NarrativeReviewError(errors or ["冷观众模型未返回有效结构"])


def _blind_prompt(payload: dict[str, Any], *, observation_id: str) -> dict[str, Any]:
    return {
        "task": "模拟一次连续观看；先冻结自由复述，再做不暗示答案的中性观察",
        "input": payload,
        "output_contract": {
            "observation_id": observation_id,
            "audience_prior_id": payload["audience_prior"]["audience_prior_id"],
            "anchor": {"type": "sequence", "id": "episode"},
            "spontaneous_recall": {
                "recognized_entities": [],
                "inferred_propositions": [],
                "causal_hypotheses": [],
                "character_goal_hypotheses": [],
                "active_question_ids": [],
            },
            "neutral_followup_observations": [],
            "noticed_attention_target_ids": [],
            "spatial_temporal_model": {},
            "felt_affective_state": {},
            "perceived_relationship_deltas": [],
            "perceived_stakes": [],
            "experienced_pressure_curve": [],
            "experienced_rhythm": {
                "momentum": 0.0,
                "processing_sufficiency": 0.0,
                "drag_or_rush_observations": [],
            },
            "next_event_expectations": [],
            "uncertainties": [],
            "spontaneous_supporting_evidence_ids": [],
            "supporting_evidence_ids": [],
            "confidence": 0.0,
        },
        "hard_rules": [
            "spontaneous_recall 必须先完成并视为冻结",
            "spontaneous_supporting_evidence_ids 必须在任何追问前与 spontaneous_recall 一起冻结",
            "neutral_followup_observations 不得反写 spontaneous_recall",
            "只依据 input.ordered_storyboard_as_seen 中按顺序实际呈现的首尾帧、可见动作、"
            "时码声轨、时码文字与剪辑关系",
            "不得用剧本、原文或未拍内容补全分镜中没有呈现的信息",
            "不得输出 director_objective、target_deltas 或猜测目标答案",
        ],
    }


def _blind_perceptual_input_content(
    *,
    prior,
    screenplay: EpisodeScreenplay,
    board: Storyboard,
    observation_id: str,
) -> dict[str, Any]:
    """Freeze the exact target-free model payload and both of its identities."""
    surface = audience_perceptual_surface(prior, screenplay, board)
    if _contains_forbidden_contract_key(surface, _BLIND_FORBIDDEN_CONTRACT_KEYS):
        raise NarrativeReviewError([
            "[BLIND_REVIEW_TARGET_LEAK] AudiencePerceptualSurface 含导演目标或计划事实"
        ])
    model_prompt_payload = _blind_prompt(surface, observation_id=observation_id)
    return {
        "audience_prior_id": prior.audience_prior_id,
        "observation_id": observation_id,
        "serializer_version": AUDIENCE_PERCEPTUAL_SURFACE_VERSION,
        "prompt_version": BLIND_READER_PROMPT_VERSION,
        "perceptual_surface_hash": audience_perceptual_surface_hash(surface),
        "prompt_payload_hash": audience_perceptual_surface_hash(model_prompt_payload),
        "model_prompt_payload": model_prompt_payload,
    }


def _comparator_prompt(
    screenplay: EpisodeScreenplay,
    observations: list[BlindAudienceObservation],
    *,
    report_id: str,
) -> dict[str, Any]:
    plan = screenplay.narrative_plan
    assert plan is not None
    target_ids_by_prior = {
        prior.audience_prior_id: [
            delta.target_delta_id
            for intent in plan.experience_intents
            for path in intent.audience_paths
            if path.audience_prior_id == prior.audience_prior_id
            for delta in path.target_deltas
        ]
        for prior in plan.audience_priors
    }
    # This second-stage prompt may see intent, but only after observations have
    # been parsed, validated and persisted as immutable artifacts.
    return {
        "task": "将冻结的冷观众首轮观察与逐先验导演意图进行比较",
        "scope_id": plan.scope_id,
        "experience_intents": [item.model_dump(mode="json") for item in plan.experience_intents],
        "audience_priors": [item.model_dump(mode="json") for item in plan.audience_priors],
        "dramatic_questions": [item.model_dump(mode="json") for item in plan.dramatic_questions],
        "action_relation_audits": [
            item.model_dump(mode="json") for item in plan.action_relation_audits
        ],
        "frozen_blind_observations": [item.model_dump(mode="json") for item in observations],
        "output_contract": {
            "narrative_review_report_id": report_id,
            "scope_id": plan.scope_id,
            "experience_intent_ids": [item.experience_intent_id for item in plan.experience_intents],
            "observation_ids": [item.observation_id for item in observations],
            "target_delta_results": [
                {
                    "audience_prior_id": path.audience_prior_id,
                    "target_delta_id": delta.target_delta_id,
                    "result": "satisfied|missed|contradicted|needs_review",
                    "predicted_score": 0.0,
                    "supporting_observation_ids": [item.observation_id for item in observations if item.audience_prior_id == path.audience_prior_id],
                    "supporting_evidence_ids": [],
                    "reason": "只基于同先验冻结复述与其实际引用证据的比较理由",
                }
                for intent in plan.experience_intents
                for path in intent.audience_paths
                for delta in path.target_deltas
            ],
            "character_goal_readability_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "当前比较未声明该维度目标；若实际适用必须改为 applies 并给出冻结观察证据"},
            "attention_alignment_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "当前比较未声明该维度目标；若实际适用必须改为 applies 并给出冻结观察证据"},
            "spatial_temporal_orientation_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "当前比较未声明该维度目标；若实际适用必须改为 applies 并给出冻结观察证据"},
            "affective_alignment_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "当前比较未声明该维度目标；若实际适用必须改为 applies 并给出冻结观察证据"},
            "relationship_change_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "当前比较未声明该维度目标；若实际适用必须改为 applies 并给出冻结观察证据"},
            "stakes_readability_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "当前比较未声明该维度目标；若实际适用必须改为 applies 并给出冻结观察证据"},
            "pressure_rhythm_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "当前比较未声明该维度目标；若实际适用必须改为 applies 并给出冻结观察证据"},
            "action_functional_repetition_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "若存在动作语义重复，必须证明冷观众感知到新的观众/人物/证据增量，否则不得通过"},
            "next_expectation_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "当前比较未声明该维度目标；若实际适用必须改为 applies 并给出冻结观察证据"},
            "intentional_ambiguity_result": {"applicability": "not_applicable", "passed": False, "evidence_ids": [], "reason": "当前比较未声明该维度目标；若实际适用必须改为 applies 并给出冻结观察证据"},
            "low_percentile_result": {
                "passed": False,
                "per_prior": {
                    prior_id: {
                        "passed": False,
                        "target_delta_ids": target_ids,
                        "reason": "该先验路径的冻结首轮理解结论",
                    }
                    for prior_id, target_ids in target_ids_by_prior.items()
                },
                "reason": "",
            },
            "inference_variance": 0.0,
            "evidence_gap_ids": [],
            "unintended_inference_ids": [],
            "decision": "pass|revise|needs_human_review",
            "reason": "",
        },
        "hard_rules": [
            "逐 audience_prior_id、逐 target_delta_id 比较，禁止先平均",
            "每个 target_delta_result 必须给出 0..1 predicted_score，表示仅基于冻结首轮观察时"
            "该目标实际达成的模型置信度；不得按最终 decision 统一填 0 或 1",
            "只有 spontaneous_recall 自发出现且可下钻到 spontaneous_supporting_evidence_ids 的理解可计首轮达成",
            "故意隐藏须按 withheld_propositions/延续问题判断，不得误报为遗漏",
            "任何低分位关键路径 missed/contradicted/needs_review 时不得判 pass",
            "decision=pass 时，每个 applicability=applies 的审读维度都必须 passed=true，并引用冷观众实际观察到的 evidence_id",
            "action_relation_audits 中的功能性重复必须单独审读；只有冷观众首轮感知到新增量才可 passed",
            "low_percentile_result.per_prior 必须覆盖每个先验及其全部 target_delta_id，任一路径不通过则总决策不得 pass",
        ],
    }


def _issue(message: str, episode_id: str) -> Issue:
    code = "NARRATIVE_REVIEW_FAILED"
    if message.startswith("[") and "]" in message:
        code = message[1:message.index("]")]
    return Issue(
        code=code,
        severity=IssueSeverity.BLOCKER,
        subject=episode_id,
        message=message,
        repairable=True,
        evidence={"stage": "blind_audience_review"},
    )


async def run_blind_audience_review(
    *,
    episode_id: str,
    screenplay: EpisodeScreenplay,
    board: Storyboard,
    screenplay_artifact_id: str | None = None,
    storyboard_artifact_id: str | None = None,
) -> tuple[list[BlindAudienceObservation], NarrativeReviewReport, list[str]]:
    """Run, persist and evaluate a complete multi-prior cold review.

    Returns observations, report and the created artifact IDs.  A non-pass
    report is still persisted for repair evidence, then raises
    ``NarrativeReviewError`` so publication cannot proceed.
    """
    screenplay, screenplay_artifact_id = _resolve_review_screenplay_authority(
        episode_id=episode_id,
        supplied_screenplay=screenplay,
        supplied_artifact_id=screenplay_artifact_id,
    )
    plan = screenplay.narrative_plan
    if plan is None:
        raise NarrativeReviewError(["[NARRATIVE_PLAN_MISSING] 冷观众审读缺少剧本叙事合同"])
    index = index_narrative_plan(plan)
    artifact_ids: list[str] = []
    review_input_parent_ids = _current_review_input_parent_artifact_ids(
        episode_id,
        board,
        screenplay_artifact_id,
    )
    if storyboard_artifact_id:
        supplied_input = evidence_repository.get_artifact(storyboard_artifact_id)
        if (
            supplied_input is None
            or supplied_input.get("type") != "storyboard_review_input"
            or supplied_input.get("status")
            in {"stale", "rejected", "superseded", "needs_revision"}
            or supplied_input.get("content") != board.model_dump(mode="json")
            or list(supplied_input.get("parent_artifact_ids") or [])
            != review_input_parent_ids
        ):
            raise NarrativeReviewError([
                "[REVIEW_INPUT_ARTIFACT_INVALID] 指定的审读输入未精确绑定当前剧本与逐镜证据"
            ])
        artifact_ids.append(str(storyboard_artifact_id))
    else:
        review_input = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard_review_input",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content=board.model_dump(mode="json"),
            parent_artifact_ids=review_input_parent_ids,
            contract_version=NARRATIVE_CONTRACT_VERSION,
        ))
        storyboard_artifact_id = str(review_input["id"])
        artifact_ids.append(storyboard_artifact_id)
    parents = [review_input_parent_ids[0], str(storyboard_artifact_id)]
    observations: list[BlindAudienceObservation] = []
    perceptual_input_artifact_ids: list[str] = []
    observation_artifact_ids: list[str] = []
    for ordinal, prior in enumerate(plan.audience_priors, start=1):
        observation_id = f"BAO-{episode_id}-{ordinal}"
        perceptual_input_content = _blind_perceptual_input_content(
            prior=prior,
            screenplay=screenplay,
            board=board,
            observation_id=observation_id,
        )
        perceptual_input_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type=BLIND_PERCEPTUAL_INPUT_ARTIFACT_TYPE,
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T2",
                content=perceptual_input_content,
                parent_artifact_ids=parents,
                contract_version=AUDIENCE_PERCEPTUAL_SURFACE_VERSION,
                prompt_version=BLIND_READER_PROMPT_VERSION,
            )
        )
        perceptual_input_artifact_id = str(perceptual_input_artifact["id"])
        artifact_ids.append(perceptual_input_artifact_id)
        perceptual_input_artifact_ids.append(perceptual_input_artifact_id)
        observation = await _structured_call(
            system=_BLIND_SYSTEM,
            prompt=perceptual_input_content["model_prompt_payload"],
            model_cls=BlindAudienceObservation,
            call_role="blind_reader",
            episode_id=episode_id,
        )
        if observation.observation_id != observation_id:
            raise NarrativeReviewError([
                f"[BLIND_OBSERVATION_ID_MISMATCH] 冷观众输出 {observation.observation_id}，"
                f"本轮只能输出 {observation_id}"
            ])
        if observation.audience_prior_id != prior.audience_prior_id:
            raise NarrativeReviewError([
                f"[BLIND_PRIOR_MISMATCH] 冷观众输出 {observation.audience_prior_id}，"
                f"本轮只能输出 {prior.audience_prior_id}"
            ])
        if _contains_forbidden_contract_key(
            observation.spontaneous_recall,
            {"target_deltas", "target_delta_id", "director_objective", "withheld_propositions"},
        ):
            raise NarrativeReviewError(["[BLIND_REVIEW_TARGET_LEAK] 冷观众自由复述泄漏了导演目标"])
        visible_handles = {
            evidence_id
            for shot in perceptual_input_content["model_prompt_payload"]["input"][
                "ordered_storyboard_as_seen"
            ]
            for evidence_id in shot.get("observable_evidence_handles") or []
        }
        if not set(observation.supporting_evidence_ids).issubset(visible_handles):
            raise NarrativeReviewError([
                "[BLIND_EVIDENCE_NOT_VISIBLE] 冷观众引用了输入中不存在的可见证据句柄"
            ])
        if not set(observation.spontaneous_supporting_evidence_ids).issubset(
            observation.supporting_evidence_ids
        ):
            raise NarrativeReviewError([
                "[BLIND_SPONTANEOUS_EVIDENCE_INVALID] 首轮冻结证据不是观察实际可见证据的子集"
            ])
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="blind_audience_observation",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content=observation.model_dump(mode="json"),
            parent_artifact_ids=[*parents, perceptual_input_artifact_id],
            contract_version=NARRATIVE_CONTRACT_VERSION,
            prompt_version=BLIND_READER_PROMPT_VERSION,
        ))
        evidence_repository.create_evaluation(
            artifact["id"],
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="blind_review_isolation_gate",
                evaluator_version=BLIND_READER_PROMPT_VERSION,
                status="passed",
                hard_gate_passed=True,
                evaluation_role="runtime_gate",
                runtime_blocking=True,
                score=100,
                evidence={
                    "audience_prior_id": prior.audience_prior_id,
                    "target_free_payload": True,
                    "perceptual_input_artifact_id": perceptual_input_artifact_id,
                    "serializer_version": AUDIENCE_PERCEPTUAL_SURFACE_VERSION,
                    "prompt_version": BLIND_READER_PROMPT_VERSION,
                    "perceptual_surface_hash": perceptual_input_content[
                        "perceptual_surface_hash"
                    ],
                    "prompt_payload_hash": perceptual_input_content[
                        "prompt_payload_hash"
                    ],
                },
            ),
        )
        observations.append(observation)
        observation_artifact_id = str(artifact["id"])
        artifact_ids.append(observation_artifact_id)
        observation_artifact_ids.append(observation_artifact_id)

    report = await _structured_call(
        system=_COMPARATOR_SYSTEM,
        prompt=_comparator_prompt(screenplay, observations, report_id=f"NRR-{episode_id}"),
        model_cls=NarrativeReviewReport,
        call_role="intent_comparator",
        episode_id=episode_id,
    )
    if report.decision == "pass":
        low = dict(report.low_percentile_result or {})
        expected_by_prior = {
            prior.audience_prior_id: {
                delta.target_delta_id
                for intent in plan.experience_intents
                for path in intent.audience_paths
                if path.audience_prior_id == prior.audience_prior_id
                for delta in path.target_deltas
            }
            for prior in plan.audience_priors
        }
        satisfied_by_prior = {
            prior_id: {
                item.target_delta_id
                for item in report.target_delta_results
                if item.audience_prior_id == prior_id
                and item.result == "satisfied"
            }
            for prior_id in expected_by_prior
        }
        if "passed" not in low:
            low["passed"] = all(
                expected and expected <= satisfied_by_prior.get(prior_id, set())
                for prior_id, expected in expected_by_prior.items()
            )
        if not isinstance(low.get("per_prior"), dict):
            low["per_prior"] = {
                prior_id: {
                    "passed": bool(
                        expected
                        and expected <= satisfied_by_prior.get(prior_id, set())
                    ),
                    "target_delta_ids": sorted(expected),
                    "reason": "按该先验逐目标比较结果确定",
                }
                for prior_id, expected in expected_by_prior.items()
            }
        low.setdefault("reason", "按逐先验目标比较结果确定低分位是否通过")
        report.low_percentile_result = low
    validation_errors = validate_blind_review(screenplay, observations, report)
    passed = not validation_errors and report.decision == "pass"
    if report.decision == "pass" and not report.target_delta_results and index.deltas:
        validation_errors.append("[REVIEW_RESULT_MISSING] pass 报告没有逐目标比较")
        passed = False
    report_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="narrative_review_report",
        scope_type="episode",
        scope_id=episode_id,
        status="validated" if passed else "needs_revision",
        trust_level="T2",
        content=report.model_dump(mode="json"),
        parent_artifact_ids=list(dict.fromkeys([
            *parents,
            *[
                artifact_id
                for pair in zip(
                    perceptual_input_artifact_ids,
                    observation_artifact_ids,
                )
                for artifact_id in pair
            ],
        ])),
        contract_version=NARRATIVE_CONTRACT_VERSION,
        prompt_version=COMPARATOR_PROMPT_VERSION,
    ))
    artifact_ids.append(str(report_artifact["id"]))
    issues = [_issue(message, episode_id) for message in validation_errors]
    issues.extend(
        _issue(
            f"[AUDIENCE_TARGET_MISSED] prior={result.audience_prior_id} "
            f"target_delta={result.target_delta_id} result={result.result}",
            episode_id,
        )
        for result in report.target_delta_results
        if result.result != "satisfied"
    )
    if report.decision != "pass" and not issues:
        issues = [_issue(
            f"[NARRATIVE_REVIEW_DECISION] 冷观众比较结论为 {report.decision}：{report.reason}",
            episode_id,
        )]
    evaluation = Evaluation(
        evaluator_type="model",
        evaluator_name="narrative_blind_comparator",
        evaluator_version=COMPARATOR_PROMPT_VERSION,
        status="passed" if passed else "failed",
        hard_gate_passed=passed,
        evaluation_role="runtime_gate",
        runtime_blocking=True,
        retry_eligible=not passed,
        score=100 if passed else 0,
        issues=issues,
        evidence={
            "report_artifact_id": report_artifact["id"],
            "perceptual_input_artifact_ids": perceptual_input_artifact_ids,
            "observation_artifact_ids": observation_artifact_ids,
            "perceptual_surface_version": AUDIENCE_PERCEPTUAL_SURFACE_VERSION,
            "blind_prompt_version": BLIND_READER_PROMPT_VERSION,
            "per_prior": sorted(index.priors),
            "low_percentile_result": report.low_percentile_result,
        },
        confidence=min((item.confidence for item in observations), default=0.0),
    )
    evidence_repository.create_evaluation(report_artifact["id"], evaluation)
    if storyboard_artifact_id:
        evidence_repository.create_evaluation(storyboard_artifact_id, evaluation)
    if not passed:
        raise NarrativeReviewError([issue.message for issue in issues])
    return observations, report, artifact_ids
