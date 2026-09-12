"""Capability Registry：系统（默认不对外 MCP）领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。

``_COMMAND_SPECS`` 是模块级常量而不是塞在 ``commands()`` 函数体里的一串
``_cmd(...)`` 调用：命令声明是数据，不是控制流，条目变多只是「表变长」，不该
被 ``function_lines`` 闸门当成「函数变复杂」——与
``app/capabilities/exemptions.py::EXEMPT_ROUTE_PERMISSIONS`` 同一处理，见该
文件模块文档「整份清单是数据」一段（2026-09-12，EP-05 §8 新增
``system.model_binding_upsert`` 时顺手把这一条也做成数据，之前逐条 ``_cmd``
堆在 ``commands()`` 里已经把它的 function_lines 顶到基线 130 的零余量）。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import system as h_system
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel

_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    _cmd(
        "system.update_settings",
        title="更新运行设置",
        description="白名单字段更新；禁止 Agent 自动调高预算/并发",
        input_model=I.SystemUpdateSettingsInput,
        risk=RiskLevel.R3_DESTRUCTIVE,
        confirmation=ConfirmationPolicy.NEVER,
        idempotency=IdempotencyPolicy.REQUIRED,
        scopes={"manju:admin"},
        side_effect="updates_settings",
        handler=h_system.update_settings,
        rest_routes=("PUT /api/settings",),
        mcp_exposed=False,
        admin_only=True,
        tags=("system",),
    ),
    _cmd(
        "system.model_create",
        title="添加模型",
        description="加入自定义模型元数据（不含密钥明文）",
        input_model=I.SystemModelCreateInput,
        risk=RiskLevel.R3_DESTRUCTIVE,
        confirmation=ConfirmationPolicy.NEVER,
        idempotency=IdempotencyPolicy.REQUIRED,
        scopes={"manju:admin"},
        side_effect="creates_model_metadata",
        handler=h_system.model_create,
        rest_routes=("POST /api/models",),
        mcp_exposed=False,
        admin_only=True,
        tags=("system",),
    ),
    _cmd(
        "system.model_update",
        title="更新模型",
        description="更新自定义模型元数据",
        input_model=I.SystemModelUpdateInput,
        risk=RiskLevel.R3_DESTRUCTIVE,
        confirmation=ConfirmationPolicy.NEVER,
        idempotency=IdempotencyPolicy.REQUIRED,
        scopes={"manju:admin"},
        side_effect="updates_model_metadata",
        handler=h_system.model_update,
        rest_routes=("PUT /api/models/{model_id}",),
        mcp_exposed=False,
        admin_only=True,
        tags=("system",),
    ),
    _cmd(
        "system.model_delete",
        title="删除模型",
        description="删除自定义模型",
        input_model=I.SystemModelDeleteInput,
        risk=RiskLevel.R3_DESTRUCTIVE,
        confirmation=ConfirmationPolicy.ALWAYS,
        idempotency=IdempotencyPolicy.REQUIRED,
        scopes={"manju:admin"},
        side_effect="deletes_model_metadata",
        handler=h_system.model_delete,
        rest_routes=("DELETE /api/models/{model_id}",),
        mcp_exposed=False,
        admin_only=True,
        tags=("system",),
    ),
    _cmd(
        "system.model_test",
        title="测试模型连接",
        description="测试模型连通性；结果必须脱敏",
        input_model=I.SystemModelTestInput,
        risk=RiskLevel.R1_REVERSIBLE,
        confirmation=ConfirmationPolicy.OPTIONAL,
        idempotency=IdempotencyPolicy.RECOMMENDED,
        scopes={"manju:admin"},
        side_effect="external_connectivity_probe",
        handler=h_system.model_test,
        rest_routes=("POST /api/models/test", "POST /api/models/{model_id}/test"),
        mcp_exposed=True,
        admin_only=False,
        tags=("system",),
    ),
    _cmd(
        "system.model_binding_upsert",
        title="调整用途绑定优先级",
        description="新增/改写某个 purpose 在某个优先级槽位上的候选模型（模型中心：调顺序、启停候选）",
        input_model=I.SystemModelBindingUpsertInput,
        risk=RiskLevel.R3_DESTRUCTIVE,
        confirmation=ConfirmationPolicy.NEVER,
        idempotency=IdempotencyPolicy.REQUIRED,
        scopes={"manju:admin"},
        side_effect="updates_model_routing_binding",
        handler=h_system.model_binding_upsert,
        rest_routes=("PUT /api/models/registry/bindings",),
        mcp_exposed=False,
        admin_only=True,
        tags=("system",),
    ),
    _cmd(
        "system.set_engine",
        title="切换 Harness Engine",
        description="按项目启用/关闭 Workflow Engine",
        input_model=I.SystemEngineInput,
        risk=RiskLevel.R2_MATERIAL,
        confirmation=ConfirmationPolicy.NEVER,
        idempotency=IdempotencyPolicy.REQUIRED,
        scopes={"manju:admin"},
        side_effect="toggles_engine",
        handler=h_system.set_engine,
        rest_routes=("PUT /api/projects/{project_id}/engine",),
        mcp_exposed=False,
        admin_only=True,
        tags=("system",),
    ),
    _cmd(
        "system.mkdir",
        title="在已授权目录下建子目录",
        description="仅允许在用户已授权的 directory_grant 下创建子目录",
        input_model=I.SystemMkdirInput,
        risk=RiskLevel.R2_MATERIAL,
        confirmation=ConfirmationPolicy.NEVER,
        idempotency=IdempotencyPolicy.REQUIRED,
        scopes={"manju:admin"},
        side_effect="creates_directory_under_grant",
        handler=h_system.mkdir,
        rest_routes=("POST /api/system/mkdir",),
        mcp_exposed=False,
        admin_only=True,
        tags=("system", "filesystem"),
    ),
    _cmd(
        "system.run_benchmark",
        title="运行 Benchmark",
        description="运行并记录双轨基准",
        input_model=I.BenchmarkRunInput,
        risk=RiskLevel.R2_MATERIAL,
        confirmation=ConfirmationPolicy.NEVER,
        idempotency=IdempotencyPolicy.REQUIRED,
        scopes={"manju:admin"},
        side_effect="creates_benchmark_run",
        handler=h_system.run_benchmark,
        rest_routes=("POST /api/benchmarks",),
        mcp_exposed=False,
        admin_only=True,
        tags=("system",),
    ),
)


def commands() -> list[CommandSpec]:
    return list(_COMMAND_SPECS)
