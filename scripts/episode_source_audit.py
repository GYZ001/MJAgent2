#!/usr/bin/env python3
"""逐集对照审计：episode_prep_pack（剧本台产物）vs 小说原文。

背景（见 docs/TRANSFORM_FREEZE_PLAN.md §3、app/production/prep_pack.py 模块
docstring）：剧本台为每一集产出 episode_prep_pack（artifacts.content_json，
type='episode_prep_pack'），其中 asset_manifest.characters/scenes 把本集事件
绑定到人物谱（character_portraits）/场景库（scene_references）的既有条目。
生成器内部已有若干确定性门禁（1.4.2 的称谓证据闸、1.5.0 的说话人名册核验），
但已发布的历史产物可能是门禁上线之前生成的旧版本（真实事故：round-16 EP5
产物是 1.4.1，早于 1.4.2 的证据闸，把"靠山宗旁山峰的灰袍老者"绑成谱内「丹鬼」、
场景绑成「大青山山顶」，两个名字在第 5 章原文里出现次数均为 0；EP2 台词说话人
被写成第 5 章才出场的「韩宗」）。本工具是独立于生成器的**外部复核**，不信任
生成器自称"已校验"，只认三样数据：pack 自身字段、character_portraits/
scene_references、chapters 原文——重新核验一遍。

铁律（用户明令）：禁止任何硬编码黑白名单。所有判据都从上述三份数据推导，
对任何项目、任何集数通用。

两个方向：

方向 A · 幻觉检查（名册 → 原文，确定性判定，超出容差即报错）
  A1  asset_manifest.characters[].display_name 或 aliases 逐字出现在本集源文本？
  A2  asset_manifest.scenes[].display_name 逐字出现在本集源文本？
  A3  event_chain[].key_lines[].speaker 逐字出现在本集源文本？
  A3b （1.5.0 起）key_lines[].speaker_ref 是否指向本集资产名册（characters/
      functional_extras）里真实存在的条目？
  A4  source_evidence 引文抽查（每集抽 ``--sample-size`` 条，默认 5）：quote
      是否逐字出现在本集源文本？

  1.5.0 起，_ModelCharacterMention/_ModelSceneMention 新增了模型自报的
  ``suspected_true_name``（先验知识假设），生成器用 app.portraits 的前瞻窗口
  核验后才接受，接受结果记在 evaluations.evidence_json 的 ``true_name_hints``
  里（kind/mention/suspected_true_name/status）。若某个 A1/A2 直接文本命中
  失败，但该 pack 的 true_name_hints 里有一条 status=accepted 且
  suspected_true_name（或其 mention）匹配这个 display_name/alias，本工具会
  独立复核：suspected_true_name 是否逐字出现在**本项目任意一章**原文中（不
  止本集）——命中才放行，并在通过说明里标出命中的章节号。
  已知偏差：真实 true_name_hints 结构没有携带具体段号（segment_index），
  只有 kind/mention/suspected_true_name/status 四个字段（见
  app/production/prep_pack.py 的 _pass() 内 true_name_hints.append(...)）；
  任务描述里"验证该段确实含该称谓"的"段"，本工具用"该名字在项目哪一章原文
  中出现"代替单一段号级别的核验粒度，是在现有数据形状下能做到的最接近实现，
  在报告里如实注明，不是编造一个不存在的字段。

方向 B · 遗漏检查（原文 → 名册，数据驱动，只报"疑似"，不做武断判定）
  B1  本集适用范围内（ep_start<=集号<=ep_end 或 ep_end 为空）的每个
      character_portraits.character_name，若其 portrait_id 未出现在
      asset_manifest.characters[].portrait_id 集合里，且该名字（或已登记
      别名）在本集原文中出现 >=1 次 → 报「疑似遗漏出场角色」，附出现次数、
      首处上下文（约 40 字）、名字后是否紧跟冒号/引号的"对白痕迹"信号。
  B2  scene_references 同理扫 scene_name，与 asset_manifest.scenes[]。

  "已登记别名"的数据来源（因为 app.schemas.Character 本身没有 aliases 字段，
  只有 Scene 有）：
    - 角色：聚合本项目**全部**已发布 episode_prep_pack 里 asset_manifest.
      characters[].aliases（按 portrait_id 归并）——这是系统自己在历史生成
      中记录过的、真实解析成功过的称呼变体，不是外部猜测。
    - 场景：projects.bible_json 里 Scene.aliases（按 scene_name 匹配）。
  两者都可能是空集（项目还没有任何别名记录时），此时退化为只用主名匹配，
  不影响判定的正确性，只是召回率会低一些。

输出：每集一张差异表（A 类逐条 + B 类逐条 + 通过项计数），末尾全局汇总。
exit code：0=全清；1=存在 A 类（无论 B 类如何）；2=A 类全清但存在 B 类疑似。

用法：
    py scripts/episode_source_audit.py                       # 默认项目 EP1-10
    py scripts/episode_source_audit.py --project P --start 1 --end 20
    py scripts/episode_source_audit.py --json out.json       # 附加写结构化结果
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "manju.db"
DEFAULT_PROJECT_ID = "proj_3ac0b627fa46"
DEFAULT_START_EPISODE = 1
DEFAULT_END_EPISODE = 10
DEFAULT_EVIDENCE_SAMPLE = 5
CONTEXT_WINDOW_CHARS = 40
# B 方向噪音抑制：单字名字符串匹配假阳性率极高（常见字/偏旁重合），与
# app.production.prep_pack._prep_pack_prose_lint_warnings 的同类 lint 用同一
# 长度门槛（>=2），不是新引入的判据。
MIN_SCAN_NAME_CHARS = 2


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Issue:
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckTally:
    checked: int = 0
    passed: int = 0


@dataclass
class EpisodeAuditResult:
    episode_no: int
    episode_id: str | None = None
    artifact_id: str | None = None
    prep_pack_version: str | None = None
    chapter_indexes: list[int] = field(default_factory=list)
    skipped_reason: str | None = None
    a_issues: list[Issue] = field(default_factory=list)
    b_issues: list[Issue] = field(default_factory=list)
    tallies: dict[str, CheckTally] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 只读 DB 访问
# ---------------------------------------------------------------------------

def readonly_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _build_source_text(conn: sqlite3.Connection, project_id: str, chapter_indexes: list[int]) -> str:
    """镜像 app/domain/common.py::_episode_source_text 的拼接方式（【标题】\\n正文，
    章节间以两个换行分隔），保证跟生成时模型实际读到的原文字符串完全一致——
    否则"逐字出现"判定会因为格式差异产生误报/漏报。已知偏差：不复刻该函数里
    "单章节且是标题占位 stub → 用下一章顶替"的历史导入兼容分支（罕见的旧数据
    修复路径），对本项目当前数据无影响（章节均为正文完整章节）。
    """
    if not chapter_indexes:
        return ""
    placeholders = ",".join("?" for _ in chapter_indexes)
    rows = conn.execute(
        f"SELECT idx, title, content FROM chapters WHERE project_id=? AND idx IN ({placeholders}) "
        "ORDER BY idx",
        (project_id, *chapter_indexes),
    ).fetchall()
    return "\n\n".join(f"【{row['title'] or ''}】\n{row['content'] or ''}" for row in rows)


def _load_pack_for_episode(
    conn: sqlite3.Connection, episode_row: sqlite3.Row,
) -> tuple[dict[str, Any] | None, str | None]:
    """优先用 episodes.published_screenplay_artifact_id（当前对外生效的准备包）；
    该指针为空时退化为该集最新 status='approved' 的 episode_prep_pack Artifact。
    """
    artifact_id = episode_row["published_screenplay_artifact_id"]
    if not artifact_id:
        row = conn.execute(
            "SELECT id FROM artifacts WHERE type='episode_prep_pack' AND scope_type='episode' "
            "AND scope_id=? AND status='approved' ORDER BY version DESC LIMIT 1",
            (episode_row["id"],),
        ).fetchone()
        artifact_id = row["id"] if row else None
    if not artifact_id:
        return None, None
    art = conn.execute("SELECT content_json FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if art is None or not art["content_json"]:
        return None, artifact_id
    try:
        payload = json.loads(art["content_json"])
    except (TypeError, ValueError):
        return None, artifact_id
    if not isinstance(payload, dict) or "prep_pack_version" not in payload:
        return None, artifact_id
    return payload, artifact_id


def _load_true_name_hints(conn: sqlite3.Connection, artifact_id: str | None) -> list[dict[str, Any]]:
    if not artifact_id:
        return []
    try:
        row = conn.execute(
            "SELECT evidence_json FROM evaluations WHERE artifact_id=? ORDER BY created_at DESC LIMIT 1",
            (artifact_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    if row is None or not row["evidence_json"]:
        return []
    try:
        evidence = json.loads(row["evidence_json"])
    except (TypeError, ValueError):
        return []
    hints = evidence.get("true_name_hints") if isinstance(evidence, dict) else None
    return hints if isinstance(hints, list) else []


def _known_characters(conn: sqlite3.Connection, project_id: str, episode_no: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, character_name FROM character_portraits WHERE project_id=? "
        "AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) ORDER BY character_name",
        (project_id, episode_no, episode_no),
    ).fetchall()


def _known_scenes(conn: sqlite3.Connection, project_id: str, episode_no: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, scene_name FROM scene_references WHERE project_id=? "
        "AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) ORDER BY scene_name",
        (project_id, episode_no, episode_no),
    ).fetchall()


def character_alias_registry(conn: sqlite3.Connection, project_id: str) -> dict[str, set[str]]:
    """portrait_id -> 该项目全部已发布 episode_prep_pack 里记录过的 aliases 并集。"""
    registry: dict[str, set[str]] = {}
    rows = conn.execute(
        "SELECT a.content_json FROM artifacts a JOIN episodes e "
        "ON e.published_screenplay_artifact_id = a.id "
        "WHERE e.project_id=? AND a.type='episode_prep_pack'",
        (project_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError):
            continue
        characters = ((payload.get("asset_manifest") or {}).get("characters")) or []
        for character in characters:
            portrait_id = character.get("portrait_id")
            if not portrait_id:
                continue
            bucket = registry.setdefault(str(portrait_id), set())
            for alias in character.get("aliases") or []:
                alias = str(alias).strip()
                if alias:
                    bucket.add(alias)
    return registry


def scene_alias_registry(conn: sqlite3.Connection, project_id: str) -> dict[str, set[str]]:
    """scene_name -> projects.bible_json 里 Scene.aliases（app.schemas.Scene 才有
    该字段；Character 没有，故角色侧改用 character_alias_registry）。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    registry: dict[str, set[str]] = {}
    if not row or not row["bible_json"]:
        return registry
    try:
        bible = json.loads(row["bible_json"])
    except (TypeError, ValueError):
        return registry
    for scene in bible.get("scenes") or []:
        name = str(scene.get("name") or "").strip()
        if not name:
            continue
        aliases = {str(a).strip() for a in (scene.get("aliases") or []) if str(a).strip()}
        if aliases:
            registry[name] = aliases
    return registry


