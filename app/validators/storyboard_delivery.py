"""分镜台声轨与必保留内容交付校验：声轨统计、台词/剧情点是否落到分镜
（V10/V11）、脊柱交付台账、分镜覆盖大纲校验、关键台词容量判据。
"""
from __future__ import annotations

import re

from app import config
from app.schemas import (
    EpisodeScreenplay,
    Shot,
    Storyboard,
    StoryboardOutline,
)
from app.spoken_contract import (
    content_char_count,
    max_speech_chars,
    spoken_text_of,
)

from .ending_hook import _claim_clearly_absent
from .screenplay_text import (
    KEY_CONTENT_MAX_REPORT,
    KEY_LINE_BIGRAM_COVERAGE,
    KEY_LINE_PRESENT_RATIO,
    KEY_POINT_COVERAGE,
    _atomize_claim,
    _bigram_coverage,
    _condense,
    _iter_script_sound_matches,
    _longest_run_ratio,
    _speaker_name,
    _strip_speaker,
    key_line_order_errors,
    key_lines_in_story_order,
)

def validate_storyboard_soundtrack(board: Storyboard, screenplay: EpisodeScreenplay,
                                   target_duration_s: int) -> list[str]:
    """校验从完整剧本拆分出的分镜是否保留了可听见的剧情信息。

    通用 validate_storyboard 只管结构与画面可生成性；这里专门约束“映射台已有台词/内心/旁白，
    分镜台不能把它们压成纯画面卡”。反应镜头可仅保留环境声/氛围，不强制 75% 镜头都有口播。
    错误会进入修复回路，让模型补齐关键声轨。
    """
    errors: list[str] = []
    shots = board.shots
    if not shots:
        return errors

    stats = _screenplay_sound_stats(screenplay)
    script_sound_cues = sum(stats.values())
    if script_sound_cues == 0:
        return errors

    script_dialogue_targets = stats["dialogues"] + stats["quoted_voice"]
    if script_dialogue_targets >= 2:
        dialogue_count = sum(len(shot.dialogues) for shot in shots)
        # Renderability：只要求覆盖主线口播密度，不再按「剧本对白处数 × 50% 镜头」硬逼。
        key_line_n = len([ln for ln in (screenplay.key_lines or []) if ln and str(ln).strip()])
        min_dialogues = max(1, min(script_dialogue_targets, key_line_n or 2, 4))
        if dialogue_count < min_dialogues:
            errors.append(
                f"分镜对白不足：主线至少需要约 {min_dialogues} 句角色开口"
                f"（当前 dialogues={dialogue_count}）；请把 key_lines/主线对白写入 dialogues，"
                "群嘲等环境声可写进 action_desc，不要为密度硬塞碎镜")

    # 产品合同禁止旁白/内心OS：剧本中的内心描写改由画面姿态表达，不再强制落到 narration。
    return errors


