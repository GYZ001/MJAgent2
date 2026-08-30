"""把身份决议应用到整份剧本：apply_screenplay_character_resolutions 及
剧本身份/画外音标注归一化。
"""

from __future__ import annotations

import re

from app.character_policy import resolution_declares_functional_identity
from app.schemas import Bible

from .discovery_fragments import _identity_carrier_annotation_base
from .resolution_apply_labels import (
    _merge_duplicate_narrative_identity_contracts,
    _replace_identity_list_label,
    _replace_narrative_plan_identity,
    _replace_resolved_label,
    _replace_screenplay_body_label,
    _restore_non_dialogue_prefix,
)

def apply_screenplay_character_resolutions(screenplay, resolutions: list[dict] | None) -> list[dict]:
    """在剧本进入 QA/发布之前原子性落实人物身份映射。

    原文证据字段（source_text/source_basis/source_fact/source_span）保持不变，
    避免破坏逐字证据；所有会被下游当成角色身份的字段统一改名。
    """
    changes: list[dict] = []
    authoritative_speakers = {
        str(turn.speaker or "").strip()
        for chain in getattr(screenplay, "dialogue_chains", None) or []
        for turn in chain.turns or []
        if str(turn.speaker or "").strip()
    }
    authoritative_lines_by_speaker: dict[str, set[str]] = {}
    for chain in getattr(screenplay, "dialogue_chains", None) or []:
        for turn in chain.turns or []:
            speaker = str(turn.speaker or "").strip()
            line = str(turn.line or "").strip()
            if speaker and line:
                authoritative_lines_by_speaker.setdefault(
                    speaker,
                    set(),
                ).add(line)
    for item in resolutions or []:
        if not isinstance(item, dict):
            continue
        # Occurrence-scoped identity decisions can legitimately share one
        # source label (for example two people both called “绿袍修士”).  Their
        # authority_id is already bound inside the IR, so a global text
        # replacement here would arbitrarily assign every occurrence to the
        # first entity and corrupt the compiled identity graph.
        if str(item.get("source_instance_key") or "").strip():
            continue
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        if not source_label or not canonical_name or source_label == canonical_name:
            continue
        replace_display_text = item.get("resolution") != "future_identity"

        changed = False
        for scene in getattr(screenplay, "scene_outline", None) or []:
            before = list(scene.characters or [])
            scene.characters = list(dict.fromkeys(
                canonical_name if name == source_label else name
                for name in before
            ))
            changed = changed or scene.characters != before
            if replace_display_text:
                for field in ("story_function", "summary", "conflict", "turn"):
                    value = getattr(scene, field, "") or ""
                    replaced = _replace_resolved_label(value, source_label, canonical_name)
                    if replaced != value:
                        setattr(scene, field, replaced)
                        changed = True

        body = getattr(screenplay, "full_script_text", "") or ""
        replaced_body = _replace_screenplay_body_label(
            body,
            source_label,
            canonical_name,
            replace_prose=replace_display_text,
            replace_speaker=source_label in authoritative_speakers,
        )
        if source_label not in authoritative_speakers:
            replaced_body = _restore_non_dialogue_prefix(
                replaced_body,
                source_label,
                canonical_name,
                authoritative_lines=authoritative_lines_by_speaker.get(
                    canonical_name,
                    set(),
                ),
            )
        if replaced_body != body:
            screenplay.full_script_text = replaced_body
            changed = True

        spine = getattr(screenplay, "plot_spine", None)
        if spine is not None:
            for beat in spine.spine_beats or []:
                for field in (
                    ("who", "does", "turn")
                    if replace_display_text
                    else ("who",)
                ):
                    value = getattr(beat, field, "") or ""
                    replaced = (
                        _replace_identity_list_label(
                            value,
                            source_label,
                            canonical_name,
                        )
                        if field == "who"
                        else _replace_resolved_label(
                            value,
                            source_label,
                            canonical_name,
                        )
                    )
                    if replaced != value:
                        setattr(beat, field, replaced)
                        changed = True

        for chain in getattr(screenplay, "dialogue_chains", None) or []:
            for turn in chain.turns or []:
                if (turn.speaker or "").strip() == source_label:
                    turn.speaker = canonical_name
                    changed = True

        for event in getattr(screenplay, "events", None) or []:
            if replace_display_text:
                for field in ("state_in", "trigger", "visible_change", "state_out", "adaptation_reason"):
                    value = getattr(event, field, "") or ""
                    replaced = _replace_resolved_label(value, source_label, canonical_name)
                    if replaced != value:
                        setattr(event, field, replaced)
                        changed = True

        for info in getattr(screenplay, "information_ledger", None) or []:
            if (info.speaker_id or "").strip() == source_label:
                info.speaker_id = canonical_name
                changed = True
            if replace_display_text:
                content = info.content or ""
                replaced = _replace_resolved_label(content, source_label, canonical_name)
                if replaced != content:
                    info.content = replaced
                    changed = True

        for voice in getattr(screenplay, "voice_bible", None) or []:
            if (voice.speaker_id or "").strip() == source_label:
                voice.speaker_id = canonical_name
                if getattr(screenplay, "narrative_plan", None) is not None:
                    if (
                        resolution_declares_functional_identity(item)
                        and str(voice.role_type or "").strip() != "narrator"
                    ):
                        voice.role_type = "functional_character"
                elif resolution_declares_functional_identity(item):
                    voice.role_type = "functional_character"
                changed = True

        changed = _replace_narrative_plan_identity(
            getattr(screenplay, "narrative_plan", None),
            source_label,
            canonical_name,
            replace_display_text=replace_display_text,
        ) or changed

        if replace_display_text:
            for field in (
                "logline", "dramatic_question", "protagonist_goal", "obstacle", "stakes",
                "emotional_curve", "ending_hook", "adaptation_direction", "opening", "development",
                "conflict", "climax", "episode_premise",
            ):
                value = getattr(screenplay, field, "") or ""
                replaced = _replace_resolved_label(value, source_label, canonical_name)
                if replaced != value:
                    setattr(screenplay, field, replaced)
                    changed = True
            for field in (
                "key_lines", "key_plot_points", "character_state_changes",
                "approved_adaptations", "forbidden_additions",
            ):
                values = list(getattr(screenplay, field, None) or [])
                replaced_values = [
                    _replace_resolved_label(value, source_label, canonical_name)
                    for value in values
                ]
                if replaced_values != values:
                    setattr(screenplay, field, replaced_values)
                    changed = True

        if changed:
            changes.append({
                "source_label": source_label,
                "canonical_name": canonical_name,
                "resolution": item.get("resolution") or "unknown",
            })
    changes.extend(_merge_duplicate_narrative_identity_contracts(
        getattr(screenplay, "narrative_plan", None),
    ))
    return changes


