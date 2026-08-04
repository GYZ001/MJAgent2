"""Single typed identity authority for narrative downstream consumers.

The screenplay's identity contracts decide rendering and asset behaviour.  The
resolver performs exact ID/alias joins only; it never infers policy from a
person's name, title, gender, occupation, or story genre.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from app.schemas import Bible, EpisodeScreenplay, NarrativeIdentityContract


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
                if (
                    contract.visual_policy == "canonical"
                    and _clean(contract.visual_canonical)
                    != _clean(character.appearance_canonical)
                ):
                    raise IdentityContractError(
                        f"身份合同 {identity_id} 的 canonical 外观与 Bible 不一致"
                    )
            resolved = ResolvedIdentity(
                identity_id=identity_id,
                display_name=display_name,
                asset_name=bible_name or display_name,
                kind=_clean(contract.kind),
                visual_policy=contract.visual_policy,
                visual_canonical=_clean(contract.visual_canonical),
                asset_requirement=contract.asset_requirement,
                voice_ids=tuple(_clean(value) for value in contract.voice_ids),
                source="narrative_contract",
            )
            self._register(resolved)

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
