"""Single typed identity authority for narrative downstream consumers.

The screenplay's identity contracts decide rendering and asset behaviour.  The
resolver performs exact ID/alias joins only; it never infers policy from a
person's name, title, gender, occupation, or story genre.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Literal

from app.schemas import (
    Bible,
    EpisodeScreenplay,
    NarrativeIdentityContract,
    Shot,
    Storyboard,
)


IdentityUsage = Literal["reference", "visual", "voice"]


class IdentityContractError(ValueError):
    """A narrative identity is missing, ambiguous, or used against its policy."""


@dataclass(frozen=True)
class ResolvedIdentity:
    identity_id: str
    display_name: str
    asset_name: str
    kind: str
    visual_policy: Literal[
        "canonical", "contextual", "collective", "offscreen_only",
    ]
    visual_canonical: str
    asset_requirement: Literal["required", "optional", "forbidden"]
    voice_ids: tuple[str, ...]
    source: Literal["bible", "narrative_contract", "voice_narrator"]

    @property
    def can_be_visible(self) -> bool:
        return self.visual_policy != "offscreen_only"

    @property
    def is_collective(self) -> bool:
        return self.visual_policy == "collective"

    @property
    def requires_asset(self) -> bool:
        return self.asset_requirement == "required"

    @property
    def allows_asset(self) -> bool:
        return self.asset_requirement != "forbidden"

    def visual_anchor(self) -> str:
        if not self.can_be_visible:
            raise IdentityContractError(
                f"身份 {self.identity_id} ({self.display_name}) 只允许画外出现"
            )
        anchor = self.visual_canonical.strip()
        if not anchor:
            raise IdentityContractError(
                f"可见身份 {self.identity_id} ({self.display_name}) 缺少 visual_canonical"
            )
        return anchor


def _clean(value: object) -> str:
    return str(value or "").strip()


def _semantic_alias_key(value: object) -> str:
    """Normalize a contract-local role phrase without using a global title list."""
    return "".join(
        character.casefold()
        for character in _clean(value)
        if character.isalnum() and character != "的"
    )


def _contract_evidence_errors(
    contract: NarrativeIdentityContract,
    screenplay: EpisodeScreenplay,
) -> list[str]:
    plan = screenplay.narrative_plan
    if plan is None:
        return []
    evidence = contract.evidence
    source_ids = {item.source_evidence_id for item in plan.source_evidence}
    proposition_ids = {item.proposition_id for item in plan.propositions}
    decision_ids = {
        item.adaptation_decision_id for item in plan.adaptation_decisions
    }
    errors: list[str] = []
    for label, values, allowed in (
        ("source_evidence_ids", evidence.source_evidence_ids, source_ids),
        ("proposition_ids", evidence.proposition_ids, proposition_ids),
        ("adaptation_decision_ids", evidence.adaptation_decision_ids, decision_ids),
    ):
        missing = sorted({_clean(value) for value in values if _clean(value)} - allowed)
        if missing:
            errors.append(f"{label} 引用了不存在的 ID {missing}")
    if not any((
        evidence.source_evidence_ids,
        evidence.proposition_ids,
        evidence.adaptation_decision_ids,
    )):
        errors.append("缺少可追溯的 evidence ID")
    if not _clean(evidence.rationale):
        errors.append("缺少身份意图判定 rationale")
    return errors


class NarrativeIdentityResolver:
    """Exact, fail-closed join of Bible, narrative contracts, and voice Bible."""

    def __init__(self, bible: Bible, screenplay: EpisodeScreenplay):
        if screenplay.narrative_plan is None:
            raise IdentityContractError("只能为含 narrative_plan 的剧本构建身份解析器")
        self._by_token: dict[str, ResolvedIdentity] = {}
        self._voice_tokens: dict[str, ResolvedIdentity] = {}
        self._identities: dict[str, ResolvedIdentity] = {}
        self._rationale_by_identity: dict[str, str] = {}
        bible_by_name = {
            _clean(character.name): character
            for character in bible.characters
            if _clean(character.name)
        }
        claimed_bible_names: set[str] = set()

        for contract in screenplay.narrative_plan.identity_contracts:
            evidence_errors = _contract_evidence_errors(contract, screenplay)
            if evidence_errors:
                raise IdentityContractError(
                    f"身份合同 {contract.identity_id} 无法验证："
                    + "；".join(evidence_errors)
                )
            identity_id = _clean(contract.identity_id)
            display_name = _clean(contract.display_name)
            matching_bible_names = {
                value for value in (identity_id, display_name) if value in bible_by_name
            }
            if len(matching_bible_names) > 1:
                raise IdentityContractError(
                    f"身份合同 {identity_id} 同时指向多个 Bible 身份："
                    f"{sorted(matching_bible_names)}"
                )
            bible_name = next(iter(matching_bible_names), None)
            if bible_name:
                claimed_bible_names.add(bible_name)
                character = bible_by_name[bible_name]
                visual_policy = "canonical"
                visual_canonical = _clean(character.appearance_canonical)
                asset_requirement = "required"
            else:
                visual_policy = contract.visual_policy
                visual_canonical = _clean(contract.visual_canonical)
                asset_requirement = contract.asset_requirement
            resolved = ResolvedIdentity(
                identity_id=identity_id,
                display_name=display_name,
                asset_name=bible_name or display_name,
                kind=_clean(contract.kind),
                visual_policy=visual_policy,
                visual_canonical=visual_canonical,
                asset_requirement=asset_requirement,
                voice_ids=tuple(_clean(value) for value in contract.voice_ids),
                source="bible" if bible_name else "narrative_contract",
            )
            self._register(resolved)
            self._rationale_by_identity[identity_id] = _clean(
                contract.evidence.rationale
            )

        # Bible remains authoritative for persistent characters even when a
        # plan does not repeat them in ``identity_contracts``.  This is an
        # explicit source merge, not a name-shape inference.
        for name, character in bible_by_name.items():
            if name in claimed_bible_names:
                continue
            self._register(ResolvedIdentity(
                identity_id=name,
                display_name=name,
                asset_name=name,
                kind="bible_character",
                visual_policy="canonical",
                visual_canonical=_clean(character.appearance_canonical),
                asset_requirement="required",
                voice_ids=(),
                source="bible",
            ))

        for voice in screenplay.voice_bible:
            speaker_id = _clean(voice.speaker_id)
            if not speaker_id:
                continue
            role_type = _clean(voice.role_type)
            linked = self._identity_linked_to_voice(speaker_id)
            if linked is None and role_type == "narrator":
                linked = ResolvedIdentity(
                    identity_id=speaker_id,
                    display_name=speaker_id,
                    asset_name=speaker_id,
                    kind="narrator",
                    visual_policy="offscreen_only",
                    visual_canonical="",
                    asset_requirement="forbidden",
                    voice_ids=(speaker_id,),
                    source="voice_narrator",
                )
                self._register(linked)
            elif linked is None:
                raise IdentityContractError(
                    f"voice_bible speaker_id={speaker_id} 未通过 identity_contract.voice_ids "
                    "绑定身份，也不是 Bible 中同 ID 角色"
                )
            self._register_voice_aliases(linked, speaker_id)

    @property
    def identities(self) -> tuple[ResolvedIdentity, ...]:
        return tuple(self._identities.values())

    def _register(self, identity: ResolvedIdentity) -> None:
        if identity.identity_id in self._identities:
            raise IdentityContractError(f"identity_id 重复：{identity.identity_id}")
        tokens = {identity.identity_id, identity.display_name, *identity.voice_ids}
        for token in tokens:
            if not token:
                continue
            existing = self._by_token.get(token)
            if existing is not None and existing.identity_id != identity.identity_id:
                raise IdentityContractError(
                    f"身份 token={token} 同时指向 {existing.identity_id} 与 "
                    f"{identity.identity_id}"
                )
        self._identities[identity.identity_id] = identity
        for token in tokens:
            if token:
                self._by_token[token] = identity

    def _identity_linked_to_voice(self, speaker_id: str) -> ResolvedIdentity | None:
        explicit = [
            identity
            for identity in self._identities.values()
            if speaker_id in identity.voice_ids
        ]
        if len(explicit) > 1:
            raise IdentityContractError(
                f"voice_id={speaker_id} 同时绑定多个身份"
            )
        if explicit:
            return explicit[0]
        candidate = self._by_token.get(speaker_id)
        if candidate is not None and candidate.source == "bible":
            return candidate
        return None

    def _register_voice_aliases(
        self,
        identity: ResolvedIdentity,
        speaker_id: str,
    ) -> None:
        for token in {
            speaker_id, identity.identity_id, identity.display_name, *identity.voice_ids,
        }:
            existing = self._voice_tokens.get(token)
            if existing is not None and existing.identity_id != identity.identity_id:
                raise IdentityContractError(
                    f"声音 token={token} 同时指向多个身份"
                )
            self._voice_tokens[token] = identity
        if speaker_id not in identity.voice_ids:
            updated = replace(
                identity,
                voice_ids=tuple(dict.fromkeys((*identity.voice_ids, speaker_id))),
            )
            self._identities[identity.identity_id] = updated
            for token, registered in list(self._by_token.items()):
                if registered.identity_id == identity.identity_id:
                    self._by_token[token] = updated
            for token, registered in list(self._voice_tokens.items()):
                if registered.identity_id == identity.identity_id:
                    self._voice_tokens[token] = updated

    def resolve(self, token: str, *, usage: IdentityUsage = "reference") -> ResolvedIdentity:
        value = _clean(token)
        identity = (
            self._voice_tokens.get(value)
            if usage == "voice"
            else self._by_token.get(value)
        )
        if identity is None:
            alias_key = _semantic_alias_key(value)
            semantic_matches = [
                candidate
                for identity_id, rationale in self._rationale_by_identity.items()
                for candidate in [self._identities[identity_id]]
                if (
                    len(alias_key) >= 2
                    and alias_key in _semantic_alias_key(rationale)
                    and (
                        usage != "voice"
                        or bool(candidate.voice_ids)
                    )
                )
            ]
            if len(semantic_matches) == 1:
                identity = semantic_matches[0]
        if identity is None:
            if usage == "voice":
                raise IdentityContractError(
                    f"声音身份「{value}」未在 voice_bible + identity contract 中声明"
                )
            raise IdentityContractError(
                f"身份「{value}」未在 Bible 或 narrative identity contract 中声明"
            )
        if usage == "visual":
            identity.visual_anchor()
        return identity

    def visual_anchor(self, token: str) -> str:
        return self.resolve(token, usage="visual").visual_anchor()


def narrative_identity_resolver(
    bible: Bible,
    screenplay: EpisodeScreenplay,
) -> NarrativeIdentityResolver:
    """Build the sole identity-policy resolver for a narrative screenplay."""
    return NarrativeIdentityResolver(bible, screenplay)


def identity_ids_in_authority_text(
    screenplay: EpisodeScreenplay,
    value: object,
    *,
    bible: Bible | None = None,
    strip_dialogue: bool = False,
) -> set[str]:
    """Resolve exact authority tokens from open-vocabulary relation prose.

    Tokens come only from the current typed identity registry.  Longest tokens
    consume their matched surface first so one display name cannot also trigger
    a shorter identity whose name happens to be its suffix.
    """
    plan = screenplay.narrative_plan
    if plan is None:
        return set()
    text = str(value or "")
    if strip_dialogue:
        for pattern in (
            r"「[^」]*」", r"“[^”]*”", r"『[^』]*』", r'"[^"]*"',
        ):
            text = re.sub(pattern, "", text)
    identities: list[object]
    if bible is not None:
        identities = list(
            narrative_identity_resolver(bible, screenplay).identities
        )
    else:
        identities = list(plan.identity_contracts)
    token_owners = sorted(
        {
            (str(token), str(identity.identity_id))
            for identity in identities
            for token in (
                identity.identity_id,
                identity.display_name,
                *identity.voice_ids,
            )
            if str(token or "").strip()
        },
        key=lambda item: (-len(item[0]), item[0], item[1]),
    )
    matched: set[str] = set()
    unmatched = text
    for token, identity_id in token_owners:
        if token not in unmatched:
            continue
        matched.add(identity_id)
        unmatched = unmatched.replace(token, "")
    return matched


def storyboard_action_relation_ids(
    screenplay: EpisodeScreenplay,
    event_id: str,
    action: Any,
    *,
    bible: Bible | None = None,
) -> tuple[list[str], list[str]]:
    """Return immutable graph relations with a legacy text-backed projection.

    Modern events own explicit onscreen identities and keep their typed action
    relation unchanged.  For older events without that field, quoted dialogue
    is excluded and only exact current-registry tokens in action/state prose may
    narrow a relation.  An empty projection preserves the typed relation rather
    than guessing from pronouns.
    """
    actor_ids = list(getattr(action, "actor_ids", None) or [])
    target_ids = list(getattr(action, "target_ids", None) or [])
    plan = screenplay.narrative_plan
    if plan is None:
        return actor_ids, target_ids
    event = next(
        (
            candidate
            for candidate in plan.events
            if candidate.event_id == str(event_id or "").strip()
        ),
        None,
    )
    if event is None or event.onscreen_entity_ids:
        return actor_ids, target_ids
    relation_ids = identity_ids_in_authority_text(
        screenplay,
        "\n".join((
            str(getattr(action, "semantic_intent", "") or ""),
            str(getattr(action, "completion_condition", "") or ""),
            *(
                str(getattr(phase, "start_condition", "") or "")
                for phase in (getattr(action, "temporal_phases", None) or [])
            ),
            *(
                str(getattr(phase, "end_condition", "") or "")
                for phase in (getattr(action, "temporal_phases", None) or [])
            ),
        )),
        bible=bible,
        strip_dialogue=True,
    )
    projected_actors = [
        identity_id for identity_id in actor_ids if identity_id in relation_ids
    ]
    projected_targets = [
        identity_id for identity_id in target_ids if identity_id in relation_ids
    ]
    return (
        projected_actors or actor_ids,
        projected_targets or target_ids,
    )


def _longest_shared_authority_surface(left: str, right: str) -> str:
    """Return the longest exact alphanumeric surface shared by two texts."""
    left_key = _semantic_alias_key(left)
    right_key = _semantic_alias_key(right)
    if not left_key or not right_key:
        return ""
    previous = [0] * (len(right_key) + 1)
    best_length = 0
    best_end = 0
    for left_index, left_char in enumerate(left_key, start=1):
        current = [0] * (len(right_key) + 1)
        for right_index, right_char in enumerate(right_key, start=1):
            if left_char != right_char:
                continue
            current[right_index] = previous[right_index - 1] + 1
            if current[right_index] > best_length:
                best_length = current[right_index]
                best_end = left_index
        previous = current
    return left_key[best_end - best_length:best_end]


def storyboard_visual_identity_relation(
    shot: Shot,
    allowed_entity_ids: list[str],
    bible: Bible,
    screenplay: EpisodeScreenplay,
) -> dict[str, object]:
    """Resolve a shot's visual cast against its exact narrative relation.

    The comparison is wholly contract-driven.  It does not classify identities
    from names, titles, occupations, or story vocabulary.
    """
    resolver = narrative_identity_resolver(bible, screenplay)
    allowed = []
    allowed_ids: set[str] = set()
    for token in allowed_entity_ids:
        try:
            identity = resolver.resolve(token, usage="visual")
        except IdentityContractError:
            continue
        if identity.identity_id not in allowed_ids:
            allowed.append(identity)
            allowed_ids.add(identity.identity_id)

    visual_tokens = [
        *(
            str(value or "").strip()
            for value in (shot.characters or [])
            if str(value or "").strip()
        ),
        *(
            str(value or "").strip()
            for value in (shot.characters_visible or [])
            if str(value or "").strip()
        ),
    ]
    for role in shot.reference_roles or []:
        prefix, separator, value = str(role or "").partition(":")
        if separator and prefix in {"character_identity", "collective_group"}:
            visual_tokens.append(value.strip())

    unexpected: dict[str, str] = {}
    for token in dict.fromkeys(visual_tokens):
        try:
            identity = resolver.resolve(token, usage="visual")
        except IdentityContractError:
            continue
        if identity.identity_id not in allowed_ids:
            unexpected[identity.identity_id] = identity.display_name

    visual_text = "\n".join((
        str(shot.action_desc or ""),
        str(shot.first_frame_desc or ""),
        str(shot.last_frame_desc or ""),
    ))
    unmatched_visual_text = visual_text
    for token in sorted(
        {
            token
            for identity in allowed
            for token in (identity.identity_id, identity.display_name)
            if token
        },
        key=len,
        reverse=True,
    ):
        unmatched_visual_text = unmatched_visual_text.replace(token, "")
    for identity in resolver.identities:
        if not identity.can_be_visible or identity.identity_id in allowed_ids:
            continue
        if any(
            token and token in unmatched_visual_text
            for token in (identity.identity_id, identity.display_name)
        ):
            unexpected[identity.identity_id] = identity.display_name

    # A model may describe a contracted identity by its canonical appearance
    # instead of repeating its display name.  Match only an exact, sufficiently
    # long appearance surface that is unique across the current typed identity
    # registry.  The evidence is generated from authority data at runtime; no
    # name, title, role, costume, or story-vocabulary list is maintained here.
    visible_identities = [
        identity for identity in resolver.identities
        if (
            identity.can_be_visible
            and not identity.is_collective
            and identity.visual_canonical.strip()
        )
    ]
    authority_anchors = {
        identity.identity_id: _semantic_alias_key(identity.visual_canonical)
        for identity in visible_identities
    }
    for identity in visible_identities:
        if identity.identity_id in allowed_ids or identity.identity_id in unexpected:
            continue
        surface = _longest_shared_authority_surface(
            visual_text,
            identity.visual_canonical,
        )
        if len(surface) < 5 or surface.isdigit():
            continue
        surface_owner_ids = {
            owner_id
            for owner_id, owner_anchor in authority_anchors.items()
            if surface in owner_anchor
        }
        # Multiple contracts may intentionally share one group appearance.
        # The surface is still decisive when every matching contract is outside
        # this shot.  If even one allowed identity shares it, attribution is
        # genuinely ambiguous and deterministic projection leaves it alone.
        if surface_owner_ids & allowed_ids:
            continue
        unexpected[identity.identity_id] = identity.display_name

    return {
        "allowed_identity_ids": sorted(allowed_ids),
        "allowed_display_names": list(dict.fromkeys(
            identity.display_name for identity in allowed
        )),
        "unexpected_identity_ids": sorted(unexpected),
        "unexpected_display_names": list(dict.fromkeys(unexpected.values())),
    }


def canonicalize_storyboard_operational_identities(
    board: Storyboard,
    bible: Bible,
    screenplay: EpisodeScreenplay,
) -> list[dict[str, object]]:
    """Project internal narrative IDs to downstream display/voice identities.

    Narrative graph fields keep their immutable internal IDs. Only fields
    consumed as visible people, speakers or continuity state are rewritten.
    """
    if screenplay.narrative_plan is None:
        return []
    resolver = narrative_identity_resolver(bible, screenplay)
    token_map: dict[str, str] = {}
    for identity in resolver.identities:
        for token in (
            identity.identity_id,
            identity.display_name,
            *identity.voice_ids,
        ):
            if token:
                token_map[token] = identity.display_name

    line_speakers: dict[str, str] = {}
    for chain in screenplay.dialogue_chains or []:
        for turn in chain.turns or []:
            line = _clean(turn.line)
            speaker = _clean(turn.speaker)
            if line and speaker:
                line_speakers[line] = speaker
    for item in screenplay.key_lines or []:
        speaker, separator, line = str(item).partition("：")
        if not separator:
            speaker, separator, line = str(item).partition(":")
        if separator and _clean(speaker) and _clean(line):
            line_speakers.setdefault(_clean(line), _clean(speaker))

    # Dialogue text is immutable screenplay authority and provides an exact
    # bridge for persistent characters whose narrative graph uses opaque IDs.
    for shot in board.shots:
        for dialogue in shot.dialogues or []:
            canonical_speaker = line_speakers.get(_clean(dialogue.line))
            if canonical_speaker:
                token_map[_clean(dialogue.speaker)] = canonical_speaker
        for item in shot.audio_timeline or []:
            canonical_speaker = line_speakers.get(_clean(item.text))
            if canonical_speaker and _clean(item.speaker_id):
                token_map[_clean(item.speaker_id)] = canonical_speaker
        actual_speakers = list(dict.fromkeys(
            speaker
            for speaker in (
                *(
                    line_speakers.get(_clean(dialogue.line), "")
                    for dialogue in (shot.dialogues or [])
                ),
                *(
                    line_speakers.get(_clean(item.text), "")
                    for item in (shot.audio_timeline or [])
                    if item.speaker_id
                ),
            )
            if speaker
        ))
        for internal, display in zip(shot.audio_cast, actual_speakers):
            token_map[_clean(internal)] = display

    # When the visible list and business-name list are positionally complete,
    # their shared ordering is an explicit shot contract rather than inference.
    for shot in board.shots:
        raw_characters = [_clean(value) for value in shot.characters]
        raw_visible = [_clean(value) for value in shot.characters_visible]
        if len(raw_characters) == len(raw_visible):
            for internal, display in zip(raw_visible, raw_characters):
                if internal and display and display in token_map:
                    token_map.setdefault(internal, token_map[display])
        elif len(raw_characters) == 1 and len(raw_visible) == 1:
            token_map.setdefault(raw_visible[0], token_map.get(
                raw_characters[0], raw_characters[0],
            ))

    changes: list[dict[str, object]] = []

    def canonical(token: object) -> str:
        value = _clean(token)
        if value in token_map:
            return token_map[value]
        try:
            return resolver.resolve(value).display_name
        except IdentityContractError:
            return value

    def replace_list(shot_no: int, field: str, values: list[str]) -> list[str]:
        replaced = [canonical(value) for value in values]
        replaced = list(dict.fromkeys(value for value in replaced if value))
        if replaced != values:
            changes.append({
                "shot_no": shot_no,
                "field": field,
                "from": list(values),
                "to": replaced,
            })
        return replaced

    for shot in board.shots:
        shot_no = int(shot.shot_no)
        shot.characters = replace_list(
            shot_no, "characters", list(shot.characters),
        )
        shot.characters_visible = replace_list(
            shot_no,
            "characters_visible",
            list(shot.characters_visible),
        )
        shot.audio_cast = replace_list(
            shot_no, "audio_cast", list(shot.audio_cast),
        )
        for dialogue in shot.dialogues or []:
            before = dialogue.speaker
            dialogue.speaker = canonical(dialogue.speaker)
            if dialogue.speaker != before:
                changes.append({
                    "shot_no": shot_no,
                    "field": "dialogues.speaker",
                    "from": before,
                    "to": dialogue.speaker,
                })
        for item in shot.audio_timeline or []:
            if not item.speaker_id:
                continue
            before = item.speaker_id
            item.speaker_id = canonical(item.speaker_id)
            if item.speaker_id != before:
                changes.append({
                    "shot_no": shot_no,
                    "field": "audio_timeline.speaker_id",
                    "from": before,
                    "to": item.speaker_id,
                })
        for state_field in ("continuity_state_in", "continuity_state_out"):
            continuity = getattr(shot, state_field, None)
            if continuity is None:
                continue
            remapped_characters = {
                canonical(token): state
                for token, state in continuity.characters.items()
            }
            if remapped_characters.keys() != continuity.characters.keys():
                changes.append({
                    "shot_no": shot_no,
                    "field": f"{state_field}.characters",
                    "from": list(continuity.characters),
                    "to": list(remapped_characters),
                })
                continuity.characters = remapped_characters
            for state in continuity.characters.values():
                state.gaze_target = canonical(state.gaze_target)
            for prop in continuity.props.values():
                prop.owner = canonical(prop.owner)
    return changes