def _find_in_any_chapter(conn: sqlite3.Connection, project_id: str, name: str) -> tuple[int, str] | None:
    """在本项目全部章节（不止本集）里找 ``name`` 的第一次逐字出现，返回
    (章节 idx, 上下文)。只在 true_name_hints 的独立复核里用到（见模块 docstring
    的"已知偏差"说明：真实数据没有段号，退而求其次用章节级证据）。"""
    if not name:
        return None
    rows = conn.execute(
        "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,),
    ).fetchall()
    for row in rows:
        text = f"【{row['title'] or ''}】\n{row['content'] or ''}"
        pos = text.find(name)
        if pos >= 0:
            return int(row["idx"]), _context_window(text, pos, len(name))
    return None


# ---------------------------------------------------------------------------
# 文本判据小工具
# ---------------------------------------------------------------------------

def _context_window(text: str, pos: int, match_len: int, width: int = CONTEXT_WINDOW_CHARS) -> str:
    start = max(0, pos - width // 2)
    end = min(len(text), start + width)
    start = max(0, end - width)
    return text[start:end].replace("\n", " ")


_DIALOGUE_TAIL_RE = re.compile(r'^\s*[:：]|^\s*["“]')


def _has_dialogue_signal(text: str, end_pos: int) -> bool:
    tail = text[end_pos:end_pos + 6]
    return bool(_DIALOGUE_TAIL_RE.match(tail))


def _scan_name_occurrences(text: str, candidates: list[str]) -> tuple[int, int, str] | None:
    """在 ``text`` 里统计 ``candidates``（去重、按长度降序，避免短别名是长别名
    子串时重复计数）的合计命中次数，返回 (次数, 首次出现位置, 命中的那个候选词)。
    """
    uniq = list(dict.fromkeys(c for c in candidates if c and len(c) >= MIN_SCAN_NAME_CHARS))
    if not uniq:
        return None
    uniq.sort(key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(c) for c in uniq))
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    first = matches[0]
    return len(matches), first.start(), first.group(0)


