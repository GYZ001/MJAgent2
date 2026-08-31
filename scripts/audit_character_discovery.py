"""角色卡按需发现审计：人物谱现状 + 遗漏候选 + 疑似重复 + 分集发现曲线。

背景：架构从"预先定全书名单"转向"每集读原文、谁出场就建谁的卡"。这个架构唯一
真正的判据是拿真实项目对着原文逐个核——本工具把此前每次都靠主会话临时手写
sqlite 查询的过程固化成可重复脚本，回答四个问题：

  1. 谁进来了：人物谱现有角色、别名、定妆照状态。
  2. 谁可能被漏了：原文里高频、具备称谓形态、但不在人物谱/别名里的候选。
  3. 有没有同一个人两张卡：别名跨角色碰撞、name 撞 alias、疑似同人共现信号。
  4. 分集发现有没有在跑：随集号推进新增了多少角色卡（新架构下应单调增长）。

铁律（CLAUDE.md「禁止黑白名单与枚举穷举」）：问题 2/3 不维护任何角色名/人名
词表，判据全部从本次读到的原文与本项目自己的人物谱数据推导，能否在任意新书上
直接跑是唯一的设计约束。做不到"干净判据"的地方（尤其问题 2/3），本工具选择把
结果标成"供人工判断的原始信号"，不假装是结论——详见各函数 docstring 里记录的
真实反证过程（哪些看起来合理的启发式被真实数据推翻了）。

全部只读：``--db`` 默认指向 ``app.config.DB_PATH``（生产库），本工具只执行
SELECT，不做任何写入。

用法：
    .venv/bin/python scripts/audit_character_discovery.py [--project proj_xxx] [--db 路径]
    .venv/bin/python scripts/audit_character_discovery.py --min-freq 5 --top-missed 15
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import DB_PATH  # noqa: E402

# 问题 2 判据：对话引导语位置（称谓形态的结构信号，不是人名词表）。中文网文最
# 通行的标注惯例是"引号收尾 + 称呼/称呼+副词 + 道 + 标点"（如 ``"……”孟浩淡淡
# 道，``）——「道」是语法功能字，不属于任何具体人名，用它做锚点等价于用逗号/
# 引号做锚点，不是名单。
ATTRIB_RE = re.compile(r'[”"]([一-鿿]{2,6})道[，,。.！!？?：:“"]')
PREFIX_LENGTHS = (2, 3, 4)
# 称呼常带副词尾巴（"孟浩淡淡道"里的"淡淡"），合并步骤按频次保留量决定要不要
# 把 2 字词根延伸成 3/4 字——样本量太小才会把副词一起吞进候选词，先要求词根
# 出现次数达到这个下限，减少这类噪声。
CONSOLIDATE_MIN_BASE = 3
CONSOLIDATE_KEEP_RATIO = 0.6

# 问题 3 判据：紧邻共现比例。经验证据见 find_cooccurrence_suspects docstring。
ADJACENCY_WINDOW = 6
ADJACENCY_RATIO_THRESHOLD = 0.4
ADJACENCY_MIN_COUNT = 5


def readonly_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---- 数据加载 ----
def load_bible(row: sqlite3.Row) -> dict:
    raw = row["bible_json"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def alias_texts(character: dict) -> list[str]:
    out: list[str] = []
    for item in character.get("aliases") or []:
        text = item.get("text") if isinstance(item, dict) else item
        text = (text or "").strip() if isinstance(text, str) else ""
        if text:
            out.append(text)
    return out


def load_chapters(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT idx, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,),
    ).fetchall()


def full_text_of(chapters: list[sqlite3.Row]) -> str:
    return "\n\n".join(r["content"] or "" for r in chapters)


# ---- 问题 1：谁进来了——人物谱 + 定妆盘点 ----
@dataclass
class RosterRow:
    name: str
    aliases: list[str]
    current_portrait_status: str | None  # None = 当前集数区间没有生效定妆
    version_count: int  # ep_start>=0 的定妆行数（正常应为 1；>1 需要人工看是否是重画）
    legacy_slot_count: int  # ep_start<0 的历史槽位数（promote_staged_initial_portrait 的作废槽位）


def build_roster(conn: sqlite3.Connection, project_id: str, characters: list[dict], asof_ep: int) -> list[RosterRow]:
    """asof_ep 用项目当前 episodes 总数——取"当前生效"定妆段。

    ep_start>=0 是硬约束（CLAUDE.md 已知坑）：负数是 promote_staged_initial_portrait
    压进去的已作废历史槽位（从 -1 递减、ep_end=0），必须先过滤掉，否则会把重做过
    定妆照的角色错算成"有多张卡"。
    """
    rows = conn.execute(
        "SELECT character_name, ep_start, ep_end, pack_status FROM character_portraits "
        "WHERE project_id=? AND ep_start>=0 ORDER BY character_name, ep_start", (project_id,),
    ).fetchall()
    by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_name[r["character_name"]].append(r)
    legacy = conn.execute(
        "SELECT character_name, COUNT(*) c FROM character_portraits "
        "WHERE project_id=? AND ep_start<0 GROUP BY character_name", (project_id,),
    ).fetchall()
    legacy_count = {r["character_name"]: r["c"] for r in legacy}

    out: list[RosterRow] = []
    for character in characters:
        name = character.get("name") or "<无名>"
        versions = by_name.get(name, [])
        current = None
        for v in versions:
            if v["ep_start"] <= asof_ep and (v["ep_end"] is None or v["ep_end"] >= asof_ep):
                current = v["pack_status"]
        out.append(RosterRow(
            name=name, aliases=alias_texts(character), current_portrait_status=current,
            version_count=len(versions), legacy_slot_count=legacy_count.get(name, 0),
        ))
    return out


# ---- 问题 2：谁可能被漏了——高频 + 称谓形态候选 ----
def extract_attribution_spans(chapters: list[sqlite3.Row]) -> tuple[Counter, dict[str, set[int]]]:
    counts: Counter = Counter()
    chapters_seen: dict[str, set[int]] = defaultdict(set)
    for row in chapters:
        text = row["content"] or ""
        for m in ATTRIB_RE.finditer(text):
            span = m.group(1)
            counts[span] += 1
            chapters_seen[span].add(row["idx"])
    return counts, chapters_seen


def consolidate_candidates(
    raw_counts: Counter, raw_chapters: dict[str, set[int]],
) -> tuple[Counter, dict[str, set[int]]]:
    """把同词根的 2/3/4 字候选合成一个最终候选。

    先把每个原始 span 的 2/3/4 字前缀各自累计频次；再从 2 字词根出发，只有词根
    出现次数达到 CONSOLIDATE_MIN_BASE、且延伸后的前缀仍保留 CONSOLIDATE_KEEP_
    RATIO 以上的频次，才认为延伸出的部分是词根本身而非副词，采用更长的那个。
    样本量太小时不延伸，避免把"某某笑着"当成候选词（真实数据出现过"韩贝笑着"
    这种因样本只有 2 次、任意多出的字都能凑够比例的假延伸）。
    """
    prefix_counts: Counter = Counter()
    prefix_chapters: dict[str, set[int]] = defaultdict(set)
    for span, c in raw_counts.items():
        chs = raw_chapters[span]
        for length in PREFIX_LENGTHS:
            if len(span) >= length:
                p = span[:length]
                prefix_counts[p] += c
                prefix_chapters[p] |= chs

    final_counts: Counter = Counter()
    final_chapters: dict[str, set[int]] = {}
    roots = {p for p in prefix_counts if len(p) == 2}
    for root in roots:
        base = prefix_counts[root]
        best_token, best_count = root, base
        if base >= CONSOLIDATE_MIN_BASE:
            # 延伸后频次至多与词根持平，不会更高，不能要求"严格大于"（那样
            # 永远不延伸）；改成从最长尝试起，保留住足够比例频次就采用。
            for length in (4, 3):
                at_length = [(cand, c) for cand, c in prefix_counts.items()
                             if len(cand) == length and cand.startswith(root)]
                if not at_length:
                    continue
                cand, c = max(at_length, key=lambda pair: pair[1])
                if c >= CONSOLIDATE_KEEP_RATIO * base:
                    best_token, best_count = cand, c
                    break
        final_counts[best_token] = best_count
        final_chapters[best_token] = prefix_chapters[best_token]
    return final_counts, final_chapters


@dataclass
class MissedCandidate:
    token: str
    freq: int
    chapter_count: int
    solo_chapters: int  # 出现该候选、但同章没有任何已知角色称呼出现的章节数


def find_missed_candidates(
    chapters: list[sqlite3.Row], characters: list[dict], min_freq: int, min_chapters: int, top_n: int,
) -> list[MissedCandidate]:
    known = {c.get("name", "").strip() for c in characters if c.get("name")}
    for c in characters:
        known.update(alias_texts(c))
    known.discard("")

    raw_counts, raw_chapters = extract_attribution_spans(chapters)
    counts, cand_chapters = consolidate_candidates(raw_counts, raw_chapters)

    known_chapters: set[int] = set()
    if known:
        pat = re.compile("|".join(re.escape(k) for k in sorted(known, key=len, reverse=True)))
        for row in chapters:
            if pat.search(row["content"] or ""):
                known_chapters.add(row["idx"])

    out: list[MissedCandidate] = []
    for token, freq in counts.items():
        if token in known or freq < min_freq:
            continue
        chs = cand_chapters[token]
        if len(chs) < min_chapters:
            continue
        out.append(MissedCandidate(token, freq, len(chs), len(chs - known_chapters)))
    out.sort(key=lambda m: (-m.freq, m.token))
    return out[:top_n]


# ---- 问题 3：有没有同一个人两张卡 ----
def find_alias_collisions(characters: list[dict]) -> list[str]:
    """同一个别名字符串被登记在两个不同角色名下——结构性碰撞，与频次无关。"""
    owners: dict[str, list[str]] = defaultdict(list)
    for c in characters:
        name = c.get("name") or "<无名>"
        for alias in alias_texts(c):
            owners[alias].append(name)
    return [
        f"别名「{alias}」同时登记在 {owner_list} 名下，可能是同一个人被拆成了多张卡"
        for alias, owner_list in owners.items() if len(set(owner_list)) > 1
    ]


def find_name_alias_overlap(characters: list[dict]) -> list[str]:
    """某角色的 name 逐字等于另一角色的 alias.text——最直接的"其实是同一个人"信号。"""
    names = {c.get("name") or "<无名>" for c in characters}
    issues = []
    for c in characters:
        cname = c.get("name") or "<无名>"
        for alias in alias_texts(c):
            if alias in names and alias != cname:
                issues.append(f"「{cname}」的别名「{alias}」正是另一张卡「{alias}」的 name，两卡疑似同一人")
    return issues


def find_cooccurrence_suspects(
    chapters: list[sqlite3.Row], characters: list[dict], window: int, ratio_threshold: float, min_count: int,
) -> list[str]:
    """紧邻共现信号（弱信号，必须人工核实，不构成结论）。

    直觉上"两个称呼经常同段出现"更像是两个人在互动（正常），不是同一个人。真正
    对同人有区分力的是"紧邻"（同句/极短窗口内前后脚出现，如"小胖子（李富贵）"
    这种同位语式写法），本函数只统计这种紧邻拼接，不是整章共现。

    已验证的反证（《我欲封天》真实数据实测，不是写死进代码的名单）：整章共现会让
    几乎每一对主要角色都命中（角色少、章节多时必然如此），毫无区分力，故不采用；
    紧邻窗口下真正的已知别名对（"小胖子"/"李富贵"）比例只有 0.089，反而比两个
    明显不同人的常规互动对（"孟浩"/"许清"，男女主角，0.191）更低——说明比例高
    不能证明是别名关系，只是弱信号，阈值须设在正常互动对上界之上留足余量。
    """
    known = {c.get("name") or "<无名>": [c.get("name") or ""] + alias_texts(c) for c in characters}
    full_text = full_text_of(chapters)
    freq_cache: dict[str, int] = {}

    def freq(term: str) -> int:
        if term not in freq_cache:
            freq_cache[term] = len(re.findall(re.escape(term), full_text)) if term else 0
        return freq_cache[term]

    def adjacency(term_a: str, term_b: str) -> int:
        cnt = 0
        for m in re.finditer(re.escape(term_a), full_text):
            if term_b in full_text[m.end():m.end() + window]:
                cnt += 1
            if term_b in full_text[max(0, m.start() - window):m.start()]:
                cnt += 1
        return cnt

    linked: set[tuple[str, str]] = set()
    for name, terms in known.items():
        for t in terms:
            linked.add((name, t))
            linked.add((t, name))

    issues = []
    names = list(known)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if (a, b) in linked or (b, a) in linked:
                continue
            adj = adjacency(a, b)
            if adj < min_count:
                continue
            ratio = adj / max(1, min(freq(a), freq(b)))
            if ratio >= ratio_threshold:
                issues.append(
                    f"「{a}」与「{b}」紧邻共现 {adj} 次（比例 {ratio:.2f}），互不为别名——"
                    f"弱信号，需人工核实是否其实是同一人"
                )
    return issues


# ---- 问题 4：分集发现曲线 ----
def build_growth_curve(conn: sqlite3.Connection, project_id: str, episode_count: int) -> list[tuple[int, int, int, int]]:
    """按 ep_start（ep_start>=0）分桶统计新增角色卡数，桶数动态适配集数规模。"""
    rows = conn.execute(
        "SELECT DISTINCT character_name, ep_start FROM character_portraits "
        "WHERE project_id=? AND ep_start>=0", (project_id,),
    ).fetchall()
    first_seen: dict[str, int] = {}
    for r in rows:
        name = r["character_name"]
        ep = r["ep_start"]
        if name not in first_seen or ep < first_seen[name]:
            first_seen[name] = ep

    n_buckets = min(20, max(1, episode_count))
    bucket_size = max(1, -(-episode_count // n_buckets))
    buckets = [[b * bucket_size + 1, min(episode_count, (b + 1) * bucket_size), 0] for b in range(n_buckets)]
    for ep in first_seen.values():
        idx = min(n_buckets - 1, max(0, (ep - 1) // bucket_size))
        buckets[idx][2] += 1

    out = []
    cumulative = 0
    for start, end, new_count in buckets:
        cumulative += new_count
        out.append((start, end, new_count, cumulative))
    return out


def count_discovery_calls(conn: sqlite3.Connection, project_id: str) -> int:
    """assess_new_character 阶段的 provider_calls 次数——发现通道有没有真的在跑。

    stage 记在 meta JSON 里（不是独立列），SQLite 若带 JSON1 扩展可用
    json_extract 直接查；没有就退化成逐行 Python 解析，两条路径结果一致。
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM provider_calls WHERE project_id=? "
            "AND json_extract(meta,'$.stage')='assess_new_character'", (project_id,),
        ).fetchone()
        return int(row["c"])
    except sqlite3.OperationalError:
        pass
    count = 0
    for row in conn.execute("SELECT meta FROM provider_calls WHERE project_id=?", (project_id,)):
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        if meta.get("stage") == "assess_new_character":
            count += 1
    return count


