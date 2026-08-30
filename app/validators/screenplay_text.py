"""C1.5 可拍剧本正文解析（场次标题/对白行/音效行识别、对白轮次切分），
以及「必保留清单」关键台词（key_lines）模糊匹配与原文台词片段提取工具。

screenplay_speaker_names / _script_dialogue_turns 被 app.portraits 用于
从剧本正文提取说话人；derive_key_lines 被剧本台/分镜台多处复用。
"""
from __future__ import annotations

import re

from app import textmatch
from app.renderability import (
    KEY_LINES_MIN,
    KEY_PLOT_POINTS_MIN,
)
from app.schemas import (
    EpisodeScreenplay,
    KeyDialogueChain,
)

SCRIPT_SCENE_HEADING_RE = re.compile(r"【场\s*\d+】")
SCRIPT_DIALOGUE_LINE_RE = re.compile(r"^[^\n：]{1,16}(?:（[^）]{1,12}）)?：", re.M)
SCRIPT_SOUND_LINE_RE = re.compile(r"^([^\n：（]{1,16})(?:（([^）]{1,12})）)?：(.+)$", re.M)
# 模型偶发把「【场1】角色：台词」粘在同一行；剥场次标题后再识别说话人。
_SCRIPT_GLUED_HEADING_DIALOGUE_RE = re.compile(
    r"^【场\s*\d+】\s*([^\n：/（]{1,16})(?:（([^）]{1,12})）)?：(.+)$"
)
def _iter_script_sound_matches(full_text: str):
    """逐行提取剧本对白，避免把场次标题/地点梗概误判成说话人。"""
    for raw_line in (full_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if SCRIPT_SCENE_HEADING_RE.match(line):
            glued = _SCRIPT_GLUED_HEADING_DIALOGUE_RE.match(line)
            if glued and "/" not in glued.group(1):
                yield glued
            continue
        match = SCRIPT_SOUND_LINE_RE.match(line)
        if not match:
            continue
        speaker = match.group(1).strip()
        # 地点梗概（含 /）或残留场次标记不是说话人
        if "/" in speaker or "【" in speaker:
            continue
        yield match


def screenplay_speaker_names(full_text: str) -> list[str]:
    """Return distinct speaker IDs using the canonical screenplay-line parser."""
    return list(dict.fromkeys(
        match.group(1).strip()
        for match in _iter_script_sound_matches(full_text)
        if match.group(1).strip()
    ))


def _script_dialogue_turns(full_text: str) -> list[tuple[int, str, str]]:
    """Return screenplay dialogue turns as ``(scene_no, speaker, line)`` in story order."""
    turns: list[tuple[int, str, str]] = []
    scene_no = 0
    for raw_line in (full_text or "").splitlines():
        line = raw_line.strip()
        heading = SCRIPT_SCENE_HEADING_RE.search(line)
        if heading:
            number = re.search(r"\d+", heading.group(0))
            if number:
                scene_no = int(number.group(0))
        for match in _iter_script_sound_matches(line):
            turns.append((scene_no, match.group(1).strip(), match.group(3).strip()))
    return turns


def _screenplay_scene_space(heading: str) -> tuple[str, str]:
    """把场次标题拆成时间与地点，用于识别同一连续空间的子区域。"""
    value = re.sub(r"^【场\s*\d+】\s*", "", (heading or "").strip())
    parts = re.split(r"\s*/\s*", value, maxsplit=1)
    if len(parts) == 1:
        return "", _condense(parts[0])
    return _condense(parts[0]), _condense(parts[1])


def _dialogue_chain_crosses_hard_scene_boundary(
    script: EpisodeScreenplay,
    scene_numbers: set[int],
) -> bool:
    """区分真正换场与同一时空内的相邻子区域切块。

    剧本常把「迎客大厅」和「迎客大厅角落」拆成两个节拍场块；这不是对白
    因果被打断，不应阻断发布。非相邻场次、时间变化或地点无连续关系时仍按
    真正跨场处理。
    """
    ordered = sorted(number for number in scene_numbers if number > 0)
    if len(ordered) != len(scene_numbers) or any(
        right != left + 1 for left, right in zip(ordered, ordered[1:])
    ):
        return True
    headings = {
        int(scene.scene_no): str(scene.scene_heading or "")
        for scene in (script.scene_outline or [])
    }
    for left, right in zip(ordered, ordered[1:]):
        left_heading = headings.get(left, "")
        right_heading = headings.get(right, "")
        if not left_heading or not right_heading:
            return True
        left_time, left_location = _screenplay_scene_space(left_heading)
        right_time, right_location = _screenplay_scene_space(right_heading)
        if left_time and right_time and left_time != right_time:
            return True
        if not left_location or not right_location:
            return True
        if left_location in right_location or right_location in left_location:
            continue
        common = 0
        for left_char, right_char in zip(left_location, right_location):
            if left_char != right_char:
                break
            common += 1
        if common < 3:
            return True
    return False


# ---------- 关键内容（必保留清单）模糊匹配工具 ----------
# 防丢失校验的共用底座：映射台/分镜台都要判断"某条关键台词/剧情点是否仍真实存在于文本里"。
# 务实优先（本次定调）：只拦【明显丢失】，用模糊匹配容忍口语化改写/标点差异，绝不逐字比对，
# 避免像历史 false-positive 那样空耗修复轮次。
_SPEAKER_PREFIX_RE = textmatch._SPEAKER_PREFIX_RE
_NON_CONTENT_RE = textmatch._NON_CONTENT_RE
KEY_LINE_PRESENT_RATIO = textmatch.KEY_LINE_PRESENT_RATIO
KEY_LINE_BIGRAM_COVERAGE = textmatch.KEY_LINE_BIGRAM_COVERAGE
KEY_POINT_COVERAGE = textmatch.KEY_POINT_COVERAGE
KEY_CONTENT_MAX_REPORT = 4       # 单条错误最多点名几条，避免错误列表过长把 prompt 撑爆
MIN_KEY_LINES = KEY_LINES_MIN
MIN_KEY_PLOT_POINTS = KEY_PLOT_POINTS_MIN
# key_line_order_errors 专用：去说话人/标点后短于此字数的关键台词（如"不对。""莫非……"，
# 核心内容只有 1~2 个汉字）不参与顺序判定，见该函数文档字符串的真实回归案例。
# 数值与本文件里 drop_list 判定使用的"可判定内容最短长度"（`len(_condense(d)) < 6`）
# 保持一致，不是新引入的独立阈值。
ORDER_CHECK_MIN_CORE_CHARS = 6


_strip_speaker = textmatch.strip_speaker
_speaker_name = textmatch.speaker_name


def _structured_key_line_functions(
    script: EpisodeScreenplay,
    line: str,
) -> set[str]:
    """Return authoritative dialogue functions for an exact structured key line."""
    spoken = _condense(_strip_speaker(line or ""))
    expected_speaker = _condense(_speaker_name(line or ""))
    if not spoken:
        return set()
    functions: set[str] = set()
    for chain in script.dialogue_chains or []:
        for turn in chain.turns or []:
            if _condense(turn.line or "") != spoken:
                continue
            if expected_speaker and _condense(turn.speaker or "") != expected_speaker:
                continue
            functions.add((turn.function or "").strip())
    return functions


def _matching_text_indices(needle: str, ordered_texts: list[str]) -> list[int]:
    core = _strip_speaker(needle)
    return [
        i for i, text in enumerate(ordered_texts)
        if _longest_run_ratio(core, text) >= KEY_LINE_PRESENT_RATIO
        or _bigram_coverage(core, text) >= KEY_LINE_BIGRAM_COVERAGE
    ]


def key_lines_in_story_order(key_lines: list[str], full_script_text: str) -> list[str]:
    """Return key-line text in its actual screenplay order without changing KL identities.

    ``dialogue_chains`` are model-produced groups and can arrive in topic/importance order.
    Existing storyboards already persist KL01.. references derived from that list, so validation
    must sort a copy for narrative-order checks rather than renumbering the stored catalog.
    """
    cleaned = [line.strip() for line in key_lines if line and line.strip()]
    dialogue_turns = _script_dialogue_turns(full_script_text or "")
    if len(cleaned) < 2 or not dialogue_turns:
        return cleaned
    ordered_speakers = [speaker.strip() for _scene, speaker, _spoken in dialogue_turns]
    ordered_texts = [spoken for _scene, _speaker, spoken in dialogue_turns]
    fallback_start = len(ordered_texts)
    ranked: list[tuple[int, int, str]] = []
    for original_index, line in enumerate(cleaned):
        expected_speaker = _speaker_name(line)
        candidates = _matching_text_indices(line, ordered_texts)
        if expected_speaker:
            speaker_matches = [
                index for index in candidates
                if ordered_speakers[index] == expected_speaker
            ]
            if speaker_matches:
                candidates = speaker_matches
        position = candidates[0] if candidates else fallback_start + original_index
        ranked.append((position, original_index, line))
    return [line for _position, _original_index, line in sorted(ranked)]


def _key_lines_from_chains(chains: list[KeyDialogueChain]) -> list[str]:
    lines: list[str] = []
    for chain in chains:
        for turn in chain.turns:
            speaker = (turn.speaker or "").strip()
            line = (turn.line or "").strip()
            if not line:
                continue
            lines.append(f"{speaker}：{line}" if speaker else line)
    return lines


def derive_key_lines(
    chains: list[KeyDialogueChain],
    full_script_text: str,
) -> list[str]:
    """Single source of truth for ``EpisodeScreenplay.key_lines``.

    权威源是 ``dialogue_chains``（触发→回应的主线对白链）。此函数先按链结构
    平铺，再用 ``key_lines_in_story_order`` 依据 ``full_script_text`` 的正文出现
    顺序重排。IR 编译、document 投影、校验期归一化都必须走这一条算法，才能对同
    一份输入产出逐字段相等的 key_lines，杜绝双派生路径漂移。放在 validators 与
    ``key_lines_in_story_order`` 同层，避免 screenplay_ir 反向依赖 production 层。
    """
    return key_lines_in_story_order(
        _key_lines_from_chains(chains),
        full_script_text or "",
    )


def key_line_order_errors(
    key_lines: list[str], ordered_texts: list[str], *, subject: str,
) -> list[str]:
    """Ensure key dialogue remains in narrative order, not merely present as a bag of lines.

    超短台词（去说话人/标点后 < ``ORDER_CHECK_MIN_CORE_CHARS`` 字，如"不对。""莫非……"）
    被跳过，既不用来推进游标，也不因"顺序不对"而报错。原因：``_matching_text_indices``
    的模糊匹配对 1~2 字的核心内容几乎必然在多处产生假阳性命中——任何包含这两三个字的
    文本都会被 ``_longest_run_ratio``/``_bigram_coverage`` 判定"命中"，因为分母（台词自身
    长度）太小。真实回归（EP6 run_9bfcd5cbe128，2026-08-25）：大纲里"孟浩：莫非……"
    的候选命中列表里混进了一处远超其真实剧情位置的镜头，一旦当真就把 last_index
    提前推到那里，导致其后 4 条本来顺序完全正确的台词（分别在更早的镜头里、且
    key_line_ids 已经显式声明了正确位置）被连带误判成"打乱顺序"。这类超短台词的
    真实位置本来就无法靠散文模糊匹配可靠判定，跳过它们不会漏判——它们是否被交付，
    由 key_line_ids 结构化台账负责（见 validate_storyboard_outline 的 missing_lines
    分支），这里只负责判定"可靠可判定的台词"是否被打乱顺序。
    """
    last_index = -1
    out_of_order: list[str] = []
    for line in key_lines:
        if len(_condense(_strip_speaker(line))) < ORDER_CHECK_MIN_CORE_CHARS:
            continue
        candidates = _matching_text_indices(line, ordered_texts)
        if not candidates:  # Missing-content validators report this separately.
            continue
        following = [index for index in candidates if index >= last_index]
        if following:
            last_index = following[0]
        else:
            out_of_order.append(line)
    if not out_of_order:
        return []
    shown = "；".join(out_of_order[:KEY_CONTENT_MAX_REPORT])
    return [
        f"{subject}打乱了主线对白顺序：{shown}；key_lines 是按剧情发生顺序排列的对白链，"
        "提问/刺激必须先于回答/安慰/反驳，禁止只保留一组无序金句"
    ]


_condense = textmatch.condense
_longest_run_ratio = textmatch.longest_run_ratio
_bigram_set = textmatch.bigram_set
_bigram_coverage = textmatch.bigram_coverage
_CLAIM_SPLIT_RE = textmatch._CLAIM_SPLIT_RE
_atomize_claim = textmatch.atomize_claim


_SOURCE_QUOTED_UTTERANCE_RE = re.compile(
    r"[“「『](?P<line>[^”」』\n]{2,240})[”」』]"
)
_SOURCE_PREFIXED_UTTERANCE_RE = re.compile(
    r"(?m)^\s*[^\n：:“「『]{1,20}(?:[（(][^）)]{1,12}[）)])?\s*[：:]\s*(?P<line>\S.{1,239})\s*$"
)


def source_dialogue_fragments(source_text: str | None) -> list[str]:
    """Extract source utterances in deterministic source order.

    This inventory exists before the model chooses ``key_lines``.  It closes
    the former circular contract where a line omitted by the model could no
    longer be detected because the model-authored key-line list was the only
    source of truth.
    """
    if not source_text:
        return []
    matches: list[tuple[int, str]] = []
    for pattern in (_SOURCE_QUOTED_UTTERANCE_RE, _SOURCE_PREFIXED_UTTERANCE_RE):
        for match in pattern.finditer(source_text):
            line = match.group("line").strip().strip("“”「」『』\"'")
            if len(_condense(line)) >= 2:
                matches.append((match.start(), line))
    matches.sort(key=lambda item: item[0])
    result: list[str] = []
    seen: set[str] = set()
    for _offset, line in matches:
        identity = _condense(line)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(line)
    return result
