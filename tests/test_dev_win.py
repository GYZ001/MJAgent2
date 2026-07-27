from __future__ import annotations

import subprocess

import pytest

from scripts import dev_win


class _FakeProcess:
    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code


def test_kill_port_reports_failure_when_listener_remains(monkeypatch) -> None:
    monkeypatch.setattr(dev_win, "_pids_on_port", lambda _port: [1234])
    monkeypatch.setattr(dev_win.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev_win.time, "sleep", lambda _seconds: None)

    assert dev_win._kill_port(8230, "后端") is False


def test_wait_ready_rejects_dead_new_process_before_accepting_old_server() -> None:
    with pytest.raises(SystemExit, match="后端启动失败"):
        dev_win._wait_ready(
            _FakeProcess(exit_code=1),  # type: ignore[arg-type]
            _FakeProcess(exit_code=None),  # type: ignore[arg-type]
            timeout=0.1,
        )


def test_restart_aborts_if_old_service_cannot_stop(monkeypatch) -> None:
    monkeypatch.setattr(dev_win.sys, "argv", ["dev_win.py", "restart"])
    monkeypatch.setattr(
        dev_win,
        "_kill_port",
        lambda port, _name: port != 8230,
    )
    start_called = False

    def fake_start() -> tuple[subprocess.Popen[bytes], subprocess.Popen[bytes]]:
        nonlocal start_called
        start_called = True
        raise AssertionError("should not start")

    monkeypatch.setattr(dev_win, "_start", fake_start)

    with pytest.raises(SystemExit, match="旧服务未能完全停止"):
        dev_win.main()

    assert start_called is False
