"""短时效 attachment_token：把用户已选择的上传文件换成 Command 可消费的凭证。

PRD §5.1 / §12.1：禁止把任意 ``file_path`` 暴露给 Agent。用户在系统文件选择器中选定文件后，
前端立即调用一次性上传换 token 的入口（见 ``app/api.py`` 的 ``POST /api/attachments/novel``），
随后无论页面 REST 还是 Agent/MCP 调用 ``project.import_novel``，都只传递 token，不传真实路径。

Token 落盘到临时文件、单次兑换、短时效自动失效，兑换或过期后立即删除临时文件。
"""
from __future__ import annotations

import hashlib
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL_S = 15 * 60
LEASE_TTL_S = 2 * 60


@dataclass
class _StoredAttachment:
    token: str
    filename: str
    path: Path
    content_type: str | None
    expires_at: float
    consumed: bool = False
    leased_at: float | None = None
    upload_key: str = ""


_STORE: dict[str, _StoredAttachment] = {}
_UPLOAD_KEYS: dict[str, str] = {}
_STORE_LOCK = threading.RLock()


def _upload_key(filename: str, content: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(Path(filename or "upload.txt").name.encode("utf-8", "replace"))
    digest.update(b"\0")
    digest.update(content)
    return digest.hexdigest()


def store_upload(
    filename: str,
    content: bytes,
    *,
    content_type: str | None = None,
    ttl_s: float = DEFAULT_TTL_S,
) -> str:
    """落盘到临时文件并登记短时效 token；用户选择/上传文件后立即调用一次。"""
    if not content:
        raise ValueError("文件为空，请选择包含正文的 TXT 小说")
    safe_filename = Path(filename or "upload.txt").name
    upload_key = _upload_key(safe_filename, content)
    with _STORE_LOCK:
        existing_token = _UPLOAD_KEYS.get(upload_key)
        existing = _STORE.get(existing_token or "")
        if existing is not None and time.time() <= existing.expires_at and existing.path.exists():
            return existing.token
        if existing_token:
            _discard(existing_token)

        token = f"att_{secrets.token_hex(16)}"
        suffix = Path(safe_filename).suffix or ".bin"
        fd, tmp_path = tempfile.mkstemp(prefix="manju_attach_", suffix=suffix)
        with open(fd, "wb") as handle:
            handle.write(content)
        _STORE[token] = _StoredAttachment(
            token=token,
            filename=safe_filename,
            path=Path(tmp_path),
            content_type=content_type,
            expires_at=time.time() + max(ttl_s, 1.0),
            upload_key=upload_key,
        )
        _UPLOAD_KEYS[upload_key] = token
    return token


def _peek(token: str) -> _StoredAttachment:
    with _STORE_LOCK:
        item = _STORE.get(token)
        if item is None:
            raise KeyError("附件凭证不存在或已完成导入，请重新选择文件")
        if item.consumed:
            _discard(token)
            raise KeyError("附件凭证已完成导入，请重新选择文件")
        if time.time() > item.expires_at:
            _discard(token)
            raise KeyError("附件凭证已过期，请重新选择文件")
        if item.leased_at is not None:
            if time.time() - item.leased_at <= LEASE_TTL_S:
                raise KeyError("这份小说正在导入，请等待当前操作完成")
            item.leased_at = None
        return item


def read(token: str) -> tuple[str, bytes]:
    """读取附件但暂不销毁，领域操作失败时允许使用同一凭证安全重试。"""
    if not token:
        raise KeyError("附件凭证不能为空，请重新选择文件")
    with _STORE_LOCK:
        item = _peek(token)
        item.leased_at = time.time()
    try:
        return item.filename, item.path.read_bytes()
    except OSError as exc:
        _discard(token)
        raise KeyError("附件临时文件不可读取，请重新选择文件") from exc


def release(token: str) -> None:
    """领域操作失败时释放读取租约，保留附件供下一次批准后重试。"""
    with _STORE_LOCK:
        item = _STORE.get(token)
        if item is not None:
            item.leased_at = None


def discard(token: str) -> None:
    """在领域操作成功提交后销毁附件凭证。"""
    _discard(token)


def consume(token: str) -> tuple[str, bytes]:
    """兑换 token 为 (filename, content)。只能兑换一次，兑换后立即失效并清理临时文件。"""
    filename, content = read(token)
    try:
        return filename, content
    finally:
        _discard(token)


def _discard(token: str) -> None:
    with _STORE_LOCK:
        item = _STORE.pop(token, None)
        if item is not None:
            if _UPLOAD_KEYS.get(item.upload_key) == token:
                _UPLOAD_KEYS.pop(item.upload_key, None)
            try:
                item.path.unlink(missing_ok=True)
            except OSError:
                pass


def purge_expired() -> int:
    """清理过期未兑换的附件（可挂到定时任务/启动恢复）。"""
    now = time.time()
    with _STORE_LOCK:
        expired = [token for token, item in _STORE.items() if now > item.expires_at]
    for token in expired:
        _discard(token)
    return len(expired)


def reset_for_tests() -> None:
    with _STORE_LOCK:
        tokens = list(_STORE.keys())
    for token in tokens:
        _discard(token)
