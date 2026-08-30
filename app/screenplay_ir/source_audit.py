"""The source-audit contract check: cross-validates model-declared source_audit_annotations against compiled coverage and Blueprint source semantics."""
from __future__ import annotations

from pydantic import BaseModel

from app.narrative_blueprint import BlueprintSourceAuditAnnotation, BlueprintSourceSemantics

from .constants import IR_VERSION, _AUDIT_SOURCE_SEMANTICS, _SourceAuditAnnotationIdentity, _SourceSemanticIdentity
from .contract_validation import _canonical_source_audit_annotation_identity, _canonical_source_semantic_identity
from .models_core import IRCoverageGroup


def screenplay_ir_source_audit_contract_errors(
    value: object,
    *,
    expected_source_audit_annotations: list[object] | None = None,
) -> list[str]:
    """Validate the explicit audit authority carried by the current IR."""
    if not isinstance(value, dict):
        return ["[IR_SOURCE_AUDIT_CONTRACT] payload 必须是对象"]
    if str(value.get("format_version") or IR_VERSION) != IR_VERSION:
        return []
    if "source_audit_annotations" not in value:
        return [
            "[IR_SOURCE_AUDIT_FIELD_MISSING] "
            "source_audit_annotations 必须显式提供"
        ]
    annotations = value.get("source_audit_annotations")
    if not isinstance(annotations, list):
        return [
            "[IR_SOURCE_AUDIT_INVALID] "
            "source_audit_annotations 必须是数组"
        ]

    errors: list[str] = []
    annotation_identities: list[_SourceSemanticIdentity] = []
    annotation_authority_identities: list[
        _SourceAuditAnnotationIdentity
    ] = []
    annotation_node_keys: list[str] = []
    required_annotation_fields = set(
        BlueprintSourceAuditAnnotation.model_fields
    )
    for index, annotation in enumerate(annotations):
        if isinstance(annotation, BlueprintSourceAuditAnnotation):
            annotation = annotation.model_dump(mode="json")
        if not isinstance(annotation, dict):
            errors.append(
                f"[IR_SOURCE_AUDIT_INVALID] "
                f"source_audit_annotations[{index}] 必须是对象"
            )
            continue
        missing_fields = required_annotation_fields - set(annotation)
        if missing_fields:
            errors.append(
                "[IR_SOURCE_AUDIT_FIELD_MISSING] "
                f"source_audit_annotations[{index}] 缺少显式字段："
                + "、".join(sorted(missing_fields))
            )
        node_key = str(annotation.get("node_key") or "").strip()
        source_ids = annotation.get("source_segment_ids")
        if not node_key or not isinstance(source_ids, list) or not source_ids:
            errors.append(
                "[IR_SOURCE_AUDIT_INVALID] "
                f"source_audit_annotations[{index}] 缺少节点或来源"
            )
            continue
        try:
            typed_annotation = BlueprintSourceAuditAnnotation.model_validate(
                annotation
            )
        except ValueError:
            errors.append(
                "[IR_SOURCE_AUDIT_SEMANTIC_CONFLICT] "
                f"source_audit_annotations[{index}] 违反 audit 语义合同"
            )
            continue
        annotation_node_keys.append(typed_annotation.node_key.strip())
        annotation_authority_identities.append(
            _canonical_source_audit_annotation_identity(typed_annotation)
        )
        annotation_identities.extend(
            _canonical_source_semantic_identity(
                source_id,
                _AUDIT_SOURCE_SEMANTICS,
            )
            for source_id in typed_annotation.source_segment_ids
        )

    coverage_identities: list[_SourceSemanticIdentity] = []
    for group in value.get("coverage") or []:
        if isinstance(group, BaseModel):
            group = group.model_dump(mode="json")
        if not isinstance(group, dict) or not (
            group.get("disposition") == "audit_only"
            or group.get("projection_policy") == "audit_only"
        ):
            continue
        try:
            typed_group = IRCoverageGroup.model_validate(group)
        except ValueError:
            errors.append(
                "[IR_SOURCE_AUDIT_SEMANTIC_CONFLICT] "
                "coverage 违反 audit 语义合同"
            )
            continue
        coverage_identities.extend(
            _canonical_source_semantic_identity(
                source_id,
                _AUDIT_SOURCE_SEMANTICS,
            )
            for source_id in typed_group.source_segment_ids
        )
    source_semantics = value.get("source_semantics")
    semantic_identities: list[_SourceSemanticIdentity] = []
    for source_id, semantics in (
        source_semantics.items()
        if isinstance(source_semantics, dict)
        else ()
    ):
        if not isinstance(semantics, dict) or not (
            semantics.get("disposition") == "audit_only"
            or semantics.get("projection_policy") == "audit_only"
        ):
            continue
        try:
            typed_semantics = BlueprintSourceSemantics.model_validate(
                semantics
            )
        except ValueError:
            errors.append(
                "[IR_SOURCE_AUDIT_SEMANTIC_CONFLICT] "
                f"source_semantics[{source_id}] 违反来源语义合同"
            )
            continue
        semantic_identities.append(
            _canonical_source_semantic_identity(
                source_id,
                typed_semantics,
            )
        )
    for label, identities in (
        ("annotation node", annotation_node_keys),
        ("annotation source", annotation_identities),
        ("coverage audit source", coverage_identities),
        ("semantic audit source", semantic_identities),
    ):
        if len(identities) != len(set(identities)):
            errors.append(
                f"[IR_SOURCE_AUDIT_DUPLICATE] {label} 含重复 identity"
            )
    if (
        set(annotation_identities) != set(coverage_identities)
        or set(annotation_identities) != set(semantic_identities)
    ):
        errors.append(
            "[IR_SOURCE_AUDIT_COVERAGE_MISMATCH] "
            "source_audit_annotations、coverage 与 source_semantics "
            "必须完整一致："
            f"annotations={annotation_identities}, "
            f"coverage={coverage_identities}, "
            f"semantics={semantic_identities}"
        )
    if expected_source_audit_annotations is not None:
        try:
            expected_authority_identities = [
                _canonical_source_audit_annotation_identity(annotation)
                for annotation in expected_source_audit_annotations
            ]
        except ValueError:
            errors.append(
                "[IR_SOURCE_AUDIT_AUTHORITY_INVALID] "
                "Blueprint source_audit_annotations 违反 audit 语义合同"
            )
        else:
            if sorted(annotation_authority_identities) != sorted(
                expected_authority_identities
            ):
                errors.append(
                    "[IR_SOURCE_AUDIT_AUTHORITY_MISMATCH] "
                    "source_audit_annotations 必须保留 Blueprint 的 "
                    "node/source/semantics 完整绑定："
                    f"actual={sorted(annotation_authority_identities)}, "
                    f"expected={sorted(expected_authority_identities)}"
                )
    return list(dict.fromkeys(errors))
