"""场景资产 QA 的产品政策与确定性状态归一化。

模型负责提供观察事实，是否可用由这里的硬门禁决定。任何总分都不能覆盖人物、
禁止文字、空间类型或视角语义失败；实用质量模式可将不遮挡主体的供应商角落标识
降为警告，证据不完整时仍进入待复核。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

SCENE_QA_POLICY_VERSION = "scene-practical-quality-1.1.0"
SCENE_QA_RULE_VERSION = "scene-hard-gates-1.1.0"

_PERSON_TOKENS = ("人物", "人像", "角色", "人体", "人影", "行人", "crowd", "person", "people")
_WATERMARK_TOKENS = ("水印", "logo", "watermark", "ai生成", "ai generated")
_TEXT_TOKENS = ("字幕", "角标", "叠字", "随机文字", "多余文字", "外字幕", "caption", "overlay text")


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "detected", "present", "pass", "passed", "match", "matched"}:
            return True
        if normalized in {"0", "false", "no", "not_detected", "absent", "fail", "failed", "mismatch"}:
            return False
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _issue_mentions(issues: list[str], tokens: tuple[str, ...]) -> bool:
    joined = "\n".join(issues).lower()
    return any(token.lower() in joined for token in tokens)


def normalize_scene_image_qa(qa: dict[str, Any] | None, *, environment_only: bool = True) -> dict[str, Any]:
    """把单图模型结果归一化为硬失败/警告/待复核合同。"""
    result = dict(qa or {})
    issues = _strings(result.get("issues"))
    hard = _strings(result.get("hard_failures"))
    uncertain = _strings(result.get("uncertainties"))

    person_count: int | None = None
    raw_person_count = result.get("person_count")
    try:
        if raw_person_count is not None:
            person_count = max(0, int(raw_person_count))
    except (TypeError, ValueError):
        uncertain.append("无法确认画面人物数量")
    person_detected = _bool(result.get("person_detected"))
    if person_count is not None:
        person_detected = person_count > 0
    elif person_detected is None and _issue_mentions(issues, _PERSON_TOKENS):
        person_detected = True

    watermark_detected = _bool(result.get("watermark_detected"))
    if watermark_detected is None and _issue_mentions(issues, _WATERMARK_TOKENS):
        watermark_detected = True
    forbidden_text_detected = _bool(result.get("forbidden_text_detected"))
    if forbidden_text_detected is None and _issue_mentions(issues, _TEXT_TOKENS):
        forbidden_text_detected = True
    space_type_matches = _bool(result.get("space_type_matches"))
    allowed_provider_watermark = _bool(result.get("non_occluding_provider_watermark")) is True
    provider_mark_text_only = _bool(result.get("forbidden_text_is_provider_mark")) is True

    if environment_only and person_detected is True:
        hard.append("纯环境策略下检测到人物")
    if watermark_detected is True and not allowed_provider_watermark:
        hard.append("检测到水印或 Logo")
    if forbidden_text_detected is True and not provider_mark_text_only:
        hard.append("检测到禁止的多余文字")
    if space_type_matches is False:
        hard.append("场景空间类型与锚点不符")

    # 不能确认水印状态时不得绿色；明确的非遮挡供应商标识按上方受控例外处理。
    if watermark_detected is None:
        uncertain.append("无法确认画面无水印")
    if environment_only and person_detected is None:
        uncertain.append("无法确认纯环境画面中无人")
    if forbidden_text_detected is None:
        uncertain.append("无法确认画面无禁止文字")
    if space_type_matches is None:
        uncertain.append("无法确认场景空间类型")
    if result.get("qa_recovered"):
        uncertain.append("QA 结果由非标准输出恢复，必须复核")

    hard = _dedupe(hard)
    uncertain = _dedupe(uncertain)
    soft = [*_strings(result.get("warnings")), *(item for item in issues if item not in hard)]
    status = "failed" if hard else ("unverified" if uncertain else ("warning" if soft else "passed"))
    result.update({
        "policy_version": SCENE_QA_POLICY_VERSION,
        "rule_version": SCENE_QA_RULE_VERSION,
        "person_count": person_count,
        "person_detected": person_detected,
        "watermark_detected": watermark_detected,
        "non_occluding_provider_watermark": allowed_provider_watermark,
        "forbidden_text_detected": forbidden_text_detected,
        "forbidden_text_is_provider_mark": provider_mark_text_only,
        "space_type_matches": space_type_matches,
        "hard_failures": hard,
        "warnings": _dedupe(soft),
        "uncertainties": uncertain,
        "status": status,
        "hard_gate_passed": status in {"passed", "warning"},
        "score_affects_pass": status in {"passed", "warning"},
    })
    return result


def normalize_scene_pack_qa(
    qa: dict[str, Any] | None,
    *,
    required_roles: Iterable[str],
    actual_roles: Iterable[str],
) -> dict[str, Any]:
    """归一化整包视角角色/轴线/标志物门禁。SSIM 仅保留为证据。"""
    result = dict(qa or {})
    required = list(dict.fromkeys(str(role) for role in required_roles if role))
    actual = list(dict.fromkeys(str(role) for role in actual_roles if role))
    hard = _strings(result.get("hard_failures"))
    issues = _strings(result.get("issues"))
    uncertain = _strings(result.get("uncertainties"))

    missing = [role for role in required if role not in actual]
    hard.extend(f"缺少必需视角：{role}" for role in missing)
    reported = {
        str(item.get("view_role") or ""): item
        for item in (result.get("views") or []) if isinstance(item, dict)
    }
    normalized_views: list[dict[str, Any]] = []
    for role in required:
        item = dict(reported.get(role) or {})
        item["view_role"] = role
        role_matches = _bool(item.get("view_role_matches"))
        axis_valid = _bool(item.get("camera_axis_valid"))
        landmark_valid = _bool(item.get("landmark_relation_valid"))
        coverage_valid = _bool(item.get("space_coverage_valid"))
        view_hard = _strings(item.get("hard_failures"))
        if role_matches is False:
            view_hard.append(f"{role} 未识别为期望视角角色")
        if role in {"reverse_angle", "action_zone"} and axis_valid is False:
            view_hard.append(f"{role} 相机轴线变化不合格")
        if role in {"reverse_angle", "action_zone"} and landmark_valid is False:
            view_hard.append(f"{role} 标志物方位关系不合格")
        if role == "action_zone" and coverage_valid is False:
            view_hard.append("动作区未覆盖主要动作空间")
        if role_matches is None:
            uncertain.append(f"{role} 视角角色无法确认")
        if role in {"reverse_angle", "action_zone"} and axis_valid is None:
            uncertain.append(f"{role} 相机轴线无法确认")
        if role in {"reverse_angle", "action_zone"} and landmark_valid is None:
            uncertain.append(f"{role} 标志物方位无法确认")
        if role == "action_zone" and coverage_valid is None:
            uncertain.append("动作区覆盖无法确认")
        view_hard = _dedupe(view_hard)
        hard.extend(view_hard)
        item.update({
            "view_role_matches": role_matches,
            "camera_axis_valid": axis_valid,
            "landmark_relation_valid": landmark_valid,
            "space_coverage_valid": coverage_valid,
            "hard_failures": view_hard,
            "status": "failed" if view_hard else "pending",
        })
        normalized_views.append(item)

    if result.get("qa_recovered"):
        uncertain.append("整包 QA 未完成，必须复核")
    hard = _dedupe(hard)
    uncertain = _dedupe(uncertain)
    status = "failed" if hard else ("unverified" if uncertain else ("warning" if issues else "ready"))
    for item in normalized_views:
        if item["status"] != "failed":
            item["status"] = "ready" if status in {"ready", "warning"} else "unverified"
    result.update({
        "policy_version": SCENE_QA_POLICY_VERSION,
        "rule_version": SCENE_QA_RULE_VERSION,
        "required_views": required,
        "actual_views": actual,
        "missing_required": missing,
        "views": normalized_views,
        "hard_failures": hard,
        "warnings": _dedupe(issues),
        "uncertainties": uncertain,
        "status": status,
        "hard_gate_passed": status in {"ready", "warning"},
        "score_affects_pass": status in {"ready", "warning"},
    })
    return result


def scene_asset_state(
    pack_status: str | None,
    qa: dict[str, Any] | None,
    *,
    has_image: bool,
    primary_usable: bool = False,
) -> str:
    """返回产品状态：missing/generating/passed/warning/failed/unverified。"""
    if not has_image:
        return "missing"
    if pack_status in {"generating", "qa_pending", "running"}:
        return "generating"
    gate = dict(qa or {})
    if primary_usable and (
        pack_status == "failed" or gate.get("hard_failures") or gate.get("status") == "failed"
    ):
        return "warning"
    if gate.get("hard_failures") or gate.get("status") == "failed" or pack_status == "failed":
        return "failed"
    if gate.get("status") in {"unverified", "pending", None} or pack_status in {None, "legacy_partial"}:
        return "unverified"
    if gate.get("warnings") or gate.get("issues") or gate.get("status") == "warning":
        return "warning"
    return "passed" if pack_status == "ready" and gate.get("hard_gate_passed") is True else "unverified"


def normalize_scene_prompt(*segments: str) -> str:
    """规范标点并仅移除完全重复片段，保留语义性强调。"""
    seen: set[str] = set()
    parts: list[str] = []
    for segment in segments:
        for raw in re.split(r"[。；;\n]+", str(segment or "")):
            part = re.sub(r"[，,]{2,}", "，", raw.strip(" ，,。；;"))
            key = re.sub(r"\s+", "", part).lower()
            if not part or key in seen:
                continue
            seen.add(key)
            parts.append(part)
    return "。".join(parts) + ("。" if parts else "")
