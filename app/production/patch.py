"""剧本 / 分镜领域 Patch 执行器。"""
from __future__ import annotations

import copy
from collections import OrderedDict
from threading import RLock
from typing import Any

from pydantic import BaseModel, Field

from app.db import now
from app.evidence import repository as evidence_repository
from app.errors import ArtifactNeedsRebuildError
from app.harness.types import EvidenceArtifact, Issue
from app.production.metrics import record_noop_rejected, record_patch
from app.production.policy import assert_patch_ops_allowed, FullRegenDenied
from app.production.revision import get_production_revision, update_working_artifact
from app.production.screenplay_document import (
    ScreenplayDocument,
    apply_field_patch,
    document_to_screenplay,
    rederive_projections,
    screenplay_to_document,
    split_dialogue_chain_by_scene,
    split_dialogue_turn_by_capacity,
)
from app.schemas import EpisodeScreenplay, NARRATIVE_CONTRACT_VERSION


_SCREENPLAY_ARTIFACT_MODEL_CACHE: OrderedDict[
    tuple[str, str], EpisodeScreenplay
] = OrderedDict()
_SCREENPLAY_ARTIFACT_MODEL_CACHE_SIZE = 16
_SCREENPLAY_ARTIFACT_MODEL_CACHE_LOCK = RLock()


class PatchOperation(BaseModel):
    op: str
    target: dict[str, Any] = Field(default_factory=dict)
    path: str = ""
    value: Any = None


class PatchRequest(BaseModel):
    production_revision_id: str
    expected_artifact_id: str
    expected_hash: str
    issue_set_hash: str = ""
    operations: list[PatchOperation]
    idempotency_key: str = ""
    reason: str = ""
    planner_model: str = ""
    tool_call_ids: list[str] = Field(default_factory=list)


class PatchResult(BaseModel):
    ok: bool
    before_artifact_id: str
    after_artifact_id: str | None = None
    before_hash: str = ""
    after_hash: str = ""
    touched_node_ids: list[str] = Field(default_factory=list)
    diff: dict[str, Any] = Field(default_factory=dict)
    needs_full_qa: bool = True
    error: str | None = None
    failure_kind: str = ""
    patch_artifact_id: str | None = None


def _artifact_content_hash(artifact: dict[str, Any]) -> str:
    return artifact.get("content_hash") or evidence_repository.content_hash(artifact.get("content"))


def apply_patch_operation_to_document(
    document: ScreenplayDocument,
    operation: PatchOperation,
) -> tuple[ScreenplayDocument, list[str]]:
    """Execute one operation on an isolated document using the production path."""
    if operation.op == "rederive":
        return rederive_projections(document), ["rederive"]
    if operation.op == "split_dialogue_chain_by_scene":
        chain_id = str(
            (operation.target or {}).get("chain_id")
            or (operation.target or {}).get("id")
            or ""
        )
        return split_dialogue_chain_by_scene(document, chain_id=chain_id)
    if operation.op == "split_dialogue_turn_by_capacity":
        target = operation.target or {}
        return split_dialogue_turn_by_capacity(
            document,
            chain_id=str(target.get("chain_id") or target.get("id") or ""),
            turn_index=int(target.get("turn_index") or 0),
            max_chars=int(
                (operation.value or {}).get("max_chars")
                if isinstance(operation.value, dict)
                else operation.value
            ),
        )
    if operation.op in {"replace_field", "add_field"}:
        return apply_field_patch(
            document,
            path=operation.path,
            value=operation.value,
            target=operation.target,
        )
    if operation.op == "create_node":
        return _create_node(document, operation)
    if operation.op == "delete_node":
        return _delete_node(document, operation)
    if operation.op in {"insert_node", "split_node", "move_node"}:
        return _structure_op(document, operation)
    raise FullRegenDenied(f"不支持的 op: {operation.op}")


