"""剧本身份决议的错误面校验：未消解身份与未知身份引用检查。
"""

from __future__ import annotations

import re

from app.character_policy import resolution_declares_functional_identity
from app.identity_authority import identity_resolution_is_authoritative
from app.schemas import Bible

from ._identity_tokens import (
    _identity_list_tokens,
    _project_identity_token,
)
from .resolution_apply_labels import _identity_value_contains

def screenplay_character_resolution_errors(screenplay, resolutions: list[dict] | None) -> list[str]:
    """剧本发布前硬门禁：过渡称谓不得再占据任何角色身份位。"""
    errors: list[str] = []
    for item in resolutions or []:
        if not isinstance(item, dict):
            continue
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        if not source_label or not canonical_name or source_label == canonical_name:
            continue
        preserves_current_display = item.get("resolution") == "future_identity"
        residual_paths: list[str] = []
        for scene in getattr(screenplay, "scene_outline", None) or []:
            if source_label in (scene.characters or []):
                residual_paths.append(f"scene_outline[{scene.scene_no}].characters")
        spine = getattr(screenplay, "plot_spine", None)
        for beat_index, beat in enumerate(
            (spine.spine_beats if spine is not None else None) or []
        ):
            for token in _identity_list_tokens(beat.who):
                projected = _project_identity_token(
                    token,
                    source_label,
                    canonical_name,
                )
                if token == source_label or projected != token:
                    residual_paths.append(
                        f"plot_spine.spine_beats[{beat_index}].who[{token}]"
                    )
        for chain_index, chain in enumerate(getattr(screenplay, "dialogue_chains", None) or []):
            for turn_index, turn in enumerate(chain.turns or []):
                if (turn.speaker or "").strip() == source_label:
                    residual_paths.append(f"dialogue_chains[{chain_index}].turns[{turn_index}].speaker")
        for index, info in enumerate(getattr(screenplay, "information_ledger", None) or []):
            if (info.speaker_id or "").strip() == source_label:
                residual_paths.append(f"information_ledger[{index}].speaker_id")
        for index, voice in enumerate(getattr(screenplay, "voice_bible", None) or []):
            if (voice.speaker_id or "").strip() == source_label:
                residual_paths.append(f"voice_bible[{index}].speaker_id")
        body = getattr(screenplay, "full_script_text", "") or ""
        speaker_pattern = re.compile(
            rf"(?m)^\s*{re.escape(source_label)}(?:[\(（][^\)）]{{0,16}}[\)）])?[:：]"
        )
        if not preserves_current_display and speaker_pattern.search(body):
            residual_paths.append("full_script_text.speaker")
        plan = getattr(screenplay, "narrative_plan", None)
        if plan is not None:
            for index, proposition in enumerate(plan.propositions):
                if source_label in proposition.entity_ids:
                    residual_paths.append(f"narrative_plan.propositions[{index}].entity_ids")
            for index, fact in enumerate(plan.state_facts):
                if fact.subject_id == source_label or _identity_value_contains(
                    fact.value.data, source_label,
                ):
                    residual_paths.append(f"narrative_plan.state_facts[{index}]")
            for index, evidence in enumerate(plan.evidence):
                if source_label in {
                    *evidence.perceivable_by,
                    *evidence.competing_attention_ids,
                }:
                    residual_paths.append(f"narrative_plan.evidence[{index}]")
            for index, action in enumerate(plan.atomic_actions):
                if source_label in {*action.actor_ids, *action.target_ids}:
                    residual_paths.append(f"narrative_plan.atomic_actions[{index}]")
            for index, state in enumerate(plan.character_states):
                if (
                    state.character_id == source_label
                    or _identity_value_contains(state.relationship_state, source_label)
                    or _identity_value_contains(state.emotion, source_label)
                ):
                    residual_paths.append(f"narrative_plan.character_states[{index}]")
            for index, belief in enumerate(plan.character_beliefs):
                if belief.character_id == source_label:
                    residual_paths.append(f"narrative_plan.character_beliefs[{index}]")
            for index, state in enumerate(plan.audience_states):
                if any(
                    _identity_value_contains(getattr(state, field), source_label)
                    for field in (
                        "causal_hypotheses",
                        "character_goal_hypotheses",
                        "spatial_model",
                        "temporal_model",
                        "working_memory",
                        "attention_residue_ids",
                        "affective_state",
                    )
                ):
                    residual_paths.append(f"narrative_plan.audience_states[{index}]")
            for index, intent in enumerate(plan.experience_intents):
                if source_label in intent.attention_target_ids:
                    residual_paths.append(f"narrative_plan.experience_intents[{index}]")
            for index, scene in enumerate(plan.scene_contracts):
                if (
                    scene.point_of_view_character_id == source_label
                    or _identity_value_contains(scene.relationship_deltas, source_label)
                ):
                    residual_paths.append(f"narrative_plan.scene_contracts[{index}]")
        if residual_paths:
            errors.append(
                f"角色身份预解析未落实：「{source_label}」必须在剧本阶段改为「{canonical_name}」；"
                f"残留位置：{', '.join(residual_paths[:8])}"
            )
    return errors


