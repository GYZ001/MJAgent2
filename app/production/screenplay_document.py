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

from app import textmatch
from app.schemas import (
    EpisodeScreenplay,
    InformationItem,
    KeyDialogueChain,
    KeyDialogueTurn,
    NarrativeContinuityPlan,
    PlotSpine,
    PlotSpineBeat,
    ScriptScene,
    SourceCoverageDecision,
    StoryEvent,
    VoiceCanonical,
)


class DialogueSceneBindingError(ValueError):
    """A structured dialogue chain names a scene absent from the document."""


def _declared_dialogue_scene(
    scene_blocks: list[SceneBlockNode],
    chain: KeyDialogueChain,
) -> SceneBlockNode | None:
    """Resolve an explicit scene binding; only an empty binding may fall back."""
    scene_id = (chain.scene_id or "").strip()
    if not scene_id:
        return None
    block = next(
        (item for item in scene_blocks if item.scene_id == scene_id),
        None,
    )
    if block is None:
        raise DialogueSceneBindingError(
            f"dialogue_chain {chain.chain_id or '<unknown>'} 引用不存在的 "
            f"scene_id={scene_id}；仅 scene_id 为空时允许语义回退",
        )
    return block


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
    previous_scene_exit_state: str = ""
    opening_image: str = ""
    agency_contracts: list[dict[str, str]] = Field(default_factory=list)
    entry_state: str = ""
    exit_state: str = ""
    context_requirements: list[str] = Field(default_factory=list)
    action_blocks: list[ActionBlockNode] = Field(default_factory=list)
    dialogue_turns: list[DialogueTurnNode] = Field(default_factory=list)
    # Stable references preserving the authored action/dialogue interleave.
    # Historical documents leave this empty and retain the legacy
    # actions-then-dialogues projection.
    body_order: list[str] = Field(default_factory=list)


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
    # 叙事图是剧本文档的权威内容，不是可从散文投影重建的缓存。
    # 因此 EpisodeScreenplay <-> ScreenplayDocument 必须完整往返它。
    narrative_plan: NarrativeContinuityPlan | None = None
    plot_spine: PlotSpine | None = None
    source_coverage: list[SourceCoverageDecision] = Field(default_factory=list)
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
        narrative_plan=(
            script.narrative_plan.model_copy(deep=True)
            if script.narrative_plan is not None
            else None
        ),
        plot_spine=script.plot_spine,
        source_coverage=list(script.source_coverage or []),
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
            previous_scene_exit_state=block.previous_scene_exit_state,
            opening_image=block.opening_image,
            agency_contracts=list(block.agency_contracts),
            entry_state=block.entry_state,
            exit_state=block.exit_state,
            context_requirements=list(block.context_requirements),
        )
        for block in rederived.scene_blocks
    ]
    full_text = render_full_script_text(rederived)
    from app.validators import derive_key_lines

    key_lines = derive_key_lines(rederived.dialogue_chains, full_text)
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
        source_coverage=list(rederived.source_coverage),
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
        narrative_plan=(
            rederived.narrative_plan.model_copy(deep=True)
            if rederived.narrative_plan is not None
            else None
        ),
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
    _remove_cross_scene_prefixed_duplicates(out)
    # 若 dialogue_chains 空但 scene 有 turns，重建 chains
    if not out.dialogue_chains and any(b.dialogue_turns for b in out.scene_blocks):
        out.dialogue_chains = _chains_from_scene_turns(out.scene_blocks)
    if out.dialogue_chains and out.scene_blocks:
        _sync_dialogue_chains_into_scenes(out)
        _remove_actions_projected_as_dialogue(out)
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
        _normalize_scene_body_order(block)
    return out


def _normalize_scene_body_order(block: SceneBlockNode) -> None:
    if not block.body_order:
        return
    known = {
        *(item.action_id for item in block.action_blocks if item.action_id),
        *(item.turn_id for item in block.dialogue_turns if item.turn_id),
    }
    normalized = list(dict.fromkeys(
        value for value in block.body_order if value in known
    ))
    normalized.extend(
        item.action_id
        for item in block.action_blocks
        if item.action_id and item.action_id not in normalized
    )
    normalized.extend(
        item.turn_id
        for item in block.dialogue_turns
        if item.turn_id and item.turn_id not in normalized
    )
    block.body_order = normalized


def _dialogue_identity(value: str) -> str:
    return re.sub(r"[\s，。！？；：、,.!?;:'\"“”‘’（）()《》【】\-—…]+", "", value or "")


def action_block_spoken_identity(text: str) -> tuple[str, str] | None:
    """Parse legacy ``角色（表演），台词`` prose into a spoken identity."""
    match = re.match(
        r"^(?P<speaker>[^，,：:。！？!?\n（）()]{1,16})"
        r"(?:[（(][^）)\n]{1,12}[）)])?\s*[，,：:]\s*(?P<line>\S.+)$",
        (text or "").strip(),
    )
    if match is None:
        return None
    return match.group("speaker").strip(), match.group("line").strip()


def _node_index(nodes: list[DialogueTurnNode], target: DialogueTurnNode) -> int:
    return next((index for index, node in enumerate(nodes) if node is target), len(nodes))


