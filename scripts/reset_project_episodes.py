#!/usr/bin/env python3
"""把【一个项目的指定集号区间】清回「小说已导入、分集已规划」，供一次性全链路重跑。

与 ``scripts/reset_pipeline_data.py`` 的分工：那一份是**全库**整表清空，判据反着
写（列保留清单、其余一律清掉），完整但不分项目——库里同时存在别的项目时跑它
等于连别人的作品一起毁掉。本脚本反过来：只动一个项目、只动指定集号，代价是
必须逐条给出归属路径，而枚举归属路径正是容易漏的地方。

**因此本脚本的判据不是「我删了哪些表」，而是清完之后逐表查残留断言为 0**——
漏删一张表会让上一轮的产物混进这一轮、看起来像新产出，而这恰好会毁掉「一次性
从零跑通十集」这个结论本身。删除清单可能不全，验证清单必须全，两者独立。

分集级产出走产品自己的清除端点（顺带在测真实用户路径），项目级视觉资产没有
对应端点、只能走 SQL。

用法：
    py scripts/reset_project_episodes.py --project 我欲封天 --from 1 --to 10 --dry-run
    py scripts/reset_project_episodes.py --project 我欲封天 --from 1 --to 10
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402
from scripts.session_token import WITH_LOCAL_SECRET, session_token  # noqa: E402

BASE = "http://127.0.0.1:8230"
DB_PATH = str(config.DB_PATH)


def log(msg: str) -> None:
    print(msg, flush=True)


def call(method: str, path: str, body: dict | None = None,
         headers: dict | None = None, timeout: int = 180) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("X-Manju-Session", session_token(WITH_LOCAL_SECRET))
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:400]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": repr(exc)}


def approved(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """命令总线的两段式确认：先回 202 + approval_token，带同名头重放才真正执行。"""
    code, resp = call(method, path, body)
    token = resp.get("approval_token") if isinstance(resp, dict) else None
    if token:
        code, resp = call(method, path, body,
                          headers={"x-manju-approval-token": token})
    return code, resp


def readonly_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_project(selector: str) -> tuple[str, str]:
    """按名字或 id 解析，回收站里的项目不参与按名匹配（同名软删项目会让解析歧义）。"""
    conn = readonly_conn()
    try:
        rows = conn.execute(
            "SELECT id, name FROM projects"
            " WHERE id=? OR (name=? AND deleted_at IS NULL) ORDER BY id",
            (selector, selector),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise SystemExit(f"库里找不到项目：{selector!r}")
    if len(rows) > 1:
        matched = "、".join(f"{r['id']}({r['name']})" for r in rows)
        raise SystemExit(f"项目 {selector!r} 命中多个，请改用 id：{matched}")
    return rows[0]["id"], rows[0]["name"]


def collect_scope(conn: sqlite3.Connection, project_id: str,
                  ep_from: int, ep_to: int) -> dict[str, list[str]]:
    """先把要清的 id 集合全部快照下来——删完之后父行不在了，就再也 join 不出
    孤儿了，验证必须拿删除前的 id 逐个去查"还在不在"。"""
    eps = [r["id"] for r in conn.execute(
        "SELECT id FROM episodes WHERE project_id=? AND episode_no BETWEEN ? AND ?"
        " ORDER BY episode_no", (project_id, ep_from, ep_to))]
    if not eps:
        raise SystemExit(f"项目 {project_id} 在 EP{ep_from}-EP{ep_to} 没有分集")
    ph = ",".join("?" * len(eps))
    shots = [r["id"] for r in conn.execute(
        f"SELECT id FROM shots WHERE episode_id IN ({ph})", eps)]
    refsets = _in_query(conn, "SELECT id FROM reference_sets WHERE shot_id IN", shots)
    portraits = [r["id"] for r in conn.execute(
        "SELECT id FROM character_portraits WHERE project_id=?", (project_id,))]
    scene_refs = [r["id"] for r in conn.execute(
        "SELECT id FROM scene_references WHERE project_id=?", (project_id,))]
    # 2026-09-05：物件库参考图与连播任务也是流水线产物（09-04 新增的表），一并纳入快照与残留验证。
    props = [r["id"] for r in conn.execute(
        "SELECT id FROM prop_references WHERE project_id=?", (project_id,))]
    series_tasks = [r["id"] for r in conn.execute(
        "SELECT id FROM series_tasks WHERE project_id=?", (project_id,))]
    scopes = [project_id, *eps, *shots]
    runs = _in_query(conn, "SELECT id FROM workflow_runs WHERE scope_id IN", scopes)
    arts = _in_query(conn, "SELECT id FROM artifacts WHERE scope_id IN", scopes)
    return {
        "episodes": eps, "shots": shots, "reference_sets": refsets,
        "portraits": portraits, "scene_references": scene_refs,
        "props": props, "series_tasks": series_tasks,
        "workflow_runs": runs, "artifacts": arts,
    }


def _in_query(conn: sqlite3.Connection, prefix: str, values: list[str]) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for i in range(0, len(values), 500):
        chunk = values[i:i + 500]
        ph = ",".join("?" * len(chunk))
        out += [r[0] for r in conn.execute(f"{prefix} ({ph})", chunk)]
    return out


# 验证用：表名 -> 该表引用上面某个 id 集合的列名。删除清单可能不全，这份
# 必须全——每加一张持有流水线产出的表都要在这里登记，漏登记就是漏验证。
RESIDUE_PROBES: list[tuple[str, str, str]] = [
    ("prop_references", "id", "props"),
    ("series_tasks", "id", "series_tasks"),
    ("shots", "episode_id", "episodes"),
    ("screenplay_drafts", "episode_id", "episodes"),
    ("storyboard_source_bindings", "shot_id", "shots"),
    ("storyboard_workspace_state", "episode_id", "episodes"),
    ("storyboard_action_previews", "episode_id", "episodes"),
    ("shot_versions", "shot_id", "shots"),
    ("shot_scenes", "shot_id", "shots"),
    ("shot_video_generation_plans", "shot_id", "shots"),
    ("video_boundary_assets", "shot_id", "shots"),
    ("video_plan_dependencies", "shot_id", "shots"),
    ("reference_sets", "shot_id", "shots"),
    ("reference_assets", "reference_set_id", "reference_sets"),
    ("character_portraits", "id", "portraits"),
    ("character_portrait_views", "portrait_id", "portraits"),
    ("scene_references", "id", "scene_references"),
    ("scene_reference_views", "scene_reference_id", "scene_references"),
    ("workflow_runs", "id", "workflow_runs"),
    ("run_events", "run_id", "workflow_runs"),
    ("step_runs", "run_id", "workflow_runs"),
    ("artifacts", "id", "artifacts"),
    ("evaluations", "artifact_id", "artifacts"),
    ("episode_video_generation_plans", "episode_id", "episodes"),
    # 刻意不清 episode_video_budget_authorities / provider_video_budget_claims：
    # 那是已经产生的付款责任，属于账目而不是流水线产出（与 reset_pipeline_data
    # 把 payment_orders / quota_ledger 归入保留是同一条界线）。认领是 settled 的
    # 那笔钱真的花掉了，删掉等于篡改账；授权行是它的上限记录，一起留着。
    ("video_budget_authorization_receipts", "episode_id", "episodes"),
    ("delivery_packages", "episode_id", "episodes"),
    ("concat_operation_receipts", "episode_id", "episodes"),
]


def verify_clean(conn: sqlite3.Connection, scope: dict[str, list[str]]) -> list[str]:
    """逐条查删除前快照的 id 现在还在不在；返回残留描述，空列表即干净。"""
    residue: list[str] = []
    for table, column, key in RESIDUE_PROBES:
        ids = scope.get(key) or []
        if not ids:
            continue
        rows = _in_query(conn, f"SELECT {column} FROM {table} WHERE {column} IN", ids)
        if rows:
            residue.append(f"{table}.{column} 残留 {len(rows)} 行")
    return residue


ACTIVE_RUN_STATUSES = ("CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN",
                       "WAITING_AUTHORIZATION", "PAUSED_BUDGET", "PAUSED_EXTERNAL")


#: 服务重启后没有任何进程持有的运行状态：暂停/等待重试/等待人工/等待授权。它们不是在途，
#: 是上一轮进程留下的壳；``--inert-paused`` 把它们排除在「在途」判定之外（清扫时会一并删掉）。
INERT_AFTER_RESTART = ("PAUSED_EXTERNAL", "WAITING_RETRY", "WAITING_HUMAN", "WAITING_AUTHORIZATION")


def assert_idle(conn: sqlite3.Connection, scope: dict[str, list[str]],
                *, dry_run: bool, inert_paused: bool = False) -> None:
    """目标范围里还有在途运行就拒绝执行——清到一半被写回来的残留最难查。

    ``--dry-run`` 只警告不退出：预览的价值就在于先看清要动什么，被在途任务挡住
    反而什么都看不到。
    """
    scopes = scope["episodes"] + scope["shots"]
    if not scopes:
        return
    ph = ",".join("?" * len(scopes))
    statuses = [x for x in ACTIVE_RUN_STATUSES if not (inert_paused and x in INERT_AFTER_RESTART)]
    st = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT id, workflow_type, status FROM workflow_runs"
        f" WHERE scope_id IN ({ph}) AND status IN ({st})",
        (*scopes, *statuses),
    ).fetchall()
    if rows:
        detail = "、".join(f"{r['id']}({r['workflow_type']}/{r['status']})" for r in rows)
        if dry_run:
            log(f"[警告] 目标范围内有在途运行，真正执行前要先停掉：{detail}")
            return
        raise SystemExit(f"目标范围内有在途运行，先停掉再清：{detail}")


def clear_episode_via_api(eid: str, label: str) -> list[str]:
    """分集级产出走产品自己的清除端点，顺带在测真实用户路径。"""
    problems: list[str] = []
    steps = [
        ("POST", f"/api/episodes/{eid}/clear-artifacts", None, "参考图/视频/分析"),
        ("STORYBOARD", f"/api/episodes/{eid}/storyboard/clear", None, "分镜"),
        ("DELETE", f"/api/episodes/{eid}/screenplay", None, "剧本"),
    ]
    for method, path, body, what in steps:
        if method == "STORYBOARD":
            code, resp = _clear_storyboard(eid)
        else:
            code, resp = approved(method, path, body)
        ok = code in (200, 202, 204, 404)
        log(f"  {label} 清{what} -> HTTP{code}"
            f"{'' if ok else ' ' + json.dumps(resp, ensure_ascii=False)[:200]}")
        if not ok:
            problems.append(f"{label} 清{what} HTTP{code}")
    return problems


def _clear_storyboard(eid: str) -> tuple[int, dict]:
    """分镜清空是两段式：先取影响预览的 preview_token，再带着它执行。

    少了第一段一律 428「请先查看并批准最新影响预览」——这是给人看影响面的
    设计，不是可以绕开的形式。已经没有分镜可清时预览会拒绝，那种拒绝按"本来
    就是空的"处理，不算失败。
    """
    code, resp = approved("POST", f"/api/episodes/{eid}/storyboard/clear-preview")
    token = resp.get("preview_token") if isinstance(resp, dict) else None
    if not token:
        if code in (409, 422) and "没有" in json.dumps(resp, ensure_ascii=False):
            return 404, resp
        return code, resp
    return approved("POST", f"/api/episodes/{eid}/storyboard/clear",
                    {"preview_token": token, "reason": "全链路重跑前清空"})


PROJECT_ASSET_SWEEPS = [
    ("character_portrait_views",
     "DELETE FROM character_portrait_views WHERE portrait_id IN"
     " (SELECT id FROM character_portraits WHERE project_id=?)"),
    ("character_portraits", "DELETE FROM character_portraits WHERE project_id=?"),
    ("scene_reference_views",
     "DELETE FROM scene_reference_views WHERE scene_reference_id IN"
     " (SELECT id FROM scene_references WHERE project_id=?)"),
    ("scene_references", "DELETE FROM scene_references WHERE project_id=?"),
    ("character_payment_quotes",
     "DELETE FROM character_payment_quotes WHERE project_id=?"),
    ("visual_entity_merges", "DELETE FROM visual_entity_merges WHERE project_id=?"),
    ("prop_references", "DELETE FROM prop_references WHERE project_id=?"),
    ("series_queue_state", "DELETE FROM series_queue_state WHERE project_id=?"),
]
#: 项目媒体目录里全部是流水线产物（定妆照/场景图/物件图/分集视频/连播成片），小说原文在库里的
#: chapters 表，不在文件系统——整目录删。只在范围覆盖全部有产出分集时执行（与视觉资产同一判据）。
PROJECT_MEDIA_DIRS = ("episodes", "refs", "scene_refs", "prop_refs", "series")


def purge_project_media(project_id: str) -> None:
    root = Path(config.PROJECTS_DIR) / project_id
    for name in PROJECT_MEDIA_DIRS:
        target = root / name
        if not target.exists():
            continue
        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        shutil.rmtree(target)
        log(f"  删目录 {target.relative_to(Path(config.PROJECTS_DIR))}（{size / 1e6:.1f} MB）")

# 画风是用户选的运维配置，重跑不该把它清掉（清了会跑在默认画风上）；
# 其余状态字段回 idle，让人物谱/场景库回到"未生成"。
PROJECT_STATUS_RESET = {
    "bible_status": "idle", "bible_error": None, "refs_status": "idle",
    "portraits_status": "idle", "scene_refs_status": "idle",
}


def sweep_orchestration_residue(conn: sqlite3.Connection,
                                scope: dict[str, list[str]]) -> None:
    """产品的清除端点只管流水线产出，不动编排与产物记录；这里做兜底清扫。

    留着它们"从零重跑"就不成立：``artifacts`` 里装着分镜/剧本产物与视频补齐
    checkpoint，而过期 checkpoint 正是让整个视频阶段被跳过的那个 bug 的宿主。

    删除顺序直接取 ``RESIDUE_PROBES`` 的逆序——那份表里父表在前、子表在后，
    倒过来正好是子表先删。不另写一份删除清单：两份清单迟早会分叉，而分叉的
    那一条就是漏网的残留。
    """
    for table, column, key in reversed(RESIDUE_PROBES):
        ids = scope.get(key) or []
        if not ids:
            continue
        removed = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            removed += conn.execute(
                f"DELETE FROM {table} WHERE {column} IN ({ph})", chunk).rowcount
        if removed:
            log(f"  兜底清 {table}.{column}：{removed} 行")
    conn.commit()



def _range_covers_all_produced_episodes(
    conn: sqlite3.Connection, project_id: str, scope: dict[str, list[str]],
) -> bool:
    """本次集号范围是否覆盖了项目里全部"还有产出"的分集。

    判据挂产物信号而不是集号区间：有 shots 的分集才算有产出，范围外还有这样
    的分集就说明清项目级资产会越界。
    """
    produced = {
        str(row["episode_id"]) for row in conn.execute(
            """SELECT DISTINCT e.id AS episode_id
                 FROM episodes e JOIN shots s ON s.episode_id=e.id
                WHERE e.project_id=?""",
            (project_id,),
        )
    }
    return not (produced - set(scope["episodes"]))


def _reset_bible_keeping_style(conn: sqlite3.Connection, project_id: str) -> None:
    """人物谱只保留 ``world``（里面装着用户选的画风），清掉角色与场景。

    新架构下角色/场景都由映射台按需发现，它们是流水线产物；只重置状态字段而
    留着 bible_json 会造出"谱里有角色、定妆照表里没有"的错位（那正是手动补图
    路径看不见任何角色的那个缺陷的形态），而且下一轮映射会把它们当成已有候选，
    "从零重跑"就不成立。
    """
    row = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    try:
        bible = json.loads((row["bible_json"] if row else "") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        bible = {}
    kept = {"characters": [], "scenes": []}
    if isinstance(bible.get("world"), dict):
        kept["world"] = bible["world"]
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps(kept, ensure_ascii=False), project_id),
    )
    log(f"  人物谱重置：角色 {len(bible.get('characters') or [])} → 0，"
        f"场景 {len(bible.get('scenes') or [])} → 0（画风保留）")


def purge_project_assets(conn: sqlite3.Connection, project_id: str) -> None:
    """项目级视觉资产没有对应的清除端点，只能走 SQL；一律按 project_id 收敛。"""
    for table, sql in PROJECT_ASSET_SWEEPS:
        cur = conn.execute(sql, (project_id,))
        log(f"  SQL 清 {table}：{cur.rowcount} 行")
    _reset_bible_keeping_style(conn, project_id)
    sets = ", ".join(f"{k}=?" for k in PROJECT_STATUS_RESET)
    conn.execute(f"UPDATE projects SET {sets} WHERE id=?",
                 (*PROJECT_STATUS_RESET.values(), project_id))
    conn.commit()


def report_scope(scope: dict[str, list[str]], project_id: str, name: str,
                 ep_from: int, ep_to: int) -> None:
    log(f"目标项目：{name}（{project_id}），集号 EP{ep_from}-EP{ep_to}")
    for key, ids in scope.items():
        log(f"  {key}: {len(ids)} 条")
    log("保留：chapters、episodes 行本身、projects 行（含已选画风）、"
        "provider_calls、账号与配额。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="项目名或项目 id")
    parser.add_argument("--from", dest="ep_from", type=int, default=1)
    parser.add_argument("--to", dest="ep_to", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="只预览，不修改")
    parser.add_argument("--inert-paused", action="store_true",
                        help="服务刚重启、无进程持有运行时，把暂停/等待态的运行行视为壳而不是在途")
    args = parser.parse_args()

    project_id, name = resolve_project(args.project)
    ro = readonly_conn()
    try:
        scope = collect_scope(ro, project_id, args.ep_from, args.ep_to)
        assert_idle(ro, scope, dry_run=args.dry_run, inert_paused=args.inert_paused)
    finally:
        ro.close()
    report_scope(scope, project_id, name, args.ep_from, args.ep_to)
    if args.dry_run:
        log("--dry-run：以上都没有执行。")
        return 0

    problems: list[str] = []
    for idx, eid in enumerate(scope["episodes"], start=args.ep_from):
        problems += clear_episode_via_api(eid, f"EP{idx}")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # 项目级视觉资产（定妆照/场景图）按 project_id 收敛，没有集号维度：
        # 只清一部分集号时清掉它们会越界毁掉范围外分集的资产绑定——实测
        # 只清 EP1 却把 EP2-EP10 的定妆照一起清了。因此只有当本次范围覆盖
        # 了项目里全部有产出的分集时才清；否则跳过并说明，由调用方决定是不是
        # 要扩大范围。
        if _range_covers_all_produced_episodes(conn, project_id, scope):
            purge_project_assets(conn, project_id)
            purge_project_media(project_id)
        else:
            log("  跳过项目级视觉资产：本次范围之外还有已产出的分集，"
                "清掉定妆照/场景图会越界（要清就把范围扩到全部分集）")
        sweep_orchestration_residue(conn, scope)
    finally:
        conn.close()

    ro = readonly_conn()
    try:
        residue = verify_clean(ro, scope)
    finally:
        ro.close()
    # 退出码只挂产物信号——逐表验证有没有残留。单步的非 2xx 不参与判定：
    # 清除是幂等动作，"本集没有可删除的剧本""没有分镜可清"这类响应恰恰说明
    # 目标已经是干净的，把它们算成失败会让退出码永远为 1、从而失去意义。
    # 但仍然如实打印出来：某一步真的失败时，它会同时表现为残留。
    if problems:
        log("[非 2xx 的清除步骤，仅供排查] " + "；".join(problems))
    if residue:
        log("[未清干净] " + "；".join(residue))
        return 1
    log("清除完成，逐表验证无残留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
