"""ScreenplayDocument：可局部修补的权威剧本结构。

兼容现有 EpisodeScreenplay：权威节点在 scene_blocks / dialogue_turns /
story_events / information_ledger / plot_spine；full_script_text /
scene_outline / key_lines 为确定性投影。
"""
from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel, Field

from app.schemas import (
    EpisodeScreenplay,
    InformationItem,
    KeyDialogueChain,
    KeyDialogueTurn,
    PlotSpine,
    ScriptScene,
    StoryEvent,
    VoiceCanonical,
)


class DialogueTurnNode(BaseModel):
    turn_id: str
    chain_id: str = ""
    speaker: str = ""
    line: str = ""
    function: str = "statement"
    source_text: str = ""


class ActionBlockNode(BaseModel):
    action_id: str
    text: str = ""


class SceneBlockNode(BaseModel):
    scene_id: str
    scene_no: int = 1
    scene_heading: str = ""
    story_function: str = ""
    characters: list[str] = Field(default_factory=list)
    summary: str = ""
    conflict: str = ""
    turn: str = ""
    source_basis: str = ""
    action_blocks: list[ActionBlockNode] = Field(default_factory=list)
    dialogue_turns: list[DialogueTurnNode] = Field(default_factory=list)


class ScreenplayMetadata(BaseModel):
    episode_no: int = 0
    mode: str = "full_script"
    title: str = ""
    source_text_range: str = ""
    logline: str = ""
    script_format_note: str = ""
    dramatic_question: str = ""
    protagonist_goal: str = ""
    obstacle: str = ""
    stakes: str = ""
    emotional_curve: str = ""
    ending_hook: str = ""
    source_basis: str = ""
    adaptation_direction: str = ""
    opening: str = ""
    development: str = ""
    conflict: str = ""
    climax: str = ""
    episode_premise: str = ""
    character_state_changes: list[str] = Field(default_factory=list)
    key_plot_points: list[str] = Field(default_factory=list)
    approved_adaptations: list[str] = Field(default_factory=list)
    forbidden_additions: list[str] = Field(default_factory=list)
    id: str | None = None
    created_at: float | None = None
    updated_at: float | None = None


class ScreenplayDocument(BaseModel):
    """权威可修补结构。"""

    screenplay_metadata: ScreenplayMetadata = Field(default_factory=ScreenplayMetadata)
    plot_spine: PlotSpine | None = None
    scene_blocks: list[SceneBlockNode] = Field(default_factory=list)
    story_events: list[StoryEvent] = Field(default_factory=list)
    information_ledger: list[InformationItem] = Field(default_factory=list)
    voice_bible: list[VoiceCanonical] = Field(default_factory=list)
    # 原始 dialogue_chains 保底（与 scene 内 turns 同步）
    dialogue_chains: list[KeyDialogueChain] = Field(default_factory=list)


_SCENE_HEADING_RE = re.compile(r"^【场\s*(\d+)】\s*(.*)$")
_DIALOGUE_RE = re.compile(r"^([^：:]{1,20})(?:（[^）]*）)?[：:]\s*(.+)$")


