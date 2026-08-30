"""剧本生成——分节拍分镜生成基线 _generate_screenplay_scene_sharded_baseline。"""
from __future__ import annotations

import asyncio
import json
from typing import Any


from app import config
from app.db import get_conn
from app.errors import ContentGenerationError
from app.narrative_blueprint import (
    BLUEPRINT_VERSION,
    NarrativeBlueprint,
    derive_blueprint_scene_plans,
    validate_and_apply_blueprint_scene_contract,
)
from app.schemas import (Bible, Dialogue, EMOTIONS, EpisodeScreenplay,
                         StoryboardOutlineShot)
from app.validators import (key_line_catalog)
from app.screenplay_ir import (
    compile_screenplay_ir,
)

from .blueprint_checkpoint import (
    _clear_ungrounded_ending_hook,
    _commit_blueprint_authority_checkpoint,
    _run_screenplay_workflow_step,
)
from .blueprint_generate_entry import _save_screenplay_generation_checkpoint
from .constants import SCREENPLAY_BLUEPRINT_PROMPT_VERSION
from .ir_complete import _complete_screenplay_ir_fidelity
from .ir_snapshot import _current_blueprint_authority_snapshot, _select_current_blueprint_artifact


async def _generate_screenplay_scene_sharded_baseline(
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    *,
    narrative_blueprint: NarrativeBlueprint,
) -> EpisodeScreenplay:
    """Generate resumable Envelope + Blueprint-owned scene shards, then compile."""
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace
    from app.screenplay_scene_shards import (
        SCREENPLAY_SHARD_PLAN_VERSION,
        blueprint_content_hash,
        blueprint_referenced_content_owners,
        build_frozen_identity_registry,
        build_screenplay_scene_input_contract_set,
        build_screenplay_scene_shard_plans,
        generate_screenplay_envelope,
        generate_screenplay_scene_shards,
        merge_screenplay_scene_shards,
        persist_identity_registry,
        persist_merged_ir,
        shard_progress,
    )

    # Rebuild the complete ownership contract at the orchestration boundary.
    # Resumed artifacts must not retain a pre-v3 scene projection.
    derive_blueprint_scene_plans(narrative_blueprint)

    # The old fixed third identity scan is replaced by a typed audit after the
    # Blueprint exists.  Only participant references not already covered by the
    # frozen authority projection are sent, together with their owned SRC.
    from app.identity_authority import identity_authority_registry
    from app.portraits import (
        ensure_structural_identity_coverage,
        screenplay_character_resolutions_for_source,
    )
    from app.orchestration.state_machine import StateConflict

    episode["character_resolutions"] = (
        screenplay_character_resolutions_for_source(
            list(episode.get("character_resolutions") or []),
            episode_no=int(episode.get("episode_no") or 0),
            source_text=source_text,
        )
    )
    authorities = identity_authority_registry(
        bible,
        list(episode.get("character_resolutions") or []),
    )
    known_identity_labels = {
        str(value).strip()
        for authority in authorities
        for value in (
            authority.get("canonical_name"),
            *(authority.get("source_labels") or []),
            authority.get("authority_id"),
        )
        if str(value or "").strip()
    }
    bible_identity_names = {
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
    }
    structural_identity_evidence: list[dict[str, Any]] = []
    for node in narrative_blueprint.nodes:
        if node.narrative_layer == "paratext":
            continue
        evidence_by_key = {
            item.identity_key: item
            for item in node.participant_evidence
        }
        for participant in node.participants:
            evidence = evidence_by_key.get(participant)
            usage = evidence.usage if evidence else "visible"
            if participant in known_identity_labels:
                matching_authorities = [
                    authority
                    for authority in authorities
                    if participant in {
                        str(authority.get("canonical_name") or "").strip(),
                        str(authority.get("authority_id") or "").strip(),
                        *(
                            str(value or "").strip()
                            for value in authority.get("source_labels") or []
                        ),
                    }
                ]
                nonmaterializable_named = any(
                    str(authority.get("identity_kind") or "").strip()
                    != "functional"
                    and not (
                        str(authority.get("canonical_name") or "").strip()
                        in bible_identity_names
                        and str(authority.get("authority_id") or "").strip()
                        == "bible:"
                        + str(authority.get("canonical_name") or "").strip()
                    )
                    for authority in matching_authorities
                )
                if (
                    usage in {"visible", "voice", "state_subject"}
                    and nonmaterializable_named
                ):
                    raise ContentGenerationError(
                        "Blueprint 可见人物只有不可物化的引用身份："
                        f"{participant} ({usage})"
                    )
                continue
            structural_identity_evidence.append({
                "identity_key": participant,
                "source_label": participant,
                "source_segment_ids": (
                    list(evidence.source_segment_ids)
                    if evidence else list(node.source_segment_ids)
                ),
                "usage": usage,
                "node_key": node.key,
            })
    if structural_identity_evidence and episode.get("project_id") and episode.get("id"):
        coverage_trace = current_trace()
        from app.production.revision import get_active_production_revision

        coverage_revision = get_active_production_revision(
            str(episode["id"]), "screenplay"
        )

        def assert_coverage_owner() -> None:
            owner_row = get_conn().execute(
                "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
                (str(episode["id"]),),
            ).fetchone()
            actual_owner = str(
                owner_row["active_screenplay_run_id"] or ""
            ) if owner_row else "missing"
            if actual_owner != coverage_trace.run_id:
                raise StateConflict(
                    "screenplay_resolution_owner",
                    str(episode["id"]),
                    {coverage_trace.run_id},
                    actual_owner,
                )

        coverage = await ensure_structural_identity_coverage(
            str(episode["project_id"]),
            str(episode["id"]),
            int(episode["episode_no"]),
            source_text,
            bible,
            structural_identity_evidence,
            write_guard=assert_coverage_owner,
            expected_active_run_id=coverage_trace.run_id,
            expected_revision_id=(
                coverage_revision.id if coverage_revision is not None else None
            ),
        )
        if coverage.get("errors"):
            raise ValueError(
                "蓝图人物权威收口失败："
                + "；".join(str(value) for value in coverage["errors"][:10])
            )
        if "resolutions" in coverage:
            episode["character_resolutions"] = list(
                coverage.get("resolutions") or []
            )
        if coverage.get("added"):
            project_row = get_conn().execute(
                "SELECT bible_json FROM projects WHERE id=?",
                (str(episode["project_id"]),),
            ).fetchone()
            if project_row and project_row["bible_json"]:
                bible = Bible.model_validate(json.loads(project_row["bible_json"]))

    episode_id = str(
        episode.get("id") or f"episode-{episode['episode_no']}"
    )
    blueprint_hash = blueprint_content_hash(narrative_blueprint)
    trace = current_trace()
    blueprint_row = get_conn().execute(
        """SELECT id,content_json,content_hash,model_snapshot_json,contract_version,
                  prompt_version
             FROM artifacts
             WHERE scope_type='episode' AND scope_id=?
               AND type='screenplay_narrative_blueprint'
               AND status='validated'
             ORDER BY created_at DESC LIMIT 20""",
        (episode_id,),
    ).fetchall()
    blueprint_artifact_id, legacy_same_hash_artifact_id = (
        _select_current_blueprint_artifact(
            list(blueprint_row),
            narrative_blueprint,
            source_text,
        )
    )
    if blueprint_artifact_id is None:
        artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint",
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T1",
                content=narrative_blueprint.model_dump(mode="json"),
                parent_artifact_ids=(
                    [legacy_same_hash_artifact_id]
                    if legacy_same_hash_artifact_id else []
                ),
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                model_snapshot=_current_blueprint_authority_snapshot(
                    source_text,
                    generation_mode="current_authority_wrapper",
                ),
            ),
            step_run_id=trace.step_run_id,
        )
        blueprint_artifact_id = str(artifact["id"])
    _commit_blueprint_authority_checkpoint(
        episode_id=episode_id,
        blueprint_artifact_id=blueprint_artifact_id,
        blueprint_hash=blueprint_hash,
        source_text=source_text,
    )

    async def freeze_identity() -> tuple[list[Any], list[dict[str, Any]], str, str]:
        current_resolutions = screenplay_character_resolutions_for_source(
            list(episode.get("character_resolutions") or []),
            episode_no=int(episode.get("episode_no") or 0),
            source_text=source_text,
        )
        episode["character_resolutions"] = current_resolutions
        identities_value, registry_value, registry_hash_value = (
            build_frozen_identity_registry(
                bible,
                current_resolutions,
                referenced_content_owners=(
                    blueprint_referenced_content_owners(narrative_blueprint)
                ),
            )
        )
        from app.production.revision import get_active_production_revision

        active_revision = get_active_production_revision(
            episode_id,
            "screenplay",
        )
        reused_identity = (
            (active_revision.checkpoint_json or {})
            .get("reused_inputs", {})
            .get("identity_registry", {})
            if active_revision is not None
            else {}
        )
        reused_identity_id = str(
            reused_identity.get("artifact_id") or ""
        )
        reused_identity_hash = str(
            reused_identity.get("identity_registry_hash") or ""
        )
        identity_parents = [blueprint_artifact_id]
        if (
            reused_identity_id
            and reused_identity_hash == registry_hash_value
        ):
            identity_parents.append(reused_identity_id)
        artifact_id_value = persist_identity_registry(
            episode_id=episode_id,
            identity_registry=registry_value,
            identity_registry_hash=registry_hash_value,
            parent_artifact_ids=identity_parents,
        )
        return (
            identities_value,
            registry_value,
            registry_hash_value,
            artifact_id_value,
        )

    (
        identities,
        identity_registry,
        identity_registry_hash,
        identity_artifact_id,
    ) = await _run_screenplay_workflow_step(
        "screenplay_identity_freeze",
        freeze_identity,
        agent_name="screenplay_identity_freeze",
        context_manifest={"episode_id": episode_id},
    )
    plans = build_screenplay_scene_shard_plans(
        narrative_blueprint,
        source_text=source_text,
        identity_registry_hash=identity_registry_hash,
        identity_registry=identity_registry,
    )
    scene_input_contracts = build_screenplay_scene_input_contract_set(
        plans=plans,
        blueprint=narrative_blueprint,
        source_text=source_text,
        identity_registry=identity_registry,
    )
    plan_payload = {
        "contract_version": SCREENPLAY_SHARD_PLAN_VERSION,
        "blueprint_hash": blueprint_hash,
        "identity_registry_hash": identity_registry_hash,
        "plans": [plan.model_dump(mode="json") for plan in plans],
    }
    shard_plan_hash = evidence_repository.content_hash(plan_payload)
    plan_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_scene_shard_plan",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content={**plan_payload, "shard_plan_hash": shard_plan_hash},
            parent_artifact_ids=[blueprint_artifact_id, identity_artifact_id],
            contract_version=SCREENPLAY_SHARD_PLAN_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    _save_screenplay_generation_checkpoint(
        episode_id,
        "ENVELOPE_GENERATION",
        identity_artifact_id=identity_artifact_id,
        identity_registry_hash=identity_registry_hash,
        shard_plan_artifact_id=plan_artifact["id"],
        shard_plan_hash=shard_plan_hash,
        shards=[
            {
                "shard_id": plan.shard_id,
                "status": "pending",
                "attempt": 0,
                "source_hash": plan.source_hash,
                "boundary_hash": plan.boundary_hash,
            }
            for plan in plans
        ],
        yield_reason=None,
    )

    def update_shard_progress(rows: list[dict[str, Any]]) -> None:
        _save_screenplay_generation_checkpoint(
            episode_id,
            "SCENE_SHARD_GENERATION",
            shards=rows,
            shard_progress=shard_progress(rows),
            yield_reason=None,
        )

    envelope_result, shard_result = await asyncio.gather(
        _run_screenplay_workflow_step(
            "screenplay_envelope",
            lambda: generate_screenplay_envelope(
                episode=episode,
                blueprint=narrative_blueprint,
                identity_registry=identity_registry,
                identity_registry_hash=identity_registry_hash,
                blueprint_artifact_id=blueprint_artifact_id,
                identity_artifact_id=identity_artifact_id,
                source_text=source_text,
            ),
            agent_name="screenplay_envelope",
            context_manifest={"episode_id": episode_id},
        ),
        _run_screenplay_workflow_step(
            "screenplay_scene_shards",
            lambda: generate_screenplay_scene_shards(
                episode=episode,
                source_text=source_text,
                blueprint=narrative_blueprint,
                identity_registry=identity_registry,
                identities=identities,
                plans=plans,
                scene_input_contracts=scene_input_contracts,
                blueprint_artifact_id=blueprint_artifact_id,
                identity_artifact_id=identity_artifact_id,
                progress=update_shard_progress,
            ),
            agent_name="screenplay_scene_shards",
            context_manifest={
                "episode_id": episode_id,
                "shard_count": len(plans),
            },
        ),
    )
    envelope, envelope_artifact_id = envelope_result
    shards, shard_artifact_ids, checkpoint_rows = shard_result
    _save_screenplay_generation_checkpoint(
        episode_id,
        "IR_MERGE",
        envelope_artifact_id=envelope_artifact_id,
        shards=checkpoint_rows,
        shard_progress=shard_progress(checkpoint_rows),
        yield_reason=None,
    )
    async def merge_ir() -> tuple[Any, str]:
        merged_value = merge_screenplay_scene_shards(
            envelope=envelope,
            identities=identities,
            plans=plans,
            shards=shards,
            scene_input_contracts=scene_input_contracts,
            blueprint=narrative_blueprint,
            source_text=source_text,
        )
        artifact_id_value = persist_merged_ir(
            episode_id=episode_id,
            ir=merged_value,
            parent_artifact_ids=[
                blueprint_artifact_id,
                identity_artifact_id,
                envelope_artifact_id,
                *shard_artifact_ids,
            ],
            blueprint_hash=blueprint_hash,
            identity_registry_hash=identity_registry_hash,
        )
        return merged_value, artifact_id_value

    merged_ir, merged_artifact_id = await _run_screenplay_workflow_step(
        "screenplay_merge",
        merge_ir,
        agent_name="screenplay_merge",
        context_manifest={
            "episode_id": episode_id,
            "shard_count": len(shards),
        },
    )
    _save_screenplay_generation_checkpoint(
        episode_id,
        "IR_MERGE",
        merged_ir_artifact_id=merged_artifact_id,
        yield_reason=None,
    )
    completed_ir = await _complete_screenplay_ir_fidelity(
        merged_ir,
        episode=episode,
        source_text=source_text,
        bible=bible,
        parent_artifact_id=merged_artifact_id,
        narrative_blueprint=narrative_blueprint,
    )
    blueprint_errors = validate_and_apply_blueprint_scene_contract(
        completed_ir,
        narrative_blueprint,
    )
    if blueprint_errors:
        raise ValueError("；".join(blueprint_errors))
    compiler_audit: list[dict[str, Any]] = []
    script = compile_screenplay_ir(
        completed_ir,
        episode=episode,
        source_text=source_text,
        bible=bible,
        audit=compiler_audit,
    )
    object.__setattr__(
        script,
        "_source_ir_artifact_id",
        getattr(completed_ir, "evidence_artifact_id", merged_artifact_id),
    )
    # 溯源校验：ending_hook 必须能在本集正文中找到对应内容支持，否则视为编造并清空。
    # 不再依据 episode.cliffhanger 是否预设来决定是否允许写 ending_hook——
    # 判据从"字段是否预设"换成"内容是否真的来自本集正文"。此处 script.events
    # 已由 compile_screenplay_ir 编译完成，传入后额外要求结构化事件溯源。清空
    # 动作本身必须可观测（见 _clear_ungrounded_ending_hook docstring）。
    _clear_ungrounded_ending_hook(
        script,
        episode_id=str(episode.get("id") or ""),
        source="screenplay_scene_sharded_baseline",
    )
    return script


