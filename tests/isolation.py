from __future__ import annotations

import os
import socket
import sqlite3
import traceback
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable
from urllib.parse import unquote


UNROUTABLE_PROVIDER_BASE_URL = "http://pytest-deny-network.invalid"


class TestIsolationViolation(RuntimeError):
    """Raised before a test can access an external network or protected DB."""

    __test__ = False


@dataclass(frozen=True)
class ProviderConfigurationSchema:
    """Provider settings discovered from the application's exported config."""

    credentials: tuple[str, ...]
    endpoints: tuple[str, ...]
    endpoint_settings: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: ModuleType) -> ProviderConfigurationSchema:
        fields = {
            name
            for name, value in vars(settings).items()
            if name.isupper() and isinstance(value, str) and not name.startswith("DEFAULT_")
        }
        endpoints = tuple(sorted(name for name in fields if name.endswith("_BASE_URL")))
        setting_defaults = getattr(settings, "DEFAULT_SETTINGS", {})
        return cls(
            credentials=tuple(sorted(name for name in fields if name.endswith("_API_KEY"))),
            endpoints=endpoints,
            endpoint_settings=tuple(name.lower() for name in endpoints if name.lower() in setting_defaults),
        )


def isolate_provider_environment(
    environment: MutableMapping[str, str],
    *,
    blocked_endpoint: str = UNROUTABLE_PROVIDER_BASE_URL,
) -> None:
    """Remove inherited provider access before application settings are imported."""

    for name in tuple(environment):
        if name.endswith("_API_KEY"):
            environment[name] = ""
        elif name.endswith("_BASE_URL"):
            environment[name] = blocked_endpoint


class ProviderConfigurationIsolation:
    """Keep provider credentials and endpoints fail-closed between tests."""

    def __init__(
        self,
        *,
        settings: ModuleType,
        environment: MutableMapping[str, str],
        blocked_endpoint: str = UNROUTABLE_PROVIDER_BASE_URL,
    ) -> None:
        self.settings = settings
        self.environment = environment
        self.blocked_endpoint = blocked_endpoint
        self.schema = ProviderConfigurationSchema.from_settings(settings)

    def apply(self) -> None:
        isolate_provider_environment(
            self.environment,
            blocked_endpoint=self.blocked_endpoint,
        )
        for name in self.schema.credentials:
            self.environment[name] = ""
            setattr(self.settings, name, "")
        for name in self.schema.endpoints:
            self.environment[name] = self.blocked_endpoint
            setattr(self.settings, name, self.blocked_endpoint)
        setting_defaults = getattr(self.settings, "DEFAULT_SETTINGS", {})
        for name in self.schema.endpoint_settings:
            setting_defaults[name] = self.blocked_endpoint

    def state(self) -> dict[str, dict[str, dict[str, str | None]]]:
        credential_environment = set(self.schema.credentials)
        endpoint_environment = set(self.schema.endpoints)
        for name in self.environment:
            if name.endswith("_API_KEY"):
                credential_environment.add(name)
            elif name.endswith("_BASE_URL"):
                endpoint_environment.add(name)
        return {
            "credentials": {
                "environment": {name: self.environment.get(name) for name in sorted(credential_environment)},
                "runtime": {name: getattr(self.settings, name) for name in self.schema.credentials},
            },
            "endpoints": {
                "environment": {name: self.environment.get(name) for name in sorted(endpoint_environment)},
                "runtime": {name: getattr(self.settings, name) for name in self.schema.endpoints},
                "settings": {name: self.settings.DEFAULT_SETTINGS.get(name) for name in self.schema.endpoint_settings},
            },
        }


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
            f"pytest isolated profile rejected {detail}; use an explicitly marked live integration test instead"
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


#: 只在部署环境里出现、会改变被测行为的环境变量。``.env`` 是 gitignored 的，
#: CI 与新克隆都没有它，所以整套测试原本就是按**代码默认值**写的；本机因为存在
#: ``.env`` 才会红，那是本地部署配置污染了测试环境，不是测试写错。
#:
#: ``app/config.py`` 用 ``os.environ.setdefault`` 加载 ``.env``，读取方
#: （``legacy_shared_session_enabled`` / ``media_ticket_required``）又都是**调用时**
#: 读 ``os.environ``，所以在测试进程里把这些键删掉就能让代码默认值重新生效。
#:
#: 这里删除而不是「设成 1/0」是有意的：把默认值抄一份到测试配置里，将来代码默认值
#: 一改就出现两处不一致，而且没有任何东西会提醒。删掉覆盖，默认值只有一个来源。
DEPLOYMENT_ONLY_OVERRIDES = (
    "MJ_LEGACY_SHARED_SESSION",
    "MJ_MEDIA_REQUIRE_TICKET",
)


def isolate_deployment_overrides(environment: MutableMapping[str, str]) -> list[str]:
    """删除 ``.env`` 带进来的部署侧开关，让测试跑在代码默认值上。

    返回实际删掉的键名，便于调用方在需要时报告「本机 .env 覆盖了哪些开关」。
    """
    removed = []
    for name in DEPLOYMENT_ONLY_OVERRIDES:
        if name in environment:
            del environment[name]
            removed.append(name)
    return removed