def _remove_cross_scene_prefixed_duplicates(doc: ScreenplayDocument) -> None:
    """Drop a legacy injected turn whose line repeats its speaker and another body turn."""
    canonical_turns = {
        (_dialogue_identity(turn.speaker), _dialogue_identity(turn.line))
        for block in doc.scene_blocks
        for turn in block.dialogue_turns
        if (turn.speaker or "").strip() and (turn.line or "").strip()
        and not re.match(
            rf"^{re.escape((turn.speaker or '').strip())}\s*[：:]\s*",
            (turn.line or "").strip(),
        )
    }
    for block in doc.scene_blocks:
        retained: list[DialogueTurnNode] = []
        for turn in block.dialogue_turns:
            speaker = (turn.speaker or "").strip()
            line = (turn.line or "").strip()
            prefix = re.match(
                rf"^{re.escape(speaker)}\s*[：:]\s*" if speaker else r"$^",
                line,
            )
            if prefix is not None:
                unprefixed = line[prefix.end():].strip()
                identity = (
                    _dialogue_identity(speaker),
                    _dialogue_identity(unprefixed),
                )
                if all(identity) and identity in canonical_turns:
                    continue
            retained.append(turn)
        block.dialogue_turns = retained


def _remove_actions_projected_as_dialogue(doc: ScreenplayDocument) -> None:
    """Once a cue-prefixed action becomes a chain turn, keep only the dialogue node."""
    for block in doc.scene_blocks:
        dialogue_identities = {
            (
                _dialogue_identity(turn.speaker),
                _dialogue_identity(turn.line),
            )
            for turn in block.dialogue_turns
            if (turn.speaker or "").strip() and (turn.line or "").strip()
        }
        retained: list[ActionBlockNode] = []
        for action in block.action_blocks:
            spoken = action_block_spoken_identity(action.text)
            if spoken is not None:
                identity = tuple(_dialogue_identity(value) for value in spoken)
                if all(identity) and identity in dialogue_identities:
                    continue
            retained.append(action)
        block.action_blocks = retained


def _best_scene_for_unmatched_chain_turn(
    scene_blocks: list[SceneBlockNode],
    chain: KeyDialogueChain,
    turn: KeyDialogueTurn,
) -> SceneBlockNode:
    """Place a projection gap in the closest existing scene by document semantics."""
    speaker = _dialogue_identity(turn.speaker)
    speaker_scenes = [
        block
        for block in scene_blocks
        if speaker
        and speaker in {
            _dialogue_identity(character)
            for character in block.characters
        }
    ]
    candidates = speaker_scenes or scene_blocks
    queries = [
        str(chain.topic or "").strip(),
        str(turn.line or "").strip(),
        str(turn.source_text or "").strip(),
    ]

    def rank(block: SceneBlockNode) -> tuple[float, float, float, int]:
        scene_text = " ".join([
            block.scene_heading,
            block.story_function,
            block.summary,
            block.conflict,
            block.turn,
            block.source_basis,
            *(action.text for action in block.action_blocks),
            *(
                f"{item.speaker} {item.line}"
                for item in block.dialogue_turns
            ),
        ])
        scores = [
            max(
                textmatch.longest_run_ratio(query, scene_text),
                textmatch.bigram_coverage(query, scene_text),
            )
            if query else 0.0
            for query in queries
        ]
        return (
            scores[0] * 2.0 + scores[1] + scores[2],
            scores[0],
            max(scores[1:]),
            -block.scene_no,
        )

    return max(candidates, key=rank)


