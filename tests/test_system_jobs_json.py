"""``GET /api/system/jobs`` 必须始终能被 ``json.dumps(allow_nan=False)`` 序列化。

真实故障（2026-09-01）：成本预算拦截体系退场后 ``episode_video_budget_limit()``
恒返回 ``math.inf`` 哨兵（见 ``app/media_exec/common.py``），这个值经
``persist_new_video_version`` 写进了 ``workflow_runs.budget_limit_cny``。
``jobs_overview()`` 用 ``SELECT wr.*`` 把整行原样吐进响应，Starlette 的
``JSONResponse`` 用标准库 ``json.dumps``（默认 ``allow_nan=True`` 会放行
``inf``/``nan``，但走的是 FastAPI 的 ``JSONResponse``——它继承 Starlette，
Starlette 显式传 ``allow_nan=False`` 让不合规的浮点值直接抛
``ValueError``），于是任何一个后端运行中出现过视频生成的实例，
``/api/system/jobs`` 都会 500。

修复：``jobs_overview()`` 在拼装每个 run 行时把这个已退场、无业务含义的字段
从响应里摘掉，而不是留着让它随时炸响应。本测试直接构造一条
``budget_limit_cny=inf`` 的 workflow_runs 记录复现故障，并断言修复后的
响应能被 ``json.dumps(..., allow_nan=False)`` 序列化——回归到「摘字段」这个
具体动作，而不只是宽泛地断言状态码。
"""
from __future__ import annotations

import json
import math
import sqlite3

from app import system_api


def _conn_with_inf_budget_run() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs(
            id TEXT, kind TEXT, shot_id TEXT, version_id TEXT, episode_id TEXT,
            project_id TEXT, status TEXT, error TEXT, created_at REAL, updated_at REAL,
            run_id TEXT
        );
        CREATE TABLE shots(id TEXT, episode_id TEXT, shot_no INTEGER);
        CREATE TABLE projects(id TEXT, name TEXT);
        CREATE TABLE episodes(
            id TEXT, project_id TEXT, episode_no INTEGER, title TEXT,
            screenplay_status TEXT, screenplay_error TEXT, screenplay_started_at REAL,
            screenplay_updated_at REAL, created_at REAL
        );
        CREATE TABLE workflow_runs(
            id TEXT, workflow_type TEXT, scope_type TEXT, scope_id TEXT,
            status TEXT, current_step_key TEXT, updated_at REAL,
            failure_message TEXT, recovered_by_run_id TEXT, budget_limit_cny REAL,
            cost_cny REAL
        );
        INSERT INTO projects VALUES('p1', '测试项目');
        INSERT INTO episodes VALUES(
            'e1', 'p1', 1, '第一集', 'ready', NULL, 100, 120, 10
        );
    """)
    conn.execute(
        """INSERT INTO workflow_runs VALUES(
               'run_video', 'video_generation', 'episode', 'e1',
               'RUNNING', 'video_generation', 200, NULL, NULL, ?, 0
           )""",
        (math.inf,),
    )
    conn.commit()
    return conn


def test_jobs_overview_response_is_json_serializable_when_budget_limit_is_inf(
    monkeypatch,
) -> None:
    conn = _conn_with_inf_budget_run()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)

    result = system_api.jobs_overview()

    # 退场字段必须整个消失，而不是被 None/0 兜底藏起来。
    run_row = next(row for row in result["recent"] if row["id"] == "run_video")
    assert "budget_limit_cny" not in run_row

    # 核心回归判据：这就是 Starlette JSONResponse 实际使用的序列化调用，
    # 之前会在这里抛 ``ValueError: Out of range float values are not JSON
    # compliant: inf``。
    json.dumps(result, allow_nan=False)


def test_query_jobs_reuses_the_fixed_payload(monkeypatch) -> None:
    """``/system/jobs/query`` 内部直接调用 ``jobs_overview()`` 复用同一份数据。"""
    conn = _conn_with_inf_budget_run()
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)

    # query_jobs() 的 page/page_size 形参默认值是 FastAPI 的 Query(...) 哨兵，
    # 只有经由路由依赖注入才会被解析成 int；直接函数调用必须显式传值。
    query_result = system_api.query_jobs(page=1, page_size=20)
    run_item = next(item for item in query_result["items"] if item["id"] == "run_video")
    assert "budget_limit_cny" not in run_item
    json.dumps(query_result, allow_nan=False)
