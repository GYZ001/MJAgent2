"""分镜大纲整体校验 validate_storyboard_outline、运镜方向合同校验
（validate_storyboard_direction_contract）与可读性评分。
"""
from __future__ import annotations

from app.continuity import outline_atomic_errors
from app.renderability import shot_count_budget_errors
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    Shot,
    Storyboard,
    StoryboardOutline,
)

from .outline_capacity import outline_scene_coverage_errors
from .outline_dialogue import outline_key_line_speaker_errors
from .primitives import _too_similar
from .scene_match import validate_storyboard_outline_scene_alignment
from .screenplay_text import (
    KEY_CONTENT_MAX_REPORT,
    KEY_LINE_BIGRAM_COVERAGE,
    KEY_LINE_PRESENT_RATIO,
    KEY_POINT_COVERAGE,
    _bigram_coverage,
    _condense,
    _longest_run_ratio,
    _strip_speaker,
    key_line_order_errors,
    key_lines_in_story_order,
)
from .storyboard_delivery import (
    key_line_catalog,
    outline_key_line_capacity_errors,
)

def validate_storyboard_outline(outline: StoryboardOutline, screenplay: EpisodeScreenplay,
                                target_duration_s: int, *,
                                bible: Bible | None = None) -> list[str]:
    """校验分镜大纲：镜头数在范围内、shot_no 连续、每镜有推进、相邻镜不停留在同一节拍，
    且全集必保留关键台词/剧情点都被分配到某一镜（防止规划阶段就把剧情铺一半、后段漏戏）。

    新叙事权威路径只依据稳定 ID、动作关系和容量合同；不按 covers 中的角色名、
    动词、题材词或修辞词决定通过、改写或拆镜。
    """
    errors: list[str] = []
    shots = outline.shots
    if not shots:
        return ["分镜大纲为空；请按主线骨架规划连续镜头并覆盖 must_keep spine"]
    errors.extend(shot_count_budget_errors(len(shots), context="分镜大纲"))
    errors.extend(outline_scene_coverage_errors(
        outline,
        screenplay,
        bible,
    ))
    actual = [s.shot_no for s in shots]
    if actual != list(range(1, len(shots) + 1)):
        errors.append(f"大纲 shot_no 必须为连续递增 1..{len(shots)}，当前为 {actual}")
    for i, s in enumerate(shots):
        if len((s.beat or "").strip()) < 6:
            errors.append(f"大纲第 {i + 1} 镜 beat 过短或缺失；请用一句话写清本镜推进的剧情（谁做了什么/局势如何变化）")
    # 反停留：相邻两镜的 beat 几乎逐字相同 = 停在同一节拍上空转，必须推进到新剧情。
    for i in range(1, len(shots)):
        if _too_similar(shots[i - 1].beat, shots[i].beat):
            errors.append(
                f"大纲第 {i} 与第 {i + 1} 镜剧情几乎相同（停留在同一节拍）；"
                "每镜必须推进到新的剧情进展，禁止把同一情绪/同一句原文拆成多镜空耗时长")
        repeated_key_lines = sorted(
            set(shots[i - 1].key_line_ids or []).intersection(shots[i].key_line_ids or [])
        )
        if repeated_key_lines:
            errors.append(
                f"大纲第 {i} 与第 {i + 1} 镜重复分配关键台词 "
                f"{repeated_key_lines}；同一句台词只能在一镜完整说出，下一镜应推进到人物反应或新信息"
            )
    # 关键台词/剧情点必须在大纲里被分配到某一镜（beat 或 covers 中体现），否则后段必丢戏。
    plan_text = "".join((s.beat or "") + (s.covers or "") for s in shots)
    catalog = key_line_catalog(screenplay)
    key_lines = list(catalog.values())
    key_points = [pt.strip() for pt in (screenplay.key_plot_points or []) if pt and pt.strip()]
    # 结构化 ID 优先于散文模糊匹配（真实回归：EP6 run_9bfcd5cbe128，2026-08-25）。
    #
    # app.textmatch 模块自己的文档已经写明这一层的定位：「PRD VAL-422 §4.4.4 之后，
    # 这些函数的定位被下调……不得单独产生 must_keep missing blocker——结构化 ID 台账
    # 才是主判据」。但下面这条 missing_lines 判定此前一直违反这条约定：它只看
    # beat/covers 的散文摘要是否复述了台词字面，完全不看模型是否已经通过
    # key_line_ids 显式声明了分配。真实案例里模型把全部 17 条 KL 逐一分配进
    # key_line_ids（覆盖无遗漏、无重复、无超容量，三条硬性结构校验全部通过），
    # 只是 covers 摘要用了转述而非近似引用，散文匹配就判"未安排"，用启发式否决了
    # 已经显式声明的结构化事实。
    #
    # 放松之后谁来守"声明了但没交付"：
    #   1) 就在本函数内、无条件调用的 outline_key_line_capacity_errors 已经对
    #      "用了 key_line_ids 就必须覆盖全部 catalog" 做了硬性判定——未知 ID、
    #      重复分配、单镜超容量、任何一条 catalog KL 未被分配，都会在那里产生
    #      [OUTLINE_KEY_LINE_CAPACITY_INVALID] 错误，不依赖任何模糊匹配；
    #   2) 大纲一旦进入逐镜生成，key_line_ids 不只是"计划"：
    #      app.stages._scene_pack_dialogues 直接用 key_line_ids 从这份 catalog
    #      逐字取出 speaker/line 构造该镜真正的 Dialogue 列表——covers 散文完全
    #      不参与"是否真的说出口"，说不说得出口只取决于 key_line_ids 有没有分配；
    #   3) 人工编辑阶段还有 key_line_delivery_errors（validators.py）用同样的
    #      模糊匹配再核一遍，但比对对象是 spoken_text_of(shot)（真实口播/声轨），
    #      不是 covers 摘要——摘要写得像不像台词，从不是这句词能否播出的决定因素。
    #   因此对已使用 key_line_ids 规划的大纲，"是否安排"这件事只由 key_line_ids
    #   是否覆盖 catalog 决定；covers 散文只是给人看的摘要，不应该单独否决结构化
    #   声明。只有大纲完全没有使用 key_line_ids（旧数据/降级路径，无结构化台账可
    #   依赖）时，才退回散文模糊匹配兜底——这与该判据历史上唯一被需要的场景一致。
    uses_key_line_ids = any((s.key_line_ids or []) for s in shots)
    if uses_key_line_ids:
        assigned_kl_ids = {
            str(kid).strip().upper()
            for s in shots
            for kid in (s.key_line_ids or [])
            if str(kid).strip()
        }
        missing_lines = [
            text for kid, text in catalog.items()
            if kid not in assigned_kl_ids
        ]
    else:
        missing_lines = [
            ln for ln in key_lines
            if _longest_run_ratio(_strip_speaker(ln), plan_text) < KEY_LINE_PRESENT_RATIO
            and _bigram_coverage(_strip_speaker(ln), plan_text) < KEY_LINE_BIGRAM_COVERAGE
        ]
    if missing_lines:
        shown = "；".join(missing_lines[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_lines) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_lines) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"大纲未安排 {len(missing_lines)} 条必保留关键台词：{shown}{extra}；"
            "请把每条关键台词分配到对应镜头的 covers，确保整集都规划进去")
    errors.extend(key_line_order_errors(
        key_lines_in_story_order(key_lines, screenplay.full_script_text),
        [(s.beat or "") + (s.covers or "") for s in shots],
        subject="分镜大纲",
    ))
    missing_points = [pt for pt in key_points if _bigram_coverage(pt, plan_text) < KEY_POINT_COVERAGE]
    if missing_points:
        shown = "；".join(missing_points[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_points) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_points) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"大纲未安排 {len(missing_points)} 条主线剧情点：{shown}{extra}；"
            "请把每个剧情点分配到对应镜头的 beat/covers；drop_list 内容禁止安排")
    spine = screenplay.plot_spine
    if spine and spine.drop_list:
        for drop in spine.drop_list:
            d = (drop or "").strip()
            if len(_condense(d)) < 6:
                continue
            if _bigram_coverage(d, plan_text) >= 0.55:
                errors.append(
                    f"大纲安排了 drop_list 内容「{d[:40]}」；已声明不拍的支线不得进入大纲"
                )
                break
    errors.extend(outline_key_line_speaker_errors(outline, screenplay))
    errors.extend(outline_key_line_capacity_errors(outline, screenplay))
    errors.extend(outline_atomic_errors(outline))
    if bible is not None:
        errors.extend(validate_storyboard_outline_scene_alignment(outline, screenplay, bible))
    return errors