def normalize_screenplay_identity_annotations(screenplay, bible: Bible) -> list[dict]:
    """Strip carrier annotations only when the base is already authoritative.

    Identity fields may contain presentation notes such as ``角色（画外）``.
    This normalization never interprets the note or classifies role names. It
    only projects an exact Bible/contract/voice token back to its canonical
    display name; ambiguous or unknown bases remain unresolved for model audit.
    """
    visual_targets: dict[str, set[str]] = {}
    voice_targets: dict[str, set[str]] = {}

    def register(targets: dict[str, set[str]], token: object, canonical: str) -> None:
        value = str(token or "").strip()
        if value and canonical:
            targets.setdefault(value, set()).add(canonical)

    for character in bible.characters:
        name = str(character.name or "").strip()
        register(visual_targets, name, name)
        register(voice_targets, name, name)

    plan = getattr(screenplay, "narrative_plan", None)
    for contract in (getattr(plan, "identity_contracts", None) or []):
        canonical = str(contract.display_name or "").strip()
        if str(contract.visual_policy or "").strip() != "offscreen_only":
            register(visual_targets, contract.identity_id, canonical)
            register(visual_targets, contract.display_name, canonical)
        for voice_id in contract.voice_ids or []:
            register(voice_targets, voice_id, canonical)

    for voice in getattr(screenplay, "voice_bible", None) or []:
        if str(voice.role_type or "").strip() == "narrator":
            speaker_id = str(voice.speaker_id or "").strip()
            register(voice_targets, speaker_id, speaker_id)

    usages: dict[str, set[str]] = {}

    def collect(raw: object, usage: str) -> None:
        value = str(raw or "").strip()
        if _identity_carrier_annotation_base(value):
            usages.setdefault(value, set()).add(usage)

    for scene in getattr(screenplay, "scene_outline", None) or []:
        for character in scene.characters or []:
            collect(character, "visual")
    for chain in getattr(screenplay, "dialogue_chains", None) or []:
        for turn in chain.turns or []:
            collect(turn.speaker, "voice")
    for item in getattr(screenplay, "information_ledger", None) or []:
        collect(item.speaker_id, "voice")
    for voice in getattr(screenplay, "voice_bible", None) or []:
        collect(voice.speaker_id, "voice")
    from app.validators import screenplay_speaker_names
    for speaker in screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or ""
    ):
        collect(speaker, "voice")

    resolutions: list[dict] = []
    target_maps = {"visual": visual_targets, "voice": voice_targets}
    for source_label, required_usages in usages.items():
        base = _identity_carrier_annotation_base(source_label)
        candidates: set[str] | None = None
        for usage in required_usages:
            current = target_maps[usage].get(base, set())
            candidates = set(current) if candidates is None else candidates & current
        if candidates and len(candidates) == 1:
            resolutions.append({
                "source_label": source_label,
                "canonical_name": next(iter(candidates)),
                "resolution": "authority_annotation",
            })
    if not resolutions:
        return []
    return apply_screenplay_character_resolutions(screenplay, resolutions)


