"""Strict (``additionalProperties: false``, all-required) provider JSON schema
derivation used to force the semantic review/repair response format onto the
shard creative schema.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


_SCREENPLAY_SCENE_STRICT_PROVIDER_SCHEMA_KEYWORDS = frozenset({
    "$defs",
    "$ref",
    "additionalProperties",
    "enum",
    "items",
    "properties",
    "required",
    "type",
})


def _scene_shard_strict_provider_schema(
    local_schema: dict[str, Any],
) -> dict[str, Any]:
    """Keep only the strict-provider JSON Schema compatibility subset.

    The complete dynamic schema remains authoritative in the prompt and local
    validation.  Provider-side structured output is an additional generation
    constraint, so unsupported annotation/cardinality keywords must not turn a
    locally enforceable contract into an HTTP 400.
    """

    source_definitions = local_schema.get("$defs", {})
    projected_definitions: dict[str, dict[str, Any]] = {}

    def merge_schema(
        base: dict[str, Any],
        overlay: dict[str, Any],
    ) -> dict[str, Any]:
        merged = deepcopy(base)
        for keyword, value in overlay.items():
            if (
                isinstance(value, dict)
                and isinstance(merged.get(keyword), dict)
            ):
                merged[keyword] = merge_schema(merged[keyword], value)
            else:
                merged[keyword] = deepcopy(value)
        return merged

    def project_all_of(schema_node: dict[str, Any]) -> dict[str, Any]:
        all_of = schema_node.get("allOf")
        if all_of is not None:
            reference_branches = [
                branch
                for branch in all_of
                if (
                    isinstance(branch, dict)
                    and isinstance(branch.get("$ref"), str)
                )
            ]
            if len(reference_branches) != 1:
                raise ValueError(
                    "strict provider schema cannot project allOf without "
                    "one authoritative $ref"
                )
            reference = str(reference_branches[0]["$ref"])
            definition_prefix = "#/$defs/"
            if not reference.startswith(definition_prefix):
                raise ValueError(
                    "strict provider schema only projects local $defs refs"
                )
            definition_name = reference.removeprefix(definition_prefix)
            referenced_schema = source_definitions.get(definition_name)
            if not isinstance(referenced_schema, dict):
                raise ValueError(
                    "strict provider schema references unknown definition: "
                    f"{definition_name}"
                )
            merged = deepcopy(referenced_schema)
            for branch in all_of:
                if branch is reference_branches[0]:
                    continue
                if not isinstance(branch, dict):
                    raise ValueError(
                        "strict provider schema allOf branch must be an object"
                    )
                merged = merge_schema(merged, branch)
            projected = project_all_of(merged)
            digest = hashlib.sha256(
                json.dumps(
                    projected,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:16]
            projected_name = f"ScreenplaySceneStrict_{digest}"
            projected_definitions[projected_name] = projected
            return {"$ref": f"#/$defs/{projected_name}"}

        projected: dict[str, Any] = {}
        for keyword, value in schema_node.items():
            if keyword in {"$defs", "properties"} and isinstance(value, dict):
                projected[keyword] = {
                    name: project_all_of(child_schema)
                    for name, child_schema in value.items()
                }
            elif keyword == "items" and isinstance(value, dict):
                projected[keyword] = project_all_of(value)
            else:
                projected[keyword] = deepcopy(value)
        return projected

    projected_schema = project_all_of(local_schema)
    if projected_definitions:
        projected_schema.setdefault("$defs", {}).update(
            projected_definitions
        )

    def sanitize(schema_node: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for keyword, value in schema_node.items():
            if keyword == "const":
                sanitized["enum"] = [deepcopy(value)]
                continue
            if (
                keyword
                not in _SCREENPLAY_SCENE_STRICT_PROVIDER_SCHEMA_KEYWORDS
            ):
                continue
            if keyword in {"$defs", "properties"}:
                sanitized[keyword] = {
                    name: sanitize(child_schema)
                    for name, child_schema in value.items()
                }
            elif keyword == "items" and isinstance(value, dict):
                sanitized[keyword] = sanitize(value)
            else:
                sanitized[keyword] = deepcopy(value)
        properties = sanitized.get("properties")
        if isinstance(properties, dict):
            if sanitized.get("additionalProperties") is not False:
                raise ValueError(
                    "strict provider object schemas must forbid extra fields"
                )
            # Strict schemas require every declared property to be required.
            # Local defaults still own the business meaning of empty creative
            # fields such as performance and resulting_state.
            sanitized["required"] = list(properties)
        return sanitized

    return sanitize(projected_schema)
