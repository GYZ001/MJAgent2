"""system.* Command Handlers（运行设置、模型库、Harness Engine、受限目录）。

均为 ``admin_only``（不对外 MCP），仍需经过 Command Bus 的确认/幂等/审计。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def update_settings(args: I.SystemUpdateSettingsInput) -> CommandResult:
    from app import system_api

    outcome = await call_guarded(system_api.put_settings, args.patch)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("运行设置已更新", data=outcome)


async def model_create(args: I.SystemModelCreateInput) -> CommandResult:
    from app import system_api

    outcome = await call_guarded(system_api.add_model, args.model)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"模型 {outcome.get('id')} 已加入模型库", data=outcome)


async def model_update(args: I.SystemModelUpdateInput) -> CommandResult:
    from app import system_api

    outcome = await call_guarded(system_api.update_model, args.model_id, args.patch)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"模型 {args.model_id} 已更新", data=outcome)


async def model_delete(args: I.SystemModelDeleteInput) -> CommandResult:
    from app import system_api

    outcome = await call_guarded(system_api.delete_model, args.model_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"模型 {args.model_id} 已删除", data=outcome)


async def model_test(args: I.SystemModelTestInput) -> CommandResult:
    from app import system_api

    if args.model_id:
        outcome = await call_guarded(system_api.test_saved_model, args.model_id, body=args.draft or {})
    elif args.draft:
        outcome = await call_guarded(system_api.test_model_connection, args.draft)
    else:
        from app.capabilities.handlers.common import failed

        return failed("必须提供 model_id 或 draft", error_code="invalid_input")
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("模型连接测试完成", data=outcome)


async def set_engine(args: I.SystemEngineInput) -> CommandResult:
    from app.orchestration import api as orch_api

    outcome = await call_guarded(orch_api.set_project_engine, args.project_id, {"enabled": args.enabled})
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        f"项目 {args.project_id} 的 Workflow Engine 已{'启用' if args.enabled else '关闭'}", data=outcome
    )


async def mkdir(args: I.SystemMkdirInput) -> CommandResult:
    from app import system_api

    outcome = await call_guarded(system_api.make_dir, {"path": args.parent_grant, "name": args.name})
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"目录已创建：{outcome.get('path')}", data=outcome)


async def run_benchmark(args: I.BenchmarkRunInput) -> CommandResult:
    from app.orchestration import api as orch_api

    outcome = await call_guarded(orch_api.run_benchmark, args.payload)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("Benchmark 已记录", data=outcome)


async def quota_grant_video_addon(args: I.QuotaGrantVideoAddonInput) -> CommandResult:
    """发放视频加量包。复用路由函数本体（同层 app.auth.admin_api，L5），而不是
    绕过它直接调 app.quota_addon——路由里那几步（用户存在性、packages 取值域、
    monitor_audit 落账）是这条命令正确性的一部分，两条入口必须共用同一份。"""
    from app.auth import admin_api
    from app.auth.principal import get_current_principal

    body: dict = {"packages": args.packages}
    if args.idempotency_key:
        body["idempotency_key"] = args.idempotency_key
    # actor 必须显式传：路由签名里它是 ``Depends(require_system_admin)``，只有走
    # FastAPI 才会被解析：直接调用会把 Depends 对象原样当成 actor，落审计那行
    # ``actor.user_id`` 当场 AttributeError（CLAUDE.md「所有权必须显式」）。
    outcome = await call_guarded(
        admin_api.grant_video_addon, args.user_id, body, actor=get_current_principal(),
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        f"已为账号 {args.user_id} 发放 {args.packages} 个视频加量包", data=outcome,
    )
