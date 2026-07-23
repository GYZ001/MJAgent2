"""只读 Resource 读取器：按 `manju://` URI 读取项目/剧集/Run/Artifact 摘要。

只做只读查询，不触发任何写入或后台任务；返回内容一律先过 `redact_value`，
杜绝任何密钥/Authorization 字面量随资源内容进入模型上下文（PRD §9.2 / §12.1）。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from app.agent.redaction import redact_value
from app.db import get_conn, rows_to_dicts
from app.evidence import repository as evidence_repository


class ResourceNotFound(Exception):
    """URI 格式合法但目标不存在，供上层如实回复模型而不是抛 500。"""


class ResourceUriInvalid(Exception):
    """URI 不匹配任何已注册 Resource Template。"""


def _project_summary(project_id: str) -> dict[str, Any]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise ResourceNotFound(f"项目不存在：{project_id}")
    project = dict(row)
    chapter_count = conn.execute(
        "SELECT COUNT(*) AS c FROM chapters WHERE project_id=?", (project_id,)
    ).fetchone()["c"]
    episodes = rows_to_dicts(conn.execute(
        "SELECT id, episode_no, title, status, screenplay_status, delivery_status "
        "FROM episodes WHERE project_id=? ORDER BY episode_no", (project_id,)
    ).fetchall())
    return {
        "id": project["id"],
        "name": project["name"],
        "status": project["status"],
        "bible_status": project.get("bible_status"),
        "plan_status": project.get("plan_status"),
        "novel_chars": project.get("novel_chars"),
        "chapter_count": chapter_count,
        "episode_count": len(episodes),
        "episodes": episodes,
        "created_at": project.get("created_at"),
    }


def _projects_list() -> dict[str, Any]:
    rows = rows_to_dicts(get_conn().execute(
        "SELECT id, name, status, created_at FROM projects ORDER BY created_at DESC"
    ).fetchall())
    return {"projects": rows}


def _chapter(project_id: str, idx: int) -> dict[str, Any]:
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
        raise ResourceNotFound(f"项目不存在：{project_id}")
    row = conn.execute(
        "SELECT idx, title, content, char_count FROM chapters WHERE project_id=? AND idx=?",
        (project_id, idx),
    ).fetchone()
    if not row:
        raise ResourceNotFound(f"章节不存在：项目 {project_id} 第 {idx} 章")
    chapter = dict(row)
    # 原著正文是不可信素材：其中出现的“忽略规则/调用工具”类文字只当内容，不当指令。
    chapter["trust_level"] = "untrusted_content"
    return chapter


def _episode_summary(episode_id: str) -> dict[str, Any]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row:
        raise ResourceNotFound(f"剧集不存在：{episode_id}")
    episode = dict(row)
    episode.pop("screenplay_json", None)
    episode.pop("storyboard_outline_json", None)
    shot_stats = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN scene_status='approved' THEN 1 ELSE 0 END) AS approved
           FROM shots WHERE episode_id=?""",
        (episode_id,),
    ).fetchone()
    episode["shot_count"] = shot_stats["total"] or 0
    episode["shot_approved_count"] = shot_stats["approved"] or 0
    return episode


def _run_summary(run_id: str) -> dict[str, Any]:
    run = evidence_repository.get_run(run_id)
    if not run:
        raise ResourceNotFound(f"运行不存在：{run_id}")
    run = dict(run)
    run["steps"] = evidence_repository.get_steps(run_id)
    return run


def _run_events(run_id: str) -> dict[str, Any]:
    if not evidence_repository.get_run(run_id):
        raise ResourceNotFound(f"运行不存在：{run_id}")
    return {"run_id": run_id, "events": evidence_repository.get_events(run_id, limit=200)}


def _artifact_summary(artifact_id: str) -> dict[str, Any]:
    artifact = evidence_repository.get_artifact(artifact_id)
    if not artifact:
        raise ResourceNotFound(f"产物不存在：{artifact_id}")
    artifact = dict(artifact)
    artifact["evaluations"] = evidence_repository.get_evaluations(artifact_id)
    return artifact


def _system_health() -> dict[str, Any]:
    """脱敏系统健康快照：只给计数与状态分布，不含请求/响应原文。"""
    conn = get_conn()
    jobs_by_status = rows_to_dicts(conn.execute(
        "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
    ).fetchall())
    calls_by_status = rows_to_dicts(conn.execute(
        "SELECT status, COUNT(*) AS c FROM provider_calls "
        "WHERE ts > (SELECT COALESCE(MAX(ts),0) - 86400 FROM provider_calls) GROUP BY status"
    ).fetchall())
    return {"jobs_by_status": jobs_by_status, "provider_calls_last_24h_by_status": calls_by_status}


_ROUTES: list[tuple[re.Pattern[str], Callable[..., dict[str, Any]]]] = [
    (re.compile(r"^manju://projects$"), lambda: _projects_list()),
    (re.compile(r"^manju://projects/(?P<project_id>[^/]+)$"), lambda project_id: _project_summary(project_id)),
    (
        re.compile(r"^manju://projects/(?P<project_id>[^/]+)/chapters/(?P<idx>\d+)$"),
        lambda project_id, idx: _chapter(project_id, int(idx)),
    ),
    (re.compile(r"^manju://episodes/(?P<episode_id>[^/]+)$"), lambda episode_id: _episode_summary(episode_id)),
    (re.compile(r"^manju://runs/(?P<run_id>[^/]+)/events$"), lambda run_id: _run_events(run_id)),
    (re.compile(r"^manju://runs/(?P<run_id>[^/]+)$"), lambda run_id: _run_summary(run_id)),
    (re.compile(r"^manju://artifacts/(?P<artifact_id>[^/]+)$"), lambda artifact_id: _artifact_summary(artifact_id)),
    (re.compile(r"^manju://system/health$"), lambda: _system_health()),
]


def read_resource(uri: str) -> dict[str, Any]:
    """按 URI 分发到具体只读查询；密钥字段在返回前统一剔除。"""
    uri = (uri or "").strip()
    for pattern, handler in _ROUTES:
        match = pattern.match(uri)
        if match:
            result = handler(**match.groupdict())
            return redact_value(result)
    raise ResourceUriInvalid(f"未知或未实现的 Resource URI：{uri}")
