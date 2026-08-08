"""人物定妆照（跨集一致性增强，PRD §5.4 第 2 层的时间维扩展）。

定妆照按"适用集区间"分段存于 character_portraits（ep_start/ep_end，ep_end=NULL 表示开区间=当前最新版）。
两条反应式产生路径都按集触发、不做全量轮询。新角色发现挂在【剧本阶段】并在正式剧本校验前完成，
分镜阶段保留幂等兜底；已有角色外观漂移仍在分镜展开前处理：
  ① 新角色发现：剧本里出现、人物谱里没有、戏份够的角色 → 建卡 + 定妆，适用集从首次出场那集起开放。
  ② 已有角色按集漂移：剧本里出现、本集之前已有定妆照的角色 → 用【本集源文】判断外观相比当前锚点
     是否明显变化：
       - 变化不大 → 沿用当前定妆照（开区间自然向后覆盖），不重绘、不花钱；
       - 变化很大 → 关闭当前定妆照右区间（= 本集-1），以当前定妆照为底【图生图】重绘新定妆照
         （左区间=本集、右区间开放），并把 bible 该角色锚点同步成最新（供人物谱 UI 展示）。

生成台/关键帧出图时按集号选用覆盖该集的定妆照与外观锚点：图走 portrait_for_episode，文字锚点走
bible_for_episode（把 bible 换成"本集视图"），二者同段同源（见 app.refs / app.video_modes / app.worker）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from app import config, hiagent, textmatch
from app.atomic_io import atomic_write_bytes
from app.character_policy import resolution_declares_functional_identity
from app.db import get_conn, get_setting, new_id, now, set_setting
from app.evidence import repository as evidence_repository
from app.evidence.media import record_reference_asset
from app.errors import ContentGenerationError, code_ref
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.identity_authority import (
    normalize_character_resolution,
    normalize_character_resolutions,
)
from app.ingest import chapter_is_stub, chapter_titles_match
from app.refs import (
    PRODUCTION_APPEARANCE_MAX_CHARS,
    PRODUCTION_APPEARANCE_MIN_CHARS,
    _safe_name,
    missing_production_appearance_dimensions,
    portrait_prompt,
    production_appearance_anchor,
)
from app.schemas import Bible, Character, EpisodeScreenplay, extract_json
from app.source_excerpt import align_source_excerpt, index_source_segments

FRAGMENT_WINDOW = 220   # 命中角色名前后各取多少字
FRAGMENT_BUDGET = 4000  # 单角色单段送审片段总字数预算
APPEARANCE_MIN = PRODUCTION_APPEARANCE_MIN_CHARS
APPEARANCE_MAX = PRODUCTION_APPEARANCE_MAX_CHARS
STAGED_INITIAL_EP_START = 2_147_483_647  # 候选包不得命中任何真实集号
CAST_DISCOVERY_SOURCE_BUDGET = 18000
CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET = 8000
CHARACTER_CARD_MAX_TOKENS = 4096


# ---------- 原文片段抽取（纯本地，不调模型） ----------

def extract_character_fragments(text: str, name: str, *, window: int = FRAGMENT_WINDOW,
                                budget: int = FRAGMENT_BUDGET) -> str:
    """从正文里抽取提及 name 的片段（命中处前后 window 字），合并重叠区间，封顶 budget 字。"""
    if not name or not text:
        return ""
    spans: list[tuple[int, int]] = []
    for m in re.finditer(re.escape(name), text):
        spans.append((max(0, m.start() - window), min(len(text), m.end() + window)))
    if not spans:
        return ""
    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out: list[str] = []
    used = 0
    for s, e in merged:
        if used >= budget:
            break
        piece = text[s:e].strip()[: max(0, budget - used)]
        if piece:
            out.append(piece)
            used += len(piece)
    return "\n……\n".join(out)


# ---------- 外观变化判定（调模型，按集一次批量判定） ----------

async def screen_appearance_changes(entries: list[dict], ep_label: str) -> dict[str, dict]:
    """一次调用，批量判断本集里哪些【已有定妆照】角色外观相比各自当前锚点发生【明显视觉变化】。

    entries: [{"name", "current_appearance", "fragments"}]（fragments 为空者会被忽略）。
    返回 {name: {new_appearance, reason, change_dimensions, persistence, evidence_excerpt}}，
    仅含确实变化、且给出了新锚点的角色。"""
    entries = [e for e in entries if (e.get("fragments") or "").strip()]
    if not entries:
        return {}
    blocks = []
    for i, e in enumerate(entries, 1):
        blocks.append(
            f"角色{i}「{e['name']}」\n当前定妆照外观锚点：{e.get('current_appearance') or '（无）'}\n"
            f"本集提及该角色的原文片段：\n{(e.get('fragments') or '')[:FRAGMENT_BUDGET]}")
    body = "\n\n".join(blocks)
    prompt = f"""任务：逐个判断下列小说人物在新一段剧情（{ep_label}）里，外观相比各自【既有定妆照】是否发生【明显视觉变化】。

{body}

判断口径：只依据原文与当前定妆照的可见、稳定、可跨镜复现差异；
不得用姓名、题材、称谓或固定词表猜测变化。没有直接证据时 changed=false。

对 changed=true 的角色，给出整合后的【新外观锚点串】new_appearance：40~60 字，沿用既有锚点未变部分，只改真正变化处；保留性别年龄感/发型发色/服装款式与颜色/标志性特征。
- 外观锚点只写中性站姿下直接可见、可跨镜稳定复现的静态形态，不写行为、关系或镜头状态。
同时给出：
- change_dimensions：开放的稳定变化维度数组，名称应直接描述本次结构化差异，不套固定分类词表
- persistence：persistent（跨集持续）/ episode（仅本集）/ shot_only（单镜临时，不应更新人物谱）
- evidence_excerpt：原文短片段依据
- identity_change_authorized：只有原文证据明确支持持久身份形态变化时为 true，否则为 false

只输出一个 JSON 对象：{{"changes": [{{"name": "角色名", "changed": true/false, "new_appearance": "", "change_dimensions": [str], "identity_change_authorized": bool, "persistence": "persistent", "reason": "一句话依据", "evidence_excerpt": "原文短片段"}}]}}"""
    raw = await model_gateway.chat(
        [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=1600,
        call_meta={"stage": "screen_appearance_changes"},
    )
    obj = extract_json(raw)
    valid = {e["name"] for e in entries}
    out: dict[str, dict] = {}
    from app.multiview import normalize_appearance_change
    for item in (obj.get("changes") or []):
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if name not in valid or not bool(item.get("changed")):
            continue
        new_app = (item.get("new_appearance") or "").strip()
        if not new_app:
            continue  # 说变了却没给新锚点 → 保守沿用，不重绘
        normalized = normalize_appearance_change({**item, "character": name, "new_appearance": new_app})
        if normalized.get("persistence") == "shot_only":
            continue  # 临时状态不更新人物谱
        out[name] = {
            "new_appearance": normalized["new_appearance"][:APPEARANCE_MAX],
            "reason": normalized["reason"],
            "change_dimensions": normalized["change_dimensions"],
            "identity_change_authorized": normalized["identity_change_authorized"],
            "persistence": normalized["persistence"],
            "evidence_excerpt": normalized["evidence_excerpt"],
        }
    return out


# ---------- 新角色发现（剧本阶段反应式：按需检索原文判断戏份，够分量才建卡） ----------
#
# 设计：人物谱只在进项目时谱写一次；之后由剧本阶段触发——剧本里出现、人物谱里没有的名字，
# 向后检索若干章原文判断戏份，画面够多才单独建卡 + 定妆。必须在【分镜展开前】完成，
# 否则 validate_storyboard 会因"角色圣经中不存在"把新角色从分镜里刷掉。

IDENTITY_DISCOVERY_FORWARD_CHAPTERS = 10
CHARACTER_IMPORTANCE_FORWARD_CHAPTERS = 20
DISCOVERY_REJUDGE_WINDOW = 20     # 判过"戏份不足"的名字，隔多少集才重新评估一次（避免对龙套反复调模型）

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


def _discovery_skip_key(project_id: str, name: str) -> str:
    return f"char_discovery_skip:{project_id}:{name}"


def _name_in_bible(conn, project_id: str, name: str) -> bool:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    return any((c.get("name") or "") == name for c in json.loads(row["bible_json"]).get("characters", []))


def _forward_fragments(conn, project_id: str, name: str, from_episode_no: int) -> tuple[str, str]:
    """保留原有人物重要性评估窗口，不与“未来 10 章找真名”耦合。"""
    ep = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, from_episode_no)).fetchone()
    src = json.loads(ep["source_chapters"] or "[]") if ep and ep["source_chapters"] else []
    lo, hi = (min(src), max(src)) if src else (0, 0)
    rows = conn.execute(
        "SELECT content FROM chapters WHERE project_id=? AND idx>=? AND idx<=? ORDER BY idx",
        (project_id, lo, hi + CHARACTER_IMPORTANCE_FORWARD_CHAPTERS)).fetchall()
    text = "\n".join((r["content"] or "") for r in rows)
    return (
        extract_character_fragments(text, name),
        f"第 {from_episode_no} 集相关章节 +{CHARACTER_IMPORTANCE_FORWARD_CHAPTERS} 章",
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


def _source_identity_contexts(source_text: str, *, budget: int) -> list[str]:
    """Split the complete current source into bounded paragraph-preserving batches."""
    text = str(source_text or "").strip()
    if not text:
        return ["（本集原文为空）"]
    paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for paragraph in paragraphs:
        if len(paragraph) > budget:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_chars = 0
            start = 0
            while start < len(paragraph):
                end = min(len(paragraph), start + budget)
                chunks.append(paragraph[start:end])
                start = end
            continue
        added = len(paragraph) + (1 if current else 0)
        if current and current_chars + added > budget:
            chunks.append("\n".join(current))
            current = [paragraph]
            current_chars = len(paragraph)
        else:
            current.append(paragraph)
            current_chars += added
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


async def _discover_character_candidates_legacy(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    future_text: str = "",
    future_label: str = "",
    existing_resolutions: list[dict] | None = None,
) -> list[dict]:
    """Resolve the current episode's cast before/after screenplay generation.

    后续章节只能用来把当前章节的“大汉/老者/黑衣人”解析成稳定真名，
    不得把尚未出场的人物或剧情带回本集。身份模型确认的稳定真名必须完成
    最小人物卡；未确认真名的一次性人物保留来源称谓并签发 typed functional identity。
    """
    known_names = [c.name for c in bible.characters if c.name]
    known = "、".join(known_names) or "（无）"
    existing_functional_routes = {
        str(item.get("canonical_name") or "").strip()
        for item in (existing_resolutions or [])
        if (
            isinstance(item, dict)
            and resolution_declares_functional_identity(item)
            and str(item.get("canonical_name") or "").strip()
        )
    }
    existing_resolution_projection = [
        {
            "source_label": str(item.get("source_label") or "").strip(),
            "canonical_name": str(item.get("canonical_name") or "").strip(),
        }
        for item in (existing_resolutions or [])
        if (
            isinstance(item, dict)
            and resolution_declares_functional_identity(item)
            and str(item.get("source_label") or "").strip()
            and str(item.get("canonical_name") or "").strip()
        )
    ]
    current_haystack = f"{source_text or ''}\n{draft_text or ''}"
    draft_projection = _draft_identity_projection(draft_text)
    source_contexts = (
        ["（Baseline 后增量审计：当前原文已在首次预检中处理，本次不重复发送）"]
        if draft_text
        else _source_identity_contexts(
            source_text,
            budget=CAST_DISCOVERY_SOURCE_BUDGET,
        )
    )
    seen: set[tuple[str, str, str]] = set()
    candidates: list[dict] = []

    def collect(
        raw: str,
        *,
        identity_haystack: str,
        group_scope: str,
    ) -> None:
        try:
            obj = extract_json(raw, repair_unescaped_inner_quotes=True)
        except ValueError as exc:
            raise ContentGenerationError(
                "人物身份模型返回了不可验证的非结构化结果，当前阶段已停止"
            ) from exc
        for item in obj.get("characters") or []:
            if not isinstance(item, dict):
                continue
            # 兼容旧模型形状 {name, kind, evidence}。新协议的身份判断完全以模型输出为准。
            legacy_name = str(item.get("name") or "").strip()
            model_source_label = str(
                item.get("source_label") or legacy_name
            ).strip()
            source_label = _aligned_identity_source_label(
                model_source_label,
                current_haystack,
            )
            identity_kind = str(item.get("identity_kind") or "named").strip().lower()
            canonical_name = str(item.get("canonical_name") or legacy_name).strip()
            if identity_kind not in {"named", "functional"}:
                continue
            future_evidence = str(
                item.get("future_evidence") or ""
            ).strip()
            if (
                group_scope in {"future", "coverage"}
                and canonical_name
                and future_evidence
                and canonical_name in known_names
            ):
                # Only an existing canonical Bible identity can repair provider
                # enum drift. Repeating a relation/description in source text
                # proves label presence, not a stable named identity.
                identity_kind = "named"
            elif identity_kind == "functional":
                canonical_name = ""
            dedupe_key = (source_label, canonical_name, identity_kind)
            if (
                not source_label
                or len(source_label) > 16
                or dedupe_key in seen
                or (
                    identity_kind == "named"
                    and (
                        not canonical_name
                        or len(canonical_name) > 16
                        or (
                            canonical_name not in identity_haystack
                            and canonical_name not in known_names
                        )
                    )
                )
            ):
                continue
            seen.add(dedupe_key)
            functional_identity_key = str(
                item.get("functional_identity_key") or ""
            ).strip()[:64]
            existing_route_name = (
                functional_identity_key
                if functional_identity_key in existing_functional_routes
                else ""
            )
            prior_groups = {
                str(candidate.get("identity_group") or "").strip()
                for candidate in candidates
                if (
                    str(candidate.get("source_label") or "").strip()
                    == source_label
                    and str(candidate.get("identity_group") or "").strip()
                )
            }
            declared_group = str(
                item.get("identity_group")
                or functional_identity_key
                or ""
            ).strip()
            existing_groups = {
                str(candidate.get("identity_group") or "").strip()
                for candidate in candidates
                if str(candidate.get("identity_group") or "").strip()
            }
            declared_matches = {
                group
                for group in existing_groups
                if (
                    declared_group
                    and (
                        group == declared_group
                        or group.endswith(f":{declared_group}")
                    )
                )
            }
            identity_group = (
                next(iter(prior_groups))
                if group_scope in {"future", "coverage"} and len(prior_groups) == 1
                else (
                    next(iter(declared_matches))
                    if (
                        group_scope in {"future", "coverage"}
                        and len(declared_matches) == 1
                    )
                    else (
                        f"existing:{existing_route_name}"
                        if existing_route_name
                        else f"{group_scope}:{declared_group or source_label}"
                    )
                )
            )
            candidates.append({
                "name": canonical_name or source_label,
                "source_label": source_label,
                "identity_kind": identity_kind,
                "identity_group": identity_group,
                "existing_route_name": existing_route_name,
                "kind": "mentioned" if item.get("kind") == "mentioned" else "onscreen",
                "evidence": str(item.get("evidence") or "").strip()[:80],
                "future_evidence": future_evidence[:120],
                "source_segment_id": str(
                    item.get("source_segment_id") or ""
                ).strip(),
                "source_quote": str(item.get("source_quote") or "").strip()[:240],
                "model_source_label": (
                    model_source_label
                    if model_source_label != source_label else ""
                ),
            })

    for current_batch, source_context in enumerate(source_contexts, start=1):
        prompt = f"""任务：为第 {episode_no} 集做人物身份增量预检。请用语义和上下文判断，
不要依赖服饰、性别、年龄或称谓后缀的固定词表。

当前人物谱已有角色：
{known}

本集原文：
{source_context}

剧本草稿身份投影（只含类型合同中的身份字段，可能为空）：
{draft_projection or '（无）'}

本集已有功能身份决议（可为空；canonical_name 是已分配的本集稳定 ID）：
{json.dumps(existing_resolution_projection, ensure_ascii=False, separators=(',', ':'))}

规则：
1. 找出本集原文/草稿中实际出场或开口的人，source_label 填本集对他/她的原始称谓。
2. 若当前输入有明确同一人证据，identity_kind="named"，canonical_name 填稳定真名。
3. canonical_name 可以是文本明确给出的人名，也可以是跨章节稳定、唯一指向同一实体的法号、
   尊号或专属称号。泛化职位、外貌标签和无法唯一指人的称谓才判为 functional。
4. 姓名单独出现不算同一人证据；必须能确认该真名与 source_label 是同一人，有歧义一律不猜。
5. 若是一次性角色，或在可见线索中无法确认稳定真名，identity_kind="functional"、canonical_name=""。
6. 若身份投影中的 source_label 混入动作或表演提示，必须结合对应 line_context 判断真正说话人；
   source_label 保留原始完整字符串，canonical_name/functional_identity_key 绑定到真正说话人。
   禁止按“说、喊、点头”等固定词表或后缀规则猜测。
7. 每个 functional 项必须填写 functional_identity_key：
   - 若它与“本集已有功能身份决议”中的某人是同一人，精确填写该人的 canonical_name。
   - 否则填写本次响应内的不透明分组 ID（如 F1、F2）；不同 source_label 若明确是同一人必须共用同一 ID。
   - 无法确认是否同一人时必须使用不同 ID，禁止根据称谓字面相似猜测。
