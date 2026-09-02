"""Narrative continuity graph, audience-path and storyboard hard gates.

The validators in this package are intentionally relation-driven.  They never
classify a story by title, genre, object or action words.  Text is retained as
evidence for people and models; deterministic code validates provenance,
causality, perception, ownership, capacity and cross-shot hand-offs.

``app/narrative.py`` (3,435 lines / 24 top-level defs) was one file until it
was split by concern into this package: a standalone storyboard-authority
projection, generic id/reference/fragment-matching primitives shared by both
validators, narrative-plan indexing, the screenplay narrative hard gate, the
storyboard narrative hard gate (plus the authority-alignment check it calls),
and narrative-review-report pass/fail + audience payload hashing.

This file is the sole stable entry point: every existing
``from app.narrative import X`` / ``import app.narrative`` / ``narrative.X``
call site across the repo must keep working unmodified. Every symbol is
re-exported from its **true source** exactly once: symbols defined inside
this package are exported from the submodule that actually defines them;
symbols from other modules (``app.schemas``, ``app.spoken_contract``) are
imported directly from there, not borrowed via a submodule that happened to
import them too (``name as name`` PEP 484 explicit-re-export form, no
``from .x import *`` -- see the ``star_import`` gate in
``app/FILE_CONVENTIONS.toml``). ``config`` is ``app.config``, the real
shared module singleton (not a per-submodule copy of a value);
``tests/test_narrative_monkeypatch_guard.py`` names ``narrative.config`` as
the one exempted "attribute on a shared singleton" pattern, so it stays
re-exported from its true source ``app``. Plain stdlib/typing names (``Any``,
``Iterable``, ``dataclass``, ``defaultdict``, ``hashlib``, ``json``) are no
longer re-exported as package attributes -- submodule implementation
details, not this package's public API; a repo-wide grep found no
``narrative.<name>`` reads or monkeypatch targets depending on them.
Add new narrative validation logic to the concern-matching submodule, not
back into this file.
"""
from __future__ import annotations

from app import config as config
from app.schemas import (
    EpisodeScreenplay as EpisodeScreenplay,
    NARRATIVE_CONTRACT_VERSION as NARRATIVE_CONTRACT_VERSION,
    NarrativeContinuityPlan as NarrativeContinuityPlan,
    NarrativeReviewReport as NarrativeReviewReport,
    ShotContribution as ShotContribution,
    Storyboard as Storyboard,
    StoryboardOutline as StoryboardOutline,
    is_system_environment_entity_id as is_system_environment_entity_id,
    system_environment_entity_id as system_environment_entity_id,
)
from app.spoken_contract import onscreen_text_for_capacity as onscreen_text_for_capacity

from .authority import storyboard_authority_projection as storyboard_authority_projection
from .plan_index import (
    NarrativeIndex as NarrativeIndex,
    action_participant_delivery_errors as action_participant_delivery_errors,
    index_narrative_plan as index_narrative_plan,
)
from .primitives import (
    _anchor_ref_errors as _anchor_ref_errors,
    _belief_fragment_matches as _belief_fragment_matches,
    _changed_audience_state_fields as _changed_audience_state_fields,
    _contribution_nonempty as _contribution_nonempty,
    _curve_errors as _curve_errors,
    _cycle_nodes as _cycle_nodes,
    _declared_change_matches as _declared_change_matches,
    _ids as _ids,
    _json_fragment_matches as _json_fragment_matches,
    _norm as _norm,
    _require_refs as _require_refs,
    _state_without_identity as _state_without_identity,
    _target_state_fragment_matches as _target_state_fragment_matches,
    normalize_source_evidence_text as normalize_source_evidence_text,
)
from .review import (
    audience_perceptual_surface_hash as audience_perceptual_surface_hash,
    narrative_review_passes as narrative_review_passes,
)
from .screenplay_validate import validate_screenplay_narrative as validate_screenplay_narrative
from .storyboard_validate import (
    _outline_as_shots as _outline_as_shots,
    validate_storyboard_narrative as validate_storyboard_narrative,
    validate_storyboard_screenplay_authority as validate_storyboard_screenplay_authority,
)
