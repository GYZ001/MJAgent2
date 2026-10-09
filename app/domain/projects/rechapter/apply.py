"""唯一的写库点：在一个 ``BEGIN IMMEDIATE`` 事务内重新核验计划仍然成立，
再写 chapters/episodes；任一核验失败整个项目的写入全部取消，旧数据原封不
动。失败分支的第一条语句是 ``conn.rollback()``，排在任何日志/审计调用之前
（CLAUDE.md「Ownership Must Be Explicit」）。审计落盘用独立连接
（``app.audit.recorder.record_command`` -> ``app.audit.store.
insert_operation_audit_row`` -> ``app.db._run_write_transaction_once``，各自
开关自己的连接），不会在调用方这条连接上触发隐式提交，所以特意放在
``conn.commit()`` 之后才调用。
"""
from __future__ import annotations

import json
from typing import Any

from app import config
from app.audit.recorder import record_command
from app.db import new_id, now
from app.planning import replan_blockers

from app.domain.projects.rechapter.frozen import frozen_chapter_idx, frozen_episode_rows
from app.domain.projects.rechapter.models import ChapterWrite, EpisodeWrite, ProjectPlan


def _reverify_not_stale(conn: Any, project_id: str, plan: ProjectPlan) -> str | None:
    """重新核验：无在途任务、冻结集合/冻结章节未变、计划读到的章节内容未变。"""
    blockers = replan_blockers(conn, project_id)
    if blockers["blocked"]:
        return "重新核验时发现项目仍有未结束的下游任务，取消本次写入"
    frozen_episodes = frozen_episode_rows(conn, project_id)
    if {str(e["id"]) for e in frozen_episodes} != plan.frozen_episode_ids:
        return "重新核验时发现冻结集合已变化，取消本次写入"
    if frozen_chapter_idx(conn, project_id, frozen_episodes) != plan.frozen_idx:
        return "重新核验时发现冻结章节已变化，取消本次写入"
    for segment in plan.segments:
        for old in segment.old_chapters:
            row = conn.execute("SELECT content FROM chapters WHERE id=?", (old.id,)).fetchone()
            if row is None or (row["content"] or "") != old.content:
                return f"重新核验时发现章节 idx={old.idx} 内容已变化，取消本次写入"
    return None


def _verify_deletable(conn: Any, plan: ProjectPlan) -> str | None:
    """多余旧章节仅在确实无任何分镜原文证据引用时才可删；命中引用则整个
    项目拒绝写入，不做部分删除。"""
    delete_ids = [w.id for w in plan.chapter_writes if w.action == "delete" and w.id is not None]
    if not delete_ids:
        return None
    marks = ",".join("?" for _ in delete_ids)
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM storyboard_source_bindings WHERE chapter_id IN ({marks})",
        delete_ids,
    ).fetchone()
    if row["c"]:
        return "待删除的多余章节仍被分镜原文证据引用，取消整个项目的写入"
    return None


def _write_chapters(conn: Any, project_id: str, writes: list[ChapterWrite]) -> None:
    for w in writes:
        if w.action == "update":
            conn.execute(
                "UPDATE chapters SET title=?, content=?, char_count=?, paratext_json=? WHERE id=?",
                (w.title, w.content, w.char_count, w.paratext_json, w.id),
            )
        elif w.action == "insert":
            conn.execute(
                "INSERT INTO chapters(project_id, idx, title, content, char_count, paratext_json) "
                "VALUES(?,?,?,?,?,?)",
                (project_id, w.idx, w.title, w.content, w.char_count, w.paratext_json),
            )
        elif w.action == "delete":
            conn.execute("DELETE FROM chapters WHERE id=?", (w.id,))


def _write_episodes(conn: Any, project_id: str, writes: list[EpisodeWrite]) -> None:
    for w in writes:
        if w.action == "update":
            conn.execute(
                "UPDATE episodes SET title=?, synopsis=? WHERE id=?",
                (w.title, w.synopsis, w.episode_id),
            )
        elif w.action == "insert":
            conn.execute(
                "INSERT INTO episodes(id, project_id, episode_no, title, hook, cliffhanger, synopsis, "
                "source_chapters, target_duration_s, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'planned', ?)",
                (
                    new_id("ep"), project_id, w.episode_no, w.title, "", "", w.synopsis,
                    json.dumps([w.episode_no]), config.EPISODE_TARGET_DEFAULT_S, now(),
                ),
            )
        elif w.action == "delete":
            conn.execute("DELETE FROM episodes WHERE id=?", (w.episode_id,))


def _record_audit(plan: ProjectPlan) -> None:
    try:
        record_command(
            name="project.rechapter_apply", title="存量项目切章修正",
            source="script", status="succeeded", error_code=None,
            summary=(
                f"章节 {plan.old_chapter_count}→{plan.new_chapter_count}；"
                f"chapters 写入 {len(plan.chapter_writes)} 条，episodes 写入 {len(plan.episode_writes)} 条"
            ),
            command_id=None, run_id=None,
            args={"project_id": plan.project_id}, duration_ms=None,
        )
    except Exception:  # noqa: BLE001 -- 审计失败不能回头拖垮已经提交成功的写入
        pass


def apply_project(conn: Any, project_id: str, plan: ProjectPlan) -> dict:
    """落库唯一入口。``conn`` 必传——不设默认值，漏传在调用那一刻就是
    ``TypeError``（CLAUDE.md「可选参数是缺陷的温床」）。"""
    if not plan.ok:
        return {"status": "rejected", "reason": plan.reject_reason}
    if not plan.changed:
        return {"status": "unchanged"}
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        reason = _reverify_not_stale(conn, project_id, plan) or _verify_deletable(conn, plan)
        if reason:
            conn.rollback()
            return {"status": "rejected", "reason": reason}
        _write_chapters(conn, project_id, plan.chapter_writes)
        _write_episodes(conn, project_id, plan.episode_writes)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _record_audit(plan)
    return {
        "status": "applied",
        "chapter_writes": len(plan.chapter_writes),
        "episode_writes": len(plan.episode_writes),
    }
