"""Generates the screenplay envelope (title/logline/experience metadata) via
the provider, reusing a compatible cached artifact when the blueprint and
identity registry it was built from are unchanged.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import json
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.narrative_blueprint import NarrativeBlueprint
from app.observability.tracing import current_trace
from typing import Any

from .artifact_compat import screenplay_envelope_artifact_compatibility
from .common import _setting_int
from .constants import SCREENPLAY_ENVELOPE_VERSION
from .identity_registry import (
    _assert_episode_owner,
    blueprint_content_hash,
)
from .models import ScreenplayEnvelopeIR
from .shard_merge import (
    _latest_validated_artifact,
    _raw_parent_artifact,
)


async def generate_screenplay_envelope(
    *,
    episode: dict[str, Any],
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict[str, Any]],
    identity_registry_hash: str,
    blueprint_artifact_id: str | None = None,
    identity_artifact_id: str | None = None,
    source_text: str = "",
) -> tuple[ScreenplayEnvelopeIR, str]:
    episode_id = str(episode.get("id") or f"episode-{episode['episode_no']}")
    _assert_episode_owner(episode_id)
    blueprint_hash = blueprint_content_hash(blueprint)
    cached = _latest_validated_artifact(
        episode_id=episode_id,
        artifact_type="screenplay_envelope",
        predicate=lambda content: (
            content.get("blueprint_hash") == blueprint_hash
            and content.get("identity_registry_hash") == identity_registry_hash
            and content.get("contract_version") == SCREENPLAY_ENVELOPE_VERSION
        ),
    )
    if cached:
        compatible, _reason = screenplay_envelope_artifact_compatibility(
            cached,
            expected_blueprint_hash=blueprint_hash,
            expected_identity_registry_hash=identity_registry_hash,
            raw_artifact=_raw_parent_artifact(cached),
            expected_authority_artifact_ids={
                str(blueprint_artifact_id or ""),
                str(identity_artifact_id or ""),
            },
        )
        if compatible:
            return ScreenplayEnvelopeIR.model_validate(cached["content"]), str(cached["id"])
    node_summary = [
        {
            "key": node.key,
            "summary": node.summary,
            "time_relation": node.time_relation,
            "location": node.location_label,
            "participants": node.participants,
            "scene_role": node.scene_role,
            "dramatic_load": node.dramatic_load,
            "action_logic": node.action_logic,
            "decision": node.decision.model_dump(mode="json") if node.decision else None,
            "agency": (
                node.decision.narrative_attribution if node.decision else None
            ),
        }
        for node in blueprint.nodes
    ]
    prompt = (
        "任务：根据已验证叙事蓝图生成整集全局 Screenplay Envelope。"
        "这里只决定 metadata 与 experience，不写 scenes，不需要也不得索要完整原文。"
        "不得在 approved_adaptations 中伪造来源事实。\n集信息：\n"
        + json.dumps({
            key: episode.get(key)
            for key in (
                "episode_no", "title", "synopsis", "hook", "cliffhanger",
            )
        }, ensure_ascii=False, separators=(",", ":"))
        + "\n蓝图全局摘要：\n"
        + json.dumps(node_summary, ensure_ascii=False, separators=(",", ":"))
        + "\n冻结身份摘要：\n"
        + json.dumps(identity_registry, ensure_ascii=False, separators=(",", ":"))
        + "\n只输出 Schema 对象：\n"
        + json.dumps(ScreenplayEnvelopeIR.model_json_schema(), ensure_ascii=False)
        + f"\n固定字段：contract_version={SCREENPLAY_ENVELOPE_VERSION},"
        f" episode_no={episode['episode_no']}, blueprint_hash={blueprint_hash},"
        f" identity_registry_hash={identity_registry_hash}"
    )

    def validate_envelope(value: ScreenplayEnvelopeIR) -> list[str]:
        errors: list[str] = []
        if value.episode_no != int(episode["episode_no"]):
            errors.append("episode_no 不匹配")
        if value.blueprint_hash != blueprint_hash:
            errors.append("blueprint_hash 不匹配")
        if value.identity_registry_hash != identity_registry_hash:
            errors.append("identity_registry_hash 不匹配")
        ending_hook = value.metadata.ending_hook.strip()
        if ending_hook:
            from app.validators import ending_hook_is_grounded

            if not ending_hook_is_grounded(ending_hook, source_text):
                errors.append(
                    "ending_hook 与本集原文几乎不重合，判定为编造下一集钩子："
                    "ending_hook 必须来自本集原文真实存在的悬念/转折/未完成动作，或留空"
                )
        return errors

    attempts: list[dict[str, Any]] = []
    envelope = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=ScreenplayEnvelopeIR,
        validate=validate_envelope,
        operation_id=(
            f"screenplay.envelope:{SCREENPLAY_ENVELOPE_VERSION}:"
            f"{episode_id}:{blueprint_hash}:{identity_registry_hash}"
        ),
        max_tokens=6144,
        temperature=0.2,
        format_retry_limit=_setting_int(
            "screenplay_format_retry_limit", 1, minimum=0, maximum=3
        ),
        semantic_retry_limit=_setting_int(
            "screenplay_semantic_retry_limit", 1, minimum=0, maximum=3
        ),
        call_meta={
            "stage": "剧本全局包络",
            "stage_key": "screenplay_envelope",
            "substage": "envelope",
            "episode_id": episode_id,
            "input_chars": len(prompt),
            "source_count": 0,
        },
        on_attempt=attempts.append,
    )
    _assert_episode_owner(episode_id)
    trace = current_trace()
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_envelope_raw",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T0",
            content={
                "operation_id": (
                    f"screenplay.envelope:{blueprint_hash}:{identity_registry_hash}"
                ),
                "attempts": attempts,
            },
            parent_artifact_ids=[
                value for value in (blueprint_artifact_id, identity_artifact_id) if value
            ],
            contract_version=SCREENPLAY_ENVELOPE_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_envelope",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content=envelope.model_dump(mode="json"),
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version=SCREENPLAY_ENVELOPE_VERSION,
        ),
        step_run_id=trace.step_run_id,
    )
    return envelope, str(artifact["id"])
