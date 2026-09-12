"""EP-02 JIT 自动开户与角色映射（L2，见 app/LAYERS.toml::app.sso.provision）。

规则表从 claim 推导，命中即用，未命中走 ``on_no_match``（PRD EP-02 §5）。
**唯一身份键是 ``(idp_id, sub)``，不是 email**——本模块自始至终不用 email 做
任何查找/匹配键，只用作展示字段落库。**禁止把 IdP 的管理员标记自动映射成
``is_system_admin``**：新建账号的 ``is_system_admin`` 硬编码为 0，本模块没有
任何代码路径读取或转发"管理员"类 claim，IdP 被攻破时不会直接失守本系统的
最高权限（这是结构性保证，不是靠某处判断"漏掉"就失效的检查）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.db import get_conn, new_id, now
from app.orgs import store as orgs_store
from app.sso import store as sso_store


class NoAccountError(Exception):
    """``auto_create=false`` 且该 subject 尚未开户——调用方应提示"联系管理员"
    并在管理员侧留痕（这里体现为一条 operation_audit 行，见 record_sso_audit）。
    """


class ProvisionRejectedError(Exception):
    """规则未命中且 ``on_no_match=reject``。"""


@dataclass(frozen=True)
class NormalizedClaims:
    subject: str
    username: str
    display_name: str
    email: str | None
    dept: str | None
    groups: tuple[str, ...] = field(default_factory=tuple)
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProvisionResult:
    user_id: str
    created: bool
    team_id: str | None = None
    role_id: str | None = None


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_groups(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(g.strip() for g in value.split(",") if g.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(g) for g in value)
    return ()


def normalize_claims(idp_row: dict, raw_claims: dict) -> NormalizedClaims:
    """按 ``claim_map_json`` 把身份提供方原始 claims 翻译成标准字段；
    ``claim_map`` 缺失的字段各自回退到 OIDC 常见 claim 名。"""
    claim_map = json.loads(idp_row.get("claim_map_json") or "{}")

    def pick(field_name: str, default_key: str) -> object:
        return raw_claims.get(claim_map.get(field_name, default_key))

    subject = _as_str(pick("subject", "sub"))
    if not subject:
        raise ValueError("身份提供方未返回有效的 subject（sub）声明")
    username = _as_str(pick("username", "preferred_username")) or subject
    display_name = _as_str(pick("display_name", "name")) or username
    return NormalizedClaims(
        subject=subject, username=username, display_name=display_name,
        email=_as_str(pick("email", "email")), dept=_as_str(pick("dept", "department")),
        groups=_as_groups(pick("groups", "groups")), raw=raw_claims,
    )


def _rule_claim_value(claims: NormalizedClaims, claim_key: str) -> object:
    if claim_key == "groups":
        return claims.groups
    return claims.raw.get(claim_key)


def _op_matches(op: str, value: object, target: object) -> bool:
    if op == "equals":
        return value == target
    if op == "contains":
        if isinstance(value, (list, tuple, set, frozenset)):
            return target in value
        return isinstance(value, str) and isinstance(target, str) and target in value
    raise ValueError(f"不支持的规则操作符：{op!r}（仅支持 equals/contains）")


def match_provision_rule(provision_cfg: dict, claims: NormalizedClaims) -> tuple[str | None, str | None]:
    """按 ``rules`` 顺序匹配，命中即返回 ``(team_id, role_id)``；未命中
    返回 ``(None, None)``。

    命中的规则允许只写 ``team_id`` 或只写 ``role_id`` 之一——PRD EP-02 §5
    第二条示例规则（``{"claim": "groups", "op": "contains", "value":
    "manju-admins", "role_id": "role_org_admin"}``）就没有 ``team_id``：
    意图是"这个人还是进默认团队，但角色换成 org_admin"，不是"命中了就不
    落地任何团队成员资格"。缺的那一半从 ``default_team_id``/
    ``default_role_id`` 补，两者都缺才真正视为未命中（调用方据此走
    ``on_no_match``）。
    """
    for rule in provision_cfg.get("rules") or []:
        claim_key = str(rule.get("claim") or "")
        op = str(rule.get("op") or "equals")
        if _op_matches(op, _rule_claim_value(claims, claim_key), rule.get("value")):
            team_id = rule.get("team_id") or provision_cfg.get("default_team_id")
            role_id = rule.get("role_id") or provision_cfg.get("default_role_id")
            return team_id, role_id
    return None, None


def _unique_username(conn, preferred: str) -> str:
    if not conn.execute("SELECT 1 FROM users WHERE username=?", (preferred,)).fetchone():
        return preferred
    suffix = new_id("u").rsplit("_", 1)[-1]
    return f"{preferred}_{suffix}"


def record_sso_audit(
    *, event: str, outcome: str, user_id: str | None = None, username: str | None = None,
    target: str | None = None, summary: str | None = None, error_code: str | None = None,
) -> None:
    """全链路审计入口：登录成功/失败/自动开户/角色映射结果都经这里落
    ``operation_audit``（PRD EP-02 §2 P0-6）。GET 发起的登录回调不是
    mutating REST 方法，不会被 ``app.audit.recorder`` 的 HTTP 中间件自动
    记录，必须显式写——与 ``app.models_registry.routing.record_route_failure``
    同一手法（直接调 ``insert_operation_audit_row``，不经总线）。"""
    from app.audit.store import insert_operation_audit_row

    insert_operation_audit_row({
        "id": new_id("opaudit"), "ts": now(),
        "user_id": user_id, "username": username, "is_system_admin": None,
        "source": "sso", "event": event, "event_label": None,
        "method": None, "path": None, "project_id": None, "episode_id": None,
        "target": target, "outcome": outcome, "http_status": None,
        "error_id": None, "error_code": error_code, "summary": summary,
        "duration_ms": None, "ip": None, "user_agent": None, "args_json": "{}",
    })


def _create_sso_user(conn, idp_row: dict, claims: NormalizedClaims) -> str:
    """新建账号：``is_system_admin`` 恒为 0（见模块文档），``password_hash``
    恒为 NULL（SSO 用户没有本地口令，天然无法走 ``POST /api/auth/login``）。
    """
    user_id = new_id("user")
    ts = now()
    conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, auth_provider, "
        "external_subject, status, is_system_admin, must_change_password, created_at, org_id) "
        "VALUES(?,?,?,NULL,?,?,'active',0,0,?,?)",
        (
            user_id, _unique_username(conn, claims.username), claims.display_name,
            idp_row["kind"], claims.subject, ts, idp_row.get("org_id"),
        ),
    )
    return user_id


def provision_or_login(idp_row: dict, claims: NormalizedClaims) -> ProvisionResult:
    """已绑定 -> 直接登录并续last_login；未绑定 -> 按 ``provision_json`` 做
    JIT 开户 + 团队/角色映射。整个函数是单一事务：任何一步失败都不该留下
    半开的账号或半绑定的身份。
    """
    conn = get_conn()
    idp_id = idp_row["id"]
    existing = sso_store.get_identity(conn, idp_id, claims.subject)
    if existing is not None:
        sso_store.update_identity_last_login(conn, idp_id, claims.subject, now())
        conn.commit()
        return ProvisionResult(user_id=existing["user_id"], created=False)

    provision_cfg = json.loads(idp_row.get("provision_json") or "{}")
    if not bool(provision_cfg.get("auto_create", True)):
        record_sso_audit(
            event="sso.provision_no_account", outcome="rejected", target=claims.subject,
            summary=f"idp={idp_id} subject={claims.subject} auto_create=false 且未预先开户",
        )
        raise NoAccountError(claims.subject)

    team_id, role_id = match_provision_rule(provision_cfg, claims)
    if team_id is None and role_id is None:
        if str(provision_cfg.get("on_no_match") or "default") == "reject":
            record_sso_audit(
                event="sso.provision_rejected", outcome="rejected", target=claims.subject,
                summary=f"idp={idp_id} subject={claims.subject} 未命中任何映射规则，on_no_match=reject",
            )
            raise ProvisionRejectedError(claims.subject)
        team_id = provision_cfg.get("default_team_id")
        role_id = provision_cfg.get("default_role_id")

    user_id = _create_sso_user(conn, idp_row, claims)
    sso_store.link_identity(conn, user_id=user_id, idp_id=idp_id, external_subject=claims.subject)
    mapped = False
    if team_id and role_id and orgs_store.get_team(conn, team_id) and orgs_store.get_role(conn, role_id):
        orgs_store.add_team_member(conn, team_id=team_id, user_id=user_id, role_id=role_id, created_by="system:sso")
        mapped = True
    conn.commit()
    record_sso_audit(
        event="sso.provision_created", outcome="ok", user_id=user_id, username=claims.username,
        target=claims.subject, summary=f"idp={idp_id} team_id={team_id} role_id={role_id} mapped={mapped}",
    )
    return ProvisionResult(user_id=user_id, created=True, team_id=team_id if mapped else None, role_id=role_id if mapped else None)
