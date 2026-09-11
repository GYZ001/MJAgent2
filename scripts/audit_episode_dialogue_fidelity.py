#!/usr/bin/env python3
"""下游台词保真外部复核：分镜台词 / 视频提示词 vs 小说原文。

与 ``scripts/episode_source_audit.py`` 分工：那一份查**映射台产物**
（episode_prep_pack 的人物/场景/事件链）对不对得上原文；本份查它下游的两层——
真正决定成片里角色开口说什么的那两层，此前没有任何对照工具。

三项检查，全部只读、零模型调用，判据从数据推导（禁止任何硬编码黑白名单）：

A · 分镜台词是否是原文说过的话
    ``delivery=spoken_dialogue`` 的每一句，必须是本集原文台词取值域的逐字连续
    子串。取值域直接复用台账自己的抽取器
    ``app.production.storyboard_dialogue_extract.extract_dialogue_targets``——
    它已经确定性地区分小说体（引号句）与剧本格式（``说话人（备注）：台词`` 行），
    三侧（账本 / 提示词规则 / 本复核）因此同源。取「原文说过的话」而不是「原文
    出现过的字」是必要的：叙述句的任意一截也是原文的子串，但那不是任何人说出口
    的话。2026-09-10 实测「我欲封天」前 10 集：259 条里 16 条不在取值域内。

B · 视频提示词是否逐字转发了分镜台词
    ``shot_versions.prompt_text`` 是真正提交给视频模型的那段文字。分镜台账里记着
    的每一句，必须逐字出现在它里面；对不上就是两种事故之一：整句在成片里没人说
    （台账说说了、画面上嘴在动却没词），或者被改写（实测把原著自带的错别字
    「整个外宗五人不知」"顺手改正"成「无人不敬佩」）。同日实测 250 条里 13 条。

C · 分镜对原文的覆盖率
    每个镜头的 ``source_excerpt`` 拼起来，覆盖了本集原文的百分之多少。剧情整段
    没被任何镜头认领，成片就会跳过它。

退出码：发现任何 A/B 项即 1，全绿 0。C 只报数不判分——覆盖率的阈值属于分镜台
自己的闸门，这里不重复立第二套标准。

用法：
    py scripts/audit_episode_dialogue_fidelity.py --project 我欲封天
    py scripts/audit_episode_dialogue_fidelity.py --project 我欲封天 --from 1 --to 10
    py scripts/audit_episode_dialogue_fidelity.py --db /path/manju.db --project proj_xxx
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402
from app.production.storyboard_dialogue_extract import extract_dialogue_targets  # noqa: E402
from app.source_excerpt import index_source_segments  # noqa: E402

_NON_CONTENT = re.compile(r"[\s“”\"'‘’「」『』。，、！？：；…—·,.!?:;()（）]")


def _condense(text: str) -> str:
    """压成纯内容串。与 app.textmatch.condense 同族，但连中文直角引号一起去掉——
    原文用「」、产物用“”是常见的排版差异，不该被读成台词改写。"""
    return _NON_CONTENT.sub("", text or "")


def readonly_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_project(conn: sqlite3.Connection, wanted: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT id,name FROM projects WHERE (id=? OR name=?) AND deleted_at IS NULL", (wanted, wanted),
    ).fetchone()
    if row is None:
        raise SystemExit(f"找不到项目：{wanted}")
    return row["id"], row["name"]


def speaker_names(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """人物谱正名与别名——``extract_dialogue_targets`` 判定「这段是不是剧本格式」
    要用它逐字比对行首说话人。取值来自本项目实际数据，不是名单常量。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    bible = json.loads(row["bible_json"]) if row and row["bible_json"] else {}
    names: set[str] = set()
    for character in bible.get("characters") or []:
        if name := str(character.get("name") or "").strip():
            names.add(name)
        for alias in character.get("aliases") or []:
            text = alias.get("text") if isinstance(alias, dict) else alias
            if text and str(text).strip():
                names.add(str(text).strip())
    return sorted(names)


def source_text(conn: sqlite3.Connection, project_id: str, chapter_indexes: list[int]) -> str:
    if not chapter_indexes:
        return ""
    placeholders = ",".join("?" * len(chapter_indexes))
    rows = conn.execute(
        f"SELECT content FROM chapters WHERE project_id=? AND idx IN ({placeholders}) ORDER BY idx",
        (project_id, *chapter_indexes),
    ).fetchall()
    return "\n\n".join(row["content"] or "" for row in rows)


def spoken_value_domain(text: str, names: list[str]) -> str:
    """本集原文里「有人说出口的话」拼成的取值域（已 condense）。"""
    segments = index_source_segments(text)
    quotes = extract_dialogue_targets(segments, set(), speaker_names=names)
    return "".join(_condense(quote.text) for quote in quotes)