def apply_screenplay_patch(
    request: PatchRequest,
    *,
    episode_id: str,
    run_local_validate: bool = True,
    character_resolutions: list[dict] | None = None,
) -> PatchResult:
    """在工作副本上应用剧本 Patch，成功则创建新 Artifact 并 CAS 更新 working 指针。"""
    rev = get_production_revision(request.production_revision_id)
    if rev is None:
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            error="production revision 不存在",
            failure_kind="not_found",
        )
    try:
        assert_patch_ops_allowed([op.model_dump(mode="json") for op in request.operations])
    except FullRegenDenied as exc:
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            error=str(exc),
            failure_kind="policy_denied",
        )

    before = evidence_repository.get_artifact(request.expected_artifact_id)
    if not before:
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            error="expected artifact 不存在",
            failure_kind="not_found",
        )
    before_hash = _artifact_content_hash(before)
    if request.expected_hash and before_hash != request.expected_hash:
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            before_hash=before_hash,
            error="expected_hash 不匹配（CAS 冲突）",
            failure_kind="cas_conflict",
        )
    if rev.working_artifact_id and rev.working_artifact_id != request.expected_artifact_id:
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            before_hash=before_hash,
            error="expected_artifact_id 不是当前 working 链头",
            failure_kind="cas_conflict",
        )

    content = before.get("content") or {}
    # content 可能是 EpisodeScreenplay 或 ScreenplayDocument
    try:
        if "screenplay_metadata" in content:
            doc = ScreenplayDocument.model_validate(content)
        else:
            script = EpisodeScreenplay.model_validate(content)
            doc = screenplay_to_document(script)
    except Exception as exc:  # noqa: BLE001
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            before_hash=before_hash,
            error=f"无法解析工作 Artifact: {exc}",
            failure_kind="invalid_artifact",
        )

    touched: list[str] = []
    working = doc
    for op in request.operations:
        try:
            working, nodes = apply_patch_operation_to_document(working, op)
        except FullRegenDenied as exc:
            return PatchResult(
                ok=False,
                before_artifact_id=request.expected_artifact_id,
                before_hash=before_hash,
                error=str(exc),
                failure_kind="policy_denied",
            )
        touched.extend(nodes)

    working = rederive_projections(working)
    script = document_to_screenplay(working)
    if character_resolutions:
        from app.portraits import apply_screenplay_character_resolutions

        identity_changes = apply_screenplay_character_resolutions(
            script, character_resolutions,
        )
        if identity_changes:
            touched.extend(
                f"character_identity:{item['source_label']}"
                for item in identity_changes
            )
            working = screenplay_to_document(script)
    after_content = working.model_dump(mode="json")
    after_hash = evidence_repository.content_hash(after_content)
    if after_hash == before_hash:
        record_noop_rejected(kind="screenplay", episode_id=episode_id)
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            before_hash=before_hash,
            after_hash=after_hash,
            error="no-op Patch 已拒绝",
            failure_kind="no_op",
        )

    local_issues: list[Issue] = []
    if run_local_validate:
        local_issues = _local_screenplay_schema_check(
            script, expected_scope_id=episode_id,
        )

    after_art = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_document",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate" if local_issues else "validated",
            trust_level="T1" if local_issues else "T2",
            content=after_content,
            parent_artifact_ids=[request.expected_artifact_id],
            contract_version=rev.contract_version or None,
        )
    )
    patch_payload = {
        "issue_set_hash": request.issue_set_hash,
        "before_artifact_id": request.expected_artifact_id,
        "before_hash": before_hash,
        "operations": [op.model_dump(mode="json") for op in request.operations],
        "touched_node_ids": list(dict.fromkeys(touched)),
        "dependency_closure": ["rendered_full_script_text", "key_lines", "scene_outline"],
        "after_artifact_id": after_art["id"],
        "after_hash": after_hash,
        "planner_model": request.planner_model,
        "tool_call_ids": request.tool_call_ids,
        "reason": request.reason,
        "idempotency_key": request.idempotency_key,
        "created_at": now(),
    }
    patch_art = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="artifact_patch",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content=patch_payload,
            parent_artifact_ids=list(dict.fromkeys([
                request.expected_artifact_id,
                after_art["id"],
            ])),
            contract_version=rev.contract_version or None,
        )
    )
    try:
        update_working_artifact(
            request.production_revision_id,
            after_art["id"],
            expected_hash=before_hash,
        )
    except RuntimeError as exc:
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            before_hash=before_hash,
            error=str(exc),
            failure_kind="cas_conflict",
        )

    record_patch(
        kind="screenplay",
        episode_id=episode_id,
        revision_id=request.production_revision_id,
        touched=len(set(touched)),
    )
    return PatchResult(
        ok=True,
        before_artifact_id=request.expected_artifact_id,
        after_artifact_id=after_art["id"],
        before_hash=before_hash,
        after_hash=after_hash,
        touched_node_ids=list(dict.fromkeys(n for n in touched if n)),
        diff={
            "touched_node_ids": list(dict.fromkeys(touched)),
            "operations": [op.op for op in request.operations],
            "local_issue_count": len(local_issues),
        },
        needs_full_qa=True,
        patch_artifact_id=patch_art["id"],
    )


