"""剧本 Production Repair Agent：Baseline 一次生成后只做局部 Patch。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.db import get_conn, now
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.production.certificate import issue_completion_certificate
from app.production.grant import assert_grant_allows, issue_production_grant
from app.production.metrics import (
    record_activation,
    record_baseline_generation,
    record_issue_reopened,
    record_patch,
)
from app.production.patch import (
    PatchOperation,
    PatchRequest,
    apply_screenplay_patch,
    load_screenplay_from_artifact,
    screenplay_artifact_payload,
)
from app.production.policy import assert_baseline_allowed, deny_full_regen_after_qa
from app.production.publish import can_issue_certificate, publish_screenplay
from app.production.revision import (
    ensure_production_revision,
    get_production_revision,
    mark_baseline_generated,
    mark_first_evaluation,
    save_checkpoint,
    update_working_artifact,
)
from app.production.screenplay_document import (
    document_to_screenplay,
    rederive_projections,
    screenplay_to_document,
)
from app.production.structured_issues import (
    blocker_count,
    enrich_issues,
    issue_set_hash,
    issues_from_validator_messages,
    must_fix_count,
)
from app.schemas import Bible, EpisodeScreenplay


MAX_REPAIR_ACTIVATION_PATCHES = 12
MAX_STRATEGY_ATTEMPTS_PER_ISSUE = 3


def _eval_id_from_create(evaluation_row: dict[str, Any] | str | None) -> str:
    if isinstance(evaluation_row, dict):
        return str(evaluation_row.get("id") or "")
    return str(evaluation_row or "")


def run_screenplay_qa(
    script: EpisodeScreenplay,
    *,
    bible: Bible,
    source_text: str,
    episode: dict[str, Any],
    artifact_id: str | None = None,
    artifact_hash: str | None = None,
) -> tuple[list[Issue], Evaluation]:
    from app import config
    from app.stages import adaptation_hook_errors
    from app.validators import validate_screenplay

    expected = max(1, int(episode.get("target_duration_s") or 50) // config.VIDEO_DURATION_MIN_S)
    messages = validate_screenplay(
        script, bible, expected,
        episode_no=episode.get("episode_no"),
        source_text=source_text,
        require_dialogue_chains=True,
    )
    messages.extend(adaptation_hook_errors(script, episode))
    issues = issues_from_validator_messages(
        list(dict.fromkeys(messages)),
        subject="screenplay",
        stage="screenplay",
    )
    issues = enrich_issues(issues, stage="screenplay", artifact_id=artifact_id)
    for issue in issues:
        if artifact_hash:
            issue.evidence["artifact_hash"] = artifact_hash
    status = "passed" if not issues else "failed"
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="screenplay_production_qa",
        evaluator_version="production-repair-1",
        status=status,
        hard_gate_passed=not issues,
        score=100.0 if not issues else max(0.0, 100.0 - 10.0 * len(issues)),
        issues=issues,
        evidence={
            "artifact_id": artifact_id,
            "artifact_hash": artifact_hash,
            "blocker_count": blocker_count(issues),
            "must_fix_count": must_fix_count(issues),
        },
    )
    return issues, evaluation


def plan_screenplay_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    strategy_history: dict[str, list[str]] | None = None,
) -> list[PatchOperation]:
    """最小范围 Patch 规划（确定性优先，避免整对象替换）。"""
    history = strategy_history or {}
    fp = issue.fingerprint
    tried = set(history.get(fp) or [])
    path = str((issue.evidence or {}).get("path") or "")
    code = issue.code
    related = list((issue.evidence or {}).get("related_node_ids") or [])

    ops: list[PatchOperation] = []

    # S0：派生字段问题 → rederive
    if code in {"FORMAT_CONTRACT_INVALID"} and "scene_outline" in (issue.message or ""):
        if "rederive" not in tried:
            return [PatchOperation(op="rederive")]

    # S1：戏剧契约单字段
    for field in ("stakes", "obstacle", "protagonist_goal", "dramatic_question"):
        if field in path or field in (issue.message or "") or (
            code == "DRAMATIC_CONTRACT_INCOMPLETE" and field in (issue.message or "").lower()
        ):
            strategy = f"fill_{field}"
            if strategy in tried:
                continue
            value = _heuristic_fill_dramatic_field(field, script)
            if value:
                return [PatchOperation(
                    op="replace_field",
                    path=field,
                    value=value,
                    target={"kind": "metadata", "id": field},
                )]

    # ledger event_id
    if code == "LEDGER_INVALID" or "event_id" in (issue.message or ""):
        info_id = next((n for n in related if n.startswith("I")), "")
        event_ids = [e.event_id for e in (script.events or []) if e.event_id]
        if info_id and event_ids and "fix_ledger_event" not in tried:
            return [PatchOperation(
                op="replace_field",
                path="event_id",
                value=event_ids[0],
                target={"kind": "information", "id": info_id},
            )]

    # missing dramatic fields from message patterns
    if code == "DRAMATIC_CONTRACT_INCOMPLETE":
        msg = issue.message or ""
        for field in ("stakes", "obstacle", "protagonist_goal", "dramatic_question"):
            if field in msg:
                value = _heuristic_fill_dramatic_field(field, script)
                if value:
                    return [PatchOperation(
                        op="replace_field",
                        path=field,
                        value=value,
                        target={"kind": "metadata", "id": field},
                    )]

    # 默认：rederive（安全、无内容改写）若尚未试过
    if "rederive" not in tried:
        return [PatchOperation(op="rederive")]

    # 对白链触发：尝试在首场插入 trigger turn
    if code == "KEY_LINE_MISSING" and "insert_trigger" not in tried:
        scene_id = next((n for n in related if n.startswith("SC")), None)
        if not scene_id and script.scene_outline:
            scene_id = f"SC{int(script.scene_outline[0].scene_no):02d}"
        line = _extract_quoted_fragment(issue.message or "")
        if scene_id and line:
            return [PatchOperation(
                op="create_node",
                target={"kind": "dialogue_turn", "scene_id": scene_id},
                value={
                    "chain_id": "DC_FIX",
                    "speaker": _guess_speaker(line, script),
                    "line": line,
                    "function": "trigger",
                    "source_text": line,
                },
            )]

    return ops


def _heuristic_fill_dramatic_field(field: str, script: EpisodeScreenplay) -> str:
    spine = script.plot_spine
    premise = (spine.episode_premise if spine else "") or script.episode_premise or script.logline or ""
    ending = (spine.must_keep_ending if spine else "") or script.ending_hook or ""
    if field == "stakes":
        if script.stakes.strip():
            return ""
        base = premise or ending or script.title
        return f"若失败将无法推进「{base[:40]}」，失去本集目标与立场。"
    if field == "obstacle":
        if script.obstacle.strip():
            return ""
        return f"外部阻力与内心犹豫阻碍实现：{premise[:60] or script.title}"
    if field == "protagonist_goal":
        if script.protagonist_goal.strip():
            return ""
        who = ""
        if spine and spine.spine_beats:
            who = spine.spine_beats[0].who
        return f"{who or '主角'}完成本集目标：{premise[:60] or ending[:60]}"
    if field == "dramatic_question":
        if script.dramatic_question.strip():
            return ""
        return f"主角能否在阻力下完成：{premise[:50] or script.title}？"
    return ""


def _extract_quoted_fragment(message: str) -> str:
    for pattern in (r"[「『]([^」』]+)[」』]", r"[“\"]([^”\"]+)[”\"]", r"：([^\s；;]{2,40})"):
        m = re.search(pattern, message)
        if m:
            return m.group(1).strip()
    return ""


def _guess_speaker(line: str, script: EpisodeScreenplay) -> str:
    for chain in script.dialogue_chains or []:
        for turn in chain.turns:
            if turn.line and turn.line in line:
                return turn.speaker or "角色"
    if script.scene_outline and script.scene_outline[0].characters:
        return script.scene_outline[0].characters[0]
    return "角色"


async def ensure_source_characters_incremental(
    episode_id: str,
    source_text: str,
    draft_text: str = "",
) -> dict[str, Any]:
    """增量追加 source-backed 角色，不触发完整 regenerate。"""
    from app.domain import screenplay_ops
    return await screenplay_ops._screenplay_character_discovery(
        episode_id, source_text, draft_text=draft_text,
    )


async def run_screenplay_production(
    *,
    episode_id: str,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    prev_ending: str = "",
    run_id: str | None = None,
    resume: bool = True,
) -> EpisodeScreenplay:
    """一次 Baseline + 局部修复直到证书发布（或 WAITING_INPUT）。"""
    from app.harness.contracts import get_contract
    from app.stages import generate_screenplay_baseline

    conn = get_conn()
    project = conn.execute(
        "SELECT id, bible_version FROM projects WHERE id=?", (episode["project_id"],)
    ).fetchone()
    contract = get_contract("screenplay")
    input_fp = hashlib.sha256(
        f"{episode_id}|{source_text[:2000]}|{project['bible_version'] if project else 0}".encode()
    ).hexdigest()

    rev = ensure_production_revision(
        episode_id=episode_id,
        kind="screenplay",
        input_fingerprint=input_fp,
        contract_version=contract.version,
        qa_profile_version="screenplay-qa-1",
        resume=resume,
    )
    # 签发 Production Grant
    if not rev.grant_id:
        grant, _token = issue_production_grant(
            episode_id=episode_id,
            project_id=episode["project_id"],
            production_revision_id=rev.id,
            kind="screenplay",
            input_artifact_hash="",
        )
        rev = get_production_revision(rev.id)  # type: ignore[assignment]

    checkpoint = dict(rev.checkpoint_json or {})
    strategy_history: dict[str, list[str]] = dict(checkpoint.get("issue_strategy_history") or {})
    patch_ids: list[str] = list(checkpoint.get("patch_artifact_ids") or [])
    activation_no = int(checkpoint.get("activation_no") or 0) + 1
    record_activation(kind="screenplay", episode_id=episode_id, activation_no=activation_no)

    # ---- Baseline（仅一次）----
    if not rev.baseline_done:
        assert_baseline_allowed(rev, command="screenplay.generate", episode_id=episode_id)
        if run_id:
            evidence_repository.append_event(
                run_id, "BASELINE_GENERATION_STARTED", "info",
                "剧本 Baseline 生成（本 revision 仅此一次）",
                payload={"revision_id": rev.id},
            )
        script = await generate_screenplay_baseline(
            episode, source_text, bible, prev_ending=prev_ending,
        )
        # 增量人物：只追加 Bible，不二次完整生成
        draft_audit = await ensure_source_characters_incremental(
            episode_id, source_text, draft_text=script.model_dump_json(),
        )
        if draft_audit.get("added"):
            # 重新加载 bible，仅 patch 受影响的 speaker/voice（最小）
            p = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
            from app.domain.common import _project_bible_or_placeholder
            bible = _project_bible_or_placeholder(p)

        from app.validators import normalize_screenplay_ledgers
        normalize_screenplay_ledgers(script)
        payload = screenplay_artifact_payload(script)
        baseline_art = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_document",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T1",
                content=payload,
                contract_version=contract.version,
            )
        )
        rev = mark_baseline_generated(
            rev.id,
            baseline_artifact_id=baseline_art["id"],
            working_artifact_id=baseline_art["id"],
        )
        record_baseline_generation(
            kind="screenplay", episode_id=episode_id, revision_id=rev.id,
        )
        if run_id:
            evidence_repository.append_event(
                run_id, "BASELINE_GENERATION_DONE", "info",
                "Baseline 已落库，进入 QA",
                payload={"artifact_id": baseline_art["id"], "revision_id": rev.id},
            )
    else:
        deny_full_regen_after_qa(rev, command="screenplay.generate", episode_id=episode_id)
        if not rev.working_artifact_id:
            raise RuntimeError("revision 已有 baseline 计数但缺少 working artifact")

    # ---- Repair loop ----
    patches_this_activation = 0
    prev_issue_fps: set[str] = set(checkpoint.get("last_issue_fingerprints") or [])

    while patches_this_activation < MAX_REPAIR_ACTIVATION_PATCHES:
        rev = get_production_revision(rev.id)  # type: ignore[assignment]
        working_id = rev.working_artifact_id
        assert working_id
        art = evidence_repository.get_artifact(working_id)
        assert art
        artifact_hash = art.get("content_hash") or evidence_repository.content_hash(art.get("content"))
        script = load_screenplay_from_artifact(working_id)

        issues, evaluation = run_screenplay_qa(
            script,
            bible=bible,
            source_text=source_text,
            episode=episode,
            artifact_id=working_id,
            artifact_hash=artifact_hash,
        )
        eval_row = evidence_repository.create_evaluation(working_id, evaluation)
        eval_id = _eval_id_from_create(eval_row)
        if not rev.first_evaluation_done:
            rev = mark_first_evaluation(rev.id, eval_id or f"eval-{working_id}")

        current_fps = {i.fingerprint for i in issues}
        reopened = prev_issue_fps & current_fps
        # reopened means previously cleared then came back - track when we had improvement
        if checkpoint.get("cleared_fingerprints"):
            for fp in set(checkpoint["cleared_fingerprints"]) & current_fps:
                record_issue_reopened(kind="screenplay", episode_id=episode_id, fingerprint=fp)
                reopened.add(fp)

        if can_issue_certificate(issues):
            if run_id:
                evidence_repository.append_event(
                    run_id, "CERTIFYING", "info", "剧本 QA 全部通过，签发完成凭证",
                    payload={"artifact_id": working_id, "evaluation_id": eval_id},
                )
            # 首次发布清空下游；若已有 published 且同 hash 则仍走 publish
            result = publish_screenplay(
                episode_id=episode_id,
                revision_id=rev.id,
                artifact_id=working_id,
                artifact_hash=artifact_hash,
                evaluation_ids=[eval_id] if eval_id else [],
                input_fingerprint=rev.input_fingerprint,
                contract_version=rev.contract_version,
                qa_profile_version=rev.qa_profile_version,
                clear_downstream=True,
            )
            if run_id:
                evidence_repository.append_event(
                    run_id, "PUBLISHED", "info", "可交付剧本已发布",
                    payload=result,
                )
            save_checkpoint(rev.id, {
                **checkpoint,
                "phase": "SUCCEEDED",
                "activation_no": activation_no,
                "working_artifact_id": working_id,
                "open_issue_ids": [],
                "issue_strategy_history": strategy_history,
                "patch_artifact_ids": patch_ids,
            })
            return load_screenplay_from_artifact(working_id)

        # 选择最高依赖 Issue
        issue = _choose_issue(issues)
        if issue is None or not issue.repairable:
            _mark_waiting_input(episode_id, issues, run_id=run_id)
            save_checkpoint(rev.id, {
                **checkpoint,
                "phase": "WAITING_INPUT",
                "activation_no": activation_no,
                "working_artifact_id": working_id,
                "open_issue_ids": [i.fingerprint for i in issues],
                "issue_strategy_history": strategy_history,
                "patch_artifact_ids": patch_ids,
                "last_issue_fingerprints": list(current_fps),
            })
            # 不交付 warning；状态 repairing
            conn.execute(
                "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
                "screenplay_updated_at=? WHERE id=?",
                (
                    "自动修复等待输入：" + "；".join(i.message for i in issues[:3]),
                    now(),
                    episode_id,
                ),
            )
            conn.commit()
            raise RuntimeError("WAITING_INPUT: " + issues[0].message if issues else "WAITING_INPUT")

        ops = plan_screenplay_patch(issue, script, strategy_history=strategy_history)
        if not ops:
            # 扩大一层：尝试 LLM 单字段修补
            ops = await _llm_field_patch(issue, script, source_text=source_text)
        if not ops:
            strategy_history.setdefault(issue.fingerprint, []).append("exhausted")
            if len(strategy_history[issue.fingerprint]) >= MAX_STRATEGY_ATTEMPTS_PER_ISSUE:
                _mark_waiting_input(episode_id, [issue], run_id=run_id)
                raise RuntimeError(f"WAITING_INPUT: 无法局部修复 {issue.code}: {issue.message}")
            # 记一次空策略并继续下一 issue
            prev_issue_fps = current_fps
            continue

        strategy_key = ops[0].op + ":" + (ops[0].path or ops[0].target.get("id", ""))
        strategy_history.setdefault(issue.fingerprint, []).append(strategy_key)

        if rev.grant_id:
            assert_grant_allows(rev.grant_id, command="screenplay.patch", episode_id=episode_id)

        if run_id:
            evidence_repository.append_event(
                run_id, "APPLYING_PATCH", "info",
                f"局部修复 {issue.code} @ {issue.evidence.get('path')}",
                payload={
                    "issue": issue.model_dump(mode="json"),
                    "operations": [o.model_dump(mode="json") for o in ops],
                },
            )

        result = apply_screenplay_patch(
            PatchRequest(
                production_revision_id=rev.id,
                expected_artifact_id=working_id,
                expected_hash=artifact_hash,
                issue_set_hash=issue_set_hash(issues),
                operations=ops,
                idempotency_key=f"{rev.id}:{issue.fingerprint}:{strategy_key}:{patches_this_activation}",
                reason=issue.message[:200],
            ),
            episode_id=episode_id,
        )
        if not result.ok:
            strategy_history.setdefault(issue.fingerprint, []).append(f"fail:{result.error}")
            if "no-op" in (result.error or ""):
                prev_issue_fps = current_fps
                continue
            # CAS 冲突：重新观察
            if "CAS" in (result.error or "") or "hash" in (result.error or "").lower():
                continue
            raise RuntimeError(result.error or "patch failed")

        if result.patch_artifact_id:
            patch_ids.append(result.patch_artifact_id)
        patches_this_activation += 1
        cleared = prev_issue_fps - current_fps
        checkpoint["cleared_fingerprints"] = list(
            set(checkpoint.get("cleared_fingerprints") or []) | cleared
        )
        prev_issue_fps = current_fps
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
            "screenplay_updated_at=? WHERE id=?",
            (
                f"自动修复中：已处理 {patches_this_activation} 次补丁，剩余问题 {len(issues)}",
                now(),
                episode_id,
            ),
        )
        conn.commit()
        save_checkpoint(rev.id, {
            **checkpoint,
            "phase": "QA",
            "activation_no": activation_no,
            "working_artifact_id": result.after_artifact_id,
            "open_issue_ids": [i.fingerprint for i in issues],
            "issue_strategy_history": strategy_history,
            "patch_artifact_ids": patch_ids,
            "last_issue_fingerprints": list(current_fps),
            "last_touched": result.touched_node_ids,
        })

    # activation 预算用尽：checkpoint 并保持 repairing，由调度器续跑
    save_checkpoint(rev.id, {
        **checkpoint,
        "phase": "WAITING_RETRY",
        "activation_no": activation_no,
        "issue_strategy_history": strategy_history,
        "patch_artifact_ids": patch_ids,
        "yield_reason": "activation_budget",
    })
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
        ("自动修复让出：将自动续跑", now(), episode_id),
    )
    conn.commit()
    if run_id:
        evidence_repository.append_event(
            run_id, "REPAIR_YIELD", "info",
            "单次 activation 预算用尽，已写 checkpoint，等待自动续跑",
            payload={"activation_no": activation_no, "patches": patches_this_activation},
        )
    # 返回当前工作副本（未发布）——调用方不得把它当 ready
    rev = get_production_revision(rev.id)  # type: ignore[assignment]
    return load_screenplay_from_artifact(rev.working_artifact_id)  # type: ignore[arg-type]


def get_active_safe(episode_id: str):
    from app.production.revision import get_active_production_revision
    return get_active_production_revision(episode_id, "screenplay")


def _choose_issue(issues: list[Issue]) -> Issue | None:
    if not issues:
        return None
    priority = {
        "SCHEMA_INVALID": 0,
        "DRAMATIC_CONTRACT_INCOMPLETE": 1,
        "LEDGER_INVALID": 2,
        "KEY_LINE_MISSING": 3,
        "PLOT_SPINE_INVALID": 4,
        "CHARACTER_CONSISTENCY": 5,
        "FORMAT_CONTRACT_INVALID": 6,
        "SOURCE_FIDELITY": 7,
    }
    repairable = [i for i in issues if i.repairable]
    pool = repairable or issues
    return sorted(pool, key=lambda i: priority.get(i.code, 50))[0]


def _mark_waiting_input(episode_id: str, issues: list[Issue], *, run_id: str | None) -> None:
    if run_id:
        evidence_repository.append_event(
            run_id, "WAITING_INPUT", "warning",
            "存在不可自动修复的真实冲突，需用户决定",
            payload={"issues": [i.model_dump(mode="json") for i in issues[:5]]},
        )


async def _llm_field_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
) -> list[PatchOperation]:
    """受限 LLM：只产出单字段 JSON patch，失败则返回空。"""
    path = str((issue.evidence or {}).get("path") or "")
    field = path.strip("/").split("/")[-1] if path else ""
    dramatic_fields = {"stakes", "obstacle", "protagonist_goal", "dramatic_question"}
    if field not in dramatic_fields and issue.code != "DRAMATIC_CONTRACT_INCOMPLETE":
        # 尝试从 message 推断
        for f in dramatic_fields:
            if f in (issue.message or ""):
                field = f
                break
    if field not in dramatic_fields:
        return []
    value = _heuristic_fill_dramatic_field(field, script)
    if not value:
        return []
    return [PatchOperation(
        op="replace_field",
        path=field,
        value=value,
        target={"kind": "metadata", "id": field},
    )]