def screenplay_to_document(script: EpisodeScreenplay) -> ScreenplayDocument:
    meta = ScreenplayMetadata(
        episode_no=script.episode_no,
        mode=script.mode or "full_script",
        title=script.title or "",
        source_text_range=script.source_text_range or "",
        logline=script.logline or "",
        script_format_note=script.script_format_note or "",
        dramatic_question=script.dramatic_question or "",
        protagonist_goal=script.protagonist_goal or "",
        obstacle=script.obstacle or "",
        stakes=script.stakes or "",
        emotional_curve=script.emotional_curve or "",
        ending_hook=script.ending_hook or "",
        source_basis=script.source_basis or "",
        adaptation_direction=script.adaptation_direction or "",
        opening=script.opening or "",
        development=script.development or "",
        conflict=script.conflict or "",
        climax=script.climax or "",
        episode_premise=script.episode_premise or "",
        character_state_changes=list(script.character_state_changes or []),
        key_plot_points=list(script.key_plot_points or []),
        approved_adaptations=list(script.approved_adaptations or []),
        forbidden_additions=list(script.forbidden_additions or []),
        id=script.id,
        created_at=script.created_at,
        updated_at=script.updated_at,
    )
    scene_blocks = _build_scene_blocks(script)
    return ScreenplayDocument(
        screenplay_metadata=meta,
        plot_spine=script.plot_spine,
        scene_blocks=scene_blocks,
        story_events=list(script.events or []),
        information_ledger=list(script.information_ledger or []),
        voice_bible=list(script.voice_bible or []),
        dialogue_chains=list(script.dialogue_chains or []),
    )


def document_to_screenplay(doc: ScreenplayDocument) -> EpisodeScreenplay:
    """从权威文档确定性投影 EpisodeScreenplay（含 full_script_text / scene_outline / key_lines）。"""
    rederived = rederive_projections(doc)
    meta = rederived.screenplay_metadata
    scene_outline = [
        ScriptScene(
            scene_no=block.scene_no,
            scene_heading=block.scene_heading,
            story_function=block.story_function,
            characters=list(block.characters),
            summary=block.summary,
            conflict=block.conflict,
            turn=block.turn,
            source_basis=block.source_basis,
        )
        for block in rederived.scene_blocks
    ]
    key_lines = _key_lines_from_chains(rederived.dialogue_chains)
    full_text = render_full_script_text(rederived)
    return EpisodeScreenplay(
        episode_no=meta.episode_no,
        id=meta.id,
        mode=meta.mode,
        title=meta.title,
        source_text_range=meta.source_text_range,
        logline=meta.logline,
        script_format_note=meta.script_format_note,
        dramatic_question=meta.dramatic_question,
        protagonist_goal=meta.protagonist_goal,
        obstacle=meta.obstacle,
        stakes=meta.stakes,
        key_lines=key_lines,
        dialogue_chains=list(rederived.dialogue_chains),
        key_plot_points=list(meta.key_plot_points),
        plot_spine=rederived.plot_spine,
        scene_outline=scene_outline,
        full_script_text=full_text,
        character_state_changes=list(meta.character_state_changes),
        emotional_curve=meta.emotional_curve,
        ending_hook=meta.ending_hook,
        source_basis=meta.source_basis,
        adaptation_direction=meta.adaptation_direction,
        opening=meta.opening,
        development=meta.development,
        conflict=meta.conflict,
        climax=meta.climax,
        episode_premise=meta.episode_premise,
        events=list(rederived.story_events),
        information_ledger=list(rederived.information_ledger),
        voice_bible=list(rederived.voice_bible),
        approved_adaptations=list(meta.approved_adaptations),
        forbidden_additions=list(meta.forbidden_additions),
        created_at=meta.created_at,
        updated_at=meta.updated_at,
    )


def rederive_projections(doc: ScreenplayDocument) -> ScreenplayDocument:
    """确定性重排编号、同步 dialogue_chains 与 scene turns。"""
    out = ScreenplayDocument.model_validate(copy.deepcopy(doc.model_dump(mode="json")))
    # 重排 scene_no / scene_id
    for idx, block in enumerate(out.scene_blocks, start=1):
        block.scene_no = idx
        if not block.scene_id or not block.scene_id.startswith("SC"):
            block.scene_id = f"SC{idx:02d}"
        for a_idx, action in enumerate(block.action_blocks, start=1):
            if not action.action_id:
                action.action_id = f"AC{idx:02d}-{a_idx:02d}"
        for t_idx, turn in enumerate(block.dialogue_turns, start=1):
            if not turn.turn_id:
                chain = turn.chain_id or f"DC{idx}"
                turn.turn_id = f"{chain}-T{t_idx}"
    # 若 dialogue_chains 空但 scene 有 turns，重建 chains
    if not out.dialogue_chains and any(b.dialogue_turns for b in out.scene_blocks):
        out.dialogue_chains = _chains_from_scene_turns(out.scene_blocks)
    # 若 chains 有值，回填 key 顺序已由 document_to_screenplay 处理
    # 同步 scene turns 的 chain_id
    chain_by_turn: dict[str, str] = {}
    for chain in out.dialogue_chains:
        for t_idx, turn in enumerate(chain.turns, start=1):
            tid = f"{chain.chain_id}-T{t_idx}"
            chain_by_turn[tid] = chain.chain_id
    for block in out.scene_blocks:
        for turn in block.dialogue_turns:
            if turn.turn_id in chain_by_turn:
                turn.chain_id = chain_by_turn[turn.turn_id]
    return out


