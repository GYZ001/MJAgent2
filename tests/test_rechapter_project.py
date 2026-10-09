"""存量项目切章修正（``app.domain.projects.rechapter``）的安全规则测试。

覆盖：冻结集/冻结章节逐字节不变、残片被正确剔除并进报告、"后面还跟着冻结
章节"的段一旦重切后数量/序号不稳定就整段保持原样、尾段并块拆开新增集、
项目不是一章一集整体拒绝、apply 时发现在途任务回滚且旧数据不变（用第二条
独立连接读盘核对）、dry-run 不写库、CLI ``--all`` 只列出会变化的项目、无序号
标题（"楔子"类）的稳定性比较既不误拒也不放过真不一致。

每段正文守恒校验（丢字必拒、补出的字允许但计数）与由它触发的报告措辞在
``tests/test_rechapter_conservation.py``——那几条移出去纯粹是这个文件快
碰到 500 行红线，不是职责拆分；``_make_project``/``_freeze_via_artifact``/
``_chapter_row``/``_pad`` 这几个共用夹具留在本文件，被那边 import。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from app import config
from app.db import get_conn, new_id, now
from app.domain.projects.rechapter.apply import apply_project
from app.domain.projects.rechapter.models import ChapterRow
from app.domain.projects.rechapter.plan import plan_project
from app.domain.projects.rechapter.report import affected_old_idx, scan_bible_chapter_references
from app.domain.projects.rechapter.segment import _stability_ok

_PAD = "故事慢慢展开人物逐渐丰满情节渐入佳境众人纷纷议论此事如何收场暂且不表"


def _pad(n: int = 3) -> str:
    return _PAD * n


def _make_project(conn, chapters: list[tuple[str, str]], *, bible_json: str | None = None) -> str:
    """按 (title, content) 列表建一个一章一集的项目，口径同
    ``app/domain/projects/create.py`` 导入写入 + ``app.planning.
    _insert_regex_plan_episodes``。"""
    project_id = new_id("proj")
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json) VALUES(?,?,?,?,?)",
        (project_id, "测试项目", "planned", now(), bible_json),
    )
    for idx, (title, content) in enumerate(chapters, start=1):
        conn.execute(
            "INSERT INTO chapters(project_id, idx, title, content, char_count) VALUES(?,?,?,?,?)",
            (project_id, idx, title, content, len(content)),
        )
        conn.execute(
            "INSERT INTO episodes(id, project_id, episode_no, title, hook, cliffhanger, synopsis, "
            "source_chapters, target_duration_s, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?, 'planned', ?)",
            (
                new_id("ep"), project_id, idx, title or f"第{idx}章", "", "",
                content[:100], json.dumps([idx]), 50, now(),
            ),
        )
    conn.commit()
    return project_id


def _freeze_via_artifact(conn, project_id: str, episode_no: int) -> None:
    conn.execute(
        "UPDATE episodes SET screenplay_status='ready', screenplay_artifact_id=? "
        "WHERE project_id=? AND episode_no=?",
        (new_id("art"), project_id, episode_no),
    )
    conn.commit()


def _freeze_via_failed_status(conn, project_id: str, episode_no: int) -> None:
    """命中产物信号的另一种形态：剧本失败但没有任何产物——仍然冻结。"""
    conn.execute(
        "UPDATE episodes SET screenplay_status='failed' WHERE project_id=? AND episode_no=?",
        (project_id, episode_no),
    )
    conn.commit()


def _chapter_row(conn, project_id: str, idx: int) -> dict | None:
    row = conn.execute(
        "SELECT id, idx, title, content FROM chapters WHERE project_id=? AND idx=?",
        (project_id, idx),
    ).fetchone()
    return dict(row) if row else None


def _episode_row(conn, project_id: str, episode_no: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, episode_no),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 1. 冻结集/冻结章节逐字节不变；尾段并块拆开新增集；已清爽的段不产生空写入
# ---------------------------------------------------------------------------

def test_frozen_untouched_and_tail_block_splits_into_new_episode() -> None:
    conn = get_conn()
    chapters = [
        ("第1章 开场", f"第1章 开场\n{_pad()}"),
        ("第2章 发展", f"第2章 发展\n{_pad()}"),  # 冻结：有产物（screenplay_artifact_id）
        ("第3章 转折", f"第3章 转折\n{_pad()}"),  # 冻结：剧本失败但无产物，仍冻结
        (  # 尾段：并块，含两个真实章节，之前被当成一章存了
            "第4章 终篇甲",
            f"第4章 终篇甲\n{_pad()}\n第5章 终篇乙\n{_pad(2)}",
        ),
    ]
    bible_json = json.dumps({"characters": [{"source_evidence": [{"evidence_chapter_index": 4, "evidence_quote": "x"}]}]})
    project_id = _make_project(conn, chapters, bible_json=bible_json)
    _freeze_via_artifact(conn, project_id, 2)
    _freeze_via_failed_status(conn, project_id, 3)

    before_frozen = [_chapter_row(conn, project_id, i) for i in (2, 3)]
    before_frozen_episodes = [_episode_row(conn, project_id, i) for i in (2, 3)]
    before_idx1 = _chapter_row(conn, project_id, 1)

    plan = plan_project(conn, project_id)
    assert plan.ok, plan.reject_reason
    assert plan.frozen_idx == {2, 3}
    # idx=1 已经是干净的，重切结果应与原文完全一致——不产生空操作写入。
    assert all(w.idx != 1 for w in plan.chapter_writes)
    # 尾段：旧 1 章 -> 新 2 章。
    tail = next(s for s in plan.segments if s.start_idx == 4)
    assert tail.accepted and tail.is_tail
    assert len(tail.old_chapters) == 1 and len(tail.new_chapters) == 2

    affected = affected_old_idx(plan)
    assert affected == {4}
    plan.bible_reference_report = scan_bible_chapter_references(bible_json, affected)
    assert plan.bible_reference_report.get("evidence_chapter_index") == 1

    result = apply_project(conn, project_id, plan)
    assert result["status"] == "applied"

    # 冻结集/冻结章节一个字节不变。
    after_frozen = [_chapter_row(conn, project_id, i) for i in (2, 3)]
    after_frozen_episodes = [_episode_row(conn, project_id, i) for i in (2, 3)]
    assert after_frozen == before_frozen
    assert after_frozen_episodes == before_frozen_episodes
    assert _chapter_row(conn, project_id, 1) == before_idx1

    # 尾段新增：idx=4 内容变短（只剩"甲"那部分），idx=5 是全新一章。
    new4 = _chapter_row(conn, project_id, 4)
    new5 = _chapter_row(conn, project_id, 5)
    assert new5 is not None
    assert "第5章" not in new4["content"]
    assert new5["title"].startswith("第5章")
    ep5 = _episode_row(conn, project_id, 5)
    assert ep5 is not None and json.loads(ep5["source_chapters"]) == [5]
    assert conn.execute(
        "SELECT COUNT(*) c FROM chapters WHERE project_id=?", (project_id,)
    ).fetchone()["c"] == 5


# ---------------------------------------------------------------------------
# 2. 段首残留文本（<=200 字）被剔除并进报告，不进新首章
# ---------------------------------------------------------------------------

def test_residual_prefix_excluded_and_reported() -> None:
    conn = get_conn()
    residual = "这是残留的一小段话"
    chapters = [
        ("第1章 开场", f"第1章 开场\n{_pad()}"),  # 冻结
        (residual + "第2章风起", residual + f"第2章风起\n{_pad()}"),  # 尾段，粘连残片
        ("第3章 收尾", f"第3章 收尾\n{_pad()}"),  # 尾段，干净
    ]
    project_id = _make_project(conn, chapters)
    _freeze_via_artifact(conn, project_id, 1)

    plan = plan_project(conn, project_id)
    assert plan.ok, plan.reject_reason
    tail = next(s for s in plan.segments if s.start_idx == 2)
    assert tail.accepted, tail.reason
    assert tail.excluded_prefix == residual

    result = apply_project(conn, project_id, plan)
    assert result["status"] == "applied"
    new2 = _chapter_row(conn, project_id, 2)
    assert new2["title"] == "第2章风起"
    assert residual not in new2["content"]
    # idx=3 本来就干净，不应该被改写。
    assert conn.execute(
        "SELECT COUNT(*) c FROM chapters WHERE project_id=? AND idx=3", (project_id,)
    ).fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# 3. 中间段（后面还跟着冻结章节）重切后数量不稳定 -> 整段保持原样
# ---------------------------------------------------------------------------

def test_middle_segment_kept_as_is_when_resplit_changes_chapter_count() -> None:
    conn = get_conn()
    chapters = [
        ("第1章 开场", f"第1章 开场\n{_pad()}"),  # 冻结
        (  # 中间段：并块，重切会产出 2 章而不是 1 章
            "第10章 甲",
            f"第10章 甲\n{_pad()}\n第11章 乙\n{_pad(2)}",
        ),
        ("第3章 丙", f"第3章 丙\n{_pad()}"),  # 冻结，跟在中间段后面
    ]
    project_id = _make_project(conn, chapters)
    _freeze_via_artifact(conn, project_id, 1)
    _freeze_via_artifact(conn, project_id, 3)
    before = _chapter_row(conn, project_id, 2)

    plan = plan_project(conn, project_id)
    assert plan.ok, plan.reject_reason
    middle = next(s for s in plan.segments if s.start_idx == 2)
    assert not middle.accepted
    assert "数" in middle.reason or "序号" in middle.reason
    assert not plan.changed

    result = apply_project(conn, project_id, plan)
    assert result["status"] == "unchanged"
    assert _chapter_row(conn, project_id, 2) == before


# ---------------------------------------------------------------------------
# 4. 项目不是一章一集 -> 整个项目拒绝
# ---------------------------------------------------------------------------

def test_non_one_to_one_project_is_rejected() -> None:
    conn = get_conn()
    project_id = _make_project(conn, [("第1章", f"第1章\n{_pad()}"), ("第2章", f"第2章\n{_pad()}")])
    conn.execute(
        "UPDATE episodes SET source_chapters=? WHERE project_id=? AND episode_no=1",
        (json.dumps([1, 2]), project_id),
    )
    conn.commit()

    plan = plan_project(conn, project_id)
    assert not plan.ok
    assert "一章一集" in plan.reject_reason
    assert apply_project(conn, project_id, plan) == {"status": "rejected", "reason": plan.reject_reason}


# ---------------------------------------------------------------------------
# 5. dry-run（只调用 plan_project）绝不写库
# ---------------------------------------------------------------------------

def test_dry_run_never_writes() -> None:
    conn = get_conn()
    chapters = [
        ("第1章 开场", f"第1章 开场\n{_pad()}"),
        ("第2章 终篇甲", f"第2章 终篇甲\n{_pad()}\n第3章 终篇乙\n{_pad(2)}"),
    ]
    project_id = _make_project(conn, chapters)
    _freeze_via_artifact(conn, project_id, 1)
    before_count = conn.execute(
        "SELECT COUNT(*) c FROM chapters WHERE project_id=?", (project_id,)
    ).fetchone()["c"]

    plan = plan_project(conn, project_id)
    assert plan.ok and plan.changed

    after_count = conn.execute(
        "SELECT COUNT(*) c FROM chapters WHERE project_id=?", (project_id,)
    ).fetchone()["c"]
    assert after_count == before_count


# ---------------------------------------------------------------------------
# 6. apply 时发现在途任务 -> 回滚，旧数据原封不动（第二条独立连接核对）
# ---------------------------------------------------------------------------

def test_apply_rolls_back_when_active_job_appears() -> None:
    conn = get_conn()
    chapters = [
        ("第1章 开场", f"第1章 开场\n{_pad()}"),
        ("第2章 终篇甲", f"第2章 终篇甲\n{_pad()}\n第3章 终篇乙\n{_pad(2)}"),
    ]
    project_id = _make_project(conn, chapters)
    _freeze_via_artifact(conn, project_id, 1)
    plan = plan_project(conn, project_id)
    assert plan.ok and plan.changed

    conn.execute(
        "INSERT INTO jobs(id, kind, episode_id, project_id, status, created_at, updated_at, "
        "cancellation_requested, abandoned) VALUES(?,?,?,?,?,?,?,0,0)",
        (new_id("job"), "video", None, project_id, "running", now(), now()),
    )
    conn.commit()

    result = apply_project(conn, project_id, plan)
    assert result["status"] == "rejected"
    assert "任务" in result["reason"]

    import sqlite3

    independent = sqlite3.connect(str(config.DB_PATH))
    independent.row_factory = sqlite3.Row
    try:
        row = independent.execute(
            "SELECT content FROM chapters WHERE project_id=? AND idx=2", (project_id,)
        ).fetchone()
        assert "第3章" in row["content"]  # 仍是未拆分前的并块，没有被写入
        count = independent.execute(
            "SELECT COUNT(*) c FROM chapters WHERE project_id=?", (project_id,)
        ).fetchone()["c"]
        assert count == 2
    finally:
        independent.close()


# ---------------------------------------------------------------------------
# 7. CLI ``--all`` 只列出结果会变化的项目
# ---------------------------------------------------------------------------

def test_cli_all_only_lists_changed_projects(tmp_path: Path) -> None:
    conn = get_conn()
    changed_project = _make_project(conn, [
        ("第1章 开场", f"第1章 开场\n{_pad()}"),
        ("第2章 终篇甲", f"第2章 终篇甲\n{_pad()}\n第3章 终篇乙\n{_pad(2)}"),
    ])
    _freeze_via_artifact(conn, changed_project, 1)
    unchanged_project = _make_project(conn, [("第1章 唯一", f"第1章 唯一\n{_pad()}")])

    # WAL 模式下已提交的数据可能还只在 -wal 侧车文件里；复制前先 checkpoint
    # 回主文件，否则子进程读到的是缺了最新事务的旧快照（空结果但不报错）。
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    sandbox = tmp_path / "cli_sandbox"
    (sandbox / "data").mkdir(parents=True)
    shutil.copy(str(config.DB_PATH), str(sandbox / "data" / "manju.db"))

    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "rechapter_project.py"), "--all", "--json"],
        cwd=str(repo_root),
        env={
            **__import__("os").environ,
            "MANJU_TEST_PROFILE": "isolated",
            "MANJU_TEST_SANDBOX": str(sandbox),
        },
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    project_ids = {r["plan"]["project_id"] for r in results}
    assert changed_project in project_ids
    assert unchanged_project not in project_ids


# ---------------------------------------------------------------------------
# 8. 无序号标题（"楔子"类）：不误拒，也不放过真不一致
# ---------------------------------------------------------------------------

def test_preamble_without_ordinal_segment_accepted_and_glued_heading_fixed() -> None:
    """复现沙箱误拒：段从 idx=1 开始、首章是无序号的"楔子"，后面还跟着冻结
    章节。旧实现里 ``_stability_ok`` 对"楔子"两侧都解析不出序号就直接判不
    通过，把整段（133 章）误拒。修复后"楔子"==" 楔子"按标题相等通过，段内
    真正的粘连标题（"残片甲第2章风起"）仍然被正确剔除修正。"""
    conn = get_conn()
    chapters = [
        ("楔子", f"楔子\n{_pad()}"),
        ("残片甲第2章风起", f"残片甲第2章风起\n{_pad()}"),
        ("第3章 收尾", f"第3章 收尾\n{_pad()}"),
        ("第4章 定稿", f"第4章 定稿\n{_pad()}"),  # 冻结，跟在这个段后面
    ]
    project_id = _make_project(conn, chapters)
    _freeze_via_artifact(conn, project_id, 4)

    plan = plan_project(conn, project_id)
    assert plan.ok, plan.reject_reason
    segment = next(s for s in plan.segments if s.start_idx == 1)
    assert segment.accepted, segment.reason
    assert segment.end_idx == 3 and not segment.is_tail

    result = apply_project(conn, project_id, plan)
    assert result["status"] == "applied"
    new1, new2, new3 = (_chapter_row(conn, project_id, i) for i in (1, 2, 3))
    assert new1["title"] == "楔子"
    assert new2["title"] == "第2章风起"
    assert "残片甲" not in new2["content"]
    assert new3["title"] == "第3章 收尾"


def test_stability_ok_titles_without_ordinal_must_match_exactly() -> None:
    """``_stability_ok`` 的直接单测：两侧都无序号——标题相等才通过（"楔子"==
    "楔子"），标题不同仍判不通过（"楔子" != "引子"），不会被"反正都没有
    序号"放过。"""
    same_title_old = [ChapterRow(id=1, idx=1, title="楔子", content="x")]
    same_title_new = [{"title": "楔子", "content": "y"}]
    assert _stability_ok(same_title_old, same_title_new) is True

    different_title_old = [ChapterRow(id=1, idx=1, title="楔子", content="x")]
    different_title_new = [{"title": "引子", "content": "y"}]
    assert _stability_ok(different_title_old, different_title_new) is False