def _raw_narrative_plan(content: object) -> dict[str, Any] | None:
    if not isinstance(content, dict):
        return None
    projection = content.get("_projection")
    screenplay_payload = projection if isinstance(projection, dict) else content
    plan = screenplay_payload.get("narrative_plan")
    return plan if isinstance(plan, dict) else None


def _raw_screenplay_payload(content: object) -> dict[str, Any] | None:
    if not isinstance(content, dict):
        return None
    projection = content.get("_projection")
    return projection if isinstance(projection, dict) else content


def _current_ir_semantic_gaps(art: dict[str, Any]) -> list[str]:
    from app.screenplay_ir import (
        IR_VERSION,
        screenplay_ir_missing_event_semantic_paths,
    )
    from app.screenplay_scene_shards import SCREENPLAY_MERGED_IR_VERSION

    pending = [
        str(parent_id)
        for parent_id in art.get("parent_artifact_ids") or []
        if str(parent_id)
    ]
    seen: set[str] = set()
    gaps: list[str] = []
    while pending:
        artifact_id = pending.pop(0)
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        parent = evidence_repository.get_artifact(artifact_id)
        if parent is None:
            continue
        pending.extend(
            str(parent_id)
            for parent_id in parent.get("parent_artifact_ids") or []
            if str(parent_id) and str(parent_id) not in seen
        )
        if parent.get("type") != "screenplay_generation_ir_merged":
            continue
        content = parent.get("content")
        if str(parent.get("contract_version") or "") != SCREENPLAY_MERGED_IR_VERSION:
            gaps.append(f"{artifact_id}:contract_version")
            continue
        if not isinstance(content, dict):
            gaps.append(f"{artifact_id}:content")
            continue
        if str(content.get("format_version") or "") != IR_VERSION:
            gaps.append(f"{artifact_id}:format_version")
            continue
        gaps.extend(
            f"{artifact_id}:{path}"
            for path in screenplay_ir_missing_event_semantic_paths(content)
        )
    return gaps


def _assert_screenplay_artifact_contract(
    art: dict[str, Any],
    content: object,
) -> None:
    plan = _raw_narrative_plan(content)
    if plan is None:
        return
    missing = [
        f"narrative_plan.atomic_actions[{index}].participant_deliveries"
        for index, action in enumerate(plan.get("atomic_actions") or [])
        if isinstance(action, dict) and "participant_deliveries" not in action
    ]
    semantic_fields = (
        "narrative_layer",
        "event_priority",
        "render_policy",
    )
    missing.extend(
        f"narrative_plan.events[{index}].{field}"
        for index, event in enumerate(plan.get("events") or [])
        if isinstance(event, dict)
        for field in semantic_fields
        if field not in event
    )
    screenplay_payload = _raw_screenplay_payload(content) or {}
    missing.extend(
        f"source_coverage[{index}].{field}"
        for index, coverage in enumerate(
            screenplay_payload.get("source_coverage") or []
        )
        if isinstance(coverage, dict)
        for field in ("disposition", "projection_policy")
        if field not in coverage
    )
    missing.extend(_current_ir_semantic_gaps(art))
    if missing:
        raise ArtifactNeedsRebuildError(
            artifact_id=str(art.get("id") or ""),
            artifact_type=str(art.get("type") or "screenplay_document"),
            reason="缺少当前合同显式结构字段 " + "、".join(missing[:10]),
        )
    invalid_agencies: list[str] = []
    for index, action in enumerate(plan.get("atomic_actions") or []):
        if not isinstance(action, dict):
            continue
        agency = action.get("action_agency")
        if not isinstance(agency, dict):
            continue
        has_relation = bool(
            action.get("actor_ids") or action.get("target_ids")
        )
        identity_bearing = bool(agency.get("identity_bearing"))
        agency_kind = str(agency.get("kind") or "").strip()
        character_agency = (
            agency_kind == "character"
            or agency_kind.startswith("character_")
        )
        if (
            identity_bearing != has_relation
            or character_agency and not has_relation
        ):
            invalid_agencies.append(
                str(action.get("action_id") or f"atomic_actions[{index}]")
            )
    if invalid_agencies:
        raise ArtifactNeedsRebuildError(
            artifact_id=str(art.get("id") or ""),
            artifact_type=str(art.get("type") or "screenplay_document"),
            reason=(
                "action agency 与 actor/target 结构关系不一致："
                + "、".join(invalid_agencies[:20])
            ),
        )
    contract_version = str(plan.get("contract_version") or "")
    if contract_version != NARRATIVE_CONTRACT_VERSION:
        raise ArtifactNeedsRebuildError(
            artifact_id=str(art.get("id") or ""),
            artifact_type=str(art.get("type") or "screenplay_document"),
            reason=(
                f"叙事合同为 {contract_version or 'missing'}，"
                f"当前要求 {NARRATIVE_CONTRACT_VERSION}"
            ),
        )