def _find_accepted_hint(
    hints: list[dict[str, Any]], *, kind: str, target_names: set[str],
) -> dict[str, Any] | None:
    for hint in hints:
        if not isinstance(hint, dict):
            continue
        if hint.get("kind") != kind or hint.get("status") != "accepted":
            continue
        suspected = str(hint.get("suspected_true_name") or "").strip()
        mention = str(hint.get("mention") or "").strip()
        if suspected in target_names or mention in target_names:
            return hint
    return None


# ---------------------------------------------------------------------------
# 方向 A · 幻觉检查
# ---------------------------------------------------------------------------

def check_manifest_characters(
    pack: dict[str, Any], source_text: str, hints: list[dict[str, Any]], verify_hint,
) -> tuple[list[Issue], int, int]:
    issues: list[Issue] = []
    entries = ((pack.get("asset_manifest") or {}).get("characters")) or []
    checked = len(entries)
    passed = 0
    for character in entries:
        display_name = str(character.get("display_name") or "").strip()
        aliases = [str(a).strip() for a in (character.get("aliases") or []) if str(a).strip()]
        candidates = [display_name, *aliases]
        if any(name and name in source_text for name in candidates):
            passed += 1
            continue
        hint = _find_accepted_hint(hints, kind="character", target_names=set(candidates))
        if hint is not None:
            verified = verify_hint(str(hint.get("suspected_true_name") or ""))
            if verified is not None:
                passed += 1
                continue
        issues.append(Issue(
            code="A1_character_no_text_evidence",
            message=(
                f"角色「{display_name}」（portrait_id={character.get('portrait_id')}）"
                f"在本集原文中无任何文本依据（display_name 与全部 {len(aliases)} 个 "
                "aliases 均未逐字出现）"
            ),
            detail={
                "display_name": display_name, "aliases": aliases,
                "portrait_id": character.get("portrait_id"), "event_ids": character.get("event_ids"),
            },
        ))
    return issues, checked, passed


