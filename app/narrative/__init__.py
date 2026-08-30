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
call site across the repo must keep working unmodified -- every symbol
(including every plain stdlib/third-party name the original file imported at
module level, since those were reachable as ``narrative.<name>`` too) is
explicitly re-exported below using the ``name as name`` PEP 484 explicit-
re-export form, matching the precedent set by ``app/validators/__init__.py``
and ``app/screenplay_ir/__init__.py`` (``from .x import *`` is forbidden by
the ``star_import`` gate in ``app/FILE_CONVENTIONS.toml``). Add new narrative
validation logic to the concern-matching submodule, not back into this file.
"""
from __future__ import annotations

from .authority import (
    Any as Any,
    Storyboard as Storyboard,
    storyboard_authority_projection as storyboard_authority_projection,
)

from .primitives import (
    Any as Any,
    Iterable as Iterable,
    ShotContribution as ShotContribution,
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

from .plan_index import (
    Any as Any,
    EpisodeScreenplay as EpisodeScreenplay,
    NarrativeContinuityPlan as NarrativeContinuityPlan,
    NarrativeIndex as NarrativeIndex,
    _ids as _ids,
    _norm as _norm,
    action_participant_delivery_errors as action_participant_delivery_errors,
    dataclass as dataclass,
    index_narrative_plan as index_narrative_plan,
)

from .screenplay_validate import (
    EpisodeScreenplay as EpisodeScreenplay,
    Iterable as Iterable,
    index_narrative_plan as index_narrative_plan,
    system_environment_entity_id as system_environment_entity_id,
    validate_screenplay_narrative as validate_screenplay_narrative,
)

# validate_screenplay_narrative's helper phases now live in sibling
# screenplay_validate_*.py files (see screenplay_validate.py's module
# docstring for the split map); a handful of plain stdlib/third-party names
# that used to be importable as ``app.narrative.<name>`` because the
# pre-split single file happened to import them at module level moved with
# their phase. Most (Any/_anchor_ref_errors/_norm/_require_refs/etc.) are
# already re-exported above from .primitives/.plan_index/.storyboard_validate
# with the identical object, so only the two not covered anywhere else are
# re-sourced here.
from .screenplay_validate_core import is_system_environment_entity_id as is_system_environment_entity_id
from .screenplay_validate_experience_paths import config as config

from .storyboard_validate import (
    Any as Any,
    EpisodeScreenplay as EpisodeScreenplay,
    NARRATIVE_CONTRACT_VERSION as NARRATIVE_CONTRACT_VERSION,
    Storyboard as Storyboard,
    StoryboardOutline as StoryboardOutline,
    _contribution_nonempty as _contribution_nonempty,
    _declared_change_matches as _declared_change_matches,
    _norm as _norm,
    _outline_as_shots as _outline_as_shots,
    _require_refs as _require_refs,
    _state_without_identity as _state_without_identity,
    _target_state_fragment_matches as _target_state_fragment_matches,
    action_participant_delivery_errors as action_participant_delivery_errors,
    defaultdict as defaultdict,
    index_narrative_plan as index_narrative_plan,
    onscreen_text_for_capacity as onscreen_text_for_capacity,
    validate_storyboard_narrative as validate_storyboard_narrative,
    validate_storyboard_screenplay_authority as validate_storyboard_screenplay_authority,
)

from .review import (
    Any as Any,
    NarrativeReviewReport as NarrativeReviewReport,
    audience_perceptual_surface_hash as audience_perceptual_surface_hash,
    hashlib as hashlib,
    json as json,
    narrative_review_passes as narrative_review_passes,
)
