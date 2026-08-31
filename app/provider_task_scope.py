"""供应商任务清空判据的作用域构建：把「删哪个项目/集/镜/版本」翻译成 SQL 子句。

从 ``app/provider_task_clearance.py`` 抽出。那个文件被一个 335 行的
``_provider_task_clearance_evaluation`` 统治（单函数上限是 50 行），整文件顶到了
500 行的默认上限。作用域构建是天然的接缝：它只依赖四个 id 输入和 jobs 表的实际
列形态，产出两组 ``(子句, 参数)``，与后面的取证查询、逐行判定互不相关。

拆分而不是加基线：``app/FILE_CONVENTIONS.toml`` 的基线是棘轮，只降不升，不许为了
让自己的改动过关而调大——装不下时先想怎么拆。

四种范围各一个私有构造器，`build_clearance_scope` 只做编排。子句与参数成对返回，
避免「子句加了参数没加」这类 SQL 占位符错位——那种错在运行时才炸，且报错跟根因
没关系。
"""

from __future__ import annotations

from typing import Any, NamedTuple

#: 一段范围产出的 (jobs 子句, jobs 参数, claims 子句, claims 参数)。
_ScopeFragment = tuple[list[str], list[str], list[str], list[str]]


class ClearanceScope(NamedTuple):
    """一次清空请求的作用域，以及 jobs 表实际存在的列。

    ``job_columns`` 之所以随作用域一起返回：下游拼 SELECT 时还要按同一份列形态
    决定 ``j.project_id`` / ``j.episode_id`` 能不能直接取，两处必须同源，不能各自
    再探一次表结构。
    """

    job_columns: set[str]
    job_scope_clauses: list[str]
    job_scope_params: list[str]
    claim_scope_clauses: list[str]
    claim_scope_params: list[str]


def _project_fragment(project_id: str, job_columns: set[str]) -> _ScopeFragment:
    """项目范围：jobs 上可能没有 project_id/episode_id 列，按实际列形态取。"""
    clauses: list[str] = []
    params: list[str] = []
    if "project_id" in job_columns:
        clauses.append("j.project_id=?")
        params.append(project_id)
    if "episode_id" in job_columns:
        clauses.append("j.episode_id IN (SELECT id FROM episodes WHERE project_id=?)")
        params.append(project_id)
    clauses.extend([
        """j.shot_id IN (
               SELECT s.id FROM shots s
               JOIN episodes e ON e.id=s.episode_id
              WHERE e.project_id=?
           )""",
        """j.version_id IN (
               SELECT v.id FROM shot_versions v
               JOIN shots s ON s.id=v.shot_id
               JOIN episodes e ON e.id=s.episode_id
              WHERE e.project_id=?
           )""",
    ])
    params.extend([project_id, project_id])
    return clauses, params, ["c.project_id=?"], [project_id]


def _episode_fragment(episode_id: str, job_columns: set[str]) -> _ScopeFragment:
    clauses: list[str] = []
    params: list[str] = []
    if "episode_id" in job_columns:
        clauses.append("j.episode_id=?")
        params.append(episode_id)
    clauses.extend([
        "j.shot_id IN (SELECT id FROM shots WHERE episode_id=?)",
        """j.version_id IN (
               SELECT v.id FROM shot_versions v
               JOIN shots s ON s.id=v.shot_id
              WHERE s.episode_id=?
           )""",
    ])
    params.extend([episode_id, episode_id])
    return (
        clauses,
        params,
        ["(c.episode_id=? OR c.origin_episode_id=?)"],
        [episode_id, episode_id],
    )


def _shot_fragment(shots: list[str]) -> _ScopeFragment:
    marks = ",".join("?" for _ in shots)
    return (
        [
            f"j.shot_id IN ({marks})",
            f"j.version_id IN (SELECT id FROM shot_versions WHERE shot_id IN ({marks}))",
        ],
        [*shots, *shots],
        [f"(c.shot_id IN ({marks}) OR c.origin_shot_id IN ({marks}))"],
        [*shots, *shots],
    )


def _version_fragment(versions: list[str]) -> _ScopeFragment:
    marks = ",".join("?" for _ in versions)
    return (
        [f"j.version_id IN ({marks})"],
        list(versions),
        [f"(c.version_id IN ({marks}) OR c.origin_version_id IN ({marks}))"],
        [*versions, *versions],
    )


def _dedup(values: list[str] | tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def build_clearance_scope(
    db: Any,
    *,
    project_id: str | None,
    episode_id: str | None,
    shot_ids: list[str] | tuple[str, ...],
    version_ids: list[str] | tuple[str, ...],
) -> ClearanceScope:
    """把资源范围翻译成 jobs/claims 两侧的 WHERE 子句。

    ``db`` 必传，与整条清空判据链一致：范围判定必须和调用方在同一个事务里看到
    同一份数据（见 provider_task_clearance 顶部注释）。

    没有任何范围时抛 ``ValueError``——空范围不等于「全部」，也不等于「无需检查」。
    """
    job_columns = {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in db.execute("PRAGMA table_info(jobs)").fetchall()
    }
    shots = _dedup(shot_ids)
    versions = _dedup(version_ids)

    fragments: list[_ScopeFragment] = []
    if project_id:
        fragments.append(_project_fragment(project_id, job_columns))
    if episode_id:
        fragments.append(_episode_fragment(episode_id, job_columns))
    if shots:
        fragments.append(_shot_fragment(shots))
    if versions:
        fragments.append(_version_fragment(versions))

    scope = ClearanceScope(job_columns, [], [], [], [])
    for job_clauses, job_params, claim_clauses, claim_params in fragments:
        scope.job_scope_clauses.extend(job_clauses)
        scope.job_scope_params.extend(job_params)
        scope.claim_scope_clauses.extend(claim_clauses)
        scope.claim_scope_params.extend(claim_params)
    if not scope.job_scope_clauses:
        raise ValueError("provider task clearance requires a resource scope")
    return scope