def check_manifest_scenes(
    pack: dict[str, Any], source_text: str, hints: list[dict[str, Any]], verify_hint,
) -> tuple[list[Issue], int, int]:
    issues: list[Issue] = []
    entries = ((pack.get("asset_manifest") or {}).get("scenes")) or []
    checked = len(entries)
    passed = 0
    for scene in entries:
        display_name = str(scene.get("display_name") or "").strip()
        if display_name and display_name in source_text:
            passed += 1
            continue
        hint = _find_accepted_hint(hints, kind="scene", target_names={display_name})
        if hint is not None:
            verified = verify_hint(str(hint.get("suspected_true_name") or ""))
            if verified is not None:
                passed += 1
                continue
        issues.append(Issue(
            code="A2_scene_no_text_evidence",
            message=(
                f"场景「{display_name}」（scene_reference_id={scene.get('scene_reference_id')}）"
                "在本集原文中无任何文本依据"
            ),
            detail={
                "display_name": display_name, "scene_reference_id": scene.get("scene_reference_id"),
                "event_ids": scene.get("event_ids"),
            },
        ))
    return issues, checked, passed


def _build_roster_ref_set(pack: dict[str, Any]) -> set[str]:
    manifest = pack.get("asset_manifest") or {}
    refs: set[str] = set()
    for character in manifest.get("characters") or []:
        identity_id = character.get("identity_id")
        if identity_id:
            refs.add(str(identity_id))
    for extra in manifest.get("functional_extras") or []:
        label = extra.get("label")
        if label:
            refs.add(f"extra:{label}")
    return refs


