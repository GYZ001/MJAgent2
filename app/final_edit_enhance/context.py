"""编排计划的输入上下文：本集节拍表/各段梗概/台词/原文/人物谱，从已持久化
数据现取，不新造第二份读取逻辑。

「段」= ``shot_no``：分镜台 2.x 契约下一行 shot 就是一个 15 秒叙事段
（``storyboard_pack_segment`` 非 None），``prompt_text`` 即该段梗概/视觉描述。
旧版（非 2.x）shot 契约没有这个字段时，该段梗概退化为空串——仍然可用于
时间轴与对白，只是喂给模型的段落描述更简略，不阻断整个增强流程（本次试点
范围本就只覆盖 2.x 契约的项目）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.schemas import Bible, character_is_portrait_eligible
from app.source_chapters import _episode_source_text


@dataclass(frozen=True)
class DialogueLineContext:
    utterance_id: str
    speaker: str
    text: str


@dataclass(frozen=True)
class ShotContext:
    shot_no: int
    start_s: float
    duration_s: float
    prompt_text: str
    dialogue: tuple[DialogueLineContext, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EpisodeContext:
    shots: tuple[ShotContext, ...]
    total_duration_s: float
    source_text: str
    character_roster: tuple[str, ...]


def _shot_contract(row: Any) -> dict[str, Any] | None:
    try:
        contract = json.loads(row["shot_contract_json"] or "null")
    except (TypeError, ValueError):
        return None
    return contract if isinstance(contract, dict) else None


def _dialogue_from_segment(segment: dict[str, Any]) -> tuple[DialogueLineContext, ...]:
    lines = []
    for index, item in enumerate(segment.get("dialogue") or [], start=1):
        if not isinstance(item, dict):
            continue
        lines.append(DialogueLineContext(
            utterance_id=str(item.get("utterance_id") or f"U{index:02d}"),
            speaker=str(item.get("speaker_identity_id") or ""),
            text=str(item.get("line") or ""),
        ))
    return tuple(lines)


def _project_bible(conn: Any, project_id: str) -> Bible | None:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return None
    return Bible.model_validate(json.loads(row["bible_json"]))


def _character_roster(bible: Bible | None) -> tuple[str, ...]:
    if bible is None:
        return ()
    return tuple(c.name for c in bible.characters if character_is_portrait_eligible(c))


def build_episode_context(
    conn: Any, ep_row: Any, piece_specs: list[tuple[int, str, float]],
) -> EpisodeContext:
    """``piece_specs``：本次实际入选合成的 (shot_no, path, playback_rate) 列表，
    与 ``app.media_exec.concat.concatenate_episode`` 用的是同一份——只有真正
    进最终成片的段才会出现在编排计划的时间轴里，跳过的镜头不会被误当作
    "这段没有台词"的静默区。"""
    shot_rows = {
        int(row["shot_no"]): row
        for row in conn.execute(
            "SELECT * FROM shots WHERE episode_id=?", (ep_row["id"],),
        ).fetchall()
    }
    shots: list[ShotContext] = []
    cursor = 0.0
    for shot_no, _path, rate in piece_specs:
        row = shot_rows.get(shot_no)
        if row is None:
            continue
        nominal_duration_s = float(row["duration_s"] or 0.0)
        effective_duration_s = max(0.05, nominal_duration_s / max(rate, 1e-6))
        contract = _shot_contract(row)
        segment = contract.get("storyboard_pack_segment") if contract else None
        prompt_text = str((segment or {}).get("prompt_text") or "")
        dialogue = _dialogue_from_segment(segment) if segment else ()
        shots.append(ShotContext(
            shot_no=shot_no, start_s=cursor, duration_s=effective_duration_s,
            prompt_text=prompt_text, dialogue=dialogue,
        ))
        cursor += effective_duration_s
    bible = _project_bible(conn, ep_row["project_id"])
    return EpisodeContext(
        shots=tuple(shots), total_duration_s=cursor,
        source_text=_episode_source_text(conn, ep_row),
        character_roster=_character_roster(bible),
    )
