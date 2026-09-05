"""AI shot-plan candidate normalization and reference-image-only scene boundary
enforcement.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale).
"""
from __future__ import annotations

from typing import Any

from .models import AssetSource, ShotVideoGenerationPlan, VideoGenerationMode
from .prev_frame_reference import prev_frame_reference_enabled
from .primitives import _json, _row_value


#: 依赖枚举的同义写法（2026-09-03 我欲封天第 6 集：模型写了 end_only，契约里没有这个值，
#: 整集被打回）。end_only 说的是「本镜的结尾要被下一镜接上」，那是下一镜的 start_only，
#: 对本镜而言没有对前一镜的依赖，归一为 none。
_DEPENDENCY_ALIASES: dict[str, str] = {"end_only": "none", "start": "start_only", "both": "start_and_end"}

_RELATION_ALIASES: dict[str, dict[str, str]] = {
    "temporal": {
        "episode_start": "new_domain",
        "continuous": "same_moment",
        "time_skip_brief": "elapsed",
    },
    "spatial": {
        "establishing": "new_space",
        "same_scene_reposition": "same_space",
        "scene_change": "new_space",
        "same_scene_reverse_angle": "same_space",
        "same_scene": "same_space",
    },
    "edit": {
        "none": "unknown",
        "same_scene_cut": "angle_cut",
        "scene_change": "scene_cut",
    },
    "action": {
        "origin": "starts_new_action",
        "new_action_same_scene": "starts_new_action",
        "new_action_with_trajectory": "starts_new_action",
        "new_scene_action": "starts_new_action",
        "reaction_insert": "observes_result",
        "transformative_action_phase": "starts_new_action",
        "new_scene_action_with_pose_change": "starts_new_action",
        "action_phase_state_change": "starts_new_action",
    },
}


def _literal_values(model: type, field: str) -> tuple[set[str], Any]:
    """从 pydantic 模型的 Literal 注解取合法值集合与声明默认值（取值域来自模型本身，不另抄一份）。"""
    from typing import get_args
    info = model.model_fields[field]
    return set(get_args(info.annotation)), info.default