def screenplay_unknown_identity_errors(
    screenplay,
    bible: Bible,
    resolutions: list[dict] | None = None,
) -> list[str]:
    """确定性检查“模型判断是否已经落地”，不猜测称谓语义。"""
    bible_names = {character.name for character in bible.characters}
    narrative_plan = getattr(screenplay, "narrative_plan", None)
    narrative_authority = narrative_plan is not None
    if not bible_names and not narrative_authority:
        # 保留无真实人物谱项目的历史占位流程；有 Bible 时才启用身份硬门禁。
        return []
    resolver = None
    if narrative_authority:
        from app.identity_contracts import (
            IdentityContractError,
            narrative_identity_resolver,
        )

        try:
            resolver = narrative_identity_resolver(bible, screenplay)
        except IdentityContractError as exc:
            return [f"剧本身份合同无法解析：{exc}"]
    locations: dict[str, list[str]] = {}
    typed_functional_names = {
        str(item.get("canonical_name") or "").strip()
        for item in (resolutions or [])
        if (
            isinstance(item, dict)
            and identity_resolution_is_authoritative(item)
            and resolution_declares_functional_identity(item)
            and str(item.get("canonical_name") or "").strip()
        )
    }

    def collect(raw_name: str, path: str, *, usage: str) -> None:
        name = str(raw_name or "").strip()
        if not name:
            return
        if narrative_authority:
            try:
                resolver.resolve(name, usage=usage)
                return
            except IdentityContractError:
                pass
        elif name == "旁白" or name in bible_names:
            return
        elif name in typed_functional_names:
            return
        locations.setdefault(name, []).append(path)

    for scene_index, scene in enumerate(getattr(screenplay, "scene_outline", None) or []):
        for name in scene.characters or []:
            collect(name, f"scene_outline[{scene_index}].characters", usage="visual")
    # PlotSpineBeat.who is an event subject, not a visual-identity declaration.
    # It may carry a typed identity, prop, spatial boundary, or offscreen source.
    # Identity policy comes from the typed carriers above/below and the narrative
    # graph. Exact character resolutions still project into ``who`` and retain
    # their dedicated residual check in screenplay_character_resolution_errors.
    for chain_index, chain in enumerate(getattr(screenplay, "dialogue_chains", None) or []):
        for turn_index, turn in enumerate(chain.turns or []):
            collect(
                turn.speaker,
                f"dialogue_chains[{chain_index}].turns[{turn_index}].speaker",
                usage="voice",
            )
    for index, item in enumerate(getattr(screenplay, "information_ledger", None) or []):
        collect(item.speaker_id, f"information_ledger[{index}].speaker_id", usage="voice")
    # 与 validate_screenplay 共用同一台本解析器，避免把“地点：”“场景：”
    # 这类台本标签误当成人名。这里只检查模型决议是否落地，不猜称谓语义。
    from app.validators import screenplay_speaker_names
    for speaker in screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or ""
    ):
        collect(speaker, "full_script_text.speaker", usage="voice")
    return [
        f"剧本人物身份未解决：「{name}」既不在人物谱，"
        + (
            "也未由本集 identity_contracts + voice_bible 定义可见/声音政策；"
            if narrative_authority
            else "也未被人物预检模型映射为一次性角色；"
        )
        + f"位置：{', '.join(paths[:8])}"
        for name, paths in locations.items()
    ]