8. evidence 只描述身份依据，不复述与人物身份无关的剧情。
9. source_segment_id/source_quote 必须引用当前输入中实际承载该称谓的最小来源段；
   source_quote 必须逐字来自该段。短称谓只有在该段内唯一时才可输出。

只输出 JSON：
{{"characters": [{{"source_segment_id":"SRC0001","source_quote":"原文逐字短句","source_label": "本集称谓", "canonical_name": "真名或空串", "identity_kind": "named|functional", "functional_identity_key": "已有稳定ID或本次响应内分组ID", "kind": "onscreen|mentioned", "evidence": "本集依据", "future_evidence": "同一性依据或空串"}}]}}"""

        response = await model_gateway.chat_structured(
            [{"role": "user", "content": prompt}],
            model_type=_IdentityCandidateResponse,
            validate=None,
            operation_id=(
                f"screenplay.identity.current.v2:{episode_no}:{current_batch}:"
                + evidence_repository.content_hash(source_context)
            ),
            temperature=0.1,
            max_tokens=8192,
            call_meta={
                "stage": "discover_character_candidates",
                "episode_no": episode_no,
                "discovery_phase": "current",
                "source_batch": current_batch,
                "source_batches": len(source_contexts),
                "reuse_successful_operation": True,
            },
        )
        raw = response.model_dump_json()
        collect(
            raw,
            identity_haystack=current_haystack,
            group_scope=f"current-{current_batch}",
        )

    unresolved_onscreen_groups = {
        str(item.get("identity_group") or "").strip()
        for item in candidates
        if (
            item.get("identity_kind") == "functional"
            and item.get("kind") == "onscreen"
            and str(item.get("identity_group") or "").strip()
        )
    }
    future_candidates = [
        item
        for item in candidates
        if (
            item.get("identity_kind") == "functional"
            and (
                item.get("kind") == "onscreen"
                or str(item.get("identity_group") or "").strip()
                in unresolved_onscreen_groups
                or str(item.get("source_label") or "").strip()
                in future_text
            )
        )
    ]
    future_context = _future_identity_context(
        future_text,
        [item["source_label"] for item in future_candidates],
        known_names=known_names,
        current_text=source_text,
    )
    if future_context:
        current_identity_audit_source = str(source_text or "").strip()
        if len(current_identity_audit_source) > CAST_DISCOVERY_SOURCE_BUDGET:
            half = CAST_DISCOVERY_SOURCE_BUDGET // 2
            current_identity_audit_source = (
                current_identity_audit_source[:half]
                + "\n……（身份覆盖复核中段省略）……\n"
                + current_identity_audit_source[-half:]
            )
        future_prompt = f"""任务：只为当前集已经发现的人物称谓做后续姓名消歧。

当前人物谱已有角色：
{known}

当前集尚未确认真名的候选：
{json.dumps(future_candidates, ensure_ascii=False, separators=(',', ':'))}

当前集原文（同时复核第一遍是否漏掉独立出场/开口的实体）：
{current_identity_audit_source}

后续章节中命中这些称谓的局部窗口（{future_label or '后续章节'}）：
{future_context}

规则：
1. 优先输出当前集候选中 source_label 完全相同的项目；若第一遍遗漏了当前原文中可区分、
   独立出场或开口的实体，也必须补充输出，source_label 使用当前原文逐字称谓。
   称谓可以在章节边界发生变化；
   若当前集离场状态、后续开场承接和人物谱真名窗口共同形成唯一同一性证据，可据此确认，
   不要求旧称谓与真名必须出现在同一句。
2. canonical_name 必须出现在后续窗口或当前人物谱中；稳定唯一的法号、尊号、专属称号
   也属于 named identity，不要求必须是户籍式真名；有歧义就不输出。
3. 不得新增只在后续章节出场的人，不得复述与身份无关的剧情。

只输出 JSON：
{{"characters": [{{"source_label": "当前称谓", "canonical_name": "稳定真名", "identity_kind": "named", "kind": "onscreen|mentioned", "evidence": "本集身份依据", "future_evidence": "同一性依据"}}]}}"""
        future_raw = await model_gateway.chat(
            [{"role": "user", "content": future_prompt}],
            temperature=0.1,
            max_tokens=4096,
            call_meta={
                "stage": "discover_character_candidates",
                "episode_no": episode_no,
                "discovery_phase": "future_identity",
                "reuse_successful_operation": True,
            },
        )
        collect(
            future_raw,
            identity_haystack=f"{current_haystack}\n{future_context}",
            group_scope="future",
        )
        if len(current_identity_audit_source) >= 1000:
            coverage_prompt = f"""任务：独立审计当前集人物身份覆盖，只找第一遍遗漏或错误降级的实体。

当前人物谱已有角色：
{known}

当前集原文：
{current_identity_audit_source}

前两遍候选：
{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}

后续姓名证据：
{future_context}

规则：
1. 逐段核对每个独立行动、开口或具有可区分外观的实体；集合称谓不能替代其中的独立人物。
2. 只输出遗漏实体，或已有候选中能由后续证据唯一升级为 named identity 的实体。
3. source_label 必须逐字来自当前集原文。canonical_name 必须有当前原文、人物谱或后续窗口证据。
4. 不得按职业、年龄、服饰、称号词表判断人物是否重要；无法唯一确认就不输出。

只输出 JSON：
{{"characters": [{{"source_label": "当前原文逐字称谓", "canonical_name": "稳定真名或专属称号", "identity_kind": "named|functional", "identity_group": "若与已有候选同一实体则精确复用其 identity_group，否则空串", "functional_identity_key": "同一实体分组或空串", "kind": "onscreen|mentioned", "evidence": "当前依据", "future_evidence": "后续同一性依据"}}]}}"""
            coverage_raw = await model_gateway.chat(
                [{"role": "user", "content": coverage_prompt}],
                temperature=0.05,
                max_tokens=4096,
                call_meta={
                    "stage": "discover_character_candidates",
                    "episode_no": episode_no,
                    "discovery_phase": "coverage_audit",
                    "reuse_successful_operation": True,
                },
            )
            collect(
                coverage_raw,
                identity_haystack=f"{current_haystack}\n{future_context}",
                group_scope="coverage",
            )

    # 同一称谓在不同后文批次中可能先被保守判为 functional，后被真名证据命中。
    # 具名证据唯一时优先；出现两个不同真名时不猜，降级为一次性角色。
    resolved: list[dict] = []
    for source_label in dict.fromkeys(item["source_label"] for item in candidates):
        options = [item for item in candidates if item["source_label"] == source_label]
        named_by_name = {
            item["name"]: item for item in options if item["identity_kind"] == "named"
        }
        if len(named_by_name) == 1:
            resolved.append(next(iter(named_by_name.values())))
        elif len(named_by_name) > 1:
            functional = next(
                (item for item in options if item["identity_kind"] == "functional"),
                None,
            )
            resolved.append(functional or {
                "name": source_label,
                "source_label": source_label,
                "identity_kind": "functional",
                "identity_group": f"conflict:{source_label}",
                "existing_route_name": "",
                "kind": "onscreen",
                "evidence": "多批次身份线索冲突，不猜真名",
                "future_evidence": "",
            })
        else:
            resolved.append(options[0])

    named_by_group: dict[str, set[str]] = {}
    named_evidence: dict[tuple[str, str], dict] = {}
    for item in resolved:
        if item.get("identity_kind") != "named":
            continue
        group = str(item.get("identity_group") or "").strip()
        name = str(item.get("name") or "").strip()
        if group and name:
            named_by_group.setdefault(group, set()).add(name)
            named_evidence[(group, name)] = item
    upgraded: list[dict] = []
    for item in resolved:
        group = str(item.get("identity_group") or "").strip()
        names = named_by_group.get(group, set())
        if item.get("identity_kind") == "functional" and len(names) == 1:
            canonical_name = next(iter(names))
            evidence = named_evidence[(group, canonical_name)]
            upgraded.append({
                **item,
                "name": canonical_name,
                "identity_kind": "named",
                "future_evidence": str(
                    evidence.get("future_evidence") or ""
                ),
            })
        else:
            upgraded.append(item)
    resolved = upgraded

    # 按本集第一次出现排序，保证后续“路人甲/乙/丙/丁”分配不受模型输出顺序影响。
    return sorted(
        resolved,
        key=lambda item: current_haystack.find(item["source_label"]),
    )


class _IdentityCandidateResponse(BaseModel):
    characters: list[dict] = Field(default_factory=list)


def _attach_candidate_source_evidence(
    candidates: list[dict],
    source_text: str,
) -> list[dict]:
    """Bind candidate labels to one owned SRC without guessing from vocabulary."""
    segments = index_source_segments(source_text)
    by_id = {segment.segment_id: segment for segment in segments}
    for candidate in candidates:
        label = str(candidate.get("source_label") or "").strip()
        cited_id = str(candidate.get("source_segment_id") or "").strip()
        cited = by_id.get(cited_id)
        owned = (
            [cited]
            if cited is not None and label and label in cited.text
            else [segment for segment in segments if label and label in segment.text]
        )
        # A short label is accepted only when the cited source span has one
        # occurrence.  Ambiguous spans remain unresolved for structural audit.
        if len(owned) == 1 and (
            len(textmatch.condense(label)) > 3
            or owned[0].text.count(label) == 1
        ):
            candidate["source_segment_id"] = owned[0].segment_id
            model_quote = str(candidate.get("source_quote") or "").strip()
            candidate["source_quote"] = (
                model_quote
                if model_quote and model_quote in owned[0].text and label in model_quote
                else owned[0].text
            )
        else:
            candidate["source_segment_id"] = ""
            candidate["source_quote"] = ""
    return candidates


async def extract_current_identity_candidates(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    existing_resolutions: list[dict] | None = None,
) -> list[dict]:
    """Extract current-episode identities without future or coverage prompts."""
    candidates = await _discover_character_candidates_legacy(
        source_text,
        bible,
        episode_no,
        draft_text=draft_text,
        future_text="",
        existing_resolutions=existing_resolutions,
    )
    return _attach_candidate_source_evidence(candidates, source_text)


async def resolve_future_identity_candidates(
    candidates: list[dict],
    *,
    source_text: str,
    future_text: str,
    bible: Bible,
    episode_no: int,
    future_label: str = "",
) -> list[dict]:
    """Resolve only current unresolved identities from bounded future windows."""
    unresolved_onscreen_groups = {
        str(item.get("identity_group") or "").strip()
        for item in candidates
        if item.get("identity_kind") == "functional"
        and item.get("kind") == "onscreen"
        and str(item.get("identity_group") or "").strip()
    }
    unresolved = [
        dict(item) for item in candidates
        if item.get("identity_kind") == "functional"
        and (
            item.get("kind") == "onscreen"
            or str(item.get("identity_group") or "").strip()
            in unresolved_onscreen_groups
            or str(item.get("source_label") or "").strip() in future_text
        )
    ]
    if not unresolved or not str(future_text or "").strip():
        return candidates
    known_names = [character.name for character in bible.characters if character.name]
    future_context = _future_identity_context(
        future_text,
        [str(item.get("source_label") or "") for item in unresolved],
        known_names=known_names,
        current_text=source_text,
    )
    if not future_context:
        return candidates
    authority_projection = [
        {"authority_id": f"bible:{name}", "canonical_name": name}
        for name in known_names
    ]
    prompt = f"""任务：只为当前集尚未确认的身份做后续姓名消歧。
