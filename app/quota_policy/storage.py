"""EP-04 第二阶段：项目目录实测存储占用（L2，见 app/LAYERS.toml::app.quota_policy）。

判据挂产物信号——目录/文件的实测字节数，不挂任何状态字段（CLAUDE.md「Gates
and Criteria」）。三件事，边界严格分开：

1. **采样**（``sample_project_storage``/``storage_sample_sweep_loop``）：唯一
   允许做 ``os.walk`` 全树遍历的地方，用 ``asyncio.to_thread`` + 超时挪到线程
   执行，独立连接写入结果（诊断类写入不等调用方的锁，见 app/quota_policy/
   schema.py 模块文档同一惯例）。**绝不在请求路径调用**——请求路径只读上一
   次采样的结果（``latest_sample``/``bytes_used_for_*``），哪怕这个结果已经
   是几十分钟前的快照，也比同步跑一次 ``du`` 卡住请求安全。
2. **查询**（``latest_sample``/``bytes_used_for_*``/``top_projects_by_storage``）：
   只读最近一次（或若干次，供时间序列用）采样行，零文件系统访问。
3. **清理候选与执行**（``cleanup_candidates``/``execute_cleanup``）：超限时给
   用户一份「最大的 N 个未被采纳的版本」建议清单——这里对候选版本各自做一次
   ``Path.stat()``（不是整树遍历，是有限条数的单文件 stat，请求路径内可接
   受）；真正的删除必须用户确认后调用 ``execute_cleanup``，不自动执行
   （CLAUDE.md「超限行为：不删任何数据」）。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

from app import config, db
from app.db import new_id, now
from app.quota_policy import schema

DEFAULT_SAMPLE_TIMEOUT_S = 90.0
DEFAULT_SWEEP_INTERVAL_S = 3600.0
DEFAULT_CLEANUP_CANDIDATE_LIMIT = 20
_MAX_CLEANUP_CANDIDATE_LIMIT = 200


def _dir_size_bytes(root: Path) -> int:
    """同步遍历单个项目目录求总字节数；在线程里跑（见 ``sample_project_storage``）。

    单个文件/目录读取失败（权限、扫描期间被并发删除的竞态）跳过而不是整体报
    错——存储采样是巡检性质，不能因为一个坏条目让整个项目的采样失败。"""
    total = 0
    if not root.exists():
        return 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


async def sample_project_storage(
    project_id: str, owner_user_id: str | None, project_dir: Path,
    *, timeout_s: float = DEFAULT_SAMPLE_TIMEOUT_S,
) -> dict:
    """异步定时采样单个项目目录占用——线程执行 + 超时，独立连接写入结果，结
    果带 ``sampled_at`` 时间戳（UI 据此如实显示「截至 X 时」）。超时/异常都落
    一行（``status`` 标记），不是静默丢弃：下一轮巡检会再采一次，但这一轮的
    失败本身也是可观测信息。"""
    started = time.monotonic()
    status = "ok"
    error: str | None = None
    bytes_total = 0
    try:
        bytes_total = await asyncio.wait_for(
            asyncio.to_thread(_dir_size_bytes, project_dir), timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        status = "timeout"
        error = f"采样超时（>{timeout_s:.0f}s）"
    except OSError as exc:
        status = "error"
        error = str(exc)
    duration_s = time.monotonic() - started
    return _write_storage_sample(project_id, owner_user_id, bytes_total, duration_s, status, error)


def _write_storage_sample(
    project_id: str, owner_user_id: str | None, bytes_total: int,
    duration_s: float, status: str, error: str | None,
) -> dict:
    sample_id = new_id("pss")
    sampled_at = now()

    def operation(conn: sqlite3.Connection) -> None:
        schema.ensure_tables_on_connection(conn)
        conn.execute(
            "INSERT INTO project_storage_samples("
            "id, project_id, owner_user_id, bytes_total, duration_s, status, error, sampled_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (sample_id, project_id, owner_user_id, int(bytes_total), duration_s, status, error, sampled_at),
        )

    db._run_write_transaction_once(operation)
    return {
        "id": sample_id, "project_id": project_id, "owner_user_id": owner_user_id,
        "bytes_total": int(bytes_total), "duration_s": duration_s,
        "status": status, "error": error, "sampled_at": sampled_at,
    }


# ---------------------------------------------------------------------------
# 查询：只读最近一次（或若干次）采样，零文件系统访问
# ---------------------------------------------------------------------------


def latest_sample(conn: sqlite3.Connection, project_id: str) -> dict | None:
    schema.ensure_tables_on_connection(conn)
    row = conn.execute(
        "SELECT * FROM project_storage_samples WHERE project_id=? "
        "ORDER BY sampled_at DESC LIMIT 1", (project_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _latest_bytes_per_project(conn: sqlite3.Connection, project_ids: list[str]) -> dict[str, dict]:
    """按 project_id 取各自最新一条采样（含 sampled_at，供「截至 X 时」展示）。
    ``GROUP BY ... HAVING MAX(sampled_at)`` 写法在并列时间戳下可能取到多行，
    这里改用子查询逐个取最新一行，项目数通常是几十到几百量级，可接受。"""
    result: dict[str, dict] = {}
    for project_id in project_ids:
        sample = latest_sample(conn, project_id)
        if sample is not None:
            result[project_id] = sample
    return result


def bytes_used_for_user_ids(conn: sqlite3.Connection, user_ids: list[str]) -> tuple[float, float | None]:
    """账号/团队/组织范围内全部项目最新采样字节数之和 + 最旧一次采样的
    ``sampled_at``（供 UI 标注「本次汇总截至最早 X 时的采样，之后的变化尚未
    反映」——取最旧而不是最新，是保守口径：只要范围内有一个项目采样落后，
    整个聚合数字就不能声称比那次采样更新）。范围内没有任何采样过的项目时返回
    ``(0.0, None)``。"""
    if not user_ids:
        return 0.0, None
    schema.ensure_tables_on_connection(conn)
    placeholders = ",".join("?" for _ in user_ids)
    rows = conn.execute(
        f"SELECT id FROM projects WHERE owner_user_id IN ({placeholders}) AND deleted_at IS NULL",
        user_ids,
    ).fetchall()
    project_ids = [str(r["id"]) for r in rows]
    samples = _latest_bytes_per_project(conn, project_ids)
    if not samples:
        return 0.0, None
    total = sum(float(s["bytes_total"]) for s in samples.values())
    oldest_sampled_at = min(float(s["sampled_at"]) for s in samples.values())
    return total, oldest_sampled_at


def bytes_used_for_project(conn: sqlite3.Connection, project_id: str) -> tuple[float, float | None]:
    sample = latest_sample(conn, project_id)
    if sample is None:
        return 0.0, None
    return float(sample["bytes_total"]), float(sample["sampled_at"])


def top_projects_by_storage(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """全库按最新采样字节数排名前 N 的项目——供用量看板「资源」页与超限时的
    「看看谁占用最多」入口共用。软删除项目不参与排名（已经在回收站，不该占
    治理注意力）。"""
    schema.ensure_tables_on_connection(conn)
    limit = max(1, min(int(limit), _MAX_CLEANUP_CANDIDATE_LIMIT))
    rows = conn.execute(
        """SELECT s.project_id AS project_id, s.bytes_total AS bytes_total, s.sampled_at AS sampled_at
             FROM project_storage_samples s
             JOIN (
                 SELECT project_id, MAX(sampled_at) AS max_sampled_at
                   FROM project_storage_samples GROUP BY project_id
             ) latest ON latest.project_id = s.project_id AND latest.max_sampled_at = s.sampled_at
             JOIN projects p ON p.id = s.project_id
            WHERE p.deleted_at IS NULL
            ORDER BY s.bytes_total DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {"project_id": r["project_id"], "bytes_total": float(r["bytes_total"]), "sampled_at": r["sampled_at"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 清理候选与执行：超限时给出路，不自动删
# ---------------------------------------------------------------------------


def cleanup_candidates(
    conn: sqlite3.Connection, project_id: str, limit: int = DEFAULT_CLEANUP_CANDIDATE_LIMIT,
) -> list[dict]:
    """本项目「未被采纳」的最大 N 个成功版本——判据从数据推导：
    ``shots.adopted_version_id`` 不指向这一行（或该镜压根没有采用任何版本），
    且状态是 ``succeeded``（有真实文件，不是占位/失败行）。只对候选版本各自
    ``stat()``（有限条数，不是整树遍历），请求路径内可接受。磁盘上已经不存
    在的文件（``stat()`` 失败）跳过——不能建议用户删一个不存在的东西。"""
    limit = max(1, min(int(limit), _MAX_CLEANUP_CANDIDATE_LIMIT))
    rows = conn.execute(
        """SELECT v.id AS version_id, v.shot_id AS shot_id, v.video_path AS video_path,
                  v.created_at AS created_at, s.shot_no AS shot_no
             FROM shot_versions v
             JOIN shots s ON s.id = v.shot_id
             JOIN episodes e ON e.id = s.episode_id
            WHERE e.project_id=? AND v.status='succeeded'
              AND v.video_path IS NOT NULL AND v.video_path != ''
              AND (s.adopted_version_id IS NULL OR s.adopted_version_id != v.id)""",
        (project_id,),
    ).fetchall()
    candidates: list[dict] = []
    for row in rows:
        path = Path(row["video_path"])
        try:
            size = path.stat().st_size
        except OSError:
            continue
        candidates.append({
            "version_id": row["version_id"], "shot_id": row["shot_id"], "shot_no": row["shot_no"],
            "video_path": row["video_path"], "bytes": size, "created_at": row["created_at"],
        })
    candidates.sort(key=lambda item: -item["bytes"])
    return candidates[:limit]


def _classify_cleanup_targets(conn: sqlite3.Connection, project_id: str, version_ids: list[str]) -> tuple[list[str], dict[str, str], list[dict]]:
    """逐条校验属于本项目、非当前采纳版本、状态终态。返回
    ``(允许删除的 version_id 列表, {version_id: video_path}, 跳过清单)``——
    防御性校验，不信任调用方已经在 UI 侧做过滤，拒绝清单中任何一个当前被采纳
    的版本。"""
    placeholders = ",".join("?" for _ in version_ids)
    rows = conn.execute(
        f"""SELECT v.id AS version_id, v.video_path AS video_path, v.status AS status,
                   s.adopted_version_id AS adopted_version_id
              FROM shot_versions v
              JOIN shots s ON s.id = v.shot_id
              JOIN episodes e ON e.id = s.episode_id
             WHERE e.project_id=? AND v.id IN ({placeholders})""",
        (project_id, *version_ids),
    ).fetchall()
    by_id = {str(r["version_id"]): r for r in rows}
    to_delete: list[str] = []
    skipped: list[dict] = []
    for version_id in version_ids:
        row = by_id.get(version_id)
        if row is None:
            skipped.append({"version_id": version_id, "reason": "不属于该项目或不存在"})
        elif row["adopted_version_id"] == version_id:
            skipped.append({"version_id": version_id, "reason": "当前正被采纳，拒绝清理"})
        elif row["status"] != "succeeded":
            skipped.append({"version_id": version_id, "reason": f"状态不是 succeeded（{row['status']}）"})
        else:
            to_delete.append(version_id)
    paths = {vid: by_id[vid]["video_path"] for vid in to_delete}
    return to_delete, paths, skipped


def _unlink_cleanup_files(to_delete: list[str], paths: dict[str, str]) -> tuple[list[str], int]:
    """逐个 unlink 已经在 DB 里标记为 ``rejected_cleanup`` 的文件；单个文件
    失败只记录不中断（文件已被业务判定可丢弃，磁盘残留是运维层面的空间没收
    回，不是正确性问题）。"""
    bytes_freed = 0
    deleted: list[str] = []
    for version_id in to_delete:
        path_str = paths.get(version_id)
        if path_str:
            path = Path(path_str)
            try:
                size = path.stat().st_size
                path.unlink()
                bytes_freed += size
            except OSError as exc:
                from app.errors import log_error  # 延迟导入：app.errors 反向依赖 db/quota 链
                log_error(exc, action="quota_policy.storage.execute_cleanup", context={"version_id": version_id})
        deleted.append(version_id)
    return deleted, bytes_freed


def execute_cleanup(conn: sqlite3.Connection, project_id: str, version_ids: list[str], *, actor: str) -> dict:
    """用户确认后执行清理：先提交 DB 状态变更（``status='rejected_cleanup'``
    + 清空 ``video_path``），确认新状态落盘成功后才 unlink 磁盘文件
    （CLAUDE.md「破坏性操作要有原子性」——反过来「先删文件再改库」会在中途崩
    溃时留下「DB 说还在，磁盘已经没了」的假态，比现在这个顺序更危险）。"""
    if not version_ids:
        return {"deleted": [], "skipped": [], "bytes_freed": 0}
    to_delete, paths, skipped = _classify_cleanup_targets(conn, project_id, version_ids)
    if to_delete:
        del_placeholders = ",".join("?" for _ in to_delete)
        conn.execute(
            f"UPDATE shot_versions SET status='rejected_cleanup', "
            f"error=COALESCE(NULLIF(error,''), '用户已确认清理，释放存储空间'), video_path=NULL "
            f"WHERE id IN ({del_placeholders})",
            to_delete,
        )
        conn.commit()
    deleted, bytes_freed = _unlink_cleanup_files(to_delete, paths)
    return {"deleted": deleted, "skipped": skipped, "bytes_freed": bytes_freed, "actor": actor}


# ---------------------------------------------------------------------------
# 定时采样巡检——与 app.recovery 里其它 sweep loop 同一种调度形态
# （task_registry.spawn 起常驻协程，见 app/main.py::lifespan），本仓已有的
# 唯一周期任务机制，不引入 APScheduler 之类的新依赖。
# ---------------------------------------------------------------------------


def _active_projects(conn: sqlite3.Connection) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        "SELECT id, owner_user_id FROM projects WHERE deleted_at IS NULL",
    ).fetchall()
    return [(str(r["id"]), (str(r["owner_user_id"]) if r["owner_user_id"] else None)) for r in rows]


async def sample_all_projects_once() -> dict:
    """给全部未软删除项目各采样一次；单个项目失败不影响其余项目（各自独立
    try/except，理由与 ``project_recycle_bin_sweep_loop`` 的既有注释一致）。"""
    conn = db.get_conn()
    projects = _active_projects(conn)
    ok = 0
    failed = 0
    for project_id, owner_user_id in projects:
        try:
            result = await sample_project_storage(
                project_id, owner_user_id, config.PROJECTS_DIR / project_id,
            )
            if result["status"] == "ok":
                ok += 1
            else:
                failed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 巡检循环自身不得因单个项目坏数据退出
            from app.errors import log_error
            log_error(exc, action="quota_policy.storage.sample_all_projects_once", context={"project_id": project_id})
            failed += 1
    return {"total": len(projects), "ok": ok, "failed": failed}


async def storage_sample_sweep_loop(interval_s: float = DEFAULT_SWEEP_INTERVAL_S) -> None:
    """周期性给全部项目采样一次存储占用。与 ``app.recovery`` 那几个 sweep loop
    同一种调度形态与容错策略：单轮失败不让循环本身退出，睡眠区间夹在
    [60s, 21600s]（存储不是抢时间的操作，默认 1 小时一轮已经足够及时，但也
    不该允许调用方传一个荒谬小的 interval_s 压成忙轮询）。"""
    while True:
        try:
            await sample_all_projects_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            from app.errors import log_error
            log_error(exc, action="storage_sample_sweep_loop", context={"interval_s": interval_s})
        await asyncio.sleep(max(60.0, min(float(interval_s), 21600.0)))
