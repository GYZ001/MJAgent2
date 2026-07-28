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

_PRESENTATION_TOKENS = (
    "表情", "眼神", "目光", "微笑", "笑容", "神态", "气质", "妩媚", "虚荣",
    "爱慕", "轻浮", "戏谑", "清冷", "温和", "姿态", "站姿", "身姿", "动作",
    "expression", "gaze", "smile", "temperament", "pose",
)
_ANCHOR_COMPARISON_TOKENS = (
    "锚点", "设定", "要求", "未提及", "未要求", "不符合", "不一致", "实际为",
    "anchor", "brief", "requested", "not specified", "mismatch",
)
_FLEXIBLE_APPEARANCE_TOKENS = (
    "年龄", "视觉年龄", "服装", "衣着", "上衣", "长裤", "裙", "颜色", "发型",
    "发箍", "发饰", "头饰", "装饰", "莲花", "花瓣", "配饰",
    "age", "outfit", "clothing", "dress", "color", "hair", "accessory", "decoration",
)
_STABLE_TOKENS = (
    "年龄", "性别", "身份", "脸", "五官", "发型", "发色", "服装", "衣着", "体型",
    "材质", "透明", "半透明", "魂体", "幽灵", "非实体", "悬浮", "漂浮", "道具",
    "空间关系", "多余人物", "肢体", "畸形", "崩坏", "全身", "缺失", "裁切", "水印",
    "文字", "logo", "age", "gender", "identity", "face", "hair", "outfit", "body",
    "transparent", "floating", "prop", "deform", "watermark", "text",
)
_DETERMINISTIC_HARD_TOKENS = (
    "性别不符", "身份错误", "错误角色", "不是同一角色", "明显换人",
    "多余人物", "额外人物", "肢体畸形", "五官崩坏", "全身未完整", "主体不完整",
    "裁切", "水印", "文字", "logo", "未呈现透明", "未呈现半透明", "魂体缺失",
    "未悬浮", "未漂浮", "道具缺失", "age mismatch", "wrong gender", "wrong identity",
    "extra person", "deform", "watermark", "forbidden text", "cropped",
)
_WATERMARK_TOKENS = ("watermark", "logo", "水印", "ai生成", "页面文字")
_OCCLUSION_TOKENS = (
    "occlud", "cover the subject", "遮挡", "遮住", "覆盖主体", "覆盖人物",
    "干扰主体", "影响识别",
)
_NON_OCCLUSION_TOKENS = (
    "not occlud", "non-occlud", "does not cover", "未遮挡", "不遮挡",
    "没有遮挡", "未遮住", "不影响主体", "未影响识别",
)
_MINOR_CROP_TOKENS = (
    "脚尖", "脚部", "鞋尖", "鞋底", "衣摆", "裙摆", "发梢",
    "toes", "feet", "shoe tip", "hem",
)
_CROP_SIGNAL_TOKENS = (
    "裁", "截", "未完全", "不完整", "缺失", "未展示", "不可见",
    "crop", "cut off", "missing", "not visible",
)
_MAJOR_CROP_TOKENS = (
    "半身", "腰部以上", "膝盖以上", "头部被截", "脸部被截",
    "下半身缺失", "上半身缺失", "主体大面积", "half body",
    "cropped at the waist", "cropped above the knee", "head cropped",
)


def string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value).strip()]


