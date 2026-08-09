#!/usr/bin/env python3
"""Run the backend in stable mode and restart it on a fixed interval."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKEND_LOG = Path("/tmp/manju2_backend.log")
DEFAULT_LOCK_FILE = Path("/tmp/manju2_backend_cycle.lock")
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def _log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def _backend_env() -> dict[str, str]:
    env = os.environ.copy()
    path_entries = [item for item in env.get("PATH", "").split(os.pathsep) if item]
    media_dirs = [
        item
        for item in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")
        if (Path(item) / "ffmpeg").is_file() and item not in path_entries
    ]
    env["PATH"] = os.pathsep.join([*media_dirs, *path_entries])

    inherit_proxy = env.get("MJ_BACKEND_INHERIT_PROXY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not inherit_proxy:
        for key in PROXY_ENV_KEYS:
            env.pop(key, None)
    return env


def _stop_child(child: subprocess.Popen[bytes], timeout: float) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(f"backend pid={child.pid} did not stop in {timeout:g}s; killing it")
        child.kill()
        child.wait()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval",
        type=float,
        default=1800,
        help="seconds between scheduled backend restarts (default: 1800)",
    )
    parser.add_argument(
        "--crash-retry-delay",
        type=float,
        default=60,
        help="seconds to wait after an unexpected backend exit (default: 60)",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=30,
        help="seconds to wait before force-killing the backend (default: 30)",
    )
    parser.add_argument(
        "--backend-log",
        type=Path,
        default=DEFAULT_BACKEND_LOG,
        help=f"backend log path (default: {DEFAULT_BACKEND_LOG})",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=DEFAULT_LOCK_FILE,
        help=f"single-instance lock path (default: {DEFAULT_LOCK_FILE})",
    )
    args = parser.parse_args()
    for name in ("interval", "crash_retry_delay", "shutdown_timeout"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than zero")
    return args


def main() -> int:
    args = _parse_args()
    args.lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock = args.lock_file.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _log(f"another backend supervisor is active ({args.lock_file})")
        return 1

    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        _log(f"received signal {signum}; stopping supervisor")
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    args.backend_log.parent.mkdir(parents=True, exist_ok=True)
    backend_log = args.backend_log.open("ab", buffering=0)
    command = [
        str(ROOT / ".venv/bin/uvicorn"),
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8230",
        "--timeout-graceful-shutdown",
        str(int(args.shutdown_timeout)),
    ]

    try:
        while not stop_event.is_set():
            child = subprocess.Popen(
                command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=backend_log,
                stderr=backend_log,
                env=_backend_env(),
            )
            _log(
                f"started backend pid={child.pid}; "
                f"scheduled restart in {args.interval:g}s"
            )
            deadline = time.monotonic() + args.interval

            while child.poll() is None and not stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                stop_event.wait(min(1, remaining))

            if stop_event.is_set():
                _stop_child(child, args.shutdown_timeout)
                break

            if child.poll() is None:
                _log(f"scheduled restart for backend pid={child.pid}")
                _stop_child(child, args.shutdown_timeout)
                continue

            _log(
                f"backend pid={child.pid} exited with code {child.returncode}; "
                f"retrying in {args.crash_retry_delay:g}s"
            )
            stop_event.wait(args.crash_retry_delay)
    finally:
        backend_log.close()
        try:
            args.lock_file.unlink()
        except FileNotFoundError:
            pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()

    _log("backend supervisor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
