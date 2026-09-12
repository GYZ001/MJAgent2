"""EP-02 企业身份接入的存取层（L2，见 app/LAYERS.toml::app.sso）。

只做 SQL 读写 + client_secret 的加解密封装，零业务判定（规则匹配/开户在
``app.sso.provision``，OIDC 协议在 ``app.sso.oidc``）。每个函数的
``conn: sqlite3.Connection`` 是必填参数，不留 ``conn=None``（CLAUDE.md「可选
参数是缺陷的温床」）；函数内部不调用 ``conn.commit()``——事务边界由调用方
决定，与 ``app/orgs/store.py`` 同一惯例。三个例外——``purge_expired_auth_
requests()``/``consume_break_glass_code()``/``consume_login_exchange()``
各自独立开连接并自行提交——见各自 docstring：前两者分别是巡检任务的诊断性
清理（CLAUDE.md「诊断类写入用独立连接」）与运维恢复码的一次性核销，第三者
是会话交接一次性交换码的核销，三者都是自成一体的独立动作，不该与调用方的
其它事务绑在一起。

接受调用方 conn 的函数用 ``schema.ensure_tables_on_connection(conn)`` 兜底
建表（同连接、不开新连接、不申请新锁）——本模块可能被别的模块在调用方已
持有的 ``BEGIN IMMEDIATE`` 事务里直接传 ``conn`` 调用，沿用
``schema.ensure_schema()`` 会跟调用方抢写锁、2 秒超时后静默建表失败（见
``app/sso/schema.py`` 模块文档）；三个自成一体独立开连接的例外仍用
``ensure_schema()``。

client_secret 加密**复用** ``app.models_registry.crypto`` 的 AES-256-GCM 原语
（``gcm_encrypt``/``gcm_decrypt``），不在本模块重写算法；主密钥复用
``app.models_registry.keyprovider`` 的同一份 ``data/master.key``（同一台机器
上没有理由为"模型凭据"和"IdP 客户端密钥"两类同性质的机密各开一份主密钥
文件）。AAD 绑定 ``idp_id``（``_idp_secret_aad``），与
``crypto.credential_aad()`` 绑定 ``model_id`` 同一思路：密文被挪到另一个
``idp_id`` 的行下面会认证失败，不会解出"看似正常"的错误明文。
"""
from __future__ import annotations

import os
import sqlite3

from app.db import get_conn, new_id, now
from app.models_registry import crypto
from app.models_registry.keyprovider import get_default_provider
from app.sso import schema

AUTH_REQUEST_TTL_S = 10 * 60  # PRD EP-02 §3：state/nonce/PKCE 一次性凭据 10 分钟过期
BREAK_GLASS_CODE_TTL_S = 10 * 60


def _idp_secret_aad(idp_id: str) -> bytes:
    idp_id = str(idp_id or "").strip()
    if not idp_id:
        raise ValueError("idp_id 不能为空——凭据必须绑定到具体 IdP")
    return f"identity_providers:{idp_id}".encode("utf-8")


def encrypt_client_secret(idp_id: str, plaintext: str) -> tuple[bytes, bytes]:
    """返回 ``(nonce, ciphertext_with_tag)``，两者都要落库。"""
    key = get_default_provider().get_key()
    nonce = os.urandom(crypto.NONCE_LEN)
    ciphertext = crypto.gcm_encrypt(key, nonce, _idp_secret_aad(idp_id), plaintext.encode("utf-8"))
    return nonce, ciphertext


def decrypt_client_secret(idp_id: str, nonce: bytes, ciphertext: bytes) -> str:
    key = get_default_provider().get_key()
    plaintext = crypto.gcm_decrypt(key, nonce, _idp_secret_aad(idp_id), ciphertext)
    return plaintext.decode("utf-8")


# ---------------------------------------------------------------------------
# identity_providers
# ---------------------------------------------------------------------------


