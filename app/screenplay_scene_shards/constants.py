"""Module-level constants for the screenplay scene-shard pipeline: contract
version strings, semantic review/repair token and retry budgets, delivery-mode
and finding/violation vocabularies, and the undelivered-answer retry schedule.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations


SCREENPLAY_ENVELOPE_VERSION = "screenplay-envelope.v2"
SCREENPLAY_SCENE_SHARD_VERSION = "screenplay-scene-shard.v12"
SCREENPLAY_SHARD_PLAN_VERSION = "screenplay-scene-shard-plan.v6"
SCREENPLAY_SCENE_INPUT_VERSION = "screenplay-scene-input.v10"
SCREENPLAY_SCENE_CREATIVE_VERSION = "screenplay-scene-creative.v8"
SCREENPLAY_MERGED_IR_VERSION = "screenplay-generation-ir-merged.v9"
SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION = (
    "screenplay-scene-semantic-review.v15"
)
SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT = (
    "只返回一个符合用户消息内 JSON Schema 的 JSON 对象。"
    "不得返回 Markdown、解释或对象外文本。"
)
SCREENPLAY_SCENE_SEMANTIC_REVIEW_MIN_OUTPUT_TOKENS = 2048
SCREENPLAY_SCENE_SEMANTIC_REVIEW_OUTPUT_RESERVE_PERCENT = 100
SCREENPLAY_SCENE_SEMANTIC_REVIEW_CONTEXT_RESERVE_TOKENS = 1024
SCREENPLAY_SCENE_SEMANTIC_REPAIR_MIN_OUTPUT_TOKENS = 4096
SCREENPLAY_SCENE_SEMANTIC_REPAIR_ROOT_RESERVE_PERCENT = 20
SCREENPLAY_SCENE_SEMANTIC_MAX_REPAIR_ROUNDS = 3
SCREENPLAY_SCENE_SEMANTIC_INITIAL_FORMAT_RETRY_LIMIT = 2
SCREENPLAY_SCENE_SEMANTIC_POST_REPAIR_FORMAT_RETRY_LIMIT = 2
SCREENPLAY_SCENE_SEMANTIC_FINDING_MESSAGE_MAX_CHARS = 160
SCREENPLAY_SCENE_SEMANTIC_FINDING_CODES = (
    "state_subject_semantic_drift",
    "source_semantic_drift",
)
SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS = (
    "wrong_subject",
    "unsupported_action",
    "source_contradiction",
    "cross_slot_duplication",
    "environment_personification",
)
_SCREENPLAY_SCENE_SEMANTIC_OPTIONAL_UNIT_KINDS = frozenset({
    "unsupported_action",
    "environment_personification",
})
SCREENPLAY_SCENE_SHARD_MIN_OUTPUT_TOKENS = 4096
SCREENPLAY_SCENE_SHARD_MAX_OUTPUT_TOKENS = 16384
SCREENPLAY_SCENE_SHARD_SCENE_RESERVE_TOKENS = 512
SCREENPLAY_SCENE_SHARD_UNIT_RESERVE_TOKENS = 128
SCREENPLAY_SCENE_SHARD_REASONING_RESERVE_PERCENT = 20


# Measured on this pipeline: the semantic review stage fired 50 calls in one
# round, the first 14 all succeeded, then 16 were cut inside a short window, and
# afterwards they succeeded again.  A brief pause before re-issuing therefore
# beats an immediate one, which lands inside the same burst.
#
# The pause must stay short.  This retry runs *inside* the provider-slot lease
# (it has to: the lease's failure callback tears the batch down), so every
# second spent waiting is a second one of the few provider slots sits idle.  A
# 4s+16s schedule starved the pool badly enough to cancel three episodes'
# blueprint stage outright -- worse than the failure it was meant to absorb.
# Engraved characters, notices and sound effects are attributed by
# ``content_owner_key`` -- the sect that made the token owns the 「杂」 carved on
# it -- and belong to nobody's present state.  Demanding a person's
# state_subject for them asks the wrong question, and they are not ownerless
# environment either, so neither branch of the usual rule fits.  They already
# carry a typed owner, which is why this is a narrower rule and not a hole.
# Deliberately no chunk-level backoff.  Waiting out a provider burst was tried
# with an 8s/25s/60s schedule and it destabilised the whole pipeline: the
# shard tasks hold the bounded workflow/provider pools while they sleep, so
# stages that were nowhere near the review -- entire blueprint runs on other
# episodes -- ran out their own wall-clock budgets and the batch was cancelled.
# In a pipeline with shared bounded pools and per-stage time budgets, added
# latency anywhere is paid everywhere; the only affordable pause is the short
# in-lease one above.
_ATTRIBUTED_TEXT_DELIVERY_MODES = frozenset({"written_text", "sound_effect"})

SCENE_SHARD_UNDELIVERED_BACKOFF_S = (1.5, 4.0)
SCENE_SHARD_UNDELIVERED_RETRIES = len(SCENE_SHARD_UNDELIVERED_BACKOFF_S)
