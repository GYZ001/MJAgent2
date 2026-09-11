"""模型凭据加密主密钥供给：本地文件（0600）+ KMS 接缝。L1，零 db 依赖。

主密钥来自 ``data/master.key``，首次启动时随机生成并以 0600 权限落盘；环境变量
``MJ_MASTER_KEY_FILE`` 可覆盖路径（测试隔离、容器化部署换路径）。``KeyProvider``
是纯读取协议，默认实现 ``FileKeyProvider`` 读本地文件；接口留出是为了将来接
KMS（AWS/阿里云）时只需换一个实现，不动调用方（``app/models_registry/store.py``
只认 ``KeyProvider.get_key() -> bytes``）。

读 ``app.config.DATA_DIR`` 必须用 ``from app import config`` + ``config.DATA_DIR``
（模块限定访问），不能 ``from app.config import DATA_DIR``（名字拷贝）：
``tests/conftest.py`` 在测试隔离时是直接重写 ``app.config`` 模块对象的
``DATA_DIR`` 属性（``app_config.DATA_DIR = _SANDBOX / "data"``），拷贝一份的写法
会在 conftest 改写之前就已经把值定死，测试期间悄悄用到仓库里真实的
``data/master.key``。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol

KEY_LEN = 32  # AES-256


class KeyProvider(Protocol):
    def get_key(self) -> bytes:
        """返回 32 字节主密钥。"""
        ...


def _key_path() -> Path:
    override = os.environ.get("MJ_MASTER_KEY_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    from app import config

    return config.DATA_DIR / "master.key"


def _load_or_create(path: Path) -> bytes:
    if path.exists():
        key = path.read_bytes()
        if len(key) != KEY_LEN:
            raise ValueError(
                f"主密钥文件 {path} 长度异常（{len(key)} 字节，应为 {KEY_LEN}），拒绝启动"
            )
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(KEY_LEN)
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # 权限设置失败不阻塞启动，但落盘的内容本身仍是正确的密钥
    return key


class FileKeyProvider:
    """默认实现：本地文件，首次调用时生成并落盘，此后按路径缓存。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached_key: bytes | None = None
        self._cached_path: Path | None = None

    def get_key(self) -> bytes:
        path = _key_path()
        with self._lock:
            if self._cached_key is not None and self._cached_path == path:
                return self._cached_key
            key = _load_or_create(path)
            self._cached_key = key
            self._cached_path = path
            return key


_default_provider = FileKeyProvider()


def get_default_provider() -> KeyProvider:
    return _default_provider
