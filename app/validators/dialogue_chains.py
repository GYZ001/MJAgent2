"""剧本对白链路归一化（normalize_screenplay_dialogue_chains）与校验
（validate_dialogue_chains）——把原文对白按说话人/轮次组织成结构化链条，
供剧本整体校验与关键台词落实判据复用。
"""
from __future__ import annotations

import re
from typing import Any

from app import config, textmatch
from app.renderability import (
    DIALOGUE_CHAIN_TURNS_HARD_MAX,
    chunk_dialogue_turns,
)
from app.schemas import (
    EpisodeScreenplay,
    KeyDialogueChain,
)
from app.source_excerpt import align_source_excerpt
from app.spoken_contract import content_char_count

from .screenplay_text import (
    KEY_LINE_BIGRAM_COVERAGE,
    KEY_LINE_PRESENT_RATIO,
    MIN_KEY_LINES,
    SCRIPT_SOUND_LINE_RE,
    _bigram_coverage,
    _condense,
    _dialogue_chain_crosses_hard_scene_boundary,
    _longest_run_ratio,
    _matching_text_indices,
    _script_dialogue_turns,
    key_lines_in_story_order,
)

_DIALOGUE_TURN_FUNCTIONS = {
    "trigger", "announcement", "question", "response", "decision", "statement",
}
_DIALOGUE_RESPONSE_FUNCTIONS = {"response"}


