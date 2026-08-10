from __future__ import annotations

import os
import socket
import sqlite3
import traceback
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote


class TestIsolationViolation(RuntimeError):
    """Raised before a test can access an external network or protected DB."""

    __test__ = False


def _callsite() -> str:
    frames = traceback.extract_stack(limit=12)
    for frame in reversed(frames[:-2]):
        path = Path(frame.filename)
        if path.name != "isolation.py":
            return f"{path.name}:{frame.lineno} in {frame.name}"
    return "unknown callsite"


def _sqlite_path(database: object) -> Path | None:
    try:
        raw = os.fsdecode(os.fspath(database))
    except TypeError:
        return None
    if raw == ":memory:":
        return None
    if raw.startswith("file:"):
        raw = unquote(raw[5:].partition("?")[0])
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


class AccessAudit:
    """Record forbidden access even when application code catches the error."""

    def __init__(self, *, sandbox: Path) -> None:
        self.sandbox = sandbox.expanduser().resolve()
        self.violations: list[str] = []

    def _reject(self, resource: str) -> None:
        detail = f"{resource} attempted from {_callsite()}"
        self.violations.append(detail)
        raise TestIsolationViolation(
            f"pytest isolated profile rejected {detail}; "
            "use an explicitly marked live integration test instead"
        )

    def reject_network(self, operation: str, target: object) -> None:
        self._reject(f"external network {operation} to {target!r}")

    def connect_database(
        self,
        delegate: Callable[..., sqlite3.Connection],
        database: object,
        *args: Any,
        **kwargs: Any,
    ) -> sqlite3.Connection:
        target = _sqlite_path(database)
        if target is not None and not target.is_relative_to(self.sandbox):
            self._reject(f"persistent database outside test sandbox: {target}")
        return delegate(database, *args, **kwargs)

    def assert_clean(self) -> None:
        if self.violations:
            raise TestIsolationViolation("\n".join(self.violations))


class IsolationSession:
    """Install and restore process-wide transport guards for one pytest run."""

    def __init__(self, *, sandbox: Path) -> None:
        self.audit = AccessAudit(sandbox=sandbox)
        self._socket_type = socket.socket
        self._create_connection = socket.create_connection
        self._getaddrinfo = socket.getaddrinfo
        self._sqlite_connect = sqlite3.connect
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        audit = self.audit
        socket_type = self._socket_type

        class IsolatedSocket(socket_type):
            def connect(self, address: object) -> None:
                audit.reject_network("connect", address)

            def connect_ex(self, address: object) -> int:
                audit.reject_network("connect_ex", address)
                return 1

        def reject_create_connection(address: object, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            audit.reject_network("create_connection", address)

        def reject_getaddrinfo(host: object, port: object, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            audit.reject_network("DNS resolution", (host, port))

        def guarded_sqlite_connect(
            database: object,
            *args: Any,
            **kwargs: Any,
        ) -> sqlite3.Connection:
            return audit.connect_database(
                self._sqlite_connect,
                database,
                *args,
                **kwargs,
            )

        socket.socket = IsolatedSocket
        socket.create_connection = reject_create_connection
        socket.getaddrinfo = reject_getaddrinfo
        sqlite3.connect = guarded_sqlite_connect
        self._installed = True

    def restore(self) -> None:
        if not self._installed:
            return
        socket.socket = self._socket_type
        socket.create_connection = self._create_connection
        socket.getaddrinfo = self._getaddrinfo
        sqlite3.connect = self._sqlite_connect
        self._installed = False