def validate_storyboard_direction_contract(
    board: Storyboard,
    outline: StoryboardOutline | None,
) -> list[str]:
    """Validate shot purpose, context delivery and camera grammar for scene packs."""
    if outline is None or not outline.scene_contexts:
        return []
    errors: list[str] = []
    briefs = {int(item.shot_no): item for item in outline.shots}
    scene_shots: dict[str, list[Shot]] = {}
    valid_focuses = {
        "context", "action", "emotion", "dialogue", "evidence", "transition",
    }
    for shot in board.shots:
        brief = briefs.get(int(shot.shot_no))
        if brief is None:
            errors.append(f"第 {shot.shot_no} 镜没有对应导演规划任务")
            continue
        scene_id = str(shot.scene_id or brief.scene_id or "").strip()
        if not scene_id:
            errors.append(f"第 {shot.shot_no} 镜缺少 scene_id")
        scene_shots.setdefault(scene_id, []).append(shot)
        if len((shot.purpose or "").strip()) < 6:
            errors.append(f"第 {shot.shot_no} 镜 purpose 过短；必须说明本镜为何存在")
        if len((shot.resulting_change or "").strip()) < 4:
            errors.append(
                f"第 {shot.shot_no} 镜 resulting_change 过短；"
                "必须写清剧情、人物、空间、情绪或上下文发生了什么变化"
            )
        if shot.readability_focus not in valid_focuses:
            errors.append(
                f"第 {shot.shot_no} 镜 readability_focus={shot.readability_focus!r} 非法；"
                f"必须从 {sorted(valid_focuses)} 中选择"
            )
        if not (shot.camera_angle or "").strip():
            errors.append(f"第 {shot.shot_no} 镜缺少 camera_angle；摄影三元组不完整")
        if len((shot.camera_motivation or "").strip()) < 6:
            errors.append(
                f"第 {shot.shot_no} 镜 camera_motivation 过短；"
                "必须解释景别、角度与运动如何服务本镜作用"
            )
        if shot.repeat_of_shot_id and len((shot.repeat_gain or "").strip()) < 6:
            errors.append(
                f"第 {shot.shot_no} 镜声明重复 {shot.repeat_of_shot_id}，"
                "但 repeat_gain 未说明新增视角、反应或兑现价值"
            )
        if set(shot.context_requirement_ids or []) - set(
            brief.context_requirement_ids or []
        ):
            errors.append(
                f"第 {shot.shot_no} 镜交付了导演规划未分配的 context_requirement_ids"
            )

    for index in range(1, len(board.shots)):
        previous = board.shots[index - 1]
        current = board.shots[index]
        previous_delivery = {
            *list(previous.spine_beat_ids or []),
            *list(previous.context_requirement_ids or []),
            *list(previous.key_line_ids or []),
            *list(previous.information_ids or []),
        }
        current_delivery = {
            *list(current.spine_beat_ids or []),
            *list(current.context_requirement_ids or []),
            *list(current.key_line_ids or []),
            *list(current.information_ids or []),
        }
        previous_contribution = (
            previous.shot_contribution.model_dump(mode="json")
            if previous.shot_contribution is not None else {}
        )
        current_contribution = (
            current.shot_contribution.model_dump(mode="json")
            if current.shot_contribution is not None else {}
        )
        previous_contribution.pop("shot_contribution_id", None)
        current_contribution.pop("shot_contribution_id", None)

        def _has_structured_gain() -> bool:
            """Detect a real graph delivery that the legacy ID set cannot express."""
            for field, value in current_contribution.items():
                previous_value = previous_contribution.get(field)
                if isinstance(value, list):
                    if set(value) - set(previous_value or []):
                        return True
                elif isinstance(value, dict):
                    if value and value != (previous_value or {}):
                        return True
                elif value not in (None, "", 0, 0.0, False) and value != previous_value:
                    return True
            return False

        same_delivery = (
            bool(previous_delivery)
            and previous_delivery == current_delivery
            and _too_similar(previous.resulting_change, current.resulting_change)
            and not _has_structured_gain()
        )
        split_action_continuation = bool(
            current.continuity_mode == "action_continuation"
            and (previous.primary_action or "").strip()
            and (current.primary_action or "").strip()
            and not _too_similar(previous.primary_action, current.primary_action)
            and (previous.state_out or "").strip()
            and (current.state_in or "").strip()
            and _too_similar(previous.state_out, current.state_in)
        )
        if (
            same_delivery
            and not current.repeat_of_shot_id
            and not split_action_continuation
        ):
            errors.append(
                f"第 {previous.shot_no} 与第 {current.shot_no} 镜交付内容和结果几乎相同；"
                "请合并，或显式填写 repeat_of_shot_id/repeat_gain 说明新作用"
            )

    for scene in outline.scene_contexts:
        shots = scene_shots.get(scene.scene_id, [])
        if not shots:
            errors.append(f"场景 {scene.scene_id} 没有生成任何镜头")
            continue
        delivered: dict[str, int] = {}
        for shot in shots:
            for requirement_id in shot.context_requirement_ids or []:
                delivered.setdefault(requirement_id, int(shot.shot_no))
        for requirement in scene.context_requirements:
            owner = delivered.get(requirement.requirement_id)
            if owner is None:
                errors.append(
                    f"场景 {scene.scene_id} 未建立上下文 {requirement.requirement_id}："
                    f"{requirement.description}"
                )
            elif (
                requirement.required_before_shot_no is not None
                and owner > requirement.required_before_shot_no
            ):
                errors.append(
                    f"场景 {scene.scene_id} 的上下文 {requirement.requirement_id} "
                    f"到第 {owner} 镜才建立，晚于依赖镜 {requirement.required_before_shot_no}"
                )
    return list(dict.fromkeys(errors))


