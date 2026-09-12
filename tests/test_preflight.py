"""``scripts/preflight.py``：PRD/enterprise/EP-06 §8 要求"故意制造的错误环境下
逐条报出问题"。每个 check_* 函数独立测试，覆盖正常与故意制造的错误两侧，不
依赖真实改动系统时钟/真实占用生产端口——都是本地可控的注入。
"""
from __future__ import annotations

import socket
import time
from email.utils import format_datetime

import pytest

from scripts import preflight


# ---------------------------------------------------------------------------
# 磁盘空间
# ---------------------------------------------------------------------------


def test_disk_space_ok_when_plenty_free(tmp_path):
    result = preflight.check_disk_space(tmp_path, warn_gb=0.001, fail_gb=0.0001)
    assert result.level == preflight.OK


def test_disk_space_fail_when_thresholds_absurdly_high(tmp_path):
    result = preflight.check_disk_space(tmp_path, warn_gb=10**9, fail_gb=10**9)
    assert result.level == preflight.FAIL


# ---------------------------------------------------------------------------
# 密钥文件权限——PRD §8 明确的"master.key 权限 0644"场景
# ---------------------------------------------------------------------------


def test_secret_file_permissions_flags_wrong_mode(tmp_path):
    key = tmp_path / "master.key"
    key.write_text("fake-key-material")
    key.chmod(0o644)
    results = {r.name: r for r in preflight.check_secret_file_permissions(tmp_path)}
    assert results["密钥文件权限 master.key"].level == preflight.FAIL
    assert "0600" in results["密钥文件权限 master.key"].message


def test_secret_file_permissions_ok_when_0600(tmp_path):
    key = tmp_path / "master.key"
    key.write_text("fake-key-material")
    key.chmod(0o600)
    results = {r.name: r for r in preflight.check_secret_file_permissions(tmp_path)}
    assert results["密钥文件权限 master.key"].level == preflight.OK


def test_secret_file_permissions_warns_when_missing(tmp_path):
    results = {r.name: r for r in preflight.check_secret_file_permissions(tmp_path)}
    assert results["密钥文件权限 master.key"].level == preflight.WARN


# ---------------------------------------------------------------------------
# 时钟偏移——PRD §8"故意制造 5 分钟偏移"场景：不碰真实系统时钟，改让"参照
# 时间"落后本机 5 分钟，效果等价（时钟偏移检查比较的就是两者之差）。
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, date_str: str):
        self.headers = {"date": date_str}


def test_clock_skew_fails_on_five_minute_gap(monkeypatch):
    skewed_remote = time.time() - 310
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: _FakeResponse(format_datetime(
            __import__("datetime").datetime.fromtimestamp(skewed_remote, __import__("datetime").timezone.utc)
        ))
    )
    result = preflight.check_clock_skew(warn_s=60, fail_s=300)
    assert result.level == preflight.FAIL
    assert "310" in result.message or "30" in result.message  # 允许秒级抖动


def test_clock_skew_ok_when_in_sync(monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: _FakeResponse(format_datetime(
            __import__("datetime").datetime.fromtimestamp(now, __import__("datetime").timezone.utc)
        ))
    )
    result = preflight.check_clock_skew(warn_s=60, fail_s=300)
    assert result.level == preflight.OK


def test_clock_skew_falls_back_when_network_unreachable(monkeypatch):
    def _raise(*a, **k):
        raise OSError("network unreachable (test)")
    monkeypatch.setattr("httpx.get", _raise)
    result = preflight.check_clock_skew()
    assert result.level in (preflight.OK, preflight.WARN)
    assert "network unreachable" in result.message


# ---------------------------------------------------------------------------
# 模型连通性
# ---------------------------------------------------------------------------


def test_model_connectivity_warns_when_nothing_configured(monkeypatch):
    for _, url_attr, key_attr in preflight.PROVIDER_ENV:
        monkeypatch.setattr(preflight.app.config, url_attr, "", raising=False)
        monkeypatch.setattr(preflight.app.config, key_attr, "", raising=False)
    results = preflight.check_model_connectivity()
    assert any(r.level == preflight.FAIL and r.name == "模型连通性" for r in results)


@pytest.mark.live_integration  # 真实 socket 连接，tests/isolation.py 的沙箱审计会拦真实网络调用
def test_model_connectivity_fails_on_unreachable_host(monkeypatch):
    for _, url_attr, key_attr in preflight.PROVIDER_ENV:
        monkeypatch.setattr(preflight.app.config, url_attr, "", raising=False)
        monkeypatch.setattr(preflight.app.config, key_attr, "", raising=False)
    monkeypatch.setattr(preflight.app.config, "HIAGENT_BASE_URL", "https://127.0.0.1:1/", raising=False)
    monkeypatch.setattr(preflight.app.config, "HIAGENT_API_KEY", "test-key", raising=False)
    results = {r.name: r for r in preflight.check_model_connectivity(timeout_s=1.0)}
    assert results["模型连通性 HiAgent"].level == preflight.FAIL


