"""``scripts/smoke_live_routes*.py`` 的纯函数测试：路径/query 参数解析、
判据分类、index.html 资产抽取、``/media`` URL 百分号编码。不连接真实后端、
不在模块级读凭证文件；数据库相关测试都用本文件自建的内存 sqlite3 连接。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.smoke_live_routes import classify_result, extract_asset_refs
from scripts.smoke_live_routes_params import (
    append_query, build_url, path_param_names, quote_media_url_path, resolve_query_params,
    resolve_route_params,
)

# path_param_names / build_url

def test_path_param_names_extracts_all_braced_segments() -> None:
    assert path_param_names("/api/episodes/{episode_id}") == ["episode_id"]
    assert path_param_names(
        "/api/projects/{project_id}/observability/calls/{call_id}/content/{index}"
    ) == ["project_id", "call_id", "index"]
    assert path_param_names("/api/settings") == []  # 静态路由无参数

def test_build_url_substitutes_by_name_not_position() -> None:
    url = build_url(
        "/api/projects/{project_id}/chapters/{idx}",
        {"idx": 3, "project_id": "proj_abc"},
    )
    assert url == "/api/projects/proj_abc/chapters/3"

def test_build_url_quotes_special_characters_in_normal_params() -> None:
    url = build_url("/api/video-capabilities/{provider}/{model}", {"provider": "a:b", "model": "m"})
    assert url == "/api/video-capabilities/a%3Ab/m"  # 冒号须整体转义
    assert build_url("/media/{path}", {"path": "proj_x/refs/a.jpg"}) == "/media/proj_x/refs/a.jpg"  # 斜杠保留

# resolve_route_params：内存 sqlite3 模拟真实表结构，不碰真实数据库

@pytest.fixture
def conn() -> sqlite3.Connection:  # 供 resolve_route_params 与 resolve_query_params 共用
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE projects(id TEXT, deleted_at REAL);
        CREATE TABLE chapters(project_id TEXT, idx INTEGER);
        CREATE TABLE provider_calls(id INTEGER, project_id TEXT);
        CREATE TABLE artifacts(id TEXT, scope_type TEXT, scope_id TEXT, file_path TEXT);
        CREATE TABLE workflow_runs(id TEXT); CREATE TABLE jobs(id TEXT); CREATE TABLE episodes(id TEXT);
        """
    )
    return connection

def test_resolve_route_params_no_params_returns_single_empty_row(conn: sqlite3.Connection) -> None:
    assert resolve_route_params(conn, [], 1, Path("/tmp")) == ([{}], None)