def validate_storyboard_preserves_key_content(board: Storyboard,
                                              screenplay: EpisodeScreenplay) -> list[str]:
    """防丢失核心校验：分镜必须保留映射台显式标记的【必保留关键台词 / 关键剧情点】。

    与 validate_storyboard_soundtrack 互补——后者只看"有没有声轨、声轨够不够多"，
    这里看"剧本里那几句金句/那几个关键反转有没有真的落到镜头里"，专治"重要台词/剧情被静默丢弃"。
    务实优先：用模糊匹配只拦【明显丢失】，命中即放行；剧本未声明清单时（旧数据/兜底）直接放行。
    """
    # A narrative contract has stable action/event/fact/target-delta ownership
    # and a complete audience hand-off graph.  Text overlap against key-line or
    # plot-point prose is a legacy migration aid only; using it here would make
    # a valid semantic paraphrase fail on a fixed vocabulary threshold.  The
    # narrative graph gate is fail-closed and is invoked by every modern caller.
    if screenplay.narrative_plan is not None:
        return []

    errors: list[str] = []
    shots = board.shots
    if not shots:
        return errors
    key_lines = list(key_line_catalog(screenplay).values())
    key_points = [pt.strip() for pt in (screenplay.key_plot_points or []) if pt and pt.strip()]

    # 关键台词只认有效口播（spoken_text_of）；source_excerpt 是审计证据，不能证明「已说出」。
    spoken_text = "".join(spoken_text_of(s) for s in shots)
    # 剧情点/画面覆盖可用动作描述；仍不含 source_excerpt，避免把摘录当已拍证据。
    visual_text = spoken_text + "".join((s.action_desc or "") for s in shots)

    missing_lines = []
    for ln in key_lines:
        core = _strip_speaker(ln)
        if (
            _longest_run_ratio(core, spoken_text) < KEY_LINE_PRESENT_RATIO
            and _bigram_coverage(core, spoken_text) < KEY_LINE_BIGRAM_COVERAGE
        ):
            missing_lines.append(ln)
    if missing_lines:
        shown = "；".join(missing_lines[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_lines) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_lines) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"分镜丢失了剧本标记的 {len(missing_lines)} 条主线台词：{shown}{extra}；"
            "请把它们写进对应镜头的 dialogues（人物开口），不要在压缩中丢弃")
    ordered_spoken_turns = [
        dialogue.line
        for shot in shots
        for dialogue in (shot.dialogues or [])
        if (dialogue.line or "").strip()
    ]
    errors.extend(key_line_order_errors(
        key_lines_in_story_order(key_lines, screenplay.full_script_text),
        ordered_spoken_turns,
        subject="分镜 dialogues",
    ))

    spine = screenplay.plot_spine
    # textmatch 模糊匹配只用于没有稳定 ID 的旧数据降级判定。新版剧本已有 plot_spine，
    # 其 must_keep 由下方 validate_spine_delivery_ledger 逐 ID 校验；若仍用自由文本摘要的
    # 2-gram 重合率单独报 blocker，会把”甲一三段低级已由 S01+KL01/KL02 交付”误判成
    # 没逐字写“天才跌落谷底”而缺剧情，与 textmatch 模块的主从口径相冲突。
    if not (spine and spine.spine_beats):
        missing_points = [
            pt for pt in key_points
            if _bigram_coverage(pt, visual_text) < KEY_POINT_COVERAGE
        ]
        if missing_points:
            shown = "；".join(missing_points[:KEY_CONTENT_MAX_REPORT])
            extra = (f"（另有 {len(missing_points) - KEY_CONTENT_MAX_REPORT} 条从略）"
                     if len(missing_points) > KEY_CONTENT_MAX_REPORT else "")
            errors.append(
                f"分镜丢失了剧本标记的 {len(missing_points)} 条主线剧情点：{shown}{extra}；"
                "请在对应镜头的 action_desc 或声轨中体现这些局势变化，不能整段略过")

    if spine and spine.spine_beats:
        errors.extend(validate_spine_delivery_ledger(board, screenplay))
        for drop in spine.drop_list or []:
            d = (drop or "").strip()
            if len(_condense(d)) < 6:
                continue
            if _bigram_coverage(d, visual_text) >= 0.55:
                errors.append(
                    f"分镜又拍回了 drop_list 内容「{d[:40]}」；已声明不拍的支线不得进入分镜"
                )
                break
    return errors


def key_line_delivery_errors(shot: Shot, screenplay: EpisodeScreenplay | None = None) -> list[str]:
    """单镜声明的 key_line_ids 必须真实出现在有效口播段中（source_excerpt 不算交付）。"""
    kids = [str(k).strip().upper() for k in (shot.key_line_ids or []) if str(k).strip()]
    if not kids:
        return []
    catalog = key_line_catalog(screenplay) if screenplay is not None else {}
    spoken = spoken_text_of(shot)
    errors: list[str] = []
    for kid in kids:
        text = catalog.get(kid)
        if text:
            core = _strip_speaker(text)
            if (
                _longest_run_ratio(core, spoken) < KEY_LINE_PRESENT_RATIO
                and _bigram_coverage(core, spoken) < KEY_LINE_BIGRAM_COVERAGE
            ):
                errors.append(
                    f"shot_no={shot.shot_no} 声明了 {kid} 但有效口播未说出「{core[:36]}」；"
                    "关键台词必须出现在 dialogues/audio_timeline，source_excerpt 不算交付"
                )
        # 无剧本 catalog 时只校验 ID 格式（格式已由 shot_id_space_errors 负责）
    return errors