def normalize_screenplay_dialogue_chains(
    script: EpisodeScreenplay,
    source_text: str = "",
) -> EpisodeScreenplay:
    """Make structured dialogue chains authoritative for downstream key-line delivery."""
    if not script.dialogue_chains:
        return script
    allowed_speakers = {
        str(item.speaker_id or "").strip()
        for item in (script.voice_bible or [])
        if str(item.speaker_id or "").strip()
    }
    if script.narrative_plan is not None:
        for identity in script.narrative_plan.identity_contracts:
            allowed_speakers.update({
                str(identity.identity_id or "").strip(),
                str(identity.display_name or "").strip(),
                *(
                    str(voice_id or "").strip()
                    for voice_id in (identity.voice_ids or [])
                ),
            })
    if source_text:
        for chain in script.dialogue_chains:
            grounded_turns = []
            for turn in chain.turns or []:
                source_line = str(turn.source_text or "").strip()
                if not source_line:
                    aligned = align_source_excerpt(
                        str(turn.line or ""),
                        source_text,
                    )
                    if aligned is None:
                        continue
                    turn.source_text = aligned.excerpt
                    source_line = aligned.excerpt
                if (
                    len(_condense(source_line)) < 2
                    and source_line in source_text
                    and _condense(turn.line) != _condense(source_line)
                ):
                    turn.line = source_line
                grounded_turns.append(turn)
            chain.turns = grounded_turns
        script.dialogue_chains = [
            chain for chain in script.dialogue_chains if chain.turns
        ]
        while script.dialogue_chains and script.dialogue_chains[0].turns:
            first_turn = script.dialogue_chains[0].turns[0]
            line = str(first_turn.line or "").strip()
            evidence = str(first_turn.source_text or "").strip()
            if (
                textmatch.spoken_digit_sequence_equivalent(evidence, line)
                or _longest_run_ratio(line, evidence)
                >= KEY_LINE_PRESENT_RATIO
                or _bigram_coverage(line, evidence)
                >= KEY_LINE_BIGRAM_COVERAGE
            ):
                break
            aligned = align_source_excerpt(line, source_text)
            if aligned is not None:
                first_turn.source_text = aligned.excerpt
                break
            script.dialogue_chains[0].turns.pop(0)
            if not script.dialogue_chains[0].turns:
                script.dialogue_chains.pop(0)
                continue
    for chain in script.dialogue_chains:
        turns = list(chain.turns or [])
        narrator_only_chain = bool(turns) and all(
            str(item.speaker or "").strip() == "旁白"
            for item in turns
        )
        normalized_turns = []
        for index, turn in enumerate(turns):
            speaker = str(turn.speaker or "").strip()
            source = _condense(turn.source_text)
            duplicate_source = bool(source) and any(
                index != other_index
                and (
                    source == _condense(other.source_text)
                    or source in _condense(other.source_text)
                    or _condense(other.source_text) in source
                )
                for other_index, other in enumerate(turns)
                if _condense(other.source_text)
            )
            source_contains_line = bool(
                _condense(turn.line)
                and _condense(turn.line) in source
            )
            if duplicate_source and (
                (
                    allowed_speakers
                    and speaker not in allowed_speakers
                )
                or (
                    speaker == "旁白"
                    and (
                        speaker not in allowed_speakers
                        or not narrator_only_chain
                    )
                    and not source_contains_line
                )
            ):
                line = str(turn.line or "").strip()
                for separator in ("：", ":"):
                    dialogue_line = f"{speaker}{separator}{line}"
                    if dialogue_line in (script.full_script_text or ""):
                        replacement = (
                            line
                            if speaker == "旁白"
                            else f"{speaker.rstrip('，,。；; ')}，{line}"
                        )
                        script.full_script_text = script.full_script_text.replace(
                            dialogue_line,
                            replacement,
                        )
                continue
            normalized_turns.append(turn)
        chain.turns = normalized_turns
    authoritative_turns = {
        (
            _condense(turn.speaker),
            _condense(turn.line),
        )
        for chain in script.dialogue_chains
        for turn in chain.turns or []
        if _condense(turn.speaker) and _condense(turn.line)
    }
    normalized_body_lines: list[str] = []
    for raw_line in (script.full_script_text or "").splitlines(
        keepends=True,
    ):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line):]
        match = SCRIPT_SOUND_LINE_RE.match(line.strip())
        if (
            match is not None
            and (
                _condense(match.group(1)),
                _condense(match.group(3)),
            )
            not in authoritative_turns
        ):
            indent = line[:len(line) - len(line.lstrip())]
            line = (
                f"{indent}{match.group(1).strip()}，"
                f"{match.group(3).strip()}"
            )
        normalized_body_lines.append(line + ending)
    script.full_script_text = "".join(normalized_body_lines)
    full_script_turns = _script_dialogue_turns(
        script.full_script_text or ""
    )

    def chain_scene_nos(chain: KeyDialogueChain) -> set[int]:
        identities = {
            (
                _condense(turn.speaker),
                _condense(turn.line),
            )
            for turn in (chain.turns or [])
        }
        return {
            scene_no
            for scene_no, speaker, line in full_script_turns
            if (_condense(speaker), _condense(line)) in identities
        }

    used_chain_ids = {
        str(chain.chain_id or "").strip()
        for chain in script.dialogue_chains
        if str(chain.chain_id or "").strip()
    }
    next_chain_number = 1

    def next_chain_id() -> str:
        nonlocal next_chain_number
        while f"DC{next_chain_number}" in used_chain_ids:
            next_chain_number += 1
        value = f"DC{next_chain_number}"
        used_chain_ids.add(value)
        next_chain_number += 1
        return value

    split_chains: list[KeyDialogueChain] = []
    for chain in script.dialogue_chains:
        located_groups: list[tuple[int, list[Any]]] = []
        previous_scene = 0
        for turn in chain.turns or []:
            turn_identity = (
                _condense(turn.speaker),
                _condense(turn.line),
            )
            scene_no = next((
                candidate_scene
                for candidate_scene, speaker, line in full_script_turns
                if (
                    _condense(speaker),
                    _condense(line),
                ) == turn_identity
            ), 0)
            scene_no = scene_no or previous_scene
            previous_scene = scene_no
            if located_groups and located_groups[-1][0] == scene_no:
                located_groups[-1][1].append(turn)
            else:
                located_groups.append((scene_no, [turn]))
        nonzero_scenes = {
            scene_no for scene_no, _turns in located_groups if scene_no
        }
        if len(nonzero_scenes) <= 1:
            split_chains.append(chain)
            continue
        for group_index, (_scene_no, turns) in enumerate(located_groups):
            split = chain.model_copy(deep=True)
            split.chain_id = (
                chain.chain_id
                if group_index == 0
                else next_chain_id()
            )
            split.topic = (
                chain.topic
                if group_index == 0
                else f"{str(chain.topic or '').strip()}（续）"
            )
            split.turns = turns
            if (
                group_index > 0
                and split.turns
                and split.turns[0].function == "response"
            ):
                split.turns[0].function = "statement"
            split_chains.append(split)
    script.dialogue_chains = split_chains

    merged_chains: list[KeyDialogueChain] = []
    for chain in script.dialogue_chains:
        if merged_chains:
            previous = merged_chains[-1]
            previous_topic = re.sub(
                r"[（(]\s*续\s*[）)]\s*$",
                "",
                str(previous.topic or "").strip(),
            )
            current_topic = re.sub(
                r"[（(]\s*续\s*[）)]\s*$",
                "",
                str(chain.topic or "").strip(),
            )
            first_function = (
                str(chain.turns[0].function or "").strip()
                if chain.turns else ""
            )
            combined_scenes = {
                *chain_scene_nos(previous),
                *chain_scene_nos(chain),
            }
            if (
                previous_topic
                and previous_topic == current_topic
                and first_function == "response"
                and len(combined_scenes) <= 1
                and len(previous.turns) + len(chain.turns)
                <= DIALOGUE_CHAIN_TURNS_HARD_MAX
            ):
                previous.turns = [
                    *previous.turns,
                    *chain.turns,
                ]
                continue
            if (
                previous_topic
                and previous_topic == current_topic
                and first_function == "response"
                and len(combined_scenes) > 1
            ):
                chain.turns[0].function = "statement"
        merged_chains.append(chain)
    script.dialogue_chains = merged_chains

    # 长度上限必须由**这个共享归一化器**兜底，不能只指望某一个生产者。
    # `DIALOGUE_CHAIN_TURNS_HARD_MAX` 原先只有 `compile_screenplay_ir` 的分块
    # 循环真正执行；validators / repair / document 投影里另外 7 处写
    # `chain.turns` 的地方都不检查它，于是一条 11 轮的 chain 能一路走到硬门禁
    # 才被拒——而修复层**没有任何策略能让 chain 变短**（只有往里补话轮的
    # `dialogue_chain_continuity`），必然记 exhausted（EP4 实测 0 个补丁）。
    #
    # 按发言边界切分：话轮总数与顺序不变，因此 `derive_key_lines` 展平后的
    # key_lines 与 KL## 编号逐字不变，对已生成的分镜零影响。
    bounded_chains: list[KeyDialogueChain] = []
    for chain in script.dialogue_chains:
        turns = list(chain.turns or [])
        if len(turns) <= DIALOGUE_CHAIN_TURNS_HARD_MAX:
            bounded_chains.append(chain)
            continue
        base_topic = re.sub(
            r"[（(]\s*续\s*[）)]\s*$", "", str(chain.topic or "").strip()
        )
        for part_index, chunk in enumerate(chunk_dialogue_turns(turns)):
            part = chain.model_copy(deep=True)
            part.turns = chunk
            if part_index:
                part.chain_id = next_chain_id()
                part.topic = f"{base_topic}（续）"
                if part.turns and part.turns[0].function == "response":
                    part.turns[0].function = "statement"
            bounded_chains.append(part)
    script.dialogue_chains = bounded_chains

    flattened: list[str] = []
    for chain in script.dialogue_chains:
        for turn in chain.turns or []:
            speaker = (turn.speaker or "").strip()
            line = (turn.line or "").strip()
            if speaker and speaker != "旁白" and line:
                flattened.append(f"{speaker}：{line}")
    script.key_lines = key_lines_in_story_order(flattened, script.full_script_text)
    return script