def _sync_dialogue_chains_into_scenes(doc: ScreenplayDocument) -> None:
    """Project authoritative chain turns into scene dialogue without rewriting prose."""
    matches: dict[tuple[int, int], tuple[SceneBlockNode, DialogueTurnNode]] = {}
    used_nodes: set[int] = set()

    # Bind body dialogue to authoritative turns first. Punctuation differences
    # are harmless; speaker + spoken text must still match.
    for chain_index, chain in enumerate(doc.dialogue_chains):
        declared_block = _declared_dialogue_scene(doc.scene_blocks, chain)
        for turn_index, turn in enumerate(chain.turns or []):
            speaker = _dialogue_identity(turn.speaker)
            line = _dialogue_identity(turn.line)
            if not speaker or not line:
                continue
            found: tuple[SceneBlockNode, DialogueTurnNode] | None = None
            for block in doc.scene_blocks:
                for node in block.dialogue_turns:
                    if id(node) in used_nodes:
                        continue
                    if (
                        _dialogue_identity(node.speaker) == speaker
                        and _dialogue_identity(node.line) == line
                    ):
                        found = (block, node)
                        break
                if found:
                    break
            if found is None:
                continue
            block, node = found
            if declared_block is not None and block is not declared_block:
                block.dialogue_turns.remove(node)
                block.body_order = [
                    item for item in block.body_order if item != node.turn_id
                ]
                declared_block.dialogue_turns.append(node)
                if declared_block.body_order:
                    declared_block.body_order.append(node.turn_id)
                block = declared_block
            previous_turn_id = node.turn_id
            node.turn_id = f"{chain.chain_id}-T{turn_index + 1}"
            block.body_order = [
                node.turn_id if item == previous_turn_id else item
                for item in block.body_order
            ]
            node.chain_id = chain.chain_id
            node.function = turn.function
            node.source_text = turn.source_text
            used_nodes.add(id(node))
            matches[(chain_index, turn_index)] = (block, node)

    # A chain turn missing from the body is a projection gap, not new creative
    # content. Insert it beside the nearest matched sibling from the same chain.
    for chain_index, chain in enumerate(doc.dialogue_chains):
        declared_block = _declared_dialogue_scene(doc.scene_blocks, chain)
        turns = list(chain.turns or [])
        for turn_index, turn in enumerate(turns):
            key = (chain_index, turn_index)
            if key in matches or not (turn.speaker or "").strip() or not (turn.line or "").strip():
                continue
            previous = next(
                (
                    matches[(chain_index, index)]
                    for index in range(turn_index - 1, -1, -1)
                    if (chain_index, index) in matches
                ),
                None,
            )
            following = next(
                (
                    matches[(chain_index, index)]
                    for index in range(turn_index + 1, len(turns))
                    if (chain_index, index) in matches
                ),
                None,
            )
            if previous is not None:
                block, sibling = previous
                insert_at = _node_index(block.dialogue_turns, sibling) + 1
            elif following is not None:
                block, sibling = following
                insert_at = _node_index(block.dialogue_turns, sibling)
            else:
                block = declared_block or _best_scene_for_unmatched_chain_turn(
                    doc.scene_blocks, chain, turn,
                )
                insert_at = len(block.dialogue_turns)
            node = DialogueTurnNode(
                turn_id=f"{chain.chain_id}-T{turn_index + 1}",
                chain_id=chain.chain_id,
                speaker=turn.speaker,
                line=turn.line,
                function=turn.function,
                source_text=turn.source_text,
            )
            block.dialogue_turns.insert(insert_at, node)
            sibling_id = sibling.turn_id if previous is not None or following is not None else ""
            if block.body_order:
                if sibling_id and sibling_id in block.body_order:
                    order_at = block.body_order.index(sibling_id)
                    if previous is not None:
                        order_at += 1
                    block.body_order.insert(order_at, node.turn_id)
                else:
                    block.body_order.append(node.turn_id)
            matches[key] = (block, node)

    # Baseline prose can contain the same spoken line twice, for example once
    # with a performance cue and once as the authoritative chain projection.
    # Keep repeated authoritative turns, but discard non-authoritative copies
    # of a speaker + line already present in the same scene.
    authoritative_nodes = {
        id(node)
        for _block, node in matches.values()
    }
    authoritative_by_line: dict[str, list[DialogueTurnNode]] = {}
    for _block, node in matches.values():
        line = _dialogue_identity(node.line)
        if line:
            authoritative_by_line.setdefault(line, []).append(node)
    for block in doc.scene_blocks:
        seen: set[tuple[str, str]] = set()
        deduped: list[DialogueTurnNode] = []
        for node in block.dialogue_turns:
            identity = (
                _dialogue_identity(node.speaker),
                _dialogue_identity(node.line),
            )
            if id(node) not in authoritative_nodes and identity[1]:
                covered_by = {
                    id(authoritative)
                    for line, authoritative_turns in authoritative_by_line.items()
                    if len(line) >= 4 and identity[1].startswith(line)
                    for authoritative in authoritative_turns
                }
                if len(covered_by) == 1:
                    # The prose parser can mistake a performance cue such as
                    # "角色不耐烦地说：" for a new speaker, then preserve the
                    # same authoritative line plus trailing action as another
                    # turn. The typed chain owns the spoken content.
                    continue
            if (
                all(identity)
                and identity in seen
                and id(node) not in authoritative_nodes
            ):
                continue
            deduped.append(node)
            if all(identity):
                seen.add(identity)
        block.dialogue_turns = deduped


def render_full_script_text(doc: ScreenplayDocument) -> str:
    """从 scene_blocks 确定性渲染台本正文。"""
    parts: list[str] = []
    for block in doc.scene_blocks:
        heading = block.scene_heading or f"场{block.scene_no}"
        if not heading.startswith("【"):
            heading = f"【场{block.scene_no}】{heading}"
        parts.append(heading)
        if not block.body_order:
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
            parts.append("")
            continue
        action_by_id = {
            action.action_id: action for action in block.action_blocks
        }
        turn_by_id = {
            turn.turn_id: turn for turn in block.dialogue_turns
        }
        emitted: set[str] = set()

        def emit_action(action: ActionBlockNode) -> None:
            if action.text.strip():
                parts.append(action.text.strip())

        def emit_turn(turn: DialogueTurnNode) -> None:
            speaker = (turn.speaker or "旁白").strip()
            line = (turn.line or "").strip()
            if line:
                parts.append(f"{speaker}：{line}")

        for node_id in block.body_order:
            if node_id in emitted:
                continue
            if node_id in action_by_id:
                emit_action(action_by_id[node_id])
                emitted.add(node_id)
            elif node_id in turn_by_id:
                emit_turn(turn_by_id[node_id])
                emitted.add(node_id)
        for action in block.action_blocks:
            if action.action_id not in emitted:
                emit_action(action)
                emitted.add(action.action_id)
        if not block.action_blocks and block.summary.strip():
            parts.append(block.summary.strip())
        for turn in block.dialogue_turns:
            if turn.turn_id not in emitted:
                emit_turn(turn)
                emitted.add(turn.turn_id)
        parts.append("")  # blank between scenes
    text = "\n".join(parts).strip()
    return text


_SPINE_BEAT_ALIAS_RE = re.compile(r"spine_beats?[\[._-](\d+)\]?", re.I)