def screenplay_from_artifact_record(art: dict[str, Any]) -> EpisodeScreenplay:
    """Validate an immutable Artifact once and isolate every mutable reader.

    ``EpisodeScreenplay`` is a mutable Pydantic model.  Returning the cached
    instance directly lets any downstream normalization contaminate the
    process-wide authority template, so a later resolver can report drift even
    though neither the Artifact nor the persisted page projection changed.
    """
    artifact_id = str(art.get("id") or "")
    content = art.get("content") or {}
    _assert_screenplay_artifact_contract(art, content)
    content_fingerprint = evidence_repository.content_hash(content)
    cache_key = (artifact_id, content_fingerprint)
    with _SCREENPLAY_ARTIFACT_MODEL_CACHE_LOCK:
        cached = _SCREENPLAY_ARTIFACT_MODEL_CACHE.get(cache_key)
        if cached is not None:
            _SCREENPLAY_ARTIFACT_MODEL_CACHE.move_to_end(cache_key)
    if cached is not None:
        return cached.model_copy(deep=True)
    if "_projection" in content:
        screenplay = EpisodeScreenplay.model_validate(content["_projection"])
    elif "screenplay_metadata" in content:
        screenplay = document_to_screenplay(ScreenplayDocument.model_validate(content))
    else:
        screenplay = EpisodeScreenplay.model_validate(content)
    with _SCREENPLAY_ARTIFACT_MODEL_CACHE_LOCK:
        _SCREENPLAY_ARTIFACT_MODEL_CACHE[cache_key] = screenplay
        _SCREENPLAY_ARTIFACT_MODEL_CACHE.move_to_end(cache_key)
        while len(_SCREENPLAY_ARTIFACT_MODEL_CACHE) > _SCREENPLAY_ARTIFACT_MODEL_CACHE_SIZE:
            _SCREENPLAY_ARTIFACT_MODEL_CACHE.popitem(last=False)
    return screenplay.model_copy(deep=True)


def load_screenplay_from_artifact(artifact_id: str) -> EpisodeScreenplay:
    art = evidence_repository.get_artifact(artifact_id)
    if not art:
        raise ValueError(f"artifact 不存在: {artifact_id}")
    try:
        return screenplay_from_artifact_record(art)
    except ArtifactNeedsRebuildError as exc:
        conn = evidence_repository.get_conn()
        conn.execute(
            "UPDATE artifacts SET status='stale',stale_reason=? "
            "WHERE id=? AND status!='rejected'",
            (str(exc), artifact_id),
        )
        conn.commit()
        raise


def screenplay_artifact_payload(script: EpisodeScreenplay) -> dict[str, Any]:
    return screenplay_to_document(script).model_dump(mode="json")