def create_idp(
    conn: sqlite3.Connection, *, org_id: str | None, kind: str, name: str, issuer: str | None,
    client_id: str, client_secret_plain: str | None, discovery_url: str | None,
    authorize_url: str | None, token_url: str | None, userinfo_url: str | None,
    jwks_url: str | None, scopes: str, claim_map_json: str, provision_json: str,
    allowed_domains: str | None, enabled: bool, created_by: str,
) -> str:
    schema.ensure_tables_on_connection(conn)
    idp_id = new_id("idp")
    nonce = ciphertext = None
    if client_secret_plain:
        nonce, ciphertext = encrypt_client_secret(idp_id, client_secret_plain)
    ts = now()
    conn.execute(
        "INSERT INTO identity_providers("
        "id, org_id, kind, name, enabled, issuer, client_id, client_secret_ciphertext, "
        "client_secret_nonce, discovery_url, authorize_url, token_url, userinfo_url, "
        "jwks_url, scopes, claim_map_json, provision_json, allowed_domains, "
        "created_at, updated_at, created_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            idp_id, org_id, kind, name, int(enabled), issuer, client_id, ciphertext, nonce,
            discovery_url, authorize_url, token_url, userinfo_url, jwks_url, scopes,
            claim_map_json, provision_json, allowed_domains, ts, ts, created_by,
        ),
    )
    return idp_id


def get_idp(conn: sqlite3.Connection, idp_id: str) -> dict | None:
    schema.ensure_tables_on_connection(conn)
    row = conn.execute("SELECT * FROM identity_providers WHERE id=?", (idp_id,)).fetchone()
    return dict(row) if row else None


def list_idps(conn: sqlite3.Connection, *, org_id: str | None = None, enabled_only: bool = False) -> list[dict]:
    schema.ensure_tables_on_connection(conn)
    clauses: list[str] = []
    params: list[object] = []
    if org_id is not None:
        clauses.append("(org_id=? OR org_id IS NULL)")
        params.append(org_id)
    if enabled_only:
        clauses.append("enabled=1")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM identity_providers{where} ORDER BY created_at", params).fetchall()
    return [dict(r) for r in rows]


def update_idp(conn: sqlite3.Connection, idp_id: str, **fields: object) -> None:
    """局部更新：只有传入的键才会被写入。``client_secret_plain`` 特殊处理——
    非 None 时重新加密并覆盖两列，其余键原样映射到同名列。"""
    schema.ensure_tables_on_connection(conn)
    assignments: list[str] = []
    values: list[object] = []
    client_secret_plain = fields.pop("client_secret_plain", None)
    for key, value in fields.items():
        assignments.append(f"{key}=?")
        values.append(int(value) if key == "enabled" else value)
    if client_secret_plain:
        nonce, ciphertext = encrypt_client_secret(idp_id, str(client_secret_plain))
        assignments += ["client_secret_ciphertext=?", "client_secret_nonce=?"]
        values += [ciphertext, nonce]
    if not assignments:
        return
    assignments.append("updated_at=?")
    values.append(now())
    values.append(idp_id)
    conn.execute(f"UPDATE identity_providers SET {', '.join(assignments)} WHERE id=?", values)


def delete_idp(conn: sqlite3.Connection, idp_id: str) -> None:
    schema.ensure_tables_on_connection(conn)
    conn.execute("DELETE FROM identity_providers WHERE id=?", (idp_id,))


# ---------------------------------------------------------------------------
# user_identities
# ---------------------------------------------------------------------------


def get_identity(conn: sqlite3.Connection, idp_id: str, external_subject: str) -> dict | None:
    schema.ensure_tables_on_connection(conn)
    row = conn.execute(
        "SELECT * FROM user_identities WHERE idp_id=? AND external_subject=?",
        (idp_id, external_subject),
    ).fetchone()
    return dict(row) if row else None