def episode_status_breakdown(conn: sqlite3.Connection, project_id: str) -> Counter:
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM episodes WHERE project_id=? GROUP BY status", (project_id,),
    ).fetchall()
    return Counter({r["status"]: r["c"] for r in rows})


# ---- 输出渲染 ----
def _section(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * 4}")


def render_roster(roster: list[RosterRow]) -> None:
    _section("问题 1：谁进来了（人物谱 + 定妆）")
    if not roster:
        print("  人物谱为空")
        return
    for r in roster:
        alias_str = "、".join(r.aliases) if r.aliases else "（无）"
        status = r.current_portrait_status or "缺失"
        flag = ""
        if r.version_count > 1:
            flag += f"  ⚠ 当前区间内有 {r.version_count} 条定妆记录，需人工确认是否为重画版本化"
        if r.legacy_slot_count:
            flag += f"  （另有 {r.legacy_slot_count} 个已作废历史槽位，已按 ep_start>=0 排除）"
        print(f"  - {r.name:<10} 别名: {alias_str:<20} 定妆状态: {status}{flag}")


def render_missed(candidates: list[MissedCandidate], min_freq: int, min_chapters: int) -> None:
    _section(f"问题 2：谁可能被漏了（原始信号，供人工判断；freq>={min_freq}, 覆盖章数>={min_chapters}）")
    print("  判据：对话引导语位置（\"……”称呼+道\"）出现的称谓形态候选，逐字排除已在")
    print("  人物谱/别名里的词。不是结论——通用角色描述（老者/男子/少女之类）也会被")
    print("  当成候选列出，需要人工用常识过滤，脚本不做人名词表式的自动排除。")
    if not candidates:
        print("  （无候选，或原文太短不足以形成稳定信号）")
        return
    print(f"  {'候选':<10}{'称谓频次':>8}{'覆盖章数':>8}{'其中无已知角色同章':>16}")
    for m in candidates:
        print(f"  {m.token:<10}{m.freq:>8}{m.chapter_count:>8}{m.solo_chapters:>16}")


