"""EP-03 第二阶段：密码策略——强度校验、历史口令去重、有效期（L2，见
app/LAYERS.toml::app.auth.password_policy）。

策略项全部走 ``settings``（``app.monitoring.SETTINGS_SCHEMA`` 新增的
``password_min_length``/``password_classes``/``password_max_age_days``/
``password_history_size`` 四项，见该模块改动），不新建配置体系。

这四个键**故意不重复写进** ``app.config.DEFAULT_SETTINGS``——那个文件已卡在
``FILE_CONVENTIONS.toml`` 行数棘轮基线（612 行）零余量，任何净增行都会撞线，
而 ``DEFAULT_SETTINGS`` 不是唯一真源：本模块下面每个 ``_min_length``/
``_required_classes``/``_history_size``/``max_age_days`` 都用
``get_setting(key) or <字面量默认值>`` 独立兜底同一份默认值（与
``SETTINGS_SCHEMA`` 各键的 ``"default"`` 逐一对齐），``get_setting()`` 读到
空字符串（settings 表缺该行、或表本身还没建）时这层兜底照样生效，行为与
"写进 DEFAULT_SETTINGS"完全等价。B 上 ``settings`` 表此前不可能有这些全新
键的旧值，不存在"旧值压掉新默认"的冲突。

``password_history`` 表的 DDL 放在 ``app.provisioning.schema``（同层，见该
模块文档「password_history」一段说明为什么不新起第三个包）——本模块只是
调用方，不重复定义建表语句。

三个写入调用点（``app.auth.admin_api.update_user`` 重置密码、
``app.auth.api.change_password`` 自助改密、``app.provisioning.invitations.
accept_invitation`` 邀请接受首次设密）都必须先调用 ``enforce_password_change``
再真正写 ``users.password_hash``，写完再调用 ``record_password_change`` 把
被替换掉的旧哈希归档——两步分开是因为"校验"不改任何状态、可以在
``HTTPException`` 时安全短路，而"归档"必须在新密码真正落库**之后**才有意义
（否则一次被拒绝的改密尝试也会污染历史表）。
"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from app.auth.passwords import verify_password
from app.db import get_setting, new_id, now
from app.provisioning import schema as provisioning_schema

_CLASS_LABELS: tuple[str, ...] = ("大写字母", "小写字母", "数字", "符号")


def _min_length() -> int:
    try:
        return int(float(get_setting("password_min_length") or 12))
    except (TypeError, ValueError):
        return 12


def _required_classes() -> int:
    try:
        return int(float(get_setting("password_classes") or 3))
    except (TypeError, ValueError):
        return 3


def _history_size() -> int:
    try:
        return int(float(get_setting("password_history_size") or 5))
    except (TypeError, ValueError):
        return 5


def max_age_days() -> float:
    try:
        return float(get_setting("password_max_age_days") or 0)
    except (TypeError, ValueError):
        return 0.0


def _classes_present(password: str) -> dict[str, bool]:
    return {
        "大写字母": any(c.isupper() for c in password),
        "小写字母": any(c.islower() for c in password),
        "数字": any(c.isdigit() for c in password),
        "符号": any(not c.isalnum() and not c.isspace() for c in password),
    }


def check_strength(password: str) -> None:
    """长度 + 字符类别校验；不满足时把每一条具体原因都列出来
    （CLAUDE.md「弱口令被拒时提示具体哪条不满足」），不是笼统一句话。"""
    violations: list[str] = []
    min_len = _min_length()
    if len(password) < min_len:
        violations.append(f"长度至少 {min_len} 位（当前 {len(password)} 位）")

    classes = _classes_present(password)
    present = [label for label in _CLASS_LABELS if classes[label]]
    required = _required_classes()
    if len(present) < required:
        missing = [label for label in _CLASS_LABELS if not classes[label]]
        violations.append(
            f"至少包含 {required} 类字符（大写字母/小写字母/数字/符号），"
            f"当前只有 {len(present)} 类（{'、'.join(present) or '无'}），"
            f"请从以下类别中再加入字符：{'、'.join(missing)}"
        )
    if violations:
        raise HTTPException(
            422,
            {"code": "weak_password", "message": "口令不满足密码策略", "violations": violations},
        )


def check_history_reuse(conn: sqlite3.Connection, *, user_id: str, password: str) -> None:
    """与最近 N 次历史口令（``password_history_size``，0=不校验）比对；命中则拒绝。
    只在 ``user_id`` 已有账号时调用——全新账号没有历史可比对。"""
    size = _history_size()
    if size <= 0:
        return
    # 同连接补列，不开独立连接：conn 是调用方传入的（app.auth.admin_api.
    # update_user 在同一请求里可能已经先写过别的字段但还没提交，见
    # app.auth.session_policy 模块文档同一条 2026-09-12 修复说明）。
    provisioning_schema.ensure_tables_on_connection(conn)
    rows = conn.execute(
        "SELECT password_hash FROM password_history WHERE user_id=? ORDER BY changed_at DESC LIMIT ?",
        (user_id, size),
    ).fetchall()
    for row in rows:
        if verify_password(password, row["password_hash"]):
            raise HTTPException(
                422,
                {
                    "code": "password_reused",
                    "message": f"新口令不能与最近 {size} 次使用过的口令相同，请换一个未用过的口令",
                    "violations": [f"与最近 {size} 次历史口令中的一条重复"],
                },
            )


def enforce_password_change(
    conn: sqlite3.Connection, *, user_id: str | None, new_password: str, current_password_hash: str | None,
) -> None:
    """强度 + 历史重用双重校验，不落库。``user_id=None``（全新账号，管理员开户
    /CSV 导入/邀请接受首次设密）时跳过历史比对——没有历史可比对。"""
    check_strength(new_password)
    if current_password_hash and verify_password(new_password, current_password_hash):
        raise HTTPException(
            422,
            {
                "code": "password_reused", "message": "新口令不能与当前口令相同",
                "violations": ["与当前口令相同"],
            },
        )
    if user_id is not None:
        check_history_reuse(conn, user_id=user_id, password=new_password)


def record_password_change(conn: sqlite3.Connection, *, user_id: str, old_password_hash: str | None) -> None:
    """把**被替换掉**的旧口令哈希归档进 ``password_history``，裁剪到只留最近
    ``password_history_size`` 条。``old_password_hash=None``（全新账号第一次
    设密，没有旧值可归档）时是no-op。调用方负责在真正写入新
    ``users.password_hash`` 之后调用本函数；本函数自己 commit（与
    ``app.orgs.store`` 不同——这里没有调用方共享事务的需求，见各调用点）。"""
    if not old_password_hash:
        return
    provisioning_schema.ensure_tables_on_connection(conn)  # 同连接补列，理由同上
    conn.execute(
        "INSERT INTO password_history(id, user_id, password_hash, changed_at) VALUES(?,?,?,?)",
        (new_id("pwh"), user_id, old_password_hash, now()),
    )
    size = _history_size()
    if size > 0:
        conn.execute(
            "DELETE FROM password_history WHERE user_id=? AND id NOT IN ("
            "SELECT id FROM password_history WHERE user_id=? ORDER BY changed_at DESC LIMIT ?)",
            (user_id, user_id, size),
        )
    conn.commit()


def is_password_expired(*, password_changed_at: float | None, created_at: float) -> bool:
    """有效期判据：``password_max_age_days=0`` 永不过期；否则基线取
    ``password_changed_at``（从未改过密则退化为 ``created_at``，账号开户时
    设的初始口令同样受有效期约束）。"""
    days = max_age_days()
    if days <= 0:
        return False
    baseline = password_changed_at if password_changed_at else created_at
    return now() - float(baseline) > days * 86400.0


def flag_expired_password(conn: sqlite3.Connection, *, user_id: str, password_changed_at: float | None, created_at: float) -> bool:
    """登录成功后调用：口令已过期则强制置位 ``must_change_password``，返回是否
    刚刚置位（供调用方决定要不要在响应里额外提示"口令已过期"）。已经是
    1 的不重复写库。"""
    if not is_password_expired(password_changed_at=password_changed_at, created_at=created_at):
        return False
    cur = conn.execute(
        "UPDATE users SET must_change_password=1 WHERE id=? AND must_change_password=0", (user_id,),
    )
    conn.commit()
    return cur.rowcount > 0