def _spine_delivery_clauses(
    does: str,
) -> tuple[list[str], list[str], list[str]]:
    """Keep legacy prose whole; typed KL/I delivery fields own channel semantics."""
    claim = (does or "").strip(" ，,；;。")
    return ([claim] if claim else []), [], []


def _spine_receptive_claim(clause: str) -> str:
    """Compatibility helper; typed delivery contracts no longer rewrite prose."""
    return clause


def validate_spine_delivery_ledger(
    board: Storyboard, screenplay: EpisodeScreenplay
) -> list[str]:
    """结构化主线覆盖（PRD VAL-422 §4.4.3）：以 spine_beat_ids + I*/KL* 台账为主判据。

    - 至少一个镜头声明对应 spine_beat_id，且 beat.who 必须在该镜或相邻镜的可见动作中出现；
      只写 ID、由测验员宣布结果或由路人谈论当事人，不能替代真正拍出动作主体；
    - 若 beat 绑定了 information_ids/key_line_ids，则这些原子必须由声明该 spine 的镜头
      （或其相邻镜头）交付；
    - 全集没有任何结构化 ID 时，二元字组失败只产生 LEGACY_COVERAGE_UNCERTAIN，
      不得单独冒充 must_keep missing blocker。
    """
    spine = screenplay.plot_spine
    if not spine or not spine.spine_beats:
        return []
    shots = board.shots or []
    delivered_ids: set[str] = set()
    shots_for_beat: dict[str, list[Shot]] = {}
    for s in shots:
        for beat_id in s.spine_beat_ids or []:
            bid = str(beat_id).strip().upper()
            if not bid:
                continue
            delivered_ids.add(bid)
            shots_for_beat.setdefault(bid, []).append(s)

    structured_mode = bool(delivered_ids) or any(
        (s.key_line_ids or s.information_ids) for s in shots
    )
    catalog = key_line_catalog(screenplay)
    ledger_by_id = {
        (item.info_id or "").strip().upper(): item
        for item in (screenplay.information_ledger or [])
        if (item.info_id or "").strip()
    }
    visual_text = "".join(spoken_text_of(s) + (s.action_desc or "") for s in shots)
    errors: list[str] = []
    missing_beats: list[str] = []
    legacy_uncertain: list[str] = []
    missing_atoms: list[str] = []
    missing_visual_subjects: list[str] = []
    missing_visible_actions: list[str] = []

    for beat in spine.spine_beats:
        if not beat.must_keep:
            continue
        beat_id = (beat.beat_id or "").strip().upper()
        claim = f"{beat.who}{beat.does}{beat.turn}"
        if beat_id and beat_id in delivered_ids:
            owners = shots_for_beat.get(beat_id) or []
            # 允许相邻镜共同交付：把声明该 spine 的镜号 ±1 一并纳入窗口。
            owner_nos = {s.shot_no for s in owners}
            window = [
                s for s in shots
                if s.shot_no in owner_nos
                or any(abs(s.shot_no - n) == 1 for n in owner_nos)
            ]
            window_spoken = "".join(spoken_text_of(s) for s in window)
            window_visual = window_spoken + "".join((s.action_desc or "") for s in window)
            who_parts = [
                part.strip()
                for part in re.split(r"[、，,和与及/／\s]+", (beat.who or "").strip())
                if part.strip()
            ]
            # Stable identities must enter the visible contract; prose labels
            # receive no role-name exceptions.
            for who in who_parts:
                subject_shots = [
                    s for s in window
                    if any(
                        who in visible_name or visible_name in who
                        for visible_name in {
                            str(name).strip()
                            for name in [*(s.characters or []), *(s.characters_visible or [])]
                            if str(name).strip()
                        }
                    )
                ]
                if not subject_shots:
                    missing_visual_subjects.append(f"{beat_id}/{who}")
                    continue
                subject_action_text = "".join(
                    (s.primary_action or "")
                    + (s.action_desc or "")
                    + (s.first_frame_desc or "")
                    + (s.last_frame_desc or "")
                    for s in subject_shots
                )
                subject_spoken_text = "".join(
                    dialogue.line
                    for s in subject_shots
                    for dialogue in (s.dialogues or [])
                    if (
                        who == (dialogue.speaker or "").strip()
                        or who in (dialogue.speaker or "").strip()
                        or (dialogue.speaker or "").strip() in who
                    )
                )
                visible_clauses, spoken_clauses, receptive_clauses = (
                    ([], [], [])
                    if beat.key_line_ids or beat.information_ids
                    else _spine_delivery_clauses(beat.does or "")
                )
                visible_missing = any(
                    _claim_clearly_absent(clause, subject_action_text)
                    for clause in visible_clauses
                )
                spoken_missing = bool(spoken_clauses) and not subject_spoken_text.strip()
                receptive_missing = any(
                    _claim_clearly_absent(
                        _spine_receptive_claim(clause),
                        window_visual,
                    )
                    for clause in receptive_clauses
                )
                if visible_missing or spoken_missing or receptive_missing:
                    missing_visible_actions.append(f"{beat_id}/{who}:{beat.does}")
            for kid in (beat.key_line_ids or []):
                kid_u = str(kid).strip().upper()
                text = catalog.get(kid_u)
                if not text:
                    continue
                core = _strip_speaker(text)
                if (
                    _longest_run_ratio(core, window_spoken) < KEY_LINE_PRESENT_RATIO
                    and _bigram_coverage(core, window_spoken) < KEY_LINE_BIGRAM_COVERAGE
                ):
                    missing_atoms.append(f"{beat_id}/{kid_u}")
            for iid in (beat.information_ids or []):
                iid_u = str(iid).strip().upper()
                item = ledger_by_id.get(iid_u)
                content = (item.content if item else "") or iid_u
                owner = (item.delivery_owner if item else "visual_action") or "visual_action"
                haystack = window_spoken if owner == "spoken_dialogue" else window_visual
                if item and item.exact_text:
                    if (
                        _longest_run_ratio(item.exact_text, haystack) < KEY_LINE_PRESENT_RATIO
                        and _bigram_coverage(item.exact_text, haystack) < KEY_LINE_BIGRAM_COVERAGE
                    ):
                        missing_atoms.append(f"{beat_id}/{iid_u}")
                elif _bigram_coverage(content, haystack) < KEY_POINT_COVERAGE:
                    missing_atoms.append(f"{beat_id}/{iid_u}")
            continue

        # 无 spine_beat_id：结构化模式下硬失败；legacy 模式字面命中放行，否则 uncertain。
        if structured_mode:
            missing_beats.append(f"{beat_id or '?'}:{beat.does}")
            continue
        if _bigram_coverage(claim, visual_text) >= KEY_POINT_COVERAGE:
            continue
        legacy_uncertain.append(f"{beat_id or '?'}:{beat.does}")

    if missing_beats:
        shown = "；".join(missing_beats[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_beats) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_beats) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"分镜未覆盖 {len(missing_beats)} 条 must_keep 主线节拍：{shown}{extra}；"
            "请在对应镜头写入 spine_beat_ids，允许相邻多镜共同交付同一 S*"
        )
    if missing_atoms:
        shown = "；".join(missing_atoms[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_atoms) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_atoms) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"主线节拍缺少必需信息原子/关键台词：{shown}{extra}；"
            "请在声明该 spine_beat_id 的镜头或其相邻镜交付对应 information_id/key_line_id"
        )
    if missing_visual_subjects:
        unique_missing = list(dict.fromkeys(missing_visual_subjects))
        shown = "；".join(unique_missing[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(unique_missing) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(unique_missing) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"主线节拍缺少可见动作主体：{shown}{extra}；"
            "请让 beat.who 在对应镜头或相邻镜的 action_desc/首尾帧中亲自完成主线动作；"
            "宣布结果、路人议论或仅填写 spine_beat_ids 都不能替代动作入画"
        )
    if missing_visible_actions:
        unique_missing = list(dict.fromkeys(missing_visible_actions))
        shown = "；".join(unique_missing[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(unique_missing) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(unique_missing) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"主线节拍主体已入画但未完成对应动作/对白交付：{shown}{extra}；"
            "可见动作请在 action_desc/首尾帧中拍出，口头交付请由该主体在 dialogues 中说出，"
            "不要用后续走位、静态反应或他人宣布替代"
        )
    if legacy_uncertain:
        shown = "；".join(legacy_uncertain[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(legacy_uncertain) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(legacy_uncertain) > KEY_CONTENT_MAX_REPORT else "")
        msg = (
            f"LEGACY_COVERAGE_UNCERTAIN：{len(legacy_uncertain)} 条 must_keep 主线节拍缺少 "
            f"spine_beat_ids 且字面证据不足：{shown}{extra}；请补 ID 或人工复核，"
            "二元字组匹配不得单独判定 must_keep missing"
        )
        from app.observability.metrics import inc, spine_structured_hard_gate
        inc("spine_coverage_legacy_fallback_total", count=len(legacy_uncertain))
        if spine_structured_hard_gate():
            errors.append(msg)
        # flag 关闭时仅记指标，不阻断确认
    return errors


def validate_storyboard_shot_covers_outline(
    shot: Shot, covers: str, shot_no: int,
    *, prior_text: str = "", later_planned_covers: str = "",
    narrative_authority: bool = False,
) -> list[str]:
    """逐镜填充阶段校验：大纲声明本镜要落实的 covers，必须真的进入本镜文本。

    这比收尾时才跑整集必保留校验更早发现漏戏，避免模型第 6 镜才被告知第 2 镜漏了"低级"。

    covers 是模型自写的复合事实改写，旧数据按句读拆成原子逐条核对。
    判定务实优先、只拦"整件事彻底没拍"：用更宽的"明显缺失"阈值（_claim_clearly_absent），
    某条原子在本镜+前序里几乎零命中才算漏。新叙事权威路径不解释 covers 文案，
    而是由事件、动作、证据与台词稳定 ID 完成覆盖校验。
    两类"承接"不算本镜漏戏：
    - 向前承接：该原子已在前序已通过镜头（prior_text）里体现；
    - 向后承接：大纲把同一事实也排给了后续镜头（later_planned_covers），留给后面拍。
    """
    # The authority path compares the typed outline task to the shot and then
    # validates the full narrative graph.  ``covers`` is free prose retained
    # for readers, so atomising it and applying synonym tables would be a
    # language-specific second source of truth.
    if narrative_authority:
        return []

    atoms = _atomize_claim(covers)
    if not atoms:
        return []

    # 口播只认有效口播段；source_excerpt / 旁白不得充当「已说出」证据（VAL-422）。
    spoken = spoken_text_of(shot)
    shot_text = (shot.action_desc or "") + spoken
    realized_text = shot_text + (prior_text or "")
    later = later_planned_covers or ""
    missing = [
        atom for atom in atoms
        if _claim_clearly_absent(atom, realized_text)
        and not (later and not _claim_clearly_absent(atom, later))
    ]
    if not missing:
        return []

    shown = "；".join(missing[:KEY_CONTENT_MAX_REPORT])
    extra = (f"（另有 {len(missing) - KEY_CONTENT_MAX_REPORT} 条从略）"
             if len(missing) > KEY_CONTENT_MAX_REPORT else "")
    return [
        f"第 {shot_no} 镜未落实本镜大纲 covers：{shown}{extra}；"
        "请把这些事实或台词明确写进本镜 action_desc 或有效口播（dialogues/audio_timeline），不能只停留在大纲里"
    ]


def key_line_catalog(screenplay: EpisodeScreenplay) -> dict[str, str]:
    """剧本 key_lines 的稳定 ID 映射：按出现顺序 KL01..KLnn。"""
    catalog: dict[str, str] = {}
    for idx, line in enumerate(screenplay.key_lines or [], start=1):
        text = (line or "").strip()
        if not text or _speaker_name(text) == "旁白":
            continue
        catalog[f"KL{idx:02d}"] = text
    return catalog


def outline_key_line_capacity_errors(
    outline: StoryboardOutline, screenplay: EpisodeScreenplay
) -> list[str]:
    """大纲阶段：分配到同一镜的必保留台词字数不得超过该镜最大口播容量。"""
    catalog = key_line_catalog(screenplay)
    if not catalog:
        return []
    errors: list[str] = []
    assigned: set[str] = set()
    for shot in outline.shots or []:
        duration = int(shot.duration_s or config.DEFAULT_VIDEO_DURATION_S)
        capacity = max_speech_chars(duration)
        kids = [str(k).strip().upper() for k in (shot.key_line_ids or []) if str(k).strip()]
        required_chars = 0
        lines_for_msg: list[str] = []
        for kid in kids:
            text = catalog.get(kid)
            if not text:
                errors.append(
                    "[OUTLINE_KEY_LINE_CAPACITY_INVALID] "
                    f"大纲第 {shot.shot_no} 镜 key_line_ids 含未知「{kid}」；"
                    f"合法范围：{', '.join(catalog)}"
                )
                continue
            if kid in assigned:
                errors.append(
                    "[OUTLINE_KEY_LINE_OWNER_DUPLICATE] "
                    f"关键台词 {kid} 被重复分配到第 {shot.shot_no} 镜；"
                    "同一句台词只能有一个交付镜，后续镜头应改为反应、动作或新信息"
                )
                continue
            assigned.add(kid)
            chars = content_char_count(_strip_speaker(text))
            required_chars += chars
            lines_for_msg.append(f"{kid}({chars}字)")
        if required_chars > capacity:
            errors.append(
                "[OUTLINE_KEY_LINE_CAPACITY_INVALID] "
                f"大纲第 {shot.shot_no} 镜必保留台词约 {required_chars} 字，"
                f"超过 {duration}s 口播上限 {capacity} 字（{', '.join(lines_for_msg)}）；"
                "请拆镜或把部分 key_line_ids 挪到相邻镜头，禁止把不可满足合同交给逐镜修复"
            )
    # 未分配的关键台词：若大纲声明了任何 key_line_ids，则要求全集覆盖
    if any((s.key_line_ids or []) for s in (outline.shots or [])):
        missing = [kid for kid in catalog if kid not in assigned]
        if missing:
            errors.append(
                "[OUTLINE_KEY_LINE_CAPACITY_INVALID] "
                f"大纲未分配关键台词 ID：{', '.join(missing)}；"
                "请把每条 KL* 写入某一镜的 key_line_ids"
            )
    return errors


# 从 screenplay_validate 段落移到这里，打破本包内部的 import 环（_screenplay_sound_stats 原本紧邻的段落会反过来依赖它所在的段落）。
def _screenplay_sound_stats(script: EpisodeScreenplay) -> dict[str, int]:
    full_text = (script.full_script_text or "").strip()
    stats = {"dialogues": 0, "inner": 0, "narration": 0, "quoted_voice": 0}
    for match in _iter_script_sound_matches(full_text):
        speaker = match.group(1).strip()
        if speaker == "旁白":
            stats["narration"] += 1
        else:
            stats["dialogues"] += 1
    return stats