def render_duplicates(alias_issues: list[str], overlap_issues: list[str], cooccur_issues: list[str]) -> None:
    _section("问题 3：有没有同一个人两张卡")
    all_issues = alias_issues + overlap_issues
    if not all_issues:
        print("  别名碰撞 / name-alias 撞车：未发现")
    for line in all_issues:
        print(f"  - [确定性] {line}")
    print("  疑似同人（紧邻共现弱信号，务必人工核实，可能包含正常互动角色对）：")
    if not cooccur_issues:
        print("    未触发（不代表没有，只代表紧邻共现比例没有超过阈值）")
    for line in cooccur_issues:
        print(f"    - {line}")


def render_growth(curve: list[tuple[int, int, int, int]], discovery_calls: int, status_counts: Counter) -> None:
    _section("问题 4：分集发现有没有在跑")
    print(f"  assess_new_character 调用次数（provider_calls）：{discovery_calls}")
    status_str = "、".join(f"{k}:{v}" for k, v in status_counts.most_common())
    print(f"  集状态分布：{status_str or '（无集）'}")
    print(f"  {'集号区间':<14}{'新增':>6}{'累计':>6}")
    for start, end, new_count, cumulative in curve:
        bar = "#" * new_count
        print(f"  {start:>5}-{end:<7}{new_count:>6}{cumulative:>6}  {bar}")
    if len(curve) > 1 and all(c[2] == 0 for c in curve[1:]):
        print("  ⚠ 除首个区间外全无新增——人物谱可能是一次性建完的，分集发现通道未在跑")