def check_key_line_speakers(
    pack: dict[str, Any], source_text: str,
) -> tuple[list[Issue], tuple[int, int], tuple[int, int]]:
    issues: list[Issue] = []
    checked_text = passed_text = 0
    checked_ref = passed_ref = 0
    roster = _build_roster_ref_set(pack)
    for event in pack.get("event_chain") or []:
        event_id = event.get("event_id")
        for key_line in event.get("key_lines") or []:
            speaker = str(key_line.get("speaker") or "").strip()
            if speaker:
                checked_text += 1
                if speaker in source_text:
                    passed_text += 1
                else:
                    issues.append(Issue(
                        code="A3_speaker_no_text_evidence",
                        message=f"事件 {event_id} 台词说话人「{speaker}」在本集原文中无任何文本依据",
                        detail={"event_id": event_id, "speaker": speaker, "line": key_line.get("line")},
                    ))
            if "speaker_ref" in key_line:
                checked_ref += 1
                ref = str(key_line.get("speaker_ref") or "")
                if ref in roster:
                    passed_ref += 1
                else:
                    issues.append(Issue(
                        code="A3b_speaker_ref_unresolved",
                        message=(
                            f"事件 {event_id} 台词说话人「{speaker}」的 speaker_ref={ref!r} "
                            "未指向本集资产名册（characters/functional_extras）任何条目"
                        ),
                        detail={"event_id": event_id, "speaker": speaker, "speaker_ref": ref},
                    ))
    return issues, (checked_text, passed_text), (checked_ref, passed_ref)


def sample_source_evidence(
    pack: dict[str, Any], sample_size: int,
) -> list[tuple[str, int, str]]:
    """从 event_chain 全部 source_evidence 里均匀抽样，覆盖首尾而非只取前几条。"""
    items: list[tuple[str, int, str]] = []
    for event in pack.get("event_chain") or []:
        event_id = event.get("event_id")
        for evidence in event.get("source_evidence") or []:
            items.append((event_id, evidence.get("segment_index"), str(evidence.get("quote") or "")))
    if not items or sample_size <= 0:
        return []
    n = len(items)
    if n <= sample_size:
        return items
    picks: list[tuple[str, int, str]] = []
    seen_index: set[int] = set()
    for i in range(sample_size):
        idx = round(i * (n - 1) / (sample_size - 1)) if sample_size > 1 else 0
        if idx not in seen_index:
            seen_index.add(idx)
            picks.append(items[idx])
    return picks


def check_evidence_sample(
    sample: list[tuple[str, int, str]], source_text: str,
) -> tuple[list[Issue], int, int]:
    issues: list[Issue] = []
    passed = 0
    for event_id, segment_index, quote in sample:
        quote_stripped = quote.strip()
        if quote_stripped and quote_stripped in source_text:
            passed += 1
        else:
            issues.append(Issue(
                code="A4_evidence_quote_mismatch",
                message=(
                    f"事件 {event_id} 段 {segment_index} 的 source_evidence 引文"
                    f"「{quote_stripped[:40]}」未逐字命中本集原文"
                ),
                detail={"event_id": event_id, "segment_index": segment_index, "quote": quote_stripped},
            ))
    return issues, len(sample), passed


