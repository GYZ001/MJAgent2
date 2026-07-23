"""短时效 attachment_token：把用户已选择的上传文件换成 Command 可消费的凭证。

PRD §5.1 / §12.1：禁止把任意 ``file_path`` 暴露给 Agent。用户在系统文件选择器中选定文件后，
前端立即调用一次性上传换 token 的入口（见 ``app/api.py`` 的 ``POST /api/attachments/novel``），
随后无论页面 REST 还是 Agent/MCP 调用 ``project.import_novel``，都只传递 token，不传真实路径。

Token 落盘到临时文件、单次兑换、短时效自动失效，兑换或过期后立即删除临时文件。
"""
from __future__ import annotations

import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL_S = 15 * 60


@dataclass
class _StoredAttachment:
    token: str
    filename: str
    path: Path
    content_type: str | None
    expires_at: float
    consumed: bool = False


_STORE: dict[str, _StoredAttachment] = {}


def store_upload(
    filename: str,
    content: bytes,
    *,
    content_type: str | None = None,
    ttl_s: float = DEFAULT_TTL_S,
) -> str:
    """落盘到临时文件并登记短时效 token；用户选择/上传文件后立即调用一次。"""
    token = f"att_{secrets.token_hex(16)}"
    suffix = Path(filename or "upload").suffix or ".bin"
    fd, tmp_path = tempfile.mkstemp(prefix="manju_attach_", suffix=suffix)
    with open(fd, "wb") as handle:
        handle.write(content or b"")
    _STORE[token] = _StoredAttachment(
        token=token,
        filename=filename or "upload.txt",
        path=Path(tmp_path),
        content_type=content_type,
        expires_at=time.time() + max(ttl_s, 1.0),
    )
    return token


def _peek(token: str) -> _StoredAttachment:
    item = _STORE.get(token)
    if item is None:
        raise KeyError("attachment_token 不存在或已被使用")
    if item.consumed:
        _discard(token)
        raise KeyError("attachment_token 已被使用")
    if time.time() > item.expires_at:
        _discard(token)
        raise KeyError("attachment_token 已过期，请重新选择文件")
    return item


def consume(token: str) -> tuple[str, bytes]:
    """兑换 token 为 (filename, content)。只能兑换一次，兑换后立即失效并清理临时文件。"""
    if not token:
        raise KeyError("attachment_token 不能为空")
    item = _peek(token)
    try:
        content = item.path.read_bytes()
    finally:
        _discard(token)
    return item.filename, content


def _discard(token: str) -> None:
    item = _STORE.pop(token, None)
    if item is not None:
        try:
            item.path.unlink(missing_ok=True)
        except OSError:
            pass


def purge_expired() -> int:
    """清理过期未兑换的附件（可挂到定时任务/启动恢复）。"""
    now = time.time()
    expired = [token for token, item in _STORE.items() if now > item.expires_at]
    for token in expired:
        _discard(token)
    return len(expired)


def reset_for_tests() -> None:
    for token in list(_STORE.keys()):
        _discard(token)
