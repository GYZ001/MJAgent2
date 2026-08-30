"""MCP Bearer Token 管理（PRD AGENT_MCP_CAPABILITY §9.6 / §12.2）。

本地单用户部署也必须认证：token 明文只在创建时返回一次，落盘只存 hash；
可随时撤销；scope 与 Capability Registry 的 `manju:*` 完全对齐。

账号即项目空间落地后，token 还必须携带**归属账号**：``app.domain.common.
_project_or_404``/``_episode_or_404``（domain 层唯一的项目/剧集存在性入口，
账号级隔离的咽喉点）只看 ``app.auth.principal.get_current_principal()``，
MCP 请求此前从不注入 Principal，等价于「未挂鉴权闸门的内部调用」，任何
token 都能触达任意账号的项目——见 ``app/mcp/server.py::_principal_from_claims``
如何把这里签发的 claims 转成一个真实 ``Principal`` 并注入同一个 ContextVar。
token 创建时若上下文里没有 Principal（bootstrap token、脚本/测试直接调用
``create_token()``），归属留空，落到「未绑定账号」这一档——``_principal_from_
claims`` 会把它合成一个不匹配任何真实账号的身份，结构上拒绝一切项目级资源，
而不是放行（CLAUDE 记录的「空集合不等于无需检查」）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.atomic_io import atomic_write_text
from app.auth.principal import get_current_principal
from app.config import DATA_DIR

TOKENS_PATH = DATA_DIR / "mcp_tokens.json"
BOOTSTRAP_TOKEN_PATH = DATA_DIR / "mcp_bootstrap_token.txt"

# 与 Capability Registry 的 CommandSpec.scopes 完全一致（PRD §9.6）。
ALL_SCOPES = frozenset(
    {
        "manju:read",
        "manju:project-write",
        "manju:generation-text",
        "manju:generation-media",
        "manju:delivery",
        "manju:admin",
    }
)

DEFAULT_SCOPES = frozenset({"manju:read"})
TOKEN_PREFIX = "mcp"

_lock = threading.Lock()


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    token_id: str
    scopes: frozenset[str]
    name: str | None = None
    expires_at: float | None = None
    # 归属账号：token 创建时从 get_current_principal() 捕获，None 表示「未绑定
    # 账号」（bootstrap token / 脚本或测试在无 HTTP 会话上下文时直接调用
    # create_token()）。见 app/mcp/server.py::_principal_from_claims 的 fail
    # closed 处理——未绑定不等于放行，而是不匹配任何真实账号。
    owner_user_id: str | None = None
    owner_username: str | None = None
    is_system_admin: bool = False


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _load() -> dict[str, Any]:
    if not TOKENS_PATH.exists():
        return {"tokens": {}}
    try:
        data = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # 文件存在但不可读/损坏时 fail closed，禁止当作「首次启动」去签发 bootstrap token。
        raise RuntimeError(
            f"MCP token 存储损坏或不可读：{TOKENS_PATH}；请人工修复或删除后重启"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        raise RuntimeError(
            f"MCP token 存储格式非法：{TOKENS_PATH}；请人工修复或删除后重启"
        )
    return data


def _save(data: dict[str, Any]) -> None:
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(TOKENS_PATH, json.dumps(data, ensure_ascii=False, indent=2))


def _normalize_scopes(scopes: list[str] | tuple[str, ...] | None) -> frozenset[str]:
    if scopes is None:
        return DEFAULT_SCOPES
    requested = frozenset(str(s).strip() for s in scopes if str(s).strip())
    invalid = requested - ALL_SCOPES
    if invalid:
        raise ValueError(f"unknown scopes: {sorted(invalid)}")
    return requested or DEFAULT_SCOPES


def _is_expired(record: dict[str, Any]) -> bool:
    expires_at = record.get("expires_at")
    return bool(expires_at) and time.time() > float(expires_at)


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "name": record.get("name") or "",
        "scopes": sorted(record.get("scopes") or []),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
        "revoked_at": record.get("revoked_at"),
        "last_used_at": record.get("last_used_at"),
        "active": record.get("revoked_at") is None and not _is_expired(record),
        # 界面承诺必须与实际行为一致：token 能碰到哪个账号的项目，列表就必须
        # 如实显示，不能只显示 scope 让人误以为「有 scope 就能碰所有项目」。
        "owner_user_id": record.get("owner_user_id"),
        "owner_username": record.get("owner_username") or "",
        "is_system_admin": bool(record.get("is_system_admin")),
    }


def create_token(
    scopes: list[str] | None = None,
    ttl_s: int | None = None,
    *,
    name: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """创建一枚新 token。返回值 (明文 token, 脱敏记录)；明文只在此处出现一次。"""
    normalized = _normalize_scopes(scopes)
    if ttl_s is not None and ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    token_id = f"mcpt_{secrets.token_hex(8)}"
    secret = secrets.token_urlsafe(32)
    # token_id 本身含下划线，明文用 "." 分隔 id/secret，避免解析时被 "_" 切错段。
    plaintext = f"{TOKEN_PREFIX}_{token_id}.{secret}"
    created_at = time.time()
    # 归属账号从签发时的请求身份捕获，与 app/auth/principal.py::current_actor_name
    # 记审计字段同一个口径——不接受调用方自报 user_id，只信 get_current_principal()。
    # 没有 Principal（bootstrap/脚本/测试）时留空，落到「未绑定账号」一档，见
    # 本文件顶部模块 docstring 与 app/mcp/server.py::_principal_from_claims。
    principal = get_current_principal()
    record = {
        "id": token_id,
        "name": (name or "").strip()[:80],
        "scopes": sorted(normalized),
        "secret_hash": _hash_secret(secret),
        "created_at": created_at,
        "expires_at": (created_at + ttl_s) if ttl_s else None,
        "revoked_at": None,
        "last_used_at": None,
        "owner_user_id": principal.user_id if principal is not None else None,
        "owner_username": principal.username if principal is not None else None,
        "is_system_admin": bool(principal.is_system_admin) if principal is not None else False,
    }
    with _lock:
        data = _load()
        data["tokens"][token_id] = record
        _save(data)
    return plaintext, _public_record(record)


def revoke_token(token_id: str) -> bool:
    with _lock:
        data = _load()
        record = data["tokens"].get(token_id)
        if not record:
            return False
        record["revoked_at"] = time.time()
        _save(data)
    return True


def list_tokens() -> list[dict[str, Any]]:
    data = _load()
    records = sorted(data["tokens"].values(), key=lambda r: r.get("created_at") or 0, reverse=True)
    return [_public_record(record) for record in records]


def validate_bearer(header: str | None) -> TokenClaims:
    if not header or not header.strip():
        raise AuthError("missing_token", "缺少 Authorization: Bearer <token>", 401)
    parts = header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError("invalid_scheme", "Authorization 必须是 Bearer <token> 格式", 401)
    token = parts[1].strip()
    prefix = f"{TOKEN_PREFIX}_"
    if not token.startswith(prefix) or "." not in token:
        raise AuthError("invalid_token", "token 格式不合法", 401)
    token_id, _, secret = token[len(prefix):].partition(".")
    if not token_id or not secret:
        raise AuthError("invalid_token", "token 格式不合法", 401)
    with _lock:
        data = _load()
        record = data["tokens"].get(token_id)
        if not record:
            raise AuthError("invalid_token", "token 不存在或已被撤销", 401)
        if record.get("revoked_at"):
            raise AuthError("revoked_token", "token 已被撤销", 401)
        if _is_expired(record):
            raise AuthError("expired_token", "token 已过期", 401)
        if not hmac.compare_digest(_hash_secret(secret), str(record.get("secret_hash") or "")):
            raise AuthError("invalid_token", "token 不合法", 401)
        record["last_used_at"] = time.time()
        _save(data)
    return TokenClaims(
        token_id=token_id,
        scopes=frozenset(record.get("scopes") or []),
        name=record.get("name") or None,
        expires_at=record.get("expires_at"),
        owner_user_id=record.get("owner_user_id"),
        owner_username=record.get("owner_username"),
        is_system_admin=bool(record.get("is_system_admin")),
    )


def ensure_bootstrap_token() -> None:
    """首次启动、且尚无任何 token 时，自动生成一枚本机只读 token 方便联调。

    明文只写入一次性本地文件 ``data/mcp_bootstrap_token.txt``（不进日志/不进 DB）。
    默认仅 ``manju:read`` + 24h TTL；写权限请通过监制房人工签发（Todolist T3）。
    """
    with _lock:
        data = _load()
        if data["tokens"]:
            return
    plaintext, _ = create_token(
        scopes=sorted(DEFAULT_SCOPES),
        ttl_s=24 * 60 * 60,
        name="bootstrap-local-readonly",
    )
    try:
        atomic_write_text(
            BOOTSTRAP_TOKEN_PATH,
            f"{plaintext}\n# 本机首次启动自动生成（只读，24h）；可在监制房撤销后重新创建更小范围的 token。\n",
        )
        try:
            BOOTSTRAP_TOKEN_PATH.chmod(0o600)
        except OSError:
            pass
    except OSError:
        pass
