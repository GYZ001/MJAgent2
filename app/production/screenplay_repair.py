"""剧本 Production Repair Agent：Baseline 一次生成后只做局部 Patch。"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any

from app import config, textmatch
from app.character_policy import resolution_declares_functional_identity
from app.db import get_conn, get_setting, now
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
    rebind_input_fingerprint,
    save_checkpoint,
    update_working_artifact,
)
from app.production.structured_issues import (
    blocker_count,
    enrich_issues,
    issue_set_hash,
    issues_from_validator_messages,
    must_fix_count,
    structured_issue,
)
from app.schemas import Bible, EpisodeScreenplay
from app.renderability import DIALOGUE_CHAIN_TURNS_HARD_MAX


MAX_REPAIR_ACTIVATION_PATCHES = 12
MAX_REPAIR_ACTIVATION_PASSES = 32
MAX_STRATEGY_ATTEMPTS_PER_ISSUE = 5
SCREENPLAY_REPAIR_PLANNER_VERSION = "screenplay-repair-16"


class ScreenplayIdentityGateError(RuntimeError):
    """人物身份未解决时保留可操作的剧本阶段诊断。"""


class ScreenplayNarrativeGateError(RuntimeError):
    """修复耗尽仍未通过硬门禁；工作稿保留，但绝不发布。"""

_SCENE_STORY_FUNCTION_CODES = {
    "SCENE_STORY_FUNCTION_TOO_SHORT",
}
_SCENE_NUMBER_RE = re.compile(r"scene_outline\s*第\s*(\d+)\s*场|/scene_blocks/SC(\d+)", re.I)
_DIALOGUE_SOURCE_MISMATCH_RE = re.compile(
    r"dialogue_chains\[(\d+)\]\.turns\[(\d+)\]\.source_text\s+"
    r"(?:未在本集原文中找到|与改编台词语义不匹配)"
)
_SOURCE_SPAN_EXACT_MISMATCH_RE = re.compile(
    r"\[SOURCE_SPAN_EXACT_MISMATCH\]\s+([^\s.。:：]+)"
)
_SOURCE_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_SOURCE_EVIDENCE_STOP_CHARS = set(
    "的一了是在有和与把被就都还又只这那个人我们你他她它说问答"
)


def _eval_id_from_create(evaluation_row: dict[str, Any] | str | None) -> str:
    if isinstance(evaluation_row, dict):
        return str(evaluation_row.get("id") or "")
    return str(evaluation_row or "")


def non_waivable_screenplay_issues(issues: list[Issue]) -> list[Issue]:
    """Select runtime gates from issue attributes, never from a code whitelist."""
    return [
        issue
        for issue in issues
        if (
            bool((issue.evidence or {}).get("must_fix", False))
            or bool((issue.evidence or {}).get("runtime_blocking", False))
        )
    ]


def screenplay_identity_gate_issues(issues: list[Issue]) -> list[Issue]:
    """Return identity-specific gates using their structured owner metadata."""
    return [
        issue
        for issue in non_waivable_screenplay_issues(issues)
        if (
            str((issue.evidence or {}).get("path") or "").startswith(
                "/character_identities/"
            )
            or str(
                (issue.evidence or {}).get("rule_id") or ""
            ) == "character_identity_must_resolve_before_publish"
        )
    ]


def _gate_failure_message(
    open_issues: list[Issue],
    *,
    failed_issue: Issue | None,
) -> str:
    """Put the issue that actually stopped repair ahead of the remaining backlog."""
    ordered: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for issue in ([failed_issue] if failed_issue is not None else []) + open_issues:
        identity = (issue.fingerprint, issue.message)
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(issue)

    prefix = "剧本工作稿已保留，但叙事/质量硬门禁仍未通过，禁止发布："
    if failed_issue is not None:
        prefix += f"自动修复停止于 {failed_issue.code}："
    return (prefix + "；".join(issue.message for issue in ordered[:5]))[:1200]


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
    if op.op == "split_dialogue_chain_by_scene":
        return f"split_dialogue_chain_{(op.target or {}).get('chain_id') or 'unknown'}"
    if kind == "metadata":
        return f"fill_{op.path}"
    if kind in {"scene", "screenplay_scene"}:
        return f"fill_scene_{op.target.get('id')}_{op.path}"
    if kind == "information" and op.path == "event_id":
        return "fix_ledger_event"
    if kind == "dialogue_chain_turn" and op.path == "source_text":
        return str(
            (op.target or {}).get("strategy")
            or (
                f"fix_dialogue_source_{op.target.get('chain_id')}_"
                f"{op.target.get('turn_index')}"
            )
        )
    if kind == "dialogue_chain_turn" and op.path == "function":
        return (
            f"fix_dialogue_function_{op.target.get('chain_id')}_"
            f"{op.target.get('turn_index')}"
        )
    if (
        kind == "narrative_node"
        and str((op.target or {}).get("collection") or "") == "source_evidence"
        and op.path in {"source_span", "verbatim_excerpt"}
    ):
        return str(
            (op.target or {}).get("strategy")
            or f"fix_source_span_{(op.target or {}).get('id') or 'unknown'}"
        )
    if kind == "narrative_node":
        return (
            f"{op.op}:{(op.target or {}).get('id') or 'unknown'}:"
            f"{op.path or 'node'}"
        )
    if op.op == "create_node" and kind == "dialogue_turn":
        return "insert_trigger"
    if op.op == "create_node" and kind == "action_block":
        return str(
            (op.target or {}).get("strategy")
            or f"create_action_{(op.target or {}).get('id') or 'unknown'}"
        )
    locator = op.path or str((op.target or {}).get("id") or "")
    return f"{op.op}:{locator}" if locator else op.op


def _strategy_attempt_count(entries: list[str]) -> int:
    return sum(
        1 for entry in entries
        if entry and not entry.startswith(("fail:", "exhausted"))
    )


def _source_evidence_contexts(script: EpisodeScreenplay) -> dict[str, list[str]]:
    plan = script.narrative_plan
    if plan is None:
        return {}
    contexts: dict[str, list[str]] = {}
    for proposition in plan.propositions or []:
        statement = str(proposition.canonical_statement or "").strip()
        if not statement:
            continue
        for evidence_id in proposition.direct_source_evidence_ids or []:
            contexts.setdefault(str(evidence_id), []).append(statement)
    return contexts


def _source_span_issue_evidence_id(issue: Issue) -> str:
    match = _SOURCE_SPAN_EXACT_MISMATCH_RE.search(issue.message or "")
    return match.group(1).strip() if match else ""


def _plan_source_span_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
    tried: list[str],
) -> list[PatchOperation]:
    if script.narrative_plan is None or not source_text:
        return []
    evidence_id = _source_span_issue_evidence_id(issue)
    if not evidence_id:
        return []
    strategy = f"fix_source_span_{evidence_id}"
    if _strategy_was_tried(tried, strategy):
        return []
    evidence = next(
        (
            item for item in script.narrative_plan.source_evidence
            if item.source_evidence_id == evidence_id
        ),
        None,
    )
    if evidence is None:
        return []
    contexts = _source_evidence_contexts(script)
    resolved = _source_evidence_span(
        source_text,
        evidence.verbatim_excerpt,
        context=" ".join(contexts.get(evidence_id, [])),
    )
    if resolved is None:
        return []
    start, end, expanded_excerpt = resolved
    target = {
        "kind": "narrative_node",
        "collection": "source_evidence",
        "id": evidence_id,
        "strategy": strategy,
    }
    operations: list[PatchOperation] = []
    if (
        expanded_excerpt is not None
        and expanded_excerpt != evidence.verbatim_excerpt
    ):
        operations.append(PatchOperation(
            op="replace_field",
            path="verbatim_excerpt",
            value=expanded_excerpt,
            target=target,
        ))
    current_span = evidence.source_span.model_dump(mode="json")
    if current_span.get("start") != start or current_span.get("end") != end:
        operations.append(PatchOperation(
            op="replace_field",
            path="source_span",
            value={**current_span, "start": start, "end": end},
            target=target,
        ))
    return operations


def _best_scene_for_spine_beat(
    script: EpisodeScreenplay,
    *,
    beat_index: int,
    who: str,
    does: str,
) -> str:
    """Place one authoritative spine action in the closest existing scene."""
    scenes = list(script.scene_outline or [])
    if not scenes:
        return ""
    beat_text = f"{who}{does}"
    ranked: list[tuple[float, int, int, str]] = []
    for index, scene in enumerate(scenes):
        scene_text = " ".join([
            scene.story_function or "",
            scene.summary or "",
            scene.conflict or "",
            scene.turn or "",
            scene.source_basis or "",
        ])
        semantic_score = max(
            textmatch.longest_run_ratio(beat_text, scene_text),
            textmatch.bigram_coverage(beat_text, scene_text),
        )
        actor_score = int(
            bool(who)
            and any(
                who == character
                or who in character
                or character in who
                for character in (scene.characters or [])
            )
        )
        ordinal_distance = abs(index - min(beat_index, len(scenes) - 1))
        ranked.append((
            semantic_score,
            actor_score,
            -ordinal_distance,
            f"SC{int(scene.scene_no):02d}",
        ))
    ranked.sort(reverse=True)
    return ranked[0][3]


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
    from app.narrative import validate_screenplay_narrative
    from app.stages import adaptation_hook_errors
    from app.validators import (
        validate_screenplay,
        validate_screenplay_source_coverage,
    )
    from app.harness.contracts import get_contract
    from app.production.screenplay_authority import (
        SCREENPLAY_QA_PROFILE_VERSION,
        screenplay_contract_requires_narrative,
        screenplay_authority_fingerprint,
    )

    contract_version = str(
        episode.get("screenplay_contract_version")
        or get_contract("screenplay").version
    )
    authority_error = ""
    try:
        authority_input_fingerprint = screenplay_authority_fingerprint(
            str(episode.get("id") or ""),
            source_text=source_text,
            bible=bible,
            contract_version=contract_version,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        )
    except ValueError as exc:
        # Isolated unit tests may validate a detached screenplay fixture.  A
        # real persisted episode, however, must never fall back when its source
        # authority disagrees with the QA input.
        if "episode not found" in str(exc):
            fallback_material = {
                "authority_contract": "screenplay-source-authority.v1",
                "episode_id": str(episode.get("id") or ""),
                "source_text": source_text,
                "bible": bible.model_dump(mode="json"),
                "constraints": {
                    key: episode.get(key)
                    for key in (
                        "title", "hook", "cliffhanger", "synopsis",
                        "target_duration_s", "character_resolutions",
                    )
                },
                "contract_version": contract_version,
                "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
            }
            authority_input_fingerprint = hashlib.sha256(
                json.dumps(
                    fallback_material,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        else:
            authority_error = str(exc)
            authority_input_fingerprint = ""

    expected = max(1, int(episode.get("target_duration_s") or 50) // config.VIDEO_DURATION_MIN_S)
    character_resolutions = list(
        episode.get("character_resolutions") or []
    )
    functional_identity_names = {
        str(item.get("canonical_name") or "").strip()
        for item in character_resolutions
        if (
            isinstance(item, dict)
            and resolution_declares_functional_identity(item)
            and str(item.get("canonical_name") or "").strip()
        )
    }
    messages = validate_screenplay(
        script, bible, expected,
        episode_no=episode.get("episode_no"),
        source_text=source_text,
        require_dialogue_chains=True,
        validate_narrative=False,
        functional_identity_names=functional_identity_names,
    )
    source_chapter_contract_present = "source_chapters" in episode
    raw_source_chapters = episode.get("source_chapters") or []
    if isinstance(raw_source_chapters, str):
        try:
            raw_source_chapters = json.loads(raw_source_chapters)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_source_chapters = []
    authorized_source_chapter_ids = [
        str(value)
        for value in raw_source_chapters
        if str(value or "").strip()
    ]
    authorized_source_chapters = None
    episode_id = str(episode.get("id") or "")
    if source_chapter_contract_present and episode_id:
        try:
            from app.production.screenplay_authority import (
                screenplay_authorized_source_chapters,
            )

            authorized_source_chapters = screenplay_authorized_source_chapters(
                episode_id,
            )
        except ValueError:
            authorized_source_chapters = None
    requires_narrative = screenplay_contract_requires_narrative(contract_version)
    if script.narrative_plan is not None:
        messages.extend(validate_screenplay_narrative(
            script,
            require=True,
            source_text=source_text,
            expected_scope_id=str(episode.get("id") or script.id or "") or None,
            authorized_source_chapter_ids=(
                authorized_source_chapter_ids
                if source_chapter_contract_present
                and authorized_source_chapters is None
                else None
            ),
            authorized_source_chapters=authorized_source_chapters,
        ))
    elif requires_narrative:
        messages.append(
            "[NARRATIVE_PLAN_REQUIRED] 当前剧本合同要求 narrative_plan；"
            "缺失时禁止按 legacy 路径发布"
        )
    else:
        messages.extend(validate_screenplay_source_coverage(
            script,
            source_text,
        ))
    from app.portraits import (
        screenplay_character_resolution_errors,
        screenplay_unknown_identity_errors,
    )
    identity_messages = screenplay_character_resolution_errors(
        script, character_resolutions,
    )
    identity_messages.extend(screenplay_unknown_identity_errors(
        script,
        bible,
        character_resolutions,
    ))
    messages.extend(adaptation_hook_errors(script, episode))
    if authority_error:
        messages.append(f"[SCREENPLAY_SOURCE_AUTHORITY_MISMATCH] {authority_error}")
    issues = enrich_issues(issues_from_validator_messages(
        list(dict.fromkeys(messages)),
        subject="screenplay",
        stage="screenplay",
    ), stage="screenplay", artifact_id=artifact_id)
    issues.extend(
        structured_issue(
            code="CHARACTER_IDENTITY_UNRESOLVED",
            message=message,
            subject="screenplay",
            path=(
                "/character_identities/"
                + hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
            ),
            rule_id="character_identity_must_resolve_before_publish",
            repairable=True,
            requires_user_input=False,
            must_fix=True,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            stage="screenplay",
        )
        for message in dict.fromkeys(identity_messages)
    )
    for issue in issues:
        if artifact_hash:
            issue.evidence["artifact_hash"] = artifact_hash
    score = 100.0 if not issues else max(0.0, 100.0 - 10.0 * len(issues))
    try:
        pass_score = min(100.0, max(0.0, float(get_setting("screenplay_qa_pass_score") or 80)))
    except (TypeError, ValueError):
        pass_score = 80.0
    quality_passed = (
        blocker_count(issues) == 0
        and must_fix_count(issues) == 0
        and score >= pass_score
    )
    runtime_gate_issues = non_waivable_screenplay_issues(issues)
    runtime_blocking = bool(runtime_gate_issues)
    status = (
        "failed" if runtime_blocking
        else "passed" if quality_passed
        else "warning"
    )
    evaluation_role = "runtime_gate" if runtime_blocking else "score_only"
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="screenplay_production_qa",
        evaluator_version="screenplay-qa-gate-2",
        status=status,
        hard_gate_passed=not runtime_blocking,
        evaluation_role=evaluation_role,
        score_status="scored",
        runtime_blocking=runtime_blocking,
        retry_eligible=runtime_blocking and any(
            issue.repairable for issue in runtime_gate_issues
        ),
        score=score,
        issues=issues,
        evidence={
            "artifact_id": artifact_id,
            "artifact_hash": artifact_hash,
            "authority_input_fingerprint": authority_input_fingerprint,
            "blocker_count": blocker_count(issues),
            "must_fix_count": must_fix_count(issues),
            "evaluation_role": evaluation_role,
            "runtime_blocking": runtime_blocking,
            "pass_score": pass_score,
            "verdict": "passed" if quality_passed else "quality_risk",
            "runtime_gate_issue_count": len(runtime_gate_issues),
            "gate_retry_exhausted": bool(issues),
        },
    )
    return issues, evaluation


def plan_screenplay_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str = "",
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

    if code == "SCENE_FIELD_INVALID" and path.endswith("/turn"):
        scene_id, scene = _scene_from_issue(issue, script)
        strategy = f"derive_scene_turn_{scene_id}"
        if (
            scene_id
            and scene is not None
            and not _strategy_was_tried(tried, strategy)
        ):
            exit_state = re.sub(
                r"\s+", " ", str(scene.exit_state or "")
            ).strip(" ，,；;。")
            story_function = re.sub(
                r"\s+", " ", str(scene.story_function or scene.summary or "")
            ).strip(" ，,；;。")
            value = (
                f"本场结束时，{exit_state or '人物与局势完成状态变化'}，"
                f"并完成「{story_function or '本场戏剧任务'}」"
            )
            return [PatchOperation(
                op="replace_field",
                target={
                    "kind": "screenplay_scene",
                    "id": scene_id,
                    "strategy": strategy,
                },
                path="turn",
                value=value,
            )]

    if code == "SOURCE_SPAN_EXACT_MISMATCH":
        ops = _plan_source_span_patch(
            issue,
            script,
            source_text=source_text,
            tried=tried,
        )
        if ops:
            return ops

    if code == "SPINE_MISSING" and script.plot_spine is not None:
        referenced_ids = {
            str(value).strip().upper()
            for value in related
            if re.fullmatch(r"S\d+", str(value).strip(), re.I)
        }
        referenced_ids.update(
            match.upper()
            for match in re.findall(r"\bS\d+\b", issue.message or "", re.I)
        )
        for beat_index, beat in enumerate(script.plot_spine.spine_beats or []):
            beat_id = str(beat.beat_id or "").strip().upper()
            if (
                not beat.must_keep
                or (referenced_ids and beat_id not in referenced_ids)
            ):
                continue
            strategy = f"deliver_spine_{beat_id}"
            if _strategy_was_tried(tried, strategy):
                continue
            scene_id = _best_scene_for_spine_beat(
                script,
                beat_index=beat_index,
                who=(beat.who or "").strip(),
                does=(beat.does or "").strip(),
            )
            if not scene_id:
                continue
            who = (beat.who or "").strip()
            does = (beat.does or "").strip()
            action_text = f"{who}{does}".strip()
            if not action_text:
                continue
            return [PatchOperation(
                op="create_node",
                target={
                    "kind": "action_block",
                    "id": f"AC-SPINE-{beat_id}",
                    "scene_id": scene_id,
                    "strategy": strategy,
                },
                value={
                    "action_id": f"AC-SPINE-{beat_id}",
                    "text": action_text.rstrip("。") + "。",
                },
            )]

    staging_match = re.search(
        r"\]\s*([^/\s]+)/([^\s]+)\s+在\s+([^\s]+)\s+->\s+([^\s]+)"
        r"\s+之间缺少中间 AudienceState",
        issue.message or "",
    )
    if (
        code == "AUDIENCE_TARGET_DELTA_STAGING_REQUIRED"
        and staging_match
        and script.narrative_plan is not None
    ):
        path_id, prior_id, current_delta_id, next_delta_id = (
            staging_match.groups()
        )
        plan = script.narrative_plan
        path = next(
            (
                item
                for intent in plan.experience_intents
                for item in intent.audience_paths
                if (
                    item.audience_path_id == path_id
                    and item.audience_prior_id == prior_id
                )
            ),
            None,
        )
        if path is not None:
            current_delta = next(
                (
                    item
                    for item in path.target_deltas
                    if item.target_delta_id == current_delta_id
                ),
                None,
            )
            next_delta = next(
                (
                    item
                    for item in path.target_deltas
                    if item.target_delta_id == next_delta_id
                ),
                None,
            )
            state_in = next(
                (
                    item
                    for item in plan.audience_states
                    if item.audience_state_id == path.audience_state_in_id
                ),
                None,
            )
            strategy = (
                f"stage_{path_id}_{current_delta_id}_{next_delta_id}"
            )
            if (
                current_delta is not None
                and next_delta is not None
                and state_in is not None
                and not _strategy_was_tried(tried, strategy)
            ):
                state_data = state_in.model_dump(mode="json")
                fragment = deepcopy(current_delta.to_state)
                if current_delta.dimension == "belief":
                    beliefs_by_id = {
                        str(item.get("proposition_id") or ""): item
                        for item in state_data.get("beliefs") or []
                        if isinstance(item, dict)
                    }
                    explicit = fragment.pop("beliefs", None)
                    if isinstance(explicit, list):
                        for belief in explicit:
                            if not isinstance(belief, dict):
                                continue
                            proposition_id = str(
                                belief.get("proposition_id") or ""
                            )
                            if proposition_id:
                                beliefs_by_id[proposition_id] = deepcopy(belief)
                    else:
                        belief_patch = {
                            key: deepcopy(value)
                            for key, value in fragment.items()
                            if key in {"stance", "confidence", "evidence_ids"}
                        }
                        for proposition_id in current_delta.proposition_ids:
                            current = dict(
                                beliefs_by_id.get(proposition_id) or {
                                    "proposition_id": proposition_id,
                                }
                            )
                            current.update(belief_patch)
                            beliefs_by_id[proposition_id] = current
                    state_data["beliefs"] = list(beliefs_by_id.values())
                for field, value in fragment.items():
                    if field in {"stance", "confidence", "evidence_ids"}:
                        continue
                    if (
                        isinstance(value, dict)
                        and isinstance(state_data.get(field), dict)
                    ):
                        state_data[field] = {
                            **state_data[field],
                            **deepcopy(value),
                        }
                    elif field in type(state_in).model_fields:
                        state_data[field] = deepcopy(value)

                existing_ids = {
                    item.audience_state_id
                    for item in plan.audience_states
                }
                stem = re.sub(
                    r"[^A-Za-z0-9_-]+",
                    "-",
                    f"AS-{prior_id}-MID-{current_delta.deadline_event_id}",
                ).strip("-")
                state_id = stem
                suffix = 2
                while state_id in existing_ids:
                    state_id = f"{stem}-{suffix}"
                    suffix += 1
                state_data.update({
                    "audience_state_id": state_id,
                    "audience_prior_id": prior_id,
                    "anchor": {
                        "type": "event",
                        "id": current_delta.deadline_event_id,
                    },
                })
                return [PatchOperation(
                    op="create_node",
                    target={
                        "kind": "narrative_node",
                        "collection": "audience_states",
                        "id": state_id,
                        "strategy": strategy,
                    },
                    value=state_data,
                )]

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

    # 普通话轮的原文依据必须按报错索引精确修复。模型偶尔会写入
    # “原文叙述转为对白”之类说明性占位词；此时只能替换为本集原文中的
    # 可核验句子，不能误改开场话轮，也不能用占位词绕过 SOURCE_FIDELITY。
    mismatch = _DIALOGUE_SOURCE_MISMATCH_RE.search(issue.message or "")
    if code == "SOURCE_FIDELITY" and mismatch and source_text:
        chain_index, turn_index = map(int, mismatch.groups())
        turn_ref = _dialogue_turn_at(script, chain_index, turn_index)
        if turn_ref is not None:
            chain, turn = turn_ref
            strategy = f"fix_dialogue_source_{chain.chain_id}_{turn_index}"
            if not _strategy_was_tried(tried, strategy):
                evidence = (
                    _unique_source_dialogue(turn.line or "", source_text)
                    or _best_source_evidence_for_turn(
                        script,
                        chain_index=chain_index,
                        turn_index=turn_index,
                        source_text=source_text,
                    )
                )
                if evidence and evidence != (turn.source_text or "").strip():
                    return [PatchOperation(
                        op="replace_field",
                        path="source_text",
                        value=evidence,
                        target={
                            "kind": "dialogue_chain_turn",
                            "id": f"{chain.chain_id}-T{turn_index + 1}",
                            "chain_id": chain.chain_id,
                            "turn_index": turn_index,
                            "strategy": strategy,
                        },
                    )]

    # 原文开场对白锚点：只处理明确的开场锚点错误。
    if (
        code == "SOURCE_FIDELITY"
        and "原文开场第一句对白未作为" in (issue.message or "")
        and not _strategy_was_tried(tried, "fix_opening_source_anchor")
    ):
        opening = _opening_anchor_from_issue(issue.message or "")
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
                    "strategy": "fix_opening_source_anchor",
                },
            )]

    # 模型把同一人物的连续自语误标成 response 时，只改结构标签，不改台词。
    response_match = re.search(
        r"dialogue_chains\[(\d+)\]\.turns\[(\d+)\]\s*是\s*response",
        issue.message or "",
    )
    if code == "KEY_LINE_MISSING" and response_match:
        chain_index, turn_index = map(int, response_match.groups())
        if 0 <= chain_index < len(script.dialogue_chains or []):
            chain = script.dialogue_chains[chain_index]
            turns = chain.turns or []
            if 0 <= turn_index < len(turns):
                strategy = f"fix_dialogue_function_{chain.chain_id}_{turn_index}"
                if not _strategy_was_tried(tried, strategy):
                    return [PatchOperation(
                        op="replace_field",
                        path="function",
                        value="statement",
                        target={
                            "kind": "dialogue_chain_turn",
                            "id": f"{chain.chain_id}-T{turn_index + 1}",
                            "chain_id": chain.chain_id,
                            "turn_index": turn_index,
                        },
                    )]

    # 同一对白链被正文场次切开：按台词的实际场次拆成多条完整链。
    # 只修结构归属，不移动正文，比 rederive 更小且能真正消除问题。
    cross_scene_match = re.search(
        r"dialogue_chains\[(\d+)\]\s*被拆到多个场次",
        issue.message or "",
    )
    if code == "KEY_LINE_MISSING" and cross_scene_match:
        chain_index = int(cross_scene_match.group(1))
        if 0 <= chain_index < len(script.dialogue_chains or []):
            chain = script.dialogue_chains[chain_index]
            strategy = f"split_dialogue_chain_{chain.chain_id}"
            if not _strategy_was_tried(tried, strategy):
                return [PatchOperation(
                    op="split_dialogue_chain_by_scene",
                    target={"kind": "dialogue_chain", "id": chain.chain_id,
                            "chain_id": chain.chain_id},
                )]

    capacity_match = re.search(
        r"dialogue_chains\[(\d+)\]\.turns\[(\d+)\]\s+纯文字\s+(\d+)\s+字",
        issue.message or "",
    )
    if code == "DIALOGUE_TURN_CAPACITY_EXCEEDED" and capacity_match:
        from app import config

        chain_index, turn_index, _spoken_chars = map(
            int,
            capacity_match.groups(),
        )
        if 0 <= chain_index < len(script.dialogue_chains or []):
            chain = script.dialogue_chains[chain_index]
            strategy = (
                f"split_dialogue_turn_{chain.chain_id}_{turn_index}"
            )
            if not _strategy_was_tried(tried, strategy):
                return [PatchOperation(
                    op="split_dialogue_turn_by_capacity",
                    target={
                        "kind": "dialogue_chain_turn",
                        "id": f"{chain.chain_id}-T{turn_index + 1}",
                        "chain_id": chain.chain_id,
                        "turn_index": turn_index,
                        "strategy": strategy,
                    },
                    value={
                        "max_chars": config.MAX_SPOKEN_CHARS_PER_SHOT,
                    },
                )]

    decision_match = re.search(
        r"\]\s*([^/\s]+)/([^\s]+)\s+的执行者\s+([^\s]+)\s+缺少感知",
        issue.message or "",
    )
    if (
        code == "CHARACTER_DECISION_CHAIN_MISSING"
        and decision_match
        and script.narrative_plan is not None
    ):
        event_id, action_id, actor_id = decision_match.groups()
        strategy = f"create_decision_chain_{event_id}_{action_id}_{actor_id}"
        if not _strategy_was_tried(tried, strategy):
            plan = script.narrative_plan
            event = next(
                (item for item in plan.events if item.event_id == event_id),
                None,
            )
            action = next(
                (item for item in plan.atomic_actions if item.action_id == action_id),
                None,
            )
            if (
                event is not None
                and action is not None
                and action_id in event.action_ids
                and actor_id in action.actor_ids
                and action.decision_requirement == "applies"
            ):
                proposition_ids = list(dict.fromkeys(event.proposition_ids or []))
                evidence = [
                    item for item in plan.evidence
                    if (
                        item.anchor.type == "event"
                        and item.anchor.id == event_id
                        and actor_id in item.perceivable_by
                        and set(item.supports_proposition_ids) & set(proposition_ids)
                    )
                ]
                if len(evidence) == 1:
                    evidence_item = evidence[0]
                    supported = [
                        proposition_id
                        for proposition_id in proposition_ids
                        if proposition_id in evidence_item.supports_proposition_ids
                    ]
                    existing_ids = {
                        item.character_belief_id
                        for item in plan.character_beliefs
                    }
                    belief_no = 1
                    while f"CB-{belief_no}" in existing_ids:
                        belief_no += 1
                    belief_id = f"CB-{belief_no}"
                    return [PatchOperation(
                        op="create_node",
                        target={
                            "kind": "narrative_node",
                            "collection": "character_beliefs",
                            "id": belief_id,
                            "strategy": strategy,
                        },
                        value={
                            "character_belief_id": belief_id,
                            "character_id": actor_id,
                            "anchor": {"type": "event", "id": event_id},
                            "perceived_evidence_ids": [evidence_item.evidence_id],
                            "beliefs": [
                                {
                                    "proposition_id": proposition_id,
                                    "stance": "believed",
                                    "confidence": 1.0,
                                    "evidence_ids": [evidence_item.evidence_id],
                                }
                                for proposition_id in supported
                            ],
                            "misbelief_proposition_ids": [],
                            "decision_proposition_ids": supported,
                            "decision_basis_ids": [evidence_item.evidence_id],
                            "decision_action_ids": [action_id],
                        },
                    )]

    dramatic_state_match = re.search(
        r"\]\s*([^/\s]+)/([^\s]+)\s+的执行者\s+([^\s]+)"
        r"\s+缺少目标/情绪/关系状态",
        issue.message or "",
    )
    if (
        code == "CHARACTER_DRAMATIC_STATE_MISSING"
        and dramatic_state_match
        and script.narrative_plan is not None
    ):
        event_id, action_id, actor_id = dramatic_state_match.groups()
        strategy = f"create_dramatic_state_{event_id}_{action_id}_{actor_id}"
        if not _strategy_was_tried(tried, strategy):
            plan = script.narrative_plan
            event = next(
                (item for item in plan.events if item.event_id == event_id),
                None,
            )
            action = next(
                (
                    item for item in plan.atomic_actions
                    if item.action_id == action_id
                ),
                None,
            )
            if (
                event is not None
                and action is not None
                and action_id in event.action_ids
                and actor_id in action.actor_ids
            ):
                existing_ids = {
                    item.character_state_id
                    for item in plan.character_states
                }
                state_no = 1
                while f"CDS-{state_no}" in existing_ids:
                    state_no += 1
                state_id = f"CDS-{state_no}"
                evidence_ids = [
                    item.evidence_id
                    for item in plan.evidence
                    if (
                        item.anchor.type == "event"
                        and item.anchor.id == event_id
                        and actor_id in item.perceivable_by
                    )
                ]
                return [PatchOperation(
                    op="create_node",
                    target={
                        "kind": "narrative_node",
                        "collection": "character_states",
                        "id": state_id,
                        "strategy": strategy,
                    },
                    value={
                        "character_state_id": state_id,
                        "character_id": actor_id,
                        "anchor": {"type": "event", "id": event_id},
                        "goal_proposition_ids": list(
                            event.proposition_ids
                        ),
                        "stakes_proposition_ids": [],
                        "relationship_state": {},
                        "emotion": {
                            "label": "事件压力",
                            "intensity": max(
                                0.0,
                                min(1.0, float(event.salience or 0)),
                            ),
                            "observable_evidence": evidence_ids,
                        },
                        "pressure": max(
                            0.0,
                            min(1.0, float(event.salience or 0)),
                        ),
                        "tactic": action.semantic_intent,
                    },
                )]

    delta_match = re.search(
        r"\]\s*([^\s.]+)\.(from_state|to_state)\s+不是",
        issue.message or "",
    )
    if (
        code in {
            "TARGET_DELTA_FROM_STATE_MISMATCH",
            "TARGET_DELTA_TO_STATE_MISMATCH",
        }
        and delta_match
        and script.narrative_plan is not None
    ):
        delta_id, state_field = delta_match.groups()
        plan = script.narrative_plan
        located = next(
            (
                (path, delta)
                for intent in plan.experience_intents
                for path in intent.audience_paths
                for delta in path.target_deltas
                if delta.target_delta_id == delta_id
            ),
            None,
        )
        if located is not None:
            path, delta = located
            state_id = (
                path.audience_state_in_id
                if state_field == "from_state"
                else path.audience_state_out_target_id
            )
            state = next(
                (
                    item for item in plan.audience_states
                    if item.audience_state_id == state_id
                ),
                None,
            )
            prior = next(
                (
                    item for item in plan.audience_priors
                    if item.audience_prior_id == path.audience_prior_id
                ),
                None,
            )
            if state is not None and delta.dimension == "belief":
                beliefs_by_id = {
                    item.proposition_id: item.model_dump(mode="json")
                    for item in state.beliefs
                }
                missing = [
                    proposition_id
                    for proposition_id in delta.proposition_ids
                    if proposition_id not in beliefs_by_id
                ]
                can_add_unknown = (
                    state_field == "from_state"
                    and prior is not None
                    and set(missing).issubset(
                        set(prior.assumed_unknown_proposition_ids)
                    )
                )
                if not missing or can_add_unknown:
                    added = [
                        {
                            "proposition_id": proposition_id,
                            "stance": "unknown",
                            "confidence": 0.0,
                            "evidence_ids": [],
                        }
                        for proposition_id in missing
                    ]
                    full_beliefs = [
                        item.model_dump(mode="json")
                        for item in state.beliefs
                    ] + added
                    selected = {
                        item["proposition_id"]: item
                        for item in full_beliefs
                        if item["proposition_id"] in delta.proposition_ids
                    }
                    fragment = {
                        "beliefs": [
                            selected[proposition_id]
                            for proposition_id in delta.proposition_ids
                        ]
                    }
                    strategy = f"bind_{delta_id}_{state_field}"
                    if not _strategy_was_tried(tried, strategy):
                        operations = [PatchOperation(
                            op="replace_field",
                            path=state_field,
                            value=fragment,
                            target={
                                "kind": "narrative_node",
                                "collection": "experience_intents",
                                "id": delta_id,
                                "strategy": strategy,
                            },
                        )]
                        if missing:
                            operations.append(PatchOperation(
                                op="replace_field",
                                path="beliefs",
                                value=full_beliefs,
                                target={
                                    "kind": "narrative_node",
                                    "collection": "audience_states",
                                    "id": state_id,
                                    "strategy": strategy,
                                },
                            ))
                        return operations

    uncovered_match = re.search(
        r"\]\s*([^\s]+)\s+入/出状态的结构变化没有 target_delta 负责：\[(.+)\]",
        issue.message or "",
    )
    if (
        code == "AUDIENCE_TARGET_STATE_DIFF_UNASSIGNED"
        and uncovered_match
        and script.narrative_plan is not None
    ):
        path_id, raw_fields = uncovered_match.groups()
        uncovered_fields = set(re.findall(r"'([^']+)'", raw_fields))
        plan = script.narrative_plan
        located = next(
            (
                path
                for intent in plan.experience_intents
                for path in intent.audience_paths
                if path.audience_path_id == path_id
            ),
            None,
        )
        if located is not None:
            state_in = next(
                (
                    item for item in plan.audience_states
                    if item.audience_state_id == located.audience_state_in_id
                ),
                None,
            )
            state_out = next(
                (
                    item for item in plan.audience_states
                    if item.audience_state_id == located.audience_state_out_target_id
                ),
                None,
            )
            prior = next(
                (
                    item for item in plan.audience_priors
                    if item.audience_prior_id == located.audience_prior_id
                ),
                None,
            )
            if state_in is not None and state_out is not None:
                if (
                    uncovered_fields == {"working_memory"}
                    and state_in.attention_residue_ids
                    == state_out.attention_residue_ids
                ):
                    strategy = f"align_{path_id}_working_memory"
                    if not _strategy_was_tried(tried, strategy):
                        return [PatchOperation(
                            op="replace_field",
                            path="working_memory",
                            value=list(state_in.working_memory),
                            target={
                                "kind": "narrative_node",
                                "collection": "audience_states",
                                "id": state_out.audience_state_id,
                                "strategy": strategy,
                            },
                        )]
                if uncovered_fields == {"beliefs"} and prior is not None:
                    known = list(dict.fromkeys(
                        prior.assumed_known_proposition_ids
                    ))
                    if known:
                        canonical = [
                            {
                                "proposition_id": proposition_id,
                                "stance": "believed",
                                "confidence": 1.0,
                                "evidence_ids": [],
                            }
                            for proposition_id in known
                        ]
                        strategy = f"align_{path_id}_known_beliefs"
                        if not _strategy_was_tried(tried, strategy):
                            operations: list[PatchOperation] = []
                            if [
                                item.model_dump(mode="json")
                                for item in state_in.beliefs
                            ] != canonical:
                                operations.append(PatchOperation(
                                    op="replace_field",
                                    path="beliefs",
                                    value=canonical,
                                    target={
                                        "kind": "narrative_node",
                                        "collection": "audience_states",
                                        "id": state_in.audience_state_id,
                                        "strategy": strategy,
                                    },
                                ))
                            if [
                                item.model_dump(mode="json")
                                for item in state_out.beliefs
                            ] != canonical:
                                operations.append(PatchOperation(
                                    op="replace_field",
                                    path="beliefs",
                                    value=canonical,
                                    target={
                                        "kind": "narrative_node",
                                        "collection": "audience_states",
                                        "id": state_out.audience_state_id,
                                        "strategy": strategy,
                                    },
                                ))
                            if operations:
                                return operations
                if uncovered_fields == {"affective_state"}:
                    intent = next(
                        (
                            item
                            for item in plan.experience_intents
                            if any(
                                path.audience_path_id == path_id
                                for path in item.audience_paths
                            )
                        ),
                        None,
                    )
                    deadline_event_id = (
                        state_out.anchor.id
                        if state_out.anchor.type == "event"
                        else (
                            intent.anchor_event_ids[-1]
                            if intent is not None and intent.anchor_event_ids
                            else ""
                        )
                    )
                    delta_id = f"XD-{path_id}-affective"
                    existing_delta_ids = {
                        delta.target_delta_id
                        for path in (
                            intent.audience_paths if intent is not None else []
                        )
                        for delta in path.target_deltas
                    }
                    strategy = f"cover_{path_id}_affective_state"
                    if (
                        intent is not None
                        and deadline_event_id
                        and delta_id not in existing_delta_ids
                        and not _strategy_was_tried(tried, strategy)
                    ):
                        return [PatchOperation(
                            op="create_node",
                            target={
                                "kind": "narrative_node",
                                "collection": "experience_intents",
                                "id": delta_id,
                                "parent_id": path_id,
                                "parent_field": "target_deltas",
                                "strategy": strategy,
                            },
                            value={
                                "target_delta_id": delta_id,
                                "dimension": "affective",
                                "proposition_ids": [],
                                "description": (
                                    f"{path_id} 的观众情绪由入场状态"
                                    "变化到目标出场状态"
                                ),
                                "from_state": {
                                    "affective_state": dict(
                                        state_in.affective_state
                                    ),
                                },
                                "to_state": {
                                    "affective_state": dict(
                                        state_out.affective_state
                                    ),
                                },
                                "required_processing_s": 0.0,
                                "deadline_event_id": deadline_event_id,
                            },
                        )]

    recall_decision_match = re.search(
        r"\]\s*([^\s.]+)\.recall_needed=(?:False|false)"
        r"\s+与低分位记忆结果\s+(?:True|true)\s+不一致",
        issue.message or "",
    )
    if (
        code == "SETUP_RECALL_DECISION_MISMATCH"
        and recall_decision_match
        and script.narrative_plan is not None
    ):
        payoff_id = recall_decision_match.group(1)
        strategy = f"enable_recall_{payoff_id}"
        if not _strategy_was_tried(tried, strategy):
            payoff = next(
                (
                    item
                    for item in (
                        script.narrative_plan.setup_payoff_contracts
                    )
                    if item.setup_payoff_id == payoff_id
                ),
                None,
            )
            if payoff is not None:
                return [PatchOperation(
                    op="replace_field",
                    path="recall_needed",
                    value=True,
                    target={
                        "kind": "narrative_node",
                        "collection": "setup_payoff_contracts",
                        "id": payoff_id,
                        "strategy": strategy,
                    },
                )]

    recall_task_match = re.search(
        r"\]\s*([^/\s]+)/([^\s]+)\s+在使用前已遗忘\s+\[([^\]]+)\]",
        issue.message or "",
    )
    if (
        code == "SETUP_RECALL_TASK_MISSING"
        and recall_task_match
        and script.narrative_plan is not None
    ):
        payoff_id, prior_id, raw_propositions = recall_task_match.groups()
        missing_propositions = re.findall(r"'([^']+)'", raw_propositions)
        plan = script.narrative_plan
        payoff = next(
            (
                item for item in plan.setup_payoff_contracts
                if item.setup_payoff_id == payoff_id
            ),
            None,
        )
        paths = [
            (intent, path)
            for intent in plan.experience_intents
            for path in intent.audience_paths
            if path.audience_prior_id == prior_id
        ]
        payoff_event_ids = set(
            payoff.payoff_event_ids if payoff is not None else []
        )
        payoff_paths = [
            (intent, path)
            for intent, path in paths
            if (
                payoff_event_ids.intersection(intent.anchor_event_ids)
                or any(
                    delta.deadline_event_id in payoff_event_ids
                    for delta in path.target_deltas
                )
            )
        ]
        selected_paths = payoff_paths if len(payoff_paths) == 1 else paths
        if (
            payoff is not None
            and len(selected_paths) == 1
            and missing_propositions
        ):
            intent, path = selected_paths[0]
            downstream_event = next(
                iter(payoff.payoff_event_ids),
                payoff.retention_deadline_event_id,
            )
            existing_tasks = [
                task for task in plan.assimilation_tasks
                if task.audience_path_id == path.audience_path_id
            ]
            strategy = f"recall_task_{payoff_id}_{prior_id}"
            if not _strategy_was_tried(tried, strategy):
                if len(existing_tasks) == 1:
                    task = existing_tasks[0]
                    required = list(dict.fromkeys([
                        *task.required_prior_proposition_ids,
                        *missing_propositions,
                    ]))
                    downstream = list(dict.fromkeys([
                        *task.downstream_dependency_event_ids,
                        downstream_event,
                    ]))
                    operations = [
                        PatchOperation(
                            op="replace_field",
                            path="required_prior_proposition_ids",
                            value=required,
                            target={
                                "kind": "narrative_node",
                                "collection": "assimilation_tasks",
                                "id": task.assimilation_task_id,
                                "strategy": strategy,
                            },
                        ),
                    ]
                    if downstream != task.downstream_dependency_event_ids:
                        operations.append(PatchOperation(
                            op="replace_field",
                            path="downstream_dependency_event_ids",
                            value=downstream,
                            target={
                                "kind": "narrative_node",
                                "collection": "assimilation_tasks",
                                "id": task.assimilation_task_id,
                                "strategy": strategy,
                            },
                        ))
                    return operations
                if not existing_tasks and path.target_deltas:
                    existing_ids = {
                        task.assimilation_task_id
                        for task in plan.assimilation_tasks
                    }
                    task_no = 1
                    while f"AT-{task_no}" in existing_ids:
                        task_no += 1
                    task_id = f"AT-{task_no}"
                    return [PatchOperation(
                        op="create_node",
                        target={
                            "kind": "narrative_node",
                            "collection": "assimilation_tasks",
                            "id": task_id,
                            "strategy": strategy,
                        },
                        value={
                            "assimilation_task_id": task_id,
                            "experience_intent_id": intent.experience_intent_id,
                            "audience_path_id": path.audience_path_id,
                            "target_delta_id": path.target_deltas[0].target_delta_id,
                            "required_prior_proposition_ids": missing_propositions,
                            "downstream_dependency_event_ids": [downstream_event],
                            "satisfaction_criteria": (
                                f"观众在 {downstream_event} 前能回忆命题 "
                                + "、".join(missing_propositions)
                            ),
                            "status": "planned",
                        },
                    )]

    # key_lines / full_script_text 等纯派生投影才允许 rederive。上下文断裂必须
    # 修复权威 dialogue_chain，重建投影不会补出缺失的触发话轮。
    if (
        code == "KEY_LINE_MISSING"
        and "主线对白上下文断裂" not in (issue.message or "")
        and "turns 需包含" not in (issue.message or "")
        and not _strategy_was_tried(tried, "rederive")
    ):
        return [PatchOperation(op="rederive")]

    return ops


async def _plan_screenplay_repair_operations(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
    strategy_history: dict[str, list[str]],
) -> list[PatchOperation]:
    """Prefer bounded document operations; use semantic planning for graph gaps."""
    operations = plan_screenplay_patch(
        issue,
        script,
        source_text=source_text,
        strategy_history=strategy_history,
    )
    if operations:
        return operations
    return await _llm_field_patch(
        issue,
        script,
        source_text=source_text,
        strategy_history=strategy_history.get(issue.fingerprint, []),
    )


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

    # 场功能是短段元数据，不值得为省几个字截成“情绪从”这类半句。
    # 这里的宽限只防御模型异常长输入，正常的冲突与转折应完整保留。
    conflict = compact(getattr(scene, "conflict", ""), 48)
    turn = compact(getattr(scene, "turn", ""), 48)
    summary = compact(getattr(scene, "summary", ""), 64)
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


def _opening_anchor_from_issue(message: str) -> str:
    match = re.search(
        r"原文开场第一句对白未作为\s+dialogue_chains\[0\]\.turns\[0\]"
        r"[：:]\s*(.+?)(?:；|;|$)",
        message,
    )
    return match.group(1).strip() if match else ""


def _dialogue_turn_at(
    script: EpisodeScreenplay,
    chain_index: int,
    turn_index: int,
):
    chains = script.dialogue_chains or []
    if not 0 <= chain_index < len(chains):
        return None
    chain = chains[chain_index]
    turns = chain.turns or []
    if not 0 <= turn_index < len(turns):
        return None
    return chain, turns[turn_index]


def _source_sentence_candidates(source_text: str) -> list[str]:
    candidates: list[str] = []
    for paragraph in re.split(r"\n+", source_text or ""):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        sentences = [
            match.group(0).strip()
            for match in _SOURCE_SENTENCE_RE.finditer(paragraph)
            if match.group(0).strip()
        ]
        for index, sentence in enumerate(sentences):
            candidates.append(sentence)
            if index + 1 < len(sentences):
                candidates.append(f"{sentence}{sentences[index + 1]}")
    return list(dict.fromkeys(candidates))


def _source_evidence_score(candidate: str, target: str, context: str) -> float:
    compact_candidate = re.sub(r"\W+", "", candidate)
    compact_target = re.sub(r"\W+", "", target)
    compact_context = re.sub(r"\W+", "", context)
    if not compact_candidate or not compact_target:
        return 0.0

    meaningful_target = {
        char for char in compact_target
        if char not in _SOURCE_EVIDENCE_STOP_CHARS
    }
    meaningful_overlap = meaningful_target & set(compact_candidate)
    if len(meaningful_overlap) < 2:
        return 0.0

    target_bigrams = {
        compact_target[index:index + 2]
        for index in range(max(0, len(compact_target) - 1))
    }
    candidate_bigrams = {
        compact_candidate[index:index + 2]
        for index in range(max(0, len(compact_candidate) - 1))
    }
    context_bigrams = {
        compact_context[index:index + 2]
        for index in range(max(0, len(compact_context) - 1))
    }
    target_coverage = (
        len(target_bigrams & candidate_bigrams) / len(target_bigrams)
        if target_bigrams else 0.0
    )
    context_coverage = (
        len(context_bigrams & candidate_bigrams) / len(context_bigrams)
        if context_bigrams else 0.0
    )
    sequence = SequenceMatcher(None, compact_target, compact_candidate).ratio()
    char_coverage = len(meaningful_overlap) / max(1, len(meaningful_target))
    length_penalty = min(0.2, max(0, len(compact_candidate) - 100) / 500)
    return (
        target_coverage * 5.0
        + char_coverage * 2.0
        + sequence
        + context_coverage * 0.75
        - length_penalty
    )


def _best_source_evidence_for_turn(
    script: EpisodeScreenplay,
    *,
    chain_index: int,
    turn_index: int,
    source_text: str,
) -> str:
    turn_ref = _dialogue_turn_at(script, chain_index, turn_index)
    if turn_ref is None:
        return ""
    chain, turn = turn_ref
    target = (turn.line or "").strip()
    if not target:
        return ""

    context_parts = [chain.topic or ""]
    full_script = script.full_script_text or ""
    line_offset = full_script.find(target)
    if line_offset >= 0:
        headings = list(re.finditer(r"【场\s*(\d+)】", full_script[:line_offset]))
        if headings:
            scene_no = int(headings[-1].group(1))
            scene = next(
                (
                    item for item in (script.scene_outline or [])
                    if int(item.scene_no) == scene_no
                ),
                None,
            )
            if scene is not None:
                context_parts.extend([
                    scene.source_basis or "",
                    scene.summary or "",
                    scene.conflict or "",
                    scene.turn or "",
                ])
    context = " ".join(part for part in context_parts if part)

    ranked = sorted(
        (
            (_source_evidence_score(candidate, target, context), candidate)
            for candidate in _source_sentence_candidates(source_text)
        ),
        key=lambda item: (item[0], -len(item[1])),
        reverse=True,
    )
    if not ranked or ranked[0][0] < 1.0:
        return ""
    return ranked[0][1]


def _source_evidence_span(
    chapter: str,
    excerpt: str,
    *,
    context: str = "",
) -> tuple[int, int, str | None] | None:
    """Resolve one exact raw span, optionally expanding a proven excerpt elision."""
    from app.narrative import normalize_source_evidence_text

    normalized_excerpt = normalize_source_evidence_text(excerpt)
    raw_positions = [
        raw_index
        for raw_index, char in enumerate(chapter)
        if not char.isspace()
    ]
    normalized_chapter = "".join(chapter[index] for index in raw_positions)
    if not normalized_excerpt or not raw_positions:
        return None

    def occurrences(haystack: str, needle: str) -> list[int]:
        found: list[int] = []
        cursor = 0
        while needle:
            offset = haystack.find(needle, cursor)
            if offset < 0:
                break
            found.append(offset)
            cursor = offset + 1
        return found

    exact_offsets = occurrences(normalized_chapter, normalized_excerpt)
    if len(exact_offsets) == 1:
        offset = exact_offsets[0]
        return (
            raw_positions[offset],
            raw_positions[offset + len(normalized_excerpt) - 1] + 1,
            None,
        )
    if len(exact_offsets) > 1 and context:
        ranked: list[tuple[float, int]] = []
        for offset in exact_offsets:
            raw_start = raw_positions[offset]
            raw_end = raw_positions[offset + len(normalized_excerpt) - 1] + 1
            window = chapter[
                max(0, raw_start - 320):min(len(chapter), raw_end + 320)
            ]
            score = max(
                textmatch.longest_run_ratio(context, window),
                textmatch.bigram_coverage(context, window),
            )
            ranked.append((score, offset))
        ranked.sort(reverse=True)
        best_score, best_offset = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else -1.0
        if best_score >= 0.2 and best_score - second_score >= 0.03:
            return (
                raw_positions[best_offset],
                raw_positions[
                    best_offset + len(normalized_excerpt) - 1
                ] + 1,
                None,
            )
    match_excerpt = re.sub(
        r"(?:…{2,}|\.{3,})",
        "",
        normalized_excerpt,
    )
    if exact_offsets or len(match_excerpt) < 24:
        return None

    # A model may concatenate two exact source regions while omitting an
    # irrelevant paragraph. Recover only when unique prefix/suffix anchors prove
    # one bounded containing span and the authored excerpt is an ordered
    # subsequence of it. This expands evidence; it never invents or fuzzy-edits it.
    anchor_size = min(32, max(12, len(match_excerpt) // 8))
    prefix = match_excerpt[:anchor_size]
    suffix = match_excerpt[-anchor_size:]
    prefix_offsets = occurrences(normalized_chapter, prefix)
    suffix_offsets = occurrences(normalized_chapter, suffix)
    candidates: list[tuple[int, int, int]] = []
    for start in prefix_offsets:
        for suffix_start in suffix_offsets:
            end = suffix_start + len(suffix)
            if end <= start or end - start < len(match_excerpt):
                continue
            segment = normalized_chapter[start:end]
            extra = len(segment) - len(match_excerpt)
            cursor = 0
            for char in segment:
                if cursor < len(match_excerpt) and char == match_excerpt[cursor]:
                    cursor += 1
            matching_coverage = (
                sum(
                    block.size
                    for block in SequenceMatcher(
                        None,
                        match_excerpt,
                        segment,
                        autojunk=False,
                    ).get_matching_blocks()
                )
                / max(1, len(match_excerpt))
            )
            if (
                cursor == len(match_excerpt)
                or matching_coverage >= 0.98
            ):
                candidates.append((extra, start, end))

    if not candidates:
        return None
    candidates.sort()
    best_extra = candidates[0][0]
    best = [item for item in candidates if item[0] == best_extra]
    if len(best) != 1:
        return None
    _extra, start, end = best[0]
    raw_start = raw_positions[start]
    raw_end = raw_positions[end - 1] + 1
    return raw_start, raw_end, chapter[raw_start:raw_end]


def _normalize_screenplay_narrative_graph(
    script: EpisodeScreenplay,
    *,
    authorized_source_chapters: dict[str, str] | None,
) -> list[dict[str, Any]]:
    """Repair exact source offsets and unambiguous event-ID punctuation drift."""
    plan = script.narrative_plan
    if plan is None:
        return []
    data = plan.model_dump(mode="json")
    changes: list[dict[str, Any]] = []
    from app.validators import normalize_screenplay_dialogue_chains

    before_dialogue_chains = [
        chain.model_dump(mode="json")
        for chain in (script.dialogue_chains or [])
    ]
    before_full_script_text = script.full_script_text
    normalize_screenplay_dialogue_chains(script)
    after_dialogue_chains = [
        chain.model_dump(mode="json")
        for chain in (script.dialogue_chains or [])
    ]
    if (
        after_dialogue_chains != before_dialogue_chains
        or script.full_script_text != before_full_script_text
    ):
        changes.append({
            "kind": "dialogue_chain_normalization",
            "before_chain_count": len(before_dialogue_chains),
            "after_chain_count": len(after_dialogue_chains),
            "full_script_text_changed": (
                script.full_script_text != before_full_script_text
            ),
        })

    for index, chain in enumerate(script.dialogue_chains or []):
        topic = (chain.topic or "").strip()
        if len(topic) >= 4 or not chain.turns:
            continue
        speakers = list(dict.fromkeys(
            (turn.speaker or "").strip()
            for turn in chain.turns
            if (turn.speaker or "").strip()
        ))
        subject = (chain.turns[0].line or "").strip()[:16].strip("，。！？ ")
        normalized_topic = (
            f"{'与'.join(speakers[:2]) or '角色'}围绕"
            f"{subject or '当前事件'}的对话"
        )
        changes.append({
            "kind": "dialogue_topic",
            "id": chain.chain_id or f"dialogue-chain-{index}",
            "from": topic,
            "to": normalized_topic,
        })
        chain.topic = normalized_topic

    raw_chapters = (
        authorized_source_chapters
        if isinstance(authorized_source_chapters, dict)
        else {}
    )
    chapters = {
        str(chapter_id): str(text)
        for chapter_id, text in raw_chapters.items()
        if str(chapter_id).strip() and str(text)
    }
    changes.extend(_normalize_dialogue_chain_continuity(
        script,
        "\n".join(dict.fromkeys(chapters.values())),
    ))
    source_contexts: dict[str, list[str]] = {}
    for proposition in data.get("propositions") or []:
        if not isinstance(proposition, dict):
            continue
        statement = str(proposition.get("canonical_statement") or "").strip()
        if not statement:
            continue
        for evidence_id in proposition.get("direct_source_evidence_ids") or []:
            source_contexts.setdefault(str(evidence_id), []).append(statement)
    for index, evidence in enumerate(data.get("source_evidence") or []):
        if not isinstance(evidence, dict):
            continue
        span = evidence.get("source_span")
        excerpt = str(evidence.get("verbatim_excerpt") or "")
        if not isinstance(span, dict) or not excerpt:
            continue
        evidence_id = evidence.get("source_evidence_id") or f"source-{index}"
        context = " ".join(source_contexts.get(str(evidence_id), []))
        chapter_id = str(span.get("chapter_id") or "")
        chapter = chapters.get(chapter_id)
        resolved = (
            _source_evidence_span(chapter, excerpt, context=context)
            if chapter is not None
            else None
        )
        if chapter is None:
            candidates = (
                [(candidate_id, None) for candidate_id in chapters]
                if len(chapters) == 1
                else [
                    (candidate_id, candidate)
                    for candidate_id, candidate_text in chapters.items()
                    if (
                        candidate := _source_evidence_span(
                            candidate_text,
                            excerpt,
                            context=context,
                        )
                    ) is not None
                ]
            )
            if len(candidates) != 1:
                continue
            chapter_id, resolved = candidates[0]
            chapter = chapters[chapter_id]
            if resolved is None:
                resolved = _source_evidence_span(
                    chapter,
                    excerpt,
                    context=context,
                )
            changes.append({
                "kind": "source_chapter",
                "id": evidence_id,
                "from": span.get("chapter_id"),
                "to": chapter_id,
            })
            span["chapter_id"] = chapter_id
        if resolved is None:
            continue
        start, end, expanded_excerpt = resolved
        if expanded_excerpt is not None and expanded_excerpt != excerpt:
            changes.append({
                "kind": "source_excerpt_expanded",
                "id": evidence_id,
                "from_chars": len(excerpt),
                "to_chars": len(expanded_excerpt),
            })
            evidence["verbatim_excerpt"] = expanded_excerpt
        if span.get("start") != start or span.get("end") != end:
            changes.append({
                "kind": "source_span",
                "id": evidence_id,
                "from": {"start": span.get("start"), "end": span.get("end")},
                "to": {"start": start, "end": end},
            })
            span["start"] = start
            span["end"] = end

    event_ids = {
        str(event.get("event_id") or "").strip()
        for event in (data.get("events") or [])
        if isinstance(event, dict) and str(event.get("event_id") or "").strip()
    }
    event_aliases: dict[str, list[str]] = {}
    for event_id in event_ids:
        alias = re.sub(r"[^a-z0-9]+", "", event_id.casefold())
        if alias:
            event_aliases.setdefault(alias, []).append(event_id)

    def canonical_event_id(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or raw in event_ids:
            return raw
        alias = re.sub(r"[^a-z0-9]+", "", raw.casefold())
        matches = event_aliases.get(alias) or []
        return matches[0] if len(matches) == 1 else raw

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            is_event_anchor = str(value.get("type") or "").strip() == "event"
            for key, child in list(value.items()):
                child_path = f"{path}.{key}" if path else key
                if key == "id" and is_event_anchor:
                    normalized = canonical_event_id(child)
                    if normalized != child:
                        changes.append({
                            "kind": "event_ref",
                            "path": child_path,
                            "from": child,
                            "to": normalized,
                        })
                        value[key] = normalized
                elif key.endswith("event_id"):
                    normalized = canonical_event_id(child)
                    if normalized != child:
                        changes.append({
                            "kind": "event_ref",
                            "path": child_path,
                            "from": child,
                            "to": normalized,
                        })
                        value[key] = normalized
                elif key.endswith("event_ids") or key == "causal_parent_ids":
                    if isinstance(child, list):
                        normalized_values = [
                            canonical_event_id(item) for item in child
                        ]
                        if normalized_values != child:
                            changes.append({
                                "kind": "event_refs",
                                "path": child_path,
                                "from": child,
                                "to": normalized_values,
                            })
                            value[key] = normalized_values
                else:
                    walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(data)
    actions_by_id = {
        str(item.get("action_id") or "").strip(): item
        for item in (data.get("atomic_actions") or [])
        if (
            isinstance(item, dict)
            and str(item.get("action_id") or "").strip()
        )
    }
    fact_propositions = {
        str(fact.get("fact_id") or "").strip(): str(
            fact.get("proposition_id") or ""
        ).strip()
        for fact in (data.get("state_facts") or [])
        if (
            isinstance(fact, dict)
            and str(fact.get("fact_id") or "").strip()
            and str(fact.get("proposition_id") or "").strip()
        )
    }
    for event in data.get("events") or []:
        if not isinstance(event, dict):
            continue
        bound_actions = [
            actions_by_id[action_id]
            for action_id in (event.get("action_ids") or [])
            if action_id in actions_by_id
        ]
        action_preconditions = {
            str(fact_id)
            for action in bound_actions
            for fact_id in (action.get("precondition_fact_ids") or [])
            if str(fact_id or "").strip()
        }
        action_adds = {
            str(fact_id)
            for action in bound_actions
            for fact_id in (action.get("effects_add") or [])
            if str(fact_id or "").strip()
        }
        action_removes = {
            str(fact_id)
            for action in bound_actions
            for fact_id in (action.get("effects_remove") or [])
            if str(fact_id or "").strip()
        }
        touched_action_facts = (
            action_preconditions | action_adds | action_removes
        )
        derived_action_facts = {
            "precondition_fact_ids": action_preconditions - action_adds,
            "effects_add": action_adds - action_removes,
            "effects_remove": action_removes - action_adds,
        }
        for field, derived_facts in derived_action_facts.items():
            existing_facts = list(event.get(field) or [])
            preserved_event_facts = [
                fact_id
                for fact_id in existing_facts
                if str(fact_id) not in touched_action_facts
            ]
            normalized_facts = list(dict.fromkeys([
                *preserved_event_facts,
                *sorted(derived_facts),
            ]))
            if normalized_facts != existing_facts:
                changes.append({
                    "kind": "event_action_fact_refs",
                    "id": event.get("event_id"),
                    "field": field,
                    "from": existing_facts,
                    "to": normalized_facts,
                })
                event[field] = normalized_facts
        existing = list(event.get("proposition_ids") or [])
        required = [
            fact_propositions[fact_id]
            for fact_id in (
                *(event.get("precondition_fact_ids") or []),
                *(event.get("effects_add") or []),
                *(event.get("effects_remove") or []),
            )
            if fact_id in fact_propositions
        ]
        normalized = list(dict.fromkeys([*existing, *required]))
        if normalized != existing:
            changes.append({
                "kind": "event_proposition_refs",
                "id": event.get("event_id"),
                "from": existing,
                "to": normalized,
            })
            event["proposition_ids"] = normalized

    propositions_by_id = {
        str(item.get("proposition_id") or ""): item
        for item in (data.get("propositions") or [])
        if (
            isinstance(item, dict)
            and str(item.get("proposition_id") or "").strip()
        )
    }
    evidence_by_id = {
        str(item.get("evidence_id") or ""): item
        for item in (data.get("evidence") or [])
        if (
            isinstance(item, dict)
            and str(item.get("evidence_id") or "").strip()
        )
    }
    events_by_id = {
        str(item.get("event_id") or ""): item
        for item in (data.get("events") or [])
        if (
            isinstance(item, dict)
            and str(item.get("event_id") or "").strip()
        )
    }

    payoff_contracts_by_id = {
        str(item.get("setup_payoff_id") or ""): item
        for item in (data.get("setup_payoff_contracts") or [])
        if (
            isinstance(item, dict)
            and str(item.get("setup_payoff_id") or "").strip()
        )
    }
    for arc in data.get("arc_contracts") or []:
        if not isinstance(arc, dict):
            continue
        promises = [
            str(item)
            for item in (arc.get("promise_proposition_ids") or [])
            if str(item or "").strip()
        ]
        referenced_payoffs = [
            payoff_contracts_by_id[payoff_id]
            for payoff_id in (arc.get("payoff_contract_ids") or [])
            if payoff_id in payoff_contracts_by_id
        ]
        setup_promises = {
            str(proposition_id)
            for payoff in referenced_payoffs
            for proposition_id in (payoff.get("setup_proposition_ids") or [])
            if str(proposition_id or "") in propositions_by_id
        }
        orphan_promises = [
            proposition_id
            for proposition_id in promises
            if proposition_id not in setup_promises
        ]
        replacements: dict[str, str] = {}
        for proposition_id in orphan_promises:
            matching_payoffs = [
                payoff
                for payoff in referenced_payoffs
                if proposition_id in (
                    payoff.get("intended_inference_ids") or []
                )
            ]
            if len(matching_payoffs) != 1:
                break
            setup_ids = list(dict.fromkeys(
                str(item)
                for item in (
                    matching_payoffs[0].get("setup_proposition_ids") or []
                )
                if str(item or "") in propositions_by_id
            ))
            if len(setup_ids) != 1:
                break
            replacements[proposition_id] = setup_ids[0]
        else:
            if replacements:
                normalized_promises = list(dict.fromkeys(
                    replacements.get(proposition_id, proposition_id)
                    for proposition_id in promises
                ))
                changes.append({
                    "kind": "arc_promise_setup_projection",
                    "id": arc.get("arc_id"),
                    "from": promises,
                    "to": normalized_promises,
                    "inference_to_setup": replacements,
                })
                arc["promise_proposition_ids"] = normalized_promises

        current_promises = [
            str(item)
            for item in (arc.get("promise_proposition_ids") or [])
            if str(item or "").strip()
        ]
        current_payoff_ids = [
            str(item)
            for item in (arc.get("payoff_contract_ids") or [])
            if str(item or "").strip()
        ]
        current_setup_promises = {
            str(proposition_id)
            for payoff_id in current_payoff_ids
            if payoff_id in payoff_contracts_by_id
            for proposition_id in (
                payoff_contracts_by_id[payoff_id].get(
                    "setup_proposition_ids"
                ) or []
            )
        }
        newly_linked_payoffs: list[str] = []
        for proposition_id in current_promises:
            if proposition_id in current_setup_promises:
                continue
            candidates = [
                payoff_id
                for payoff_id, payoff in payoff_contracts_by_id.items()
                if proposition_id in (
                    payoff.get("setup_proposition_ids") or []
                )
            ]
            if len(candidates) == 1 and candidates[0] not in current_payoff_ids:
                newly_linked_payoffs.append(candidates[0])
                current_payoff_ids.append(candidates[0])
                current_setup_promises.add(proposition_id)
        if newly_linked_payoffs:
            changes.append({
                "kind": "arc_payoff_contract_link",
                "id": arc.get("arc_id"),
                "added": newly_linked_payoffs,
            })
            arc["payoff_contract_ids"] = current_payoff_ids

        unsupported_promises = [
            proposition_id
            for proposition_id in current_promises
            if proposition_id not in current_setup_promises
        ]
        if unsupported_promises and arc.get("core_question_ids"):
            supported_promises = [
                proposition_id
                for proposition_id in current_promises
                if proposition_id not in unsupported_promises
            ]
            changes.append({
                "kind": "arc_unsupported_promise_removed",
                "id": arc.get("arc_id"),
                "from": current_promises,
                "to": supported_promises,
                "unsupported": unsupported_promises,
            })
            arc["promise_proposition_ids"] = supported_promises

    existing_fact_ids = {
        str(item.get("fact_id") or "")
        for item in (data.get("state_facts") or [])
        if isinstance(item, dict) and str(item.get("fact_id") or "").strip()
    }
    missing_effect_ids = {
        str(fact_id)
        for event in events_by_id.values()
        for fact_id in (event.get("effects_add") or [])
        if str(fact_id or "").strip() not in existing_fact_ids
    }
    for missing_fact_id in sorted(missing_effect_ids):
        producer_events = [
            event
            for event in events_by_id.values()
            if missing_fact_id in (event.get("effects_add") or [])
        ]
        producer_actions = [
            action
            for action in actions_by_id.values()
            if missing_fact_id in (action.get("effects_add") or [])
        ]
        if len(producer_events) != 1 or len(producer_actions) > 1:
            continue
        event = producer_events[0]
        event_id = str(event.get("event_id") or "")
        supported = {
            str(proposition_id)
            for evidence in evidence_by_id.values()
            if (
                isinstance(evidence.get("anchor"), dict)
                and evidence["anchor"].get("type") == "event"
                and str(evidence["anchor"].get("id") or "") == event_id
            )
            for proposition_id in (
                evidence.get("supports_proposition_ids") or []
            )
            if str(proposition_id or "") in propositions_by_id
        }
        candidates = [
            proposition_id
            for proposition_id in (event.get("proposition_ids") or [])
            if proposition_id in supported
        ]
        if len(candidates) != 1:
            continue
        proposition_id = candidates[0]
        proposition = propositions_by_id[proposition_id]
        action = producer_actions[0] if producer_actions else None
        actors = list((action or {}).get("actor_ids") or [])
        if len(actors) != 1:
            continue
        event_position = list(events_by_id).index(event_id) + 1
        fact = {
            "fact_id": missing_fact_id,
            "proposition_id": proposition_id,
            "subject_id": actors[0],
            "predicate_id": str(
                proposition.get("semantic_identity_key")
                or f"state-after-{event_id}"
            ),
            "value": {
                "kind": "text",
                "data": str(proposition.get("canonical_statement") or ""),
            },
            "time_scope": f"main@{event_position}",
            "visibility": "visible",
            "provenance": "screenplay",
            "confidence": 1.0,
        }
        data.setdefault("state_facts", []).append(fact)
        existing_fact_ids.add(missing_fact_id)
        changes.append({
            "kind": "missing_effect_fact",
            "id": missing_fact_id,
            "event_id": event_id,
            "proposition_id": proposition_id,
            "subject_id": actors[0],
        })

    proposition_entities = {
        proposition_id: {
            str(entity_id)
            for entity_id in (item.get("entity_ids") or [])
            if str(entity_id or "").strip()
        }
        for proposition_id, item in propositions_by_id.items()
    }
    for belief in data.get("character_beliefs") or []:
        if not isinstance(belief, dict):
            continue
        character_id = str(belief.get("character_id") or "").strip()
        if not character_id:
            continue
        for evidence_id in belief.get("perceived_evidence_ids") or []:
            evidence = evidence_by_id.get(str(evidence_id))
            if evidence is None:
                continue
            perceivable = list(evidence.get("perceivable_by") or [])
            if character_id in perceivable:
                continue
            supported_entities = {
                entity_id
                for proposition_id in (
                    evidence.get("supports_proposition_ids") or []
                )
                for entity_id in proposition_entities.get(
                    str(proposition_id),
                    set(),
                )
            }
            anchor = evidence.get("anchor") or {}
            event = events_by_id.get(str(anchor.get("id") or ""))
            event_entities = {
                entity_id
                for action_id in (
                    (event or {}).get("action_ids") or []
                )
                for entity_id in (
                    *((actions_by_id.get(str(action_id)) or {}).get(
                        "actor_ids",
                    ) or []),
                    *((actions_by_id.get(str(action_id)) or {}).get(
                        "target_ids",
                    ) or []),
                )
            }
            if character_id not in supported_entities | event_entities:
                continue
            evidence["perceivable_by"] = [
                *perceivable,
                character_id,
            ]
            changes.append({
                "kind": "evidence_perceiver",
                "id": evidence_id,
                "character_id": character_id,
            })

    stance_aliases = {
        "accepted": "believed",
        "committed": "believed",
        "confirmed": "believed",
        "disbelieved": "rejected",
        "known": "believed",
        "uncertain": "suspected",
    }
    for collection, id_field in (
        ("character_beliefs", "character_belief_id"),
        ("audience_states", "audience_state_id"),
    ):
        for snapshot in data.get(collection) or []:
            if not isinstance(snapshot, dict):
                continue
            for belief in snapshot.get("beliefs") or []:
                if not isinstance(belief, dict):
                    continue
                stance = str(belief.get("stance") or "").strip()
                normalized = stance_aliases.get(stance, stance)
                if normalized == stance:
                    continue
                changes.append({
                    "kind": "belief_stance",
                    "id": (
                        f"{snapshot.get(id_field)}/"
                        f"{belief.get('proposition_id')}"
                    ),
                    "from": stance,
                    "to": normalized,
                })
                belief["stance"] = normalized

    events = [
        event for event in (data.get("events") or [])
        if isinstance(event, dict)
    ]
    causal_parent_ids = {
        str(parent_id)
        for event in events
        for parent_id in (event.get("causal_parent_ids") or [])
        if str(parent_id or "").strip()
    }
    critical_event_ids = {
        str(event.get("event_id") or "")
        for event in events
        if (
            event.get("downstream_dependency_event_ids")
            or str(event.get("event_id") or "") in causal_parent_ids
        )
    }
    intended = {
        str(proposition_id)
        for intent in (data.get("experience_intents") or [])
        if isinstance(intent, dict)
        for path in (intent.get("audience_paths") or [])
        if isinstance(path, dict)
        for delta in (path.get("target_deltas") or [])
        if isinstance(delta, dict)
        for proposition_id in (delta.get("proposition_ids") or [])
        if str(proposition_id or "").strip()
    }
    withheld = {
        str(item.get("proposition_id") or "")
        for intent in (data.get("experience_intents") or [])
        if isinstance(intent, dict)
        for item in (intent.get("withheld_propositions") or [])
        if isinstance(item, dict) and str(item.get("proposition_id") or "").strip()
    }
    missing_critical = {
        str(proposition_id)
        for event in events
        if str(event.get("event_id") or "") in critical_event_ids
        for proposition_id in (event.get("proposition_ids") or [])
        if str(proposition_id or "").strip() not in intended | withheld
    }
    if missing_critical:
        for intent in data.get("experience_intents") or []:
            if not isinstance(intent, dict):
                continue
            for path in intent.get("audience_paths") or []:
                if not isinstance(path, dict):
                    continue
                attention_deltas = [
                    delta
                    for delta in (path.get("target_deltas") or [])
                    if isinstance(delta, dict)
                    and str(delta.get("dimension") or "") == "attention"
                ]
                if len(attention_deltas) != 1:
                    continue
                delta = attention_deltas[0]
                existing = [
                    str(item)
                    for item in (delta.get("proposition_ids") or [])
                    if str(item or "").strip()
                ]
                normalized = list(dict.fromkeys([
                    *existing,
                    *sorted(missing_critical),
                ]))
                if normalized == existing:
                    continue
                changes.append({
                    "kind": "critical_proposition_intent",
                    "id": delta.get("target_delta_id"),
                    "from": existing,
                    "to": normalized,
                })
                delta["proposition_ids"] = normalized

    audience_states_by_id = {
        str(item.get("audience_state_id") or ""): item
        for item in (data.get("audience_states") or [])
        if (
            isinstance(item, dict)
            and str(item.get("audience_state_id") or "").strip()
        )
    }
    audience_priors_by_id = {
        str(item.get("audience_prior_id") or ""): item
        for item in (data.get("audience_priors") or [])
        if (
            isinstance(item, dict)
            and str(item.get("audience_prior_id") or "").strip()
        )
    }
    evidence_for_proposition = {
        proposition_id: [
            evidence_id
            for evidence_id, evidence in evidence_by_id.items()
            if proposition_id in (
                evidence.get("supports_proposition_ids") or []
            )
        ]
        for proposition_id in propositions_by_id
    }
    used_delta_ids = {
        str(delta.get("target_delta_id") or "")
        for intent in (data.get("experience_intents") or [])
        if isinstance(intent, dict)
        for path in (intent.get("audience_paths") or [])
        if isinstance(path, dict)
        for delta in (path.get("target_deltas") or [])
        if (
            isinstance(delta, dict)
            and str(delta.get("target_delta_id") or "").strip()
        )
    }
    removed_delta_ids: set[str] = set()

    def unique_delta_id(path_id: str, suffix: str) -> str:
        base = f"{path_id}-{suffix}"
        value = base
        counter = 2
        while value in used_delta_ids:
            value = f"{base}-{counter}"
            counter += 1
        used_delta_ids.add(value)
        return value

    intent_items = [
        intent
        for intent in (data.get("experience_intents") or [])
        if isinstance(intent, dict)
    ]
    prior_ids = [
        str(prior.get("audience_prior_id") or "").strip()
        for prior in (data.get("audience_priors") or [])
        if (
            isinstance(prior, dict)
            and str(prior.get("audience_prior_id") or "").strip()
        )
    ]
    states_by_prior: dict[str, list[str]] = {}
    for state_id, state in audience_states_by_id.items():
        prior_id = str(state.get("audience_prior_id") or "").strip()
        if prior_id:
            states_by_prior.setdefault(prior_id, []).append(state_id)
    used_path_ids = {
        str(path.get("audience_path_id") or "").strip()
        for intent in intent_items
        for path in (intent.get("audience_paths") or [])
        if (
            isinstance(path, dict)
            and str(path.get("audience_path_id") or "").strip()
        )
    }
    current_state_by_prior: dict[str, str] = {}
    for intent_index, intent in enumerate(intent_items, start=1):
        paths = [
            path
            for path in (intent.get("audience_paths") or [])
            if isinstance(path, dict)
        ]
        intent["audience_paths"] = paths
        paths_by_prior = {
            str(path.get("audience_prior_id") or "").strip(): path
            for path in paths
            if str(path.get("audience_prior_id") or "").strip()
        }
        for prior_id in prior_ids:
            if prior_id in paths_by_prior:
                continue
            state_id = current_state_by_prior.get(prior_id)
            if not state_id:
                state_id = next(
                    (
                        str(path.get("audience_state_in_id") or "")
                        for later_intent in intent_items[intent_index:]
                        for path in (
                            later_intent.get("audience_paths") or []
                        )
                        if (
                            isinstance(path, dict)
                            and str(
                                path.get("audience_prior_id") or ""
                            ).strip() == prior_id
                            and str(
                                path.get("audience_state_in_id") or ""
                            ).strip()
                        )
                    ),
                    "",
                )
            if not state_id:
                state_id = next(
                    iter(states_by_prior.get(prior_id) or []),
                    "",
                )
            if not state_id:
                continue
            base_path_id = (
                f"XP-{prior_id}-{intent.get('experience_intent_id')}"
            )
            path_id = base_path_id
            suffix = 2
            while path_id in used_path_ids:
                path_id = f"{base_path_id}-{suffix}"
                suffix += 1
            used_path_ids.add(path_id)
            target_state_id = state_id
            target_deltas: list[dict[str, Any]] = []
            attention_targets = [
                str(item)
                for item in (
                    intent.get("attention_target_ids") or []
                )
                if str(item or "").strip()
            ]
            source_state = audience_states_by_id.get(state_id)
            anchor_event_id = str(
                (intent.get("anchor_event_ids") or [""])[-1]
            )
            if attention_targets and source_state is not None:
                base_state_id = (
                    f"AS-{prior_id}-"
                    f"{intent.get('experience_intent_id')}-COARSE"
                )
                target_state_id = base_state_id
                state_suffix = 2
                while target_state_id in audience_states_by_id:
                    target_state_id = (
                        f"{base_state_id}-{state_suffix}"
                    )
                    state_suffix += 1
                target_state = deepcopy(source_state)
                target_state["audience_state_id"] = target_state_id
                target_state["anchor"] = {
                    "type": "event",
                    "id": anchor_event_id,
                }
                before_attention = list(
                    source_state.get("attention_residue_ids") or []
                )
                after_attention = list(dict.fromkeys([
                    *before_attention,
                    *attention_targets,
                ]))
                before_memory = deepcopy(
                    source_state.get("working_memory") or []
                )
                after_memory = deepcopy(before_memory)
                if after_attention == before_attention:
                    remembered = {
                        str(item.get("proposition_id") or "")
                        for item in after_memory
                        if isinstance(item, dict)
                    }
                    for proposition_id in attention_targets:
                        if proposition_id in remembered:
                            continue
                        after_memory.append({
                            "proposition_id": proposition_id,
                            "retention_confidence": 0.7,
                        })
                target_state["attention_residue_ids"] = after_attention
                target_state["working_memory"] = after_memory
                data.setdefault("audience_states", []).append(
                    target_state
                )
                audience_states_by_id[target_state_id] = target_state
                states_by_prior.setdefault(prior_id, []).append(
                    target_state_id
                )
                delta_id = unique_delta_id(path_id, "attention")
                event = events_by_id.get(anchor_event_id) or {}
                target_deltas.append({
                    "target_delta_id": delta_id,
                    "dimension": "attention",
                    "proposition_ids": attention_targets,
                    "description": (
                        "为缺失先验路径登记当前意图的注意目标"
                    ),
                    "from_state": {
                        "attention_residue_ids": before_attention,
                        "working_memory": before_memory,
                    },
                    "to_state": {
                        "attention_residue_ids": after_attention,
                        "working_memory": after_memory,
                    },
                    "target_confidence": None,
                    "required_processing_s": 0.5,
                    "deadline_event_id": anchor_event_id,
                    "primary_delivery_window_id": event.get(
                        "primary_delivery_window_id"
                    ),
                    "custom_dimension": None,
                })
            path = {
                "audience_path_id": path_id,
                "audience_prior_id": prior_id,
                "audience_state_in_id": state_id,
                "audience_state_out_target_id": target_state_id,
                "target_deltas": target_deltas,
            }
            paths.append(path)
            paths_by_prior[prior_id] = path
            changes.append({
                "kind": "coarse_audience_path",
                "id": path_id,
                "experience_intent_id": intent.get(
                    "experience_intent_id"
                ),
                "audience_prior_id": prior_id,
                "state_in_id": state_id,
                "state_out_id": target_state_id,
            })
        for prior_id, path in paths_by_prior.items():
            state_id = str(
                path.get("audience_state_out_target_id") or ""
            ).strip()
            if state_id:
                current_state_by_prior[prior_id] = state_id

    intent_paths_by_event_prior: dict[
        tuple[str, str],
        tuple[str, str],
    ] = {}
    for intent in intent_items:
        anchor_event_ids = [
            str(value or "").strip()
            for value in (intent.get("anchor_event_ids") or [])
            if str(value or "").strip()
        ]
        for path in intent.get("audience_paths") or []:
            if not isinstance(path, dict):
                continue
            prior_id = str(path.get("audience_prior_id") or "").strip()
            state_in_id = str(path.get("audience_state_in_id") or "").strip()
            state_out_id = str(
                path.get("audience_state_out_target_id") or ""
            ).strip()
            if not prior_id or not state_in_id or not state_out_id:
                continue
            for event_id in anchor_event_ids:
                intent_paths_by_event_prior[(event_id, prior_id)] = (
                    state_in_id,
                    state_out_id,
                )

    for scene in data.get("scene_contracts") or []:
        if not isinstance(scene, dict):
            continue
        paths = [
            path
            for path in (scene.get("audience_state_paths") or [])
            if isinstance(path, dict)
        ]
        scene["audience_state_paths"] = paths
        existing_priors = {
            str(path.get("audience_prior_id") or "").strip()
            for path in paths
        }
        scene_event_ids = [
            str(value or "").strip()
            for value in (scene.get("turn_event_ids") or [])
            if str(value or "").strip()
        ]
        for prior_id in prior_ids:
            if prior_id in existing_priors:
                continue
            scene_transitions = [
                intent_paths_by_event_prior[(event_id, prior_id)]
                for event_id in scene_event_ids
                if (event_id, prior_id) in intent_paths_by_event_prior
            ]
            if scene_transitions:
                state_in_id = scene_transitions[0][0]
                state_out_id = scene_transitions[-1][1]
            else:
                # Without an event-local transition, only the earliest known
                # state is temporally safe. The episode-final state may contain
                # facts learned in later scenes.
                state_in_id = next(
                    iter(states_by_prior.get(prior_id) or []),
                    "",
                )
                state_out_id = state_in_id
            if not state_in_id or not state_out_id:
                continue
            paths.append({
                "audience_prior_id": prior_id,
                "audience_state_in_id": state_in_id,
                "audience_state_out_target_id": state_out_id,
            })
            changes.append({
                "kind": "coarse_scene_audience_path",
                "id": scene.get("scene_id"),
                "audience_prior_id": prior_id,
                "state_in_id": state_in_id,
                "state_out_id": state_out_id,
            })

    for intent in intent_items:
        if not isinstance(intent, dict):
            continue
        for path in intent.get("audience_paths") or []:
            if not isinstance(path, dict):
                continue
            state_in = audience_states_by_id.get(
                str(path.get("audience_state_in_id") or "")
            )
            state_out = audience_states_by_id.get(
                str(path.get("audience_state_out_target_id") or "")
            )
            if state_in is None or state_out is None:
                continue
            deltas = [
                item
                for item in (path.get("target_deltas") or [])
                if isinstance(item, dict)
            ]
            path["target_deltas"] = deltas
            template = deltas[0] if deltas else {}
            deadline_event_id = str(
                template.get("deadline_event_id")
                or (intent.get("anchor_event_ids") or [""])[-1]
            )
            window_id = template.get("primary_delivery_window_id")

            incoming_beliefs = {
                str(item.get("proposition_id") or ""): item
                for item in (state_in.get("beliefs") or [])
                if isinstance(item, dict)
            }
            prior = audience_priors_by_id.get(
                str(path.get("audience_prior_id") or "")
            ) or {}
            assumed_unknown = {
                str(item)
                for item in (
                    prior.get("assumed_unknown_proposition_ids") or []
                )
            }
            outgoing_beliefs = {
                str(item.get("proposition_id") or ""): item
                for item in (state_out.get("beliefs") or [])
                if isinstance(item, dict)
            }
            for delta in deltas:
                if str(delta.get("dimension") or "") != "belief":
                    continue
                for proposition_id in delta.get("proposition_ids") or []:
                    proposition_id = str(proposition_id)
                    if (
                        proposition_id in incoming_beliefs
                        or proposition_id not in assumed_unknown
                    ):
                        continue
                    unknown_belief = {
                        "proposition_id": proposition_id,
                        "stance": "unknown",
                        "confidence": 0.0,
                        "evidence_ids": [],
                    }
                    state_in.setdefault("beliefs", []).append(
                        unknown_belief
                    )
                    incoming_beliefs[proposition_id] = unknown_belief
                    changes.append({
                        "kind": "audience_prior_unknown_belief",
                        "id": state_in.get("audience_state_id"),
                        "proposition_id": proposition_id,
                    })
                for proposition_id in delta.get("proposition_ids") or []:
                    proposition_id = str(proposition_id)
                    if proposition_id in outgoing_beliefs:
                        continue
                    target_confidence = float(
                        delta.get("target_confidence")
                        if delta.get("target_confidence") is not None
                        else 1.0
                    )
                    outgoing_beliefs[proposition_id] = {
                        "proposition_id": proposition_id,
                        "stance": "believed",
                        "confidence": target_confidence,
                        "evidence_ids": evidence_for_proposition.get(
                            proposition_id,
                            [],
                        ),
                    }
                    state_out.setdefault("beliefs", []).append(
                        outgoing_beliefs[proposition_id]
                    )
                    changes.append({
                        "kind": "audience_target_belief",
                        "id": state_out.get("audience_state_id"),
                        "proposition_id": proposition_id,
                    })
                proposition_ids = [
                    str(item)
                    for item in (delta.get("proposition_ids") or [])
                ]
                delta["from_state"] = {
                    "beliefs": [
                        item
                        for item in (state_in.get("beliefs") or [])
                        if (
                            isinstance(item, dict)
                            and str(item.get("proposition_id") or "")
                            in proposition_ids
                        )
                    ],
                }
                delta["to_state"] = {
                    "beliefs": [
                        item
                        for item in (state_out.get("beliefs") or [])
                        if (
                            isinstance(item, dict)
                            and str(item.get("proposition_id") or "")
                            in proposition_ids
                        )
                    ],
                }

            changed_belief_ids = {
                proposition_id
                for proposition_id in (
                    set(incoming_beliefs) | set(outgoing_beliefs)
                )
                if incoming_beliefs.get(proposition_id)
                != outgoing_beliefs.get(proposition_id)
            }
            covered_belief_ids = {
                str(proposition_id)
                for delta in deltas
                if str(delta.get("dimension") or "") == "belief"
                for proposition_id in (
                    delta.get("proposition_ids") or []
                )
            }
            missing_belief_ids = sorted(
                changed_belief_ids - covered_belief_ids
            )
            if missing_belief_ids:
                delta = {
                    "target_delta_id": unique_delta_id(
                        str(path.get("audience_path_id") or "path"),
                        "belief",
                    ),
                    "dimension": "belief",
                    "proposition_ids": missing_belief_ids,
                    "description": "绑定该观众路径中实际发生的信念变化",
                    "from_state": {
                        "beliefs": [
                            item
                            for item in (state_in.get("beliefs") or [])
                            if (
                                isinstance(item, dict)
                                and str(item.get("proposition_id") or "")
                                in missing_belief_ids
                            )
                        ],
                    },
                    "to_state": {
                        "beliefs": [
                            item
                            for item in (state_out.get("beliefs") or [])
                            if (
                                isinstance(item, dict)
                                and str(item.get("proposition_id") or "")
                                in missing_belief_ids
                            )
                        ],
                    },
                    "target_confidence": None,
                    "required_processing_s": 0.5,
                    "deadline_event_id": deadline_event_id,
                    "primary_delivery_window_id": window_id,
                    "custom_dimension": None,
                }
                deltas.append(delta)
                changes.append({
                    "kind": "audience_belief_delta",
                    "id": delta["target_delta_id"],
                    "proposition_ids": missing_belief_ids,
                })

            attention_changed = (
                state_in.get("attention_residue_ids")
                != state_out.get("attention_residue_ids")
                or state_in.get("working_memory")
                != state_out.get("working_memory")
            )
            attention_delta = next((
                delta
                for delta in deltas
                if str(delta.get("dimension") or "") == "attention"
            ), None)
            if attention_changed:
                if attention_delta is None:
                    attention_delta = {
                        "target_delta_id": unique_delta_id(
                            str(path.get("audience_path_id") or "path"),
                            "attention",
                        ),
                        "dimension": "attention",
                        "proposition_ids": sorted({
                            str(item.get("proposition_id") or "")
                            for item in (
                                state_out.get("working_memory") or []
                            )
                            if (
                                isinstance(item, dict)
                                and str(
                                    item.get("proposition_id") or ""
                                ).strip()
                            )
                        }),
                        "description": "绑定注意残留与工作记忆变化",
                        "target_confidence": None,
                        "required_processing_s": 0.5,
                        "deadline_event_id": deadline_event_id,
                        "primary_delivery_window_id": window_id,
                        "custom_dimension": None,
                    }
                    deltas.append(attention_delta)
                    changes.append({
                        "kind": "audience_attention_delta",
                        "id": attention_delta["target_delta_id"],
                    })
                attention_delta["from_state"] = {
                    "attention_residue_ids": list(
                        state_in.get("attention_residue_ids") or []
                    ),
                    "working_memory": deepcopy(
                        state_in.get("working_memory") or []
                    ),
                }
                attention_delta["to_state"] = {
                    "attention_residue_ids": list(
                        state_out.get("attention_residue_ids") or []
                    ),
                    "working_memory": deepcopy(
                        state_out.get("working_memory") or []
                    ),
                }

            if state_in.get("affective_state") != state_out.get(
                "affective_state"
            ):
                affective_delta = next((
                    delta
                    for delta in deltas
                    if str(delta.get("dimension") or "") == "affective"
                ), None)
                if affective_delta is None:
                    affective_delta = {
                        "target_delta_id": unique_delta_id(
                            str(path.get("audience_path_id") or "path"),
                            "affective",
                        ),
                        "dimension": "affective",
                        "proposition_ids": [],
                        "description": "绑定观众入场与目标出场的情绪状态变化",
                        "target_confidence": None,
                        "required_processing_s": 0.0,
                        "deadline_event_id": deadline_event_id,
                        "primary_delivery_window_id": window_id,
                        "custom_dimension": None,
                    }
                    deltas.append(affective_delta)
                    changes.append({
                        "kind": "audience_affective_delta",
                        "id": affective_delta["target_delta_id"],
                    })
                affective_delta["from_state"] = {
                    "affective_state": dict(
                        state_in.get("affective_state") or {}
                    ),
                }
                affective_delta["to_state"] = {
                    "affective_state": dict(
                        state_out.get("affective_state") or {}
                    ),
                }
                if not affective_delta.get("deadline_event_id"):
                    affective_delta["deadline_event_id"] = deadline_event_id
                if (
                    not affective_delta.get("primary_delivery_window_id")
                    and window_id
                ):
                    affective_delta["primary_delivery_window_id"] = window_id
                changes.append({
                    "kind": "audience_affective_delta_state",
                    "id": affective_delta["target_delta_id"],
                })

            if (
                state_in.get("active_question_ids")
                != state_out.get("active_question_ids")
            ):
                question_delta = next((
                    delta
                    for delta in deltas
                    if (
                        str(delta.get("dimension") or "") == "other"
                        and str(delta.get("custom_dimension") or "")
                        == "active_question_ids"
                    )
                ), None)
                if question_delta is None:
                    question_delta = {
                        "target_delta_id": unique_delta_id(
                            str(path.get("audience_path_id") or "path"),
                            "questions",
                        ),
                        "dimension": "other",
                        "proposition_ids": [],
                        "description": "绑定观众主动问题集合变化",
                        "target_confidence": None,
                        "required_processing_s": 0.0,
                        "deadline_event_id": deadline_event_id,
                        "primary_delivery_window_id": window_id,
                        "custom_dimension": "active_question_ids",
                    }
                    deltas.append(question_delta)
                    changes.append({
                        "kind": "audience_question_delta",
                        "id": question_delta["target_delta_id"],
                    })
                question_delta["from_state"] = {
                    "active_question_ids": list(
                        state_in.get("active_question_ids") or []
                    ),
                }
                question_delta["to_state"] = {
                    "active_question_ids": list(
                        state_out.get("active_question_ids") or []
                    ),
                }
            retained_deltas = []
            for delta in deltas:
                semantic_no_change = (
                    delta.get("from_state") == delta.get("to_state")
                )
                if str(delta.get("dimension") or "") == "belief":
                    proposition_ids = {
                        str(item)
                        for item in (
                            delta.get("proposition_ids") or []
                        )
                    }
                    before_beliefs = {
                        str(item.get("proposition_id") or ""): (
                            item.get("stance"),
                            item.get("confidence"),
                        )
                        for item in (state_in.get("beliefs") or [])
                        if (
                            isinstance(item, dict)
                            and str(
                                item.get("proposition_id") or ""
                            ) in proposition_ids
                        )
                    }
                    after_beliefs = {
                        str(item.get("proposition_id") or ""): (
                            item.get("stance"),
                            item.get("confidence"),
                        )
                        for item in (state_out.get("beliefs") or [])
                        if (
                            isinstance(item, dict)
                            and str(
                                item.get("proposition_id") or ""
                            ) in proposition_ids
                        )
                    }
                    semantic_no_change = (
                        bool(proposition_ids)
                        and all(
                            before_beliefs.get(proposition_id)
                            == after_beliefs.get(proposition_id)
                            for proposition_id in proposition_ids
                        )
                    )
                if not semantic_no_change:
                    retained_deltas.append(delta)
                    continue
                delta_id = str(
                    delta.get("target_delta_id") or ""
                ).strip()
                if delta_id:
                    removed_delta_ids.add(delta_id)
                changes.append({
                    "kind": "no_change_target_delta_removed",
                    "id": delta_id,
                    "path_id": path.get("audience_path_id"),
                })
            path["target_deltas"] = retained_deltas

    if removed_delta_ids:
        for window in data.get("readability_windows") or []:
            if not isinstance(window, dict):
                continue
            existing = list(window.get("target_delta_ids") or [])
            normalized = [
                delta_id
                for delta_id in existing
                if str(delta_id) not in removed_delta_ids
            ]
            if normalized != existing:
                window["target_delta_ids"] = normalized
                changes.append({
                    "kind": "removed_delta_window_refs",
                    "id": window.get("readability_window_id"),
                    "from": existing,
                    "to": normalized,
                })
        existing_tasks = list(data.get("assimilation_tasks") or [])
        normalized_tasks = [
            task
            for task in existing_tasks
            if (
                not isinstance(task, dict)
                or str(task.get("target_delta_id") or "")
                not in removed_delta_ids
            )
        ]
        if normalized_tasks != existing_tasks:
            data["assimilation_tasks"] = normalized_tasks
            changes.append({
                "kind": "removed_delta_assimilation_tasks",
                "removed_target_delta_ids": sorted(removed_delta_ids),
            })

    delta_requirements: dict[str, tuple[str, float]] = {}
    delta_windows: dict[str, str] = {}
    for intent in data.get("experience_intents") or []:
        if not isinstance(intent, dict):
            continue
        for path in intent.get("audience_paths") or []:
            if not isinstance(path, dict):
                continue
            prior_id = str(path.get("audience_prior_id") or "unknown")
            for delta in path.get("target_deltas") or []:
                if not isinstance(delta, dict):
                    continue
                delta_id = str(delta.get("target_delta_id") or "").strip()
                if not delta_id:
                    continue
                try:
                    required = max(
                        0.0, float(delta.get("required_processing_s") or 0),
                    )
                except (TypeError, ValueError):
                    required = 0.0
                delta_requirements[delta_id] = (prior_id, required)
                window_id = str(
                    delta.get("primary_delivery_window_id") or ""
                ).strip()
                if window_id:
                    delta_windows[delta_id] = window_id
    windows_by_id = {
        str(window.get("readability_window_id") or ""): window
        for window in (data.get("readability_windows") or [])
        if (
            isinstance(window, dict)
            and str(window.get("readability_window_id") or "").strip()
        )
    }
    for delta_id, window_id in delta_windows.items():
        window = windows_by_id.get(window_id)
        if window is None:
            continue
        existing_ids = list(window.get("target_delta_ids") or [])
        if delta_id in existing_ids:
            continue
        normalized_ids = [*existing_ids, delta_id]
        changes.append({
            "kind": "readability_target_delta_ref",
            "id": window_id,
            "from": existing_ids,
            "to": normalized_ids,
        })
        window["target_delta_ids"] = normalized_ids
    for window in data.get("readability_windows") or []:
        if not isinstance(window, dict):
            continue
        required_by_prior: dict[str, float] = {}
        for delta_id in window.get("target_delta_ids") or []:
            prior_id, required = delta_requirements.get(
                str(delta_id), ("unknown", 0.0),
            )
            required_by_prior[prior_id] = (
                required_by_prior.get(prior_id, 0.0) + required
            )
        required_processing = max(required_by_prior.values(), default=0.0)
        try:
            scheduled = float(window.get("scheduled_processing_s") or 0)
        except (TypeError, ValueError):
            scheduled = 0.0
        try:
            available = float(window.get("planned_available_s") or 0)
        except (TypeError, ValueError):
            available = 0.0
        normalized_scheduled = max(scheduled, required_processing)
        normalized_available = max(available, normalized_scheduled)
        if (
            normalized_scheduled != scheduled
            or normalized_available != available
        ):
            changes.append({
                "kind": "readability_budget",
                "id": window.get("readability_window_id"),
                "from": {
                    "scheduled_processing_s": scheduled,
                    "planned_available_s": available,
                },
                "to": {
                    "scheduled_processing_s": normalized_scheduled,
                    "planned_available_s": normalized_available,
                },
            })
            window["scheduled_processing_s"] = normalized_scheduled
            window["planned_available_s"] = normalized_available

    valid_identity_refs = {
        "source_evidence_ids": {
            str(item.get("source_evidence_id") or "")
            for item in (data.get("source_evidence") or [])
            if isinstance(item, dict)
        },
        "proposition_ids": {
            str(item.get("proposition_id") or "")
            for item in (data.get("propositions") or [])
            if isinstance(item, dict)
        },
        "adaptation_decision_ids": {
            str(item.get("adaptation_decision_id") or "")
            for item in (data.get("adaptation_decisions") or [])
            if isinstance(item, dict)
        },
    }
    for contract in data.get("identity_contracts") or []:
        if not isinstance(contract, dict):
            continue
        evidence = contract.get("evidence")
        if not isinstance(evidence, dict):
            continue
        normalized_fields = {
            field: [
                value
                for value in (evidence.get(field) or [])
                if str(value or "") in valid_ids
            ]
            for field, valid_ids in valid_identity_refs.items()
        }
        if not any(normalized_fields.values()):
            continue
        for field, normalized_values in normalized_fields.items():
            existing_values = list(evidence.get(field) or [])
            if normalized_values == existing_values:
                continue
            changes.append({
                "kind": "identity_evidence_refs",
                "id": contract.get("identity_id"),
                "field": field,
                "from": existing_values,
                "to": normalized_values,
            })
            evidence[field] = normalized_values

    if changes:
        script.narrative_plan = type(plan).model_validate(data)
    return changes


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


def _complete_screenplay_from_working_artifact(
    *,
    episode_id: str,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    revision_id: str,
    run_id: str | None,
    checkpoint: dict[str, Any],
    activation_no: int,
) -> EpisodeScreenplay:
    """Validate every production gate and publish only a zero-blocker artifact."""
    from app.harness.contracts import get_contract
    from app.portraits import (
        apply_screenplay_character_resolutions,
        normalize_screenplay_voice_ids,
        screenplay_character_resolution_errors,
        screenplay_unknown_identity_errors,
    )
    from app.production.screenplay_authority import (
        SCREENPLAY_QA_PROFILE_VERSION,
        screenplay_authority_fingerprint,
    )

    conn = get_conn()
    rev = get_production_revision(revision_id)
    if rev is None or not rev.working_artifact_id:
        raise RuntimeError("剧本结构校验缺少 working Artifact")
    working_id = rev.working_artifact_id
    artifact = evidence_repository.get_artifact(working_id)
    if artifact is None:
        raise RuntimeError("剧本 working Artifact 不存在")
    artifact_hash = artifact.get("content_hash") or evidence_repository.content_hash(
        artifact.get("content")
    )
    script = load_screenplay_from_artifact(working_id)

    save_checkpoint(revision_id, {
        **checkpoint,
        "phase": "STRUCTURE_VALIDATION",
        "activation_no": activation_no,
        "working_artifact_id": working_id,
        "yield_reason": None,
    })
    conn.execute(
        "UPDATE episodes SET screenplay_status='running',screenplay_error=?,"
        "screenplay_updated_at=? WHERE id=?",
        ("正在校验剧本结构与人物上下文", now(), episode_id),
    )
    conn.commit()
    if run_id:
        evidence_repository.append_event(
            run_id,
            "STRUCTURE_VALIDATION_STARTED",
            "info",
            "开始校验剧本结构与人物上下文",
            payload={"artifact_id": working_id},
        )

    normalization_changes = apply_screenplay_character_resolutions(
        script,
        episode.get("character_resolutions") or [],
    )
    normalization_changes.extend(normalize_screenplay_voice_ids(script, bible))
    normalization_changes.extend(_normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters=episode.get("authorized_source_chapters"),
    ))
    if normalization_changes:
        payload = screenplay_artifact_payload(script)
        normalized_hash = evidence_repository.content_hash(payload)
        if normalized_hash != artifact_hash:
            normalized = evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_document",
                    scope_type="episode",
                    scope_id=episode_id,
                    status="candidate",
                    trust_level="T1",
                    content=payload,
                    parent_artifact_ids=[working_id],
                    contract_version=rev.contract_version or None,
                )
            )
            update_working_artifact(
                revision_id,
                normalized["id"],
                expected_hash=artifact_hash,
            )
            working_id = normalized["id"]
            artifact_hash = normalized["content_hash"]

    identity_errors = list(dict.fromkeys([
        *screenplay_character_resolution_errors(
            script,
            episode.get("character_resolutions") or [],
        ),
        *screenplay_unknown_identity_errors(
            script,
            bible,
            episode.get("character_resolutions") or [],
        ),
    ]))
    if identity_errors:
        message = ("剧本缺少可确定的人物身份上下文：" + "；".join(identity_errors[:5]))[:800]
        conn.execute(
            "UPDATE episodes SET screenplay_status='failed',screenplay_error=?,"
            "screenplay_updated_at=? WHERE id=?",
            (message, now(), episode_id),
        )
        conn.commit()
        save_checkpoint(revision_id, {
            **checkpoint,
            "phase": "STRUCTURE_FAILED",
            "activation_no": activation_no,
            "working_artifact_id": working_id,
            "yield_reason": "character_identity_context_missing",
            "structural_errors": identity_errors,
        })
        raise ScreenplayIdentityGateError(message)

    save_checkpoint(revision_id, {
        **checkpoint,
        "phase": "QUALITY_SCORING",
        "activation_no": activation_no,
        "working_artifact_id": working_id,
        "yield_reason": None,
    })
    conn.execute(
        "UPDATE episodes SET screenplay_error=?,screenplay_updated_at=? WHERE id=?",
        ("结构校验已通过，正在记录质量评分", now(), episode_id),
    )
    conn.commit()
    issues, evaluation = run_screenplay_qa(
        script,
        bible=bible,
        source_text=source_text,
        episode=episode,
        artifact_id=working_id,
        artifact_hash=artifact_hash,
    )
    evaluation_row = evidence_repository.create_evaluation(working_id, evaluation)
    evaluation_id = _eval_id_from_create(evaluation_row)
    if not rev.first_evaluation_done:
        rev = mark_first_evaluation(
            revision_id,
            evaluation_id or f"eval-{working_id}",
        )

    contract_version = get_contract("screenplay").version
    current_fingerprint = screenplay_authority_fingerprint(
        episode_id,
        conn=conn,
        source_text=source_text,
        # Discovery in another concurrent episode may advance the persisted
        # composite Bible after this run loaded its generation snapshot.
        # Publication binds the current durable authority projection.
        bible=None,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    if rev.input_fingerprint != current_fingerprint:
        rev = rebind_input_fingerprint(
            revision_id,
            input_fingerprint=current_fingerprint,
            expected_working_artifact_id=working_id,
        )

    save_checkpoint(revision_id, {
        **checkpoint,
        "phase": "PUBLISHING",
        "activation_no": activation_no,
        "working_artifact_id": working_id,
        "quality_issue_count": len(issues),
        "quality_score": evaluation.score,
        "yield_reason": None,
    })
    conn.execute(
        "UPDATE episodes SET screenplay_error=?,screenplay_updated_at=? WHERE id=?",
        ("质量评分已记录，正在原子发布剧本", now(), episode_id),
    )
    conn.commit()
    published = publish_screenplay(
        episode_id=episode_id,
        revision_id=revision_id,
        artifact_id=working_id,
        artifact_hash=artifact_hash,
        evaluation_ids=[evaluation_id] if evaluation_id else [],
        input_fingerprint=current_fingerprint,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        clear_downstream=True,
    )
    save_checkpoint(revision_id, {
        **checkpoint,
        "phase": "SUCCEEDED",
        "activation_no": activation_no,
        "working_artifact_id": working_id,
        "quality_issue_count": len(issues),
        "quality_score": evaluation.score,
        "gate_retry_exhausted": bool(issues),
        "yield_reason": None,
    })
    if run_id:
        evidence_repository.append_event(
            run_id,
            "PUBLISHED",
            "info",
            "剧本结构已发布，质量问题仅作为评分提示",
            payload={
                **published,
                "quality_score": evaluation.score,
                "quality_issue_count": len(issues),
            },
        )
    return load_screenplay_from_artifact(working_id)


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
    if not isinstance(episode.get("authorized_source_chapters"), dict):
        from app.production.screenplay_authority import (
            screenplay_authorized_source_chapters,
        )

        try:
            episode["authorized_source_chapters"] = (
                screenplay_authorized_source_chapters(episode_id)
            )
        except ValueError:
            episode["authorized_source_chapters"] = {}
    contract = get_contract("screenplay")
    from app.production.screenplay_authority import screenplay_authority_fingerprint

    input_fp = screenplay_authority_fingerprint(
        episode_id,
        conn=conn,
        source_text=source_text,
        bible=bible,
        contract_version=contract.version,
        qa_profile_version="screenplay-qa-gate-2",
    )

    rev = ensure_production_revision(
        episode_id=episode_id,
        kind="screenplay",
        input_fingerprint=input_fp,
        contract_version=contract.version,
        qa_profile_version="screenplay-qa-gate-2",
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
        # 新规划器接管旧 working artifact 时重置已耗尽策略，不再恢复退役的固定对白上限。
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

    def _publish_retry_exhausted_fallback(
        current_rev,
        *,
        working_id: str,
        artifact_hash: str,
        evaluation_id: str | None,
        open_issues: list[Issue],
        reason: str,
        failed_issue: Issue | None = None,
    ) -> EpisodeScreenplay:
        """Preserve the working artifact and stop when hard gates remain open.

        This function intentionally keeps its historical name so persisted
        checkpoints can resume across the upgrade.  It no longer issues a
        completion certificate or publishes an unvalidated candidate.
        """
        hard_issues = non_waivable_screenplay_issues(open_issues)
        identity_issues = screenplay_identity_gate_issues(open_issues)
        if hard_issues and len(identity_issues) == len(hard_issues):
            message = (
                "剧本人物身份预检未通过，已在剧本阶段停止："
                + "；".join(issue.message for issue in identity_issues[:5])
            )[:800]
            conn.execute(
                "UPDATE episodes SET screenplay_status='failed',screenplay_error=?,screenplay_updated_at=? "
                "WHERE id=?",
                (message, now(), episode_id),
            )
            conn.commit()
            save_checkpoint(current_rev.id, {
                **checkpoint,
                "phase": "FAILED",
                "activation_no": activation_no,
                "working_artifact_id": working_id,
                "open_issue_ids": [issue.fingerprint for issue in open_issues],
                "issue_strategy_history": strategy_history,
                "patch_artifact_ids": patch_ids,
                "last_issue_fingerprints": [issue.fingerprint for issue in open_issues],
                "yield_reason": "character_identity_hard_gate",
                "fallback_reason": reason,
            })
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "CHARACTER_IDENTITY_HARD_GATE_BLOCKED",
                    "error",
                    "人物身份未解决，禁止发布剧本和启动分镜",
                    payload={
                        "reason": reason,
                        "issues": [issue.model_dump(mode="json") for issue in identity_issues],
                    },
                )
            raise ScreenplayIdentityGateError(message)
        message = _gate_failure_message(
            open_issues,
            failed_issue=failed_issue,
        )
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing',screenplay_error=?,screenplay_updated_at=? WHERE id=?",
            (message, now(), episode_id),
        )
        conn.commit()
        save_checkpoint(current_rev.id, {
            **checkpoint,
            "phase": "WAITING_HUMAN",
            "activation_no": activation_no,
            "working_artifact_id": working_id,
            "open_issue_ids": [issue.fingerprint for issue in open_issues],
            "issue_strategy_history": strategy_history,
            "patch_artifact_ids": patch_ids,
            "last_issue_fingerprints": [issue.fingerprint for issue in open_issues],
            "yield_reason": "narrative_gate_needs_review",
            "fallback_reason": reason,
        })
        if run_id:
            evidence_repository.append_event(
                run_id,
                "NARRATIVE_GATE_NEEDS_REVIEW",
                "error",
                "剧本门禁重试耗尽；工作稿已保留，未发布",
                payload={
                    "artifact_id": working_id,
                    "reason": reason,
                    "issue_count": len(open_issues),
                    "failed_issue": (
                        failed_issue.model_dump(mode="json")
                        if failed_issue is not None else None
                    ),
                },
            )
        raise ScreenplayNarrativeGateError(message)

    # ---- Baseline（仅一次）----
    baseline_created_this_activation = False
    if not rev.baseline_done:
        assert_baseline_allowed(rev, command="screenplay.generate", episode_id=episode_id)
        save_checkpoint(rev.id, {
            **checkpoint,
            "phase": "GENERATING_BASELINE",
            "activation_no": activation_no,
            "yield_reason": None,
        })
        if run_id:
            evidence_repository.append_event(
                run_id, "BASELINE_GENERATION_STARTED", "info",
                "剧本 Baseline 生成（本 revision 仅此一次）",
                payload={"revision_id": rev.id},
            )
        script = await generate_screenplay_baseline(
            episode, source_text, bible, prev_ending=prev_ending,
        )
        from app.renderability import screenplay_required_duration_s

        current_target = int(
            episode.get("target_duration_s")
            or config.EPISODE_TARGET_DEFAULT_S
        )
        required_target = screenplay_required_duration_s(
            script,
            minimum_s=current_target,
        )
        duration_expanded = required_target > current_target
        if duration_expanded:
            conn.execute(
                "UPDATE episodes SET target_duration_s=?,"
                "screenplay_snapshot_version=screenplay_snapshot_version+1 "
                "WHERE id=?",
                (required_target, episode_id),
            )
            conn.commit()
            episode["target_duration_s"] = required_target
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "SCREENPLAY_DURATION_EXPANDED",
                    "info",
                    "已按完整剧情与口播容量自动扩展整集时长",
                    payload={
                        "previous_target_s": current_target,
                        "required_target_s": required_target,
                    },
                )
        # 先应用首次预检已有决议并持久化 Baseline。任何后续人物增量调用
        # 都必须发生在这个耐久边界之后，避免外部失败迫使完整剧本重生。
        from app.portraits import (
            apply_screenplay_character_resolutions,
            normalize_screenplay_voice_ids,
        )
        apply_screenplay_character_resolutions(
            script,
            episode.get("character_resolutions") or [],
        )
        normalize_screenplay_voice_ids(script, bible)
        _normalize_screenplay_narrative_graph(
            script,
            authorized_source_chapters=episode.get("authorized_source_chapters"),
        )

        source_ir_artifact_id = str(
            getattr(script, "_source_ir_artifact_id", None) or ""
        )
        compiler_version = str(
            getattr(script, "_ir_compiler_version", "") or ""
        )
        compiler_audit = list(
            getattr(script, "_ir_compiler_audit", []) or []
        )
        from app.validators import normalize_screenplay_candidate
        script = normalize_screenplay_candidate(
            script,
            source_text=source_text,
        )
        payload = screenplay_artifact_payload(script)
        candidate_parent_ids: list[str] = (
            [source_ir_artifact_id] if source_ir_artifact_id else []
        )
        if run_id and not candidate_parent_ids:
            candidate_row = conn.execute(
                "SELECT output_artifact_id FROM step_runs "
                "WHERE run_id=? AND step_key='screenplay.iteration' "
                "AND output_artifact_id IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if candidate_row and candidate_row["output_artifact_id"]:
                candidate_parent_ids.append(candidate_row["output_artifact_id"])
        baseline_art = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_document",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T1",
                content=payload,
                parent_artifact_ids=candidate_parent_ids,
                contract_version=contract.version,
                model_snapshot={
                    "generation_contract": str(
                        getattr(script, "source_text_range", "") or ""
                    ),
                    "compiler_version": str(
                        compiler_version
                    ),
                    "compiler_audit_count": len(compiler_audit),
                },
            )
        )
        rev = mark_baseline_generated(
            rev.id,
            baseline_artifact_id=baseline_art["id"],
            working_artifact_id=baseline_art["id"],
        )
        if duration_expanded:
            input_fp = screenplay_authority_fingerprint(
                episode_id,
                conn=conn,
                source_text=source_text,
                # The Baseline is durable now. Rebind its duration change to
                # the latest persisted composite Bible, which concurrent
                # episode discovery may have advanced during generation.
                bible=None,
                contract_version=contract.version,
                qa_profile_version="screenplay-qa-gate-2",
            )
            rev = rebind_input_fingerprint(
                rev.id,
                input_fingerprint=input_fp,
                expected_working_artifact_id=baseline_art["id"],
            )
        baseline_created_this_activation = True
        checkpoint = {
            **checkpoint,
            "phase": "IDENTITY_AUDIT",
            "activation_no": activation_no,
            "working_artifact_id": baseline_art["id"],
            "source_ir_artifact_id": source_ir_artifact_id or None,
            "compiler_version": compiler_version,
            "compiler_audit": compiler_audit[:200],
            "yield_reason": None,
        }
        save_checkpoint(rev.id, checkpoint)
        record_baseline_generation(
            kind="screenplay", episode_id=episode_id, revision_id=rev.id,
        )
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
            "screenplay_updated_at=? WHERE id=?",
            ("首次整版 Baseline 已落库，正在执行只读 QA", now(), episode_id),
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

    # ---- Baseline 后身份收口（可恢复，不消耗第二次完整生成）----
    rev = get_production_revision(rev.id)  # type: ignore[assignment]
    assert rev and rev.working_artifact_id
    working_id = rev.working_artifact_id
    working_artifact = evidence_repository.get_artifact(working_id)
    assert working_artifact
    working_hash = (
        working_artifact.get("content_hash")
        or evidence_repository.content_hash(working_artifact.get("content"))
    )
    working_script = load_screenplay_from_artifact(working_id)
    identity_audit_required = (
        baseline_created_this_activation
        or checkpoint.get("phase") == "IDENTITY_AUDIT"
    )
    from app.portraits import (
        apply_screenplay_character_resolutions,
        bible_with_provisional_characters,
        merge_screenplay_character_resolutions,
        normalize_screenplay_voice_ids,
        screenplay_unknown_identity_errors,
    )

    # Reapply durable identity resolutions before every QA entry, including
    # resume paths whose checkpoint already advanced beyond IDENTITY_AUDIT.
    identity_normalization_changes = apply_screenplay_character_resolutions(
        working_script,
        episode.get("character_resolutions") or [],
    )
    identity_normalization_changes.extend(
        normalize_screenplay_voice_ids(working_script, bible)
    )
    draft_identity_errors = screenplay_unknown_identity_errors(
        working_script,
        bible,
        episode.get("character_resolutions") or [],
    )
    if draft_identity_errors:
        draft_audit = await ensure_source_characters_incremental(
            episode_id,
            source_text,
            draft_text=working_script.model_dump_json(),
        )
        previous_resolutions = list(
            episode.get("character_resolutions") or []
        )
        episode["character_resolutions"] = merge_screenplay_character_resolutions(
            previous_resolutions,
            draft_audit.get("resolutions") or [],
        )
        previous_resolution_pairs = {
            (
                str(item.get("source_label") or "").strip(),
                str(item.get("canonical_name") or "").strip(),
            )
            for item in previous_resolutions
            if isinstance(item, dict)
        }
        merged_resolution_pairs = {
            (
                str(item.get("source_label") or "").strip(),
                str(item.get("canonical_name") or "").strip(),
            )
            for item in episode["character_resolutions"]
            if isinstance(item, dict)
        }
        if merged_resolution_pairs - previous_resolution_pairs:
            identity_normalization_changes.extend(
                apply_screenplay_character_resolutions(
                    working_script,
                    episode["character_resolutions"],
                )
            )
        if draft_audit.get("added"):
            project = conn.execute(
                "SELECT * FROM projects WHERE id=?",
                (episode["project_id"],),
            ).fetchone()
            from app.domain.common import _project_bible_or_placeholder

            bible = _project_bible_or_placeholder(project)
        bible = bible_with_provisional_characters(bible, draft_audit)
        identity_normalization_changes.extend(
            normalize_screenplay_voice_ids(working_script, bible)
        )
    elif run_id:
        evidence_repository.append_event(
            run_id,
            "CHARACTER_DISCOVERY_DRAFT_AUDIT_SKIPPED",
            "info",
            "Baseline 身份合同已静态闭合，跳过生成后人物模型审计",
            payload={"episode_id": episode_id},
        )

    if identity_normalization_changes:
        identity_normalization_changes.extend(
            _normalize_screenplay_narrative_graph(
                working_script,
                authorized_source_chapters=episode.get(
                    "authorized_source_chapters"
                ),
            )
        )
    normalized_payload = screenplay_artifact_payload(working_script)
    normalized_hash = evidence_repository.content_hash(normalized_payload)
    if identity_normalization_changes and normalized_hash != working_hash:
        normalized_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_document",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T1",
                content=normalized_payload,
                parent_artifact_ids=[working_id],
                contract_version=rev.contract_version or contract.version,
            )
        )
        update_working_artifact(
            rev.id,
            normalized_artifact["id"],
            expected_hash=working_hash,
        )
        rev = get_production_revision(rev.id)  # type: ignore[assignment]
        assert rev
        working_id = normalized_artifact["id"]
        working_hash = normalized_artifact["content_hash"]
        if run_id:
            evidence_repository.append_event(
                run_id,
                "SCREENPLAY_IDENTITY_NORMALIZATION_APPLIED",
                "info",
                "已从耐久 Baseline 派生身份规范化工作副本",
                payload={
                    "before_artifact_id": working_artifact["id"],
                    "after_artifact_id": working_id,
                },
            )
    if identity_audit_required:
        checkpoint = {
            **checkpoint,
            "phase": "STRUCTURE_VALIDATION",
            "activation_no": activation_no,
            "working_artifact_id": working_id,
            "yield_reason": None,
        }
        save_checkpoint(rev.id, checkpoint)

    # All issues marked must_fix/runtime_blocking enter the bounded local Patch
    # loop. Quality-only findings remain score-only and never masquerade as a
    # passed runtime gate.
    initial_issues, _initial_evaluation = run_screenplay_qa(
        working_script,
        bible=bible,
        source_text=source_text,
        episode=episode,
        artifact_id=working_id,
        artifact_hash=working_hash,
    )
    initial_hard_issues = non_waivable_screenplay_issues(initial_issues)
    if not initial_hard_issues:
        return _complete_screenplay_from_working_artifact(
            episode_id=episode_id,
            episode=episode,
            source_text=source_text,
            bible=bible,
            revision_id=rev.id,
            run_id=run_id,
            checkpoint=checkpoint,
            activation_no=activation_no,
        )
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing',screenplay_error=?,"
        "screenplay_updated_at=? WHERE id=?",
        (
            f"剧本有 {len(initial_hard_issues)} 项结构或业务硬门禁问题，正在局部修复",
            now(),
            episode_id,
        ),
    )
    conn.commit()

    # ---- Repair loop ----
    patches_this_activation = 0
    attempts_this_activation = 0
    passes_this_activation = 0
    prev_issue_fps: set[str] = set(checkpoint.get("last_issue_fingerprints") or [])

    while (
        attempts_this_activation < MAX_REPAIR_ACTIVATION_PATCHES
        and passes_this_activation < MAX_REPAIR_ACTIVATION_PASSES
    ):
        passes_this_activation += 1
        rev = get_production_revision(rev.id)  # type: ignore[assignment]
        working_id = rev.working_artifact_id
        assert working_id
        art = evidence_repository.get_artifact(working_id)
        assert art
        artifact_hash = art.get("content_hash") or evidence_repository.content_hash(art.get("content"))
        script = load_screenplay_from_artifact(working_id)

        # 身份决议可能来自 Baseline 后审计、服务恢复或人工入口。无论从哪条路
        # 进入 Repair，都先把它派生为新的 working artifact，再做 QA/局部 Patch。
        from app.portraits import (
            apply_screenplay_character_resolutions,
            normalize_screenplay_voice_ids,
        )
        normalization_changes = apply_screenplay_character_resolutions(
            script,
            episode.get("character_resolutions") or [],
        )
        normalization_changes.extend(normalize_screenplay_voice_ids(script, bible))
        normalization_changes.extend(_normalize_screenplay_narrative_graph(
            script,
            authorized_source_chapters=episode.get("authorized_source_chapters"),
        ))
        if normalization_changes:
            normalization_payload = screenplay_artifact_payload(script)
            normalization_hash = evidence_repository.content_hash(
                normalization_payload
            )
            if normalization_hash == artifact_hash:
                # Some labels remain in non-identity prose by design. The
                # resolver may report those replacements on every replay even
                # though the canonical artifact payload is already identical.
                # Hash equality is the authoritative idempotency boundary.
                normalization_changes = []
        if normalization_changes:
            normalization_artifact = evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_document",
                    scope_type="episode",
                    scope_id=episode_id,
                    status="candidate",
                    trust_level="T1",
                    content=normalization_payload,
                    parent_artifact_ids=[working_id],
                    contract_version=rev.contract_version or None,
                )
            )
            update_working_artifact(
                rev.id,
                normalization_artifact["id"],
                expected_hash=artifact_hash,
            )
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "SCREENPLAY_DETERMINISTIC_NORMALIZATION_APPLIED",
                    "info",
                    "已在 QA 前应用身份、来源与事件引用的确定性归一化",
                    payload={
                        "before_artifact_id": working_id,
                        "after_artifact_id": normalization_artifact["id"],
                        "changes": normalization_changes,
                    },
                )
            # Continue QA from the normalized artifact in this iteration. A
            # separate loop turn here is unbounded by the patch budget and can
            # persist duplicate artifacts forever if normalization oscillates
            # or a stale worker repeatedly reports the same material change.
            working_id = normalization_artifact["id"]
            artifact_hash = normalization_hash
            from app.production.screenplay_document import (
                ScreenplayDocument,
                document_to_screenplay,
            )
            script = document_to_screenplay(
                ScreenplayDocument.model_validate(normalization_payload),
            )

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

        hard_issues = non_waivable_screenplay_issues(issues)
        if not hard_issues:
            return _complete_screenplay_from_working_artifact(
                episode_id=episode_id,
                episode=episode,
                source_text=source_text,
                bible=bible,
                revision_id=rev.id,
                run_id=run_id,
                checkpoint=checkpoint,
                activation_no=activation_no,
            )

        current_fps = {i.fingerprint for i in issues}
        reopened = prev_issue_fps & current_fps
        # reopened means previously cleared then came back - track when we had improvement
        if checkpoint.get("cleared_fingerprints"):
            for fp in set(checkpoint["cleared_fingerprints"]) & current_fps:
                record_issue_reopened(kind="screenplay", episode_id=episode_id, fingerprint=fp)
                reopened.add(fp)

        if evaluation.status == "passed" and can_issue_certificate(issues):
            qa_input_fingerprint = str(
                (evaluation.evidence or {}).get("authority_input_fingerprint") or "",
            )
            if not qa_input_fingerprint or qa_input_fingerprint != input_fp:
                raise ValueError(
                    "剧本 QA 权威指纹与当前运行输入不一致，禁止签发完成凭证",
                )
            if rev.input_fingerprint != qa_input_fingerprint:
                rev = rebind_input_fingerprint(
                    rev.id,
                    input_fingerprint=qa_input_fingerprint,
                    expected_working_artifact_id=working_id,
                )
            if run_id:
                evidence_repository.append_event(
                    run_id, "CERTIFYING", "info", "剧本 QA 已通过，正在签发完成凭证",
                    payload={
                        "artifact_id": working_id,
                        "evaluation_id": eval_id,
                        "qa_score": evaluation.score,
                    },
                )
            # 首次发布清空下游；若已有 published 且同 hash 则仍走 publish
            result = publish_screenplay(
                episode_id=episode_id,
                revision_id=rev.id,
                artifact_id=working_id,
                artifact_hash=artifact_hash,
                evaluation_ids=[eval_id] if eval_id else [],
                input_fingerprint=qa_input_fingerprint,
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
                "last_issue_fingerprints": [],
                "yield_reason": None,
            })
            return load_screenplay_from_artifact(working_id)

        # 选择最高依赖 Issue
        # Dependency ordering determines which must-fix issue is repaired
        # first; the selector does not route by story words or error whitelist.
        issue = _choose_issue(hard_issues)
        if issue is None or not issue.repairable:
            return _publish_retry_exhausted_fallback(
                rev,
                working_id=working_id,
                artifact_hash=artifact_hash,
                evaluation_id=eval_id,
                open_issues=issues,
                reason="no_repairable_strategy",
                failed_issue=issue,
            )

        # Document-local repairs remain deterministic even when the screenplay
        # also carries a narrative graph. Graph relationship gaps have no such
        # operation and therefore fall through to semantic candidate planning.
        ops = await _plan_screenplay_repair_operations(
            issue,
            script,
            source_text=source_text,
            strategy_history=strategy_history,
        )
        if ops:
            proposed_key = _patch_strategy_key(ops)
            if _strategy_was_tried(
                strategy_history.get(issue.fingerprint, []), proposed_key
            ):
                ops = []
        if not ops:
            strategy_history.setdefault(issue.fingerprint, []).append("exhausted")
            _mark_repair_failed(
                episode_id,
                issue,
                run_id=run_id,
                activation_no=activation_no,
                patch_count=len(patch_ids),
            )
            return _publish_retry_exhausted_fallback(
                rev,
                working_id=working_id,
                artifact_hash=artifact_hash,
                evaluation_id=eval_id,
                open_issues=issues,
                reason="strategies_exhausted",
                failed_issue=issue,
            )

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
            character_resolutions=episode.get("character_resolutions") or [],
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
                    _mark_repair_failed(
                        episode_id,
                        issue,
                        run_id=run_id,
                        activation_no=activation_no,
                        patch_count=len(patch_ids),
                    )
                    return _publish_retry_exhausted_fallback(
                        rev,
                        working_id=working_id,
                        artifact_hash=artifact_hash,
                        evaluation_id=eval_id,
                        open_issues=issues,
                        reason="no_progress",
                        failed_issue=issue,
                    )
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

    # activation 预算用尽：对最终工作副本再评分一次并直接发布，不留失败/等待态。
    rev = get_production_revision(rev.id)  # type: ignore[assignment]
    assert rev and rev.working_artifact_id
    working_id = rev.working_artifact_id
    art = evidence_repository.get_artifact(working_id)
    assert art
    artifact_hash = art.get("content_hash") or evidence_repository.content_hash(art.get("content"))
    script = load_screenplay_from_artifact(working_id)
    issues, evaluation = run_screenplay_qa(
        script, bible=bible, source_text=source_text, episode=episode,
        artifact_id=working_id, artifact_hash=artifact_hash,
    )
    eval_row = evidence_repository.create_evaluation(working_id, evaluation)
    eval_id = _eval_id_from_create(eval_row)
    return _publish_retry_exhausted_fallback(
        rev,
        working_id=working_id,
        artifact_hash=artifact_hash,
        evaluation_id=eval_id,
        open_issues=issues,
        reason="activation_budget_exhausted",
    )


def get_active_safe(episode_id: str):
    from app.production.revision import get_active_production_revision
    return get_active_production_revision(episode_id, "screenplay")


def _choose_issue(issues: list[Issue]) -> Issue | None:
    if not issues:
        return None
    repairable = [i for i in issues if i.repairable]
    pool = repairable or issues

    severity_order = {"blocker": 0, "error": 1, "warning": 2, "info": 3}

    def issue_priority(item: tuple[int, Issue]) -> tuple[float, float, float, int]:
        index, issue = item
        evidence = issue.evidence or {}
        severity_value = getattr(issue.severity, "value", issue.severity)
        severity = severity_order.get(str(severity_value), 4)
        # Producers may expose graph depth/affected scope, but missing values
        # remain neutral.  These are relation properties, never issue-code or
        # story-word mappings.
        try:
            dependency_depth = float(evidence.get("dependency_depth", 0))
        except (TypeError, ValueError):
            dependency_depth = 0.0
        try:
            affected_scope = float(evidence.get("affected_scope_size", 1))
        except (TypeError, ValueError):
            affected_scope = 1.0
        # Validator order is the dependency-neutral final tiebreaker. Sorting by
        # fingerprint here used to turn a missing graph annotation into an
        # accidental alphabetical repair policy.
        return severity, dependency_depth, -affected_scope, index

    return min(enumerate(pool), key=issue_priority)[1]


def _mark_waiting_input(episode_id: str, issues: list[Issue], *, run_id: str | None) -> None:
    if run_id:
        evidence_repository.append_event(
            run_id, "WAITING_INPUT", "warning",
            "存在不可自动修复的真实冲突，需用户决定",
            payload={"issues": [i.model_dump(mode="json") for i in issues[:5]]},
        )


def _mark_repair_failed(
    episode_id: str,
    issue: Issue,
    *,
    run_id: str | None,
    activation_no: int | None = None,
    patch_count: int | None = None,
) -> None:
    """暂停内部修复但保留 working artifact；这不是用户输入冲突。"""
    rev = get_active_safe(episode_id)
    checkpoint = dict(rev.checkpoint_json or {}) if rev else {}
    current_activation = (
        int(activation_no)
        if activation_no is not None
        else int(checkpoint.get("activation_no") or 0)
    )
    applied_patches = (
        int(patch_count)
        if patch_count is not None
        else len(checkpoint.get("patch_artifact_ids") or [])
    )
    progress = f"已启动 {current_activation} 轮、实际应用 {applied_patches} 个补丁"
    message = (
        f"REPAIR_FAILED: 自动修复暂停（{progress}）；当前问题暂无可用策略 "
        f"{issue.code}: {issue.message}"
    )
    conn = get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
        "screenplay_updated_at=? WHERE id=?",
        (message[:800], now(), episode_id),
    )
    conn.commit()
    if run_id:
        evidence_repository.append_event(
            run_id,
            "REPAIR_FAILED",
            "error",
            f"自动修复暂停（{progress}），已保留工作副本",
            payload={
                "issue": issue.model_dump(mode="json"),
                "requires_user_input": False,
            },
        )


def _identity_contract_repair_policy() -> dict[str, Any]:
    """Return the typed, content-agnostic identity rules used by graph repair."""
    return {
        "authority": (
            "identity_contracts 是所有非角色圣经身份的唯一权威声明；"
            "修复不得引入未声明的实体、场次人物或非旁白说话人"
        ),
        "contract_fields": {
            "identity_id": "稳定图引用 ID",
            "display_name": "剧本与对白使用的精确显示名",
            "kind": "基于当前来源和戏剧职责的开放语义",
            "visual_policy": "canonical | contextual | collective | offscreen_only",
            "visual_canonical": "非 offscreen_only 必填的中性视觉锚点",
            "asset_requirement": "required | optional | forbidden",
            "voice_ids": "精确回指 voice_bible.speaker_id",
            "evidence": {
                "source_evidence_ids": [],
                "proposition_ids": [],
                "adaptation_decision_ids": [],
                "rationale": "身份决策的可追溯理由",
            },
        },
        "typed_invariants": [
            "canonical 必须 asset_requirement=required",
            "offscreen_only 必须 asset_requirement=forbidden",
            "除 offscreen_only 外 visual_canonical 必填",
            "纯旁白可由 voice_bible.role_type=narrator 表达；其他画外说话人仍需合同与 voice_ids",
        ],
        "semantic_decision": (
            "具名新角色、一次性功能身份、群体或纯画外身份均按当前语义意图决策；"
            "禁止使用姓名、称谓、身份类型或题材白名单"
        ),
    }


def _issue_acceptance_test(issue: Issue) -> str:
    return (
        "把候选隔离应用到当前完整文档后，必须让 issue.message 从同一组确定性"
        "校验结果中消失，且不得新增任何校验错误。目标节点和字段只能由文档内"
        "稳定 ID、直接字段所有权与现行 schema 推导；来源内容必须可追溯到"
        "authorized_source_excerpt，禁止按问题码、题材、角色名或文本关键词套用模板"
    )


def _introduced_issue_messages(
    baseline_issues: list[Issue],
    candidate_issues: list[Issue],
) -> list[str]:
    """Detect new validation slots while allowing one aggregate slot to shrink."""
    def slot(issue: Issue) -> tuple[str, str, str, str, str, str]:
        evidence = issue.evidence or {}
        path = str(evidence.get("path") or evidence.get("span") or "")
        collection_path = re.sub(r"\[\d+\]", "[]", path)
        return (
            issue.code,
            issue.subject,
            str(evidence.get("rule_id") or ""),
            collection_path,
            str(evidence.get("stage") or ""),
            issue.severity.value,
        )

    baseline_counts = Counter(slot(issue) for issue in baseline_issues)
    candidate_counts: Counter[tuple[str, str, str, str, str, str]] = Counter()
    introduced: list[str] = []
    for issue in candidate_issues:
        key = slot(issue)
        candidate_counts[key] += 1
        if candidate_counts[key] > baseline_counts[key]:
            introduced.append(issue.message)
    return introduced


def _dialogue_chain_replacement_is_local(
    document: Any,
    *,
    chain_id: str,
    turns: Any,
    source_text: str = "",
) -> bool:
    """Allow body selection, or source-grounded recovery of one empty chain."""
    from app.production.screenplay_document import action_block_spoken_identity
    from app.spoken_contract import content_char_count

    if (
        not isinstance(turns, list)
        or not 1 <= len(turns) <= DIALOGUE_CHAIN_TURNS_HARD_MAX
    ):
        return False
    body_turns = {
        ((turn.speaker or "").strip(), (turn.line or "").strip())
        for block in document.scene_blocks
        for turn in block.dialogue_turns
        if (turn.speaker or "").strip() and (turn.line or "").strip()
    }
    body_turns.update(
        spoken
        for block in document.scene_blocks
        for action in block.action_blocks
        if (spoken := action_block_spoken_identity(action.text)) is not None
    )
    current_chain = next(
        (
            chain for chain in document.dialogue_chains
            if (chain.chain_id or "").strip() == chain_id
        ),
        None,
    )
    if current_chain is None:
        return False
    if not current_chain.turns:
        declared_speakers = {
            str(voice.speaker_id or "").strip()
            for voice in document.voice_bible
            if str(voice.speaker_id or "").strip()
        }
        plan = getattr(document, "narrative_plan", None)
        for identity in getattr(plan, "identity_contracts", []) if plan else []:
            declared_speakers.update({
                str(identity.identity_id or "").strip(),
                str(identity.display_name or "").strip(),
                *(
                    str(voice_id or "").strip()
                    for voice_id in (identity.voice_ids or [])
                ),
            })
        allowed_functions = {
            "trigger",
            "announcement",
            "question",
            "response",
            "decision",
            "statement",
        }
        candidate_turns: list[tuple[str, str]] = []
        for turn in turns:
            if not isinstance(turn, dict):
                return False
            speaker = str(turn.get("speaker") or "").strip()
            line = str(turn.get("line") or "").strip()
            function = str(turn.get("function") or "").strip()
            evidence = str(turn.get("source_text") or "").strip()
            if (
                not speaker
                or speaker not in declared_speakers
                or not line
                or content_char_count(line) > config.MAX_SPOKEN_CHARS_PER_SHOT
                or function not in allowed_functions
                or len(textmatch.condense(evidence)) < 2
                or not source_text
                or evidence not in source_text
                or not _source_references_are_grounded(turn, source_text)
            ):
                return False
            candidate_turns.append((speaker, line))
        return len(candidate_turns) == len(set(candidate_turns))

    current_turns = {
        ((turn.speaker or "").strip(), (turn.line or "").strip())
        for turn in current_chain.turns
    }
    candidate_turns: list[tuple[str, str]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            return False
        identity = (
            str(turn.get("speaker") or "").strip(),
            str(turn.get("line") or "").strip(),
        )
        if (
            not all(identity)
            or identity not in body_turns
        ):
            return False
        candidate_turns.append(identity)
    return (
        len(candidate_turns) == len(set(candidate_turns))
        and current_turns.issubset(set(candidate_turns))
    )


def _source_references_are_grounded(value: Any, source_text: str) -> bool:
    """Validate every nested source-bearing field against the authorized source."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"source_text", "verbatim_excerpt"}:
                excerpt = str(child or "").strip()
                if excerpt and excerpt not in source_text:
                    return False
            if not _source_references_are_grounded(child, source_text):
                return False
    elif isinstance(value, list):
        return all(
            _source_references_are_grounded(child, source_text)
            for child in value
        )
    return True