当前未决身份（不可新增列表外人物）：
{json.dumps(unresolved, ensure_ascii=False, separators=(',', ':'))}
候选人物权威（只用于精确绑定，不发送其未来剧情窗口）：
{json.dumps(authority_projection, ensure_ascii=False, separators=(',', ':'))}
后续局部窗口（{future_label or '后续章节'}）：
{future_context}
规则：source_label 必须逐字引用当前未决列表；只有窗口或权威投影存在唯一同一性证据时才输出 named；
不得输出只在后续出场的人。只输出 JSON：
{{"characters":[{{"source_label":"","canonical_name":"","identity_kind":"named","future_evidence":""}}]}}"""

    def validate_response(value: _IdentityCandidateResponse) -> list[str]:
        allowed = {str(item.get("source_label") or "") for item in unresolved}
        errors: list[str] = []
        for item in value.characters:
            source_label = str(item.get("source_label") or "").strip()
            canonical_name = str(item.get("canonical_name") or "").strip()
            if source_label not in allowed:
                errors.append(f"source_label 越界：{source_label}")
            if not canonical_name or (
                canonical_name not in future_context
                and canonical_name not in known_names
            ):
                errors.append(f"canonical_name 无证据：{canonical_name}")
        return errors

    response = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_IdentityCandidateResponse,
        validate=validate_response,
        operation_id=(
            f"screenplay.identity.future.v2:{episode_no}:"
            + evidence_repository.content_hash({
                "unresolved": unresolved,
                "future_context": future_context,
            })
        ),
        max_tokens=4096,
        temperature=0.1,
        call_meta={
            "stage": "discover_character_candidates",
            "stage_key": "screenplay_character_discovery",
            "substage": "future_identity",
            "discovery_phase": "future_identity",
            "episode_no": episode_no,
        },
        repair_context=future_context,
    )
    resolved_by_label = {
        str(item.get("source_label") or "").strip(): item
        for item in response.characters
        if (
            str(item.get("source_label") or "").strip()
            and str(item.get("canonical_name") or "").strip()
            and (
                str(item.get("identity_kind") or "").strip().lower() == "named"
                or str(item.get("canonical_name") or "").strip() in known_names
            )
        )
    }
    group_resolution: dict[str, dict] = {}
    candidate_by_label = {
        str(item.get("source_label") or "").strip(): item
        for item in unresolved
    }
    for label, resolution in resolved_by_label.items():
        candidate = candidate_by_label.get(label)
        group = str((candidate or {}).get("identity_group") or "").strip()
        if group:
            group_resolution[group] = resolution
    merged: list[dict] = []
    for item in candidates:
        resolution = (
            resolved_by_label.get(str(item.get("source_label") or ""))
            or group_resolution.get(str(item.get("identity_group") or "").strip())
        )
        if not resolution:
            merged.append(item)
            continue
        canonical_name = str(resolution.get("canonical_name") or "").strip()
        merged.append({
            **item,
            "name": canonical_name,
            "identity_kind": "named",
            "future_evidence": str(
                resolution.get("future_evidence") or ""
            )[:120],
        })
    return merged


async def audit_identity_coverage_from_structural_evidence(
    candidates: list[dict],
    *,
    structural_evidence: list[dict] | None,
    source_text: str,
    bible: Bible,
    episode_no: int,
) -> list[dict]:
    """Audit only typed Blueprint/IR references that lack identity ownership."""
    evidence = [item for item in (structural_evidence or []) if isinstance(item, dict)]
    if not evidence:
        return candidates
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    minimal = []
    for item in evidence:
        source_ids = [
            str(value) for value in item.get("source_segment_ids") or []
            if str(value) in source_by_id
        ]
        minimal.append({
            **item,
            "source_segment_ids": source_ids,
            "source_segments": {
                source_id: source_by_id[source_id] for source_id in source_ids
            },
        })
    prompt = (
        "任务：审计结构化蓝图/IR 中未绑定的人物引用。只处理给定引用及其 owned SRC，"
        "不得重扫全章或新增无关人物。已有人物候选：\n"
        + json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
        + "\n已有角色权威投影：\n"
        + json.dumps(
            [
                {"authority_id": f"bible:{character.name}", "canonical_name": character.name}
                for character in bible.characters if character.name
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n未决结构证据：\n"
        + json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
        + "\n只输出 JSON："
        '{"characters":[{"source_label":"原文逐字称谓","canonical_name":"真名或空串",'
        '"identity_kind":"named|functional","identity_group":"稳定分组",'
        '"kind":"onscreen","evidence":"依据"}]}'
    )
    response = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_IdentityCandidateResponse,
        validate=None,
        operation_id=(
            f"screenplay.identity.coverage.v2:{episode_no}:"
            + evidence_repository.content_hash(minimal)
        ),
        max_tokens=4096,
        temperature=0.05,
        call_meta={
            "stage": "discover_character_candidates",
            "stage_key": "screenplay_character_discovery",
            "substage": "structural_coverage",
            "discovery_phase": "coverage",
            "episode_no": episode_no,
        },
    )
    existing = {
        (str(item.get("source_label") or ""), str(item.get("identity_group") or ""))
        for item in candidates
    }
    additions: list[dict] = []
    owned_text = "\n".join(
        text for item in minimal for text in item.get("source_segments", {}).values()
    )
    for raw in response.characters:
        label = str(raw.get("source_label") or "").strip()
        if not label or label not in owned_text:
            continue
        identity_kind = str(raw.get("identity_kind") or "functional")
        canonical_name = str(raw.get("canonical_name") or "").strip()
        group = str(raw.get("identity_group") or f"structural:{label}")
        if (label, group) in existing:
            continue
        additions.append({
            "name": canonical_name or label,
            "source_label": label,
            "identity_kind": identity_kind if identity_kind in {"named", "functional"} else "functional",
            "identity_group": group,
            "kind": "onscreen",
            "evidence": str(raw.get("evidence") or "")[:80],
            "future_evidence": "",
        })
    return _attach_candidate_source_evidence([*candidates, *additions], source_text)


async def discover_character_candidates(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    future_text: str = "",
    future_label: str = "",
    existing_resolutions: list[dict] | None = None,
    structural_evidence: list[dict] | None = None,
    scope_id: str | None = None,
) -> list[dict]:
    """Targeted identity pipeline: current, unresolved future, typed audit."""
    artifact_scope_id = str(scope_id or f"episode-{episode_no}")
    targeted = str(
        get_setting("screenplay_targeted_identity_enabled") or "true"
    ).strip().lower() not in {"0", "false", "off", "no"}
    input_hash = evidence_repository.content_hash({
        "contract_version": "screenplay-identity-discovery.v2",
        "mode": "targeted" if targeted else "legacy",
        "episode_no": episode_no,
        "source_text": source_text,
        "draft_text": draft_text,
        "future_text": future_text,
        "future_label": future_label,
        "bible": bible.model_dump(mode="json"),
        "existing_resolutions": existing_resolutions or [],
        "structural_evidence": structural_evidence or [],
    })
    evidence_conn = get_conn()
    artifacts_available = bool(
        scope_id
        and evidence_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
        ).fetchone()
    )
    cached_rows = (
        evidence_conn.execute(
            """SELECT content_json FROM artifacts
                 WHERE scope_type='episode' AND scope_id=?
                   AND type='screenplay_identity_discovery' AND status='validated'
                 ORDER BY created_at DESC LIMIT 20""",
            (artifact_scope_id,),
        ).fetchall()
        if artifacts_available else []
    )
    for row in cached_rows:
        try:
            cached = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            cached.get("contract_version") == "screenplay-identity-discovery.v2"
            and cached.get("input_hash") == input_hash
            and isinstance(cached.get("candidates"), list)
        ):
            return [dict(item) for item in cached["candidates"] if isinstance(item, dict)]

    if targeted:
        current = await extract_current_identity_candidates(
            source_text,
            bible,
            episode_no,
            draft_text=draft_text,
            existing_resolutions=existing_resolutions,
        )
        resolved = await resolve_future_identity_candidates(
            current,
            source_text=source_text,
            future_text=future_text,
            bible=bible,
            episode_no=episode_no,
            future_label=future_label,
        )
        audited = await audit_identity_coverage_from_structural_evidence(
            resolved,
            structural_evidence=structural_evidence,
            source_text=source_text,
            bible=bible,
            episode_no=episode_no,
        )
    else:
        audited = _attach_candidate_source_evidence(
            await _discover_character_candidates_legacy(
                source_text,
                bible,
                episode_no,
                draft_text=draft_text,
                future_text=future_text,
                future_label=future_label,
                existing_resolutions=existing_resolutions,
            ),
            source_text,
        )
    trace = None
    try:
        from app.observability.tracing import current_trace
        trace = current_trace()
    except Exception:  # noqa: BLE001 - evidence is optional outside workflows
        pass
    if not artifacts_available:
        return audited
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery_raw",
            scope_type="episode",
            scope_id=artifact_scope_id,
            status="candidate",
            trust_level="T0",
            content={
                "contract_version": "screenplay-identity-discovery.v2",
                "input_hash": input_hash,
                "mode": "targeted" if targeted else "legacy",
                "model_candidates": audited,
            },
            contract_version="screenplay-identity-discovery.v2",
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery",
            scope_type="episode",
            scope_id=artifact_scope_id,
            status="validated",
            trust_level="T1",
            content={
                "contract_version": "screenplay-identity-discovery.v2",
                "episode_no": episode_no,
                "candidates": audited,
                "source_hash": evidence_repository.content_hash(source_text),
                "input_hash": input_hash,
                "mode": "targeted" if targeted else "legacy",
            },
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version="screenplay-identity-discovery.v2",
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    return audited


def _identity_resolution(
    item: dict,
    canonical_name: str,
    resolution: str,
    *,
    reason: str = "",
) -> dict:
    return normalize_character_resolution({
        "source_label": str(item.get("source_label") or item.get("name") or "").strip(),
        "canonical_name": canonical_name,
        "resolution": resolution,
        "reason": reason,
        "evidence": str(item.get("evidence") or "").strip()[:80],
        "future_evidence": str(item.get("future_evidence") or "").strip()[:120],
        "identity_group": str(item.get("identity_group") or "").strip()[:96],
    })


def _replace_resolved_label(text: str, source_label: str, canonical_name: str) -> str:
    if not text or source_label == canonical_name:
        return text
    if canonical_name.startswith(source_label):
        suffix = canonical_name[len(source_label):]
        if suffix:
            return re.sub(
                re.escape(source_label) + rf"(?!{re.escape(suffix)})",
                canonical_name,
                text,
            )
    return text.replace(source_label, canonical_name)


def _replace_screenplay_body_label(
    text: str,
    source_label: str,
    canonical_name: str,
    *,
    replace_prose: bool = True,
    replace_speaker: bool = True,
) -> str:
    """改剧本正文中的角色身份，不改其他角色说出的台词内容。"""
    lines: list[str] = []
    speaker_pattern = re.compile(
        rf"^(?P<indent>\s*){re.escape(source_label)}(?P<emotion>[\(（][^\)）]{{0,16}}[\)）])?(?P<colon>[:：])"
    )
    any_dialogue_pattern = re.compile(
        r"^\s*[\u3400-\u9fffA-Za-z0-9_·•・·-]{1,16}(?:[\(（][^\)）]{0,16}[\)）])?[:：]"
    )
    for line in (text or "").splitlines(keepends=True):
        if replace_speaker and speaker_pattern.match(line):
            line = speaker_pattern.sub(
                lambda match: (
                    f"{match.group('indent')}{canonical_name}"
                    f"{match.group('emotion') or ''}{match.group('colon')}"
                ),
                line,
                count=1,
            )
        elif replace_prose and not any_dialogue_pattern.match(line):
            line = _replace_resolved_label(line, source_label, canonical_name)
        lines.append(line)
    return "".join(lines)


def _restore_non_dialogue_prefix(
    text: str,
    source_label: str,
    canonical_name: str,
    *,
    authoritative_lines: set[str],
) -> str:
    """Restore a structural prefix previously mistaken for a speaker."""
    prefix = re.compile(
        rf"^(?P<indent>\s*){re.escape(canonical_name)}(?P<colon>[:：])"
        r"(?P<line>.*)$"
    )
    lines: list[str] = []
    for raw_line in (text or "").splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line):]
        match = prefix.match(line)
        if (
            match is not None
            and match.group("line").strip() not in authoritative_lines
        ):
            line = (
                f"{match.group('indent')}{source_label}"
                f"{match.group('colon')}{match.group('line')}"
            )
        lines.append(line + ending)
    return "".join(lines)


def _replace_identity_value(value, source_label: str, canonical_name: str):
    """Replace exact identity values recursively without touching source spans."""
    if isinstance(value, str):
        return canonical_name if value == source_label else value
    if isinstance(value, list):
        return [
            _replace_identity_value(item, source_label, canonical_name)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _replace_identity_value(item, source_label, canonical_name)
            for item in value
        )
    if isinstance(value, dict):
        return {
            (
                canonical_name if str(key) == source_label else key
            ): _replace_identity_value(item, source_label, canonical_name)
            for key, item in value.items()
        }
    return value


def _identity_value_contains(value, identity: str) -> bool:
    if isinstance(value, str):
        return value == identity
    if isinstance(value, (list, tuple)):
        return any(_identity_value_contains(item, identity) for item in value)
    if isinstance(value, dict):
        return any(
            str(key) == identity or _identity_value_contains(item, identity)
            for key, item in value.items()
        )
    return False


def _replace_narrative_plan_identity(
    plan,
    source_label: str,
    canonical_name: str,
    *,
    replace_display_text: bool = True,
) -> bool:
    """Atomically update every authoritative entity reference in one plan.

    SourceEvidence and direct source excerpts remain immutable.  The mapping is
    AI/project supplied; this routine validates no role vocabulary and merely
    applies one resolved identity consistently across the relation graph.
    """
    if plan is None:
        return False
    before = plan.model_dump(mode="json")

    for contract in plan.identity_contracts:
        if replace_display_text and contract.display_name == source_label:
            contract.display_name = canonical_name
        contract.voice_ids = list(dict.fromkeys(
            canonical_name if voice_id == source_label else voice_id
            for voice_id in contract.voice_ids
        ))
        if replace_display_text:
            contract.evidence.rationale = _replace_resolved_label(
                contract.evidence.rationale, source_label, canonical_name,
            )
    for proposition in plan.propositions:
        proposition.entity_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in proposition.entity_ids
        ))
        if replace_display_text:
            proposition.canonical_statement = _replace_resolved_label(
                proposition.canonical_statement, source_label, canonical_name,
            )
    for fact in plan.state_facts:
        if fact.subject_id == source_label:
            fact.subject_id = canonical_name
        fact.value.data = _replace_identity_value(
            fact.value.data, source_label, canonical_name,
        )
    for evidence in plan.evidence:
        evidence.perceivable_by = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in evidence.perceivable_by
        ))
        if replace_display_text:
            evidence.observable_claim = _replace_resolved_label(
                evidence.observable_claim, source_label, canonical_name,
            )
        evidence.competing_attention_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in evidence.competing_attention_ids
        ))
    for question in plan.dramatic_questions:
        if replace_display_text:
            question.question_text = _replace_resolved_label(
                question.question_text, source_label, canonical_name,
            )
    for action in plan.atomic_actions:
        action.actor_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in action.actor_ids
        ))
        action.target_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in action.target_ids
        ))
        if replace_display_text:
            for field in ("semantic_intent", "completion_condition", "decision_not_applicable_reason"):
                value = getattr(action, field, None)
                if isinstance(value, str):
                    setattr(action, field, _replace_resolved_label(value, source_label, canonical_name))
            for phase in action.temporal_phases:
                phase.start_condition = _replace_resolved_label(
                    phase.start_condition, source_label, canonical_name,
                )
                phase.end_condition = _replace_resolved_label(
                    phase.end_condition, source_label, canonical_name,
                )
    for event in plan.events:
        event.character_goal_effects = _replace_identity_value(
            event.character_goal_effects, source_label, canonical_name,
        )
    for state in plan.character_states:
        if state.character_id == source_label:
            state.character_id = canonical_name
        state.relationship_state = _replace_identity_value(
            state.relationship_state, source_label, canonical_name,
        )
        state.emotion = _replace_identity_value(
            state.emotion, source_label, canonical_name,
        )
        if replace_display_text:
            state.tactic = _replace_resolved_label(
                state.tactic, source_label, canonical_name,
            )
    for belief in plan.character_beliefs:
        if belief.character_id == source_label:
            belief.character_id = canonical_name
    for prior in plan.audience_priors:
        if replace_display_text:
            prior.audience_description = _replace_resolved_label(
                prior.audience_description, source_label, canonical_name,
            )
        prior.familiarity_assumptions = _replace_identity_value(
            prior.familiarity_assumptions, source_label, canonical_name,
        )
    for state in plan.audience_states:
        for field in (
            "causal_hypotheses",
            "character_goal_hypotheses",
            "spatial_model",
            "temporal_model",
            "working_memory",
            "affective_state",
        ):
            setattr(
                state,
                field,
                _replace_identity_value(
                    getattr(state, field), source_label, canonical_name,
                ),
            )
        state.attention_residue_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in state.attention_residue_ids
        ))
    for intent in plan.experience_intents:
        intent.attention_target_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in intent.attention_target_ids
        ))
        if replace_display_text:
            intent.director_objective = _replace_resolved_label(
                intent.director_objective, source_label, canonical_name,
            )
            intent.forbidden_misconceptions = [
                _replace_resolved_label(value, source_label, canonical_name)
                for value in intent.forbidden_misconceptions
            ]
    for scene in plan.scene_contracts:
        if scene.point_of_view_character_id == source_label:
            scene.point_of_view_character_id = canonical_name
        scene.relationship_deltas = _replace_identity_value(
            scene.relationship_deltas, source_label, canonical_name,
        )
        if replace_display_text:
            for field in (
                "not_applicable_reason",
                "alternative_dramatic_function",
                "value_polarity_in",
                "value_polarity_out",
                "scene_button",
            ):
                value = getattr(scene, field, None)
                if isinstance(value, str):
                    setattr(scene, field, _replace_resolved_label(value, source_label, canonical_name))
    for arc in plan.arc_contracts:
        if replace_display_text:
            for field in ("not_applicable_reason", "alternative_dramatic_function"):
                value = getattr(arc, field, None)
                if isinstance(value, str):
                    setattr(arc, field, _replace_resolved_label(value, source_label, canonical_name))
        arc.pressure_curve = _replace_identity_value(
            arc.pressure_curve, source_label, canonical_name,
        )
        arc.information_density_curve = _replace_identity_value(
            arc.information_density_curve, source_label, canonical_name,
        )
        arc.processing_beats = _replace_identity_value(
            arc.processing_beats, source_label, canonical_name,
        )
    return plan.model_dump(mode="json") != before


def _merge_duplicate_narrative_identity_contracts(plan) -> list[dict]:
    """Merge aliases that resolve to one canonical display identity."""
    if plan is None:
        return []
    data = plan.model_dump(mode="json")
    contracts = list(data.get("identity_contracts") or [])
    groups: dict[str, list[tuple[int, dict]]] = {}
    for index, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            continue
        display_name = str(contract.get("display_name") or "").strip()
        if display_name:
            groups.setdefault(display_name, []).append((index, contract))

    replacements: dict[str, str] = {}
    merged_by_display: dict[str, dict] = {}
    changes: list[dict] = []
    for display_name, members in groups.items():
        if len(members) < 2:
            continue
        _canonical_index, canonical = max(
            members,
            key=lambda item: (
                int(str(item[1].get("identity_id") or "") == display_name),
                int(str(item[1].get("visual_policy") or "") == "canonical"),
                int(str(item[1].get("asset_requirement") or "") == "required"),
                -item[0],
            ),
        )
        canonical_id = str(canonical.get("identity_id") or "").strip()
        if not canonical_id:
            continue
        merged = dict(canonical)
        merged_evidence = dict(merged.get("evidence") or {})
        merged_voice_ids = list(merged.get("voice_ids") or [])
        rationales = [str(merged_evidence.get("rationale") or "").strip()]
        merged_ids: list[str] = []
        for _index, contract in members:
            identity_id = str(contract.get("identity_id") or "").strip()
            if identity_id and identity_id != canonical_id:
                replacements[identity_id] = canonical_id
                merged_ids.append(identity_id)
            merged_voice_ids.extend(contract.get("voice_ids") or [])
            evidence = contract.get("evidence") or {}
            for field in (
                "source_evidence_ids",
                "proposition_ids",
                "adaptation_decision_ids",
            ):
                merged_evidence[field] = list(dict.fromkeys([
                    *(merged_evidence.get(field) or []),
                    *(evidence.get(field) or []),
                ]))
            rationale = str(evidence.get("rationale") or "").strip()
            if rationale:
                rationales.append(rationale)
        merged["voice_ids"] = list(dict.fromkeys(merged_voice_ids))
        merged_evidence["rationale"] = "；".join(dict.fromkeys(filter(
            None,
            rationales,
        )))
        merged["evidence"] = merged_evidence
        merged_by_display[display_name] = merged
        changes.append({
            "kind": "identity_contract_merge",
            "display_name": display_name,
            "canonical_identity_id": canonical_id,
            "merged_identity_ids": merged_ids,
        })

    if not replacements:
        return []

    def replace_merged_ids(value):
        if isinstance(value, str):
            return replacements.get(value, value)
        if isinstance(value, list):
            replaced = [replace_merged_ids(item) for item in value]
            if not any(
                isinstance(item, str) and item in replacements
                for item in value
            ):
                return replaced
            deduplicated: list = []
            seen_strings: set[str] = set()
            for item in replaced:
                if isinstance(item, str):
                    if item in seen_strings:
                        continue
                    seen_strings.add(item)
                deduplicated.append(item)
            return deduplicated
        if isinstance(value, tuple):
            return tuple(replace_merged_ids(item) for item in value)
        if isinstance(value, dict):
            return {
                replacements.get(str(key), key): replace_merged_ids(item)
                for key, item in value.items()
            }
        return value

    data = replace_merged_ids(data)

    retained_contracts: list[dict] = []
    emitted_displays: set[str] = set()
    for contract in contracts:
        display_name = str(contract.get("display_name") or "").strip()
        merged = merged_by_display.get(display_name)
        if merged is not None:
            if display_name in emitted_displays:
                continue
            normalized = replace_merged_ids(merged)
            retained_contracts.append(normalized)
            emitted_displays.add(display_name)
            continue
        normalized = replace_merged_ids(contract)
        retained_contracts.append(normalized)
    data["identity_contracts"] = retained_contracts

    rebuilt = type(plan).model_validate(data)
    for field in type(plan).model_fields:
        setattr(plan, field, getattr(rebuilt, field))
    return changes


def apply_screenplay_character_resolutions(screenplay, resolutions: list[dict] | None) -> list[dict]:
    """在剧本进入 QA/发布之前原子性落实人物身份映射。

    原文证据字段（source_text/source_basis/source_fact/source_span）保持不变，
    避免破坏逐字证据；所有会被下游当成角色身份的字段统一改名。
    """
    changes: list[dict] = []
    authoritative_speakers = {
        str(turn.speaker or "").strip()
        for chain in getattr(screenplay, "dialogue_chains", None) or []
        for turn in chain.turns or []
        if str(turn.speaker or "").strip()
    }
    authoritative_lines_by_speaker: dict[str, set[str]] = {}
    for chain in getattr(screenplay, "dialogue_chains", None) or []:
        for turn in chain.turns or []:
            speaker = str(turn.speaker or "").strip()
            line = str(turn.line or "").strip()
            if speaker and line:
                authoritative_lines_by_speaker.setdefault(
                    speaker,
                    set(),
                ).add(line)
    for item in resolutions or []:
        if not isinstance(item, dict):
            continue
        # Occurrence-scoped identity decisions can legitimately share one
        # source label (for example two people both called “绿袍修士”).  Their
        # authority_id is already bound inside the IR, so a global text
        # replacement here would arbitrarily assign every occurrence to the
        # first entity and corrupt the compiled identity graph.
        if str(item.get("source_instance_key") or "").strip():
            continue
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        if not source_label or not canonical_name or source_label == canonical_name:
            continue
        replace_display_text = item.get("resolution") != "future_identity"

        changed = False
        for scene in getattr(screenplay, "scene_outline", None) or []:
            before = list(scene.characters or [])
            scene.characters = list(dict.fromkeys(
                canonical_name if name == source_label else name
                for name in before
            ))
            changed = changed or scene.characters != before
            if replace_display_text:
                for field in ("story_function", "summary", "conflict", "turn"):
                    value = getattr(scene, field, "") or ""
                    replaced = _replace_resolved_label(value, source_label, canonical_name)
                    if replaced != value:
                        setattr(scene, field, replaced)
                        changed = True

        body = getattr(screenplay, "full_script_text", "") or ""
        replaced_body = _replace_screenplay_body_label(
            body,
            source_label,
            canonical_name,
            replace_prose=replace_display_text,
            replace_speaker=source_label in authoritative_speakers,
        )
        if source_label not in authoritative_speakers:
            replaced_body = _restore_non_dialogue_prefix(
                replaced_body,
                source_label,
                canonical_name,
                authoritative_lines=authoritative_lines_by_speaker.get(
                    canonical_name,
                    set(),
                ),
            )
        if replaced_body != body:
            screenplay.full_script_text = replaced_body
            changed = True

        spine = getattr(screenplay, "plot_spine", None)
        if spine is not None:
            for beat in spine.spine_beats or []:
                for field in (
                    ("who", "does", "turn")
                    if replace_display_text
                    else ("who",)
                ):
                    value = getattr(beat, field, "") or ""
                    replaced = _replace_resolved_label(value, source_label, canonical_name)
                    if replaced != value:
                        setattr(beat, field, replaced)
                        changed = True

        for chain in getattr(screenplay, "dialogue_chains", None) or []:
            for turn in chain.turns or []:
                if (turn.speaker or "").strip() == source_label:
                    turn.speaker = canonical_name
                    changed = True

        for event in getattr(screenplay, "events", None) or []:
            if replace_display_text:
                for field in ("state_in", "trigger", "visible_change", "state_out", "adaptation_reason"):
                    value = getattr(event, field, "") or ""
                    replaced = _replace_resolved_label(value, source_label, canonical_name)
                    if replaced != value:
                        setattr(event, field, replaced)
                        changed = True

        for info in getattr(screenplay, "information_ledger", None) or []:
            if (info.speaker_id or "").strip() == source_label:
                info.speaker_id = canonical_name
                changed = True
            if replace_display_text:
                content = info.content or ""
                replaced = _replace_resolved_label(content, source_label, canonical_name)
                if replaced != content:
                    info.content = replaced
                    changed = True

        for voice in getattr(screenplay, "voice_bible", None) or []:
            if (voice.speaker_id or "").strip() == source_label:
                voice.speaker_id = canonical_name
                if getattr(screenplay, "narrative_plan", None) is not None:
                    if (
                        resolution_declares_functional_identity(item)
                        and str(voice.role_type or "").strip() != "narrator"
                    ):
                        voice.role_type = "functional_character"
                elif resolution_declares_functional_identity(item):
                    voice.role_type = "functional_character"
                changed = True

        changed = _replace_narrative_plan_identity(
            getattr(screenplay, "narrative_plan", None),
            source_label,
            canonical_name,
            replace_display_text=replace_display_text,
        ) or changed

        if replace_display_text:
            for field in (
                "logline", "dramatic_question", "protagonist_goal", "obstacle", "stakes",
                "emotional_curve", "ending_hook", "adaptation_direction", "opening", "development",
                "conflict", "climax", "episode_premise",
            ):
                value = getattr(screenplay, field, "") or ""
                replaced = _replace_resolved_label(value, source_label, canonical_name)
                if replaced != value:
                    setattr(screenplay, field, replaced)
                    changed = True
            for field in (
                "key_lines", "key_plot_points", "character_state_changes",
                "approved_adaptations", "forbidden_additions",
            ):
                values = list(getattr(screenplay, field, None) or [])
                replaced_values = [
                    _replace_resolved_label(value, source_label, canonical_name)
                    for value in values
                ]
                if replaced_values != values:
                    setattr(screenplay, field, replaced_values)
                    changed = True

        if changed:
            changes.append({
                "source_label": source_label,
                "canonical_name": canonical_name,
                "resolution": item.get("resolution") or "unknown",
            })
    changes.extend(_merge_duplicate_narrative_identity_contracts(
        getattr(screenplay, "narrative_plan", None),
    ))
    return changes


def normalize_screenplay_identity_annotations(screenplay, bible: Bible) -> list[dict]:
    """Strip carrier annotations only when the base is already authoritative.

    Identity fields may contain presentation notes such as ``角色（画外）``.
    This normalization never interprets the note or classifies role names. It
    only projects an exact Bible/contract/voice token back to its canonical
    display name; ambiguous or unknown bases remain unresolved for model audit.
    """
    visual_targets: dict[str, set[str]] = {}
    voice_targets: dict[str, set[str]] = {}

    def register(targets: dict[str, set[str]], token: object, canonical: str) -> None:
        value = str(token or "").strip()
        if value and canonical:
            targets.setdefault(value, set()).add(canonical)

    for character in bible.characters:
        name = str(character.name or "").strip()
        register(visual_targets, name, name)
        register(voice_targets, name, name)

    plan = getattr(screenplay, "narrative_plan", None)
    for contract in (getattr(plan, "identity_contracts", None) or []):
        canonical = str(contract.display_name or "").strip()
        if str(contract.visual_policy or "").strip() != "offscreen_only":
            register(visual_targets, contract.identity_id, canonical)
            register(visual_targets, contract.display_name, canonical)
        for voice_id in contract.voice_ids or []:
            register(voice_targets, voice_id, canonical)

    for voice in getattr(screenplay, "voice_bible", None) or []:
        if str(voice.role_type or "").strip() == "narrator":
            speaker_id = str(voice.speaker_id or "").strip()
            register(voice_targets, speaker_id, speaker_id)

    usages: dict[str, set[str]] = {}

    def collect(raw: object, usage: str) -> None:
        value = str(raw or "").strip()
        if _identity_carrier_annotation_base(value):
            usages.setdefault(value, set()).add(usage)

    for scene in getattr(screenplay, "scene_outline", None) or []:
        for character in scene.characters or []:
            collect(character, "visual")
    for chain in getattr(screenplay, "dialogue_chains", None) or []:
        for turn in chain.turns or []:
            collect(turn.speaker, "voice")
    for item in getattr(screenplay, "information_ledger", None) or []:
        collect(item.speaker_id, "voice")
    for voice in getattr(screenplay, "voice_bible", None) or []:
        collect(voice.speaker_id, "voice")
    from app.validators import screenplay_speaker_names
    for speaker in screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or ""
    ):
        collect(speaker, "voice")

    resolutions: list[dict] = []
    target_maps = {"visual": visual_targets, "voice": voice_targets}
    for source_label, required_usages in usages.items():
        base = _identity_carrier_annotation_base(source_label)
        candidates: set[str] | None = None
        for usage in required_usages:
            current = target_maps[usage].get(base, set())
            candidates = set(current) if candidates is None else candidates & current
        if candidates and len(candidates) == 1:
            resolutions.append({
                "source_label": source_label,
                "canonical_name": next(iter(candidates)),
                "resolution": "authority_annotation",
            })
    if not resolutions:
        return []
    return apply_screenplay_character_resolutions(screenplay, resolutions)


def normalize_screenplay_offscreen_visual_identities(screenplay) -> list[dict]:
    """Remove typed offscreen-only identities from visual scene membership."""
    plan = getattr(screenplay, "narrative_plan", None)
    if plan is None:
        return []
    offscreen_tokens = {
        token
        for contract in plan.identity_contracts
        if str(contract.visual_policy or "").strip() == "offscreen_only"
        for token in {
            str(contract.identity_id or "").strip(),
            str(contract.display_name or "").strip(),
            *(
                str(voice_id or "").strip()
                for voice_id in (contract.voice_ids or [])
            ),
        }
        if token
    }
    if not offscreen_tokens:
        return []

    changes: list[dict] = []
    for scene in getattr(screenplay, "scene_outline", None) or []:
        before = list(scene.characters or [])
        scene.characters = [
            identity for identity in before
            if str(identity or "").strip() not in offscreen_tokens
        ]
        removed = [
            identity for identity in before
            if str(identity or "").strip() in offscreen_tokens
        ]
        if removed:
            changes.append({
                "source_label": ",".join(str(value) for value in removed),
                "canonical_name": "",
                "resolution": "offscreen_visual_membership_removed",
                "scene_no": scene.scene_no,
            })
    return changes


def normalize_screenplay_voice_ids(screenplay, bible: Bible) -> list[dict]:
    """Normalize voice aliases and remove unreferenced non-identity entries.

    New prompts require Bible character names as speaker IDs.  This migration
    path handles existing working artifacts without guessing from initials or
    role labels: the alias must own ledger text that names exactly one Bible
    character, and that character must actually speak in the screenplay.
    Ambiguous or referenced aliases remain untouched so the identity gate still
    fails closed. Unbound entries that no spoken field references are dead
    metadata, not identities, and are removed without inspecting their names or
    role labels.
    """
    changes = normalize_screenplay_identity_annotations(screenplay, bible)
    plan = getattr(screenplay, "narrative_plan", None)
    if plan is None:
        return changes
    bible_names = {
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
    }
    for voice in getattr(screenplay, "voice_bible", None) or []:
        speaker_id = str(voice.speaker_id or "").strip()
        role_type = str(voice.role_type or "").strip()
        if not speaker_id:
            continue
        matching_contracts = [
            contract
            for contract in plan.identity_contracts
            if (
                speaker_id in {
                    str(contract.identity_id or "").strip(),
                    str(contract.display_name or "").strip(),
                }
                and (
                    role_type != "narrator"
                    or str(contract.visual_policy or "").strip()
                    == "offscreen_only"
                )
            )
        ]
        if len(matching_contracts) != 1:
            continue
        contract = matching_contracts[0]
        before = list(contract.voice_ids or [])
        if speaker_id not in before:
            contract.voice_ids = [*before, speaker_id]
            changes.append({
                "source_label": speaker_id,
                "canonical_name": speaker_id,
                "resolution": (
                    "narrator_voice_contract_bound"
                    if role_type == "narrator"
                    else "voice_contract_bound"
                ),
            })
    explicitly_bound = {
        str(voice_id or "").strip()
        for contract in plan.identity_contracts
        for voice_id in contract.voice_ids
        if str(voice_id or "").strip()
    }
    from app.validators import screenplay_speaker_names

    dialogue_speakers = {
        str(turn.speaker or "").strip()
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip()
    }
    dialogue_speakers.update(screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or "",
    ))
    dialogue_turns = [
        (
            str(turn.speaker or "").strip(),
            str(turn.line or "").strip(),
        )
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip() and str(turn.line or "").strip()
    ]

    def alias_candidate(ledger_items) -> str | None:
        ledger_text = "\n".join(
            f"{item.content or ''}\n{item.exact_text or ''}"
            for item in ledger_items
        )
        exact_texts = {
            str(item.exact_text or "").strip()
            for item in ledger_items
            if str(item.exact_text or "").strip()
        }
        exact_speakers = {
            speaker
            for speaker, line in dialogue_turns
            for exact_text in exact_texts
            if (
                speaker in bible_names
                and (exact_text == line or exact_text in line or line in exact_text)
            )
        }
        mentioned_candidates = {
            name
            for name in bible_names
            if name in dialogue_speakers and name in ledger_text
        }
        leading_candidates = {
            name
            for name in mentioned_candidates
            if any(
                str(item.content or "").strip().startswith(name)
                for item in ledger_items
            )
        }
        candidates = (
            exact_speakers
            if len(exact_speakers) == 1
            else mentioned_candidates
            if len(mentioned_candidates) == 1
            else leading_candidates
        )
        return next(iter(candidates)) if len(candidates) == 1 else None

    voice_delivery_owners = {"spoken_dialogue", "offscreen_voice", "narration"}
    non_voice_carriers: set[str] = set()
    for voice in getattr(screenplay, "voice_bible", None) or []:
        source_id = str(voice.speaker_id or "").strip()
        if (
            not source_id
            or str(voice.role_type or "").strip() == "narrator"
            or source_id in bible_names
            or source_id in explicitly_bound
        ):
            continue
        ledger_items = [
            item
            for item in (getattr(screenplay, "information_ledger", None) or [])
            if str(item.speaker_id or "").strip() == source_id
        ]
        if alias_candidate(ledger_items):
            continue
        if ledger_items and all(
            str(item.delivery_owner or "").strip() not in voice_delivery_owners
            for item in ledger_items
        ):
            non_voice_carriers.add(source_id)

    if non_voice_carriers:
        for item in getattr(screenplay, "information_ledger", None) or []:
            if str(item.speaker_id or "").strip() in non_voice_carriers:
                item.speaker_id = None
        for chain in getattr(screenplay, "dialogue_chains", None) or []:
            chain.turns = [
                turn for turn in (chain.turns or [])
                if str(turn.speaker or "").strip() not in non_voice_carriers
            ]
        screenplay.dialogue_chains = [
            chain for chain in (getattr(screenplay, "dialogue_chains", None) or [])
            if chain.turns
        ]
        retained_key_lines: list[str] = []
        for line in getattr(screenplay, "key_lines", None) or []:
            speaker, separator, _ = str(line or "").partition("：")
            if not separator:
                speaker, separator, _ = str(line or "").partition(":")
            if separator and speaker.strip() in non_voice_carriers:
                continue
            retained_key_lines.append(line)
        screenplay.key_lines = retained_key_lines
        body = getattr(screenplay, "full_script_text", "") or ""
        for source_id in sorted(non_voice_carriers):
            body = re.sub(
                rf"(?m)^(\s*){re.escape(source_id)}"
                r"(?:[\(（][^\)）]{0,16}[\)）])?\s*[:：]\s*(.*)$",
                lambda match: f"{match.group(1)}【{match.group(2).strip()}】",
                body,
            )
        screenplay.full_script_text = body
        screenplay.voice_bible = [
            voice
            for voice in (getattr(screenplay, "voice_bible", None) or [])
            if str(voice.speaker_id or "").strip() not in non_voice_carriers
        ]
        non_voice_changes = [{
            "source_label": source_id,
            "canonical_name": "",
            "resolution": "non_voice_carrier_removed",
        } for source_id in sorted(non_voice_carriers)]
    else:
        non_voice_changes = []

    dialogue_speakers = {
        str(turn.speaker or "").strip()
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip()
    }
    dialogue_speakers.update(screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or "",
    ))
    ledger_speakers = {
        str(item.speaker_id or "").strip()
        for item in (getattr(screenplay, "information_ledger", None) or [])
        if str(item.speaker_id or "").strip()
    }
    referenced_speakers = dialogue_speakers | ledger_speakers
    existing_voice_ids = {
        str(voice.speaker_id or "").strip()
        for voice in (getattr(screenplay, "voice_bible", None) or [])
        if str(voice.speaker_id or "").strip()
    }
    unreferenced_voice_ids: set[str] = set()

    for voice in getattr(screenplay, "voice_bible", None) or []:
        source_id = str(voice.speaker_id or "").strip()
        if (
            not source_id
            or str(voice.role_type or "").strip() == "narrator"
            or source_id in bible_names
            or source_id in explicitly_bound
        ):
            continue
        ledger_items = [
            item
            for item in (getattr(screenplay, "information_ledger", None) or [])
            if str(item.speaker_id or "").strip() == source_id
        ]
        if not ledger_items:
            if source_id not in referenced_speakers:
                unreferenced_voice_ids.add(source_id)
            continue
        canonical_name = alias_candidate(ledger_items)
        if not canonical_name:
            continue
        if canonical_name in existing_voice_ids:
            continue

        voice.speaker_id = canonical_name
        for item in ledger_items:
            item.speaker_id = canonical_name
        existing_voice_ids.discard(source_id)
        existing_voice_ids.add(canonical_name)
        changes.append({
            "source_label": source_id,
            "canonical_name": canonical_name,
            "resolution": "voice_alias_from_ledger",
        })

    changes.extend(non_voice_changes)
    if unreferenced_voice_ids:
        screenplay.voice_bible = [
            voice
            for voice in (getattr(screenplay, "voice_bible", None) or [])
            if str(voice.speaker_id or "").strip() not in unreferenced_voice_ids
        ]
        changes.extend({
            "source_label": source_id,
            "canonical_name": "",
            "resolution": "unreferenced_voice_removed",
        } for source_id in sorted(unreferenced_voice_ids))

    changes.extend(normalize_screenplay_offscreen_visual_identities(screenplay))
    return changes


def screenplay_character_resolution_errors(screenplay, resolutions: list[dict] | None) -> list[str]:
    """剧本发布前硬门禁：过渡称谓不得再占据任何角色身份位。"""
    errors: list[str] = []
    for item in resolutions or []:
        if not isinstance(item, dict):
            continue
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        if not source_label or not canonical_name or source_label == canonical_name:
            continue
        preserves_current_display = item.get("resolution") == "future_identity"
        residual_paths: list[str] = []
        for scene in getattr(screenplay, "scene_outline", None) or []:
            if source_label in (scene.characters or []):
                residual_paths.append(f"scene_outline[{scene.scene_no}].characters")
        for chain_index, chain in enumerate(getattr(screenplay, "dialogue_chains", None) or []):
            for turn_index, turn in enumerate(chain.turns or []):
                if (turn.speaker or "").strip() == source_label:
                    residual_paths.append(f"dialogue_chains[{chain_index}].turns[{turn_index}].speaker")
        for index, info in enumerate(getattr(screenplay, "information_ledger", None) or []):
            if (info.speaker_id or "").strip() == source_label:
                residual_paths.append(f"information_ledger[{index}].speaker_id")
        for index, voice in enumerate(getattr(screenplay, "voice_bible", None) or []):
            if (voice.speaker_id or "").strip() == source_label:
                residual_paths.append(f"voice_bible[{index}].speaker_id")
        body = getattr(screenplay, "full_script_text", "") or ""
        speaker_pattern = re.compile(
            rf"(?m)^\s*{re.escape(source_label)}(?:[\(（][^\)）]{{0,16}}[\)）])?[:：]"
        )
        if not preserves_current_display and speaker_pattern.search(body):
            residual_paths.append("full_script_text.speaker")
        plan = getattr(screenplay, "narrative_plan", None)
        if plan is not None:
            for index, proposition in enumerate(plan.propositions):
                if source_label in proposition.entity_ids:
                    residual_paths.append(f"narrative_plan.propositions[{index}].entity_ids")
            for index, fact in enumerate(plan.state_facts):
                if fact.subject_id == source_label or _identity_value_contains(
                    fact.value.data, source_label,
                ):
                    residual_paths.append(f"narrative_plan.state_facts[{index}]")
            for index, evidence in enumerate(plan.evidence):
                if source_label in {
                    *evidence.perceivable_by,
                    *evidence.competing_attention_ids,
                }:
                    residual_paths.append(f"narrative_plan.evidence[{index}]")
            for index, action in enumerate(plan.atomic_actions):
                if source_label in {*action.actor_ids, *action.target_ids}:
                    residual_paths.append(f"narrative_plan.atomic_actions[{index}]")
            for index, state in enumerate(plan.character_states):
                if (
                    state.character_id == source_label
                    or _identity_value_contains(state.relationship_state, source_label)
                    or _identity_value_contains(state.emotion, source_label)
                ):
                    residual_paths.append(f"narrative_plan.character_states[{index}]")
            for index, belief in enumerate(plan.character_beliefs):
                if belief.character_id == source_label:
                    residual_paths.append(f"narrative_plan.character_beliefs[{index}]")
            for index, state in enumerate(plan.audience_states):
                if any(
                    _identity_value_contains(getattr(state, field), source_label)
                    for field in (
                        "causal_hypotheses",
                        "character_goal_hypotheses",
                        "spatial_model",
                        "temporal_model",
                        "working_memory",
                        "attention_residue_ids",
                        "affective_state",
                    )
                ):
                    residual_paths.append(f"narrative_plan.audience_states[{index}]")
            for index, intent in enumerate(plan.experience_intents):
                if source_label in intent.attention_target_ids:
                    residual_paths.append(f"narrative_plan.experience_intents[{index}]")
            for index, scene in enumerate(plan.scene_contracts):
                if (
                    scene.point_of_view_character_id == source_label
                    or _identity_value_contains(scene.relationship_deltas, source_label)
                ):
                    residual_paths.append(f"narrative_plan.scene_contracts[{index}]")
        if residual_paths:
            errors.append(
                f"角色身份预解析未落实：「{source_label}」必须在剧本阶段改为「{canonical_name}」；"
                f"残留位置：{', '.join(residual_paths[:8])}"
            )
    return errors


def screenplay_unknown_identity_errors(
    screenplay,
    bible: Bible,
    resolutions: list[dict] | None = None,
) -> list[str]:
    """确定性检查“模型判断是否已经落地”，不猜测称谓语义。"""
    bible_names = {character.name for character in bible.characters}
    narrative_plan = getattr(screenplay, "narrative_plan", None)
    narrative_authority = narrative_plan is not None
    if not bible_names and not narrative_authority:
        # 保留无真实人物谱项目的历史占位流程；有 Bible 时才启用身份硬门禁。
        return []
    resolver = None
    if narrative_authority:
        from app.identity_contracts import (
            IdentityContractError,
            narrative_identity_resolver,
        )

        try:
            resolver = narrative_identity_resolver(bible, screenplay)
        except IdentityContractError as exc:
            return [f"剧本身份合同无法解析：{exc}"]
    locations: dict[str, list[str]] = {}
    typed_functional_names = {
        str(item.get("canonical_name") or "").strip()
        for item in (resolutions or [])
        if (
            isinstance(item, dict)
            and resolution_declares_functional_identity(item)
            and str(item.get("canonical_name") or "").strip()
        )
    }

    def collect(raw_name: str, path: str, *, usage: str) -> None:
        name = str(raw_name or "").strip()
        if not name:
            return
        if narrative_authority:
            try:
                resolver.resolve(name, usage=usage)
                return
            except IdentityContractError:
                pass
        elif name == "旁白" or name in bible_names:
            return
        elif name in typed_functional_names:
            return
        locations.setdefault(name, []).append(path)

    for scene_index, scene in enumerate(getattr(screenplay, "scene_outline", None) or []):
        for name in scene.characters or []:
            collect(name, f"scene_outline[{scene_index}].characters", usage="visual")
    for chain_index, chain in enumerate(getattr(screenplay, "dialogue_chains", None) or []):
        for turn_index, turn in enumerate(chain.turns or []):
            collect(
                turn.speaker,
                f"dialogue_chains[{chain_index}].turns[{turn_index}].speaker",
                usage="voice",
            )
    for index, item in enumerate(getattr(screenplay, "information_ledger", None) or []):
        collect(item.speaker_id, f"information_ledger[{index}].speaker_id", usage="voice")
    # 与 validate_screenplay 共用同一台本解析器，避免把“地点：”“场景：”
    # 这类台本标签误当成人名。这里只检查模型决议是否落地，不猜称谓语义。
    from app.validators import screenplay_speaker_names
    for speaker in screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or ""
    ):
        collect(speaker, "full_script_text.speaker", usage="voice")
    return [
        f"剧本人物身份未解决：「{name}」既不在人物谱，"
        + (
            "也未由本集 identity_contracts + voice_bible 定义可见/声音政策；"
            if narrative_authority
            else "也未被人物预检模型映射为一次性角色；"
        )
        + f"位置：{', '.join(paths[:8])}"
        for name, paths in locations.items()
    ]


def merge_screenplay_character_resolutions(
    existing: list[dict] | None,
    incoming: list[dict] | None,
) -> list[dict]:
    """合并模型决议：后续真名证据可升级早期路人降级，不反向覆盖。"""
    merged: list[dict] = []
    for item in [*(existing or []), *(incoming or [])]:
        if not isinstance(item, dict):
            continue
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        if not source_label or not canonical_name:
            continue
        candidate = normalize_character_resolution({
            **item,
            "source_label": source_label,
            "canonical_name": canonical_name,
        })
        source_instance_key = str(
            candidate.get("source_instance_key") or ""
        ).strip()
        current_index = next((
            index
            for index, current_item in enumerate(merged)
            if (
                str(current_item.get("source_label") or "").strip()
                == source_label
                and str(
                    current_item.get("source_instance_key") or ""
                ).strip() == source_instance_key
            )
        ), None)
        current = merged[current_index] if current_index is not None else None
        if current is None:
            merged.append(candidate)
            continue
        priority = {
            "functional_extra": 0,
            "functional_identity": 1,
            "future_identity": 2,
        }
        current_priority = priority.get(
            str(current.get("resolution") or ""), 0,
        )
        candidate_priority = priority.get(
            str(candidate.get("resolution") or ""), 0,
        )
        if candidate_priority > current_priority:
            merged[current_index] = candidate
        elif (
            candidate_priority == current_priority
            and current.get("canonical_name") == canonical_name
        ):
            merged[current_index] = {**current, **candidate}
    return merged


def load_screenplay_character_resolutions(conn, episode_id: str) -> list[dict]:
    if not _has_column(conn, "episodes", "screenplay_character_resolutions"):
        return []
    row = conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if not row:
        return []
    try:
        payload = json.loads(row["screenplay_character_resolutions"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return (
        normalize_character_resolutions(payload)
        if isinstance(payload, list)
        else []
    )


def persist_screenplay_character_resolutions(
    conn,
    episode_id: str,
    resolutions: list[dict] | None,
) -> list[dict]:
    current = load_screenplay_character_resolutions(conn, episode_id)
    merged = merge_screenplay_character_resolutions(current, resolutions)
    if _has_column(conn, "episodes", "screenplay_character_resolutions"):
        conn.execute(
            "UPDATE episodes SET screenplay_character_resolutions=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), episode_id),
        )
        conn.commit()
    return merged


async def ensure_cards_for_text(
    project_id: str,
    episode_no: int,
    source_text: str,
    bible: Bible,
    *,
    draft_text: str = "",
    generate_portraits: bool = True,
    _precomputed_candidates: list[dict] | None = None,
) -> dict:
    """发现并补人物卡；同时输出供剧本使用的姓名消歧表。"""
    conn = get_conn()
    episode_row = (
        conn.execute(
            "SELECT id FROM episodes WHERE project_id=? AND episode_no=?",
            (project_id, episode_no),
        ).fetchone()
        if _has_column(conn, "episodes", "id")
        else None
    )
    existing_resolutions = (
        load_screenplay_character_resolutions(conn, episode_row["id"])
        if episode_row
        else []
    )
    future_text, future_label = _future_chapter_context(
        conn, project_id, episode_no,
    )
    candidates = (
        [dict(item) for item in _precomputed_candidates]
        if _precomputed_candidates is not None
        else await discover_character_candidates(
            source_text, bible, episode_no, draft_text=draft_text,
            future_text=future_text, future_label=future_label,
            existing_resolutions=existing_resolutions,
            scope_id=str(episode_row["id"]) if episode_row else None,
        )
    )
    known = {c.name for c in bible.characters}
    unknown_by_name: dict[str, list[dict]] = {}
    functional_candidates: list[dict] = []
    known_named_candidates: list[dict] = []
    mentioned_only_candidates: list[dict] = []
    for item in candidates:
        if item.get("identity_kind") == "functional":
            functional_candidates.append(item)
            continue
        name = str(item.get("name") or "").strip()
        if name in known:
            known_named_candidates.append(item)
        elif _candidate_requires_identity_card(item, known):
            unknown_by_name.setdefault(name, []).append(item)
        elif name:
            mentioned_only_candidates.append(item)
    added: list[dict] = []
    provisional_characters: list[dict] = []
    skipped: list[dict] = [
        {
            "status": "mentioned_only",
            "name": str(item.get("name") or "").strip(),
            "reason": "本集仅提及且未出镜/开口，不创建人物卡",
        }
        for item in mentioned_only_candidates
    ]
    errors: list[str] = []
    warnings: list[str] = []
    resolutions: list[dict] = []
    assigned_extra_names: dict[str, str] = {}
    assigned_identity_groups: dict[str, str] = {}

    for item in known_named_candidates:
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("name") or "").strip()
        if source_label and canonical_name and source_label != canonical_name:
            resolutions.append(_identity_resolution(
                item,
                canonical_name,
                "future_identity",
                reason="后续章节已确认该称谓属于人物谱已有角色",
            ))

    # 功能身份保留原文稳定称谓。是否需要人物卡与是否具备真名是两件事，
    # 不得通过改成“路人甲/乙/丙”来降低角色重要性或抹掉来源身份。
    for item in functional_candidates:
        source_label = str(item.get("source_label") or item.get("name") or "").strip()
        identity_group = str(
            item.get("identity_group") or f"source:{source_label}"
        ).strip()
        route_name = str(item.get("existing_route_name") or "").strip()
        if not route_name:
            route_name = assigned_identity_groups.get(identity_group, "")
        if not route_name:
            route_name = source_label
        assigned_identity_groups[identity_group] = route_name
        assigned_extra_names[source_label] = route_name
        resolutions.append(_identity_resolution(
            item,
            route_name,
            "functional_identity",
            reason="模型依据当前来源确认该实体为本集功能身份",
        ))

    for name, items in unknown_by_name.items():
        result = await ensure_character_card(
            project_id,
            name,
            episode_no,
            generate_portrait=generate_portraits,
            require_identity_card=True,
        )
        if result.get("status") == "added":
            added.append(result)
            if not result.get("has_portrait"):
                warnings.append(
                    f"{name}：人物卡已添加，定妆资产将在独立资产环节补齐"
                    if result.get("portrait_deferred")
                    else f"{name}：人物卡已添加，定妆照生成失败，需稍后重试"
                )
        elif result.get("status") == "pending_review":
            # 兼容旧实现返回值；新流程不应再产生用户待审项。
            errors.append(f"{name}：自动建卡流程未完成")
        elif result.get("status") in {"skipped_minor", "exists"}:
            skipped.append(result)
            if result.get("status") == "skipped_minor":
                # identity_kind=named 已由身份模型给出可靠同一性证据。
                # 不能再用“戏份不足”把真名降回路人；卡片不完整就留在剧本闸门修复。
                errors.append(
                    f"{name}：真名已确认，但人物卡未完成："
                    f"{result.get('reason') or 'unknown reason'}"
                )
        else:
            errors.append(f"{name}：{result.get('reason') or result.get('status') or '补卡失败'}")

        if result.get("status") in {"added", "exists"}:
            for item in items:
                source_label = str(item.get("source_label") or name).strip()
                if source_label != name:
                    resolutions.append(_identity_resolution(
                        item,
                        name,
                        "future_identity",
                        reason="后续章节已确认该称谓的稳定真名",
                    ))
    return {
        "checked": len(unknown_by_name),
        "candidates": candidates,
        "added": added,
        "provisional_characters": provisional_characters,
        "skipped": skipped,
        "resolutions": resolutions,
        "future_context_label": future_label,
        "errors": errors,
        "warnings": warnings,
    }


async def ensure_structural_identity_coverage(
    project_id: str,
    episode_id: str,
    episode_no: int,
    source_text: str,
    bible: Bible,
    structural_evidence: list[dict],
) -> dict:
    """Materialize only identity gaps evidenced by a validated Blueprint/IR.

    This is the replacement for the old unconditional third full-chapter scan:
    current/future candidates are reused from the normalized discovery Artifact,
    and the model sees only unresolved typed references plus their owned SRC.
    """
    conn = get_conn()
    structural_hash = evidence_repository.content_hash(structural_evidence)
    rows = conn.execute(
        """SELECT id,content_json FROM artifacts
             WHERE scope_type='episode' AND scope_id=?
               AND type='screenplay_identity_discovery' AND status='validated'
             ORDER BY created_at DESC LIMIT 20""",
        (episode_id,),
    ).fetchall()
    base_candidates: list[dict] = []
    parent_artifact_id = ""
    for row in rows:
        try:
            payload = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            payload.get("mode") == "structural_coverage"
            and payload.get("structural_evidence_hash") == structural_hash
            and isinstance(payload.get("candidates"), list)
        ):
            return {
                "checked": 0,
                "candidates": payload["candidates"],
                "added": [],
                "resolutions": load_screenplay_character_resolutions(
                    conn, episode_id
                ),
                "errors": [],
                "warnings": [],
                "reused": True,
            }
        if isinstance(payload.get("candidates"), list):
            base_candidates = [
                dict(item) for item in payload["candidates"]
                if isinstance(item, dict)
            ]
            parent_artifact_id = str(row["id"])
            break
    audited = await audit_identity_coverage_from_structural_evidence(
        base_candidates,
        structural_evidence=structural_evidence,
        source_text=source_text,
        bible=bible,
        episode_no=episode_no,
    )
    base_keys = {
        (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        for item in base_candidates
    }
    additions = [
        item for item in audited
        if (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        ) not in base_keys
    ]
    if not additions:
        trace = None
        try:
            from app.observability.tracing import current_trace

            trace = current_trace()
        except Exception:  # noqa: BLE001
            pass
        raw_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_identity_discovery_raw",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T0",
                content={
                    "mode": "structural_coverage",
                    "structural_evidence_hash": structural_hash,
                    "model_candidates": [],
                },
                parent_artifact_ids=[parent_artifact_id] if parent_artifact_id else [],
                contract_version="screenplay-identity-discovery.v2",
            ),
            step_run_id=getattr(trace, "step_run_id", None),
        )
        evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_identity_discovery",
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T1",
                content={
                    "contract_version": "screenplay-identity-discovery.v2",
                    "episode_no": episode_no,
                    "mode": "structural_coverage",
                    "candidates": audited,
                    "source_hash": evidence_repository.content_hash(source_text),
                    "structural_evidence_hash": structural_hash,
                },
                parent_artifact_ids=[raw_artifact["id"]],
                contract_version="screenplay-identity-discovery.v2",
            ),
            step_run_id=getattr(trace, "step_run_id", None),
        )
        return {
            "checked": 0,
            "candidates": audited,
            "added": [],
            "resolutions": [],
            "errors": [],
            "warnings": [],
        }
    result = await ensure_cards_for_text(
        project_id,
        episode_no,
        source_text,
        bible,
        generate_portraits=False,
        _precomputed_candidates=additions,
    )
    persisted = persist_screenplay_character_resolutions(
        conn,
        episode_id,
        result.get("resolutions") or [],
    )
    result["resolutions"] = persisted
    trace = None
    try:
        from app.observability.tracing import current_trace

        trace = current_trace()
    except Exception:  # noqa: BLE001
        pass
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery_raw",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T0",
            content={
                "mode": "structural_coverage",
                "structural_evidence_hash": structural_hash,
                "model_candidates": additions,
            },
            parent_artifact_ids=[parent_artifact_id] if parent_artifact_id else [],
            contract_version="screenplay-identity-discovery.v2",
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content={
                "contract_version": "screenplay-identity-discovery.v2",
                "episode_no": episode_no,
                "mode": "structural_coverage",
                "candidates": audited,
                "source_hash": evidence_repository.content_hash(source_text),
                "structural_evidence_hash": structural_hash,
            },
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version="screenplay-identity-discovery.v2",
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    return result


def _candidate_requires_identity_card(item: dict, known_names: set[str]) -> bool:
    """Only a new named identity that appears or speaks needs a visual card."""
    name = str(item.get("name") or "").strip()
    return bool(
        name
        and name not in known_names
        and str(item.get("identity_kind") or "named") == "named"
        and item.get("kind") != "mentioned"
    )


async def assess_new_character(name: str, fragments: str, *, style: str,
                               known_names: list[str], ep_label: str,
                               require_identity_card: bool = False) -> dict:
    """针对一个【具体名字】判断是否值得单独建卡（戏份够 / 画面多），并产出角色卡字段。
    返回 {important, reason, role, appearance_canonical, personality, speech_style, relationships}。"""
    known = "、".join(known_names) or "（无）"
    identity_contract = (
        "身份消歧模型已用上下文确认这是稳定真名；本次任务不是重新判断戏份重要度，"
        "而是生成完整的最小人物卡。无论戏份多少都输出 important=true；"
        "原文未给出的可视字段按项目画风作保守补全。"
        if require_identity_card else
        "请判断该称谓是否值得单独建人物卡并定妆。"
    )
    decision_contract = (
        f"- identity_card_required=true：固定输出 important=true，并完成 20~80 字"
        f" appearance_canonical；不得因只出现一次而拒绝建卡。"
        if require_identity_card else
        f"- important=true 仅当：「{name}」是【真正的新角色】，且在这段剧情里"
        "【反复出场 / 有正面戏份 / 画面感强】，值得稳定其外观。\n"
        "- important=false：路人、只被提及一两次、纯功能性提及，"
        "或其实是已有角色的别名/外号/尊称。"
    )
    prompt = f"""任务：判断小说角色「{name}」是否值得【单独建人物卡并定妆】（用作漫剧出镜的一致性锚点）。