def _spine_beat_alias_index(raw: str) -> int | None:
    """Recover a 0-based spine_beats list index from a malformed id alias.

    The repair loop only ever sees the beat's real ``beat_id`` (e.g. "S02")
    inside a document excerpt, or the 0-based list index inside a validator
    message such as ``plot_spine.spine_beats[221].does 过短`` (see
    ``validate_plot_spine``'s ``tag = f"plot_spine.spine_beats[{i}]"`` and the
    matching read-side excerpt in ``screenplay_repair._ISSUE_TARGET_CONTAINERS``
    / ``_ISSUE_TARGET_INDEX_RE``, which already treat that ``[i]`` as a
    0-based ``enumerate(beats)`` index).  When a model echoes that index back
    as the patch target id instead of the real beat_id, it shows up as
    "spine_beats[221]", "spine_beat_221" or a bare "221" — all three encode
    the same 0-based index, so this reuses that one established convention
    instead of inventing new fuzzy matching.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    match = _SPINE_BEAT_ALIAS_RE.search(raw)
    if match:
        return int(match.group(1))
    if raw.isdigit():
        return int(raw)
    return None


def _resolve_spine_beat_id(beat_ids: list[str], raw: str) -> str | None:
    """Map an id/path fragment to a real beat_id, exact match first."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw in beat_ids:
        return raw
    index = _spine_beat_alias_index(raw)
    if index is not None and 0 <= index < len(beat_ids):
        return beat_ids[index]
    return None


