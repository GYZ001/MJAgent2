"""RBAC 收口 /media 的访问票据回归。

背景：`/media/*` 曾经是零鉴权的裸 `StaticFiles` 挂载，`/api/*` 已经有工作空间
隔离，但浏览器的 `<img>`/`<video>` 标签不会带自定义请求头，`/api` 那套
`X-Manju-Session` 方案在结构上保护不了 `/media`——凭据必须放进 URL 里
（见 `app/media_urls.py` 的 `build_media_url` + `mt=` 票据）。

这里覆盖：
- `build_media_url` 同一天内对同一文件产出稳定 URL（nginx 长期不可变缓存的回归
  哨兵：票据一旦变成随机数，每次响应的 URL 都不同，缓存会被打穿）；
- `MJ_MEDIA_REQUIRE_TICKET` 关闭时（默认）无票据请求依旧 200，与改造前行为一致；
- 打开该开关后：合法票据放行、跨项目票据 403、缺票据 403；
- 昨天分桶签出的旧票据仍然放行（容忍跨零点）；
- Range 请求仍然返回 206 与正确的字节区间（复用 StaticFiles.get_response 原生能力）；
- `../` 路径穿越被拒绝。
"""
from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from app.main import PROJECTS_DIR, app
from app.media_urls import _day_bucket, _sign, build_media_url


@pytest.fixture()
def media_file():
    """在真实 PROJECTS_DIR 下放一个测试项目的媒体文件，用完即删。"""
    project_id = "mt-ticket-test-project"
    target_dir = PROJECTS_DIR / project_id / "episodes" / "1"
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / "clip.mp4"
    payload = bytes(range(256)) * 8  # 2048 字节，够切 Range
    file_path.write_bytes(payload)
    try:
        yield project_id, file_path, payload
    finally:
        shutil.rmtree(PROJECTS_DIR / project_id, ignore_errors=True)


@pytest.fixture()
def ticket_flag_off(monkeypatch):
    monkeypatch.delenv("MJ_MEDIA_REQUIRE_TICKET", raising=False)


@pytest.fixture()
def ticket_flag_on(monkeypatch):
    monkeypatch.setenv("MJ_MEDIA_REQUIRE_TICKET", "1")


def _rel(file_path):
    return file_path.relative_to(PROJECTS_DIR).as_posix()


def test_build_media_url_is_stable_within_same_day(media_file):
    """同一天内对同一文件重复构建，URL 必须逐字节相同——这是缓存回归的哨兵。"""
    _project_id, file_path, _payload = media_file
    url1 = build_media_url(str(file_path))
    url2 = build_media_url(str(file_path))
    assert url1 == url2
    assert url1 is not None
    assert "mt=" in url1


def test_flag_off_missing_ticket_still_succeeds(media_file, ticket_flag_off):
    """开关默认关闭：无票据请求仍必须 200——这是与改造前行为一致的等价性回归。"""
    _project_id, file_path, payload = media_file
    with TestClient(app) as client:
        resp = client.get(f"/media/{_rel(file_path)}")
    assert resp.status_code == 200
    assert resp.content == payload


def test_flag_on_valid_ticket_accepted(media_file, ticket_flag_on):
    project_id, file_path, payload = media_file
    url = build_media_url(str(file_path))
    with TestClient(app) as client:
        resp = client.get(url)
    assert resp.status_code == 200
    assert resp.content == payload


def test_flag_on_missing_ticket_rejected(media_file, ticket_flag_on):
    _project_id, file_path, _payload = media_file
    with TestClient(app) as client:
        resp = client.get(f"/media/{_rel(file_path)}")
    assert resp.status_code == 403


def test_flag_on_wrong_project_ticket_rejected(media_file, ticket_flag_on):
    """拿别的项目当天签出的票据，不能用来访问这个项目的文件。"""
    _project_id, file_path, _payload = media_file
    wrong_ticket = _sign("some-other-project", _day_bucket())
    with TestClient(app) as client:
        resp = client.get(f"/media/{_rel(file_path)}?mt={wrong_ticket}")
    assert resp.status_code == 403


def test_flag_on_yesterday_bucket_still_accepted(media_file, ticket_flag_on):
    """容忍跨零点：昨天分桶签出的票据今天仍然放行。"""
    project_id, file_path, payload = media_file
    yesterday_ticket = _sign(project_id, _day_bucket() - 1)
    with TestClient(app) as client:
        resp = client.get(f"/media/{_rel(file_path)}?mt={yesterday_ticket}")
    assert resp.status_code == 200
    assert resp.content == payload


def test_range_request_returns_206_with_correct_bytes(media_file, ticket_flag_on):
    project_id, file_path, payload = media_file
    url = build_media_url(str(file_path))
    with TestClient(app) as client:
        resp = client.get(url, headers={"Range": "bytes=10-19"})
    assert resp.status_code == 206
    assert resp.content == payload[10:20]


def test_path_traversal_rejected(ticket_flag_off):
    """`../` 逃出 PROJECTS_DIR 必须被拒绝，即便票据校验关闭。"""
    secret = PROJECTS_DIR.parent / "media-ticket-traversal-secret.txt"
    secret.write_text("should never be served via /media", encoding="utf-8")
    try:
        with TestClient(app) as client:
            # 用 %2e%2e 而不是字面 ".." ——裸 ".." 会被 httpx 在发请求前就地规约掉，
            # 测的就不是服务端的穿越防护，而是客户端库自己的 URL 规范化。
            resp = client.get(f"/media/%2e%2e/{secret.name}")
        assert resp.status_code == 404
    finally:
        secret.unlink(missing_ok=True)