# ---------------------------------------------------------------------------
# 方向 B · 遗漏检查（疑似，不做武断判定）
# ---------------------------------------------------------------------------

def check_missing_characters(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
    pack: dict[str, Any], source_text: str, alias_registry: dict[str, set[str]],
) -> tuple[list[Issue], int, int]:
    manifest_portrait_ids = {
        str(c.get("portrait_id")) for c in ((pack.get("asset_manifest") or {}).get("characters")) or []
        if c.get("portrait_id")
    }
    issues: list[Issue] = []
    scanned = 0
    clean = 0
    for row in _known_characters(conn, project_id, episode_no):
        scanned += 1
        if row["id"] in manifest_portrait_ids:
            clean += 1
            continue
        candidates = [row["character_name"], *sorted(alias_registry.get(row["id"], set()))]
        hit = _scan_name_occurrences(source_text, candidates)
        if hit is None:
            clean += 1
            continue
        count, pos, matched = hit
        issues.append(Issue(
            code="B1_character_missing",
            message=(
                f"角色「{row['character_name']}」（portrait_id={row['id']}）在本集原文中出现 "
                f"{count} 次（命中「{matched}」），但未被 asset_manifest.characters 收录"
            ),
            detail={
                "character_name": row["character_name"], "portrait_id": row["id"],
                "occurrences": count, "matched_as": matched,
                "first_context": _context_window(source_text, pos, len(matched)),
                "dialogue_signal": _has_dialogue_signal(source_text, pos + len(matched)),
            },
        ))
    return issues, scanned, clean


def check_missing_scenes(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
    pack: dict[str, Any], source_text: str, alias_registry: dict[str, set[str]],
) -> tuple[list[Issue], int, int]:
    manifest_scene_ids = {
        str(s.get("scene_reference_id")) for s in ((pack.get("asset_manifest") or {}).get("scenes")) or []
        if s.get("scene_reference_id")
    }
    issues: list[Issue] = []
    scanned = 0
    clean = 0
    for row in _known_scenes(conn, project_id, episode_no):
        scanned += 1
        if row["id"] in manifest_scene_ids:
            clean += 1
            continue
        candidates = [row["scene_name"], *sorted(alias_registry.get(row["scene_name"], set()))]
        hit = _scan_name_occurrences(source_text, candidates)
        if hit is None:
            clean += 1
            continue
        count, pos, matched = hit
        issues.append(Issue(
            code="B2_scene_missing",
            message=(
                f"场景「{row['scene_name']}」（scene_reference_id={row['id']}）在本集原文中出现 "
                f"{count} 次（命中「{matched}」），但未被 asset_manifest.scenes 收录"
            ),
            detail={
                "scene_name": row["scene_name"], "scene_reference_id": row["id"],
                "occurrences": count, "matched_as": matched,
                "first_context": _context_window(source_text, pos, len(matched)),
                "dialogue_signal": _has_dialogue_signal(source_text, pos + len(matched)),
            },
        ))
    return issues, scanned, clean


# ---------------------------------------------------------------------------
# 单集审计入口
# ---------------------------------------------------------------------------