def resolve_field_patch_target(
    doc: ScreenplayDocument,
    *,
    path: str,
    target: dict[str, Any] | None,
) -> dict[str, Any]:
    """Infer a direct field owner from stable IDs and the live document schema."""
    resolved = dict(target or {})
    if resolved.get("kind") == "narrative_node" or path.startswith("narrative_plan."):
        return resolved
    node_id = str(
        resolved.get("id")
        or resolved.get("turn_id")
        or resolved.get("chain_id")
        or ""
    ).strip()
    field = re.split(r"[./]+", (path or "").strip("/"))[-1]
    if not field:
        return resolved

    candidates: list[dict[str, Any]] = []

    def add(kind: str, identity: str, model: BaseModel, **extra: Any) -> None:
        if node_id and identity != node_id:
            return
        if field not in type(model).model_fields:
            return
        candidates.append({
            **resolved,
            "kind": kind,
            "id": identity,
            **extra,
        })

    metadata_id = node_id.removeprefix("meta:")
    if field in ScreenplayMetadata.model_fields and metadata_id in {"", field}:
        candidates.append({**resolved, "kind": "metadata", "id": field})

    spine = doc.plot_spine
    if spine is not None and spine.spine_beats and field in PlotSpineBeat.model_fields:
        beat_ids = [beat.beat_id for beat in spine.spine_beats]
        # target.id may be an alias of the real beat_id (see
        # _resolve_spine_beat_id); fall back to the same alias search over
        # `path` so a fully-qualified dotted path (e.g.
        # "plot_spine.spine_beats[221].does") resolves without a target dict.
        matched_beat_id = (
            _resolve_spine_beat_id(beat_ids, node_id)
            or _resolve_spine_beat_id(beat_ids, path)
        )
        for beat in spine.spine_beats:
            if matched_beat_id is not None:
                if beat.beat_id != matched_beat_id:
                    continue
            elif node_id and beat.beat_id != node_id:
                continue
            candidates.append({**resolved, "kind": "spine_beat", "id": beat.beat_id})

    for coverage in doc.source_coverage:
        add("source_coverage", coverage.source_segment_id, coverage)

    for block in doc.scene_blocks:
        add("scene", block.scene_id, block)
        for action in block.action_blocks:
            add(
                "action_block",
                action.action_id,
                action,
                scene_id=block.scene_id,
            )
        for turn in block.dialogue_turns:
            add(
                "dialogue_turn",
                turn.turn_id,
                turn,
                scene_id=block.scene_id,
            )

    for chain in doc.dialogue_chains:
        add("dialogue_chain", chain.chain_id, chain, chain_id=chain.chain_id)
        for turn_index, turn in enumerate(chain.turns or []):
            turn_id = f"{chain.chain_id}-T{turn_index + 1}"
            add(
                "dialogue_chain_turn",
                turn_id,
                turn,
                turn_id=turn_id,
                chain_id=chain.chain_id,
                turn_index=turn_index,
            )

    for event in doc.story_events:
        add("story_event", event.event_id, event)
    for item in doc.information_ledger:
        add("information", item.info_id, item)
    for voice in doc.voice_bible:
        add("voice", voice.speaker_id, voice)

    unique = {
        (
            candidate.get("kind"),
            candidate.get("id"),
            candidate.get("scene_id"),
            candidate.get("turn_index"),
        ): candidate
        for candidate in candidates
    }
    if len(unique) == 1:
        return next(iter(unique.values()))
    return resolved


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

    # plot_spine scalar fields (episode_premise / must_keep_ending / drop_list).
    # Indexed beat paths ("plot_spine.spine_beats[N].*") and target.kind ==
    # "spine_beat" must NOT take this shortcut: _set_by_dotted has no notion
    # of a beat's identity, so it used to silently stash the value under a
    # bogus extra key that ScreenplayDocument.model_validate then dropped.
    # Route those to the dedicated spine_beat branch below instead.
    targets_spine_beat = (
        str(target.get("kind") or "").strip() == "spine_beat"
        or bool(re.search(r"spine_beats(?:\[\d+\]|\.\d+)", path))
    )
    if path.startswith("plot_spine") and not targets_spine_beat:
        _set_by_dotted(data, path, value)
        touched.append("plot_spine")
        return ScreenplayDocument.model_validate(data), touched

    # Unified narrative graph node.  The collection and node identity come
    # from the schema/issue evidence; story words never participate in routing.
    # This keeps post-QA repair granular without replacing the whole graph.
    kind = (target.get("kind") or "").strip()
    if kind == "narrative_node" or path.startswith("narrative_plan."):
        plan = data.get("narrative_plan")
        if not isinstance(plan, dict):
            raise KeyError("narrative_plan not found")
        collection = str(target.get("collection") or "").strip()
        if not collection and path.startswith("narrative_plan."):
            parts = path.split(".")
            collection = parts[1] if len(parts) > 1 else ""
        nodes = plan.get(collection)
        if not isinstance(nodes, list):
            raise KeyError(f"narrative collection not found: {collection}")
        node_id = str(target.get("id") or "").strip()

        def find_nested(value: Any) -> dict[str, Any] | None:
            if isinstance(value, dict):
                if any(
                    key.endswith("_id") and str(candidate or "") == node_id
                    for key, candidate in value.items()
                ):
                    return value
                for child in value.values():
                    found = find_nested(child)
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = find_nested(child)
                    if found is not None:
                        return found
            return None

        node = find_nested(nodes)
        if node is None:
            raise KeyError(f"narrative node not found: {collection}/{node_id}")
        field = path.split(".")[-1]
        if field not in node:
            raise KeyError(f"unsupported narrative node field: {field}")
        node[field] = value
        touched.append(f"narrative:{collection}:{node_id}")
        return ScreenplayDocument.model_validate(data), touched

    target = resolve_field_patch_target(doc, path=path, target=target)
    kind = str(target.get("kind") or "").strip()
    node_id = str(target.get("id") or "").strip()

    if kind in {"scene_action_block", "action_block"} or node_id.upper().startswith("AC"):
        action_ref = _find_action_block(data, node_id, path)
        if action_ref is None:
            raise KeyError(f"scene action block not found: {node_id or path}")
        block, action = action_ref
        field = path.split(".")[-1] if "." in path else path
        if field not in action or field == "action_id":
            raise KeyError(f"unsupported scene action field: {field}")
        action[field] = value
        touched.extend([
            action.get("action_id") or node_id,
            block.get("scene_id") or "",
        ])
        return ScreenplayDocument.model_validate(data), touched

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

    if (
        kind == "spine_beat"
        or re.search(r"spine_beats(?:\[\d+\]|\.\d+)", path)
    ):
        plot_spine = data.get("plot_spine")
        if not isinstance(plot_spine, dict):
            raise KeyError("plot_spine not found")
        beats = plot_spine.get("spine_beats")
        if not isinstance(beats, list) or not beats:
            raise KeyError("plot_spine.spine_beats not found")
        beat_ids = [str(b.get("beat_id") or "") for b in beats if isinstance(b, dict)]
        resolved_beat_id = (
            _resolve_spine_beat_id(beat_ids, node_id)
            or _resolve_spine_beat_id(beat_ids, path)
        )
        beat = (
            _find_by_id(beats, "beat_id", resolved_beat_id)
            if resolved_beat_id
            else None
        )
        if beat is None:
            raise KeyError(f"spine beat not found: {node_id or path}")
        field = re.split(r"[./]+", path.strip("/"))[-1]
        if field not in beat:
            raise KeyError(f"unsupported spine beat field: {field}")
        beat[field] = value
        touched.append(beat.get("beat_id") or resolved_beat_id)
        return ScreenplayDocument.model_validate(data), touched

    if kind == "source_coverage" or path.startswith("source_coverage"):
        items = data.setdefault("source_coverage", [])
        item = _find_by_id(items, "source_segment_id", node_id)
        if item is None:
            raise KeyError(f"source coverage decision not found: {node_id or path}")
        field = path.split(".")[-1]
        if field in item:
            item[field] = value
        elif isinstance(value, dict):
            item.update(value)
        else:
            raise KeyError(f"unsupported source coverage field: {field}")
        touched.append(item.get("source_segment_id") or node_id)
        return ScreenplayDocument.model_validate(data), touched

    # generic dotted set on document root — only for fields that genuinely
    # live at the document root.  Anything else means kind resolution failed
    # to find an owning node; silently writing an unknown key here used to
    # report success (touched=[...]) while leaving the document unchanged,
    # because ScreenplayDocument.model_validate drops unrecognized extra
    # fields without error. Fail loudly instead so callers (see
    # _try_document_patch_operation / _llm_field_patch_once in
    # screenplay_repair.py) can fall back or surface the real reason.
    root_key = re.split(r"[.\[]", path)[0] if path else ""
    if root_key not in ScreenplayDocument.model_fields:
        raise KeyError(
            f"unresolved patch target: kind={kind or '<empty>'} "
            f"id={node_id or '<empty>'} path={path or '<empty>'}"
        )
    _set_by_dotted(data, path, value)
    touched.append(path.split(".")[0] or path)
    return ScreenplayDocument.model_validate(data), touched


