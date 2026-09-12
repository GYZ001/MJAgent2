"""模型中心管理界面的只读聚合 + 用途绑定读写 REST 接口
（``/api/models/registry/*``），系统管理员专属（EP-05 §8）。

路径特意多一段 ``registry``：``app.system_api`` 已经在 ``/models`` 前缀下
注册了 ``PUT|DELETE /models/{model_id}``、``POST /models/{model_id}/test``、
``PUT /models/{model_id}/credentials`` 等 1~2 段路由。本文件是另一个独立
``APIRouter`` 实例，Starlette 按各路由器注册顺序逐条尝试匹配——如果本文件
路径的段数与上面任一模式相同（比如裸 ``/models/bindings`` 会被
``/models/{model_id}`` 当成 ``model_id="bindings"`` 先吃掉，取决于
``app.main`` 里两个路由器谁先 ``include_router``），就会被"抢先匹配"。
多插一段 ``registry`` 让全部新路径的段数与现有模式都不同，结果不依赖注册
顺序，不需要每次改 ``app.main`` 的顺序都重新验证一遍。

L5：只被 ``app.main`` 引用，挂
``APIRouter(dependencies=[Depends(require_system_admin)])``——同
``app.audit.api`` 的角色（前端隐藏只是体验，这里才是真正边界，CLAUDE.md）。

写操作只有"用途绑定"这一项是本文件新开的口子——``model_bindings`` 此前没有
任何 HTTP 写入口，这不是"新开第二条写路径"，是"开第一条"。模型的启停/限速
走既有 ``PUT /api/models/{model_id}``（见
``app.model_registry.extra_patch_fields``）；凭据轮换走既有
``PUT /api/models/{model_id}/credentials``（探活成功才原子替换，失败保留旧
密文——见该端点，本文件不重复）。

``PUT /bindings`` 先走 ``ui_route("system.model_binding_upsert", body)``——与
``app.system_api`` 里 ``add_model_route``/``update_model_route`` 同一套
"REST 入口先过 Command Bus，Handler 内重入时 ``ui_route`` 返回 None 再执行
领域逻辑"模式（``app.capabilities.commands.system`` 已经把它登记成
``admin_only=True``/``mcp_exposed=False`` 的命令，理由：这是模型路由配置，
和 ``system.model_update`` 同一档，不是纯运维端点，见
``scripts/check_capability_coverage.py`` 的分类要求）——不是绕过覆盖闸门另开
一条不经 Command Bus 的写路径。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import require_system_admin
from app.models_registry import admin_queries, bindings

router = APIRouter(
    prefix="/api/models/registry", tags=["models_registry_admin"],
    dependencies=[Depends(require_system_admin)],
)


@router.get("/health")
def get_health(window_hours: float = 24.0) -> dict[str, Any]:
    if not (0 < window_hours <= 24 * 30):
        raise HTTPException(422, "window_hours 必须在 (0, 720] 之间")
    return {"items": admin_queries.list_model_health(window_hours=window_hours)}


@router.get("/credentials")
def get_credentials() -> dict[str, Any]:
    """只回掩码 + 指纹 + 轮换时间；``app.models_registry.store.
    list_credentials_public`` 已经保证永不带明文，这里不重复校验。"""
    return {"items": admin_queries.list_credentials()}


@router.get("/purposes")
def get_purposes() -> dict[str, Any]:
    return {"items": admin_queries.purpose_status()}


@router.put("/bindings")
async def put_binding(body: dict[str, Any]) -> dict[str, Any]:
    """新增/改写一条 ``(purpose, priority)`` 绑定——priority 是槽位标识，
    "调整顺序"是把两个 model_id 互换所在槽位，前端对同一次重排发两次调用。"""
    from app.capabilities.dispatch import ui_route

    routed = await ui_route("system.model_binding_upsert", body)
    if routed is not None:
        return routed
    return _upsert_binding(body)


def _upsert_binding(body: dict[str, Any]) -> dict[str, Any]:
    purpose = str(body.get("purpose") or "").strip()
    model_id = str(body.get("model_id") or "").strip()
    if not purpose or not model_id:
        raise HTTPException(422, "purpose 与 model_id 不能为空")
    try:
        priority = int(body.get("priority"))
    except (TypeError, ValueError):
        raise HTTPException(422, "priority 必须是整数") from None
    if priority < 0:
        raise HTTPException(422, "priority 不能为负")
    binding_id = bindings.upsert_binding(
        purpose=purpose, model_id=model_id, priority=priority,
        enabled=bool(body.get("enabled", True)),
        params=body.get("params") if isinstance(body.get("params"), dict) else None,
        created_by="models_center_ui",
    )
    return {"ok": True, "id": binding_id}
