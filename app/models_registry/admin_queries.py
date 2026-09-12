"""模型中心管理界面的只读聚合查询（EP-05 §8）：合并
``store``/``health``/``bindings``/``purposes``/``app.model_registry`` 五处
数据源，产出前端可以直接渲染的形状。纯读，不做任何写入——写入分别走
``app.system_api.update_model``（模型启停/限速，见
``app.model_registry.extra_patch_fields``）、既有
``PUT /api/models/{model_id}/credentials``（凭据轮换）与本包
``api.py`` 新增的 ``PUT /models/registry/bindings``（用途绑定，此前无任何
HTTP 写入口）。

L3：需要在模块级 import ``app.model_registry``（L3，按 ``model_id`` 反查
目录条目的 label/provider/enabled）与 ``app.models_registry.routing``
（L3，按 purpose 复算"当前真的选中了哪一条"），与两者同层；因此不能被
``app.models_registry.health``/``bindings``（L2）在模块级 import 回来，
只能反过来——本文件在这些模块之上做聚合。
"""
from __future__ import annotations

from typing import Any

from app.models_registry import bindings, health, purposes, store

_CIRCUIT_REASON_LABELS = {
    "timeout": "连续超时",
    "rate_limited": "触发限流",
    "server_error": "服务端错误（含凭据失效等 401/403/5xx）",
    "content_rejected": "内容被供应商拒答",
}


def _catalog_by_id() -> dict[str, dict[str, Any]]:
    from app import model_registry  # 同层（L3）延迟 import，避免模块级双向耦合读起来更绕

    return {str(item.get("id") or ""): item for item in model_registry.catalog_items()}


def list_model_health(*, window_hours: float = 24.0) -> list[dict[str, Any]]:
    """模型库全部条目（不止被调用过的）各自的健康状态 + 近 N 小时统计。"""
    health.refresh()
    catalog = _catalog_by_id()
    by_ref = health.calls_by_ref_in_window(window_hours=window_hours)
    items: list[dict[str, Any]] = []
    for model_id, item in catalog.items():
        stats = by_ref.get(str(item.get("model") or ""), {})
        snapshot = health.get_window_snapshot(model_id)
        items.append({
            "model_id": model_id,
            "label": str(item.get("label") or item.get("model") or model_id),
            "provider": str(item.get("provider") or ""),
            "kinds": list(item.get("kinds") or []),
            "enabled": item.get("enabled") is not False,
            "state": snapshot["state"],
            "calls_window": stats.get("calls", 0),
            "failures_window": stats.get("failures", 0),
            "failure_rate_window": stats.get("failure_rate", 0.0),
            "p50_latency_ms_window": stats.get("p50"),
            "p95_latency_ms_window": stats.get("p95"),
            "window_hours": window_hours,
            "opened_at": snapshot["opened_at"],
            "last_error_code": snapshot["last_error_code"],
            "last_error_at": snapshot["last_error_at"],
        })
    return items


def list_credentials() -> list[dict[str, Any]]:
    return store.list_credentials_public()


def _primary_unavailable_reason(
    primary: dict[str, Any] | None, catalog: dict[str, dict[str, Any]],
) -> tuple[str, str, float | None]:
    """主用（priority=0）绑定为什么不可用；可用时返回 ``("", "", None)``。"""
    if primary is None:
        return "", "", None
    if not primary.get("enabled"):
        return "binding_disabled", "主用绑定已被停用", primary.get("updated_at")
    item = catalog.get(str(primary.get("model_id") or ""))
    if item is None:
        return "model_missing", "主用模型已从模型库删除", primary.get("updated_at")
    if item.get("enabled") is False:
        return "model_disabled", "主用模型已停用", primary.get("updated_at")
    snapshot = health.get_window_snapshot(str(primary.get("model_id") or ""))
    if snapshot["state"] == "circuit_open":
        code = snapshot["last_error_code"] or ""
        label = _CIRCUIT_REASON_LABELS.get(code, code or "未知原因")
        return "circuit_open", f"主用模型已熔断：{label}", snapshot["opened_at"]
    return "", "", None


def _binding_view(row: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    label = str((catalog.get(str(row["model_id"])) or {}).get("label") or row["model_id"])
    return {**row, "label": label}


def purpose_status(*, org_id: str = "") -> list[dict[str, Any]]:
    """每个已知 purpose 的优先级链 + 是否正在用备用模型顶着（EP-05 §8 第 4 条）。"""
    from app.models_registry.routing import resolve  # 同层（L3），见模块文档

    catalog = _catalog_by_id()
    missing_zero = set(purposes.missing_priority_zero_purposes())
    result: list[dict[str, Any]] = []
    for purpose in sorted(purposes.known_purposes()):
        chain = bindings.list_bindings(purpose, org_id=org_id, enabled_only=False)
        primary = next((row for row in chain if int(row["priority"]) == 0), None)
        reason_code, reason_label, since = _primary_unavailable_reason(primary, catalog)
        active = resolve(purpose, org_id=org_id)
        fallback_active = bool(
            primary is not None and reason_code
            and active is not None and active.model_id != primary.get("model_id")
        )
        result.append({
            "purpose": purpose,
            "missing_priority_zero": purpose in missing_zero,
            "bindings": [_binding_view(row, catalog) for row in chain],
            "fallback_active": fallback_active,
            "active_priority": active.priority if active else None,
            "active_model_id": active.model_id if active else None,
            "active_label": (
                str((catalog.get(active.model_id) or {}).get("label") or active.model_id)
                if active else None
            ),
            "reason_code": reason_code if fallback_active else "",
            "reason_label": reason_label if fallback_active else "",
            "since": since if fallback_active else None,
        })
    return result