def split_dialogue_chain_by_scene(
    doc: ScreenplayDocument,
    *,
    chain_id: str,
) -> tuple[ScreenplayDocument, list[str]]:
    """按正文实际所在场次拆分被跨场的对白链。

    只修正 dialogue_chains 的结构归属，不移动/删改正文台词，因此不会打乱演员调度。
    同一场内的连续话轮保持成组；新链 ID 使用尚未占用的 DC 序号。
    """
    data = copy.deepcopy(doc.model_dump(mode="json"))
    chains = list(data.get("dialogue_chains") or [])
    chain_index = next((
        index for index, chain in enumerate(chains)
        if str(chain.get("chain_id") or "") == chain_id
    ), None)
    if chain_index is None:
        return doc, []
    chain = chains[chain_index]
    turns = list(chain.get("turns") or [])
    if len(turns) < 2:
        return doc, []

    def compact(value: Any) -> str:
        return re.sub(r"[\s\W_]+", "", str(value or ""), flags=re.UNICODE)

    def compact_speaker(value: Any) -> str:
        # 正文允许把表演提示写在角色名后（如「萧战（关切）」），而对白链中的
        # speaker 只保存角色名。定位时先去掉这类提示，否则会把所有话轮错误地
        # 回退到首场，最终把本可修复的跨场链误判成 no-op。
        base = re.sub(r"[（(][^）)]*[）)]", "", str(value or ""))
        return compact(base)

    scene_lines: list[tuple[str, list[tuple[str, str]]]] = []
    for block in data.get("scene_blocks") or []:
        values = [
            (compact_speaker(turn.get("speaker")), compact(turn.get("line")))
            for turn in (block.get("dialogue_turns") or [])
            if compact(turn.get("line"))
        ]
        scene_lines.append((str(block.get("scene_id") or ""), values))

    located: list[tuple[str, dict]] = []
    previous_scene = ""
    for turn in turns:
        speaker = compact_speaker(turn.get("speaker"))
        line = compact(turn.get("line"))
        scene_id = next((
            candidate_scene
            for candidate_scene, values in scene_lines
            if any(
                (not speaker or not candidate_speaker or speaker == candidate_speaker)
                and (line == candidate_line or line in candidate_line or candidate_line in line)
                for candidate_speaker, candidate_line in values
            )
        ), "")
        scene_id = scene_id or previous_scene or (scene_lines[0][0] if scene_lines else "")
        previous_scene = scene_id
        located.append((scene_id, turn))

    groups: list[tuple[str, list[dict]]] = []
    for scene_id, turn in located:
        if groups and groups[-1][0] == scene_id:
            groups[-1][1].append(turn)
        else:
            groups.append((scene_id, [turn]))
    if len(groups) <= 1:
        return doc, []

    used_ids = {str(item.get("chain_id") or "") for item in chains}
    next_number = 1

    def next_chain_id() -> str:
        nonlocal next_number
        while f"DC{next_number}" in used_ids:
            next_number += 1
        value = f"DC{next_number}"
        used_ids.add(value)
        next_number += 1
        return value

    replacements: list[dict] = []
    created_ids: list[str] = []
    for group_index, (_scene_id, group_turns) in enumerate(groups):
        item = copy.deepcopy(chain)
        item["chain_id"] = chain_id if group_index == 0 else next_chain_id()
        item["turns"] = group_turns
        if group_index:
            item["topic"] = f"{str(chain.get('topic') or '').strip()}（续）".strip()
            created_ids.append(item["chain_id"])
        replacements.append(item)
    data["dialogue_chains"] = [
        *chains[:chain_index], *replacements, *chains[chain_index + 1:],
    ]
    return ScreenplayDocument.model_validate(data), ["dialogue_chains", chain_id, *created_ids]


def split_dialogue_turn_by_capacity(
    doc: ScreenplayDocument,
    *,
    chain_id: str,
    turn_index: int,
    max_chars: int,
) -> tuple[ScreenplayDocument, list[str]]:
    """Split one oversized source-grounded turn at punctuation boundaries."""
    data = copy.deepcopy(doc.model_dump(mode="json"))
    chain = next(
        (
            item
            for item in data.get("dialogue_chains") or []
            if str(item.get("chain_id") or "") == chain_id
        ),
        None,
    )
    if (
        chain is None
        or max_chars <= 0
        or not 0 <= turn_index < len(chain.get("turns") or [])
    ):
        return doc, []
    turn = chain["turns"][turn_index]
    line = str(turn.get("line") or "").strip()

    def content_chars(value: str) -> int:
        return len(re.sub(r"[\W_]+", "", value, flags=re.UNICODE))

    if content_chars(line) <= max_chars:
        return doc, []
    clauses = [
        item.strip()
        for item in re.findall(r".*?[，。！？；,.!?;]|.+$", line)
        if item.strip()
    ]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        if current and content_chars(current + clause) > max_chars:
            chunks.append(current)
            current = ""
        if content_chars(clause) <= max_chars:
            current += clause
            continue
        for character in clause:
            if (
                current
                and content_chars(current + character) > max_chars
            ):
                chunks.append(current)
                current = ""
            current += character
    if current:
        chunks.append(current)
    if len(chunks) <= 1 or any(
        content_chars(chunk) > max_chars for chunk in chunks
    ):
        return doc, []

    source = str(turn.get("source_text") or "")
    replacements: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        replacement = copy.deepcopy(turn)
        replacement["line"] = chunk
        replacement["source_text"] = chunk if chunk in source else source
        if index:
            replacement["function"] = "statement"
        replacements.append(replacement)
    chain["turns"] = [
        *chain["turns"][:turn_index],
        *replacements,
        *chain["turns"][turn_index + 1:],
    ]
    return (
        ScreenplayDocument.model_validate(data),
        [
            "dialogue_chains",
            chain_id,
            f"{chain_id}-T{turn_index + 1}",
        ],
    )