def _normalize_character_decision_basis(value: Any) -> Any:
    """Constrain decision bases to evidence perceived or propositions held by the node."""
    if isinstance(value, list):
        return [_normalize_character_decision_basis(child) for child in value]
    if not isinstance(value, dict):
        return value
    normalized = {
        key: _normalize_character_decision_basis(child)
        for key, child in value.items()
    }
    basis = normalized.get("decision_basis_ids")
    if not isinstance(basis, list):
        return normalized
    perceived = {
        str(item or "").strip()
        for item in normalized.get("perceived_evidence_ids") or []
        if str(item or "").strip()
    }
    held = {
        str(item.get("proposition_id") or "").strip()
        for item in normalized.get("beliefs") or []
        if isinstance(item, dict) and str(item.get("proposition_id") or "").strip()
    }
    allowed = perceived | held
    normalized["decision_basis_ids"] = [
        str(item)
        for item in basis
        if str(item or "").strip() in allowed
    ]
    return normalized


def _unique_source_dialogue(line: str, source_text: str) -> str | None:
    """Return one uniquely matching source utterance under the validator contract."""
    for opening, closing in (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"')):
        quoted = f"{opening}{line}{closing}"
        if line and source_text.count(quoted) == 1:
            return line

    from app import textmatch
    from app.validators import (
        KEY_LINE_BIGRAM_COVERAGE,
        KEY_LINE_PRESENT_RATIO,
        source_dialogue_fragments,
    )

    ranked: list[tuple[float, str]] = []
    for candidate in source_dialogue_fragments(source_text):
        run_score = textmatch.longest_run_ratio(line, candidate)
        coverage = textmatch.bigram_coverage(line, candidate)
        if (
            run_score >= KEY_LINE_PRESENT_RATIO
            or coverage >= KEY_LINE_BIGRAM_COVERAGE
        ):
            ranked.append((max(run_score, coverage), candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    best_score = ranked[0][0]
    best = {
        candidate
        for score, candidate in ranked
        if abs(score - best_score) < 1e-9
        and source_text.count(candidate) == 1
    }
    return next(iter(best)) if len(best) == 1 else None


def _normalize_dialogue_source_references(
    value: Any,
    source_text: str,
) -> Any:
    """Resolve a non-exact dialogue citation only when one source utterance matches."""
    if isinstance(value, list):
        return [
            _normalize_dialogue_source_references(child, source_text)
            for child in value
        ]
    if not isinstance(value, dict):
        return value

    normalized = {
        key: _normalize_dialogue_source_references(child, source_text)
        for key, child in value.items()
    }
    citation = str(normalized.get("source_text") or "").strip()
    line = str(normalized.get("line") or "").strip()
    speaker = str(normalized.get("speaker") or "").strip()
    if not citation or not line or not speaker:
        return normalized

    from app import textmatch

    citation_supports_line = (
        textmatch.spoken_digit_sequence_equivalent(citation, line)
        or textmatch.longest_run_ratio(line, citation)
        >= textmatch.KEY_LINE_PRESENT_RATIO
        or textmatch.bigram_coverage(line, citation)
        >= textmatch.KEY_LINE_BIGRAM_COVERAGE
    )
    if citation in source_text and citation_supports_line:
        return normalized

    source_dialogue = _unique_source_dialogue(line, source_text)
    if source_dialogue is not None:
        normalized["source_text"] = source_dialogue
    return normalized


def _normalize_dialogue_chain_continuity(
    script: EpisodeScreenplay,
    source_text: str,
) -> list[dict[str, Any]]:
    """Fill omitted intervening source-grounded turns before dependent responses."""
    from app.production.screenplay_document import (
        action_block_spoken_identity,
        screenplay_to_document,
    )
    from app.schemas import KeyDialogueTurn

    if not source_text or not script.dialogue_chains:
        return []
    changes: list[dict[str, Any]] = []
    from app.validators import source_dialogue_fragments

    source_dialogues = source_dialogue_fragments(source_text)
    allowed_speakers = {
        str(voice.speaker_id or "").strip()
        for voice in (script.voice_bible or [])
        if str(voice.speaker_id or "").strip()
    }
    if script.narrative_plan is not None:
        for contract in script.narrative_plan.identity_contracts:
            allowed_speakers.update({
                str(contract.identity_id or "").strip(),
                str(contract.display_name or "").strip(),
                *(
                    str(voice_id or "").strip()
                    for voice_id in (contract.voice_ids or [])
                ),
            })
    allowed_speakers.discard("")
    first_turn = (
        script.dialogue_chains[0].turns[0]
        if script.dialogue_chains[0].turns else None
    )
    if first_turn is not None and source_dialogues:
        opening = source_dialogues[0]
        matched = _unique_source_dialogue(first_turn.line or "", source_text)
        if (
            matched == opening
            and (first_turn.source_text or "").strip() != opening
        ):
            changes.append({
                "kind": "opening_dialogue_source",
                "id": f"{script.dialogue_chains[0].chain_id}-T1",
                "from": (first_turn.source_text or "").strip(),
                "to": opening,
            })
            first_turn.source_text = opening
    document = screenplay_to_document(script)
    observed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for block in document.scene_blocks:
        spoken = [
            (
                (turn.speaker or "").strip(),
                (turn.line or "").strip(),
            )
            for turn in block.dialogue_turns
        ]
        spoken.extend(
            identity
            for action in block.action_blocks
            if (identity := action_block_spoken_identity(action.text)) is not None
            and identity[0] in allowed_speakers
        )
        for speaker, line in spoken:
            identity = (block.scene_id, speaker, line)
            if not speaker or not line or identity in seen:
                continue
            source_dialogue = _unique_source_dialogue(line, source_text)
            if source_dialogue is None:
                continue
            seen.add(identity)
            observed.append({
                "scene_id": block.scene_id,
                "speaker": speaker,
                "line": line,
                "source_text": source_dialogue,
                "source_position": source_text.find(source_dialogue),
            })

    for chain in script.dialogue_chains:
        turns = list(chain.turns or [])
        if len(turns) >= DIALOGUE_CHAIN_TURNS_HARD_MAX:
            continue
        existing = {
            ((turn.speaker or "").strip(), (turn.line or "").strip())
            for turn in turns
        }
        additions: list[dict[str, Any]] = []
        for turn_index, turn in enumerate(turns):
            if (turn.function or "").strip() != "response" or turn_index == 0:
                continue
            response_identity = (
                (turn.speaker or "").strip(),
                (turn.line or "").strip(),
            )
            response_matches = [
                item for item in observed
                if (item["speaker"], item["line"]) == response_identity
            ]
            if len(response_matches) != 1:
                continue
            response = response_matches[0]
            previous = turns[turn_index - 1]
            previous_source = (
                (previous.source_text or "").strip()
                if (previous.source_text or "").strip() in source_text
                else _unique_source_dialogue(previous.line or "", source_text)
            )
            if previous_source is None:
                continue
            previous_position = source_text.find(previous_source)
            eligible = [
                item for item in observed
                if (
                    item["scene_id"] == response["scene_id"]
                    and previous_position < item["source_position"] < response["source_position"]
                    and (item["speaker"], item["line"]) not in existing
                    and (item["speaker"], item["line"]) not in {
                        (added["speaker"], added["line"]) for added in additions
                    }
                )
            ]
            remaining = (
                DIALOGUE_CHAIN_TURNS_HARD_MAX
                - len(turns)
                - len(additions)
            )
            if remaining <= 0 or not eligible:
                continue
            selected = sorted(
                eligible,
                key=lambda item: item["source_position"],
            )[-remaining:]
            if not any(item["speaker"] != response["speaker"] for item in selected):
                continue
            additions.extend(selected)
        if not additions:
            continue

        combined: list[tuple[int, int, KeyDialogueTurn]] = []
        for index, turn in enumerate(turns):
            source_dialogue = (
                (turn.source_text or "").strip()
                if (turn.source_text or "").strip() in source_text
                else _unique_source_dialogue(turn.line or "", source_text)
            )
            position = (
                source_text.find(source_dialogue)
                if source_dialogue is not None
                else len(source_text) + index
            )
            combined.append((position, index, turn))
        for offset, item in enumerate(additions, start=len(turns)):
            combined.append((
                int(item["source_position"]),
                offset,
                KeyDialogueTurn(
                    speaker=item["speaker"],
                    line=item["line"],
                    function=(
                        "question"
                        if item["line"].rstrip().endswith(("?", "？"))
                        else "statement"
                    ),
                    source_text=item["source_text"],
                ),
            ))
        combined.sort(key=lambda item: (item[0], item[1]))
        chain.turns = [item[2] for item in combined]
        changes.append({
            "kind": "dialogue_chain_continuity",
            "id": chain.chain_id,
            "added_turns": [
                {
                    "speaker": item["speaker"],
                    "line": item["line"],
                    "source_text": item["source_text"],
                }
                for item in additions
            ],
        })
    return changes


def _find_narrative_node(value: Any, node_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if any(
            key.endswith("_id") and str(candidate or "") == node_id
            for key, candidate in value.items()
        ):
            return value
        for child in value.values():
            found = _find_narrative_node(child, node_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_narrative_node(child, node_id)
            if found is not None:
                return found
    return None


def _narrative_collection_for_node(
    plan_data: dict[str, Any],
    node_id: str,
) -> str | None:
    matches = [
        collection
        for collection, nodes in plan_data.items()
        if (
            isinstance(nodes, list)
            and _find_narrative_node(nodes, node_id) is not None
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _narrative_collection_for_new_node(
    plan_data: dict[str, Any],
    node_id: str,
    value: dict[str, Any],
) -> str | None:
    identity_fields = {
        key
        for key, candidate in value.items()
        if (
            key.endswith("_id")
            and str(candidate or "").strip() == node_id
        )
    }
    if not identity_fields:
        return None
    matches: list[str] = []
    for collection, nodes in plan_data.items():
        if not isinstance(nodes, list):
            continue
        if any(
            isinstance(node, dict)
            and bool(identity_fields & set(node))
            for node in nodes
        ):
            matches.append(collection)
    return matches[0] if len(matches) == 1 else None


def _candidate_targets_narrative_graph(
    candidate: dict[str, Any],
    plan_data: dict[str, Any],
) -> bool:
    operations = candidate.get("operations")
    if not isinstance(operations, list):
        return False
    for raw in operations:
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_patch_operation_payload(raw)
        target = normalized.get("target") or {}
        node_id = str(target.get("id") or "").strip()
        collection = re.split(
            r"[.\[]+",
            str(target.get("collection") or "").strip(),
            maxsplit=1,
        )[0]
        if collection and isinstance(plan_data.get(collection), list):
            return True
        if node_id and _narrative_collection_for_node(plan_data, node_id):
            return True
        value = normalized.get("value")
        if (
            normalized.get("op") in {"create_node", "insert_node"}
            and node_id
            and isinstance(value, dict)
            and _narrative_collection_for_new_node(
                plan_data,
                node_id,
                value,
            )
        ):
            return True
    return False


def _normalize_top_level_narrative_parent(
    target: dict[str, Any],
    *,
    collection: str,
    plan_data: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(target)
    parent_id = str(normalized.get("parent_id") or "").strip()
    parent_field = str(normalized.get("parent_field") or "").strip()
    if (
        collection
        and parent_field == collection
        and parent_id in {
            "narrative_plan",
            str(plan_data.get("scope_id") or ""),
        }
    ):
        normalized.pop("parent_id", None)
        normalized.pop("parent_field", None)
    return normalized


def _expand_single_action_event_closure(
    operations: list[PatchOperation],
    plan_data: dict[str, Any],
) -> list[PatchOperation]:
    """Keep a one-action event's fact transition fields structurally aligned."""
    expanded = [operation.model_copy(deep=True) for operation in operations]
    existing = {
        (
            str((operation.target or {}).get("id") or ""),
            re.split(r"[./]+", operation.path.strip("/"))[-1],
        )
        for operation in expanded
    }
    events = plan_data.get("events")
    actions = plan_data.get("atomic_actions")
    if not isinstance(events, list) or not isinstance(actions, list):
        return expanded

    for operation in list(expanded):
        if operation.op != "replace_field":
            continue
        target = operation.target or {}
        node_id = str(target.get("id") or "").strip()
        collection = str(target.get("collection") or "").strip()
        if not collection and node_id:
            collection = _narrative_collection_for_node(plan_data, node_id) or ""
        field = re.split(r"[./]+", operation.path.strip("/"))[-1]
        if collection != "events" or field not in {
            "precondition_fact_ids", "effects_add", "effects_remove",
        }:
            continue
        event = _find_narrative_node(events, node_id)
        if event is None:
            continue
        action_ids = [
            str(action_id)
            for action_id in (event.get("action_ids") or [])
            if str(action_id).strip()
        ]
        if len(action_ids) != 1:
            continue
        action = _find_narrative_node(actions, action_ids[0])
        old_value = event.get(field)
        if (
            action is None
            or not isinstance(old_value, list)
            or not isinstance(operation.value, list)
            or action.get(field) != old_value
            or (action_ids[0], field) in existing
        ):
            continue
        expanded.append(PatchOperation(
            op="replace_field",
            path=field,
            value=list(operation.value),
            target={
                "kind": "narrative_node",
                "collection": "atomic_actions",
                "id": action_ids[0],
                "derived_from_event_id": node_id,
            },
        ))
        existing.add((action_ids[0], field))
    return expanded


def _resolve_narrative_patch_owner(
    nodes: list[Any],
    *,
    node_id: str,
    patch_field: str,
    issue: Issue,
) -> tuple[dict[str, Any], str] | None:
    """Resolve a wrongly targeted ancestor only when schema evidence is unique."""
    ancestor = _find_narrative_node(nodes, node_id)
    if ancestor is None:
        return None
    if patch_field in ancestor:
        return ancestor, node_id

    owners: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if patch_field in value:
                owners.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(ancestor)
    evidence = issue.evidence or {}
    issue_locator = " ".join([
        issue.message or "",
        str(evidence.get("path") or ""),
        *[str(value) for value in evidence.get("related_node_ids") or []],
    ])
    mentioned: list[tuple[dict[str, Any], str]] = []
    for owner in owners:
        for key, candidate in owner.items():
            candidate_id = str(candidate or "").strip()
            if (
                key.endswith("_id")
                and candidate_id
                and candidate_id in issue_locator
            ):
                mentioned.append((owner, candidate_id))

    unique_mentions = {
        (id(owner), candidate_id): (owner, candidate_id)
        for owner, candidate_id in mentioned
    }
    if len(unique_mentions) == 1:
        return next(iter(unique_mentions.values()))
    if len(owners) == 1:
        identity_values = [
            str(candidate or "").strip()
            for key, candidate in owners[0].items()
            if key.endswith("_id") and str(candidate or "").strip()
        ]
        if len(identity_values) == 1:
            return owners[0], identity_values[0]
    return None


def _normalize_patch_operation_payload(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    target = dict(item.get("target") or {})
    has_field_path = bool(str(item.get("path") or "").strip())
    structural_op = str(item.get("op") or "") in {
        "create_node", "insert_node", "delete_node", "move_node",
    }
    if has_field_path and not structural_op:
        normalized["op"] = "replace_field"
    elif structural_op:
        normalized["path"] = ""
    for key in ("parent_id", "parent_field", "to_index"):
        if key in item and key not in target:
            target[key] = item[key]
    if (
        not has_field_path
        and target.get("parent_id")
        and not target.get("parent_field")
    ):
        target.pop("parent_id", None)
    normalized["target"] = target
    return normalized


def _candidate_is_executable(
    candidate: dict[str, Any],
    document: Any,
) -> bool:
    """Probe a candidate with the production executor on an isolated document."""
    from app.production.patch import (
        PatchOperation,
        apply_patch_operation_to_document,
    )

    operations = candidate.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 3:
        return False
    working = document
    try:
        for raw in operations:
            if not isinstance(raw, dict):
                return False
            operation = PatchOperation.model_validate(
                _normalize_patch_operation_payload(raw),
            )
            target = dict(operation.target or {})
            plan = getattr(working, "narrative_plan", None)
            plan_data = (
                plan.model_dump(mode="json")
                if plan is not None
                else {}
            )
            collection = re.split(
                r"[.\[]+",
                str(target.get("collection") or "").strip(),
                maxsplit=1,
            )[0]
            node_id = str(target.get("id") or "").strip()
            if not collection and node_id:
                collection = (
                    _narrative_collection_for_node(plan_data, node_id)
                    or ""
                )
            if (
                not collection
                and operation.op in {"create_node", "insert_node"}
                and node_id
                and isinstance(operation.value, dict)
            ):
                collection = (
                    _narrative_collection_for_new_node(
                        plan_data,
                        node_id,
                        operation.value,
                    )
                    or ""
                )
            if isinstance(plan_data.get(collection), list):
                target = _normalize_top_level_narrative_parent(
                    target,
                    collection=collection,
                    plan_data=plan_data,
                )
                target = {
                    **target,
                    "kind": "narrative_node",
                    "collection": collection,
                }
            operation.target = target
            working, _ = apply_patch_operation_to_document(working, operation)
    except Exception:  # noqa: BLE001 - probing untrusted model output
        return False
    return True


def _resolve_dialogue_chain_turn_target(
    document,
    *,
    target: dict[str, Any],
    patch_field: str,
) -> dict[str, Any] | None:
    turn_id = str(target.get("turn_id") or target.get("id") or "").strip()
    chain_id = str(target.get("chain_id") or "").strip()
    turn_index = target.get("turn_index")
    match = re.fullmatch(r"(.+)-T(\d+)", turn_id, re.I)
    if not chain_id and match:
        chain_id = match.group(1)
    if turn_index is None and match:
        turn_index = int(match.group(2)) - 1
    if not chain_id or turn_index is None:
        return None
    try:
        turn_index = int(turn_index)
    except (TypeError, ValueError):
        return None
    chain = next(
        (
            item for item in document.dialogue_chains
            if (item.chain_id or "").strip() == chain_id
        ),
        None,
    )
    if (
        chain is None
        or not 0 <= turn_index < len(chain.turns or [])
        or patch_field not in type(chain.turns[turn_index]).model_fields
    ):
        return None
    return {
        **target,
        "id": turn_id or f"{chain_id}-T{turn_index + 1}",
        "turn_id": turn_id or f"{chain_id}-T{turn_index + 1}",
        "chain_id": chain_id,
        "turn_index": turn_index,
    }


def _preflight_document_candidate(
    candidate: dict[str, Any],
    *,
    document: Any,
    source_text: str,
    issue: Issue,
) -> list[PatchOperation]:
    """Find the smallest executable operation subset that passes full local QA."""
    from itertools import combinations

    from app.narrative import validate_screenplay_narrative
    from app.production.patch import apply_patch_operation_to_document
    from app.production.screenplay_document import (
        document_to_screenplay,
        resolve_field_patch_target,
    )
    from app.validators import validate_screenplay

    raw_operations = candidate.get("operations")
    if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= 3:
        return []
    try:
        parsed = [
            PatchOperation.model_validate(
                _normalize_patch_operation_payload(raw),
            )
            for raw in raw_operations
            if isinstance(raw, dict)
        ]
    except Exception:  # noqa: BLE001 - untrusted model output
        return []
    if len(parsed) != len(raw_operations):
        return []

    def errors_for(candidate_document: Any) -> list[str]:
        screenplay = document_to_screenplay(candidate_document)
        errors = validate_screenplay_narrative(screenplay, require=True)
        errors.extend(validate_screenplay(
            screenplay,
            Bible(
                characters=[],
                world={"visual_style_canonical": ""},
            ),
            expected_beats=max(1, len(screenplay.scene_outline or [])),
            episode_no=screenplay.episode_no,
            source_text=source_text,
            require_dialogue_chains=True,
            validate_narrative=False,
        ))
        return errors

    baseline_errors = errors_for(document)
    baseline_issues = issues_from_validator_messages(
        baseline_errors,
        subject="screenplay",
        stage="screenplay",
    )
    for subset_size in range(1, len(parsed) + 1):
        for subset_indices in combinations(range(len(parsed)), subset_size):
            working = document
            accepted: list[PatchOperation] = []
            valid = True
            for index in subset_indices:
                operation = parsed[index].model_copy(deep=True)
                operation.value = _normalize_dialogue_source_references(
                    operation.value,
                    source_text,
                )
                target = operation.target or {}
                collection = re.split(
                    r"[.\[]+",
                    str(target.get("collection") or "").strip(),
                    maxsplit=1,
                )[0]
                script_plan = getattr(document, "narrative_plan", None)
                plan_data = (
                    script_plan.model_dump(mode="json")
                    if script_plan is not None
                    else {}
                )
                if collection and isinstance(plan_data.get(collection), list):
                    valid = False
                    break
                if operation.op == "replace_field":
                    target = resolve_field_patch_target(
                        working,
                        path=operation.path,
                        target=target,
                    )
                    operation.target = target
                    patch_field = re.split(
                        r"[./]+",
                        operation.path.strip("/"),
                    )[-1]
                    chain_id = str(
                        target.get("chain_id") or target.get("id") or "",
                    ).strip()
                    chain = next(
                        (
                            item for item in working.dialogue_chains
                            if (item.chain_id or "").strip() == chain_id
                        ),
                        None,
                    )
                    if (
                        chain is not None
                        and patch_field == "turns"
                        and not _dialogue_chain_replacement_is_local(
                            working,
                            chain_id=chain_id,
                            turns=operation.value,
                            source_text=source_text,
                        )
                    ):
                        valid = False
                        break
                if not _source_references_are_grounded(
                    operation.value,
                    source_text,
                ):
                    valid = False
                    break
                before = working.model_dump(mode="json")
                try:
                    working, _ = apply_patch_operation_to_document(
                        working,
                        operation,
                    )
                except Exception:  # noqa: BLE001 - isolated probe
                    valid = False
                    break
                if working.model_dump(mode="json") == before:
                    valid = False
                    break
                accepted.append(operation)
            if not valid:
                continue
            candidate_errors = errors_for(working)
            if issue.message in candidate_errors:
                continue
            candidate_issues = issues_from_validator_messages(
                candidate_errors,
                subject="screenplay",
                stage="screenplay",
            )
            if _introduced_issue_messages(baseline_issues, candidate_issues):
                continue
            selection = {
                "candidate_ids": [candidate.get("candidate_id")],
                "selected_candidate_id": candidate.get("candidate_id"),
                "selection_reason": (
                    "生产执行器对候选操作子集逐一隔离复验后选择的最小充分子集"
                ),
                "expected_narrative_gain": candidate.get("expected_narrative_gain"),
                "destructive_cost": candidate.get("destructive_cost"),
            }
            for operation in accepted:
                operation.target = {
                    **(operation.target or {}),
                    "semantic_selection": selection,
                }
            return accepted
    return []


async def _llm_field_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
    strategy_history: list[str] | None = None,
) -> list[PatchOperation]:
    """Retry rejected or duplicate semantic candidates with explicit feedback."""
    feedback: list[str] = []
    tried = list(strategy_history or [])
    for planner_attempt in range(1, MAX_STRATEGY_ATTEMPTS_PER_ISSUE + 1):
        operations = await _llm_field_patch_once(
            issue,
            script,
            source_text=source_text,
            planner_attempt=planner_attempt,
            rejection_feedback=feedback,
        )
        if not operations:
            feedback.append(
                "上一候选未通过本地结构、Schema 或确定性不变量校验。"
                "replace_field.target.id 必须指向直接拥有 path 字段的节点，"
                "不得指向其祖先；请提交不同候选。"
            )
            continue
        strategy_key = _patch_strategy_key(operations)
        if _strategy_was_tried(tried, strategy_key):
            feedback.append(
                f"策略 {strategy_key} 已尝试过；必须提供不同且仍满足验收测试的局部候选。"
            )
            continue
        return operations
    return []


def _narrative_patch_prompt_context(
    document,
    issue: Issue,
    source_text: str,
) -> tuple[dict[str, Any], str]:
    """Project one issue-local graph slice instead of the full document."""
    payload = document.model_dump(mode="json")
    plan = payload.get("narrative_plan") or {}
    issue_payload = issue.model_dump(mode="json")
    issue_blob = json.dumps(issue_payload, ensure_ascii=False)

    def strings(value: Any) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, list):
            return set().union(*(strings(item) for item in value), set())
        if isinstance(value, dict):
            return set().union(*(strings(item) for item in value.values()), set())
        return set()

    graph_nodes: list[tuple[str, str, dict, set[str]]] = []
    all_ids: set[str] = set()
    for collection, values in plan.items():
        if not isinstance(values, list):
            continue
        singular = collection[:-1] if collection.endswith("s") else collection
        for item in values:
            if not isinstance(item, dict):
                continue
            id_fields = [
                (key, str(value))
                for key, value in item.items()
                if key.endswith("_id") and isinstance(value, str) and value
            ]
            if not id_fields:
                continue
            primary = next(
                (
                    value
                    for key, value in id_fields
                    if key == f"{singular}_id"
                ),
                id_fields[0][1],
            )
            all_ids.add(primary)
            graph_nodes.append((collection, primary, item, set()))

    enriched_nodes: list[tuple[str, str, dict, set[str]]] = []
    for collection, primary, item, _refs in graph_nodes:
        refs = strings(item) & all_ids
        refs.discard(primary)
        enriched_nodes.append((collection, primary, item, refs))
    graph_nodes = enriched_nodes

    selected_ids = {
        identity
        for identity in all_ids
        if identity in issue_blob
    }
    ordered_selected: list[str] = [
        primary
        for _collection, primary, _item, _refs in graph_nodes
        if primary in selected_ids
    ]
    frontier = set(selected_ids)
    for _depth in range(2):
        if not frontier or len(ordered_selected) >= 24:
            break
        discovered: list[str] = []
        for _collection, primary, _item, refs in graph_nodes:
            if primary in selected_ids:
                continue
            if refs & frontier or (
                primary in set().union(*(
                    refs
                    for _c, selected, _i, refs in graph_nodes
                    if selected in frontier
                ), set())
            ):
                discovered.append(primary)
        for primary in discovered:
            if primary not in selected_ids:
                selected_ids.add(primary)
                ordered_selected.append(primary)
                if len(ordered_selected) >= 24:
                    break
        frontier = set(discovered)

    if not ordered_selected:
        related_values = strings(issue_payload)
        for _collection, primary, item, _refs in graph_nodes:
            if strings(item) & related_values:
                selected_ids.add(primary)
                ordered_selected.append(primary)
                if len(ordered_selected) >= 8:
                    break

    selected_order = {
        primary: index for index, primary in enumerate(ordered_selected)
    }
    scoped_plan: dict[str, Any] = {
        key: value
        for key, value in plan.items()
        if not isinstance(value, list)
    }
    graph_index: dict[str, list[str]] = {}
    for collection, primary, item, _refs in graph_nodes:
        graph_index.setdefault(collection, []).append(primary)
        if primary not in selected_ids:
            continue
        scoped_plan.setdefault(collection, []).append(item)
    for values in scoped_plan.values():
        if isinstance(values, list):
            values.sort(
                key=lambda item: selected_order.get(
                    next(
                        (
                            str(value)
                            for key, value in item.items()
                            if key.endswith("_id")
                            and isinstance(value, str)
                        ),
                        "",
                    ),
                    len(selected_order),
                ),
            )

    source_excerpts: list[str] = []
    for item in scoped_plan.get("source_evidence") or []:
        excerpt = str(item.get("verbatim_excerpt") or "").strip()
        if excerpt and excerpt in source_text:
            source_excerpts.append(excerpt)
    if not source_excerpts and len(source_text) <= 20_000:
        source_excerpt = source_text
    else:
        source_excerpt = "\n\n".join(dict.fromkeys(source_excerpts))
    source_excerpt = source_excerpt[:20_000]

    context = {
        "screenplay_metadata": payload.get("screenplay_metadata"),
        "scene_blocks": payload.get("scene_blocks"),
        "dialogue_chains": payload.get("dialogue_chains"),
        "voice_bible": payload.get("voice_bible"),
        "narrative_plan": scoped_plan,
        "narrative_graph_id_index": graph_index,
        "scope_note": (
            "narrative_plan 仅含当前问题的双向两跳依赖闭包；"
            "narrative_graph_id_index 是全图稳定 ID 索引。"
        ),
    }
    return context, source_excerpt


async def _llm_field_patch_once(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
    planner_attempt: int = 1,
    rejection_feedback: list[str] | None = None,
) -> list[PatchOperation]:
    """Compare semantic candidates, then return one bounded candidate patch.

    New narrative artifacts never map an issue code to an operation.  The AI
    compares at least two relation-level candidates and the selected candidate
    is CAS-applied to an isolated working artifact before full-graph QA.
    """
    path = str((issue.evidence or {}).get("path") or "")
    field = path.strip("/").split("/")[-1] if path else ""
    dramatic_fields = {"stakes", "obstacle", "protagonist_goal", "dramatic_question"}
    if script.narrative_plan is None:
        if field not in dramatic_fields:
            for candidate_field in dramatic_fields:
                if candidate_field in (issue.message or ""):
                    field = candidate_field
                    break
        if field not in dramatic_fields:
            return []
        value = _heuristic_fill_dramatic_field(field, script)
        return ([PatchOperation(
            op="replace_field",
            path=field,
            value=value,
            target={"kind": "metadata", "id": field},
        )] if value else [])

    from app.harness import model_gateway
    from app.production.screenplay_document import screenplay_to_document
    from app.schemas import extract_json

    document = screenplay_to_document(script)
    prompt_context, source_excerpt = _narrative_patch_prompt_context(
        document,
        issue,
        source_text,
    )
    prompt = {
        "task": "诊断当前剧本叙事关系缺口，比较至少两个最小候选，再选择一个局部候选",
        "planner_attempt": planner_attempt,
        "prior_rejections": list(rejection_feedback or []),
        "issue": issue.model_dump(mode="json"),
        "acceptance_test": _issue_acceptance_test(issue),
        "screenplay_document": prompt_context,
        "authorized_source_excerpt": source_excerpt,
        "identity_contract_policy": _identity_contract_repair_policy(),
        "operation_contract": {
            "op": "使用当前 PatchOperation 协议；每个候选会由生产执行器在副本上探测可执行性",
            "path": "单个现存字段；结构操作留空",
            "target": {
                "kind": "目标节点在当前文档 schema 中的类型；不得按固定类型清单猜测",
                "collection": "narrative_plan 的 schema 列表字段（包括 identity_contracts）；非叙事节点可省略",
                "id": "replace 时必须是直接拥有 path 字段的节点 ID；create_node 时必须是新节点自身 ID",
                "parent_id": "创建嵌套节点时的现存父节点 ID，可省略",
                "parent_field": "父节点中的列表字段，可省略",
                "to_index": "移动/插入位置，可省略",
            },
            "value": "replace 的新字段值或 create 的完整单节点",
            "dialogue_chain_turns": {
                "count": f"1~{DIALOGUE_CHAIN_TURNS_HARD_MAX} 个连续话轮",
                "speaker": "只能使用 voice_bible 或 identity_contracts 已声明的说话人",
                "line": (
                    f"非空且每轮纯文字不得超过 "
                    f"{config.MAX_SPOKEN_CHARS_PER_SHOT} 字"
                ),
                "function": (
                    "只能是 trigger|announcement|question|response|"
                    "decision|statement"
                ),
                "source_text": (
                    "每轮必填，且必须逐字连续存在于 authorized_source_excerpt"
                ),
            },
        },
        "output_contract": {
            "semantic_gap": "自由语义诊断；无法归类时仍需保留",
            "unclassified_dimensions": [],
            "candidate_plans": [{
                "candidate_id": "CANDIDATE-ID",
                "operations": [],
                "satisfies_gap_test": False,
                "passes_deletion_test": False,
                "passes_marginal_gain_test": False,
                "preserves_invariants": False,
                "expected_narrative_gain": 0.0,
                "destructive_cost": 0.0,
                "rationale": "关系、证据和状态理由",
            }],
            "selected_candidate_id": "CANDIDATE-ID",
            "selection_reason": "为什么是最小充分修改",
        },
        "rules": [
            "candidate_plans 至少两个；问题码只描述失败关系，不得决定操作",
            "选中候选必须逐字满足 acceptance_test；修复相邻语义但未消除当前 issue 的候选无效",
            "选中候选只能含 1~3 个局部操作，不得替换根对象或整个集合",
            "replace_field.path 只写目标节点的直接字段名，target.id 必须是该字段所属节点自身 ID，禁止用祖先节点 ID",
            "create_node 的 target.id 必须等于 value 内新节点的稳定 *_id；嵌套创建时 parent_id 指向直接父节点",
            "允许创建/删除/移动单个叙事节点，但必须证明全图引用、DAG、状态、信念和观众路径可恢复",
            "新增必须通过缺口与边际增益测试；删除必须通过删除测试；所有候选必须保持不变量",
            "不得修改现存节点的身份 ID",
            "create/replace 一旦引入新 identity_id、display_name 或非旁白 voice ID，同一候选必须以局部操作创建或补齐完整 identity_contracts 节点及 voice_ids 连接；否则候选无效",
            "修复可以更正身份合同本身，但不得借修复器绕过已有角色圣经或已发布身份合同的 ID 权威",
            "来源证据必须逐字来自 authorized_source_excerpt",
            (
                "替换 dialogue_chain.turns 时，每个 line 的纯文字不得超过 "
                f"{config.MAX_SPOKEN_CHARS_PER_SHOT} 字，"
                "function 只能是 trigger|announcement|question|response|decision|statement；"
                "禁止输出 narration、voiceover、explanation、apology、closing 等其他值"
            ),
            "改写命题不得直接挂原文证据，角色/观众信念不得补入不可感知证据",
            "修复后仍会运行整图 DAG、状态、信念与观众路径全量复验",
        ],
    }
    prompt_json = json.dumps(prompt, ensure_ascii=False)
    raw = await model_gateway.chat(
        [
            {"role": "system", "content": "你是叙事图局部修复器。只输出 JSON，不按题材或剧情关键词判断。"},
            {"role": "user", "content": prompt_json},
        ],
        temperature=0.1,
        max_tokens=4096,
        call_meta={
            "stage": "screenplay_narrative_patch",
            "stage_key": "narrative_graph_patch",
            "call_role": "semantic_patch_planner",
            "contract_version": "narrative-continuity.v1",
            "reuse_successful_operation": True,
            "planner_attempt": planner_attempt,
            "prompt_context_chars": len(prompt_json),
        },
    )
    try:
        plan_data = script.narrative_plan.model_dump(mode="json")
        payload = extract_json(raw, repair_unescaped_inner_quotes=True)
        candidates = list(payload.get("candidate_plans") or [])
        if len(candidates) < 2:
            return []
        selected_id = str(payload.get("selected_candidate_id") or "")
        model_selected = next((
            item for item in candidates
            if isinstance(item, dict) and str(item.get("candidate_id") or "") == selected_id
        ), None)
        ordered_candidates = [
            *([model_selected] if isinstance(model_selected, dict) else []),
            *[
                item for item in candidates
                if isinstance(item, dict) and item is not model_selected
            ],
        ]
        for candidate in ordered_candidates:
            if not (
                bool(candidate.get("satisfies_gap_test"))
                and bool(candidate.get("preserves_invariants"))
            ):
                continue
            if not _candidate_targets_narrative_graph(candidate, plan_data):
                preflight = _preflight_document_candidate(
                    candidate,
                    document=document,
                    source_text=source_text,
                    issue=issue,
                )
                if preflight:
                    return preflight
        selected = next((
            item for item in ordered_candidates
            if (
                bool(item.get("satisfies_gap_test"))
                and bool(item.get("preserves_invariants"))
                and _candidate_is_executable(item, document)
            )
        ), None)
        if selected is None:
            return []
        selected_id = str(selected.get("candidate_id") or "")
        used_model_selection = selected is model_selected
        raw_ops = list(selected.get("operations") or [])
        if not 1 <= len(raw_ops) <= 3:
            return []
        normalized_ops: list[dict[str, Any]] = []
        for item in raw_ops:
            if not isinstance(item, dict):
                return []
            normalized_ops.append(_normalize_patch_operation_payload(item))
        operations = [
            PatchOperation.model_validate(item) for item in normalized_ops
        ]
    except Exception:  # noqa: BLE001 - model output is untrusted
        return []
    if any(operation.op in {"create_node", "insert_node"} for operation in operations) and not bool(
        selected.get("satisfies_gap_test")
    ):
        return []
    if any(operation.op == "delete_node" for operation in operations) and not bool(
        selected.get("passes_deletion_test")
    ):
        return []

    operations = _expand_single_action_event_closure(
        operations,
        plan_data,
    )
    if len(operations) > 3:
        return []
    safe: list[PatchOperation] = []
    for operation in operations:
        target = operation.target or {}
        operation.value = _normalize_character_decision_basis(operation.value)
        operation.value = _normalize_dialogue_source_references(
            operation.value,
            source_text,
        )
        raw_collection = str(target.get("collection") or "").strip()
        collection = re.split(r"[.\[]+", raw_collection, maxsplit=1)[0]
        node_id = str(target.get("id") or "")
        if not collection and node_id:
            collection = (
                _narrative_collection_for_node(plan_data, node_id) or ""
            )
        if (
            not collection
            and operation.op in {"create_node", "insert_node"}
            and node_id
            and isinstance(operation.value, dict)
        ):
            collection = (
                _narrative_collection_for_new_node(
                    plan_data,
                    node_id,
                    operation.value,
                )
                or ""
            )
        if operation.op in {"create_node", "insert_node"} and collection:
            target = _normalize_top_level_narrative_parent(
                target,
                collection=collection,
                plan_data=plan_data,
            )
        nodes = plan_data.get(collection)
        if isinstance(nodes, list) and node_id:
            target = {
                **target,
                "kind": "narrative_node",
                "collection": collection,
                "normalized_from_kind": str(target.get("kind") or ""),
            }

            node = _find_narrative_node(nodes, node_id)
            if operation.op == "replace_field":
                patch_field = re.split(
                    r"[./]+", operation.path.strip("/"),
                )[-1]
                resolved_owner = _resolve_narrative_patch_owner(
                    nodes,
                    node_id=node_id,
                    patch_field=patch_field,
                    issue=issue,
                )
                if resolved_owner is None:
                    continue
                node, resolved_node_id = resolved_owner
                if resolved_node_id != node_id:
                    target = {
                        **target,
                        "id": resolved_node_id,
                        "retargeted_from_id": node_id,
                    }
                    node_id = resolved_node_id
                if patch_field.endswith("_id") and str(node.get(patch_field) or "") == node_id:
                    continue
                if patch_field in {"verbatim_excerpt", "source_text"} and str(
                    operation.value or ""
                ) not in source_text:
                    continue
                operation.path = patch_field
                if patch_field == "target_deltas" and isinstance(
                    operation.value, list,
                ):
                    valid_proposition_ids = {
                        str(item.get("proposition_id") or "")
                        for item in (plan_data.get("propositions") or [])
                        if isinstance(item, dict)
                    }
                    operation.value = [
                        {
                            **delta,
                            "proposition_ids": [
                                proposition_id
                                for proposition_id in (
                                    delta.get("proposition_ids") or []
                                )
                                if proposition_id in valid_proposition_ids
                            ],
                        }
                        if isinstance(delta, dict) else delta
                        for delta in operation.value
                    ]
            elif operation.op in {"delete_node", "move_node"} and node is None:
                continue
            elif operation.op in {"create_node", "insert_node"}:
                if node is not None or not isinstance(operation.value, dict):
                    continue
        elif operation.op == "replace_field":
            from app.production.screenplay_document import resolve_field_patch_target

            if not operation.path or operation.path in {"/", "$", "full_script_text"}:
                continue
            patch_field = re.split(
                r"[./]+", operation.path.strip("/"),
            )[-1]
            target = resolve_field_patch_target(
                document,
                path=patch_field,
                target=target,
            )
            chain_id = str(
                target.get("chain_id") or target.get("id") or "",
            ).strip()
            chain = next(
                (
                    item for item in document.dialogue_chains
                    if (item.chain_id or "").strip() == chain_id
                ),
                None,
            )
            if chain is not None and patch_field == "turns":
                if not _dialogue_chain_replacement_is_local(
                    document,
                    chain_id=chain_id,
                    turns=operation.value,
                    source_text=source_text,
                ):
                    continue
                target = {
                    **target,
                    "kind": "dialogue_chain",
                    "id": chain_id,
                    "chain_id": chain_id,
                }
            else:
                resolved_turn = _resolve_dialogue_chain_turn_target(
                    document,
                    target=target,
                    patch_field=patch_field,
                )
                if resolved_turn is not None:
                    target = {
                        **resolved_turn,
                        "kind": "dialogue_chain_turn",
                    }
            operation.path = patch_field
        if not _source_references_are_grounded(operation.value, source_text):
            continue
        selection_evidence = {
            "semantic_gap": payload.get("semantic_gap"),
            "candidate_ids": [item.get("candidate_id") for item in candidates if isinstance(item, dict)],
            "selected_candidate_id": selected_id,
            "selection_reason": (
                payload.get("selection_reason")
                if used_model_selection
                else (
                    "模型首选候选无法由当前 schema 与生产执行器解释；采用首个满足 "
                    f"gap/invariant 且可隔离执行的备选：{selected.get('rationale') or selected_id}"
                )
            ),
            "unclassified_dimensions": payload.get("unclassified_dimensions") or [],
            "expected_narrative_gain": selected.get("expected_narrative_gain"),
            "destructive_cost": selected.get("destructive_cost"),
        }
        operation.target = {**target, "semantic_selection": selection_evidence}
        safe.append(operation)
    if not safe:
        return []
    try:
        from app.production.patch import apply_patch_operation_to_document

        candidate_document = document
        for operation in safe:
            candidate_document, _ = apply_patch_operation_to_document(
                candidate_document,
                operation,
            )
    except Exception:  # noqa: BLE001 - reject an invalid model-authored candidate
        return []
    try:
        from app.narrative import validate_screenplay_narrative
        from app.production.screenplay_document import document_to_screenplay
        from app.validators import validate_screenplay

        def targeted_errors(candidate: EpisodeScreenplay) -> list[str]:
            errors = validate_screenplay_narrative(candidate, require=True)
            errors.extend(validate_screenplay(
                candidate,
                Bible(
                    characters=[],
                    world={"visual_style_canonical": ""},
                ),
                expected_beats=max(1, len(candidate.scene_outline or [])),
                episode_no=candidate.episode_no,
                source_text=source_text,
                require_dialogue_chains=True,
                validate_narrative=False,
            ))
            return errors

        baseline_errors = targeted_errors(document_to_screenplay(document))
        baseline_issues = issues_from_validator_messages(
            baseline_errors,
            subject="screenplay",
            stage="screenplay",
        )
        candidate_script = document_to_screenplay(candidate_document)
        _normalize_screenplay_narrative_graph(
            candidate_script,
            authorized_source_chapters=None,
        )
        candidate_errors = targeted_errors(candidate_script)
    except Exception as exc:  # noqa: BLE001 - local candidate validation must fail closed
        if rejection_feedback is not None:
            rejection_feedback.append(
                f"候选隔离复验失败：{type(exc).__name__}: {exc}",
            )
        return []
    if issue.message in candidate_errors:
        if rejection_feedback is not None:
            rejection_feedback.append(
                "以下候选操作隔离应用后，当前错误仍然存在，说明缺失关系没有被"
                "实际补齐。候选操作="
                + json.dumps(
                    [operation.model_dump(mode="json") for operation in safe],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )[:2400]
                + "；错误="
                + issue.message,
            )
        return []
    candidate_issues = issues_from_validator_messages(
        candidate_errors,
        subject="screenplay",
        stage="screenplay",
    )
    introduced = _introduced_issue_messages(
        baseline_issues,
        candidate_issues,
    )
    if introduced:
        if rejection_feedback is not None:
            rejection_feedback.append(
                "候选引入了新的确定性校验错误："
                + "；".join(introduced[:4]),
            )
        return []
    return safe
