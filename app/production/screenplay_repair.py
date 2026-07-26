"""剧本 Production Repair Agent：Baseline 一次生成后只做局部 Patch。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.db import get_conn, now
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact, Issue
from app.production.grant import assert_grant_allows, issue_production_grant
from app.production.metrics import (
    record_activation,
    record_baseline_generation,
    record_issue_reopened,
)
from app.production.patch import (
    PatchOperation,
    PatchRequest,
    apply_screenplay_patch,
    load_screenplay_from_artifact,
    screenplay_artifact_payload,
)
from app.production.policy import assert_baseline_allowed
from app.production.publish import can_issue_certificate, publish_screenplay
from app.production.revision import (
    ensure_production_revision,
    get_production_revision,
    mark_baseline_generated,
    mark_first_evaluation,
    save_checkpoint,
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
SCREENPLAY_REPAIR_PLANNER_VERSION = "screenplay-repair-2"

_SCENE_STORY_FUNCTION_CODES = {
    "SCENE_FIELD_INVALID",
    "SCENE_STORY_FUNCTION_TOO_SHORT",
}
_SCENE_NUMBER_RE = re.compile(r"scene_outline\s*第\s*(\d+)\s*场|/scene_blocks/SC(\d+)", re.I)


def _eval_id_from_create(evaluation_row: dict[str, Any] | str | None) -> str:
    if isinstance(evaluation_row, dict):
        return str(evaluation_row.get("id") or "")
    return str(evaluation_row or "")


def _strategy_was_tried(entries: list[str], strategy: str) -> bool:
    """Recognize both current keys and legacy keys such as ``rederive:``."""
    return any(
        entry == strategy or entry.startswith(f"{strategy}:")
        for entry in entries
        if not entry.startswith(("fail:", "exhausted"))
    )


def _patch_strategy_key(ops: list[PatchOperation]) -> str:
    op = ops[0]
    kind = str((op.target or {}).get("kind") or "")
    if op.op == "rederive":
        return "rederive"
    if kind == "metadata":
        return f"fill_{op.path}"
    if kind in {"scene", "screenplay_scene"}:
        return f"fill_scene_{op.target.get('id')}_{op.path}"
    if kind == "information" and op.path == "event_id":
        return "fix_ledger_event"
    if kind == "dialogue_chain_turn" and op.path == "source_text":
        return "fix_opening_source_anchor"
    if op.op == "create_node" and kind == "dialogue_turn":
        return "insert_trigger"
    locator = op.path or str((op.target or {}).get("id") or "")
    return f"{op.op}:{locator}" if locator else op.op


def _strategy_attempt_count(entries: list[str]) -> int:
    return sum(
        1 for entry in entries
        if entry and not entry.startswith(("fail:", "exhausted"))
    )


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
        required_dialogue_lines=episode.get("required_dialogue_lines") or [],
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
    tried = list(history.get(fp) or [])
    path = str((issue.evidence or {}).get("path") or "")
    code = issue.code
    related = list((issue.evidence or {}).get("related_node_ids") or [])

    ops: list[PatchOperation] = []

    # 场级字段必须直接 Patch 源节点。rederive 只会重建投影，无法修复源字段。
    if (
        code in _SCENE_STORY_FUNCTION_CODES
        or "story_function" in path
        or "story_function" in (issue.message or "")
    ):
        scene_id, scene = _scene_from_issue(issue, script)
        strategy = f"fill_scene_{scene_id}_story_function" if scene_id else ""
        if scene is not None and strategy and not _strategy_was_tried(tried, strategy):
            value = _derive_scene_story_function(scene)
            if value and value != (scene.story_function or "").strip():
                return [PatchOperation(
                    op="replace_field",
                    path="story_function",
                    value=value,
                    target={"kind": "scene", "id": scene_id},
                )]

    # S0：派生字段问题 → rederive
    if code in {"FORMAT_CONTRACT_INVALID"} and "scene_outline" in (issue.message or ""):
        if not _strategy_was_tried(tried, "rederive"):
            return [PatchOperation(op="rederive")]

    # S1：戏剧契约单字段
    for field in ("stakes", "obstacle", "protagonist_goal", "dramatic_question"):
        if field in path or field in (issue.message or "") or (
            code == "DRAMATIC_CONTRACT_INCOMPLETE" and field in (issue.message or "").lower()
        ):
            strategy = f"fill_{field}"
            if _strategy_was_tried(tried, strategy):
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
        if info_id and event_ids and not _strategy_was_tried(tried, "fix_ledger_event"):
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

    # 原文开场对白锚点：只修 dialogue_chains[0].turns[0].source_text，
    # 不覆盖整条对白链，更不会重写正文。
    if code == "SOURCE_FIDELITY" and not _strategy_was_tried(
        tried, "fix_opening_source_anchor"
    ):
        opening = _extract_quoted_fragment(issue.message or "")
        if opening and script.dialogue_chains and script.dialogue_chains[0].turns:
            chain_id = script.dialogue_chains[0].chain_id
            return [PatchOperation(
                op="replace_field",
                path="source_text",
                value=opening,
                target={
                    "kind": "dialogue_chain_turn",
                    "id": f"{chain_id}-T1",
                    "chain_id": chain_id,
                    "turn_index": 0,
                },
            )]

    # key_lines / full_script_text 等派生投影才允许 rederive。
    if code == "KEY_LINE_MISSING" and not _strategy_was_tried(tried, "rederive"):
        return [PatchOperation(op="rederive")]

    # 对白链触发：尝试在首场插入 trigger turn
    if code == "KEY_LINE_MISSING" and not _strategy_was_tried(tried, "insert_trigger"):
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


def _scene_from_issue(issue: Issue, script: EpisodeScreenplay) -> tuple[str, Any | None]:
    evidence = issue.evidence or {}
    candidates = [
        str(node).upper()
        for node in (evidence.get("related_node_ids") or [])
        if re.fullmatch(r"SC\d+", str(node), re.I)
    ]
    if not candidates:
        text = f"{evidence.get('path') or ''} {issue.message or ''}"
        match = _SCENE_NUMBER_RE.search(text)
        if match:
            number = int(match.group(1) or match.group(2))
            candidates.append(f"SC{number:02d}")
    if not candidates:
        return "", None
    scene_id = candidates[0]
    scene_no = int(scene_id[2:])
    scene = next(
        (item for item in (script.scene_outline or []) if int(item.scene_no) == scene_no),
        None,
    )
    return scene_id, scene


def _derive_scene_story_function(scene: Any) -> str:
    """从本场已有事实确定性补全功能描述，不引入新剧情。"""

    def compact(value: Any, limit: int) -> str:
        text = re.sub(r"\s+", "", str(value or "")).strip("，。；;：:、 ")
        return text[:limit].rstrip("，。；;：:、 ")

    conflict = compact(getattr(scene, "conflict", ""), 20)
    turn = compact(getattr(scene, "turn", ""), 18)
    summary = compact(getattr(scene, "summary", ""), 24)
    heading = compact(getattr(scene, "scene_heading", ""), 16)
    if conflict and turn:
        return f"呈现{conflict}，推动{turn}"
    if summary and turn:
        return f"呈现{summary}，推动{turn}"
    if summary:
        return f"呈现{summary}并推进本场局势"
    if heading:
        return f"承接{heading}场景并推动本场局势变化"
    return "推动本场核心冲突并形成后续状态变化"


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
    required_dialogue_fingerprint = "|".join(
        str(line) for line in (episode.get("required_dialogue_lines") or [])
    )
    input_fp = hashlib.sha256(
        (
            f"{episode_id}|{source_text[:2000]}|"
            f"{project['bible_version'] if project else 0}|{required_dialogue_fingerprint}"
        ).encode()
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
    if checkpoint.get("planner_version") != SCREENPLAY_REPAIR_PLANNER_VERSION:
        # 新规划器必须能接管旧 working artifact；旧版 exhausted 不能永久封死新策略。
        checkpoint = {
            **checkpoint,
            "planner_version": SCREENPLAY_REPAIR_PLANNER_VERSION,
            "issue_strategy_history": {},
            "yield_reason": "planner_upgraded",
        }
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
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
            "screenplay_updated_at=? WHERE id=?",
            ("首次整版 Baseline 已落库，现只做局部 QA Patch", now(), episode_id),
        )
        conn.commit()
        if run_id:
            evidence_repository.append_event(
                run_id, "BASELINE_GENERATION_DONE", "info",
                "Baseline 已落库，进入 QA",
                payload={"artifact_id": baseline_art["id"], "revision_id": rev.id},
            )
    elif not rev.working_artifact_id:
        raise RuntimeError("revision 已有 baseline 计数但缺少 working artifact")

    # ---- Repair loop ----
    patches_this_activation = 0
    attempts_this_activation = 0
    prev_issue_fps: set[str] = set(checkpoint.get("last_issue_fingerprints") or [])

    while attempts_this_activation < MAX_REPAIR_ACTIVATION_PATCHES:
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
        if ops:
            proposed_key = _patch_strategy_key(ops)
            if _strategy_was_tried(
                strategy_history.get(issue.fingerprint, []), proposed_key
            ):
                ops = []
        if not ops:
            strategy_history.setdefault(issue.fingerprint, []).append("exhausted")
            if bool((issue.evidence or {}).get("requires_user_input", False)):
                _mark_waiting_input(episode_id, [issue], run_id=run_id)
                save_checkpoint(rev.id, {
                    **checkpoint,
                    "phase": "WAITING_INPUT",
                    "activation_no": activation_no,
                    "working_artifact_id": working_id,
                    "open_issue_ids": [i.fingerprint for i in issues],
                    "issue_strategy_history": strategy_history,
                    "patch_artifact_ids": patch_ids,
                    "last_issue_fingerprints": list(current_fps),
                    "yield_reason": "user_input_required",
                })
                raise RuntimeError(f"WAITING_INPUT: 无法自动解决 {issue.code}: {issue.message}")
            _mark_repair_failed(episode_id, issue, run_id=run_id)
            save_checkpoint(rev.id, {
                **checkpoint,
                "phase": "REPAIR_FAILED",
                "activation_no": activation_no,
                "working_artifact_id": working_id,
                "open_issue_ids": [i.fingerprint for i in issues],
                "issue_strategy_history": strategy_history,
                "patch_artifact_ids": patch_ids,
                "last_issue_fingerprints": list(current_fps),
                "yield_reason": "strategies_exhausted",
            })
            return script

        strategy_key = _patch_strategy_key(ops)
        strategy_history.setdefault(issue.fingerprint, []).append(strategy_key)
        attempts_this_activation += 1

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
                idempotency_key=f"{rev.id}:{issue.fingerprint}:{strategy_key}:{attempts_this_activation}",
                reason=issue.message[:200],
            ),
            episode_id=episode_id,
        )
        if not result.ok:
            strategy_history.setdefault(issue.fingerprint, []).append(
                f"fail:{strategy_key}:{(result.error or 'patch failed')[:160]}"
            )
            if "no-op" in (result.error or ""):
                prev_issue_fps = current_fps
                save_checkpoint(rev.id, {
                    **checkpoint,
                    "phase": "QA",
                    "activation_no": activation_no,
                    "working_artifact_id": working_id,
                    "open_issue_ids": [i.fingerprint for i in issues],
                    "issue_strategy_history": strategy_history,
                    "patch_artifact_ids": patch_ids,
                    "last_issue_fingerprints": list(current_fps),
                    "yield_reason": "noop_rejected",
                })
                if _strategy_attempt_count(
                    strategy_history.get(issue.fingerprint, [])
                ) >= MAX_STRATEGY_ATTEMPTS_PER_ISSUE:
                    _mark_repair_failed(episode_id, issue, run_id=run_id)
                    save_checkpoint(rev.id, {
                        **checkpoint,
                        "phase": "REPAIR_FAILED",
                        "activation_no": activation_no,
                        "working_artifact_id": working_id,
                        "open_issue_ids": [i.fingerprint for i in issues],
                        "issue_strategy_history": strategy_history,
                        "patch_artifact_ids": patch_ids,
                        "last_issue_fingerprints": list(current_fps),
                        "yield_reason": "no_progress",
                    })
                    return script
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
            payload={
                "activation_no": activation_no,
                "patches": patches_this_activation,
                "attempts": attempts_this_activation,
            },
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