def _build_scene_blocks(script: EpisodeScreenplay) -> list[SceneBlockNode]:
    blocks: list[SceneBlockNode] = []
    preserve_body_order = (
        str(script.source_text_range or "").strip().startswith(
            "screenplay-generation-ir.v1"
        )
    )
    known_speakers: dict[str, str] = {}
    authoritative_speakers_by_line: dict[str, str] = {}
    for scene in script.scene_outline or []:
        for name in scene.characters or []:
            identity = _dialogue_identity(name)
            if identity:
                known_speakers[identity] = name
    for chain in script.dialogue_chains or []:
        for turn in chain.turns or []:
            speaker = (turn.speaker or "").strip()
            speaker_identity = _dialogue_identity(speaker)
            line_identity = _dialogue_identity(turn.line)
            if speaker_identity:
                known_speakers[speaker_identity] = speaker
            if line_identity and speaker:
                existing = authoritative_speakers_by_line.get(line_identity)
                authoritative_speakers_by_line[line_identity] = (
                    speaker if existing in {None, speaker} else ""
                )
    # Prefer scene_outline as structural skeleton
    if script.scene_outline:
        parsed_body = _parse_full_script_scenes(
            script.full_script_text or "",
            known_speakers=known_speakers,
            authoritative_speakers_by_line=authoritative_speakers_by_line,
        )
        for scene in script.scene_outline:
            sid = f"SC{int(scene.scene_no):02d}"
            body = parsed_body.get(
                int(scene.scene_no),
                {"actions": [], "turns": [], "order": []},
            )
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
            body_order = [
                (
                    actions[index].action_id
                    if kind == "action" and 0 <= index < len(actions)
                    else turns[index].turn_id
                    if kind == "dialogue" and 0 <= index < len(turns)
                    else ""
                )
                for kind, index in (body.get("order") or [])
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
                previous_scene_exit_state=(
                    scene.previous_scene_exit_state or ""
                ),
                opening_image=scene.opening_image or "",
                agency_contracts=list(scene.agency_contracts or []),
                entry_state=scene.entry_state or "",
                exit_state=scene.exit_state or "",
                context_requirements=list(scene.context_requirements or []),
                action_blocks=actions,
                dialogue_turns=turns,
                body_order=(
                    [value for value in body_order if value]
                    if preserve_body_order else []
                ),
            ))
        # If the prose body has no dialogue, place each authoritative turn in
        # the closest scene instead of distributing unrelated chains by index.
        if script.dialogue_chains and all(not b.dialogue_turns for b in blocks):
            for chain in script.dialogue_chains:
                declared_block = _declared_dialogue_scene(blocks, chain)
                for idx, turn in enumerate(chain.turns, start=1):
                    block = declared_block or _best_scene_for_unmatched_chain_turn(
                        blocks, chain, turn,
                    )
                    block.dialogue_turns.append(DialogueTurnNode(
                        turn_id=f"{chain.chain_id}-T{idx}",
                        chain_id=chain.chain_id,
                        speaker=turn.speaker,
                        line=turn.line,
                        function=turn.function,
                        source_text=turn.source_text,
                    ))
                    if preserve_body_order:
                        block.body_order.append(f"{chain.chain_id}-T{idx}")
        return blocks

    # Fallback: parse full_script_text only
    parsed = _parse_full_script_scenes(
        script.full_script_text or "",
        known_speakers=known_speakers,
        authoritative_speakers_by_line=authoritative_speakers_by_line,
    )
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
        actions = [
            ActionBlockNode(action_id=f"AC{scene_no:02d}-{i:02d}", text=t)
            for i, t in enumerate(body.get("actions") or [], start=1)
        ]
        turns = [
            DialogueTurnNode(
                turn_id=f"DC{scene_no}-T{i}",
                chain_id=f"DC{scene_no}",
                speaker=t.get("speaker", ""),
                line=t.get("line", ""),
            )
            for i, t in enumerate(body.get("turns") or [], start=1)
        ]
        body_order = [
            (
                actions[index].action_id
                if kind == "action" and 0 <= index < len(actions)
                else turns[index].turn_id
                if kind == "dialogue" and 0 <= index < len(turns)
                else ""
            )
            for kind, index in (body.get("order") or [])
        ]
        blocks.append(SceneBlockNode(
            scene_id=f"SC{scene_no:02d}",
            scene_no=scene_no,
            scene_heading=body.get("heading") or f"【场{scene_no}】",
            action_blocks=actions,
            dialogue_turns=turns,
            body_order=(
                [value for value in body_order if value]
                if preserve_body_order else []
            ),
        ))
    return blocks