def audit_episode(
    conn: sqlite3.Connection, project_id: str, episode_no: int, *,
    sample_size: int = DEFAULT_EVIDENCE_SAMPLE,
    char_alias_registry: dict[str, set[str]] | None = None,
    scene_alias_registry_: dict[str, set[str]] | None = None,
) -> EpisodeAuditResult:
    result = EpisodeAuditResult(episode_no=episode_no)

    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE project_id=? AND episode_no=?", (project_id, episode_no),
    ).fetchone()
    if episode_row is None:
        result.skipped_reason = "项目中不存在该集"
        return result
    result.episode_id = episode_row["id"]

    pack, artifact_id = _load_pack_for_episode(conn, episode_row)
    result.artifact_id = artifact_id
    if pack is None:
        status = episode_row["screenplay_status"]
        result.skipped_reason = f"无可用的已发布 episode_prep_pack（screenplay_status={status!r}），跳过审计"
        return result

    result.prep_pack_version = pack.get("prep_pack_version")
    chapter_indexes = list((pack.get("episode_scope") or {}).get("chapter_indexes") or [])
    if not chapter_indexes:
        raw = episode_row["source_chapters"] or "[]"
        try:
            chapter_indexes = json.loads(raw) if isinstance(raw, str) else list(raw)
        except (TypeError, ValueError):
            chapter_indexes = []
    result.chapter_indexes = [int(i) for i in chapter_indexes]

    source_text = _build_source_text(conn, project_id, result.chapter_indexes)
    hints = _load_true_name_hints(conn, artifact_id)

    def verify_hint(name: str) -> tuple[int, str] | None:
        return _find_in_any_chapter(conn, project_id, name)

    char_registry = (
        char_alias_registry if char_alias_registry is not None
        else character_alias_registry(conn, project_id)
    )
    scene_registry = (
        scene_alias_registry_ if scene_alias_registry_ is not None
        else scene_alias_registry(conn, project_id)
    )

    issues, checked, passed = check_manifest_characters(pack, source_text, hints, verify_hint)
    result.a_issues.extend(issues)
    result.tallies["A1 角色绑定文本依据"] = CheckTally(checked, passed)

    issues, checked, passed = check_manifest_scenes(pack, source_text, hints, verify_hint)
    result.a_issues.extend(issues)
    result.tallies["A2 场景绑定文本依据"] = CheckTally(checked, passed)

    issues, (checked_text, passed_text), (checked_ref, passed_ref) = check_key_line_speakers(
        pack, source_text,
    )
    result.a_issues.extend(issues)
    result.tallies["A3 台词说话人文本依据"] = CheckTally(checked_text, passed_text)
    result.tallies["A3b 说话人 speaker_ref 名册核验"] = CheckTally(checked_ref, passed_ref)

    sample = sample_source_evidence(pack, sample_size)
    issues, checked, passed = check_evidence_sample(sample, source_text)
    result.a_issues.extend(issues)
    result.tallies["A4 source_evidence 引文抽查"] = CheckTally(checked, passed)

    issues, scanned, clean = check_missing_characters(
        conn, project_id, episode_no, pack, source_text, char_registry,
    )
    result.b_issues.extend(issues)
    result.tallies["B1 角色遗漏扫描"] = CheckTally(scanned, clean)

    issues, scanned, clean = check_missing_scenes(
        conn, project_id, episode_no, pack, source_text, scene_registry,
    )
    result.b_issues.extend(issues)
    result.tallies["B2 场景遗漏扫描"] = CheckTally(scanned, clean)

    return result


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------

def format_episode(result: EpisodeAuditResult) -> str:
    lines = ["=" * 78]
    lines.append(
        f"第 {result.episode_no} 集   episode_id={result.episode_id or '-'}   "
        f"artifact_id={result.artifact_id or '-'}   "
        f"prep_pack_version={result.prep_pack_version or '-'}"
    )
    lines.append(f"章节范围（episode_scope.chapter_indexes）：{result.chapter_indexes or '-'}")
    if result.skipped_reason:
        lines.append(f"[跳过] {result.skipped_reason}")
        return "\n".join(lines)

    lines.append(f"-- 方向 A · 幻觉检查（{len(result.a_issues)} 条差异） --")
    if not result.a_issues:
        lines.append("  （无）")
    else:
        for i, issue in enumerate(result.a_issues, start=1):
            lines.append(f"  A{i}. [{issue.code}] {issue.message}")

    lines.append(f"-- 方向 B · 遗漏检查（疑似，{len(result.b_issues)} 条，供人工复核） --")
    if not result.b_issues:
        lines.append("  （无）")
    else:
        for i, issue in enumerate(result.b_issues, start=1):
            detail = issue.detail
            extra = ""
            if "occurrences" in detail:
                signal = "是" if detail.get("dialogue_signal") else "否"
                extra = f"（对白痕迹={signal}，首处：{detail.get('first_context', '')}）"
            lines.append(f"  B{i}. [{issue.code}] {issue.message}{extra}")

    lines.append("-- 通过项计数 --")
    for name, tally in result.tallies.items():
        lines.append(f"  {name}：{tally.passed}/{tally.checked} 通过")
    return "\n".join(lines)