def render_full_script_text(doc: ScreenplayDocument) -> str:
    """从 scene_blocks 确定性渲染台本正文。"""
    parts: list[str] = []
    for block in doc.scene_blocks:
        heading = block.scene_heading or f"场{block.scene_no}"
        if not heading.startswith("【"):
            heading = f"【场{block.scene_no}】{heading}"
        parts.append(heading)
        if block.action_blocks:
            for action in block.action_blocks:
                if action.text.strip():
                    parts.append(action.text.strip())
        elif block.summary.strip():
            parts.append(block.summary.strip())
        for turn in block.dialogue_turns:
            speaker = (turn.speaker or "旁白").strip()
            line = (turn.line or "").strip()
            if line:
                parts.append(f"{speaker}：{line}")
        parts.append("")  # blank between scenes
    text = "\n".join(parts).strip()
    return text


def apply_field_patch(
    doc: ScreenplayDocument,
    *,
    path: str,
    value: Any,
    target: dict[str, Any] | None = None,
) -> tuple[ScreenplayDocument, list[str]]:
    """在文档副本上替换单字段；返回 (新文档, 触及节点 id 列表)。"""
    data = copy.deepcopy(doc.model_dump(mode="json"))
    touched: list[str] = []
    path = (path or "").strip().lstrip("/")
    target = target or {}

    # metadata fields
    meta_fields = set(ScreenplayMetadata.model_fields)
    if path in meta_fields or path.startswith("screenplay_metadata."):
        key = path.split(".", 1)[-1] if path.startswith("screenplay_metadata.") else path
        data.setdefault("screenplay_metadata", {})[key] = value
        touched.append(f"meta:{key}")
        return ScreenplayDocument.model_validate(data), touched

    # plot_spine fields
    if path.startswith("plot_spine"):
        _set_by_dotted(data, path, value)
        touched.append("plot_spine")
        return ScreenplayDocument.model_validate(data), touched

    kind = (target.get("kind") or "").strip()
    node_id = str(target.get("id") or "").strip()

    if kind in {"screenplay_scene", "scene"} or path.startswith("scene_blocks"):
        block = _find_scene(data, node_id, path)
        if block is None:
            raise KeyError(f"scene not found: {node_id or path}")
        field = path.split(".")[-1] if "." in path else path
        if field in block and field not in {"scene_id", "action_blocks", "dialogue_turns"}:
            block[field] = value
            touched.append(block.get("scene_id") or node_id)
        elif "dialogue_turns" in path or kind == "dialogue_turn":
            turn = _find_turn(block, node_id, path)
            if turn is None:
                raise KeyError(f"dialogue turn not found: {node_id}")
            turn_field = path.split(".")[-1]
            if turn_field in turn:
                turn[turn_field] = value
            else:
                turn["line"] = value if isinstance(value, str) else turn.get("line")
                if isinstance(value, dict):
                    turn.update(value)
            touched.append(turn.get("turn_id") or node_id)
            touched.append(block.get("scene_id") or "")
        else:
            # nested path inside scene
            rel = path.split(".", 1)[-1] if path.startswith("scene_blocks") else path
            if rel in block:
                block[rel] = value
                touched.append(block.get("scene_id") or node_id)
            else:
                raise KeyError(f"unsupported scene path: {path}")
        return ScreenplayDocument.model_validate(data), touched

    if kind == "dialogue_chain_turn":
        chain_id = str(target.get("chain_id") or node_id).strip()
        chain = _find_chain(data, chain_id, f"dialogue_chains.{chain_id}")
        if chain is None:
            raise KeyError(f"dialogue chain not found: {chain_id}")
        turns = chain.get("turns") or []
        turn_index = target.get("turn_index")
        if turn_index is None:
            turn_id = str(target.get("turn_id") or "").strip().upper()
            match = re.search(r"-T(\d+)$", turn_id)
            turn_index = int(match.group(1)) - 1 if match else -1
        turn_index = int(turn_index)
        if not 0 <= turn_index < len(turns):
            raise KeyError(f"dialogue chain turn not found: {chain_id}[{turn_index}]")
        field = path.split(".")[-1]
        if field not in turns[turn_index]:
            raise KeyError(f"unsupported dialogue chain turn field: {field}")
        turns[turn_index][field] = value
        touched.append(f"{chain_id}-T{turn_index + 1}")
        touched.append(chain_id)
        return ScreenplayDocument.model_validate(data), touched

    if kind in {"dialogue_turn", "dialogue_chain"} or "dialogue" in path:
        # search all scenes / chains
        if kind == "dialogue_chain" or path.startswith("dialogue_chains"):
            chain = _find_chain(data, node_id, path)
            if chain is None:
                raise KeyError(f"dialogue chain not found: {node_id}")
            field = path.split(".")[-1]
            if field in chain:
                chain[field] = value
            elif isinstance(value, dict):
                chain.update(value)
            else:
                raise KeyError(f"unsupported chain path: {path}")
            touched.append(chain.get("chain_id") or node_id)
            return ScreenplayDocument.model_validate(data), touched
        for block in data.get("scene_blocks") or []:
            turn = _find_turn(block, node_id, path)
            if turn is not None:
                field = path.split(".")[-1]
                if field in turn:
                    turn[field] = value
                elif isinstance(value, str):
                    turn["line"] = value
                elif isinstance(value, dict):
                    turn.update(value)
                touched.append(turn.get("turn_id") or node_id)
                touched.append(block.get("scene_id") or "")
                return ScreenplayDocument.model_validate(data), touched
        raise KeyError(f"dialogue turn not found: {node_id or path}")

    if kind in {"story_event", "event"} or path.startswith("story_events") or path.startswith("events"):
        events = data.setdefault("story_events", [])
        event = _find_by_id(events, "event_id", node_id)
        if event is None and node_id:
            raise KeyError(f"event not found: {node_id}")
        if event is None:
            # path like events.0.field
            _set_by_dotted(data, path.replace("events.", "story_events."), value)
            touched.append(node_id or "story_events")
        else:
            field = path.split(".")[-1]
            if field in event:
                event[field] = value
            elif isinstance(value, dict):
                event.update(value)
            touched.append(event.get("event_id") or node_id)
        return ScreenplayDocument.model_validate(data), touched

    if kind in {"information", "ledger"} or path.startswith("information_ledger"):
        items = data.setdefault("information_ledger", [])
        item = _find_by_id(items, "info_id", node_id)
        if item is None:
            raise KeyError(f"ledger item not found: {node_id}")
        field = path.split(".")[-1]
        if field in item:
            item[field] = value
        elif isinstance(value, dict):
            item.update(value)
        touched.append(item.get("info_id") or node_id)
        return ScreenplayDocument.model_validate(data), touched

    # generic dotted set on document root
    _set_by_dotted(data, path, value)
    touched.append(path.split(".")[0] or path)
    return ScreenplayDocument.model_validate(data), touched