def _local_screenplay_schema_check(
    script: EpisodeScreenplay,
    *,
    expected_scope_id: str | None = None,
) -> list[Issue]:
    from app.narrative import validate_screenplay_narrative
    from app.production.structured_issues import structured_issue

    issues: list[Issue] = []
    if not (script.stakes or "").strip():
        issues.append(structured_issue(
            code="DRAMATIC_CONTRACT_INCOMPLETE",
            message="stakes 不能为空",
            subject="screenplay",
            path="/stakes",
            rule_id="stakes_required",
            related_node_ids=["meta:stakes"],
            stage="screenplay",
        ))
    if script.episode_no is None:
        issues.append(structured_issue(
            code="SCHEMA_INVALID",
            message="episode_no 缺失",
            subject="screenplay",
            path="/episode_no",
            rule_id="episode_no_required",
            stage="screenplay",
        ))
    for message in validate_screenplay_narrative(
        script,
        require=True,
        expected_scope_id=expected_scope_id,
    ):
        code = "NARRATIVE_CONTRACT_INVALID"
        if message.startswith("[") and "]" in message:
            code = message[1:message.index("]")]
        issues.append(structured_issue(
            code=code,
            message=message,
            subject="screenplay",
            path="/narrative_plan",
            rule_id="narrative_graph_full_validation",
            related_node_ids=["narrative_plan"],
            repairable=True,
            must_fix=True,
            stage="screenplay",
        ))
    return issues


def _narrative_node_location(
    value: Any,
    node_id: str,
) -> tuple[dict[str, Any], list[Any] | None, int | None] | None:
    """Find any nested narrative node by its schema identity, without story rules."""
    if isinstance(value, dict):
        if any(
            key.endswith("_id") and str(candidate or "") == node_id
            for key, candidate in value.items()
        ):
            return value, None, None
        for child in value.values():
            found = _narrative_node_location(child, node_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, dict) and any(
                key.endswith("_id") and str(candidate or "") == node_id
                for key, candidate in child.items()
            ):
                return child, value, index
            found = _narrative_node_location(child, node_id)
            if found is not None:
                return found
    return None


