"""run_screenplay_qa: the single entry point that runs every deterministic
validator plus the semantic QA evaluator over a candidate screenplay and
returns the structured Issue list the repair loop consumes.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

import hashlib
import json
from app.character_policy import resolution_declares_functional_identity
from app.db import get_setting
from app.harness.types import (
    Evaluation,
    Issue,
)
from app.identity_authority import identity_resolution_is_authoritative
from app.production.structured_issues import (
    blocker_count,
    enrich_issues,
    issues_from_validator_messages,
    must_fix_count,
    structured_issue,
)
from app.schemas import (
    Bible,
    EpisodeScreenplay,
)
from typing import Any

from .gates import non_waivable_screenplay_issues


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
            and identity_resolution_is_authoritative(item)
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
        episode=episode,
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
        evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
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
            "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
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


