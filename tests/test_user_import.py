"""EP-03 §4/§8 CSV 批量导入的端到端验收：全部经 HTTP，不写裸 SQL 铺业务状态
（唯一例外是引导首个系统管理员，与 test_rbac_admin_api.py 同一条理由：先有
鸡后有蛋，产品内无解）。

四类逐行结果（create/update/skip/error）、编码自适应（UTF-8/GBK）、批次内
重复 username 预检报错、单行错误不阻塞其它行、初始口令一次性可见、
"同一份 CSV 连导两次"幂等，逐条对应 EP-03 §8 验收清单。
"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app
from app.orgs import service as orgs_service
from app.orgs import store as orgs_store

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    conn = get_conn()
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, "
        "must_change_password, created_at) VALUES(?,?,?,'active',1,0,?)",
        (user_id, "root-import", hash_password("pw-root"), now()),
    )
    conn.commit()
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


def _upload_csv(client: TestClient, headers: dict, text: str, *, encoding: str = "utf-8", filename: str = "roster.csv"):
    return client.post(
        "/api/system/users/import/preview",
        headers=headers,
        files={"file": (filename, text.encode(encoding), "text/csv")},
    )


def _row_by_username(rows: list[dict], username: str) -> dict:
    matches = [r for r in rows if r["username"] == username]
    assert len(matches) == 1, f"expected exactly one row for {username!r}, got {matches}"
    return matches[0]


def test_preview_does_not_write_db(client: TestClient, admin_headers: dict):
    text = "username,display_name\nzhangsan,张三\n"
    resp = _upload_csv(client, admin_headers, text)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"] == {"create": 1, "update": 0, "skip": 0, "error": 0}
    assert "batch_id" in body and body["batch_id"]

    users = client.get("/api/system/users", headers=admin_headers).json()["items"]
    assert not any(u["username"] == "zhangsan" for u in users), "preview 不许写库"


def test_four_way_classification_and_apply(client: TestClient, admin_headers: dict):
    """一次导入里同时出现 create/update/skip/error 四类，互不影响。"""
    conn = get_conn()
    org_id = orgs_store.ORG_DEFAULT_ID
    team_id = orgs_service.create_team(org_id=org_id, name="研发组", description=None, created_by="test")
    viewer_role = orgs_store.get_role_by_key(conn, None, "viewer")
    assert viewer_role is not None

    # 预先建两个既有账号：一个用于 update（CSV 里改了 tier），一个用于 skip（CSV
    # 里字段与库中完全一致，视为“没有变化”）。
    created = client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "laoyonghu", "password": "initpass1", "display_name": "旧显示名", "tier": "free"},
    ).json()
    assert created["tier"] == "free"
    client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": "bubianhu", "password": "initpass1", "display_name": "不变户"},
    )

    text = (
        "username,display_name,email,employee_no,team,role,tier_or_quota_plan\n"
        "zhangsan,张三,zhangsan@example.com,E001,研发组,viewer,starter\n"  # create
        "laoyonghu,,,,,,starter\n"  # update：只改 tier
        "bubianhu,,,,,,\n"  # skip：什么都没变
        "huaishu,坏树,,,研发组,no_such_role,\n"  # error：角色不存在
    )
    preview = _upload_csv(client, admin_headers, text)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["counts"] == {"create": 1, "update": 1, "skip": 1, "error": 1}

    zhangsan = _row_by_username(body["rows"], "zhangsan")
    assert zhangsan["action"] == "create"
    huaishu = _row_by_username(body["rows"], "huaishu")
    assert huaishu["action"] == "error" and huaishu["column"] == "role" and "角色" in huaishu["reason"]

    apply_resp = client.post(f"/api/system/users/import/{body['batch_id']}/apply", headers=admin_headers)
    assert apply_resp.status_code == 200, apply_resp.text
    applied = apply_resp.json()
    assert applied["counts"] == {"create": 1, "update": 1, "skip": 1, "error": 1}

    created_row = _row_by_username(applied["rows"], "zhangsan")
    assert created_row["initial_password"], "新建账号必须返回一次性初始口令"

    # 行为断言：新口令真的能登录，且被强制改密。
    login = client.post(
        "/api/auth/login",
        json={"username": "zhangsan", "password": created_row["initial_password"]},
        headers=_HEADERS,
    )
    assert login.status_code == 200, login.text
    assert login.json()["must_change_password"] is True

    # update 真的落库了。
    users = {u["username"]: u for u in client.get("/api/system/users", headers=admin_headers).json()["items"]}
    assert users["laoyonghu"]["tier"] == "starter"
    # skip 的账号确实原样未动。
    assert users["bubianhu"]["display_name"] == "不变户"
    # error 行没有被创建成账号。
    assert "huaishu" not in users


def test_duplicate_username_in_batch_is_error_for_all_occurrences(client: TestClient, admin_headers: dict):
    text = (
        "username,display_name\n"
        "dup1,重复甲\n"
        "dup1,重复乙\n"
    )
    resp = _upload_csv(client, admin_headers, text)
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == 2
    assert all(r["action"] == "error" and "重复" in (r["reason"] or "") for r in rows)


def test_row_without_username_is_error_not_batch_failure(client: TestClient, admin_headers: dict):
    text = "username,display_name\n,没有用户名\nvalidname,合法\n"
    resp = _upload_csv(client, admin_headers, text)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"] == {"create": 1, "update": 0, "skip": 0, "error": 1}


def test_gbk_encoded_csv_with_chinese_names(client: TestClient, admin_headers: dict):
    text = "username,display_name\nwangwu,王五\n"
    resp = _upload_csv(client, admin_headers, text, encoding="gbk")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["encoding"] == "gbk"
    row = _row_by_username(body["rows"], "wangwu")
    assert row["display_name"] == "王五"


def test_undetectable_encoding_returns_explicit_error(client: TestClient, admin_headers: dict):
    # 构造一段在 UTF-8-SIG / UTF-8 / GBK 下都解不出来的字节串。
    garbage = b"username,display_name\r\n\xff\xfe\x00\xd8\x00\x00,x\r\n"
    resp = client.post(
        "/api/system/users/import/preview", headers=admin_headers,
        files={"file": ("bad.csv", garbage, "text/csv")},
    )
    assert resp.status_code == 422
    assert "编码" in resp.json()["detail"]


def test_row_count_over_limit_rejected(client: TestClient, admin_headers: dict):
    lines = ["username,display_name"] + [f"u{i},名字{i}" for i in range(1001)]
    resp = _upload_csv(client, admin_headers, "\n".join(lines) + "\n")
    assert resp.status_code == 422
    assert "1000" in resp.json()["detail"]


def test_reimporting_same_csv_twice_is_idempotent(client: TestClient, admin_headers: dict):
    text = "username,display_name\nzhaoliu,赵六\nsunqi,孙七\n"
    first_preview = _upload_csv(client, admin_headers, text)
    first_batch = first_preview.json()["batch_id"]
    first_apply = client.post(f"/api/system/users/import/{first_batch}/apply", headers=admin_headers)
    assert first_apply.json()["counts"] == {"create": 2, "update": 0, "skip": 0, "error": 0}

    before_count = len(client.get("/api/system/users", headers=admin_headers).json()["items"])

    second_preview = _upload_csv(client, admin_headers, text)
    second_body = second_preview.json()
    assert second_body["counts"] == {"create": 0, "update": 0, "skip": 2, "error": 0}
    second_apply = client.post(
        f"/api/system/users/import/{second_body['batch_id']}/apply", headers=admin_headers
    )
    assert second_apply.json()["counts"] == {"create": 0, "update": 0, "skip": 2, "error": 0}

    after_count = len(client.get("/api/system/users", headers=admin_headers).json()["items"])
    assert after_count == before_count, "同一份 CSV 连导两次不能新增账号"


def test_apply_same_batch_id_twice_does_not_double_create(client: TestClient, admin_headers: dict):
    text = "username,display_name\nchongfu,重复提交\n"
    batch_id = _upload_csv(client, admin_headers, text).json()["batch_id"]
    first = client.post(f"/api/system/users/import/{batch_id}/apply", headers=admin_headers)
    second = client.post(f"/api/system/users/import/{batch_id}/apply", headers=admin_headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["counts"] == second.json()["counts"] == {"create": 1, "update": 0, "skip": 0, "error": 0}

    users = client.get("/api/system/users", headers=admin_headers).json()["items"]
    assert sum(1 for u in users if u["username"] == "chongfu") == 1


def test_report_csv_exposes_password_once_then_redacts(client: TestClient, admin_headers: dict):
    text = "username,display_name\nbaogao,报告用户\n"
    batch_id = _upload_csv(client, admin_headers, text).json()["batch_id"]
    applied = client.post(f"/api/system/users/import/{batch_id}/apply", headers=admin_headers).json()
    password = _row_by_username(applied["rows"], "baogao")["initial_password"]
    assert password

    first_download = client.get(f"/api/system/users/import/{batch_id}/report", headers=admin_headers)
    assert first_download.status_code == 200
    first_rows = list(csv.DictReader(io.StringIO(first_download.content.decode("utf-8-sig"))))
    first_row = _row_by_username(first_rows, "baogao")
    assert first_row["initial_password"] == password

    second_download = client.get(f"/api/system/users/import/{batch_id}/report", headers=admin_headers)
    second_rows = list(csv.DictReader(io.StringIO(second_download.content.decode("utf-8-sig"))))
    second_row = _row_by_username(second_rows, "baogao")
    assert second_row["initial_password"] == "***"

    # 库中不落明文：password_hash 不等于任何明文口令本身。
    conn = get_conn()
    row = conn.execute("SELECT password_hash FROM users WHERE username='baogao'").fetchone()
    assert password not in row["password_hash"]


def test_import_requires_system_admin(client: TestClient):
    conn = get_conn()
    plain_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, "
        "must_change_password, created_at) VALUES(?,?,?,'active',0,0,?)",
        (plain_id, "not-admin-import", hash_password("pw"), now()),
    )
    conn.commit()
    headers = {**_HEADERS, "X-Manju-Session": create_session(plain_id)}
    resp = _upload_csv(client, headers, "username,display_name\nx,y\n")
    assert resp.status_code == 403
