"""The explicit, non-closure identity_key/identity_id/display resolver used throughout compilation."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .constants import ScreenplayIRIdentityConflictError
from .contract_validation import _structural_context_authority_id
from .models_core import IRIdentity


class _IRIdentityResolver:
    """Explicit, non-closure replacement for the identity_key/identity_id/
    display_name helpers the original compiler defined as nested closures
    capturing enclosing-function locals.  Behavior is unchanged from the
    original nested functions; the state they mutated now lives on this
    object and is passed to it (and read from it) explicitly.
    """

    def __init__(
        self,
        *,
        identity_by_key: dict[str, IRIdentity],
        bible_by_name: dict[str, Any],
        episode: dict[str, Any],
        compiler_audit: list[dict[str, Any]],
    ) -> None:
        self.identity_by_key = identity_by_key
        self.episode = episode
        self.compiler_audit = compiler_audit
        self.identity_token_to_key: dict[str, str] = {
            key: key for key in identity_by_key
        }
        self.ambiguous_display_tokens: dict[str, list[str]] = {}
        self.final_identity_ids: dict[str, str] = {}
        display_token_candidates: defaultdict[str, set[str]] = defaultdict(set)
        for key, identity in identity_by_key.items():
            display_name = str(identity.display_name or "").strip()
            if display_name:
                display_token_candidates[display_name].add(key)
        for token, keys in display_token_candidates.items():
            if token in identity_by_key:
                continue
            if len(keys) == 1:
                self.identity_token_to_key[token] = next(iter(keys))
            else:
                self.ambiguous_display_tokens[token] = sorted(keys)
        for name, character in bible_by_name.items():
            if name in self.identity_token_to_key:
                continue
            identity_by_key[name] = IRIdentity(
                key=name,
                authority_id=f"bible:{name}",
                display_name=name,
                kind="bible_character",
                visual_policy="canonical",
                visual_canonical=character.appearance_canonical,
                asset_requirement="required",
                voice_canonical=character.speech_style or character.personality,
                role_type="named_character",
                rationale="角色圣经已登记的本集人物",
            )
            self.identity_token_to_key[name] = name

    def key(self, token: str) -> str:
        raw = str(token or "").strip()
        key = self.identity_token_to_key.get(raw)
        if key:
            return key
        if raw in self.ambiguous_display_tokens:
            raise ScreenplayIRIdentityConflictError(
                f"IR 身份引用未使用唯一 identity_key：{raw}",
                issues=[{
                    "identity_key": "",
                    "reason": "ambiguous_identity_reference",
                    "display_name": raw,
                    "identity_keys": self.ambiguous_display_tokens[raw],
                }],
            )
        if not raw:
            raise ValueError("IR 引用了空身份")
        if raw == "audience":
            raise ValueError("audience 是观众感知主体，不是剧中身份")
        self.identity_by_key[raw] = IRIdentity(
            key=raw,
            authority_id=_structural_context_authority_id(self.episode, raw),
            display_name=raw,
            kind="event_referenced_contextual_identity",
            visual_policy="contextual",
            visual_canonical=f"当前事件中可由场次和动作关系识别的{raw}",
            asset_requirement="optional",
            voice_canonical=f"符合{raw}当前戏剧职责的稳定普通话声线",
            role_type="functional_character",
            rationale="该身份被当前 IR 的场次、动作、作用对象或声音关系实际引用",
        )
        self.identity_token_to_key[raw] = raw
        self.compiler_audit.append({
            "path": f"identities.{raw}",
            "operation": "derive_contextual_identity",
            "reason": "identity_is_referenced_by_event_or_scene",
        })
        return raw

    def id(self, token: str) -> str:
        return self.final_identity_ids[self.key(token)]

    def display(self, token: str) -> str:
        return self.identity_by_key[self.key(token)].display_name

    def finalize_ids(
        self,
        ordered_used_keys: list[str],
        bible_by_name: dict[str, Any],
    ) -> None:
        identity_key_by_authority: dict[str, str] = {}
        for key in ordered_used_keys:
            identity = self.identity_by_key[key]
            authority_id = str(identity.authority_id or "").strip()
            if not authority_id:
                authority_id = (
                    f"bible:{identity.display_name}"
                    if identity.display_name in bible_by_name
                    else _structural_context_authority_id(self.episode, key)
                )
                identity.authority_id = authority_id
                self.compiler_audit.append({
                    "path": f"identities.{key}.authority_id",
                    "operation": "bind_stable_authority_id",
                    "to": authority_id,
                    "reason": (
                        "compiled_graph_identity_ids_must_not_depend_on_order"
                    ),
                })
            previous_key = identity_key_by_authority.get(authority_id)
            if previous_key is not None and previous_key != key:
                raise ScreenplayIRIdentityConflictError(
                    f"authority_id={authority_id} 同时绑定多个 IR identity_key",
                    issues=[{
                        "reason": "authority_bound_to_multiple_ir_identities",
                        "authority_id": authority_id,
                        "identity_keys": [previous_key, key],
                    }],
                )
            identity_key_by_authority[authority_id] = key
            self.final_identity_ids[key] = authority_id