def normalize_screenplay_offscreen_visual_identities(screenplay) -> list[dict]:
    """Remove typed offscreen-only identities from visual scene membership."""
    plan = getattr(screenplay, "narrative_plan", None)
    if plan is None:
        return []
    offscreen_tokens = {
        token
        for contract in plan.identity_contracts
        if str(contract.visual_policy or "").strip() == "offscreen_only"
        for token in {
            str(contract.identity_id or "").strip(),
            str(contract.display_name or "").strip(),
            *(
                str(voice_id or "").strip()
                for voice_id in (contract.voice_ids or [])
            ),
        }
        if token
    }
    if not offscreen_tokens:
        return []

    changes: list[dict] = []
    for scene in getattr(screenplay, "scene_outline", None) or []:
        before = list(scene.characters or [])
        scene.characters = [
            identity for identity in before
            if str(identity or "").strip() not in offscreen_tokens
        ]
        removed = [
            identity for identity in before
            if str(identity or "").strip() in offscreen_tokens
        ]
        if removed:
            changes.append({
                "source_label": ",".join(str(value) for value in removed),
                "canonical_name": "",
                "resolution": "offscreen_visual_membership_removed",
                "scene_no": scene.scene_no,
            })
    return changes


def normalize_screenplay_voice_ids(screenplay, bible: Bible) -> list[dict]:
    """Normalize voice aliases and remove unreferenced non-identity entries.

    New prompts require Bible character names as speaker IDs.  This migration
    path handles existing working artifacts without guessing from initials or
    role labels: the alias must own ledger text that names exactly one Bible
    character, and that character must actually speak in the screenplay.
    Ambiguous or referenced aliases remain untouched so the identity gate still
    fails closed. Unbound entries that no spoken field references are dead
    metadata, not identities, and are removed without inspecting their names or
    role labels.
    """
    changes = normalize_screenplay_identity_annotations(screenplay, bible)
    plan = getattr(screenplay, "narrative_plan", None)
    if plan is None:
        return changes
    bible_names = {
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
    }
    for voice in getattr(screenplay, "voice_bible", None) or []:
        speaker_id = str(voice.speaker_id or "").strip()
        role_type = str(voice.role_type or "").strip()
        if not speaker_id:
            continue
        matching_contracts = [
            contract
            for contract in plan.identity_contracts
            if (
                speaker_id in {
                    str(contract.identity_id or "").strip(),
                    str(contract.display_name or "").strip(),
                }
                and (
                    role_type != "narrator"
                    or str(contract.visual_policy or "").strip()
                    == "offscreen_only"
                )
            )
        ]
        if len(matching_contracts) != 1:
            continue
        contract = matching_contracts[0]
        before = list(contract.voice_ids or [])
        if speaker_id not in before:
            contract.voice_ids = [*before, speaker_id]
            changes.append({
                "source_label": speaker_id,
                "canonical_name": speaker_id,
                "resolution": (
                    "narrator_voice_contract_bound"
                    if role_type == "narrator"
                    else "voice_contract_bound"
                ),
            })
    explicitly_bound = {
        str(voice_id or "").strip()
        for contract in plan.identity_contracts
        for voice_id in contract.voice_ids
        if str(voice_id or "").strip()
    }
    from app.validators import screenplay_speaker_names

    dialogue_speakers = {
        str(turn.speaker or "").strip()
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip()
    }
    dialogue_speakers.update(screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or "",
    ))
    dialogue_turns = [
        (
            str(turn.speaker or "").strip(),
            str(turn.line or "").strip(),
        )
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip() and str(turn.line or "").strip()
    ]

    def alias_candidate(ledger_items) -> str | None:
        ledger_text = "\n".join(
            f"{item.content or ''}\n{item.exact_text or ''}"
            for item in ledger_items
        )
        exact_texts = {
            str(item.exact_text or "").strip()
            for item in ledger_items
            if str(item.exact_text or "").strip()
        }
        exact_speakers = {
            speaker
            for speaker, line in dialogue_turns
            for exact_text in exact_texts
            if (
                speaker in bible_names
                and (exact_text == line or exact_text in line or line in exact_text)
            )
        }
        mentioned_candidates = {
            name
            for name in bible_names
            if name in dialogue_speakers and name in ledger_text
        }
        leading_candidates = {
            name
            for name in mentioned_candidates
            if any(
                str(item.content or "").strip().startswith(name)
                for item in ledger_items
            )
        }
        candidates = (
            exact_speakers
            if len(exact_speakers) == 1
            else mentioned_candidates
            if len(mentioned_candidates) == 1
            else leading_candidates
        )
        return next(iter(candidates)) if len(candidates) == 1 else None

    voice_delivery_owners = {"spoken_dialogue", "offscreen_voice", "narration"}
    non_voice_carriers: set[str] = set()
    for voice in getattr(screenplay, "voice_bible", None) or []:
        source_id = str(voice.speaker_id or "").strip()
        if (
            not source_id
            or str(voice.role_type or "").strip() == "narrator"
            or source_id in bible_names
            or source_id in explicitly_bound
        ):
            continue
        ledger_items = [
            item
            for item in (getattr(screenplay, "information_ledger", None) or [])
            if str(item.speaker_id or "").strip() == source_id
        ]
        if alias_candidate(ledger_items):
            continue
        if ledger_items and all(
            str(item.delivery_owner or "").strip() not in voice_delivery_owners
            for item in ledger_items
        ):
            non_voice_carriers.add(source_id)

    if non_voice_carriers:
        for item in getattr(screenplay, "information_ledger", None) or []:
            if str(item.speaker_id or "").strip() in non_voice_carriers:
                item.speaker_id = None
        for chain in getattr(screenplay, "dialogue_chains", None) or []:
            chain.turns = [
                turn for turn in (chain.turns or [])
                if str(turn.speaker or "").strip() not in non_voice_carriers
            ]
        screenplay.dialogue_chains = [
            chain for chain in (getattr(screenplay, "dialogue_chains", None) or [])
            if chain.turns
        ]
        retained_key_lines: list[str] = []
        for line in getattr(screenplay, "key_lines", None) or []:
            speaker, separator, _ = str(line or "").partition("：")
            if not separator:
                speaker, separator, _ = str(line or "").partition(":")
            if separator and speaker.strip() in non_voice_carriers:
                continue
            retained_key_lines.append(line)
        screenplay.key_lines = retained_key_lines
        body = getattr(screenplay, "full_script_text", "") or ""
        for source_id in sorted(non_voice_carriers):
            body = re.sub(
                rf"(?m)^(\s*){re.escape(source_id)}"
                r"(?:[\(（][^\)）]{0,16}[\)）])?\s*[:：]\s*(.*)$",
                lambda match: f"{match.group(1)}【{match.group(2).strip()}】",
                body,
            )
        screenplay.full_script_text = body
        screenplay.voice_bible = [
            voice
            for voice in (getattr(screenplay, "voice_bible", None) or [])
            if str(voice.speaker_id or "").strip() not in non_voice_carriers
        ]
        non_voice_changes = [{
            "source_label": source_id,
            "canonical_name": "",
            "resolution": "non_voice_carrier_removed",
        } for source_id in sorted(non_voice_carriers)]
    else:
        non_voice_changes = []

    dialogue_speakers = {
        str(turn.speaker or "").strip()
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip()
    }
    dialogue_speakers.update(screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or "",
    ))
    ledger_speakers = {
        str(item.speaker_id or "").strip()
        for item in (getattr(screenplay, "information_ledger", None) or [])
        if str(item.speaker_id or "").strip()
    }
    referenced_speakers = dialogue_speakers | ledger_speakers
    existing_voice_ids = {
        str(voice.speaker_id or "").strip()
        for voice in (getattr(screenplay, "voice_bible", None) or [])
        if str(voice.speaker_id or "").strip()
    }
    unreferenced_voice_ids: set[str] = set()

    for voice in getattr(screenplay, "voice_bible", None) or []:
        source_id = str(voice.speaker_id or "").strip()
        if (
            not source_id
            or str(voice.role_type or "").strip() == "narrator"
            or source_id in bible_names
            or source_id in explicitly_bound
        ):
            continue
        ledger_items = [
            item
            for item in (getattr(screenplay, "information_ledger", None) or [])
            if str(item.speaker_id or "").strip() == source_id
        ]
        if not ledger_items:
            if source_id not in referenced_speakers:
                unreferenced_voice_ids.add(source_id)
            continue
        canonical_name = alias_candidate(ledger_items)
        if not canonical_name:
            continue
        if canonical_name in existing_voice_ids:
            continue

        voice.speaker_id = canonical_name
        for item in ledger_items:
            item.speaker_id = canonical_name
        existing_voice_ids.discard(source_id)
        existing_voice_ids.add(canonical_name)
        changes.append({
            "source_label": source_id,
            "canonical_name": canonical_name,
            "resolution": "voice_alias_from_ledger",
        })

    changes.extend(non_voice_changes)
    if unreferenced_voice_ids:
        screenplay.voice_bible = [
            voice
            for voice in (getattr(screenplay, "voice_bible", None) or [])
            if str(voice.speaker_id or "").strip() not in unreferenced_voice_ids
        ]
        changes.extend({
            "source_label": source_id,
            "canonical_name": "",
            "resolution": "unreferenced_voice_removed",
        } for source_id in sorted(unreferenced_voice_ids))

    changes.extend(normalize_screenplay_offscreen_visual_identities(screenplay))
    return changes