def _create_node(doc: ScreenplayDocument, op: PatchOperation) -> tuple[ScreenplayDocument, list[str]]:
    data = copy.deepcopy(doc.model_dump(mode="json"))
    kind = (op.target or {}).get("kind") or ""
    value = op.value if isinstance(op.value, dict) else {}
    if kind == "narrative_node":
        plan = data.get("narrative_plan")
        collection = str((op.target or {}).get("collection") or "").strip()
        if not isinstance(plan, dict) or not isinstance(plan.get(collection), list):
            raise KeyError(f"narrative collection not found: {collection}")
        target_node_id = str((op.target or {}).get("id") or "").strip()
        identity_fields = [
            key
            for key, candidate in value.items()
            if (
                key.endswith("_id")
                and target_node_id
                and str(candidate or "").strip() == target_node_id
            )
        ]
        if not target_node_id or not identity_fields:
            raise KeyError(
                "new narrative node must expose target.id through a stable *_id field"
            )
        existing = _narrative_node_location(plan[collection], target_node_id)
        if existing is not None and any(
            str(existing[0].get(field) or "").strip() == target_node_id
            for field in identity_fields
        ):
            raise KeyError(f"narrative node id already exists: {target_node_id}")
        parent_id = str((op.target or {}).get("parent_id") or "").strip()
        parent_field = str((op.target or {}).get("parent_field") or "").strip()
        destination = plan[collection]
        if parent_id:
            parent_location = _narrative_node_location(plan[collection], parent_id)
            if parent_location is None:
                raise KeyError(f"narrative parent not found: {collection}/{parent_id}")
            parent = parent_location[0]
            destination = parent.get(parent_field)
            if not isinstance(destination, list):
                raise KeyError(
                    f"narrative parent field is not a list: {parent_id}/{parent_field}"
                )
        insert_at = (op.target or {}).get("to_index")
        if insert_at is None:
            destination.append(value)
        else:
            index = max(0, min(len(destination), int(insert_at)))
            destination.insert(index, value)
        return ScreenplayDocument.model_validate(data), [
            f"narrative:{collection}:{target_node_id}"
        ]
    if (
        kind in {"voice", "voice_entry", "voice_bible_entry", "voice_config"}
        or str((op.target or {}).get("collection") or "").strip() == "voice_bible"
    ):
        speaker_id = str(value.get("speaker_id") or "").strip()
        if not speaker_id:
            raise KeyError("create voice entry requires speaker_id")
        voices = data.setdefault("voice_bible", [])
        if any(
            str(voice.get("speaker_id") or "").strip() == speaker_id
            for voice in voices
            if isinstance(voice, dict)
        ):
            raise KeyError(f"voice speaker_id already exists: {speaker_id}")
        voices.append(value)
        return ScreenplayDocument.model_validate(data), [f"voice:{speaker_id}"]
    if kind in {"screenplay_scene", "scene"}:
        scenes = data.setdefault("scene_blocks", [])
        scene_no = len(scenes) + 1
        scene_id = value.get("scene_id") or f"SC{scene_no:02d}"
        node = {
            "scene_id": scene_id,
            "scene_no": scene_no,
            "scene_heading": value.get("scene_heading") or f"【场{scene_no}】",
            "story_function": value.get("story_function") or "",
            "characters": value.get("characters") or [],
            "summary": value.get("summary") or "",
            "conflict": value.get("conflict") or "",
            "turn": value.get("turn") or "",
            "source_basis": value.get("source_basis") or "",
            "action_blocks": value.get("action_blocks") or [],
            "dialogue_turns": value.get("dialogue_turns") or [],
        }
        insert_at = op.target.get("after_scene_no")
        if insert_at is not None:
            idx = max(0, min(len(scenes), int(insert_at)))
            scenes.insert(idx, node)
        else:
            scenes.append(node)
        return ScreenplayDocument.model_validate(data), [scene_id]
    if kind in {"dialogue_turn"}:
        scene_id = op.target.get("scene_id") or ""
        for block in data.get("scene_blocks") or []:
            if block.get("scene_id") == scene_id:
                turns = block.setdefault("dialogue_turns", [])
                turn_id = value.get("turn_id") or f"{value.get('chain_id', 'DCX')}-T{len(turns)+1}"
                turns.append({
                    "turn_id": turn_id,
                    "chain_id": value.get("chain_id") or "",
                    "speaker": value.get("speaker") or "",
                    "line": value.get("line") or "",
                    "function": value.get("function") or "statement",
                    "source_text": value.get("source_text") or "",
                })
                return ScreenplayDocument.model_validate(data), [turn_id, scene_id]
        raise KeyError(f"create dialogue_turn: scene {scene_id} not found")
    if kind in {"action_block", "scene_action_block"}:
        scene_id = str(op.target.get("scene_id") or "")
        action_id = str(
            value.get("action_id")
            or op.target.get("id")
            or ""
        ).strip()
        text = str(value.get("text") or "").strip()
        if not action_id or not text:
            raise KeyError("create action_block requires stable action_id and text")
        if any(
            str(action.get("action_id") or "") == action_id
            for block in data.get("scene_blocks") or []
            for action in block.get("action_blocks") or []
        ):
            raise KeyError(f"action_block id already exists: {action_id}")
        for block in data.get("scene_blocks") or []:
            if str(block.get("scene_id") or "") == scene_id:
                actions = block.setdefault("action_blocks", [])
                node = {"action_id": action_id, "text": text}
                insert_at = op.target.get("to_index")
                if insert_at is None:
                    actions.append(node)
                else:
                    index = max(0, min(len(actions), int(insert_at)))
                    actions.insert(index, node)
                return ScreenplayDocument.model_validate(data), [
                    action_id,
                    scene_id,
                ]
        raise KeyError(f"create action_block: scene {scene_id} not found")
    raise FullRegenDenied(f"不支持 create_node kind={kind}")