身份合同：{identity_contract}

已有角色（若「{name}」其实是这些人的别名/外号/尊称，则 important=false）：
{known}

下面是原文中提及「{name}」的片段（{ep_label}）：
{fragments[:12000]}

判定口径：
{decision_contract}
- appearance_canonical 是"固定外观锚点串"：40~60 字，须含 性别年龄感/发型发色/服装款式与颜色/1 个标志性特征；只写视觉可见信息，不写性格。原著未写处按画风（{style}）合理补全并保持内部一致。
- appearance_canonical 只允许常规完整着装、中性站姿下可直接看见、可跨镜稳定复现的静态形态；不得写性格、欲望、气质、眼神行为、对他人的注视方式、裸体、内衣、私密身体部位或必须暴露身体才能看见的特征。

只输出一个 JSON 对象：
{{"important": true/false, "reason": "一句话依据", "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}]}}"""
    raw = await model_gateway.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=CHARACTER_CARD_MAX_TOKENS,
        call_meta={
            "stage": "assess_new_character",
            "character_name": name,
            "expected_json": True,
        },
    )
    try:
        obj = extract_json(raw)
    except ValueError as exc:
        raise ContentGenerationError(
            f"新角色「{name}」人物卡结构化输出不完整"
        ) from exc
    important = bool(obj.get("important"))
    appearance = production_appearance_anchor(
        (obj.get("appearance_canonical") or "").strip()
    )
    if len(appearance) > APPEARANCE_MAX:
        appearance = appearance[:APPEARANCE_MAX]
    role = (obj.get("role") or "重要配角").strip() or "重要配角"
    card_complete = (
        APPEARANCE_MIN <= len(appearance) <= APPEARANCE_MAX
        and not missing_production_appearance_dimensions(appearance)
        and bool(role)
    )
    if important and not card_complete:
        important = False  # 外观太稀薄不足以稳定定妆 → 不建卡
    known_set = set(known_names)
    # 只保留指向【已知角色】且 relation 非空的关系；Relationship.to/relation 必填，漏 relation 会让校验崩。
    rels = [
        {"to": r["to"], "relation": str(r.get("relation") or "").strip()}
        for r in (obj.get("relationships") or [])
        if isinstance(r, dict) and r.get("to") in known_set and str(r.get("relation") or "").strip()
    ]
    return {
        "important": important,
        "card_complete": card_complete,
        "reason": (obj.get("reason") or "").strip(),
        "role": role,
        "appearance_canonical": appearance,
        "personality": (obj.get("personality") or "").strip(),
        "speech_style": (obj.get("speech_style") or "").strip(),
        "relationships": rels,
    }


async def ensure_character_card(
    project_id: str,
    name: str,
    from_episode_no: int,
    *,
    generate_portrait: bool = True,
    require_identity_card: bool = False,
) -> dict:
    """检查新角色的原文份量，并自动完成建卡与定妆包。

    默认由 AI 判断是否需要跨镜头保持一致；若上游身份模型已确认稳定真名，
    ``require_identity_card`` 会要求模型完成最小人物卡，不能再以戏份少降为路人。
    一次性功能角色仍跳过。建卡先落库，定妆包生成失败时保留卡片并由分镜前
    的自愈步骤重试，不再暴露人工待审队列。带 (project,name) 锁，可幂等并发。
    """
    name = (name or "").strip()
    if not name:
        return {"status": "skipped", "reason": "empty"}
    conn = get_conn()
    if _name_in_bible(conn, project_id, name):
        return {"status": "exists", "name": name}
    lock = await _card_lock(project_id, name)
    async with lock:
        if _name_in_bible(conn, project_id, name):  # 拿到锁后复查（并发兜底）
            return {"status": "exists", "name": name}
        if not _has_column(conn, "projects", "bible_auto_changes_json"):
            conn.execute("ALTER TABLE projects ADD COLUMN bible_auto_changes_json TEXT")
        pending_row = conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        try:
            change_items = json.loads(pending_row["bible_auto_changes_json"] or "[]") if pending_row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            change_items = []
        existing_change = next((
            item for item in change_items
            if item.get("kind") in {"new_character", "character_discovery", "new_bible_character"}
            and item.get("character") == name
            and item.get("status") in {"pending", "processing", "auto_applied_asset_failed"}
        ), None)
        # 负缓存：近 DISCOVERY_REJUDGE_WINDOW 集内判过"戏份不足"就先不重判；隔得够远会重新评估
        # （龙套后期可能转重要）。
        skip_raw = get_setting(_discovery_skip_key(project_id, name))
        if skip_raw and existing_change is None and not require_identity_card:
            try:
                last = int(skip_raw)
            except (TypeError, ValueError):
                last = 0
            if 0 < from_episode_no - last < DISCOVERY_REJUDGE_WINDOW:
                return {"status": "skipped_minor", "name": name, "reason": "recently judged minor"}
        bible_artifact_supported = _has_column(conn, "projects", "bible_artifact_id")
        select_cols = "bible_json, bible_version"
        if bible_artifact_supported:
            select_cols += ", bible_artifact_id"
        project = conn.execute(
            f"SELECT {select_cols} FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        if not project or not project["bible_json"]:
            return {"status": "skipped", "name": name, "reason": "no bible"}
        bible = Bible.model_validate(json.loads(project["bible_json"]))
        style = bible.world.visual_style_canonical
        known = [c.name for c in bible.characters]
        fragments, ep_label = _forward_fragments(conn, project_id, name, from_episode_no)
        if existing_change is not None:
            change_payload = (
                existing_change.get("payload")
                if isinstance(existing_change.get("payload"), dict) else {}
            )
            try:
                char_obj = Character.model_validate(change_payload.get("character_card"))
            except ValidationError as exc:
                return {"status": "error", "name": name, "reason": f"pending card invalid {exc}"[:240]}
            verdict = {
                "reason": existing_change.get("reason") or "AI 已判定为需要跨镜头保持的新角色",
            }
        else:
            if not fragments:
                # 原文里根本检索不到这个名字（多半是剧本臆造/称谓）。
                if require_identity_card:
                    return {
                        "status": "error", "name": name,
                        "reason": "真名已确认，但人物卡缺少可核验的原文片段",
                    }
                set_setting(_discovery_skip_key(project_id, name), str(from_episode_no))
                return {"status": "skipped_minor", "name": name, "reason": "no fragments in novel"}
            try:
                assessment_options = {
                    "style": style,
                    "known_names": known,
                    "ep_label": ep_label,
                }
                if require_identity_card:
                    assessment_options["require_identity_card"] = True
                verdict = await assess_new_character(
                    name, fragments, **assessment_options,
                )
            except Exception as exc:  # noqa: BLE001
                return {"status": "error", "name": name,
                        "reason": "新角色评估失败" + code_ref(exc, action="assess_new_character",
                                                              context={"project_id": project_id, "name": name})}
            card_complete = bool(verdict.get("card_complete")) or (
                bool(str(verdict.get("role") or "").strip())
                and APPEARANCE_MIN
                <= len(str(verdict.get("appearance_canonical") or "").strip())
                <= APPEARANCE_MAX
                and not missing_production_appearance_dimensions(
                    str(verdict.get("appearance_canonical") or "").strip()
                )
            )
            if not verdict["important"] and not (
                require_identity_card and card_complete
            ):
                if require_identity_card:
                    return {
                        "status": "error", "name": name,
                        "reason": "身份模型已确认真名，但人物卡模型未返回完整稳定卡片",
                    }
                set_setting(_discovery_skip_key(project_id, name), str(from_episode_no))
                return {"status": "skipped_minor", "name": name, "reason": verdict["reason"]}
            try:
                char_obj = Character.model_validate({
                    "name": name, "role": verdict["role"],
                    "appearance_canonical": verdict["appearance_canonical"],
                    "personality": verdict["personality"], "speech_style": verdict["speech_style"],
                    "relationships": verdict["relationships"], "portrait_prompt_override": None})
            except ValidationError as exc:
                return {"status": "error", "name": name, "reason": f"card invalid {exc}"[:240]}

        # 保留内部追溯记录，但不再把它当成用户待审任务。
        existing = existing_change
        if existing is None:
            evidence_fragments = [
                part.strip() for part in fragments.split("\n……\n") if part.strip()
            ][:6]
            existing = {
                "id": new_id("change"),
                "kind": "new_character",
                "status": "processing",
                "character": name,
                "ep_start": from_episode_no,
                "reason": verdict["reason"],
                "created_at": now(),
                "payload": {
                    "character_card": char_obj.model_dump(mode="json"),
                    "source_episode": from_episode_no,
                    "source_episode_label": ep_label,
                    "evidence_fragments": evidence_fragments,
                },
            }
            change_items.append(existing)
        else:
            existing["status"] = "processing"
        conn.execute(
            "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
            (json.dumps(change_items, ensure_ascii=False), project_id),
        )
        conn.commit()

        card = char_obj.model_dump(mode="json")
        bible_lock = await _bible_lock(project_id)
        async with bible_lock:
            appended = _append_character_to_bible(conn, project_id, card)
        if not appended and not _name_in_bible(conn, project_id, name):
            existing["status"] = "auto_apply_failed"
            existing["decision_reason"] = "人物卡写入失败"
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(change_items, ensure_ascii=False), project_id),
            )
            conn.commit()
            return {"status": "error", "name": name, "reason": "character card commit failed"}
        set_setting(_discovery_skip_key(project_id, name), "")

        if not generate_portrait:
            existing["status"] = "auto_applied_asset_pending"
            existing["decided_at"] = now()
            existing["decision_reason"] = "人物卡已加入；定妆包等待独立资产环节确认"
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(change_items, ensure_ascii=False), project_id),
            )
            conn.commit()
            return {
                "status": "added",
                "name": name,
                "change_id": existing["id"],
                "has_portrait": False,
                "portrait_deferred": True,
                "reason": verdict["reason"],
                "character_card": card,
            }

        latest = conn.execute(
            "SELECT bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        try:
            portrait = await _generate_discovered_character_portrait(
                project_id,
                name,
                style,
                char_obj.appearance_canonical,
                ep_start=from_episode_no,
                bible_version=int(latest["bible_version"] or 0) if latest else 0,
            )
        except Exception as exc:  # noqa: BLE001 -- 卡片仍可约束剧本，分镜前自动重试资产
            public = code_ref(
                exc,
                action="auto_generate_discovered_character_portrait",
                context={"project_id": project_id, "name": name, "episode_no": from_episode_no},
            )
            existing["status"] = "auto_applied_asset_failed"
            existing["decided_at"] = now()
            existing["decision_reason"] = public
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(change_items, ensure_ascii=False), project_id),
            )
            conn.commit()
            return {
                "status": "added", "name": name, "change_id": existing["id"],
                "has_portrait": False, "reason": verdict["reason"],
                "portrait_error": public, "character_card": card,
            }

        existing["status"] = "auto_applied"
        existing["decided_at"] = now()
        existing["decision_reason"] = "AI 判定需要人物卡并已自动生成定妆包"
        existing.setdefault("payload", {})["portrait_id"] = portrait.get("portrait_id")
        conn.execute(
            "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
            (json.dumps(change_items, ensure_ascii=False), project_id),
        )
        conn.commit()
        return {
            "status": "added", "name": name, "change_id": existing["id"],
            "has_portrait": True, "reason": verdict["reason"],
            "character_card": card, **portrait,
        }