# ---- 主流程 ----
def audit_project(conn: sqlite3.Connection, project: sqlite3.Row, args: argparse.Namespace) -> None:
    project_id = project["id"]
    print(f"\n{'#' * 60}\n项目：{project['name']}（{project_id}）\n{'#' * 60}")
    bible = load_bible(project)
    characters = bible.get("characters") or []
    episode_count = conn.execute(
        "SELECT COUNT(*) c FROM episodes WHERE project_id=?", (project_id,),
    ).fetchone()["c"]
    chapters = load_chapters(conn, project_id)

    roster = build_roster(conn, project_id, characters, asof_ep=max(1, episode_count))
    render_roster(roster)

    missed = find_missed_candidates(chapters, characters, args.min_freq, args.min_chapters, args.top_missed)
    render_missed(missed, args.min_freq, args.min_chapters)

    alias_issues = find_alias_collisions(characters)
    overlap_issues = find_name_alias_overlap(characters)
    cooccur_issues = find_cooccurrence_suspects(
        chapters, characters, ADJACENCY_WINDOW, ADJACENCY_RATIO_THRESHOLD, ADJACENCY_MIN_COUNT,
    )
    render_duplicates(alias_issues, overlap_issues, cooccur_issues)

    curve = build_growth_curve(conn, project_id, max(1, episode_count))
    discovery_calls = count_discovery_calls(conn, project_id)
    status_counts = episode_status_breakdown(conn, project_id)
    render_growth(curve, discovery_calls, status_counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=None, help="只审计这个 project_id")
    parser.add_argument("--db", default=None, help="覆盖数据库路径，默认 app.config.DB_PATH")
    parser.add_argument("--min-freq", type=int, default=3, help="问题2候选最低称谓频次")
    parser.add_argument("--min-chapters", type=int, default=2, help="问题2候选最低覆盖章数")
    parser.add_argument("--top-missed", type=int, default=25, help="问题2最多展示多少条候选")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else DB_PATH
    if not db_path.exists():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 2
    conn = readonly_connection(db_path)
    sql = "SELECT * FROM projects WHERE deleted_at IS NULL"
    params: tuple = ()
    if args.project:
        sql += " AND id=?"
        params = (args.project,)
    projects = conn.execute(sql, params).fetchall()
    if not projects:
        print("没有符合条件的项目")
        return 1
    for project in projects:
        audit_project(conn, project, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