def _is_grounded_short_utterance(
    line: str,
    source_line: str,
    source_text: str | None,
) -> bool:
    """放行有真实原文依据的单字口语回应（如“哦。”“好。”）。

    ``line`` 允许把原文动作压成自然回应，但 ``source_text`` 必须逐字命中
    本集原文。这样字数下限只拦空值，不会放过说明性占位词或无依据对白。
    """
    spoken = _condense(line)
    evidence = _condense(source_line)
    source = _condense(source_text or "")
    if not spoken or not evidence or (source and evidence not in source):
        return False
    if spoken == evidence:
        return True
    return len(spoken) == 1 and bool(source) and evidence in source


def validate_dialogue_chains(
    script: EpisodeScreenplay,
    *,
    source_text: str | None,
    required: bool,
) -> list[str]:
    """Validate source-grounded trigger→reply chains before accepting a screenplay."""
    errors: list[str] = []
    chains = script.dialogue_chains or []
    if required and not chains:
        return [
            "dialogue_chains 缺失；必须先从原文建立“触发台词→回答/安慰/反驳”的主线对白链，"
            "再由后端生成 key_lines，禁止直接挑选孤立金句"
        ]
    if not chains:
        return errors

    chain_ids: set[str] = set()
    total_turns = 0
    full_turns = _script_dialogue_turns(script.full_script_text or "")
    full_texts = [turn[2] for turn in full_turns]
    for chain_index, chain in enumerate(chains):
        tag = f"dialogue_chains[{chain_index}]"
        chain_id = (chain.chain_id or "").strip().upper()
        if not re.fullmatch(r"DC\d{1,3}", chain_id):
            errors.append(
                f"[DIALOGUE_CHAIN_ID_INVALID] {tag}.chain_id "
                "必须使用 DC1、DC2 这类稳定编号"
            )
        elif chain_id in chain_ids:
            errors.append(
                f"[DIALOGUE_CHAIN_ID_INVALID] {tag}.chain_id=「{chain_id}」重复"
            )
        else:
            chain_ids.add(chain_id)
        if len((chain.topic or "").strip()) < 4:
            errors.append(f"{tag}.topic 过短；请写清这组对白围绕的同一话题")
        turns = chain.turns or []
        total_turns += len(turns)
        if not 1 <= len(turns) <= DIALOGUE_CHAIN_TURNS_HARD_MAX:
            errors.append(
                f"[DIALOGUE_CHAIN_LENGTH_INVALID] {tag}.turns "
                f"需包含 1~{DIALOGUE_CHAIN_TURNS_HARD_MAX} 个连续话轮"
            )
            continue
        if turns and (turns[0].function or "").strip() == "response":
            errors.append(f"{tag} 不能从 response 开始；必须先保留触发句/宣布/提问")
        previous_speaker = ""
        matched_indices: list[int] = []
        for turn_index, turn in enumerate(turns):
            turn_tag = f"{tag}.turns[{turn_index}]"
            speaker = (turn.speaker or "").strip()
            line = (turn.line or "").strip()
            function = (turn.function or "").strip()
            source_line = (turn.source_text or "").strip()
            if not speaker:
                errors.append(f"{turn_tag}.speaker 不能为空")
            grounded_short = _is_grounded_short_utterance(line, source_line, source_text)
            if len(_condense(line)) < 2 and not grounded_short:
                errors.append(f"{turn_tag}.line 过短或为空")
            spoken_chars = content_char_count(line)
            if spoken_chars > config.MAX_SPOKEN_CHARS_PER_SHOT:
                errors.append(
                    "[DIALOGUE_TURN_CAPACITY_EXCEEDED] "
                    f"{turn_tag} 纯文字 {spoken_chars} 字，超过最长 "
                    f"{config.VIDEO_DURATION_MAX_S}s 单镜口播上限 "
                    f"{config.MAX_SPOKEN_CHARS_PER_SHOT} 字；"
                    "必须按原文标点拆成同说话人的连续话轮"
                )
            if function not in _DIALOGUE_TURN_FUNCTIONS:
                errors.append(
                    f"[DIALOGUE_FUNCTION_INVALID] {turn_tag}.function=「{function}」非法；只能是 "
                    "trigger|announcement|question|response|decision|statement"
                )
            if function in _DIALOGUE_RESPONSE_FUNCTIONS and (
                turn_index == 0 or not previous_speaker or previous_speaker == speaker
            ):
                errors.append(
                    f"{turn_tag} 是 response，但前一话轮没有另一角色的触发台词"
                )
            if len(_condense(source_line)) < 2 and not grounded_short:
                errors.append(f"{turn_tag}.source_text 不能为空；必须引用原文对白证据")
            else:
                if source_text and (
                    _longest_run_ratio(source_line, source_text) < KEY_LINE_PRESENT_RATIO
                    and _bigram_coverage(source_line, source_text) < KEY_LINE_BIGRAM_COVERAGE
                ):
                    errors.append(
                        f"[SOURCE_EVIDENCE_MISMATCH] {turn_tag}.source_text "
                        f"未在本集原文中找到：{source_line}"
                    )
            candidates = _matching_text_indices(line, full_texts)
            # A short character-address line can fuzzily match an earlier,
            # unrelated utterance that merely contains the same name.  Prefer
            # the declared speaker, then the exact spoken text, before using
            # the ordered fuzzy fallback.  Otherwise a chain fully contained
            # in one scene can be falsely reported as spanning several scenes.
            same_speaker = [
                idx for idx in candidates
                if _condense(full_turns[idx][1]) == _condense(speaker)
            ]
            if same_speaker:
                candidates = same_speaker
            exact_text = [
                idx for idx in candidates
                if _condense(full_turns[idx][2]) == _condense(line)
            ]
            if exact_text:
                candidates = exact_text
            after = [idx for idx in candidates if not matched_indices or idx >= matched_indices[-1]]
            if not after:
                errors.append(f"{turn_tag}.line 未按对白链顺序写进 full_script_text：{line}")
            else:
                matched_indices.append(after[0])
            previous_speaker = speaker
        if matched_indices:
            scenes = {full_turns[idx][0] for idx in matched_indices}
            speakers = {
                _condense(turn.speaker or "")
                for turn in turns
                if _condense(turn.speaker or "")
            }
            # “同一触发→回应链不得跨场”只适用于人物之间的互动链。单人自语/独白
            # 可能因动作节拍被拆成相邻场块，跨块并不会破坏对白因果，不应阻断交付。
            if (
                len(scenes) > 1
                and len(speakers) > 1
                and _dialogue_chain_crosses_hard_scene_boundary(script, scenes)
            ):
                errors.append(f"{tag} 被拆到多个场次；同一触发→回应链必须在同一场完成")

    # 对白密度由本集时长预算和后续逐镜口播容量控制。这里仅保证至少有一组
    # 可追溯的主线对白，不再把“精选台词软建议”误当成整集对白硬上限。
    if total_turns < MIN_KEY_LINES:
        errors.append(
            f"dialogue_chains 共 {total_turns} 个话轮；请至少保留 {MIN_KEY_LINES} 个"
            "推动主线且可追溯的完整话轮"
        )
    first_chain_source = (
        (chains[0].turns[0].source_text or "").strip()
        if chains and chains[0].turns else ""
    )
    first_chain_line = (
        (chains[0].turns[0].line or "").strip()
        if chains and chains[0].turns else ""
    )
    if (
        first_chain_source
        and first_chain_line
        and not textmatch.spoken_digit_sequence_equivalent(
            first_chain_source,
            first_chain_line,
        )
        and _longest_run_ratio(
            first_chain_line,
            first_chain_source,
        ) < KEY_LINE_PRESENT_RATIO
        and _bigram_coverage(
            first_chain_line,
            first_chain_source,
        ) < KEY_LINE_BIGRAM_COVERAGE
    ):
        errors.append(
            "[SOURCE_EVIDENCE_MISMATCH] "
            "dialogue_chains[0].turns[0].source_text 与改编台词语义不匹配："
            f"原文证据「{first_chain_source}」→台词「{first_chain_line}」；"
            "D001 必须引用语义支持首条改编对白的原文话语，"
            "不能强绑整章第一处引号、拟声或已舍弃场景中的无关话语"
        )
    return errors