def bible_with_provisional_characters(bible: Bible, discovery: dict | None) -> Bible:
    """兼容旧运行记录：把历史临时人物注入当前剧本生成上下文。

    新流程会在发现阶段直接自动入卡；此函数只用于断点续跑的向后兼容。
    """
    cards = (discovery or {}).get("provisional_characters") or []
    if not cards:
        return bible
    characters = list(bible.characters)
    known = {character.name for character in characters}
    for card in cards:
        if not isinstance(card, dict):
            continue
        try:
            character = Character.model_validate(card)
        except ValidationError:
            continue
        if character.name in known:
            continue
        characters.append(character)
        known.add(character.name)
    return bible.model_copy(update={"characters": characters})


def bible_with_pending_characters_for_text(
    project_id: str,
    bible: Bible,
    text: str,
) -> Bible:
    """恢复/续跑时从历史队列恢复本章实际出现的临时人物约束。

    这是只读的旧数据兼容路径，不触发出图。
    """
    if not (text or "").strip():
        return bible
    conn = get_conn()
    if not _has_column(conn, "projects", "bible_auto_changes_json"):
        return bible
    row = conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    try:
        items = json.loads(row["bible_auto_changes_json"] or "[]") if row else []
    except (TypeError, ValueError, json.JSONDecodeError):
        items = []
    cards: list[dict] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("status") != "pending"
            or item.get("kind") not in {"new_character", "character_discovery", "new_bible_character"}
        ):
            continue
        name = str(item.get("character") or "").strip()
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        card = payload.get("character_card")
        if name and name in text and isinstance(card, dict):
            cards.append(card)
    return bible_with_provisional_characters(
        bible, {"provisional_characters": cards},
    )


