#!/usr/bin/env python3
"""逐集对照审计：episode_prep_pack（映射台产物）vs 小说原文。

背景（见 docs/TRANSFORM_FREEZE_PLAN.md §3、app/production/prep_pack.py 模块
docstring）：映射台为每一集产出 episode_prep_pack（artifacts.content_json，
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
  A3  event_chain[].key_lines[].speaker 逐字出现在本集源文本，**或**（round-18
      口径修正）speaker 对应的 manifest.characters 条目（经 speaker_ref 或
      speaker 与 display_name 精确匹配定位）任一 aliases 逐字出现在本集源
      文本——事件链抽取提示词只强制 characters[].display_name 必须是原文自
      己的称谓，从未对 key_lines[].speaker 提这个要求，1.5.0 起模型经常直接
      写正名（如"李富贵"）而原文只出现过别名（如"小胖子"），这不是幻觉，
      是合法称谓变体，只是没落在 speaker 字符串本身上，见 check_key_line_
      speakers 的函数 docstring。functional_extras 的 label 没有别名来源，
      仍然只能逐字直查。
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
sys.path.insert(0, str(ROOT))

# index_source_segments is the SAME segmenter app/production/prep_pack.py uses
# to number segment_index (1-based enumerate over its output, see that
# module's _generate_prep_pack_once). provenance.anchor_segments (1.6.0, see
# the "1.6.0 provenance upgrade" note on check_manifest_characters below)
# references those same numbers -- reimplementing a different splitter here
# would make anchor verification meaningless (numbers would refer to
# different text spans than the generator meant). app/__init__.py is empty
# and app/source_excerpt.py itself has no DB/IO side effects (pure
# re/difflib/dataclasses), so this import is safe for a read-only audit
# script and does not pull in app.db / app.config / any live-service wiring.
from app.source_excerpt import SourceSegment, index_source_segments  # noqa: E402

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

# --- 1.6.0 provenance 升级（协调方指令，管线尚未实际发布，形状以后端 agent
# 实际交付为准）--------------------------------------------------------------
# 假定形状：manifest.characters[]/manifest.scenes[] 每项、以及 key_lines[]
# 每条，携带 provenance: {"method": <下列枚举之一>, "anchor_segments": [int],
# "anchor_phrase": str, ...方法特定字段}。key_lines 侧字段名未定，按指令
# "字段名有出入以实物为准并注明"处理：本工具读取 key_line.get(
# "speaker_provenance")，缺失时回退读取 key_line.get("provenance")，两者都
# 没有则按"旧版无 provenance"处理——一旦实物字段名确定且与这两个假设都不
# 同，只需改 check_key_line_speakers 取值那一行。
#
# 收尾单新增两个正式方法（此前作为未识别方法 fail-closed，现给出确定性判
# 据；只在 A1/A2——manifest.characters[]/scenes[]——生效，key_lines 侧的
# speaker 目前不使用这两个方法，协调方指令本身也只提"该资产"/manifest 绑
# 定，未提 speaker）：
#   - resolution_forward：额外字段假定 forward_chapter_label（如
#     "第 692 章"，人类可读的前瞻章节标注，不是本集自己的 segment 编号——前
#     瞻章节根本不在本集切分范围内）。核验换一条路：从标注里正则抽出章节号
#     按 chapters.idx 直接定位那一整章原文，检查 anchor_phrase 是否逐字出现
#     在那一章里。见 _verify_provenance_forward_anchor。
#   - alias_inherited：额外字段假定 source_episode_no（int，来源集号）。核
#     验：来源集必须严格早于当前集（语义上"来源"就该更早，顺带让递归天然不
#     成环）；来源集须有已发布 pack 且其 asset_manifest 中存在同一 portrait_
#     id/scene_reference_id 且同 display_name 的绑定；那条来源绑定自身还要
#     递归核验通过（用它自己的 provenance.method 或旧版回退标准，对着来源
#     集自己的原文）——不是只看"来源集里有没有同名条目"，还要看那条条目本
#     身站不站得住。见 _verify_alias_inherited_character/_scene。
TEXT_VERIFIED_METHODS = {"direct", "alias"}
# candidate_verdict（1.8.0，见 app/production/prep_pack.py PREP_PACK_VERSION
# 上方大注释与 _prep_pack_resolve_functional_extra_candidate 的完整说明）：
# 未解析角色标签的候选判别命中——代码检索卷宗 → 模型候选选择题（enum 收紧到
# 候选集 + "都不是/无法确定"）→ 段号钉证（enum 限定卷宗段号，结构性核对，不
# 比对模型转录）。与 resolution/discovery/absorbed_speaker 同构：anchor_
# segments/anchor_phrase 是这次绑定的证据链本身，且 anchor_phrase 直接取
# 钉中段落的原文文本（代码检索所得，非模型转录），天然满足逐字命中。目前只
# 在 asset_manifest.characters[] 上产出（未用于 scenes/key_lines），但核验
# 分支按 method 值分型、不按字段位置分型，登记进本集合即对三处调用点
# （check_manifest_characters/check_manifest_scenes/check_key_line_speakers）
# 统一生效，是安全的超集登记。
ANCHOR_VERIFIED_METHODS = {"resolution", "discovery", "absorbed_speaker", "candidate_verdict"}
FORWARD_ANCHOR_METHOD = "resolution_forward"
INHERITED_ALIAS_METHOD = "alias_inherited"
KNOWN_PROVENANCE_METHODS = (
    TEXT_VERIFIED_METHODS | ANCHOR_VERIFIED_METHODS | {FORWARD_ANCHOR_METHOD, INHERITED_ALIAS_METHOD}
)
# alias_inherited 的递归深度上限：来源集号严格递减已经让链条不可能成环，这
# 里只是再加一层防御，防止未来数据形状变化后出现未预期的自引用。
MAX_ALIAS_INHERITED_DEPTH = 8
_FORWARD_CHAPTER_LABEL_RE = re.compile(r"第\s*(\d+)\s*章")


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
    # 1.6.0 provenance 升级：这项检查里有多少条目走的是"无 provenance"回退
    # 路径（<=1.5.x 旧包，或 1.6.0 包里个别条目缺该字段）。0 表示这项检查在
    # 本集全部条目上都用了 provenance 分型核验，不是旧标准。
    legacy_fallback: int = 0


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


def _load_pack_for_episode_no(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """按 (project_id, episode_no) 查 episode 行后委派给 _load_pack_for_episode
    ——alias_inherited（1.6.0）核验来源集时用，找不到该集本身也是合法的失败
    原因（来源断链），不是异常。"""
    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE project_id=? AND episode_no=?", (project_id, episode_no),
    ).fetchone()
    if episode_row is None:
        return None, None
    return _load_pack_for_episode(conn, episode_row)


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

def _verify_provenance_anchor(
    segments: list[SourceSegment], provenance: dict[str, Any],
) -> tuple[bool, str]:
    """1.6.0 provenance 的锚点核验（resolution/discovery/absorbed_speaker/
    candidate_verdict 四种方法用，见 ANCHOR_VERIFIED_METHODS）：anchor_
    segments 每个段号必须落在 [1, len(segments)]（与 app.source_excerpt.
    index_source_segments 的 1-based 编号同一套，就是生成器自己的
    segment_index），且 anchor_phrase 必须逐字出现在这些段号拼接后的原文
    里。这四种方法允许 display_name/speaker 本身是合成规范名或群演标签、不
    再直接苛求逐字命中，换成要求锚点链本身确定性成立——段号必须真实存在，
    短语必须是那几段原文里真写过的话，不能是模型编的。返回
    (是否通过, 失败原因；通过时为空串)。"""
    anchor_segments = provenance.get("anchor_segments")
    anchor_phrase = str(provenance.get("anchor_phrase") or "").strip()
    if not anchor_segments:
        return False, "provenance.anchor_segments 为空"
    if not anchor_phrase:
        return False, "provenance.anchor_phrase 为空"
    texts: list[str] = []
    for raw_index in anchor_segments:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return False, f"锚点段号 {raw_index!r} 不是合法整数"
        if index < 1 or index > len(segments):
            return False, f"锚点段号 {index} 越界（本集共 {len(segments)} 段）"
        texts.append(segments[index - 1].text)
    if anchor_phrase not in "".join(texts):
        return False, (
            f"锚点短语「{anchor_phrase[:40]}」未在锚点段 {list(anchor_segments)} 的原文中逐字命中"
        )
    return True, ""


def _dispatch_provenance(provenance: Any) -> tuple[str | None, dict[str, Any] | None]:
    """provenance 缺失/非法形状 -> (None, None)，调用方按 <=1.5.x 旧包回退处
    理；否则 -> (method 字符串, provenance dict)，method 可能不在已知枚举内
    （未来新增方法/字段拼写不同），调用方对此也要有出口，不能默认放行。"""
    if not isinstance(provenance, dict):
        return None, None
    return str(provenance.get("method") or "").strip(), provenance


def _verify_provenance_forward_anchor(
    conn: sqlite3.Connection, project_id: str, provenance: dict[str, Any],
) -> tuple[bool, str]:
    """resolution_forward（收尾单新增）：provenance.forward_chapter_label（假
    定字段名，如"第 692 章"）标注 anchor_phrase 实际出现在的前瞻章节——不在
    本集自己的 segments 编号体系内，前瞻章节根本不是本集切分出的段，所以核
    验换一条路：从标注文本里正则抽出章节号，直接按 chapters.idx 定位那一整
    章原文，检查 anchor_phrase 是否逐字出现在那一整章里。章节号解析失败、章
    节不存在、或短语未命中，都是"A 类"（章节无效或短语不命中）。"""
    label = str(provenance.get("forward_chapter_label") or "").strip()
    anchor_phrase = str(provenance.get("anchor_phrase") or "").strip()
    if not label:
        return False, "provenance.forward_chapter_label 为空"
    if not anchor_phrase:
        return False, "provenance.anchor_phrase 为空"
    match = _FORWARD_CHAPTER_LABEL_RE.search(label)
    if not match:
        return False, f'前瞻章节标注「{label}」无法解析出章节号（期望形如"第 N 章"）'
    chapter_idx = int(match.group(1))
    row = conn.execute(
        "SELECT title, content FROM chapters WHERE project_id=? AND idx=?",
        (project_id, chapter_idx),
    ).fetchone()
    if row is None:
        return False, f"前瞻章节标注「{label}」（章节号 {chapter_idx}）未能定位到有效章节"
    chapter_text = f"【{row['title'] or ''}】\n{row['content'] or ''}"
    if anchor_phrase not in chapter_text:
        return False, f"锚点短语「{anchor_phrase[:40]}」未在第 {chapter_idx} 章原文中逐字命中"
    return True, ""


def _verify_character_entry(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
    source_text: str, segments: list[SourceSegment],
    hints: list[dict[str, Any]], verify_hint, character: dict[str, Any], *, depth: int = 0,
) -> tuple[bool, str, str | None]:
    """核验一条 asset_manifest.characters[] 绑定，纯判定、不构造 Issue（措辞
    因调用方是 A1 主循环还是 alias_inherited 递归而异，留给调用方拼）。返回
    (是否通过, 失败原因；通过时为空串, 实际生效的 method 标签；旧包回退时为
    None)。被 check_manifest_characters 和 _verify_alias_inherited_character
    共用，这样 alias_inherited"来源绑定自身核验通过"才是真的按同一套规则复
    核，而不是另开一条更松的路径。"""
    display_name = str(character.get("display_name") or "").strip()
    aliases = [str(a).strip() for a in (character.get("aliases") or []) if str(a).strip()]
    candidates = [display_name, *aliases]
    method, provenance = _dispatch_provenance(character.get("provenance"))

    if method is None:
        # 旧包回退：唯一还会查 true_name_hints 例外的路径（见函数 docstring）。
        if any(name and name in source_text for name in candidates):
            return True, "", None
        hint = _find_accepted_hint(hints, kind="character", target_names=set(candidates))
        if hint is not None and verify_hint(str(hint.get("suspected_true_name") or "")) is not None:
            return True, "", None
        return False, f"display_name 与全部 {len(aliases)} 个 aliases 均未逐字出现", None

    if method in TEXT_VERIFIED_METHODS:
        if any(name and name in source_text for name in candidates):
            return True, "", method
        return False, f"display_name 与全部 {len(aliases)} 个 aliases 均未逐字出现", method

    if method in ANCHOR_VERIFIED_METHODS:
        ok, reason = _verify_provenance_anchor(segments, provenance)
        return ok, reason, method

    if method == FORWARD_ANCHOR_METHOD:
        ok, reason = _verify_provenance_forward_anchor(conn, project_id, provenance)
        return ok, reason, method

    if method == INHERITED_ALIAS_METHOD:
        if depth >= MAX_ALIAS_INHERITED_DEPTH:
            return False, "alias_inherited 递归深度超限，疑似循环/断链引用", method
        ok, reason = _verify_alias_inherited_character(
            conn, project_id, episode_no, character, provenance, verify_hint, depth=depth,
        )
        return ok, reason, method

    return False, f"method={method!r} 不在已知枚举内，无法核验", method


def _verify_alias_inherited_character(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
    character: dict[str, Any], provenance: dict[str, Any], verify_hint, *, depth: int,
) -> tuple[bool, str]:
    """alias_inherited（收尾单新增，后端即将引入）：provenance.source_episode_
    no（假定字段名，int）指向更早一集——来源集须有已发布 pack，且其
    asset_manifest.characters 中存在同一 portrait_id 且同 display_name 的绑
    定（"同名绑定"），并且那条来源绑定自身也要核验通过（递归调用
    _verify_character_entry，对着来源集自己的原文/segments/hints——不是对着
    本集）。来源缺失、断链（找不到同 portrait_id 的条目、名字对不上）、或来
    源绑定自身核验不过，都判定失败。"""
    raw_source_ep = provenance.get("source_episode_no")
    try:
        source_episode_no = int(raw_source_ep)
    except (TypeError, ValueError):
        return False, f"provenance.source_episode_no={raw_source_ep!r} 不是合法集号"
    if source_episode_no >= episode_no:
        return False, f"来源集号 {source_episode_no} 不早于当前集 {episode_no}，来源非法"

    source_pack, source_artifact_id = _load_pack_for_episode_no(conn, project_id, source_episode_no)
    if source_pack is None:
        return False, f"来源集（第 {source_episode_no} 集）无已发布 episode_prep_pack，来源断链"

    portrait_id = character.get("portrait_id")
    source_entry = next(
        (
            c for c in ((source_pack.get("asset_manifest") or {}).get("characters")) or []
            if c.get("portrait_id") == portrait_id
        ),
        None,
    )
    if source_entry is None:
        return False, (
            f"来源集（第 {source_episode_no} 集）pack 中未找到 portrait_id={portrait_id} "
            "的绑定，来源断链"
        )

    display_name = str(character.get("display_name") or "").strip()
    source_display_name = str(source_entry.get("display_name") or "").strip()
    if display_name != source_display_name:
        return False, (
            f"来源集（第 {source_episode_no} 集）对该资产的绑定名为「{source_display_name}」，"
            f"与本集声称继承的「{display_name}」不一致，来源断链"
        )

    source_chapter_indexes = list((source_pack.get("episode_scope") or {}).get("chapter_indexes") or [])
    source_text = _build_source_text(conn, project_id, source_chapter_indexes)
    source_segments = index_source_segments(source_text)
    source_hints = _load_true_name_hints(conn, source_artifact_id)

    ok, reason, _method = _verify_character_entry(
        conn, project_id, source_episode_no, source_text, source_segments,
        source_hints, verify_hint, source_entry, depth=depth + 1,
    )
    if not ok:
        return False, f"来源集（第 {source_episode_no} 集）该绑定自身核验未通过：{reason}"
    return True, ""


def _verify_scene_entry(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
    source_text: str, segments: list[SourceSegment],
    hints: list[dict[str, Any]], verify_hint, scene: dict[str, Any], *, depth: int = 0,
) -> tuple[bool, str, str | None]:
    """A2 版的 _verify_character_entry，逻辑同构，见其 docstring。"""
    display_name = str(scene.get("display_name") or "").strip()
    aliases = [str(a).strip() for a in (scene.get("aliases") or []) if str(a).strip()]
    candidates = [display_name, *aliases]
    method, provenance = _dispatch_provenance(scene.get("provenance"))

    if method is None:
        # 旧包回退：唯一还会查 true_name_hints 例外的路径。
        if any(name and name in source_text for name in candidates):
            return True, "", None
        hint = _find_accepted_hint(hints, kind="scene", target_names=set(candidates))
        if hint is not None and verify_hint(str(hint.get("suspected_true_name") or "")) is not None:
            return True, "", None
        return False, f"display_name 与全部 {len(aliases)} 个 aliases 均未逐字出现", None

    if method in TEXT_VERIFIED_METHODS:
        if any(name and name in source_text for name in candidates):
            return True, "", method
        return False, f"display_name 与全部 {len(aliases)} 个 aliases 均未逐字出现", method

    if method in ANCHOR_VERIFIED_METHODS:
        ok, reason = _verify_provenance_anchor(segments, provenance)
        return ok, reason, method

    if method == FORWARD_ANCHOR_METHOD:
        ok, reason = _verify_provenance_forward_anchor(conn, project_id, provenance)
        return ok, reason, method

    if method == INHERITED_ALIAS_METHOD:
        if depth >= MAX_ALIAS_INHERITED_DEPTH:
            return False, "alias_inherited 递归深度超限，疑似循环/断链引用", method
        ok, reason = _verify_alias_inherited_scene(
            conn, project_id, episode_no, scene, provenance, verify_hint, depth=depth,
        )
        return ok, reason, method

    return False, f"method={method!r} 不在已知枚举内，无法核验", method


def _verify_alias_inherited_scene(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
    scene: dict[str, Any], provenance: dict[str, Any], verify_hint, *, depth: int,
) -> tuple[bool, str]:
    """scene 版的 _verify_alias_inherited_character，按 scene_reference_id 匹配。"""
    raw_source_ep = provenance.get("source_episode_no")
    try:
        source_episode_no = int(raw_source_ep)
    except (TypeError, ValueError):
        return False, f"provenance.source_episode_no={raw_source_ep!r} 不是合法集号"
    if source_episode_no >= episode_no:
        return False, f"来源集号 {source_episode_no} 不早于当前集 {episode_no}，来源非法"

    source_pack, source_artifact_id = _load_pack_for_episode_no(conn, project_id, source_episode_no)
    if source_pack is None:
        return False, f"来源集（第 {source_episode_no} 集）无已发布 episode_prep_pack，来源断链"

    scene_reference_id = scene.get("scene_reference_id")
    source_entry = next(
        (
            s for s in ((source_pack.get("asset_manifest") or {}).get("scenes")) or []
            if s.get("scene_reference_id") == scene_reference_id
        ),
        None,
    )
    if source_entry is None:
        return False, (
            f"来源集（第 {source_episode_no} 集）pack 中未找到 scene_reference_id="
            f"{scene_reference_id} 的绑定，来源断链"
        )

    display_name = str(scene.get("display_name") or "").strip()
    source_display_name = str(source_entry.get("display_name") or "").strip()
    if display_name != source_display_name:
        return False, (
            f"来源集（第 {source_episode_no} 集）对该资产的绑定名为「{source_display_name}」，"
            f"与本集声称继承的「{display_name}」不一致，来源断链"
        )

    source_chapter_indexes = list((source_pack.get("episode_scope") or {}).get("chapter_indexes") or [])
    source_text = _build_source_text(conn, project_id, source_chapter_indexes)
    source_segments = index_source_segments(source_text)
    source_hints = _load_true_name_hints(conn, source_artifact_id)

    ok, reason, _method = _verify_scene_entry(
        conn, project_id, source_episode_no, source_text, source_segments,
        source_hints, verify_hint, source_entry, depth=depth + 1,
    )
    if not ok:
        return False, f"来源集（第 {source_episode_no} 集）该绑定自身核验未通过：{reason}"
    return True, ""


def check_manifest_characters(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
    pack: dict[str, Any], source_text: str, segments: list[SourceSegment],
    hints: list[dict[str, Any]], verify_hint,
) -> tuple[list[Issue], int, int, int]:
    """A1。逐条委派给 _verify_character_entry（判定逻辑、method 分型、
    resolution_forward/alias_inherited 的核验规则都在那，这里只负责把结果
    拼成 Issue——见该函数 docstring 了解完整规则，包括：direct/alias 维持逐
    字标准；resolution/discovery/absorbed_speaker/candidate_verdict 用锚点
    核验；resolution_forward 核验前瞻章节；alias_inherited 递归核验来源集；
    不带 provenance 的旧包回退现行标准 + true_name_hints 例外；未识别 method
    一律 fail-closed。

    返回 (issues, checked, passed, legacy_fallback_count)：legacy_fallback_
    count 是走了"无 provenance"回退路径的条目数，调用方据此在报告里标注
    "无来源证明（旧版产物）"。
    """
    issues: list[Issue] = []
    entries = ((pack.get("asset_manifest") or {}).get("characters")) or []
    checked = len(entries)
    passed = 0
    legacy_fallback = 0
    for character in entries:
        display_name = str(character.get("display_name") or "").strip()
        aliases = [str(a).strip() for a in (character.get("aliases") or []) if str(a).strip()]
        portrait_id = character.get("portrait_id")

        ok, reason, method = _verify_character_entry(
            conn, project_id, episode_no, source_text, segments, hints, verify_hint, character,
        )
        if ok:
            passed += 1
            if method is None:
                legacy_fallback += 1
            continue
        if method is None:
            legacy_fallback += 1
            code = "A1_character_no_text_evidence"
            message = (
                f"角色「{display_name}」（portrait_id={portrait_id}）在本集原文中无任何文本依据"
                f"（{reason}；无来源证明（旧版产物），回退现行标准核验）"
            )
        elif method in TEXT_VERIFIED_METHODS:
            code = "A1_character_no_text_evidence"
            message = (
                f"角色「{display_name}」（portrait_id={portrait_id}，provenance.method={method}）"
                f"在本集原文中无任何文本依据（{reason}）"
            )
        elif method in ANCHOR_VERIFIED_METHODS or method == FORWARD_ANCHOR_METHOD:
            code = "A1_character_anchor_invalid"
            message = (
                f"角色「{display_name}」（portrait_id={portrait_id}，provenance.method={method}）"
                f"锚点核验失败：{reason}"
            )
        elif method == INHERITED_ALIAS_METHOD:
            code = "A1_character_inherited_alias_broken"
            message = (
                f"角色「{display_name}」（portrait_id={portrait_id}，provenance.method={method}）"
                f"来源链核验失败：{reason}"
            )
        else:
            code = "A1_character_unknown_provenance_method"
            message = f"角色「{display_name}」provenance.method={method!r} 不在已知枚举内，无法核验"
        issues.append(Issue(
            code=code, message=message,
            detail={
                "display_name": display_name, "aliases": aliases, "portrait_id": portrait_id,
                "event_ids": character.get("event_ids"),
                "provenance_method": method or "missing(legacy)", "reason": reason,
            },
        ))
    return issues, checked, passed, legacy_fallback


def check_manifest_scenes(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
    pack: dict[str, Any], source_text: str, segments: list[SourceSegment],
    hints: list[dict[str, Any]], verify_hint,
) -> tuple[list[Issue], int, int, int]:
    """A2，provenance 分型逻辑同 check_manifest_characters（委派给
    _verify_scene_entry）。当前已确认的 1.4.x/1.5.0 scene 条目没有 aliases
    字段，但 1.6.0 既然把 alias 定义为跟角色共享的 method 枚举值，
    _verify_scene_entry 对 scene.aliases 做了防御性兼容（字段缺失时按空列表
    处理，不报错、不改变现有 1.4.x/1.5.0 数据的判定结果）。"""
    issues: list[Issue] = []
    entries = ((pack.get("asset_manifest") or {}).get("scenes")) or []
    checked = len(entries)
    passed = 0
    legacy_fallback = 0
    for scene in entries:
        display_name = str(scene.get("display_name") or "").strip()
        scene_reference_id = scene.get("scene_reference_id")

        ok, reason, method = _verify_scene_entry(
            conn, project_id, episode_no, source_text, segments, hints, verify_hint, scene,
        )
        if ok:
            passed += 1
            if method is None:
                legacy_fallback += 1
            continue
        if method is None:
            legacy_fallback += 1
            code = "A2_scene_no_text_evidence"
            message = (
                f"场景「{display_name}」（scene_reference_id={scene_reference_id}）在本集原文中"
                f"无任何文本依据（{reason}；无来源证明（旧版产物），回退现行标准核验）"
            )
        elif method in TEXT_VERIFIED_METHODS:
            code = "A2_scene_no_text_evidence"
            message = (
                f"场景「{display_name}」（scene_reference_id={scene_reference_id}，"
                f"provenance.method={method}）在本集原文中无任何文本依据（{reason}）"
            )
        elif method in ANCHOR_VERIFIED_METHODS or method == FORWARD_ANCHOR_METHOD:
            code = "A2_scene_anchor_invalid"
            message = (
                f"场景「{display_name}」（scene_reference_id={scene_reference_id}，"
                f"provenance.method={method}）锚点核验失败：{reason}"
            )
        elif method == INHERITED_ALIAS_METHOD:
            code = "A2_scene_inherited_alias_broken"
            message = (
                f"场景「{display_name}」（scene_reference_id={scene_reference_id}，"
                f"provenance.method={method}）来源链核验失败：{reason}"
            )
        else:
            code = "A2_scene_unknown_provenance_method"
            message = f"场景「{display_name}」provenance.method={method!r} 不在已知枚举内，无法核验"
        issues.append(Issue(
            code=code, message=message,
            detail={
                "display_name": display_name, "scene_reference_id": scene_reference_id,
                "event_ids": scene.get("event_ids"),
                "provenance_method": method or "missing(legacy)", "reason": reason,
            },
        ))
    return issues, checked, passed, legacy_fallback


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


def _character_lookup_tables(
    pack: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """identity_id -> character entry, display_name -> character entry（供 A3
    的别名回退用；两个键各自可能重复但这里不需要处理歧义，manifest 本身按
    portrait_id 去重，identity_id/display_name 在同一份 manifest 内是唯一的）。"""
    characters = ((pack.get("asset_manifest") or {}).get("characters")) or []
    by_identity_id = {str(c["identity_id"]): c for c in characters if c.get("identity_id")}
    by_display_name = {str(c["display_name"]): c for c in characters if c.get("display_name")}
    return by_identity_id, by_display_name


def _speaker_text_or_alias_ok(
    speaker: str, character: dict[str, Any] | None, source_text: str,
) -> tuple[bool, list[str]]:
    """round-18 修正的核心判据，复用于旧包回退路径和 provenance
    direct/alias 两种方法：speaker 字符串本身逐字出现在原文，或（找得到对应
    角色时）该角色任一 aliases 逐字出现在原文。aliases 是 1.4.2 证据闸的产
    物，只有确实在本集原文里逐字出现过的称谓才会进入 aliases，所以两条路径
    是同一强度的证据。返回 (是否通过, 实际核验过的 aliases 列表)。"""
    if speaker and speaker in source_text:
        return True, []
    aliases = [str(a).strip() for a in (character.get("aliases") or [])] if character else []
    if any(a and a in source_text for a in aliases):
        return True, aliases
    return False, aliases


def check_key_line_speakers(
    conn: sqlite3.Connection, project_id: str, episode_no: int,
    pack: dict[str, Any], source_text: str, segments: list[SourceSegment], verify_hint,
) -> tuple[list[Issue], tuple[int, int, int], tuple[int, int]]:
    """A3：key_lines[].speaker 的文本依据判定。

    round-18 真实数据口径修正（仍然生效，见 _speaker_text_or_alias_ok）：
    1.5.0 起 speaker 字段并不保证是本集原文自己的称谓——事件链抽取提示词只对
    characters[].display_name 提出"必须逐字使用本段原文称谓"的硬性要求（见
    app/production/prep_pack.py 的 _extract_chunk 提示词），对 key_lines[].
    speaker 从未提出同等要求，模型经常直接写出已经正名解析后的真名（如"李
    富贵"），即便本集原文只用别名称呼过这个人（如"小胖子"）。判定因此改为：
    speaker 字符串本身逐字出现在原文，或 speaker 对应的 manifest.characters
    条目（优先用 key_line 自带的 speaker_ref 定位；没有该字段或未命中时退回
    按 speaker 精确匹配 display_name）的任一 aliases 逐字出现在原文，两者任
    一为真即通过。找不到对应角色条目（多半是 functional_extras 的 label，
    例如"围观弟子"）时没有别名可退，维持原判。

    1.6.0 provenance 升级（协调方指令，形状假定，见模块顶部常量注释）：
    key_line 若带 provenance（假定字段名 ``speaker_provenance``，缺失时兼容
    读取 ``provenance``——真实字段名以后端实物为准），按 method 分型，direct/
    alias 复用上面这条 round-18 判据；resolution/discovery/absorbed_speaker/
    candidate_verdict（ANCHOR_VERIFIED_METHODS 全体）改用 anchor_segments +
    anchor_phrase 的锚点核验（_verify_provenance_anchor）——"absorbed_
    speaker" 这个方法名本身指向的正是"speaker 是群演/功能性标签但确有一句
    话可锚定到具体原文"这种场景，锚点核验就是为它设计的；candidate_verdict
    （1.8.0）目前生成器只在 asset_manifest.characters[] 上产出，未见于
    key_lines，登记进这里是为了跟另外两处调用点保持同一套按 method 值分型
    （而非按字段位置分型）的统一处理，不代表已实际观测到这种数据。不带
    provenance 的 key_line（<=1.5.x 旧包）走 round-18 判据的旧路径，不受这
    次升级影响，只是在失败信息里加注"无来源证明（旧版产物）"。

    收尾单新增（real 1.6.0 live data 实测口径修正——协调方的两个新方法工单原
    文只提"该资产"/manifest 绑定，字面上没提 key_lines，但对本项目 proj_
    3ac0b627fa46 实际已发布 1.6.0 数据抽查发现 resolution_forward 确实被用
    在了 key_lines[].speaker_provenance 上（真实样本：EP2 的"小胖子"），说
    明这两个方法并不限于 A1/A2。据此把 resolution_forward/alias_inherited
    也接到 A3：resolution_forward 直接复用 _verify_provenance_forward_
    anchor（不关心是角色还是场景还是台词说话人，都是同一条"前瞻章节+短语"
    判据）；alias_inherited 先按 speaker_ref/display_name 解析出对应的
    manifest.characters 条目（跟上面 direct/alias 的角色解析复用同一段逻
    辑），再委派给 _verify_alias_inherited_character 做同样的来源集递归核
    验——解析不到对应角色（多半是 functional_extras 的群演 label，没有
    portrait_id 可继承）时直接判定失败，不是"当作旧版回退"。

    返回 (issues, (checked_text, passed_text, legacy_fallback_count),
    (checked_ref, passed_ref))。A3b（speaker_ref 名册核验）逻辑不变。
    """
    issues: list[Issue] = []
    checked_text = passed_text = 0
    legacy_fallback = 0
    checked_ref = passed_ref = 0
    roster = _build_roster_ref_set(pack)
    by_identity_id, by_display_name = _character_lookup_tables(pack)
    for event in pack.get("event_chain") or []:
        event_id = event.get("event_id")
        for key_line in event.get("key_lines") or []:
            speaker = str(key_line.get("speaker") or "").strip()
            if speaker:
                checked_text += 1
                speaker_ref = str(key_line.get("speaker_ref") or "")
                character = by_identity_id.get(speaker_ref) or by_display_name.get(speaker)
                raw_provenance = key_line.get("speaker_provenance")
                if raw_provenance is None:
                    raw_provenance = key_line.get("provenance")
                method, provenance = _dispatch_provenance(raw_provenance)

                if method is None:
                    legacy_fallback += 1
                    ok, aliases = _speaker_text_or_alias_ok(speaker, character, source_text)
                    if ok:
                        passed_text += 1
                    elif character is not None:
                        issues.append(Issue(
                            code="A3_speaker_no_text_evidence",
                            message=(
                                f"事件 {event_id} 台词说话人「{speaker}」未逐字出现在本集原文中，"
                                f"其对应角色「{character.get('display_name')}」登记的 {len(aliases)} "
                                "个 aliases 也均未逐字出现（无来源证明（旧版产物），回退现行标准核验）"
                            ),
                            detail={
                                "event_id": event_id, "speaker": speaker, "line": key_line.get("line"),
                                "checked_aliases": aliases, "provenance": "missing(legacy)",
                            },
                        ))
                    else:
                        issues.append(Issue(
                            code="A3_speaker_no_text_evidence",
                            message=(
                                f"事件 {event_id} 台词说话人「{speaker}」在本集原文中无任何文本依据"
                                "（无来源证明（旧版产物），回退现行标准核验）"
                            ),
                            detail={
                                "event_id": event_id, "speaker": speaker, "line": key_line.get("line"),
                                "provenance": "missing(legacy)",
                            },
                        ))
                elif method in TEXT_VERIFIED_METHODS:
                    ok, aliases = _speaker_text_or_alias_ok(speaker, character, source_text)
                    if ok:
                        passed_text += 1
                    else:
                        alias_note = (
                            f"，其对应角色登记的 {len(aliases)} 个 aliases 也均未逐字出现"
                            if aliases else ""
                        )
                        issues.append(Issue(
                            code="A3_speaker_no_text_evidence",
                            message=(
                                f"事件 {event_id} 台词说话人「{speaker}」（provenance.method="
                                f"{method}）未逐字出现在本集原文中{alias_note}"
                            ),
                            detail={
                                "event_id": event_id, "speaker": speaker, "line": key_line.get("line"),
                                "checked_aliases": aliases, "provenance_method": method,
                            },
                        ))
                elif method in ANCHOR_VERIFIED_METHODS:
                    ok, reason = _verify_provenance_anchor(segments, provenance)
                    if ok:
                        passed_text += 1
                    else:
                        issues.append(Issue(
                            code="A3_speaker_anchor_invalid",
                            message=(
                                f"事件 {event_id} 台词说话人「{speaker}」（provenance.method="
                                f"{method}）锚点核验失败：{reason}"
                            ),
                            detail={
                                "event_id": event_id, "speaker": speaker, "line": key_line.get("line"),
                                "provenance_method": method, "provenance": provenance, "reason": reason,
                            },
                        ))
                elif method == FORWARD_ANCHOR_METHOD:
                    ok, reason = _verify_provenance_forward_anchor(conn, project_id, provenance)
                    if ok:
                        passed_text += 1
                    else:
                        issues.append(Issue(
                            code="A3_speaker_anchor_invalid",
                            message=(
                                f"事件 {event_id} 台词说话人「{speaker}」（provenance.method="
                                f"{method}）锚点核验失败：{reason}"
                            ),
                            detail={
                                "event_id": event_id, "speaker": speaker, "line": key_line.get("line"),
                                "provenance_method": method, "provenance": provenance, "reason": reason,
                            },
                        ))
                elif method == INHERITED_ALIAS_METHOD:
                    if character is None:
                        ok, reason = False, (
                            "speaker 未能经 speaker_ref/名称匹配解析到 manifest.characters 任何"
                            "条目，没有 portrait_id 可继承，无法核验来源链"
                        )
                    else:
                        ok, reason = _verify_alias_inherited_character(
                            conn, project_id, episode_no, character, provenance, verify_hint, depth=0,
                        )
                    if ok:
                        passed_text += 1
                    else:
                        issues.append(Issue(
                            code="A3_speaker_inherited_alias_broken",
                            message=(
                                f"事件 {event_id} 台词说话人「{speaker}」（provenance.method="
                                f"{method}）来源链核验失败：{reason}"
                            ),
                            detail={
                                "event_id": event_id, "speaker": speaker, "line": key_line.get("line"),
                                "provenance_method": method, "provenance": provenance, "reason": reason,
                            },
                        ))
                else:
                    issues.append(Issue(
                        code="A3_speaker_unknown_provenance_method",
                        message=(
                            f"事件 {event_id} 台词说话人「{speaker}」provenance.method={method!r} "
                            "不在已知枚举内，无法核验"
                        ),
                        detail={"event_id": event_id, "speaker": speaker, "provenance": provenance},
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
    return issues, (checked_text, passed_text, legacy_fallback), (checked_ref, passed_ref)


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
# 契约版本闸（2.0.0 收口，见 app/production/prep_pack.py PREP_PACK_VERSION
# 上方 2.0.0 大注释与提交 48e01ff）：2.0.0 把剧本台改造成映射台，砍掉了
# event_chain/hook/cliffhanger——A3（key_lines[].speaker 文本依据）/A3b
# （speaker_ref 名册核验）/A4（source_evidence 引文抽查）三项检查的判据全部
# 挂在 event_chain 下的 key_lines/source_evidence 上。这两个字段在 2.0.0
# payload 里根本不存在，check_key_line_speakers/sample_source_evidence 对
# ``pack.get("event_chain") or []`` 取到空列表后循环体一次都不会进入，
# checked/passed 双双停在 0——不是"全部核验通过"，是"什么都没查"，但
# exit_code() 只看 a_issues/b_issues 是否非空，0 条差异等价于"全清"，会把
# 一份实际上只查了 A1/A2/B1/B2 的审计报告静默当成方向 A 全绿放行。
#
# A1（characters[].display_name/aliases）/A2（scenes[].display_name）依赖
# 的是 provenance.{method,anchor_segments,anchor_phrase}，这个形状 2.0.0
# 没有变，两者对 2.0.0 包仍然是真核验，不受影响，不在这道闸的管控范围内。
_A3_A4_EVENT_CHAIN_UNSUPPORTED_MAJOR = 2


def _prep_pack_major_version(version: str | None) -> int | None:
    """从形如 "2.0.0" 的 prep_pack_version 里取主版本号（第一个点号前的
    整数段）。解析失败（None/空串/非"数字.任意"形状）返回 None——调用方对
    None 一律按"版本不确定，不能断言它就是安全的旧契约"处理，不是当作已知
    安全版本静默放行。"""
    if not version:
        return None
    head = str(version).split(".", 1)[0].strip()
    return int(head) if head.isdigit() else None


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
    # 1.6.0 provenance.anchor_segments 引用的就是这份切分的 1-based 编号，
    # 必须跟生成器用的是同一个切分函数（见模块顶部 index_source_segments 的
    # import 注释），一次算好传给下面三个 A1/A2/A3 检查复用。
    segments = index_source_segments(source_text)
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

    issues, checked, passed, legacy = check_manifest_characters(
        conn, project_id, episode_no, pack, source_text, segments, hints, verify_hint,
    )
    result.a_issues.extend(issues)
    result.tallies["A1 角色绑定文本依据"] = CheckTally(checked, passed, legacy)

    issues, checked, passed, legacy = check_manifest_scenes(
        conn, project_id, episode_no, pack, source_text, segments, hints, verify_hint,
    )
    result.a_issues.extend(issues)
    result.tallies["A2 场景绑定文本依据"] = CheckTally(checked, passed, legacy)

    contract_major = _prep_pack_major_version(result.prep_pack_version)
    if contract_major is not None and contract_major >= _A3_A4_EVENT_CHAIN_UNSUPPORTED_MAJOR:
        # 不静默空转（见上方 _A3_A4_EVENT_CHAIN_UNSUPPORTED_MAJOR 大注释）：
        # 明确报一条方向 A 差异并让 exit_code() 非零退出，不给这两项留
        # "0/0 通过"这种看起来像"全部核验通过"的假象。
        result.a_issues.append(Issue(
            code="A0_unsupported_contract_version",
            message=(
                f"本工具尚不支持 prep_pack_version={result.prep_pack_version!r} 契约下的 "
                "A3（台词说话人文本依据）/A3b（speaker_ref 名册核验）/A4（source_evidence "
                "引文抽查）——2.0.0 起 event_chain/key_lines/source_evidence 已被撤销"
                "（改为 asset_manifest.appellation_map，定量节拍职责移交分镜台），这三项"
                "检查仍按旧字段读取，对本集永远得到 0 条检查、0 条差异。本集的方向 A 审计"
                "只覆盖了 A1/A2，不完整，不能当作这三项已经核验通过。"
            ),
            detail={"prep_pack_version": result.prep_pack_version},
        ))
    else:
        issues, (checked_text, passed_text, legacy), (checked_ref, passed_ref) = check_key_line_speakers(
            conn, project_id, episode_no, pack, source_text, segments, verify_hint,
        )
        result.a_issues.extend(issues)
        result.tallies["A3 台词说话人文本依据"] = CheckTally(checked_text, passed_text, legacy)
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
        note = (
            f"（其中 {tally.legacy_fallback} 条无来源证明（旧版产物），回退现行标准核验）"
            if tally.legacy_fallback > 0 else ""
        )
        lines.append(f"  {name}：{tally.passed}/{tally.checked} 通过{note}")
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
            name: {
                "checked": tally.checked, "passed": tally.passed,
                "legacy_fallback": tally.legacy_fallback,
            }
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
