#!/usr/bin/env python3
"""别名排他性判据（任务二）校准夹具——只读、不写库、不发起任何迁移。

背景（真实事故）：`app/stages.py:_alias_verdict_call` 的任务二提示词曾用"换成任何
符合同一类特征的陌生人，是否都不会被这样称呼"这句"陌生人测试"来定义排他性——这要求
**绝对全局唯一性**（世上另一个叫"李富贵"的人当然也叫李富贵），按字面理解几乎没有任何
称谓能通过。对《我欲封天》18 条真实别名跑真实模型，15 条被误判为非排他，包括"李富贵"
（角色本名）、"金袍老者"（类别名词+独有外观限定）等明显含个体化成分的称谓。模型是忠实
执行了这句话，不是模型能力问题（见 CLAUDE.md "模型答不出来时，先查它有没有收到标准
答案"）。修复见 `_alias_verdict_call` 任务二措辞：把"陌生人测试"换成"限定成分拆解
测试"——问"能不能从称谓字面里拆出至少一个只属于这一个人的限定成分（姓氏/本名/绰号/
独有外观/排他性头衔）"，而不是"能不能证明世界上不存在第二个符合这个称谓的人"。

本脚本做什么：对一份人工标定的真实别名清单（取自《我欲封天》项目库中已登记的
CharacterAlias 证据——真实章节原文、真实角色、真实候选人名单，不是编造的测试数据），
逐条真实调用 `_alias_verdict_call`（与 `_alias_evidence_resolution` 内部使用的是
同一个函数、同一套 dossier/candidates 构造逻辑），读取模型返回的 `is_exclusive_
reference`，与人工标定的期望值比对，打出命中率。这样以后再改任务二措辞，不用靠人肉
看，直接跑一遍这个脚本就知道有没有把明确案例改错。

标定集选取依据（每条都在报告/代码注释里给理由，不是拍脑袋）：
- 应判排他（含个体化成分）：李富贵（本名）、许师姐/陈师兄/赵师兄（姓氏+身份称谓）、
  赵武刚师兄/王腾飞师兄/韩宗师兄（姓名+身份称谓）、虎爷爷（本人绰号"虎"+身份称谓）、
  金袍老者/上官老者（独有外观/姓氏+类别名词）、低阶弟子第一人（排他性排行"第一人"+
  类别名词）、掌门（同一时间同一宗门只有一位掌门，是排他性头衔，与"排他性头衔"这一
  个体化成分类型定义直接对应）、老祖（本章语境下特指靠山宗那位开山祖师，同一时间
  该称谓在候选范围内只对应一个人，与"掌门"同一判据）。
- 应判非排他（纯类别描述，任何同类成员都适用）：大汉、少年、老者、胖子——不含姓氏、
  本名、绰号、独有外观或排他性头衔，换成候选人名单之外任何符合同一类特征的人都可能
  被这样称呼。

与 tests/test_alias_exclusivity.py 的分工（交叉引用）：该文件里
`test_qualified_aliases_are_not_regressed_by_exclusivity_filtering` 一组测试直接
构造 `CharacterAlias(...)`（`is_exclusive` 用 schema 默认值 True），验的是"registry
折叠逻辑对给定的 is_exclusive 取值处理是否正确"——它从不调用 `_alias_verdict_call`，
天然拦不住"模型判据措辞本身把标准拔到不可能达到的高度"这类问题（这正是这次真实事故
没被那组测试拦住的原因）。本脚本才是"判据措辞是否合理"的校准夹具，两者验的是不同的
东西，缺一不可，不要用其中一个替代另一个。

用法：
    .venv/bin/python scripts/calibrate_alias_exclusivity.py
    .venv/bin/python scripts/calibrate_alias_exclusivity.py --project proj_195be7df1fd6

只读：只用只读 sqlite 连接（mode=ro）取 bible_json 与 chapters，不写任何表、不调用
`backfill_character_aliases.py`。真实调用模型（`model_gateway.chat_structured`），
串行执行（不并发），避免与同时在跑的真实回归抢配额。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.schemas import Bible  # noqa: E402
from app import stages  # noqa: E402

DEFAULT_PROJECT_NAME = "我欲封天"


# ---------- 标定集：(别名文本, 期望 is_exclusive_reference, 理由) ----------
# 理由字符串会出现在报告输出里，不是纯装饰——报告里出现分歧时，理由就是复核的依据。
CALIBRATION_SET: list[tuple[str, bool, str]] = [
    ("李富贵", True, "本名——本名本身即为限定成分，不含可剥离的类别词"),
    ("许师姐", True, "姓氏+身份称谓——姓氏是限定成分"),
    ("陈师兄", True, "姓氏+身份称谓——姓氏是限定成分"),
    ("赵师兄", True, "姓氏+身份称谓——姓氏是限定成分"),
    ("赵武刚师兄", True, "全名+身份称谓——全名是限定成分"),
    ("王腾飞师兄", True, "全名+身份称谓——全名是限定成分"),
    ("韩宗师兄", True, "全名+身份称谓——全名是限定成分"),
    ("虎爷爷", True, "本人绰号'虎'+身份称谓——绰号是限定成分"),
    ("金袍老者", True, "独有外观'金袍'+类别词'老者'——外观细节是限定成分"),
    ("上官老者", True, "姓氏'上官'+类别词'老者'——姓氏是限定成分"),
    ("低阶弟子第一人", True, "排他性排行'第一人'+类别词'低阶弟子'——排行是限定成分"),
    (
        "掌门", True,
        "职位头衔——同一时间同一宗门只有一位掌门，是'排他性头衔'这一限定成分类型的"
        "典型例子（换个宗门/换个时代确实是别人，但本判据只问'字面是否带限定成分'，"
        "不是'全宇宙全历史唯一'，与'李富贵'换个故事也可能撞名同一个道理——见任务二"
        "措辞修复本身）",
    ),
    (
        "老祖", True,
        "尊称头衔——本章语境下特指候选范围内某一位开山祖师级人物，同一时间该称谓"
        "在候选范围内只对应一个人，与'掌门'同一判据（头衔本身的排他性由'当前场景下"
        "只有一位符合'决定，不要求脱离场景后仍然全局唯一）",
    ),
    ("大汉", False, "纯类别词——仅描述体型/性别，无姓氏/绰号/外观/头衔等限定成分"),
    ("少年", False, "纯类别词——仅描述年龄段/性别，无限定成分"),
    ("老者", False, "纯类别词——仅描述年龄段/性别，无限定成分"),
    ("胖子", False, "纯类别词——仅描述体型，无限定成分"),
]


def _readonly_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_project_id(conn: sqlite3.Connection, project: str | None) -> str:
    if project:
        row = conn.execute(
            "SELECT id FROM projects WHERE id=? OR name=?", (project, project),
        ).fetchone()
        if not row:
            raise SystemExit(f"项目不存在: {project}")
        return row["id"]
    row = conn.execute(
        "SELECT id FROM projects WHERE name=?", (DEFAULT_PROJECT_NAME,),
    ).fetchone()
    if not row:
        raise SystemExit(f"默认项目不存在: {DEFAULT_PROJECT_NAME}（用 --project 指定）")
    return row["id"]


def _load_bible_and_chapters(
    conn: sqlite3.Connection, project_id: str,
) -> tuple[Bible, list[dict]]:
    proj = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if not proj or not proj["bible_json"]:
        raise SystemExit(f"项目 {project_id} 尚无人物谱（bible_json 为空）")
    bible = Bible.model_validate(json.loads(proj["bible_json"]))
    chapters = conn.execute(
        "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx",
        (project_id,),
    ).fetchall()
    return bible, [dict(c) for c in chapters]


def _find_alias_evidence(bible: Bible, alias_text: str) -> tuple[str, int, str] | None:
    """在真实人物谱里找到这条别名当初登记时留下的真实证据锚点
    （true_name, evidence_chapter_index, evidence_quote）。找不到说明标定集里的
    这条别名当前库里没有真实数据支撑，调用方应跳过并如实报告，不得编造。"""
    for character in bible.characters:
        for alias in character.aliases:
            if alias.text == alias_text:
                return character.name, alias.evidence_chapter_index, alias.evidence_quote
    return None


async def _judge_one(
    bible: Bible,
    chapters_by_idx: dict[int, str],
    roster: dict[str, list[str]],
    project_id: str,
    alias_text: str,
    true_name: str,
    chapter_idx: int,
    quote: str,
) -> dict[str, Any]:
    """真实调用一次 `_alias_verdict_call`（与生产路径 `_alias_evidence_resolution`
    内部用的是同一个函数、同一套 dossier/candidates 构造逻辑），只读取
    `is_exclusive_reference`——本夹具校准的是任务二本身，不是任务一的候选判别，所以
    不经过 `_alias_evidence_resolution` 的 accepted 门槛（任务一选错人不代表任务二
    的排他性措辞有问题，两件事独立评估，与 prompt 里"两个判断互不预设对方的答案"
    是同一个道理）。仍然把任务一的结果一并打印出来供人工参考。"""
    chapter_text = chapters_by_idx.get(chapter_idx, "")
    if not chapter_text:
        return {"alias": alias_text, "error": f"章节 {chapter_idx} 原文缺失"}
    candidates = stages._alias_verdict_candidates(chapter_text, roster)
    if not candidates:
        return {"alias": alias_text, "error": "候选集为空（结构性异常，不应发生）"}
    anchor_texts = {true_name}
    dossier_anchor_texts = anchor_texts | {
        form for name in candidates for form in roster.get(name, [])
    }
    dossier = stages._alias_verdict_dossier(
        chapter_idx, chapter_text, alias_text, dossier_anchor_texts,
    )
    if not dossier:
        return {"alias": alias_text, "error": "卷宗为空（结构性异常，不应发生）"}
    try:
        cognition_card = stages.build_chapter_cognition_card(
            bible, chapters_by_idx, chapter_idx, character_names=candidates,
        )
    except Exception:  # noqa: BLE001 - 认知卡是辅助信息，组装失败不阻塞校准本身
        cognition_card = None
    response = await stages._alias_verdict_call(
        alias=alias_text, true_name=true_name, dossier=dossier,
        candidates=candidates, project_id=project_id, cognition_card=cognition_card,
    )
    task1_pinned = stages._alias_verdict_pin_segment(
        dossier, response.supporting_segment_index,
    )
    return {
        "alias": alias_text,
        "true_name": true_name,
        "is_exclusive_reference": response.is_exclusive_reference,
        "task1_selected_candidate": response.selected_candidate,
        "task1_pass": (
            response.selected_candidate == true_name and task1_pinned is not None
        ),
    }


async def _run(project: str | None) -> int:
    conn = _readonly_conn()
    try:
        project_id = _resolve_project_id(conn, project)
        bible, chapters = _load_bible_and_chapters(conn, project_id)
    finally:
        conn.close()

    chapters_by_idx = stages._chapters_by_idx(chapters)
    roster = stages._alias_verdict_roster(bible)

    print(f"项目: {project_id}（{len(bible.characters)} 个角色，{len(chapters)} 章）")
    print(f"标定集: {len(CALIBRATION_SET)} 条，串行真实调用模型（不并发）\n")

    results: list[dict[str, Any]] = []
    for alias_text, expected, reason in CALIBRATION_SET:
        evidence = _find_alias_evidence(bible, alias_text)
        if evidence is None:
            results.append({
                "alias": alias_text, "expected": expected, "reason": reason,
                "error": "库中找不到这条别名的真实证据锚点，跳过",
            })
            continue
        true_name, chapter_idx, quote = evidence
        outcome = await _judge_one(
            bible, chapters_by_idx, roster, project_id,
            alias_text, true_name, chapter_idx, quote,
        )
        outcome["expected"] = expected
        outcome["reason"] = reason
        results.append(outcome)

    print(f"{'别名':<10}{'期望':<6}{'实测':<6}{'一致':<6}任务一(选人/钉证)  理由")
    print("-" * 110)
    hit = 0
    scored = 0
    for r in results:
        alias_text = r["alias"]
        if "error" in r:
            print(f"{alias_text:<10}{'—':<6}{'ERROR':<6}{'—':<6}{r['error']}")
            continue
        scored += 1
        actual = r["is_exclusive_reference"]
        expected = r["expected"]
        match = actual == expected
        hit += int(match)
        mark = "OK" if match else "XX"
        task1 = f"{r['task1_selected_candidate']}/{'过' if r['task1_pass'] else '未过'}"
        print(
            f"{alias_text:<10}{str(expected):<6}{str(actual):<6}{mark:<6}"
            f"{task1:<18}{r['reason']}"
        )
    print("-" * 110)
    print(f"命中率: {hit}/{scored}" + (f"（另有 {len(results) - scored} 条跳过）" if scored < len(results) else ""))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=None, help="项目 id 或 name，默认《我欲封天》")
    args = parser.parse_args()
    return asyncio.run(_run(args.project))


if __name__ == "__main__":
    raise SystemExit(main())