def format_summary(results: list[EpisodeAuditResult]) -> str:
    lines = ["=" * 78, "全局汇总", "-" * 78]
    lines.append(f"{'集号':<6}{'A类':<6}{'B类':<6}状态")
    total_a = 0
    total_b = 0
    for result in results:
        a_count = len(result.a_issues)
        b_count = len(result.b_issues)
        total_a += a_count
        total_b += b_count
        if result.skipped_reason:
            status = "跳过"
        elif a_count == 0 and b_count == 0:
            status = "全清"
        elif a_count > 0:
            status = "A 类告警"
        else:
            status = "仅 B 类疑似"
        lines.append(f"{result.episode_no:<6}{a_count:<6}{b_count:<6}{status}")
    lines.append("-" * 78)
    lines.append(f"合计：A 类（确定性差异）{total_a} 条，B 类（疑似遗漏）{total_b} 条")
    return "\n".join(lines)


def exit_code(results: list[EpisodeAuditResult]) -> int:
    total_a = sum(len(r.a_issues) for r in results)
    total_b = sum(len(r.b_issues) for r in results)
    if total_a > 0:
        return 1
    if total_b > 0:
        return 2
    return 0


def _result_to_dict(result: EpisodeAuditResult) -> dict[str, Any]:
    return {
        "episode_no": result.episode_no,
        "episode_id": result.episode_id,
        "artifact_id": result.artifact_id,
        "prep_pack_version": result.prep_pack_version,
        "chapter_indexes": result.chapter_indexes,
        "skipped_reason": result.skipped_reason,
        "a_issues": [
            {"code": i.code, "message": i.message, "detail": i.detail} for i in result.a_issues
        ],
        "b_issues": [
            {"code": i.code, "message": i.message, "detail": i.detail} for i in result.b_issues
        ],
        "tallies": {
            name: {"checked": tally.checked, "passed": tally.passed}
            for name, tally in result.tallies.items()
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备包 vs 小说原文 逐集对照审计（只读）")
    parser.add_argument("--project", default=DEFAULT_PROJECT_ID, help="project_id")
    parser.add_argument("--start", type=int, default=DEFAULT_START_EPISODE, help="起始集号（含）")
    parser.add_argument("--end", type=int, default=DEFAULT_END_EPISODE, help="结束集号（含）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="manju.db 路径")
    parser.add_argument(
        "--sample-size", type=int, default=DEFAULT_EVIDENCE_SAMPLE,
        help="每集抽查的 source_evidence 引文条数",
    )
    parser.add_argument("--json", type=Path, default=None, help="可选：把结构化结果写入该 JSON 文件")
    args = parser.parse_args(argv)

    conn = readonly_connection(args.db)
    try:
        char_registry = character_alias_registry(conn, args.project)
        scene_registry = scene_alias_registry(conn, args.project)

        results: list[EpisodeAuditResult] = []
        for episode_no in range(args.start, args.end + 1):
            result = audit_episode(
                conn, args.project, episode_no,
                sample_size=args.sample_size,
                char_alias_registry=char_registry,
                scene_alias_registry_=scene_registry,
            )
            results.append(result)
            print(format_episode(result))
            print()

        print(format_summary(results))

        if args.json:
            args.json.write_text(
                json.dumps([_result_to_dict(r) for r in results], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"\n结构化结果已写入：{args.json}")

        return exit_code(results)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