def list_identities_for_user(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    schema.ensure_tables_on_connection(conn)
    rows = conn.execute(
        "SELECT * FROM user_identities WHERE user_id=? ORDER BY linked_at", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def link_identity(conn: sqlite3.Connection, *, user_id: str, idp_id: str, external_subject: str) -> None:
    """新建绑定；``(idp_id, external_subject)`` 已绑定到别的账号时拒绝——
    调用方（``app.sso.provision``/``POST /api/auth/sso/link``）必须先用
    ``get_identity`` 确认没有冲突，这里再兜底一次防御性校验。"""
    existing = get_identity(conn, idp_id, external_subject)
    if existing is not None and existing["user_id"] != user_id:
        raise ValueError(f"该 IdP 身份已绑定到另一个账号（user_id={existing['user_id']!r}）")
    ts = now()
    conn.execute(
        "INSERT INTO user_identities(user_id, idp_id, external_subject, linked_at, last_login_at) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(idp_id, external_subject) DO UPDATE SET last_login_at=excluded.last_login_at",
        (user_id, idp_id, external_subject, ts, ts),
    )


def update_identity_last_login(conn: sqlite3.Connection, idp_id: str, external_subject: str, ts: float) -> None:
    schema.ensure_tables_on_connection(conn)
    conn.execute(
        "UPDATE user_identities SET last_login_at=? WHERE idp_id=? AND external_subject=?",
        (ts, idp_id, external_subject),
    )


def unlink_identity(conn: sqlite3.Connection, *, user_id: str, idp_id: str) -> bool:
    schema.ensure_tables_on_connection(conn)
    cur = conn.execute(
        "DELETE FROM user_identities WHERE user_id=? AND idp_id=?", (user_id, idp_id)
    )
    return cur.rowcount > 0


def login_method_count(conn: sqlite3.Connection, user_id: str) -> int:
    """本账号当前拥有的登录方式数：本地口令（非空 password_hash）+ 每一条
    已绑定的 IdP 身份。解绑最后一个登录方式必须被拒绝（PRD EP-02 §4）。"""
    schema.ensure_tables_on_connection(conn)
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
    has_password = 1 if row is not None and row["password_hash"] else 0
    identity_count = conn.execute(
        "SELECT COUNT(*) c FROM user_identities WHERE user_id=?", (user_id,)
    ).fetchone()["c"]
    return has_password + int(identity_count)


# ---------------------------------------------------------------------------
# sso_auth_requests：state/nonce/PKCE 一次性凭据
# ---------------------------------------------------------------------------


def create_auth_request(
    conn: sqlite3.Connection, *, idp_id: str, nonce: str, code_verifier: str,
    redirect_to: str, link_user_id: str | None, ip: str | None,
) -> str:
    schema.ensure_tables_on_connection(conn)
    state = new_id("ssoreq")
    conn.execute(
        "INSERT INTO sso_auth_requests(state, idp_id, nonce, code_verifier, redirect_to, "
        "link_user_id, created_at, consumed_at, ip) VALUES(?,?,?,?,?,?,?,NULL,?)",
        (state, idp_id, nonce, code_verifier, redirect_to, link_user_id, now(), ip),
    )
    return state


def consume_auth_request(conn: sqlite3.Connection, state: str) -> dict | None:
    """一次性消费：原子声明（``consumed_at IS NULL`` 的 UPDATE 只可能被一个
    调用者抢到），再校验未过期。重放（第二次用同一个 state）与过期都返回
    ``None``——调用方据此一律回 400，不区分"从未存在"/"已用过"/"过了 10
    分钟"三种原因，避免向攻击者泄露状态机细节。
    """
    schema.ensure_tables_on_connection(conn)
    ts = now()
    cur = conn.execute(
        "UPDATE sso_auth_requests SET consumed_at=? WHERE state=? AND consumed_at IS NULL",
        (ts, state),
    )
    if cur.rowcount != 1:
        return None
    row = conn.execute("SELECT * FROM sso_auth_requests WHERE state=?", (state,)).fetchone()
    if row is None or float(row["created_at"]) + AUTH_REQUEST_TTL_S < ts:
        return None
    return dict(row)


def purge_expired_auth_requests() -> int:
    """独立连接 + 自行提交（诊断性清理，不复用调用方连接/事务）。由
    ``app.audit.retention.operation_audit_sweep_loop`` 的既有 6 小时巡检循环
    调用（PRD EP-02 §10 陷阱 2：不新开定时器），也可能被过期请求较多的测试
    直接调用。"""
    conn = get_conn()
    cutoff = now() - AUTH_REQUEST_TTL_S
    cur = conn.execute("DELETE FROM sso_auth_requests WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# sso_break_glass_codes：PRD §6 强制 SSO 下的本地应急登录通道
# ---------------------------------------------------------------------------


def create_break_glass_code(conn: sqlite3.Connection, *, user_id: str, code_hash: str, created_by: str) -> str:
    schema.ensure_tables_on_connection(conn)
    code_id = new_id("bgcode")
    ts = now()
    conn.execute(
        "INSERT INTO sso_break_glass_codes(id, user_id, code_hash, created_at, expires_at, "
        "used_at, created_by) VALUES(?,?,?,?,?,NULL,?)",
        (code_id, user_id, code_hash, ts, ts + BREAK_GLASS_CODE_TTL_S, created_by),
    )
    return code_id


def consume_break_glass_code(*, user_id: str, code_hash: str) -> bool:
    """独立连接 + 自行提交：一次性恢复码的核销是自成一体的运维动作，不该与
    调用方（``POST /api/auth/sso/break-glass``）后续要做的"签发会话"绑在
    同一个事务里——即便签发会话那一步失败，核销也不该被回滚重新变得可用。
    自成一体独立开连接（不接受调用方 conn），继续用 ``ensure_schema()``。
    """
    schema.ensure_schema()
    conn = get_conn()
    ts = now()
    cur = conn.execute(
        "UPDATE sso_break_glass_codes SET used_at=? WHERE user_id=? AND code_hash=? "
        "AND used_at IS NULL AND expires_at > ?",
        (ts, user_id, code_hash, ts),
    )
    conn.commit()
    return cur.rowcount == 1


# ---------------------------------------------------------------------------
# sso_login_exchanges：OIDC callback 302 跳转不能带真实会话令牌（2026-09-12），
# 只带一枚一次性交换码；真会话令牌只在 POST /api/auth/sso/exchange 的响应体
# 里返回，不进访问日志/浏览器历史/Referer。
# ---------------------------------------------------------------------------

LOGIN_EXCHANGE_TTL_S = 60


def create_login_exchange(conn: sqlite3.Connection, *, user_id: str, code_hash: str) -> None:
    """只存 ``sha256(code)``，从不落 code 明文——与 break_glass 的 code_hash
    同一约定。``conn`` 由调用方（``app.sso.api._finish_login``）提交。"""
    schema.ensure_tables_on_connection(conn)
    ts = now()
    conn.execute(
        "INSERT INTO sso_login_exchanges(code_hash, user_id, created_at, expires_at, consumed_at) "
        "VALUES(?,?,?,?,NULL)",
        (code_hash, user_id, ts, ts + LOGIN_EXCHANGE_TTL_S),
    )


def consume_login_exchange(*, code_hash: str) -> dict | None:
    """独立连接 + 自行提交：与 ``consume_break_glass_code`` 同一理由，一次性
    交换码的核销是自成一体的动作，不该与调用方后续"签发会话"绑在同一事务。
    重放（已消费）与过期都返回 ``None``，调用方一律回 400，不区分具体原因。
    自成一体独立开连接（不接受调用方 conn），继续用 ``ensure_schema()``。
    """
    schema.ensure_schema()
    conn = get_conn()
    ts = now()
    cur = conn.execute(
        "UPDATE sso_login_exchanges SET consumed_at=? WHERE code_hash=? "
        "AND consumed_at IS NULL AND expires_at > ?",
        (ts, code_hash, ts),
    )
    if cur.rowcount != 1:
        conn.commit()
        return None
    row = conn.execute("SELECT user_id FROM sso_login_exchanges WHERE code_hash=?", (code_hash,)).fetchone()
    conn.commit()
    return dict(row) if row else None


def purge_expired_login_exchanges() -> int:
    """独立连接 + 自行提交（诊断性清理）。与 ``purge_expired_auth_requests``
    同一巡检循环调用（见 ``app.audit.retention``），TTL 只有 60 秒，理论上
    多数行会先被兑换消费掉，这里兜底清理"发了从没兑换"的少数遗留行。"""
    conn = get_conn()
    cur = conn.execute("DELETE FROM sso_login_exchanges WHERE expires_at < ?", (now(),))
    conn.commit()
    return cur.rowcount
