"""``models`` / ``model_credentials`` 两表的读写层。L2（依赖 app.db，同层）。

设计偏离 PRD 一处，需要记录：EP-05 §3 给的 ``model_credentials`` schema 只有
``model_id/key_ciphertext/key_nonce/key_fingerprint/rotated_at/rotated_by/
created_at``，没有 ``base_url``。但现状（``settings.model_credentials``）把
base_url 与 api_key 存成同一条 ``{model_id: {base_url, api_key}}`` 记录，且
CLAUDE.md 明确「model 与 provider/凭据必须一起传递，分开传会让请求打错端点」。
把 base_url 拆到别处解密时再拼接，恰好制造了这条硬约束要防的那种分裂，所以
这里在 PRD schema 基础上给 ``model_credentials`` 加了一列 ``base_url``（明文，
不是秘密），让"这把 Key 配哪个地址"永远是同一行、原子读写。

``models`` 表这一阶段只是「落表」——迁移把 ``settings.custom_models`` 的目录
条目镜像进来，供下一阶段（``model_bindings``/路由）消费；本阶段任何真实选路
仍然读 ``settings.custom_models``（``app/hiagent.py``/``app/video_providers.py``/
``app/system_api.py`` 均未改动这部分），``models`` 表里的数据这一阶段不会被
路由路径读取，不影响"选路行为零变化"这条硬指标。
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, now

from app.models_registry import crypto, schema
from app.models_registry.keyprovider import get_default_provider


def _master_key() -> bytes:
    return get_default_provider().get_key()


def get_credential(model_id: str) -> dict[str, str]:
    """解密返回 ``{"base_url":..., "api_key":...}``；没有记录时返回 ``{}``
    （与旧代码 ``dict.get(model_id, {})`` 的"未配置"语义完全一致，调用方
    ``saved.get("api_key")``/``saved.get("base_url")`` 不需要改判断逻辑）。
    """
    model_id = str(model_id or "").strip()
    if not model_id:
        return {}
    schema.ensure_schema()
    row = get_conn().execute(
        "SELECT base_url, key_ciphertext, key_nonce FROM model_credentials WHERE model_id=?",
        (model_id,),
    ).fetchone()
    if row is None:
        return {}
    api_key = crypto.decrypt_secret(
        _master_key(), model_id, bytes(row["key_nonce"]), bytes(row["key_ciphertext"])
    )
    return {"base_url": row["base_url"] or "", "api_key": api_key}


def has_credential(model_id: str) -> bool:
    model_id = str(model_id or "").strip()
    if not model_id:
        return False
    schema.ensure_schema()
    row = get_conn().execute(
        "SELECT 1 FROM model_credentials WHERE model_id=?", (model_id,)
    ).fetchone()
    return row is not None


def put_credential(model_id: str, *, base_url: str, api_key: str, rotated_by: str) -> dict[str, Any]:
    """加密并原子替换（单条 UPSERT，同一事务内完成，不存在"写了一半"的中间态）。

    调用方负责"轮换原子性"里的探活顺序（先探活成功、再调用本函数）——本函数
    只保证"一旦被调用，落盘这一步本身是原子的"，不负责探活；这样探活失败时
    根本不会走到这里，旧密文天然原封不动。
    """
    model_id = str(model_id or "").strip()
    if not model_id:
        raise ValueError("model_id 不能为空")
    if not api_key:
        raise ValueError("api_key 不能为空")
    schema.ensure_schema()
    nonce, ciphertext = crypto.encrypt_secret(_master_key(), model_id, api_key)
    fp = crypto.fingerprint(api_key)
    ts = now()
    conn = get_conn()
    conn.execute(
        """INSERT INTO model_credentials
               (model_id, base_url, key_ciphertext, key_nonce, key_fingerprint,
                rotated_at, rotated_by, created_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(model_id) DO UPDATE SET
               base_url=excluded.base_url,
               key_ciphertext=excluded.key_ciphertext,
               key_nonce=excluded.key_nonce,
               key_fingerprint=excluded.key_fingerprint,
               rotated_at=excluded.rotated_at,
               rotated_by=excluded.rotated_by""",
        (model_id, base_url or "", ciphertext, nonce, fp, ts, rotated_by, ts),
    )
    conn.commit()
    return {"key_fingerprint": fp, "masked_key": crypto.mask(api_key), "rotated_at": ts}


def delete_credential(model_id: str) -> None:
    schema.ensure_schema()
    conn = get_conn()
    conn.execute("DELETE FROM model_credentials WHERE model_id=?", (str(model_id or "").strip(),))
    conn.commit()


def upsert_model(item: dict[str, Any], *, created_by: str | None = None) -> None:
    """把一条模型库目录条目（``settings.custom_models`` 的 dict 形状）镜像进
    ``models`` 表。纯落表，不做能力探测、不做校验——校验是目录本身
    （``app/system_api.py::_validated_model_definition``）的职责，这里只负责
    把已经校验过的数据存进可查询的表。
    """
    model_id = str(item.get("id") or "").strip()
    if not model_id:
        raise ValueError("模型条目缺少 id，无法落表")
    kinds = item.get("kinds") or []
    capabilities = {
        key: item[key]
        for key in ("context_window_tokens", "max_output_tokens", "token_limits_source")
        if key in item
    }
    schema.ensure_schema()
    ts = now()
    conn = get_conn()
    conn.execute(
        """INSERT INTO models
               (id, org_id, name, protocol, provider_label, model_ref, kinds_json,
                base_url, enabled, capabilities_json, rate_limit_json, notes,
                created_at, updated_at, created_by)
           VALUES(?,NULL,?,?,?,?,?,?,1,?,NULL,NULL,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, protocol=excluded.protocol,
               provider_label=excluded.provider_label, model_ref=excluded.model_ref,
               kinds_json=excluded.kinds_json, base_url=excluded.base_url,
               capabilities_json=excluded.capabilities_json,
               updated_at=excluded.updated_at""",
        (
            model_id,
            str(item.get("label") or ""),
            str(item.get("protocol") or ""),
            str(item.get("provider_label") or item.get("provider") or ""),
            str(item.get("model") or ""),
            json.dumps(list(kinds), ensure_ascii=False),
            str(item.get("base_url") or ""),
            json.dumps(capabilities, ensure_ascii=False),
            ts,
            ts,
            created_by,
        ),
    )
    conn.commit()


def get_model(model_id: str) -> dict[str, Any] | None:
    schema.ensure_schema()
    row = get_conn().execute(
        "SELECT * FROM models WHERE id=?", (str(model_id or "").strip(),)
    ).fetchone()
    return _model_row_to_dict(row) if row is not None else None


def list_models() -> list[dict[str, Any]]:
    schema.ensure_schema()
    rows = get_conn().execute("SELECT * FROM models ORDER BY created_at").fetchall()
    return [_model_row_to_dict(row) for row in rows]


def _model_row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["kinds"] = json.loads(data.pop("kinds_json") or "[]")
    data["capabilities"] = json.loads(data.pop("capabilities_json") or "{}")
    return data