def unique_messages(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def presentation_only_issue(message: str) -> bool:
    lower = str(message or "").strip().lower()
    return (
        any(token in lower for token in _PRESENTATION_TOKENS)
        and not any(token in lower for token in _STABLE_TOKENS)
    )


def flexible_anchor_issue(message: str) -> bool:
    """Treat subjective text-to-image styling differences as review notes.

    A seed portrait has no prior visual identity to drift from. Differences in
    perceived age, costume details, hair accessories, or decorative motifs
    should therefore not discard an otherwise usable design.
    """
    lower = str(message or "").strip().lower()
    return (
        any(token in lower for token in _FLEXIBLE_APPEARANCE_TOKENS)
        and (
            any(token in lower for token in _ANCHOR_COMPARISON_TOKENS)
            or any(token in lower for token in ("视觉年龄", "发箍", "发饰", "头饰", "莲花", "花瓣"))
        )
    )


def non_occluding_watermark_issue(message: str) -> bool:
    """Treat a provider corner mark as a warning unless it obscures the subject."""
    lower = str(message or "").strip().lower()
    return (
        any(token in lower for token in _WATERMARK_TOKENS)
        and (
            any(token in lower for token in _NON_OCCLUSION_TOKENS)
            or not any(token in lower for token in _OCCLUSION_TOKENS)
        )
    )


def minor_crop_issue(message: str) -> bool:
    """Recognise harmless edge crops without accepting half-body portraits."""
    lower = str(message or "").strip().lower()
    return (
        any(token in lower for token in _MINOR_CROP_TOKENS)
        and any(token in lower for token in _CROP_SIGNAL_TOKENS)
        and not any(token in lower for token in _MAJOR_CROP_TOKENS)
    )


def permitted_portrait_warning(message: str) -> bool:
    return (
        presentation_only_issue(message)
        or flexible_anchor_issue(message)
        or non_occluding_watermark_issue(message)
        or minor_crop_issue(message)
    )


def split_portrait_hard_failures(messages: Any) -> tuple[list[str], list[str]]:
    """Return (real hard failures, demoted presentation/minor warnings)."""
    hard: list[str] = []
    warnings: list[str] = []
    for message in string_list(messages):
        (warnings if permitted_portrait_warning(message) else hard).append(message)
    return unique_messages(hard), unique_messages(warnings)


def deterministic_hard_issue(message: str) -> bool:
    lower = str(message or "").strip().lower()
    if permitted_portrait_warning(lower):
        return False
    return any(token in lower for token in _DETERMINISTIC_HARD_TOKENS)


def _score(value: Any) -> float | None:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def normalize_portrait_seed_qa(qa: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize one model-sheet view into an enforceable QA contract."""
    result = dict(qa or {})
    identity = _score(result.get("identity_match"))
    presentation = _score(result.get("presentation_match"))
    clean = _score(result.get("clean_frame"))
    recovered = bool(result.get("qa_recovered"))
    hard, demoted = split_portrait_hard_failures(result.get("hard_failures"))
    raw_issues = string_list(result.get("issues"))
    hard.extend(message for message in raw_issues if deterministic_hard_issue(message))
    warnings = unique_messages([
        *(message for message in raw_issues if not deterministic_hard_issue(message)),
        *string_list(result.get("soft_warnings")),
        *demoted,
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

    all_messages = [*string_list(result.get("hard_failures")), *raw_issues]
    crop_severity = str(result.get("crop_severity") or "").strip().lower()
    minor_crop = crop_severity == "minor" or any(minor_crop_issue(item) for item in all_messages)
    major_crop = crop_severity == "major" or any(
        any(token in str(item).lower() for token in _MAJOR_CROP_TOKENS)
        for item in all_messages
    )

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
    if result.get("watermark_detected") is True:
        watermark_occluding = result.get("watermark_occluding")
        reported_occlusion = any(
            any(token in str(item).lower() for token in _OCCLUSION_TOKENS)
            and not any(token in str(item).lower() for token in _NON_OCCLUSION_TOKENS)
            for item in all_messages
        )
        # An explicit structured false is authoritative. Fall back to message
        # inference only when the evaluator omitted the occlusion field.
        if watermark_occluding is True or (
            watermark_occluding is None and reported_occlusion
        ):
            hard.append("水印或 Logo 遮挡人物主体")
        else:
            warnings.append("画面存在未遮挡主体的角落水印或 Logo")
    if result.get("forbidden_text_detected") is True:
        # Some evaluators classify the provider's corner watermark as both a
        # watermark and forbidden text. A confirmed non-occluding watermark is
        # still score-only; separate captions/body text remain a hard failure.
        only_non_occluding_watermark = (
            result.get("watermark_detected") is True
            and result.get("watermark_occluding") is False
            and not string_list(result.get("hard_failures"))
            and not any(
                deterministic_hard_issue(message)
                and not non_occluding_watermark_issue(message)
                for message in raw_issues
            )
        )
        if only_non_occluding_watermark:
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