@pytest.mark.live_integration  # 真实 socket 连接，同上
def test_model_connectivity_ok_on_reachable_host(monkeypatch):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        for _, url_attr, key_attr in preflight.PROVIDER_ENV:
            monkeypatch.setattr(preflight.app.config, url_attr, "", raising=False)
            monkeypatch.setattr(preflight.app.config, key_attr, "", raising=False)
        monkeypatch.setattr(preflight.app.config, "HIAGENT_BASE_URL", f"http://127.0.0.1:{port}/", raising=False)
        monkeypatch.setattr(preflight.app.config, "HIAGENT_API_KEY", "test-key", raising=False)
        results = {r.name: r for r in preflight.check_model_connectivity(timeout_s=2.0)}
        assert results["模型连通性 HiAgent"].level == preflight.OK
    finally:
        server.close()


# ---------------------------------------------------------------------------
# 端口绑定——PRD §8"端口占用"场景 + 0.0.0.0 误绑
# ---------------------------------------------------------------------------


def test_port_free_check_fails_when_occupied():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        result = preflight._check_port_free(port)
        assert result.level == preflight.FAIL
    finally:
        server.close()


def test_port_free_check_ok_when_free():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    result = preflight._check_port_free(port)
    assert result.level == preflight.OK


def test_bind_host_fails_on_0000(monkeypatch, tmp_path):
    monkeypatch.setenv("MJ_BACKEND_HOST", "0.0.0.0")
    missing_dockerenv = tmp_path / "not-a-container"
    result = preflight._check_bind_host("MJ_BACKEND_HOST", missing_dockerenv)
    assert result.level == preflight.FAIL


def test_bind_host_ok_on_loopback(monkeypatch, tmp_path):
    monkeypatch.setenv("MJ_BACKEND_HOST", "127.0.0.1")
    missing_dockerenv = tmp_path / "not-a-container"
    result = preflight._check_bind_host("MJ_BACKEND_HOST", missing_dockerenv)
    assert result.level == preflight.OK


def test_bind_host_0000_ok_inside_container(monkeypatch, tmp_path):
    monkeypatch.setenv("MJ_BACKEND_HOST", "0.0.0.0")
    fake_dockerenv = tmp_path / "dockerenv"
    fake_dockerenv.write_text("")
    result = preflight._check_bind_host("MJ_BACKEND_HOST", fake_dockerenv)
    assert result.level == preflight.OK


# ---------------------------------------------------------------------------
# 必需环境变量
# ---------------------------------------------------------------------------


def test_required_env_vars_fails_when_all_empty(monkeypatch):
    for key in preflight.REQUIRED_ENV_HINTS:
        monkeypatch.setattr(preflight.app.config, key, "", raising=False)
    result = preflight.check_required_env_vars()
    assert result.level == preflight.FAIL


def test_required_env_vars_ok_when_one_present(monkeypatch):
    for key in preflight.REQUIRED_ENV_HINTS:
        monkeypatch.setattr(preflight.app.config, key, "", raising=False)
    monkeypatch.setattr(preflight.app.config, "HIAGENT_API_KEY", "some-key", raising=False)
    result = preflight.check_required_env_vars()
    assert result.level == preflight.OK


# ---------------------------------------------------------------------------
# 整体报告：不能"报一句检查失败就退出"——run_all 必须每条都跑完
# ---------------------------------------------------------------------------


def test_run_all_reports_every_check_even_with_multiple_failures(monkeypatch, tmp_path):
    """一次性制造三类问题（权限、端口占用、模型未配置），断言全部三条都出现
    在报告里，而不是遇到第一个 FAIL 就中断。"""
    # run_all() 会连带跑 check_clock_skew()，真打一次外网在隔离测试里不允许
    # （见 tests/isolation.py），这里假一个同步的参照时间，只关心其它几条检查。
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: _FakeResponse(format_datetime(
            __import__("datetime").datetime.fromtimestamp(time.time(), __import__("datetime").timezone.utc)
        ))
    )
    key = tmp_path / "master.key"
    key.write_text("x")
    key.chmod(0o644)
    for _, url_attr, key_attr in preflight.PROVIDER_ENV:
        monkeypatch.setattr(preflight.app.config, url_attr, "", raising=False)
        monkeypatch.setattr(preflight.app.config, key_attr, "", raising=False)
    for k in preflight.REQUIRED_ENV_HINTS:
        monkeypatch.setattr(preflight.app.config, k, "", raising=False)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    occupied_port = server.getsockname()[1]
    try:
        results = preflight.run_all(tmp_path)
        results.append(preflight._check_port_free(occupied_port))
        by_name = {r.name: r for r in results}
        assert by_name["密钥文件权限 master.key"].level == preflight.FAIL
        assert by_name["模型连通性"].level == preflight.FAIL
        assert by_name["必需环境变量"].level == preflight.FAIL
        assert len(results) >= 10  # 报告没有在第一条失败后截断
    finally:
        server.close()


def test_main_exit_code_reflects_fail(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: _FakeResponse(format_datetime(
            __import__("datetime").datetime.fromtimestamp(time.time(), __import__("datetime").timezone.utc)
        ))
    )
    for _, url_attr, key_attr in preflight.PROVIDER_ENV:
        monkeypatch.setattr(preflight.app.config, url_attr, "", raising=False)
        monkeypatch.setattr(preflight.app.config, key_attr, "", raising=False)
    for k in preflight.REQUIRED_ENV_HINTS:
        monkeypatch.setattr(preflight.app.config, k, "", raising=False)
    exit_code = preflight.main(["--data-dir", str(tmp_path)])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