def _build_scene_blocks(script: EpisodeScreenplay) -> list[SceneBlockNode]:
    blocks: list[SceneBlockNode] = []
    # Prefer scene_outline as structural skeleton
    if script.scene_outline:
        parsed_body = _parse_full_script_scenes(script.full_script_text or "")
        for scene in script.scene_outline:
            sid = f"SC{int(scene.scene_no):02d}"
            body = parsed_body.get(int(scene.scene_no), {"actions": [], "turns": []})
            actions = [
                ActionBlockNode(action_id=f"AC{int(scene.scene_no):02d}-{i:02d}", text=t)
                for i, t in enumerate(body.get("actions") or [], start=1)
            ]
            turns = [
                DialogueTurnNode(
                    turn_id=f"DC{int(scene.scene_no)}-T{i}",
                    chain_id=f"DC{int(scene.scene_no)}",
                    speaker=t.get("speaker", ""),
                    line=t.get("line", ""),
                    function=t.get("function", "statement"),
                    source_text=t.get("source_text", ""),
                )
                for i, t in enumerate(body.get("turns") or [], start=1)
            ]
            # If no body turns, project from dialogue_chains proportionally later
            blocks.append(SceneBlockNode(
                scene_id=sid,
                scene_no=int(scene.scene_no),
                scene_heading=scene.scene_heading or "",
                story_function=scene.story_function or "",
                characters=list(scene.characters or []),
                summary=scene.summary or "",
                conflict=scene.conflict or "",
                turn=scene.turn or "",
                source_basis=scene.source_basis or "",
                action_blocks=actions,
                dialogue_turns=turns,
            ))
        # If scenes have no dialogue but chains exist, attach all turns to first scene with capacity
        if script.dialogue_chains and all(not b.dialogue_turns for b in blocks):
            all_turns: list[DialogueTurnNode] = []
            for chain in script.dialogue_chains:
                for idx, turn in enumerate(chain.turns, start=1):
                    all_turns.append(DialogueTurnNode(
                        turn_id=f"{chain.chain_id}-T{idx}",
                        chain_id=chain.chain_id,
                        speaker=turn.speaker,
                        line=turn.line,
                        function=turn.function,
                        source_text=turn.source_text,
                    ))
            if blocks and all_turns:
                # distribute round-robin-ish: put into scenes evenly
                per = max(1, (len(all_turns) + len(blocks) - 1) // len(blocks))
                for i, block in enumerate(blocks):
                    chunk = all_turns[i * per:(i + 1) * per]
                    block.dialogue_turns = chunk
        return blocks

    # Fallback: parse full_script_text only
    parsed = _parse_full_script_scenes(script.full_script_text or "")
    if not parsed:
        return [
            SceneBlockNode(
                scene_id="SC01",
                scene_no=1,
                scene_heading="【场1】",
                summary=(script.full_script_text or "")[:200],
                action_blocks=[ActionBlockNode(action_id="AC01-01", text=script.full_script_text or "")],
            )
        ]
    for scene_no, body in sorted(parsed.items()):
        blocks.append(SceneBlockNode(
            scene_id=f"SC{scene_no:02d}",
            scene_no=scene_no,
            scene_heading=body.get("heading") or f"【场{scene_no}】",
            action_blocks=[
                ActionBlockNode(action_id=f"AC{scene_no:02d}-{i:02d}", text=t)
                for i, t in enumerate(body.get("actions") or [], start=1)
            ],
            dialogue_turns=[
                DialogueTurnNode(
                    turn_id=f"DC{scene_no}-T{i}",
                    chain_id=f"DC{scene_no}",
                    speaker=t.get("speaker", ""),
                    line=t.get("line", ""),
                )
                for i, t in enumerate(body.get("turns") or [], start=1)
            ],
        ))
    return blocks


def _parse_full_script_scenes(text: str) -> dict[int, dict[str, Any]]:
    scenes: dict[int, dict[str, Any]] = {}
    current: int | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _SCENE_HEADING_RE.match(line)
        if heading:
            current = int(heading.group(1))
            scenes[current] = {
                "heading": line,
                "actions": [],
                "turns": [],
            }
            rest = heading.group(2).strip()
            if rest and "／" not in rest and "/" not in rest:
                # keep heading only
                pass
            continue
        if current is None:
            current = 1
            scenes.setdefault(1, {"heading": "【场1】", "actions": [], "turns": []})
        dlg = _DIALOGUE_RE.match(line)
        if dlg:
            scenes[current]["turns"].append({
                "speaker": dlg.group(1).strip(),
                "line": dlg.group(2).strip(),
            })
        else:
            scenes[current]["actions"].append(line)
    return scenes


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


def _chains_from_scene_turns(blocks: list[SceneBlockNode]) -> list[KeyDialogueChain]:
    by_chain: dict[str, list[DialogueTurnNode]] = {}
    for block in blocks:
        for turn in block.dialogue_turns:
            cid = turn.chain_id or block.scene_id or "DC1"
            by_chain.setdefault(cid, []).append(turn)
    chains: list[KeyDialogueChain] = []
    for cid, turns in by_chain.items():
        chains.append(KeyDialogueChain(
            chain_id=cid,
            topic="",
            turns=[
                KeyDialogueTurn(
                    speaker=t.speaker,
                    line=t.line,
                    function=t.function,
                    source_text=t.source_text,
                )
                for t in turns
            ],
        ))
    return chains


def _set_by_dotted(data: dict[str, Any], path: str, value: Any) -> None:
    parts = [p for p in path.replace("/", ".").split(".") if p]
    cur: Any = data
    for part in parts[:-1]:
        if part.isdigit():
            idx = int(part)
            cur = cur[idx]
        else:
            if part not in cur or cur[part] is None:
                cur[part] = {}
            cur = cur[part]
    last = parts[-1]
    if last.isdigit():
        cur[int(last)] = value
    else:
        cur[last] = value


def _find_scene(data: dict, node_id: str, path: str) -> dict | None:
    blocks = data.get("scene_blocks") or []
    if node_id:
        for b in blocks:
            if b.get("scene_id") == node_id or str(b.get("scene_no")) == node_id.lstrip("SC0"):
                return b
    m = re.search(r"SC(\d+)", path, re.I)
    if m:
        want = int(m.group(1))
        for b in blocks:
            if int(b.get("scene_no") or 0) == want or b.get("scene_id") == f"SC{want:02d}":
                return b
    m2 = re.search(r"scene_blocks\.(\d+)", path)
    if m2:
        idx = int(m2.group(1))
        if 0 <= idx < len(blocks):
            return blocks[idx]
    return None


def _find_turn(block: dict, node_id: str, path: str) -> dict | None:
    turns = block.get("dialogue_turns") or []
    if node_id:
        for t in turns:
            if t.get("turn_id") == node_id:
                return t
    m = re.search(r"(DC\d+-T\d+)", path, re.I)
    if m:
        want = m.group(1).upper()
        for t in turns:
            if str(t.get("turn_id", "")).upper() == want:
                return t
    return None


def _find_chain(data: dict, node_id: str, path: str) -> dict | None:
    chains = data.get("dialogue_chains") or []
    if node_id:
        for c in chains:
            if c.get("chain_id") == node_id:
                return c
    m = re.search(r"(DC\d+)", path, re.I)
    if m:
        want = m.group(1).upper()
        for c in chains:
            if str(c.get("chain_id", "")).upper() == want:
                return c
    return None


def _find_by_id(items: list[dict], key: str, node_id: str) -> dict | None:
    if not node_id:
        return None
    for item in items:
        if str(item.get(key) or "") == node_id:
            return item
    return None