def _coerce_dependency_literals(normalized: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    """规划器把 motion 的取值写进 state（2026-09-05 第 6 集 state_dependency='audio'）或写了枚举外的词时，
    先按「值合法于兄弟字段且兄弟字段为空」挪过去，否则回落到模型声明的默认值；整份计划不再因一个
    字段串枚举而 AI_PLAN_SCHEMA_INVALID。取值域从 PlannerShotAnalysis 的 Literal 注解读，不手抄名单。"""
    from .models import PlannerShotAnalysis
    pair = ("state_dependency", "motion_dependency")
    allowed = {f: _literal_values(PlannerShotAnalysis, f) for f in pair}
    for field, sibling in (pair, pair[::-1]):
        current = str(normalized.get(field) or "")
        values, default = allowed[field]
        if not current or current in values:
            continue
        sib_values, sib_default = allowed[sibling]
        if current in sib_values and str(normalized.get(sibling) or sib_default) == sib_default:
            normalized[sibling] = current
            changes.append({"field": sibling, "from": normalized.get(sibling, ""), "to": current, "reason": f"moved_from_{field}"})
        normalized[field] = default
        changes.append({"field": field, "from": current, "to": default, "reason": "unknown_value"})


def _coerce_relation_literals(relations: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    """relations 四个维度里既不是合法值也不是已知同义词的，回落到 ShotRelations 声明的默认值。"""
    from .models import ShotRelations
    for field, aliases in _RELATION_ALIASES.items():
        current = str(relations.get(field) or "")
        values, default = _literal_values(ShotRelations, field)
        if current and current not in values and aliases.get(current) is None:
            relations[field] = default
            changes.append({"field": f"relations.{field}", "from": current, "to": default, "reason": "unknown_value"})


def _prepare_candidate(value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """复制输入并先把依赖枚举的同义写法归一（记录进 changes）。"""
    normalized = dict(value)
    changes: list[dict[str, Any]] = []
    for field in ("state_dependency", "motion_dependency"):
        current = str(normalized.get(field) or "")
        replacement = _DEPENDENCY_ALIASES.get(current)
        if replacement is not None:
            normalized[field] = replacement
            changes.append({"field": field, "from": current, "to": replacement})
    _coerce_dependency_literals(normalized, changes)
    return normalized, changes


def normalize_ai_shot_plan_candidate(
    value: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Canonicalize redundant planner fields without changing its selected mode."""
    normalized, changes = _prepare_candidate(value)
    relations = normalized.get("relations")
    if isinstance(relations, dict):
        relations = dict(relations)
        normalized["relations"] = relations
        _coerce_relation_literals(relations, changes)
        for field, aliases in _RELATION_ALIASES.items():
            current = str(relations.get(field) or "")
            replacement = aliases.get(current)
            if replacement is None:
                continue
            relations[field] = replacement
            changes.append({
                "field": f"relations.{field}",
                "from": current,
                "to": replacement,
            })
        domain_changed = bool(
            relations.get("temporal") == "new_domain"
            or relations.get("spatial") == "new_space"
            or relations.get("edit") == "scene_cut"
        )
        if (
            domain_changed
            and normalized.get("mode")
            != VideoGenerationMode.REFERENCE_IMAGE_MODE.value
        ):
            previous_mode = normalized.get("mode")
            normalized.update({
                "mode": VideoGenerationMode.REFERENCE_IMAGE_MODE.value,
                "video_input_intent": None,
                "depends_on_shot_id": None,
                "required_assets": [],
                "state_dependency": "none",
                "motion_dependency": "none",
            })
            reason_codes = list(normalized.get("reason_codes") or [])
            if "SCENE_DOMAIN_CHANGED" not in reason_codes:
                reason_codes.append("SCENE_DOMAIN_CHANGED")
            normalized["reason_codes"] = reason_codes
            changes.append({
                "field": "mode",
                "from": previous_mode,
                "to": VideoGenerationMode.REFERENCE_IMAGE_MODE.value,
                "reason": "scene_domain_requires_recomposition",
            })
    if normalized.get("mode") == VideoGenerationMode.REFERENCE_IMAGE_MODE.value:
        assets = normalized.get("required_assets")
        if isinstance(assets, list):
            versioned_reference_roles = {
                "identity_reference",
                "scene_reference",
            }
            kept = [
                item
                for item in assets
                if (
                    isinstance(item, dict)
                    and item.get("role") in versioned_reference_roles
                    and item.get("source") == AssetSource.ASSET_REVISION.value
                )
            ]
            if kept != assets:
                normalized["required_assets"] = kept
                changes.append({
                    "field": "required_assets",
                    "reason": "generic_reference_resolved_at_execution",
                })
    if normalized.get("mode") in {
        VideoGenerationMode.FIRST_FRAME_MODE.value,
        VideoGenerationMode.FIRST_LAST_FRAME_MODE.value,
    }:
        assets = normalized.get("required_assets")
        first_frame = next((
            item for item in assets
            if isinstance(item, dict) and item.get("role") == "first_frame"
        ), None) if isinstance(assets, list) else None
        desired_dependency: str | None
        if (
            first_frame
            and first_frame.get("source") == AssetSource.PREVIOUS_ADOPTED_TAIL.value
            and first_frame.get("source_shot_id")
        ):
            desired_dependency = str(first_frame["source_shot_id"])
        elif (
            first_frame
            and first_frame.get("source") in {
                AssetSource.STATIC_BOUNDARY_ASSET.value,
                AssetSource.PREVIOUS_STATIC_TAIL.value,
            }
        ):
            desired_dependency = None
        else:
            desired_dependency = normalized.get("depends_on_shot_id")
        if normalized.get("depends_on_shot_id") != desired_dependency:
            changes.append({
                "field": "depends_on_shot_id",
                "from": normalized.get("depends_on_shot_id"),
                "to": desired_dependency,
                "reason": "derived_from_first_frame_source",
            })
            normalized["depends_on_shot_id"] = desired_dependency
    fallback_order = normalized.get("fallback_order")
    if fallback_order:
        normalized["fallback_order"] = []
        changes.append({
            "field": "fallback_order",
            "from": fallback_order,
            "to": [],
            "reason": "automatic_mode_fallback_disabled",
        })
    return normalized, changes


def _is_scene_entry(
    *,
    index: int,
    item: ShotVideoGenerationPlan,
    previous: ShotVideoGenerationPlan | None,
    scene_identity_by_shot_id: dict[str, str],
) -> bool:
    if index == 0 or previous is None:
        return True
    current_scene = scene_identity_by_shot_id.get(item.shot_id, "").strip()
    previous_scene = scene_identity_by_shot_id.get(previous.shot_id, "").strip()
    if current_scene and previous_scene:
        return current_scene != previous_scene
    return bool(
        item.relations.temporal == "new_domain"
        or item.relations.spatial == "new_space"
        or item.relations.edit == "scene_cut"
    )


def _scene_identity(row: Any) -> str:
    """Keep location and time separate in storage, but joint for cut continuity."""
    scene_name = str(_row_value(row, "scene_name", "") or "").strip()
    scene_time = str(_row_value(row, "scene_time", "") or "").strip()
    if not scene_name and not scene_time:
        return ""
    return _json([scene_name, scene_time])


def apply_scene_boundary_strategy(
    shots: list[ShotVideoGenerationPlan],
    *,
    scene_identity_by_shot_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Force every shot to reference-image mode; frame-chaining is retired.

    2026-08-26 (docs/STORYBOARD_PROMPT_IR_DESIGN.md, product decision frozen by
    the user): only ``REFERENCE_IMAGE_MODE`` is ever executed. This function used
    to assign ``FIRST_FRAME_MODE`` + a previous-adopted-tail dependency to shots
    that share a scene with their predecessor. That plan data was silently
    discarded at generation time -- ``app.media_exec.enqueue`` forces
    ``REFERENCE_IMAGE_MODE`` unconditionally for every episode with
    ``narrative_authority_required=False`` (verified against real generated
    ``shot_versions.image_inputs.mode`` for EP1/EP6, where the persisted plan
    said ``FIRST_FRAME_MODE`` but the actual video was generated in reference
    mode). Keeping the plan in that shape wrote a fact into ``shots.mode_plan``
    (displayed read-only to the user as "the model's decision") that never
    matched what actually happened. Scene-entry classification is kept only to
    label *why* a shot's mode is what it is in the audit trail below, not to
    branch the outcome.

    2026-09-04 试验开关 ``video_prev_frame_reference``（见 ``app.video_plan.prev_frame_reference``）：
    打开时同场戏的后续段仍是参考图模式，但挂上一段的 ``depends_on_shot_id`` 与
    ``state_dependency=start_only``——生成时等上一段视频出来、从它的三个内切镜头各抽一帧当
    普通参考图（不是首帧）。判定复用这里的 scene-entry 分类，不另造一套。
    """
    changes: list[dict[str, Any]] = []
    ordered = sorted(shots, key=lambda item: item.shot_no)
    previous: ShotVideoGenerationPlan | None = None
    scene_identities = scene_identity_by_shot_id or {}
    for index, item in enumerate(ordered):
        scene_entry = _is_scene_entry(
            index=index,
            item=item,
            previous=previous,
            scene_identity_by_shot_id=scene_identities,
        )
        previous_mode = item.mode
        item.mode = VideoGenerationMode.REFERENCE_IMAGE_MODE
        item.planned_mode = item.mode
        item.video_input_intent = None
        item.depends_on_shot_id = None
        item.state_dependency = "none"
        item.motion_dependency = "none"
        item.required_assets = [
            asset for asset in item.required_assets
            if asset.role in {
                "identity_reference",
                "scene_reference",
            }
        ]
        reason_code = (
            "FIRST_SHOT_NO_PREDECESSOR" if index == 0
            else "SCENE_ENTRY_REFERENCE_IMAGE" if scene_entry
            else "IN_SCENE_REFERENCE_IMAGE_ONLY"
        )
        if previous is not None and not scene_entry and prev_frame_reference_enabled():
            item.depends_on_shot_id = previous.shot_id
            item.state_dependency = "start_only"
            reason_code = "PREVIOUS_SEGMENT_FRAMES_REFERENCE"
        if reason_code not in item.reason_codes:
            item.reason_codes.append(reason_code)
        if previous_mode != item.mode:
            changes.append({
                "shot_id": item.shot_id,
                "field": "mode",
                "from": previous_mode.value,
                "to": item.mode.value,
                "reason": "reference_image_mode_only",
            })
        previous = item
    return changes
