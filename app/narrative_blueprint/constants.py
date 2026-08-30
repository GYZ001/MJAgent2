"""Blueprint contract version constants, the canonical source-unit-reference regex, and the audible-source-delivery-mode set (kept here, not with the voice/identity issue functions that are its only reader, because provider_normalize.py also reads it and must not depend on the voice-issues module)."""
from __future__ import annotations

import re


BLUEPRINT_VERSION = "screenplay-narrative-blueprint.v7"
# 1.11.0: evidence rows stop restating source_segment_ids (backend derives them
# from source_unit_keys) and stop emitting the visible rows a resolved
# state_subject already implies.  Both are deterministic locally, so buying them
# from the provider was pure output cost and an extra failure surface.
BLUEPRINT_PROMPT_VERSION = "screenplay-blueprint-1.11.0"
BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE = 8
# Provider-facing Blueprint shards are deliberately smaller than the final
# scene/node ownership limit.  A production 28-SRC shard exhausted 10K output
# tokens before closing its JSON object; 14 sequential SRCs leaves enough
# bounded headroom for the full typed node contract without accepting a
# truncated prefix.
BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD = 14
BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD = 18
BLUEPRINT_SHARD_POLICY_VERSION = "blueprint-shard-policy.v8"
BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION = (
    "blueprint-shard-local-authority.v11"
)
BLUEPRINT_SPLIT_MANIFEST_VERSION = "blueprint-split-manifest.v1"
STATE_SUBJECT_ADJUDICATION_VERSION = (
    "blueprint-state-subject-adjudication.v1"
)
_CANONICAL_SOURCE_UNIT_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_])SRC\d{4}:unit:\d{3}(?![A-Za-z0-9_])"
)


AUDIBLE_SOURCE_DELIVERY_MODES = {
    "spoken_dialogue",
    "offscreen_voice",
}
