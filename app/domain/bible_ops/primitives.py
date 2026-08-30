"""人物谱/画风/角色选择/引用目标/支付报价的共享小函数与常量。

从 app/domain/bible_ops.py 按原样搬移，未改逻辑；被本包其余大多数子模块依赖，是本包唯一没有反向依赖的基础层。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    new_id,
    now,
)
from app.refs import (
    SCENE_CANONICAL_MAX_CHARS,
    SCENE_CANONICAL_MIN_CHARS,
)
from app.visual_styles import (
    DEFAULT_VISUAL_STYLE_NAME,
    default_visual_style_prompt,
    visual_style_prompt,
)
from fastapi import HTTPException


def _scene_canonical_length_ok(canonical: str) -> bool:
    """场景锚点长度闸。三个入口和 validate_scene_bible 必须读同一个数字——它们
    各写一遍字面量时，生成侧提示词又写着另一个数字，模型是照着提示词盲打的。"""
    return SCENE_CANONICAL_MIN_CHARS <= len(canonical) <= SCENE_CANONICAL_MAX_CHARS

_SCENE_CANONICAL_LENGTH_MESSAGE = (
    f"每个场景锚点必须为 {SCENE_CANONICAL_MIN_CHARS}~{SCENE_CANONICAL_MAX_CHARS} 字"
)

def _project_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}

def _supports_bible_style_name(conn) -> bool:
    return "bible_style_name" in _project_columns(conn)

def _normalize_visual_style_name(style_name: str | None) -> str:
    name = (style_name or DEFAULT_VISUAL_STYLE_NAME).strip()
    if visual_style_prompt(name) is None:
        raise HTTPException(422, "请选择有效的统一画面风格")
    return name

def _visual_style_prompt_or_default(style_name: str | None) -> str:
    name = _normalize_visual_style_name(style_name)
    return visual_style_prompt(name) or default_visual_style_prompt()

def _parse_json_value(value, default=None):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default

def _normalize_character_selection(value) -> list[str] | None:
    if value in (None, ""):
        return None
    raw_items = value
    if isinstance(value, str):
        parsed = _parse_json_value(value)
        raw_items = parsed if isinstance(parsed, list) else value.split(",")
    if not isinstance(raw_items, list):
        raise HTTPException(422, "characters 必须是角色名数组")
    names: list[str] = []
    for item in raw_items:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names or None

def _refs_target_payload(only_character: str | None, only_characters: list[str] | None) -> str | None:
    if only_characters:
        return json.dumps(only_characters, ensure_ascii=False)
    return only_character

def _decode_refs_target(value: str | None) -> tuple[str | None, list[str] | None]:
    parsed = _parse_json_value(value)
    if isinstance(parsed, list):
        names = _normalize_character_selection(parsed)
        if names:
            return (names[0] if len(names) == 1 else None), names
    # 历史单角色 refs_target 为纯字符串，不升格为 only_characters 列表
    target = str(value or "").strip() or None
    return target, None

def _quote_stale(precheck: dict, message: str = "费用预检已过期或范围变化，请重新确认") -> HTTPException:
    return HTTPException(
        409,
        detail={
            "code": "QUOTE_STALE",
            "message": message,
            "precheck": precheck,
        },
    )

def _payment_confirm_required(precheck: dict | None = None) -> HTTPException:
    detail = {
        "code": "PAYMENT_CONFIRM_REQUIRED",
        "message": "必须先完成费用预检并显式确认（confirm=true）",
    }
    if precheck is not None:
        detail["precheck"] = precheck
    return HTTPException(409, detail=detail)

def _ensure_character_payment_quotes(conn) -> None:
    """兼容单测中的最小化 schema；正式数据库由 app.db.SCHEMA 创建。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS character_payment_quotes (
            quote_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            action TEXT NOT NULL,
            scope_fingerprint TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            expires_at REAL NOT NULL,
            consumed_task_id TEXT,
            consumed_run_id TEXT,
            created_at REAL NOT NULL,
            consumed_at REAL
        )"""
    )

def _issue_payment_quote(precheck: dict) -> dict:
    """将付费预检签发为有时效、可消费的服务端凭证。"""
    issued = dict(precheck)
    issued["quote_id"] = new_id("quote")
    issued["computed_at"] = now()
    issued["quote_expires_at"] = issued["computed_at"] + 300
    conn = get_conn()
    _ensure_character_payment_quotes(conn)
    conn.execute(
        "INSERT INTO character_payment_quotes(quote_id,project_id,action,scope_fingerprint,"
        "payload_json,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            issued["quote_id"], issued["project_id"], issued["action"],
            issued["scope_fingerprint"], json.dumps(issued, ensure_ascii=False),
            issued["quote_expires_at"], issued["computed_at"],
        ),
    )
    conn.commit()
    return issued

def _validate_payment_quote(project_id: str, quote_id: str | None, current: dict):
    if not quote_id:
        raise _quote_stale(current, "费用预检缺失，请重新确认")
    conn = get_conn()
    _ensure_character_payment_quotes(conn)
    row = conn.execute(
        "SELECT * FROM character_payment_quotes WHERE quote_id=? AND project_id=?",
        (quote_id, project_id),
    ).fetchone()
    if not row:
        raise _quote_stale(current)
    if row["consumed_at"] is not None:
        return row
    if float(row["expires_at"] or 0) < now():
        raise _quote_stale(current, "费用预检已过期，请重新确认")
    if row["action"] != current.get("action") or row["scope_fingerprint"] != current.get("scope_fingerprint"):
        raise _quote_stale(current)
    return row

def _consume_payment_quote(quote_id: str, *, task_id: str, run_id: str | None) -> None:
    conn = get_conn()
    _ensure_character_payment_quotes(conn)
    conn.execute(
        "UPDATE character_payment_quotes SET consumed_task_id=?, consumed_run_id=?, consumed_at=? "
        "WHERE quote_id=? AND consumed_at IS NULL",
        (task_id, run_id, now(), quote_id),
    )
    conn.commit()