def score_storyboard_direction_readability(
    board: Storyboard,
    outline: StoryboardOutline | None,
) -> list[str]:
    """Return camera-grammar preferences that must not block publication."""
    if outline is None or not outline.scene_contexts:
        return []
    briefs = {int(item.shot_no): item for item in outline.shots}
    scene_shots: dict[str, list[Shot]] = {}
    for shot in board.shots:
        brief = briefs.get(int(shot.shot_no))
        scene_id = str(
            shot.scene_id
            or (brief.scene_id if brief is not None else "")
            or ""
        ).strip()
        if scene_id:
            scene_shots.setdefault(scene_id, []).append(shot)

    warnings: list[str] = []
    for scene in outline.scene_contexts:
        shots = scene_shots.get(scene.scene_id, [])
        action_shots = [
            shot for shot in shots if shot.readability_focus == "action"
        ]
        if action_shots and not any(
            shot.shot_size in {"中景", "全景", "远景"}
            and shot.camera_move in {"跟随", "横摇"}
            for shot in action_shots
        ):
            warnings.append(
                f"场景 {scene.scene_id} 含动作段，但缺少中景/全景/远景配合跟随或横摇的"
                "空间可读镜头；动作路径、主体和作用对象可能不清楚"
            )
        emotion_shots = [
            shot for shot in shots if shot.readability_focus == "emotion"
        ]
        if emotion_shots and not any(
            shot.shot_size in {"近景", "特写"}
            and shot.camera_move in {"固定", "推近"}
            for shot in emotion_shots
        ):
            warnings.append(
                f"场景 {scene.scene_id} 含情绪转折，但缺少近景/特写配合固定或推近的"
                "情绪可读镜头"
            )
    return list(dict.fromkeys(warnings))