def _speech_budget_table_text(durations: list[int] | None = None) -> str:
    """口播预算换算表：`{duration}s≤{cap}字`，逐镜/大纲提示词共用同一数据源。

    唯一数据源是 ``config.max_spoken_chars_for_duration``；两处提示词都必须调用
    这个函数拼装文案，禁止各自手写字面量表，否则公式一改就会有一处提示词悄悄漂移
    成谎言（大纲/逐镜历史上就出现过这种分叉：大纲只给了 10s 天花板，逐镜给了全表）。
    """
    values = sorted(durations) if durations is not None else sorted(config.ALLOWED_DURATIONS)
    return "、".join(f"{value}s≤{config.max_spoken_chars_for_duration(value)}字" for value in values)


def _parse_key_line(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    for separator in ("：", ":"):
        speaker, found, line = text.partition(separator)
        if found and speaker.strip() and line.strip():
            return speaker.strip(), line.strip()
    return "", text


def _scene_pack_dialogues(
    brief: StoryboardOutlineShot,
    screenplay: EpisodeScreenplay,
    emotions: dict[str, str],
    *,
    bible: Bible,
) -> list[Dialogue]:
    catalog = key_line_catalog(screenplay)
    visible_tokens = (
        list(brief.characters_visible)
        if brief.characters_visible
        else list(brief.visible_entity_ids)
    )
    visible_names = set(_canonical_scene_pack_names(
        visible_tokens,
        bible=bible,
        screenplay=screenplay,
        usage="visual",
    ))
    dialogues: list[Dialogue] = []
    for raw_id in brief.key_line_ids:
        key_id = str(raw_id or "").strip().upper()
        speaker, line = _parse_key_line(catalog.get(key_id, ""))
        if not speaker or not line:
            continue
        canonical_speakers = _canonical_scene_pack_names(
            [speaker],
            bible=bible,
            screenplay=screenplay,
            usage="voice",
        )
        canonical_speaker = canonical_speakers[0] if canonical_speakers else speaker
        emotion = str(
            emotions.get(key_id)
            or emotions.get(line)
            or "平静"
        ).strip()
        dialogues.append(Dialogue(
            speaker=canonical_speaker,
            line=line,
            emotion=emotion if emotion in EMOTIONS else "平静",
            delivery=(
                "spoken_dialogue"
                if canonical_speaker in visible_names
                else "offscreen_voice"
            ),
        ))
    return dialogues


def _canonical_scene_pack_names(
    values: list[str],
    *,
    bible: Bible,
    screenplay: EpisodeScreenplay,
    usage: str,
) -> list[str]:
    if screenplay.narrative_plan is None:
        return list(dict.fromkeys(
            str(value or "").strip() for value in values if str(value or "").strip()
        ))
    from app.identity_contracts import (
        IdentityContractError,
        narrative_identity_resolver,
    )

    resolver = narrative_identity_resolver(bible, screenplay)
    resolved: list[str] = []
    for value in values:
        token = str(value or "").strip()
        if not token:
            continue
        try:
            identity = resolver.resolve(token, usage=usage)
        except IdentityContractError:
            continue
        if identity.display_name not in resolved:
            resolved.append(identity.display_name)
    return resolved