def test_resolve_route_params_joint_project_id_excludes_soft_deleted(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO projects(id, deleted_at) VALUES ('proj_live', NULL)")
    conn.execute("INSERT INTO projects(id, deleted_at) VALUES ('proj_deleted', 123.0)")
    rows, reason = resolve_route_params(conn, ["project_id"], 5, Path("/tmp"))
    assert reason is None
    assert rows == [{"project_id": "proj_live"}]

def test_resolve_route_params_joint_project_and_idx_pairs_consistently(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO chapters(project_id, idx) VALUES ('proj_a', 7)")
    conn.execute("INSERT INTO chapters(project_id, idx) VALUES ('proj_b', 2)")
    rows, reason = resolve_route_params(conn, ["project_id", "idx"], 1, Path("/tmp"))
    assert reason is None
    assert len(rows) == 1
    assert set(rows[0]) == {"project_id", "idx"}  # 必须同一行取出，不能各自独立挑选

def test_resolve_route_params_simple_lookup_for_single_param(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO provider_calls(id, project_id) VALUES (42, 'proj_a')")
    rows, reason = resolve_route_params(conn, ["call_id"], 1, Path("/tmp"))
    assert reason is None
    assert rows == [{"call_id": 42}]

@pytest.mark.parametrize(
    "names,expected_reason_fragment",
    [
        (["call_id"], "表为空"),  # 名字认识，但 provider_calls 表没有任何数据
        (["project_id", "object_type", "object_id"], "超出通用表/列映射范围"),
    ],
)
def test_resolve_route_params_skip_reasons(conn: sqlite3.Connection, names, expected_reason_fragment) -> None:
    rows, reason = resolve_route_params(conn, names, 1, Path("/tmp"))
    assert rows is None
    assert expected_reason_fragment in reason

def test_resolve_route_params_media_path_relative_to_projects_dir(conn: sqlite3.Connection, tmp_path) -> None:
    projects_dir = tmp_path / "projects"
    abs_path = projects_dir / "proj_x" / "refs" / "a.jpg"
    conn.execute(
        "INSERT INTO artifacts(id, scope_type, scope_id, file_path) VALUES "
        "('art_1', 'reference_asset', 'x', ?)", (str(abs_path),),
    )
    rows, reason = resolve_route_params(conn, ["path"], 1, projects_dir)
    assert reason is None
    assert rows == [{"path": "proj_x/refs/a.jpg"}]

# classify_result

@pytest.mark.parametrize(
    "status,expected_outcome",
    [
        (200, "PASS"), (201, "PASS"), (299, "PASS"), (404, "OPTIONAL_404"),
        (401, "FAIL"), (403, "FAIL"), (422, "FAIL"), (500, "FAIL"), (503, "FAIL"), (0, "FAIL"),
    ],
)
def test_classify_result_outcome(status: int, expected_outcome: str) -> None:
    outcome, _ = classify_result(status)
    assert outcome == expected_outcome

def test_classify_result_detail_messages() -> None:
    assert classify_result(0)[1] == "连接异常"
    assert "可选资源" in classify_result(404)[1]

# extract_asset_refs

def test_extract_asset_refs_picks_up_script_and_stylesheet() -> None:
    html = (
        '<script type="module" crossorigin src="/assets/index-C59J50Z9.js"></script>'
        '<link rel="stylesheet" crossorigin href="/assets/index-aaJG_DoN.css">'
    )
    assert extract_asset_refs(html) == ["/assets/index-C59J50Z9.js", "/assets/index-aaJG_DoN.css"]

def test_extract_asset_refs_ignores_non_asset_and_dedupes() -> None:
    ignored = (
        '<link rel="icon" href="data:image/svg+xml,%3Csvg/%3E">'
        '<a href="https://example.com/whatever.js">external</a>'
    )
    assert extract_asset_refs(ignored) == []
    dup = '<script src="/assets/shared-ABC.js"></script><link href="/assets/shared-ABC.js">'
    assert extract_asset_refs(dup) == ["/assets/shared-ABC.js"]

# resolve_query_params / append_query

def test_resolve_query_params_fills_single_resolvable_required(conn: sqlite3.Connection) -> None:
    # /api/review-wall/events 的真实形状：episode_id 必填且可解析。
    conn.execute("INSERT INTO episodes(id) VALUES ('ep_real')")
    op = {"parameters": [
        {"name": "episode_id", "in": "query", "required": True},
        {"name": "limit", "in": "query", "required": False},
    ]}
    query, missing = resolve_query_params(conn, op)
    assert missing == []
    assert query == {"episode_id": "ep_real"}  # limit 不认识，可选，原样不填

def test_resolve_query_params_fills_only_first_of_mutually_resolvable(conn: sqlite3.Connection) -> None:
    # run_id/job_id/call_id 都可解析、都可选（互斥三选一是业务规则），全填会撞
    # 校验，本函数只填第一个（/api/observability/resolve 的真实形状）。
    conn.execute("INSERT INTO workflow_runs(id) VALUES ('run_real')")
    conn.execute("INSERT INTO jobs(id) VALUES ('job_real')")
    op = {"parameters": [
        {"name": "run_id", "in": "query", "required": False},
        {"name": "job_id", "in": "query", "required": False},
        {"name": "call_id", "in": "query", "required": False},
    ]}
    query, missing = resolve_query_params(conn, op)
    assert missing == []
    assert query == {"run_id": "run_real"}

@pytest.mark.parametrize(
    "op,expected_missing",
    [
        ({"parameters": [{"name": "action", "in": "query", "required": True}]}, ["action"]),  # 不认识
        ({"parameters": [{"name": "episode_id", "in": "query", "required": True}]}, ["episode_id"]),  # 表空
        ({"parameters": [{"name": "project_id", "in": "path", "required": True}]}, []),  # path 不处理
    ],
)
def test_resolve_query_params_edge_cases(conn: sqlite3.Connection, op, expected_missing) -> None:
    query, missing = resolve_query_params(conn, op)
    assert query == {}
    assert missing == expected_missing

def test_append_query_encodes_params_and_noops_when_empty() -> None:
    assert append_query("/api/observability/resolve", {"run_id": "run_a b"}) == (
        "/api/observability/resolve?run_id=run_a+b"
    )
    assert append_query("/api/settings", {}) == "/api/settings"

# quote_media_url_path：build_media_url 返回值不做百分号编码，中文路径段必须补编码

def test_quote_media_url_path_encodes_non_ascii_and_leaves_query_alone() -> None:
    raw = "/media/proj_facfc3964f69/scene_refs/皇家古籍藏书室__candidate_e1bf55db5a3e.jpg?mt=517a8a2d647b"
    quoted = quote_media_url_path(raw)
    quoted.encode("ascii")  # urllib 要能编码成 ASCII，否则连接层直接抛异常
    assert quoted == (
        "/media/proj_facfc3964f69/scene_refs/%E7%9A%87%E5%AE%B6%E5%8F%A4%E7%B1%8D"
        "%E8%97%8F%E4%B9%A6%E5%AE%A4__candidate_e1bf55db5a3e.jpg?mt=517a8a2d647b"
    )
    assert quote_media_url_path("/api/settings") == "/api/settings"  # 非 /media 原样返回
    assert quote_media_url_path("/media/plain/ok.jpg?mt=abc") == "/media/plain/ok.jpg?mt=abc"