def _delete_node(doc: ScreenplayDocument, op: PatchOperation) -> tuple[ScreenplayDocument, list[str]]:
    data = copy.deepcopy(doc.model_dump(mode="json"))
    kind = (op.target or {}).get("kind") or ""
    node_id = str((op.target or {}).get("id") or "")
    if not node_id or node_id in {"*", "ALL", "all"}:
        raise FullRegenDenied("禁止 delete-all")
    if kind == "narrative_node":
        plan = data.get("narrative_plan")
        collection = str((op.target or {}).get("collection") or "").strip()
        if not isinstance(plan, dict) or not isinstance(plan.get(collection), list):
            raise KeyError(f"narrative collection not found: {collection}")
        location = _narrative_node_location(plan[collection], node_id)
        if location is None or location[1] is None or location[2] is None:
            raise KeyError(f"narrative node not found: {collection}/{node_id}")
        _node, parent_list, index = location
        parent_list.pop(index)
        return ScreenplayDocument.model_validate(data), [
            f"narrative:{collection}:{node_id}"
        ]
    if kind in {"screenplay_scene", "scene"}:
        before = len(data.get("scene_blocks") or [])
        data["scene_blocks"] = [
            b for b in (data.get("scene_blocks") or []) if b.get("scene_id") != node_id
        ]
        if len(data["scene_blocks"]) == before:
            raise KeyError(f"scene not found: {node_id}")
        if not data["scene_blocks"]:
            raise FullRegenDenied("禁止删除全部场景")
        return ScreenplayDocument.model_validate(data), [node_id]
    if kind == "dialogue_turn":
        for block in data.get("scene_blocks") or []:
            turns = block.get("dialogue_turns") or []
            new_turns = [t for t in turns if t.get("turn_id") != node_id]
            if len(new_turns) != len(turns):
                block["dialogue_turns"] = new_turns
                return ScreenplayDocument.model_validate(data), [node_id, block.get("scene_id") or ""]
        raise KeyError(f"turn not found: {node_id}")
    raise FullRegenDenied(f"不支持 delete_node kind={kind}")


def _structure_op(doc: ScreenplayDocument, op: PatchOperation) -> tuple[ScreenplayDocument, list[str]]:
    """insert/split/move 的最小实现。"""
    if op.op == "insert_node":
        return _create_node(doc, op)
    if op.op == "split_node":
        # 将一场拆成两场：原场保留前半，新建后半
        data = copy.deepcopy(doc.model_dump(mode="json"))
        node_id = str((op.target or {}).get("id") or "")
        blocks = data.get("scene_blocks") or []
        idx = next((i for i, b in enumerate(blocks) if b.get("scene_id") == node_id), -1)
        if idx < 0:
            raise KeyError(f"split scene not found: {node_id}")
        block = blocks[idx]
        turns = block.get("dialogue_turns") or []
        mid = max(1, len(turns) // 2) if turns else 0
        new_id = f"{node_id}B"
        new_block = copy.deepcopy(block)
        new_block["scene_id"] = new_id
        new_block["scene_heading"] = (op.value or {}).get("scene_heading") or (block.get("scene_heading") + "·续")
        if turns:
            block["dialogue_turns"] = turns[:mid]
            new_block["dialogue_turns"] = turns[mid:]
        actions = block.get("action_blocks") or []
        if len(actions) > 1:
            mid_a = len(actions) // 2
            block["action_blocks"] = actions[:mid_a]
            new_block["action_blocks"] = actions[mid_a:]
        blocks.insert(idx + 1, new_block)
        return ScreenplayDocument.model_validate(data), [node_id, new_id]
    if op.op == "move_node":
        data = copy.deepcopy(doc.model_dump(mode="json"))
        node_id = str((op.target or {}).get("id") or "")
        to_index = int((op.target or {}).get("to_index") or 0)
        if (op.target or {}).get("kind") == "narrative_node":
            plan = data.get("narrative_plan")
            collection = str((op.target or {}).get("collection") or "").strip()
            if not isinstance(plan, dict) or not isinstance(plan.get(collection), list):
                raise KeyError(f"narrative collection not found: {collection}")
            location = _narrative_node_location(plan[collection], node_id)
            if location is None or location[1] is None or location[2] is None:
                raise KeyError(f"narrative node not found: {collection}/{node_id}")
            node, parent_list, index = location
            parent_list.pop(index)
            to_index = max(0, min(len(parent_list), to_index))
            parent_list.insert(to_index, node)
            return ScreenplayDocument.model_validate(data), [
                f"narrative:{collection}:{node_id}"
            ]
        blocks = data.get("scene_blocks") or []
        idx = next((i for i, b in enumerate(blocks) if b.get("scene_id") == node_id), -1)
        if idx < 0:
            raise KeyError(f"move scene not found: {node_id}")
        block = blocks.pop(idx)
        to_index = max(0, min(len(blocks), to_index))
        blocks.insert(to_index, block)
        return ScreenplayDocument.model_validate(data), [node_id]
    raise FullRegenDenied(f"不支持的结构操作 {op.op}")
