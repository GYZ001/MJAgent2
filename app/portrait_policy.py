"""Deterministic policy for character model-sheet QA.

The portrait generator intentionally asks for a neutral model-sheet pose.  A
character Bible may also describe acting direction (gaze, smile, temperament),
which must not make a technically usable identity sheet fail before the other
views are generated.  This module keeps stable identity/technical defects as
hard gates and records acting-direction differences as soft warnings.
"""
from __future__ import annotations

from typing import Any, Iterable


PORTRAIT_SEED_THRESHOLD = 0.6


def string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value).strip()]


def unique_messages(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def _score(value: Any) -> float | None:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def normalize_portrait_seed_qa(qa: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize one view from typed observations; prose never controls routing."""
    result = dict(qa or {})
    identity = _score(result.get("identity_match"))
    presentation = _score(result.get("presentation_match"))
    clean = _score(result.get("clean_frame"))
    recovered = bool(result.get("qa_recovered"))
    hard: list[str] = []
    raw_issues = string_list(result.get("issues"))
    crop_severity = str(result.get("crop_severity") or "").strip().lower()
    warnings = unique_messages([
        *raw_issues,
        *string_list(result.get("hard_failures")),
        *string_list(result.get("soft_warnings")),
    ])

    if identity is None:
        identity = 0.0
        recovered = True
        hard.append("角色稳定特征评分缺失")
    if clean is None:
        clean = 0.0
        recovered = True
        hard.append("画面技术完整性评分缺失")
    if presentation is None:
        presentation = 1.0

    minor_crop = crop_severity == "minor"
    major_crop = crop_severity == "major"

    person_count = result.get("person_count")
    if isinstance(person_count, (int, float)) and int(person_count) != 1:
        hard.append(f"画面人物数量为 {int(person_count)}，要求单人物")
    if result.get("full_body_visible") is False:
        if minor_crop and not major_crop:
            warnings.append("主体边缘有轻微裁切，已按带警告可用处理")
        else:
            hard.append("主体全身未完整可见")
    if result.get("anatomy_valid") is False:
        hard.append("人物肢体或五官存在明显异常")
    if result.get("stable_identity_matches") is False:
        hard.append("结构化身份观察确认角色稳定特征不一致")
    if result.get("watermark_detected") is True:
        watermark_occluding = result.get("watermark_occluding")
        if watermark_occluding is True:
            hard.append("水印或 Logo 遮挡人物主体")
        elif watermark_occluding is False:
            warnings.append("画面存在未遮挡主体的角落水印或 Logo")
        else:
            recovered = True
            hard.append("无法确认水印是否遮挡人物主体")
    if result.get("forbidden_text_detected") is True:
        provider_mark_only = (
            result.get("watermark_detected") is True
            and result.get("watermark_occluding") is False
            and result.get("forbidden_text_is_provider_mark") is True
        )
        if provider_mark_only:
            warnings.append("角落水印含文字，但未遮挡主体，已按带提示可用处理")
        else:
            hard.append("画面检测到不允许的文字")
    if identity < PORTRAIT_SEED_THRESHOLD:
        if hard:
            hard.append(f"角色稳定特征分 {identity:.2f} 未达到 {PORTRAIT_SEED_THRESHOLD:.2f}")
        else:
            warnings.append(
                f"角色设定贴合度 {identity:.2f} 偏低，建议人工确认年龄、服装与装饰取舍"
            )
    if clean < PORTRAIT_SEED_THRESHOLD:
        hard.append(f"画面技术完整性分 {clean:.2f} 未达到 {PORTRAIT_SEED_THRESHOLD:.2f}")

    hard = unique_messages(hard)
    warnings = unique_messages(warnings)
    # A low subjective anchor score alone must not discard a clean, attractive
    # seed. Explicit identity/technical defects above remain hard gates.
    effective_identity = identity if hard else max(identity, PORTRAIT_SEED_THRESHOLD)
    result.update({
        "identity_match": identity,
        "presentation_match": presentation,
        "clean_frame": clean,
        # Only stable identity + technical cleanliness decide seed eligibility.
        "overall": round(min(effective_identity, clean), 3),
        "issues": warnings,
        "soft_warnings": warnings,
        "hard_failures": hard,
        "hard_gate_passed": not recovered and not hard,
        "qa_recovered": recovered,
        "status": "failed" if hard or recovered else ("warning" if warnings else "ready"),
    })
    return result