def _episode_source_text(conn, project_id: str, episode_no: int) -> str:
    """本集对应源章节的正文（按集做漂移判定的依据）。"""
    ep = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, episode_no)).fetchone()
    src = json.loads(ep["source_chapters"] or "[]") if ep and ep["source_chapters"] else []
    if not src:
        return ""
    has_title = _has_column(conn, "chapters", "title")
    select_cols = "idx, content" + (", title" if has_title else "")
    rows = conn.execute(
        f"SELECT {select_cols} FROM chapters WHERE project_id=? AND idx>=? AND idx<=? ORDER BY idx",
        (project_id, min(src), max(src))).fetchall()
    if has_title and len(rows) == 1 and chapter_is_stub(dict(rows[0])):
        following = conn.execute(
            "SELECT idx, title, content FROM chapters WHERE project_id=? AND idx>? ORDER BY idx LIMIT 1",
            (project_id, rows[0]["idx"]),
        ).fetchone()
        if following and not chapter_is_stub(dict(following)) and chapter_titles_match(dict(rows[0]), dict(following)):
            rows = [following]
    return "\n".join((r["content"] or "") for r in rows)


def _update_bible_appearance(conn, project_id: str, name: str, appearance: str, ref_image_path: str) -> None:
    """漂移重绘后把 bible 里该角色的外观锚点/参考图同步成最新版（供人物谱 UI 展示）。
    真正驱动按集渲染的是 character_portraits 分段表 + bible_for_episode 的本集视图，所以这里只是展示用。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return
    data = json.loads(row["bible_json"])
    for c in data.get("characters", []):
        if c.get("name") == name:
            c["appearance_canonical"] = appearance
            c["ref_image_path"] = ref_image_path
            break
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?", (json.dumps(data, ensure_ascii=False), project_id))


async def _refresh_portrait_on_drift(project_id: str, name: str, episode_no: int,
                                     new_appearance: str, style: str, bible_version: int,
                                     *, change_meta: dict | None = None) -> dict | None:
    """外观明显变化：先在临时状态生成完整多视角包，整包 QA 通过后同一事务关闭旧区间并启用新区间。
    返回 {ep_start, image_path, pack_status} 或 None。"""
    lock = await _card_lock(project_id, name)
    async with lock:
        conn = get_conn()
        cur = _open_portrait(conn, project_id, name)
        if not cur or cur["ep_start"] >= episode_no:
            return None  # 并发已处理，或本集（之后）才登场的图，无需切分
        new_path, new_prompt = await _redraw_portrait(
            project_id, name, style, new_appearance, base_path=cur["image_path"], ep_start=episode_no)
        artifact_supported = _has_column(conn, "character_portraits", "artifact_id")
        pack_supported = _has_column(conn, "character_portraits", "pack_status")
        artifact = None
        qa = None
        if artifact_supported:
            project = conn.execute(
                "SELECT bible_artifact_id FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            parent_ids = [
                artifact_id for artifact_id in (
                    cur["artifact_id"], project["bible_artifact_id"] if project else None,
                ) if artifact_id
            ]
            for attempt in range(1, 3):
                qa = await _review_portrait_asset(new_path, new_appearance)
                artifact = record_reference_asset(
                    asset_type="character_portrait",
                    scope_id=f"{project_id}:{name}:{episode_no}",
                    file_path=new_path,
                    content={"character_name": name, "appearance": new_appearance,
                             "prompt": new_prompt, "episode_start": episode_no,
                             "attempt": attempt, "change": change_meta or {}},
                    parent_artifact_ids=parent_ids,
                    qa=qa,
                )
                if artifact["status"] == "approved":
                    break
                if attempt < 2:
                    new_path, new_prompt = await _redraw_portrait(
                        project_id, name, style, new_appearance,
                        base_path=cur["image_path"], ep_start=episode_no,
                    )
            if not artifact or artifact["status"] not in {"approved", "validated"}:
                # 新主图确实不可读时继续使用旧造型；不把内容 QA 变成终态。
                return {
                    "ep_start": int(cur["ep_start"] or 1),
                    "image_path": cur["image_path"],
                    "pack_status": cur["pack_status"] if pack_supported else "ready",
                    "portrait_id": cur["id"],
                    "gate_retry_exhausted": True,
                }

        stale_segment = conn.execute(
            "SELECT id,ep_end FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start=?",
            (project_id, name, episode_no),
        ).fetchone()
        if stale_segment and stale_segment["id"] != cur["id"]:
            stale_end = stale_segment["ep_end"]
            if stale_end is None or int(stale_end) >= episode_no:
                return None
            conn.execute(
                "DELETE FROM character_portraits WHERE id=?",
                (stale_segment["id"],),
            )

        new_portrait_id = new_id("portrait")
        change_json = json.dumps(change_meta or {}, ensure_ascii=False) if change_meta else None
        # 先插入临时段（不关闭旧区间）；整包通过后再原子切换
        if artifact_supported and pack_supported:
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, artifact_id, pack_status, change_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_portrait_id, project_id, name, episode_no, episode_no,  # 临时：仅占本集，未生效
                 new_appearance, new_prompt, new_path, cur["id"], bible_version,
                 artifact["id"] if artifact else None, "generating", change_json, now()))
        elif artifact_supported:
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, artifact_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_portrait_id, project_id, name, episode_no, None, new_appearance,
                 new_prompt, new_path, cur["id"], bible_version, artifact["id"] if artifact else None, now()))
        else:
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (new_portrait_id, project_id, name, episode_no, None, new_appearance,
                 new_prompt, new_path, cur["id"], bible_version, now()))
        conn.commit()

        pack_status = "ready"
        if pack_supported:
            from app.multiview import (
                PACK_STATUS_FAILED,
                ensure_character_multiview_pack,
                pack_result_ok,
            )
            try:
                pack = await ensure_character_multiview_pack(
                    project_id=project_id,
                    portrait_id=new_portrait_id,
                    character_name=name,
                    appearance=new_appearance,
                    visual_style=style,
                    ep_start=episode_no,
                    base_portrait_id=cur["id"],
                    primary_qa=qa,
                )
            except Exception:
                conn.execute(
                    "UPDATE character_portraits SET ep_end=?,pack_status=? WHERE id=?",
                    (episode_no - 1, PACK_STATUS_FAILED, new_portrait_id),
                )
                conn.commit()
                raise
            if not pack_result_ok(pack):
                conn.execute(
                    "UPDATE character_portraits SET ep_end=?,pack_status=? WHERE id=?",
                    (episode_no - 1, PACK_STATUS_FAILED, new_portrait_id),
                )
                conn.commit()
                raise ContentGenerationError(f"角色多视角包结构不完整：{name}")
            pack_status = "ready"
            # 原子切换：关闭旧区间，开放新区间
            conn.execute("UPDATE character_portraits SET ep_end=? WHERE id=?", (episode_no - 1, cur["id"]))
            persistence = (change_meta or {}).get("persistence") or "persistent"
            new_ep_end = episode_no if persistence == "episode" else None
            conn.execute(
                "UPDATE character_portraits SET ep_end=?, pack_status=? WHERE id=?",
                (new_ep_end, pack_status, new_portrait_id),
            )
            # 若仅本集有效，结束后零付费重新绑定完整旧包（含全部视角，pack_status=ready）
            if persistence == "episode":
                from app.multiview import bind_ready_portrait_reuse
                bind_ready_portrait_reuse(
                    conn,
                    project_id=project_id,
                    character_name=name,
                    source_portrait_id=cur["id"],
                    ep_start=episode_no + 1,
                    bible_version=bible_version,
                )
            conn.commit()
        else:
            conn.execute("UPDATE character_portraits SET ep_end=? WHERE id=?", (episode_no - 1, cur["id"]))
            conn.commit()

        _update_bible_appearance(conn, project_id, name, new_appearance, new_path)
        conn.commit()
        return {"ep_start": episode_no, "image_path": new_path, "pack_status": pack_status,
                "portrait_id": new_portrait_id}


def _backfill_matching_future_portrait(
    conn,
    *,
    project_id: str,
    name: str,
    episode_no: int,
    appearance: str,
) -> dict | None:
    """Extend an identical ready pack when discovery assigned a future start."""
    covered = conn.execute(
        "SELECT id FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start<=? "
        "AND (ep_end IS NULL OR ep_end>=?) LIMIT 1",
        (project_id, name, episode_no, episode_no),
    ).fetchone()
    if covered:
        return None
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    pack_clause = "AND pack_status='ready'" if pack_supported else ""
    future = conn.execute(
        "SELECT * FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start>? "
        f"{pack_clause} ORDER BY ep_start ASC LIMIT 1",
        (project_id, name, episode_no),
    ).fetchone()
    if not future:
        return None
    if (future["appearance"] or "").strip() != (appearance or "").strip():
        return None
    image_path = str(future["image_path"] or "")
    if not image_path or not Path(image_path).is_file():
        return None
    same_start = conn.execute(
        "SELECT id FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start=? AND id<>? LIMIT 1",
        (project_id, name, episode_no, future["id"]),
    ).fetchone()
    if same_start:
        return None
    original_start = int(future["ep_start"])
    conn.execute(
        "UPDATE character_portraits SET ep_start=? WHERE id=? AND ep_start=?",
        (episode_no, future["id"], original_start),
    )
    conn.commit()
    return {
        "name": name,
        "portrait_id": future["id"],
        "ep_start": episode_no,
        "previous_ep_start": original_start,
        "image_path": image_path,
        "pack_status": future["pack_status"] if pack_supported else "ready",
        "reused": True,
    }


async def ensure_cards_for_screenplay(project_id: str, episode_no: int, screenplay, bible) -> dict:
    """剧本就绪后（分镜展开前）反应式维护本集出场角色的定妆照：
      ① 剧本外身份在这里只做快速阻断，不再延迟到分镜阶段建卡；
      ② 已有角色漂移：剧本里出现、本集之前已有定妆照的角色 → 用本集源文判断外观是否相比当前锚点
         明显变化，变了就图生图重绘新段并把 bible 锚点同步成最新。
    逐项吞错——单角色失败不阻断分镜。返回 {checked, added:[...], redrawn:[...], errors:[...]}。"""
    bible_names = {c.name for c in bible.characters}
    names: list[str] = []
    seen: set[str] = set()

    def _collect(lst) -> None:
        for n in lst or []:
            n = (n or "").strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)

    for sc in getattr(screenplay, "scene_outline", None) or []:
        _collect(getattr(sc, "characters", None))

    errors: list[str] = []

    # ① Narrative 路径只消费 typed resolver；legacy 仍保留旧分类器。
    narrative_authority = getattr(screenplay, "narrative_plan", None) is not None
    identity_by_token: dict[str, object] = {}
    resolver_error = ""
    if narrative_authority:
        from app.identity_contracts import (
            IdentityContractError,
            narrative_identity_resolver,
        )

        try:
            identity_resolver = narrative_identity_resolver(bible, screenplay)
            for name in names:
                identity_by_token[name] = identity_resolver.resolve(name, usage="visual")
        except IdentityContractError as exc:
            resolver_error = str(exc)
    unknown = (
        ([resolver_error] if resolver_error else [])
        if narrative_authority
        else [n for n in names if n not in bible_names]
    )
    added: list[dict] = []
    blocking_errors: list[str] = []
    if narrative_authority and resolver_error:
        blocking_errors.append(f"剧本 typed identity contract 未完成：{resolver_error}")
    elif not narrative_authority:
        blocking_errors.extend(
            f"剧本人物身份未完成：「{name}」未进入人物谱，也不是已编号的一次性角色；"
            "请回到剧本阶段重跑人物身份预检"
            for name in unknown
        )

    # 剧本阶段若遇到供应商短暂失败，人物卡已保留；分镜前对这些系统失败项
    # 自动补齐定妆包。这是内部自愈，不再转换为用户待审任务。
    conn = get_conn()
    # typed policy 要求资产的非 Bible 身份，直接使用合同的稳定视觉锚点
    # 建立本集定妆包。不需资产的一次性/群体/画外身份不会被名称规则误建卡。
    if narrative_authority and not resolver_error:
        project_row = conn.execute(
            "SELECT bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        bible_version = int(project_row["bible_version"] or 0) if project_row else 0
        generated_asset_ids: set[str] = set()
        for identity in identity_by_token.values():
            if not identity.requires_asset or identity.asset_name in bible_names:
                continue
            if identity.identity_id in generated_asset_ids:
                continue
            generated_asset_ids.add(identity.identity_id)
            try:
                card_lock = await _card_lock(project_id, identity.asset_name)
                async with card_lock:
                    portrait = await _generate_discovered_character_portrait(
                        project_id,
                        identity.asset_name,
                        bible.world.visual_style_canonical,
                        identity.visual_anchor(),
                        ep_start=episode_no,
                        bible_version=bible_version,
                    )
            except Exception as exc:  # noqa: BLE001 - required policy must fail closed
                public = code_ref(
                    exc,
                    action="ensure_narrative_identity_asset",
                    context={
                        "project_id": project_id,
                        "identity_id": identity.identity_id,
                        "episode_no": episode_no,
                    },
                )
                blocking_errors.append(
                    f"身份「{identity.display_name}」合同要求人物资产，但定妆包生成失败{public}"
                )
                continue
            added.append({
                "status": "added",
                "name": identity.display_name,
                "identity_id": identity.identity_id,
                "has_portrait": True,
                **portrait,
            })
    retry_changes: list[dict] = []
    if _has_column(conn, "projects", "bible_auto_changes_json"):
        change_row = conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        try:
            all_changes = json.loads(change_row["bible_auto_changes_json"] or "[]") if change_row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            all_changes = []
        retry_changes = [
            item for item in all_changes
            if item.get("kind") in {"new_character", "character_discovery", "new_bible_character"}
            and item.get("status") in {
                "auto_applied_asset_failed",
                "auto_applied_asset_pending",
            }
            and item.get("character") in names
        ]
    else:
        all_changes = []
    if retry_changes:
        refreshed_project = conn.execute(
            "SELECT bible_json,bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        refreshed_bible = Bible.model_validate(json.loads(refreshed_project["bible_json"]))
        refreshed_by_name = {character.name: character for character in refreshed_bible.characters}
        for change in retry_changes:
            retry_name = str(change.get("character") or "").strip()
            character = refreshed_by_name.get(retry_name)
            if character is None:
                continue
            try:
                retry_lock = await _card_lock(project_id, retry_name)
                async with retry_lock:
                    portrait = await _generate_discovered_character_portrait(
                        project_id,
                        retry_name,
                        refreshed_bible.world.visual_style_canonical,
                        character.appearance_canonical,
                        ep_start=max(1, int(change.get("ep_start") or episode_no)),
                        bible_version=int(refreshed_project["bible_version"] or 0),
                    )
            except Exception as exc:  # noqa: BLE001
                public = code_ref(
                    exc,
                    action="retry_auto_character_portrait",
                    context={"project_id": project_id, "name": retry_name, "episode_no": episode_no},
                )
                change["decision_reason"] = public
                blocking_errors.append(f"{retry_name}：自动定妆包生成失败，系统重试后仍未就绪")
                continue
            change["status"] = "auto_applied"
            change["decided_at"] = now()
            change["decision_reason"] = "系统已在分镜前自动补齐定妆包"
            change.setdefault("payload", {})["portrait_id"] = portrait.get("portrait_id")
            if not any(item.get("name") == retry_name for item in added):
                added.append({"status": "added", "name": retry_name, "has_portrait": True, **portrait})
        conn.execute(
            "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
            (json.dumps(all_changes, ensure_ascii=False), project_id),
        )
        conn.commit()

    # 未来章节扫描可能先发现真实姓名，但当前集剧本已经使用该角色。
    # 若完整包外观与人物谱锚点完全一致，零付费向前扩展适用区间。
    backfilled: list[dict] = []
    by_name = {c.name: c for c in bible.characters}
    for name in (item for item in names if item in bible_names):
        result = _backfill_matching_future_portrait(
            conn,
            project_id=project_id,
            name=name,
            episode_no=episode_no,
            appearance=by_name[name].appearance_canonical,
        )
        if result:
            backfilled.append(result)

    # ② 已有角色按集漂移（只判本集之前就已有定妆照的角色；本集新建的天然是最新）
    src_text = _episode_source_text(conn, project_id, episode_no)
    entries: list[dict] = []
    if src_text:
        for n in (x for x in names if x in bible_names):
            cur = _open_portrait(conn, project_id, n)
            if not cur or cur["ep_start"] >= episode_no:
                continue
            frags = extract_character_fragments(src_text, n)
            if not frags:
                continue  # 本集没正面提到 → 沿用，开区间自然覆盖
            entries.append({"name": n, "fragments": frags,
                            "current_appearance": cur["appearance"] or by_name[n].appearance_canonical})

    redrawn: list[dict] = []
    if entries:
        proj = conn.execute("SELECT bible_version FROM projects WHERE id=?", (project_id,)).fetchone()
        bible_version = (proj["bible_version"] if proj else 0) or 0
        style = bible.world.visual_style_canonical
        try:
            verdicts = await screen_appearance_changes(entries, f"第 {episode_no} 集")
        except Exception as exc:  # noqa: BLE001 判定失败不阻断分镜
            verdicts = {}
            errors.append(f"漂移判定失败@第{episode_no}集"
                          + code_ref(exc, action="screen_appearance_changes",
                                     context={"project_id": project_id, "episode_no": episode_no}))
        for name, v in verdicts.items():
            try:
                res = await _refresh_portrait_on_drift(
                    project_id, name, episode_no, v["new_appearance"], style, bible_version,
                    change_meta={
                        "change_dimensions": v.get("change_dimensions") or [],
                        "persistence": v.get("persistence") or "persistent",
                        "reason": v.get("reason") or "",
                        "evidence_excerpt": v.get("evidence_excerpt") or "",
                    },
                )
            except Exception as exc:  # noqa: BLE001 单角色重绘失败不阻断分镜
                errors.append(f"{name}@第{episode_no}集重绘失败"
                              + code_ref(exc, action="refresh_portrait_on_drift",
                                         context={"project_id": project_id, "name": name, "episode_no": episode_no}))
                continue
            if res:
                redrawn.append({"name": name, "reason": v["reason"], **res})

    return {
        "checked": len(unknown),
        "added": added,
        "backfilled": backfilled,
        "redrawn": redrawn,
        "errors": errors,
        "blocking_errors": blocking_errors,
    }


# ---------- 定妆照落盘 / 登记 ----------

async def _save_image_item(item: dict, dest: str) -> None:
    """把 hiagent.generate_image 的返回落盘到 dest（url 优先下载，其次写 b64）。"""
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


def _portrait_dir(project_id: str) -> Path:
    d = config.PROJECTS_DIR / project_id / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_portrait_path(project_id: str, name: str, ep_start: int) -> str:
    return str(
        _portrait_dir(project_id)
        / f"{_safe_name(name)}__ep{ep_start}__{new_id('candidate')}.jpg"
    )


async def _review_portrait_asset(image_path: str, appearance: str) -> dict:
    """对反应式人物锚点执行与初始定妆照相同的保守一致性门禁。"""
    from app.stages import review_portrait_image

    try:
        return await review_portrait_image(hiagent.encode_image_file(image_path), appearance)
    except Exception as exc:  # noqa: BLE001 评估失败不能伪装成通过
        return {
            "overall": 0.0,
            "issues": [f"角色一致性评估未完成：{type(exc).__name__}"],
            "qa_recovered": True,
        }


def register_initial_portrait(conn, project_id: str, name: str, image_path: str,
                              appearance: str, prompt: str, bible_version: int,
                              artifact_id: str | None = None) -> str:
    """初次定妆后登记角色首张定妆照（适用集 1~ 至今）。覆盖式：先清掉该角色全部旧分段。"""
    conn.execute("DELETE FROM character_portraits WHERE project_id=? AND character_name=?",
                 (project_id, name))
    portrait_id = new_id("portrait")
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    if pack_supported:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, pack_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, 1, None, appearance, prompt, image_path, None,
             bible_version, artifact_id, "legacy_partial", now()))
    else:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, 1, None, appearance, prompt, image_path, None,
             bible_version, artifact_id, now()))
    conn.commit()
    return portrait_id


def stage_initial_portrait(conn, project_id: str, name: str, image_path: str,
                           appearance: str, prompt: str, bible_version: int,
                           artifact_id: str | None = None) -> str:
    """暂存新的初始定妆包，不提前删除当前已采用包。

    STAGED_INITIAL_EP_START 是仅供生成/QA 使用的候选槽位，不会命中任何
    真实集号；整包验收通过后再由
    promote_staged_initial_portrait 以单个事务替换 ep_start=1 的当前包。
    """
    current = conn.execute(
        "SELECT id FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_end IS NULL AND ep_start<>? ORDER BY created_at DESC LIMIT 1",
        (project_id, name, STAGED_INITIAL_EP_START),
    ).fetchone()
    base_portrait_id = current["id"] if current else None
    conn.execute(
        "DELETE FROM character_portraits WHERE project_id=? AND character_name=? AND ep_start=?",
        (project_id, name, STAGED_INITIAL_EP_START),
    )
    portrait_id = new_id("portrait")
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    if pack_supported:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, pack_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, STAGED_INITIAL_EP_START, None, appearance, prompt, image_path, base_portrait_id,
             bible_version, artifact_id, "legacy_partial", now()),
        )
    else:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, STAGED_INITIAL_EP_START, None, appearance, prompt, image_path, base_portrait_id,
             bible_version, artifact_id, now()),
        )
    conn.commit()
    return portrait_id


def promote_staged_initial_portrait(conn, project_id: str, name: str, portrait_id: str) -> None:
    """整包验收通过后原子发布为全局初始定妆。

    手工重新定妆与剧情中的分集造型演进是两种操作：前者必须从第 1 集
    起替换全时间线，后者由 ``_refresh_portrait_on_drift`` 继续维护分段。
    """
    row = conn.execute(
        "SELECT id FROM character_portraits "
        "WHERE id=? AND project_id=? AND character_name=? AND ep_start=?",
        (portrait_id, project_id, name, STAGED_INITIAL_EP_START),
    ).fetchone()
    if not row:
        raise ValueError(f"定妆候选不存在：{name}")
    with conn:
        previous = conn.execute(
            "SELECT id FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND id<>? AND ep_start>0 "
            "ORDER BY ep_start, created_at",
            (project_id, name, portrait_id),
        ).fetchall()
        minimum = conn.execute(
            "SELECT MIN(ep_start) AS value FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=0",
            (project_id, name),
        ).fetchone()
        history_start = int(
            minimum["value"] if minimum and minimum["value"] is not None else 0
        ) - len(previous)
        for offset, previous_row in enumerate(previous):
            conn.execute(
                "UPDATE character_portraits SET ep_start=?, ep_end=0 WHERE id=?",
                (history_start + offset, previous_row["id"]),
            )
        conn.execute(
            "UPDATE character_portraits SET ep_start=1, ep_end=NULL WHERE id=?",
            (portrait_id,),
        )


def _open_portrait(conn, project_id: str, name: str):
    """该角色当前开区间（ep_end IS NULL）的最新定妆照。"""
    return conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_end IS NULL AND ep_start<>? ORDER BY ep_start DESC LIMIT 1",
        (project_id, name, STAGED_INITIAL_EP_START)).fetchone()


def portrait_for_episode(project_id: str, name: str, episode_no: int | None) -> str | None:
    """返回覆盖该集的定妆照落盘路径；未命中返回 None（调用方回退到 bible.ref_image_path）。"""
    if episode_no is None:
        return None
    try:
        row = get_conn().execute(
            "SELECT image_path FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row and row["image_path"] and Path(row["image_path"]).exists():
        return row["image_path"]
    return None


def appearance_for_episode(project_id: str, name: str, episode_no: int | None) -> str | None:
    """返回覆盖该集的定妆照有效外观锚点。

    ``appearance`` 是验收时单独持久化的结构化外观权威；不得再从
    prompt 文案中按关键词反向提取。
    """
    if episode_no is None:
        return None
    try:
        row = get_conn().execute(
            "SELECT appearance,prompt FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return production_appearance_anchor(row["appearance"] or "") or None


def bible_for_episode(project_id: str, bible: "Bible", episode_no: int | None) -> "Bible":
    """返回 bible 的【本集视图】：每个角色的 appearance_canonical / ref_image_path 用覆盖该集的分段
    定妆照覆盖（未命中保留原值）。让关键帧文字锚点与参考图同段同源——同一集永远是同一套外观描述+图。"""
    if episode_no is None:
        return bible
    view = bible.model_copy(deep=True)
    for c in view.characters:
        anchor = appearance_for_episode(project_id, c.name, episode_no)
        if anchor:
            c.appearance_canonical = anchor
        img = portrait_for_episode(project_id, c.name, episode_no)
        if img:
            c.ref_image_path = img
    return view


def portrait_views_for_episode(project_id: str, name: str, episode_no: int | None, *, ready_only: bool = False):
    """本集有效人物多视角包；供新链路使用。"""
    from app.multiview import portrait_views_for_episode as _views
    return _views(project_id, name, episode_no, ready_only=ready_only)


def redraw_prompt(style: str, appearance: str) -> str:
    """图生图重绘提示词：以参考图（旧定妆照）为身份锚点，只按新外观调整。"""
    return (
        f"{style}。参考图是同一角色的既有定妆照，请在保持【同一个人、同一角色身份】的前提下，"
        f"按新外观重绘其全身定妆照：{appearance}。"
        "正面站立，中性表情，双臂自然下垂，纯浅米色背景，全身完整可见，无文字无水印"
    )


async def _redraw_portrait(project_id: str, name: str, style: str, appearance: str,
                           *, base_path: str | None, ep_start: int) -> tuple[str, str]:
    """以上一张定妆照为底【图生图】重绘新定妆照，落盘。返回 (落盘路径, 生成 prompt)。"""
    prompt = redraw_prompt(style, appearance)
    image_inputs = None
    if base_path and Path(base_path).exists():
        image_inputs = [hiagent.data_url_from_file(base_path)]
    item = await hiagent.generate_image(
        prompt,
        size=config.REF_IMAGE_SIZE,
        image_inputs=image_inputs,
        call_meta={
            "asset_kind": "portrait",
            "character_name": name,
            "episode_no": ep_start,
            "portrait_mode": "redraw",
        })
    dest = _new_portrait_path(project_id, name, ep_start)
    await _save_image_item(item, dest)
    return dest, prompt


async def _generate_fresh_portrait(project_id: str, name: str, style: str, appearance: str,
                                   *, ep_start: int) -> tuple[str, str]:
    """为新登场角色生成一张全新定妆照（无底图，不走图生图），落盘。返回 (落盘路径, 生成 prompt)。"""
    prompt = portrait_prompt(style, appearance)
    item = await hiagent.generate_image(
        prompt,
        size=config.REF_IMAGE_SIZE,
        call_meta={
            "asset_kind": "portrait",
            "character_name": name,
            "episode_no": ep_start,
            "portrait_mode": "fresh",
        })
    dest = _new_portrait_path(project_id, name, ep_start)
    await _save_image_item(item, dest)
    return dest, prompt


def _append_character_to_bible(conn, project_id: str, char: dict) -> bool:
    """Atomically append a discovered character and advance bible lineage/version."""
    artifact_supported = (
        _has_column(conn, "projects", "bible_artifact_id")
        and _has_table(conn, "artifacts")
    )
    select_cols = "bible_json, bible_version"
    if artifact_supported:
        select_cols += ", bible_artifact_id"
    row = conn.execute(f"SELECT {select_cols} FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    data = json.loads(row["bible_json"])
    if char.get("name") in {c.get("name") for c in data.get("characters", [])}:
        return False
    data.setdefault("characters", []).append(char)
    payload = json.dumps(data, ensure_ascii=False)
    next_artifact_id = None
    if artifact_supported:
        try:
            previous_id = row["bible_artifact_id"]
            artifact = evidence_repository.create_artifact(EvidenceArtifact(
                type="character_bible",
                scope_type="project",
                scope_id=project_id,
                status="approved",
                trust_level="T2",
                content=data,
                parent_artifact_ids=[previous_id] if previous_id else [],
                contract_version="character-bible-1.0.0",
                prompt_version="incremental-character-discovery-1.0.0",
                model_snapshot={"operation": "incremental_add", "character_name": char.get("name")},
            ))
            next_artifact_id = artifact["id"]
        except Exception as exc:  # noqa: BLE001 - authority mutation must fail closed
            code_ref(
                exc,
                action="append_character_bible_artifact",
                context={"project_id": project_id, "character_name": char.get("name")},
            )
            return False
    expected_version = int(row["bible_version"] or 0)
    if artifact_supported:
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=?,bible_artifact_id=? "
            "WHERE id=? AND COALESCE(bible_version,0)=?",
            (
                payload,
                expected_version + 1,
                next_artifact_id,
                project_id,
                expected_version,
            ),
        )
    else:
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=? "
            "WHERE id=? AND COALESCE(bible_version,0)=?",
            (payload, expected_version + 1, project_id, expected_version),
        )
    conn.commit()
    return cursor.rowcount == 1


async def _generate_discovered_character_portrait(
    project_id: str,
    name: str,
    style: str,
    appearance: str,
    *,
    ep_start: int,
    bible_version: int,
) -> dict:
    """为后续剧情自动发现的角色生成并原子接入定妆包。

    Score-only（PRD QA-SO #15）：第一张技术有效主图即可接入；QA 只评分，
    不因低分重生。多视角包完整性只看必需视角文件是否齐全。
    """
    conn = get_conn()
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    candidate = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_start=? ORDER BY created_at DESC LIMIT 1",
        (project_id, name, ep_start),
    ).fetchone()

    async def _complete_candidate(
        row,
        *,
        primary_qa: dict | None = None,
        purge_on_failure: bool,
    ) -> dict:
        """补齐并发布同一个候选；重启恢复时不得再占用相同分段键。"""
        portrait_id = str(row["id"])
        image_path = str(row["image_path"] or "")
        candidate_appearance = str(row["appearance"] or appearance)
        try:
            if pack_supported:
                from app.multiview import ensure_character_multiview_pack, pack_result_ok

                existing_status = str(row["pack_status"] or "")
                if existing_status == "ready":
                    pack = {"status": "ready", "portrait_id": portrait_id, "reused": True}
                else:
                    pack = await ensure_character_multiview_pack(
                        project_id=project_id,
                        portrait_id=portrait_id,
                        character_name=name,
                        appearance=candidate_appearance,
                        visual_style=style,
                        ep_start=ep_start,
                        base_portrait_id=row["base_portrait_id"],
                        primary_qa=primary_qa,
                    )
                if not pack_result_ok(pack):
                    conn.execute(
                        "UPDATE character_portraits SET pack_status='failed' WHERE id=?",
                        (portrait_id,),
                    )
                    conn.commit()
                    raise ContentGenerationError(f"角色多视角包结构不完整：{name}")

                # 候选在多视角完成前只占本集闭区间。发布时再原子切换为开区间；
                # 服务重启后重复执行本段仍更新同一 portrait_id，不会触发唯一键冲突。
                current = _open_portrait(conn, project_id, name)
                if current and current["id"] != portrait_id:
                    if int(current["ep_start"] or 1) < ep_start:
                        conn.execute(
                            "UPDATE character_portraits SET ep_end=? WHERE id=?",
                            (ep_start - 1, current["id"]),
                        )
                    else:
                        conn.execute("DELETE FROM character_portraits WHERE id=?", (current["id"],))
                conn.execute(
                    "UPDATE character_portraits SET ep_end=NULL,pack_status=? WHERE id=?",
                    ("ready", portrait_id),
                )
                conn.commit()

            _update_bible_appearance(conn, project_id, name, candidate_appearance, image_path)
            conn.commit()
        except Exception:
            # 新候选在本调用内失败可沿用原清理语义；重启前已经付费落盘的候选必须保留，
            # 让下一次恢复继续使用，不能因为恢复代码自身异常再次烧图。
            if purge_on_failure:
                from app.rejected_media import purge_character_portrait
                purge_character_portrait(conn, portrait_id)
            raise
        return {
            "portrait_id": portrait_id,
            "image_path": image_path,
            "pack_status": "ready",
            "reused": not purge_on_failure,
            "gate_retry_exhausted": False,
        }

    # 服务重启可能发生在主图和候选行已落盘、侧视角尚未完成之间。此时该行以
    # ep_start=ep_end 占用候选槽；必须在原 portrait_id 上续补，不能重生主图后重复 INSERT。
    if candidate is not None:
        candidate_path = str(candidate["image_path"] or "")
        if candidate_path and Path(candidate_path).is_file():
            return await _complete_candidate(candidate, purge_on_failure=False)
        from app.rejected_media import purge_character_portrait
        purge_character_portrait(conn, str(candidate["id"]))

    current = _open_portrait(conn, project_id, name)
    if current and current["image_path"] and Path(current["image_path"]).is_file():
        current_pack = current["pack_status"] if pack_supported else "ready"
        if current_pack == "ready" and int(current["ep_start"] or 1) <= ep_start:
            return {
                "portrait_id": current["id"], "image_path": current["image_path"],
                "pack_status": "ready", "reused": True,
            }

    artifact_supported = (
        _has_column(conn, "character_portraits", "artifact_id")
        and _has_table(conn, "artifacts")
    )
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    parent_ids = []
    if project and "bible_artifact_id" in project.keys() and project["bible_artifact_id"]:
        parent_ids.append(project["bible_artifact_id"])

    artifact = None
    qa = None
    image_path, prompt = await _generate_fresh_portrait(
        project_id, name, style, appearance, ep_start=ep_start,
    )
    if artifact_supported:
        qa = await _review_portrait_asset(image_path, appearance)
        artifact = record_reference_asset(
            asset_type="character_portrait",
            scope_id=f"{project_id}:{name}:{ep_start}",
            file_path=image_path,
            content={
                "character_name": name,
                "appearance": appearance,
                "prompt": prompt,
                "episode_start": ep_start,
                "attempt": 1,
                "origin": "automatic_character_discovery",
            },
            parent_artifact_ids=parent_ids,
            qa=qa,
        )
        if artifact["status"] not in {"approved", "validated"}:
            if current:
                return {
                    "portrait_id": current["id"], "image_path": current["image_path"],
                    "pack_status": current["pack_status"] if pack_supported else "ready",
                    "reused": True, "gate_retry_exhausted": True,
                }
            raise hiagent.ProviderError(f"新角色定妆照文件不可用：{name}")

    portrait_id = new_id("portrait")
    values = {
        "id": portrait_id,
        "project_id": project_id,
        "character_name": name,
        "ep_start": ep_start,
        # 多视角尚未通过时只占本集候选槽，不开放右区间。
        "ep_end": ep_start if pack_supported else None,
        "appearance": appearance,
        "prompt": prompt,
        "image_path": image_path,
        "base_portrait_id": current["id"] if current else None,
        "bible_version": bible_version,
        "created_at": now(),
    }
    if _has_column(conn, "character_portraits", "artifact_id"):
        values["artifact_id"] = artifact["id"] if artifact else None
    if pack_supported:
        values["pack_status"] = "generating"
    columns = list(values)
    conn.execute(
        f"INSERT INTO character_portraits({', '.join(columns)}) "
        f"VALUES({', '.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    conn.commit()
    inserted = conn.execute(
        "SELECT * FROM character_portraits WHERE id=?", (portrait_id,),
    ).fetchone()
    return await _complete_candidate(
        inserted,
        primary_qa=qa,
        purge_on_failure=True,
    )


def _has_column(conn, table: str, column: str) -> bool:
    """Support focused tests/old snapshots before app.db runs migrations."""
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _has_table(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None
