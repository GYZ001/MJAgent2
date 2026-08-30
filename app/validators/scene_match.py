"""场景图素材库场景名归一化匹配，以及分镜大纲/剧本的场景对齐校验
（V6 场景连续性判据里「场景身份是否一致」的部分）。

match_scene_name / canonicalize_storyboard_scene 被 app.scenes、
app.domain.storyboard_ops、app.production.prep_pack 当基础库直接调用。
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from app.scene_contract import (
    compose_scene_setting,
    scene_name_of,
    split_legacy_scene_setting,
)
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    Shot,
    Storyboard,
    StoryboardOutline,
)

from .primitives import _normalize_scene_label

def _scene_label_variants(value: str) -> list[str]:
    """返回场景匹配用 token：地点优先，同时保留完整标签供已确认别名精确命中。"""
    raw = (value or "").strip()
    variants: list[str] = []
    _, legacy_location = split_legacy_scene_setting(raw)
    location = _normalize_scene_label(legacy_location)
    if location:
        variants.append(location)
    full = _normalize_scene_label(raw)
    if full and full not in variants:
        variants.append(full)
    return variants


def _scene_contiguity_key(scene: str) -> str:
    """将历史子机位后缀收敛到规范主场景。"""
    base = re.split(r"[·・•\-—/]", (scene or "").strip(), maxsplit=1)[0]
    return _normalize_scene_label(base)


def _storyboard_scene_contiguity_key(
    shot: Shot,
    *,
    narrative_authority: bool,
) -> str:
    if narrative_authority and str(shot.scene_id or "").strip():
        return str(shot.scene_id).strip()
    return _scene_contiguity_key(scene_name_of(shot))


def match_scene_name(scene_label: str, scenes, *, allow_fuzzy: bool = True) -> str | None:
    """把手输/旧式场景候选名归一化匹配到 bible.scenes 的规范场景名。
    优先级：精确地点/别名 > 最具体的包含关系 > 可选模糊匹配。所有场景统一
    比较后再选最优，禁止由场景库顺序决定结果；最高分并列时返回 None，避免把
    一个歧义标签静默绑定到错误场景。
    """
    setting = (scene_label or "").strip()
    if not setting or not scenes:
        return None
    setting_variants = _scene_label_variants(setting)
    if not setting_variants:
        return None
    canonical_exact = {
        name
        for sc in scenes
        if (name := str(getattr(sc, "name", "") or "").strip())
        and _normalize_scene_label(name) in setting_variants
    }
    if len(canonical_exact) == 1:
        return next(iter(canonical_exact))
    if len(canonical_exact) > 1:
        return None
    containment_by_scene: dict[str, tuple[int, int, int]] = {}
    fuzzy_by_scene: dict[str, float] = {}
    for sc in scenes:
        name = (getattr(sc, "name", "") or "").strip()
        if not name:
            continue
        labels = [name, *(getattr(sc, "aliases", None) or [])]
        label_variants = list(dict.fromkeys(
            variant
            for label in labels
            if str(label or "").strip()
            for variant in _scene_label_variants(str(label))
        ))
        if not label_variants:
            continue
        best_containment: tuple[int, int, int] | None = None
        for norm_setting in setting_variants:
            for norm_label in label_variants:
                if norm_label == norm_setting:
                    rank = (3, len(norm_label), 0)
                elif norm_label in norm_setting:
                    # 候选标签完整出现在输入中；越长越具体。相同长度的复合地点
                    # （如「荒山林海至黑山外围」）按文本出现顺序取起点，不能再受
                    # 场景库数组顺序影响。
                    rank = (2, len(norm_label), -norm_setting.index(norm_label))
                elif norm_setting in norm_label:
                    # 输入只是候选标签的一部分，可信度低于上一种包含方向。
                    rank = (1, len(norm_setting), 0)
                else:
                    continue
                if best_containment is None or rank > best_containment:
                    best_containment = rank
        if best_containment is not None:
            previous = containment_by_scene.get(name)
            if previous is None or best_containment > previous:
                containment_by_scene[name] = best_containment
        fuzzy_by_scene[name] = max(
            difflib.SequenceMatcher(None, norm_label, norm_setting).ratio()
            for norm_label in label_variants
            for norm_setting in setting_variants
        )

    if containment_by_scene:
        best_rank = max(containment_by_scene.values())
        winners = [name for name, rank in containment_by_scene.items() if rank == best_rank]
        return winners[0] if len(winners) == 1 else None
    if not allow_fuzzy or not fuzzy_by_scene:
        return None
    best_score = max(fuzzy_by_scene.values())
    if best_score < 0.6:
        return None
    winners = [
        name for name, score in fuzzy_by_scene.items()
        if abs(score - best_score) < 1e-12
    ]
    return winners[0] if len(winners) == 1 else None


def canonicalize_storyboard_scene(
    target: Shot | Any,
    bible: Bible,
    *,
    prefer_explicit: bool = False,
) -> str | None:
    """解析一次模糊/旧式输入，立即回填规范 scene_name。

    新数据优先信任独立 ``scene_name``。旧数据没有 ``scene_time`` 时，
    若混合 ``scene_setting`` 可解析到更准确的场景，则用它修正历史误绑定。
    """
    scenes = getattr(bible, "scenes", None) or []
    if not scenes:
        return None
    explicit_name = str(getattr(target, "scene_name", "") or "").strip()
    explicit_time = str(getattr(target, "scene_time", "") or "").strip()
    legacy_setting = str(getattr(target, "scene_setting", "") or "").strip()
    legacy_time, legacy_name = split_legacy_scene_setting(legacy_setting)

    matched = match_scene_name(explicit_name, scenes) if explicit_name else None
    legacy_match = match_scene_name(legacy_name, scenes) if legacy_name else None
    if not prefer_explicit and not explicit_time and legacy_time and legacy_match:
        # 旧行的 scene_name 可能由过去的「最先命中」算法误绑；迁移时重算。
        matched = legacy_match
    elif not matched:
        matched = legacy_match

    if not prefer_explicit and not explicit_time and legacy_time:
        target.scene_time = legacy_time
    if not matched:
        target.scene_name = ""
        return None
    target.scene_name = matched
    target.scene_setting = compose_scene_setting(
        str(getattr(target, "scene_time", "") or ""),
        matched,
        fallback=legacy_setting,
    )
    return matched


def resolve_screenplay_scene_sequence(
    screenplay: EpisodeScreenplay | None,
    bible: Bible,
) -> list[str]:
    """Resolve screenplay scenes in order while preserving later revisits."""
    if screenplay is None:
        return []
    scenes = getattr(bible, "scenes", None) or []
    resolved: list[str] = []
    for scene in screenplay.scene_outline or []:
        name = match_scene_name(scene.scene_heading, scenes, allow_fuzzy=False)
        if name:
            resolved.append(name)
    return resolved


def _screenplay_scene_resolution_errors(
    screenplay: EpisodeScreenplay | None,
    bible: Bible,
) -> list[str]:
    if screenplay is None or not (screenplay.scene_outline or []):
        return []
    scenes = getattr(bible, "scenes", None) or []
    if not scenes:
        return ["本集剧本已有场次，但相关场景尚未完成自动建库与场景图生成"]
    errors: list[str] = []
    for scene in screenplay.scene_outline or []:
        if not match_scene_name(scene.scene_heading, scenes, allow_fuzzy=False):
            errors.append(
                f"剧本第 {scene.scene_no} 场「{scene.scene_heading}」尚未解析到规范场景；"
                "请先完成该场景的自动建库与场景图生成"
            )
    return errors


def validate_storyboard_outline_scene_alignment(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay | None,
    bible: Bible,
) -> list[str]:
    """大纲只能按剧本场次顺序使用本集场景，不能从全片场景库借错地点。"""
    errors = _screenplay_scene_resolution_errors(screenplay, bible)
    expected_sequence = resolve_screenplay_scene_sequence(screenplay, bible)
    if errors or not expected_sequence:
        return errors
    allowed = set(expected_sequence)
    used: list[str] = []
    for shot in outline.shots:
        matched = canonicalize_storyboard_scene(shot, bible)
        if not matched:
            label = shot.scene_name or shot.scene_setting
            errors.append(
                f"大纲第 {shot.shot_no} 镜场景「{label}」未命中规范场景图"
            )
            continue
        used.append(matched)
        if matched not in allowed:
            errors.append(
                f"大纲第 {shot.shot_no} 镜误用了「{matched}」；本集剧本只允许场景："
                f"{'、'.join(dict.fromkeys(expected_sequence))}"
            )
    search_from = 0
    for scene_no, expected in enumerate(expected_sequence, start=1):
        matched_index = next(
            (
                index
                for index in range(search_from, len(used))
                if used[index] == expected
            ),
            None,
        )
        if matched_index is None:
            errors.append(
                f"分镜大纲遗漏剧本第 {scene_no} 场「{expected}」，"
                "或该场未按剧本场次顺序出现"
            )
            continue
        search_from = matched_index + 1
    return errors


def validate_storyboard_screenplay_scene_alignment(
    board: Storyboard,
    screenplay: EpisodeScreenplay | None,
    bible: Bible,
) -> list[str]:
    """整集/确认门禁：拒绝任何来自本集剧本之外的场景，并检查场次顺序与覆盖。"""
    errors = _screenplay_scene_resolution_errors(screenplay, bible)
    expected_sequence = resolve_screenplay_scene_sequence(screenplay, bible)
    if errors or not expected_sequence:
        return errors
    allowed = set(expected_sequence)
    used: list[str] = []
    for shot in board.shots:
        matched = canonicalize_storyboard_scene(shot, bible)
        if matched:
            used.append(matched)
        if matched not in allowed:
            errors.append(
                f"第 {shot.shot_no} 镜 scene_name「{shot.scene_name or shot.scene_setting}」与本集剧本不一致；"
                f"只能使用：{'、'.join(dict.fromkeys(expected_sequence))}"
            )
    search_from = 0
    for scene_no, expected in enumerate(expected_sequence, start=1):
        matched_index = next(
            (
                index
                for index in range(search_from, len(used))
                if used[index] == expected
            ),
            None,
        )
        if matched_index is None:
            errors.append(
                f"整集分镜遗漏剧本第 {scene_no} 场「{expected}」，"
                "或该场未按剧本场次顺序出现"
            )
            continue
        search_from = matched_index + 1
    return errors


def validate_storyboard_scenes(board: Storyboard, bible: Bible) -> list[str]:
    """V12：每个 shot 必须归一到场景图素材库的规范 ``scene_name``。

    ``scene_time`` 不参与场景图匹配；模糊/旧式标签仅解析一次，命中后立即
    回填规范名，确保后续选图一一对应。
    务实优先：库为空（旧项目或尚未生成场景圣经）时直接放行，绝不误伤。"""
    scenes = getattr(bible, "scenes", None) or []
    if not scenes:
        return []
    errors: list[str] = []
    names = "/".join(sc.name for sc in scenes if getattr(sc, "name", ""))
    for i, shot in enumerate(board.shots):
        original_label = shot.scene_name or shot.scene_setting
        matched = canonicalize_storyboard_scene(shot, bible)
        if not matched:
            errors.append(
                f"shots[{i}](shot_no={shot.shot_no}).scene_name=「{original_label}」不在场景图素材库内；"
                f"scene_name 必须命中并归一成库内规范场景之一：{names}；"
                "若确为剧情需要的新场景，必须先完成该场景的自动建库与专属场景图，禁止借用相似旧场景")
    return errors