_VISUAL_NARRATION_SPEAKER_PREFIXES = (
    "银幕", "屏幕", "画面", "投影", "字幕", "电视画面", "手机画面",
)
_EXPLICIT_SPOKEN_QUOTE_PAIRS = (
    ("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'),
)


def _has_explicit_spoken_quotes(value: str) -> bool:
    """Unknown identities need an unambiguous dialogue carrier.

    A colon alone is also ordinary Chinese prose punctuation.  Known speakers
    and source-authoritative chain lines are already structurally bound; an
    otherwise unknown prefix is treated as a speaker only when the payload is
    explicitly quoted as speech, so it can reach the typed identity gate
    without promoting action narration into a character.
    """
    text = (value or "").strip()
    return any(
        text.startswith(opening) and text.endswith(closing)
        for opening, closing in _EXPLICIT_SPOKEN_QUOTE_PAIRS
    )


def _parse_full_script_scenes(
    text: str,
    *,
    known_speakers: dict[str, str] | None = None,
    authoritative_speakers_by_line: dict[str, str] | None = None,
) -> dict[int, dict[str, Any]]:
    scenes: dict[int, dict[str, Any]] = {}
    known_speakers = known_speakers or {}
    authoritative_speakers_by_line = authoritative_speakers_by_line or {}
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
                "order": [],
            }
            rest = heading.group(2).strip()
            if rest and "／" not in rest and "/" not in rest:
                # keep heading only
                pass
            continue
        if current is None:
            current = 1
            scenes.setdefault(
                1,
                {"heading": "【场1】", "actions": [], "turns": [], "order": []},
            )
        dlg = _DIALOGUE_RE.match(line)
        if dlg:
            raw_speaker = dlg.group(1).strip()
            spoken_line = dlg.group(2).strip()
            canonical_speaker = authoritative_speakers_by_line.get(
                _dialogue_identity(spoken_line),
            )
            speaker_identity = _dialogue_identity(raw_speaker)
            exact_speaker = known_speakers.get(speaker_identity)
            starts_with_known_speaker = any(
                speaker_identity.startswith(identity)
                for identity in known_speakers
                if identity and speaker_identity != identity
            )
            if canonical_speaker or exact_speaker:
                scenes[current]["turns"].append({
                    "speaker": canonical_speaker or exact_speaker,
                    "line": spoken_line,
                })
                scenes[current]["order"].append(
                    ("dialogue", len(scenes[current]["turns"]) - 1)
                )
            elif (
                starts_with_known_speaker
                or raw_speaker.startswith(_VISUAL_NARRATION_SPEAKER_PREFIXES)
            ):
                # A visual/action label is prose, not a voice actor. Replace
                # the colon so the rendered projection cannot be parsed back
                # into a spoken line.
                scenes[current]["actions"].append(
                    f"{raw_speaker}，{spoken_line}",
                )
                scenes[current]["order"].append(
                    ("action", len(scenes[current]["actions"]) - 1)
                )
            elif _has_explicit_spoken_quotes(spoken_line):
                # Preserve explicitly formatted unknown dialogue so the typed
                # identity gate can reject an undeclared speaker.
                scenes[current]["turns"].append({
                    "speaker": raw_speaker,
                    "line": spoken_line,
                })
                scenes[current]["order"].append(
                    ("dialogue", len(scenes[current]["turns"]) - 1)
                )
            else:
                # Ordinary Chinese prose also uses a colon.  With no known
                # speaker and no authoritative chain-line match, it remains an
                # action instead of manufacturing a voice/visual identity.
                scenes[current]["actions"].append(line)
                scenes[current]["order"].append(
                    ("action", len(scenes[current]["actions"]) - 1)
                )
        else:
            scenes[current]["actions"].append(line)
            scenes[current]["order"].append(
                ("action", len(scenes[current]["actions"]) - 1)
            )
    return scenes


def _chains_from_scene_turns(blocks: list[SceneBlockNode]) -> list[KeyDialogueChain]:
    by_chain: dict[str, list[DialogueTurnNode]] = {}
    scene_ids_by_chain: dict[str, set[str]] = {}
    for block in blocks:
        for turn in block.dialogue_turns:
            cid = turn.chain_id or block.scene_id or "DC1"
            by_chain.setdefault(cid, []).append(turn)
            scene_ids_by_chain.setdefault(cid, set()).add(block.scene_id)
    chains: list[KeyDialogueChain] = []
    for cid, turns in by_chain.items():
        chains.append(KeyDialogueChain(
            chain_id=cid,
            scene_id=(
                next(iter(scene_ids_by_chain[cid]))
                if len(scene_ids_by_chain[cid]) == 1
                else ""
            ),
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


_DOTTED_BRACKET_INDEX_RE = re.compile(r"^([^\[\]]+)\[(\d+)\]$")


def _set_by_dotted(data: dict[str, Any], path: str, value: Any) -> None:
    """Set a value at a dotted/slash path; also parses `key[idx]` segments.

    "plot_spine.spine_beats[2].does" used to silently create a literal
    "spine_beats[2]" dict key (since dict-vs-list dispatch below only ever
    checked `part.isdigit()`) instead of indexing into the spine_beats list,
    and pydantic's model_validate then dropped that unknown key without
    error. Expanding `key[idx]` into two path segments ("key", "idx") lets
    the existing digit-index branch drill into the real list element.
    """
    raw_parts = [p for p in path.replace("/", ".").split(".") if p]
    parts: list[str] = []
    for raw_part in raw_parts:
        bracket_match = _DOTTED_BRACKET_INDEX_RE.match(raw_part)
        if bracket_match:
            parts.append(bracket_match.group(1))
            parts.append(bracket_match.group(2))
        else:
            parts.append(raw_part)
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


def _find_action_block(
    data: dict,
    node_id: str,
    path: str,
) -> tuple[dict, dict] | None:
    wanted = node_id.upper()
    match = re.search(r"(AC\d+-\d+)", path, re.I)
    if not wanted and match:
        wanted = match.group(1).upper()
    for block in data.get("scene_blocks") or []:
        for action in block.get("action_blocks") or []:
            if str(action.get("action_id") or "").upper() == wanted:
                return block, action
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
