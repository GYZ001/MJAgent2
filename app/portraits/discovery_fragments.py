"""角色发现的正文片段与前瞻上下文构建：候选卡/人物谱并发锁、
跳过名单键、前向片段抽取、身份载体标注、未来章节上下文分发。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from app import textmatch
from app.portraits.card_owner import resolve_card_owner
from app.schemas import Bible, EpisodeScreenplay
from app.source_excerpt import align_source_excerpt

from .constants import (
    CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET,
    FRAGMENT_BUDGET,
)
from .discovery_resample import extract_character_fragments

# ---------- 新角色发现（剧本阶段反应式：按需检索原文判断戏份，够分量才建卡） ----------
#
# 设计：人物谱只在进项目时谱写一次；之后由剧本阶段触发——剧本里出现、人物谱里没有的名字，
# 向后检索若干章原文判断戏份，画面够多才单独建卡 + 定妆。必须在【分镜展开前】完成，
# 否则 validate_storyboard 会因"角色圣经中不存在"把新角色从分镜里刷掉。

IDENTITY_DISCOVERY_FORWARD_CHAPTERS = 10
CHARACTER_IMPORTANCE_FORWARD_CHAPTERS = 20
# 曾经的负缓存过期窗口（隔多少集重新评估一次）：判据挂在"过了多少集"这个会被
# 正常追更改动的状态字段上，不是挂在"这个角色的戏份是否真的变了"本身
# （CLAUDE.md「判据必须挂在这件事本身成没成」）。已被 cards.py 里 ensure_
# character_card 的内容哈希负缓存取代（键值改成 _fragment_signature(fragments)，
# 片段变了才重判，不再按集数强制过期）。这个常量本身仍保留、不删——
# app/portraits/__init__.py 仍在重新导出它（那个文件归另一个代理管，本次不碰），
# 删掉会让它的导入直接 ImportError。
DISCOVERY_REJUDGE_WINDOW = 20

# 同名角色卡的建卡互斥锁（逐集分镜并行时，两集可能同时发现同一新角色）。
_card_locks: dict[tuple[str, str], asyncio.Lock] = {}
_card_locks_guard = asyncio.Lock()
_bible_locks: dict[str, asyncio.Lock] = {}
_bible_locks_guard = asyncio.Lock()


async def _card_lock(project_id: str, name: str) -> asyncio.Lock:
    async with _card_locks_guard:
        key = (project_id, name)
        lock = _card_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _card_locks[key] = lock
        return lock


async def _bible_lock(project_id: str) -> asyncio.Lock:
    async with _bible_locks_guard:
        lock = _bible_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            _bible_locks[project_id] = lock
        return lock


def _non_character_skip_key(project_id: str, name: str) -> str:
    """Durable record that the card layer judged this name not a character.

    The in-place demotion below only reaches in-process consumers, and
    ``ensure_cards_for_text`` copies its candidate dicts.  Structural coverage
    reads persisted artifacts, so the decision has to survive as its own
    durable fact or coverage will keep demanding a card that must never exist.
    """
    return f"char_not_character:{project_id}:{name}"


def _discovery_skip_key(project_id: str, name: str) -> str:
    return f"char_discovery_skip:{project_id}:{name}"


def _fragment_signature(fragments: str) -> str:
    """``_forward_fragments`` 产出内容的哈希指纹，供 ``_discovery_skip_key`` 的
    负缓存判据使用：片段没变，仍是同一次"戏份不足"判断的延续；片段变了（新章节
    写到这个人、检索窗口前移）就必须重判。挂产物信号，不挂"过了多少集"。
    """
    return hashlib.sha256((fragments or "").encode("utf-8")).hexdigest()


def _bible_card_owner(
    conn, project_id: str, name: str,
) -> tuple[str, str] | tuple[str, list[str]]:
    """``name`` 在人物谱里的归属判定，委托给唯一的身份归属解析器（见
    ``app.portraits.card_owner`` 模块 docstring）。返回 ``resolve_card_owner``
    的三态：owner/none/conflict——不折叠成布尔值，调用方需要区分"精确命中一个
    角色"与"命中多个角色的真实歧义"。
    """
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return ("none", "")
    bible = Bible.model_validate(json.loads(row["bible_json"]))
    return resolve_card_owner(bible, name)


def _name_in_bible(conn, project_id: str, name: str) -> bool:
    """`name` 是否已经属于人物谱里的某个角色——精确比对 name 与全部 alias.text，
    委托给唯一的身份归属解析器（见 app.portraits.card_owner 模块 docstring）。
    conflict（同一称呼命中 ≥2 个角色）按"已有归属"处理，fail closed：不再新建卡。
    """
    return _bible_card_owner(conn, project_id, name)[0] != "none"


def _card_owner_lookup(conn, project_id: str, name: str) -> dict | None:
    """``ensure_character_card`` 两处"人物谱里已有归属就不新建卡"早退判断的
    共同实现，返回可以直接作为函数结果的 payload；``None`` 表示两种既有情况
    都不成立，调用方按原计划继续走建卡流程。

    - owner：返回归属者的规范名（人物谱里真实存在的 ``Character.name``），
      不是被查询的标签——下游（``cards_ensure.py``）拿这个 name 生成身份决议，
      决议必须指向人物谱里真实存在的角色，不能是"李富贵"的别名"小胖子"本身。
    - conflict：同一称呼精确命中 ≥2 个不同角色，真实存在的合法数据（例如
      "大汉"同时是"曹阳"和"虎爷"的别名，见 card_owner 模块 docstring），必须
      fail closed——不得替调用方猜一个归属，status 与 "exists" 分开，好让
      调用方能识别并拒绝据此生成任何决议。
    """
    status, value = _bible_card_owner(conn, project_id, name)
    if status == "owner":
        return {"status": "exists", "name": value}
    if status == "conflict":
        return {
            "status": "conflict",
            "name": name,
            "owners": value,
            "reason": (
                f"「{name}」在人物谱中同时命中 {'、'.join(value)}，"
                "无法安全判定唯一归属，未新建卡也未复用任一方"
            ),
        }
    return None


def _forward_fragments(
    conn, project_id: str, name: str, from_episode_no: int,
) -> tuple[str, str, dict[int, str]]:
    """保留原有人物重要性评估窗口，不与"未来 10 章找真名"耦合。

    王有材事故修复新增第三个返回值 chapters_by_idx（idx -> 完整章节原文，未经窗口化/
    预算截断）：供 assess_new_character 核验 source_evidence 使用——evidence_chapter_index
    要能对应到完整原文，不能只对应下面 fragments 里的窗口化片段。fragments 本身改为
    【第 N 章】分块标记格式（仿照同文件 _future_chapter_context 已用的方式），原格式是
    多章直接拼接成一整块文本，模型看不出章节边界，没法准确申报 evidence_chapter_index。
    """
    ep = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, from_episode_no)).fetchone()
    src = json.loads(ep["source_chapters"] or "[]") if ep and ep["source_chapters"] else []
    lo, hi = (min(src), max(src)) if src else (0, 0)
    rows = conn.execute(
        "SELECT idx, content FROM chapters WHERE project_id=? AND idx>=? AND idx<=? ORDER BY idx",
        (project_id, lo, hi + CHARACTER_IMPORTANCE_FORWARD_CHAPTERS)).fetchall()
    chapters_by_idx: dict[int, str] = {}
    blocks: list[str] = []
    used = 0
    for row in rows:
        content = row["content"] or ""
        try:
            idx = int(row["idx"])
        except (TypeError, ValueError):
            continue
        if content.strip():
            chapters_by_idx[idx] = content
        piece = extract_character_fragments(content, name)
        if not piece or used >= FRAGMENT_BUDGET:
            continue
        block = f"【第 {idx} 章】\n{piece}"
        blocks.append(block)
        used += len(block)
    return (
        "\n\n".join(blocks),
        f"第 {from_episode_no} 集相关章节 +{CHARACTER_IMPORTANCE_FORWARD_CHAPTERS} 章",
        chapters_by_idx,
    )


def _future_chapter_context(
    conn,
    project_id: str,
    from_episode_no: int,
) -> tuple[str, str]:
    """读取本集源章节之后的小段原文，只用于角色姓名消歧。

    后续文本不会作为本集剧情素材传入剧本生成；它只在人物发现的
    受限 Prompt 中出现，用来回答“大汉/老者/黑衣人后来叫什么”。
    """
    ep = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, from_episode_no),
    ).fetchone()
    try:
        source_chapters = json.loads(ep["source_chapters"] or "[]") if ep else []
        chapter_indexes = [int(value) for value in source_chapters]
    except (TypeError, ValueError, json.JSONDecodeError):
        chapter_indexes = []
    if not chapter_indexes:
        return "", "无后续章节线索"
    last_source_chapter = max(chapter_indexes)
    last_discovery_chapter = last_source_chapter + IDENTITY_DISCOVERY_FORWARD_CHAPTERS
    rows = conn.execute(
        "SELECT idx, content FROM chapters "
        "WHERE project_id=? AND idx>? AND idx<=? ORDER BY idx",
        (project_id, last_source_chapter, last_discovery_chapter),
    ).fetchall()
    blocks = [
        f"【第 {row['idx']} 章】\n{(row['content'] or '').strip()}"
        for row in rows
        if (row["content"] or "").strip()
    ]
    return (
        "\n\n".join(blocks),
        f"第 {last_source_chapter + 1}-{last_discovery_chapter} 章（仅姓名消歧）",
    )


def _draft_identity_projection(draft_text: str) -> str:
    """Project only typed identity carriers from a screenplay draft."""
    if not draft_text:
        return ""
    try:
        script = EpisodeScreenplay.model_validate_json(draft_text)
    except (TypeError, ValueError):
        return json.dumps(
            {"parse_status": "invalid", "identity_mentions": []},
            ensure_ascii=False,
        )

    mentions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    structured_turn_surfaces: set[tuple[str, str]] = set()

    def add(value: object, path: str, *, line_context: str = "") -> None:
        text = str(value or "").strip()
        key = (text, path)
        if text and key not in seen:
            seen.add(key)
            mention = {"value": text, "path": path}
            context = str(line_context or "").strip()[:160]
            if context:
                mention["line_context"] = context
            mentions.append(mention)

    for scene_index, scene in enumerate(script.scene_outline or []):
        for character in scene.characters or []:
            add(character, f"scene_outline[{scene_index}].characters")
    for chain_index, chain in enumerate(script.dialogue_chains or []):
        for turn_index, turn in enumerate(chain.turns or []):
            speaker = str(turn.speaker or "").strip()
            line = str(turn.line or "").strip()
            add(
                speaker,
                f"dialogue_chains[{chain_index}].turns[{turn_index}].speaker",
                line_context=line,
            )
            structured_turn_surfaces.add((
                _identity_carrier_annotation_base(speaker) or speaker,
                line,
            ))
    for item_index, item in enumerate(script.information_ledger or []):
        add(item.speaker_id, f"information_ledger[{item_index}].speaker_id")
    for voice_index, voice in enumerate(script.voice_bible or []):
        add(voice.speaker_id, f"voice_bible[{voice_index}].speaker_id")

    from app.validators import _script_dialogue_turns

    for scene_no, speaker, line in _script_dialogue_turns(
        script.full_script_text or "",
    ):
        if (speaker, str(line or "").strip()) in structured_turn_surfaces:
            continue
        add(
            speaker,
            f"full_script_text.scene[{scene_no}].speaker",
            line_context=line,
        )

    plan = script.narrative_plan
    if plan is not None:
        for contract_index, contract in enumerate(plan.identity_contracts or []):
            add(
                contract.identity_id,
                f"narrative_plan.identity_contracts[{contract_index}].identity_id",
            )
            add(
                contract.display_name,
                f"narrative_plan.identity_contracts[{contract_index}].display_name",
            )
            for voice_id in contract.voice_ids or []:
                add(
                    voice_id,
                    f"narrative_plan.identity_contracts[{contract_index}].voice_ids",
                )
        for state_index, state in enumerate(plan.character_states or []):
            add(
                state.character_id,
                f"narrative_plan.character_states[{state_index}].character_id",
            )
        for belief_index, belief in enumerate(plan.character_beliefs or []):
            add(
                belief.character_id,
                f"narrative_plan.character_beliefs[{belief_index}].character_id",
            )
        for scene_index, scene in enumerate(plan.scene_contracts or []):
            add(
                scene.point_of_view_character_id,
                f"narrative_plan.scene_contracts[{scene_index}].point_of_view_character_id",
            )

    return json.dumps(
        {"parse_status": "typed", "identity_mentions": mentions},
        ensure_ascii=False,
        separators=(",", ":"),
    )


_IDENTITY_CARRIER_ANNOTATION_RE = re.compile(
    r"^(?P<base>[^()（）]+?)\s*[（(][^()（）]+[）)]\s*$"
)


def _identity_carrier_annotation_base(value: object) -> str:
    match = _IDENTITY_CARRIER_ANNOTATION_RE.fullmatch(
        str(value or "").strip()
    )
    return match.group("base").strip() if match else ""


def _aligned_identity_source_label(
    source_label: str,
    identity_haystack: str,
) -> str:
    """Recover a provider-expanded label only when source alignment is strong."""
    label = str(source_label or "").strip()
    if not label:
        return ""
    if label in identity_haystack:
        return label
    condensed = textmatch.condense(label)
    if len(condensed) < 4:
        return ""
    aligned = align_source_excerpt(
        label,
        identity_haystack,
        min_match_chars=max(3, int(len(condensed) * 0.6)),
    )
    if aligned is None:
        return ""
    excerpt = str(aligned.excerpt or "").strip()
    excerpt_chars = len(textmatch.condense(excerpt))
    if (
        excerpt_chars < 3
        or excerpt_chars > len(condensed) + 4
        or textmatch.longest_run_ratio(label, excerpt) < 0.65
        or textmatch.bigram_coverage(label, excerpt) < 0.55
    ):
        return ""
    return excerpt


def _distributed_identity_fragments(
    text: str,
    label: str,
    *,
    known_names: list[str],
    window: int,
    budget: int,
) -> str:
    """Prefer identity windows containing known names, then span the timeline."""
    spans = [
        (max(0, match.start() - window), min(len(text), match.end() + window))
        for match in re.finditer(re.escape(label), text)
    ]
    if not spans or budget <= 0:
        return ""
    # Rank the individual occurrences before de-duplicating overlap.  Merging a
    # dense run first can create one multi-kilobyte span whose leading slice
    # hides the decisive late occurrence (for example, the moment a recurring
    # label finally states a name).
    ranked = sorted(
        enumerate(spans),
        key=lambda item: (
            -sum(
                name != label and name in text[item[1][0]:item[1][1]]
                for name in known_names
            ),
            0 if item[0] == 0 else 1,
            0 if item[0] == len(spans) - 1 else 1,
            item[0],
        ),
    )
    pieces: list[tuple[int, str]] = []
    selected_spans: list[tuple[int, int]] = []
    used = 0
    for index, (start, end) in ranked:
        if used >= budget:
            break
        if any(
            max(0, min(end, other_end) - max(start, other_start))
            >= int(min(end - start, other_end - other_start) * 0.6)
            for other_start, other_end in selected_spans
        ):
            continue
        piece = text[start:end].strip()[:budget - used]
        if piece:
            pieces.append((index, piece))
            selected_spans.append((start, end))
            used += len(piece)
    pieces.sort(key=lambda item: item[0])
    return "\n……\n".join(piece for _index, piece in pieces)


def _future_identity_context(
    future_text: str,
    source_labels: list[str],
    *,
    known_names: list[str] | None = None,
    current_text: str = "",
) -> str:
    """Return bounded future excerpts only where a current identity label occurs."""
    if not str(future_text or "").strip():
        return ""
    blocks: list[str] = []
    known = list(dict.fromkeys(
        str(name or "").strip()
        for name in (known_names or [])
        if str(name or "").strip()
    ))
    remaining = CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET
    needs_boundary_handoff = any(
        str(label or "").strip()
        and str(label or "").strip() not in future_text
        for label in source_labels
    )
    boundary = (
        "\n".join(filter(None, [
            str(current_text or "").strip()[-700:],
            str(future_text or "").strip()[:900],
        ]))
        if needs_boundary_handoff else ""
    )
    if boundary:
        block = f"【章节边界身份交接】\n{boundary[:remaining]}"
        blocks.append(block)
        remaining -= len(block)
    # Do not search and resend every known Bible name.  Candidate authorities
    # are projected separately; future prose is limited to unresolved labels
    # and the chapter-boundary handoff that can actually resolve them.
    for source_label in dict.fromkeys(source_labels):
        if remaining <= 0:
            break
        fragments = _distributed_identity_fragments(
            future_text,
            source_label,
            known_names=known,
            window=180,
            budget=min(900, remaining),
        )
        if not fragments:
            continue
        cooccurring = [
            name for name in known
            if name != source_label and name in fragments
        ]
        authority_hint = (
            "\n人物谱真名：" + "、".join(cooccurring)
            if cooccurring else ""
        )
        block = f"【当前称谓：{source_label}】\n{fragments}{authority_hint}"
        blocks.append(block)
        remaining -= len(block)
    return "\n\n".join(blocks)