def _shots_of(conn: sqlite3.Connection, episode_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT shot_no, dialogues, source_excerpt FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()


def _prompts_of(conn: sqlite3.Connection, episode_id: str) -> dict[int, str]:
    """每个镜号最新一版的视频提示词。没有版本的镜头不进字典（B 项跳过它）。"""
    prompts: dict[int, str] = {}
    for row in conn.execute(
        """SELECT s.shot_no, v.prompt_text FROM shot_versions v JOIN shots s ON s.id=v.shot_id
           WHERE s.episode_id=? AND v.prompt_text IS NOT NULL ORDER BY v.created_at""",
        (episode_id,),
    ):
        prompts[int(row["shot_no"])] = row["prompt_text"]
    return prompts


def _lines_of(shot: sqlite3.Row) -> list[dict]:
    raw = shot["dialogues"]
    try:
        items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return []
    return [item for item in items if isinstance(item, dict) and str(item.get("line") or "").strip()]


def check_spoken_provenance(shots: list[sqlite3.Row], domain: str) -> list[str]:
    """A 项：开口台词必须是原文取值域的逐字连续子串。画外音/旁白不在此列——
    它们是叙述者概括，本来就允许不逐字（自己那条可追溯规则另管）。"""
    findings: list[str] = []
    for shot in shots:
        for line in _lines_of(shot):
            if (line.get("delivery") or "spoken_dialogue") != "spoken_dialogue":
                continue
            needle = _condense(str(line["line"]))
            if needle and needle not in domain:
                findings.append(
                    f"镜头{shot['shot_no']} 台词『{str(line['line'])[:40]}』"
                    f"（说话人 {line.get('speaker')}）不是本集原文里说过的话"
                )
    return findings


def check_prompt_fidelity(shots: list[sqlite3.Row], prompts: dict[int, str]) -> list[str]:
    """B 项：分镜台账里的每一句都要逐字出现在最终提示词里。

    比对 ``_condense`` 之后的形态而不是裸串：speech 模板展开时会把一句台词按句读
    拆成相邻的几段引号——台账的「又扯淡了，会飞？那是传说中的仙人，谁信啊。」在
    提示词里是「“又扯淡了，会飞？”“那是传说中的仙人，谁信啊。”」，字一个没少、
    顺序也对，只是中间多了一对引号。裸串比对会把这个判成「整句丢失」（2026-09-11
    第 1 集重跑实测踩中两条），而误报会让整份复核报告失去可信度。condense 只去标点
    与引号，改字仍然查得出来：「五人不知」→「无人不敬佩」两侧字面不同，照样报。

    已知盲区：在原话**前后**加字查不出来（「你别走。」包含「别走。」）。改成比对
    提示词里的引号跨度可以覆盖它，但会被引号不配对的产物（第 6 集 ``说出："…”``
    前英文直引号、后中文右引号）打出另一类误报。取「不漏掉真实事故形态」：整句丢失
    与句中改写这两类都能抓。
    """
    findings: list[str] = []
    for shot in shots:
        prompt = prompts.get(int(shot["shot_no"]))
        if prompt is None:
            continue
        condensed_prompt = _condense(prompt)
        for line in _lines_of(shot):
            text = str(line["line"]).strip()
            if text and _condense(text) not in condensed_prompt:
                findings.append(
                    f"镜头{shot['shot_no']} 台词『{text[:40]}』没有逐字出现在视频提示词里"
                    "（整句丢失或被改写，成片里说的不是台账记的）"
                )
    return findings


def source_coverage(shots: list[sqlite3.Row], text: str) -> tuple[int, int]:
    """C 项：``source_excerpt`` 覆盖了本集原文多少字。返回 (已覆盖, 总字数)。"""
    condensed = _condense(text)
    covered = bytearray(len(condensed))
    for shot in shots:
        needle = _condense(shot["source_excerpt"] or "")
        if not needle:
            continue
        start = condensed.find(needle)
        if start >= 0:
            covered[start:start + len(needle)] = b"\x01" * len(needle)
    return sum(covered), len(condensed)


def audit_episode(conn: sqlite3.Connection, project_id: str, episode: sqlite3.Row, names: list[str]) -> list[str]:
    chapters = json.loads(episode["source_chapters"] or "[]")
    text = source_text(conn, project_id, [int(i) for i in chapters])
    shots = _shots_of(conn, episode["id"])
    if not shots:
        print(f"  第{episode['episode_no']}集：没有分镜，跳过")
        return []
    domain = spoken_value_domain(text, names)
    findings = check_spoken_provenance(shots, domain)
    findings.extend(check_prompt_fidelity(shots, _prompts_of(conn, episode["id"])))
    hit, total = source_coverage(shots, text)
    percent = hit * 100 // total if total else 0
    print(f"  第{episode['episode_no']}集：{len(shots)} 镜，原文 {total} 字，摘录覆盖 {percent}%，问题 {len(findings)} 条")
    for item in findings:
        print(f"      ✗ {item}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(config.DB_PATH))
    parser.add_argument("--project", required=True, help="项目名或项目 id")
    parser.add_argument("--from", dest="ep_from", type=int, default=1)
    parser.add_argument("--to", dest="ep_to", type=int, default=10**6)
    args = parser.parse_args()

    conn = readonly_conn(args.db)
    try:
        project_id, name = resolve_project(conn, args.project)
        names = speaker_names(conn, project_id)
        episodes = conn.execute(
            """SELECT id, episode_no, source_chapters FROM episodes
               WHERE project_id=? AND episode_no BETWEEN ? AND ? ORDER BY episode_no""",
            (project_id, args.ep_from, args.ep_to),
        ).fetchall()
        print(f"项目：{name}（{project_id}），集号 EP{args.ep_from}-EP{args.ep_to}，人物谱称谓 {len(names)} 个")
        findings: list[str] = []
        for episode in episodes:
            findings.extend(audit_episode(conn, project_id, episode, names))
        print()
        if findings:
            print(f"共 {len(findings)} 条台词保真问题——成片里说出口的话与原文/台账对不上。")
            return 1
        print("台词保真检查全绿：开口台词都来自原文，提示词逐字转发了台账。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
