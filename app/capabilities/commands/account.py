"""Capability Registry：账号删除 / 配额领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import system as h_system
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel
from app.domain import account_deletion as h_account


def commands() -> list[CommandSpec]:
    return [
        # —— 账号删除（治理登记：REST 入口在 app/auth/api.py、app/auth/admin_api.py
        # 直接调用同名领域函数，不经本总线——两条 L2 auth 路由文件到 L5 领域/总线
        # 模块之间的跨层调用量已经在 app/LAYERS.toml 的 allowed_exceptions 里为
        # account_deletion 单独登记过，若再叠加 dispatch() 会多开一条 L2->L5 边，
        # 这里注册纯粹是为了让这三个命令出现在风险清单里，可被 preflight/审计
        # 工具查询）——
        _cmd(
            "account.self_delete",
            title="删除本账号（不可撤销）",
            description="确认后立即级联删除本账号名下全部项目（数据库行与磁盘产物）并注销账号本身，不可恢复",
            input_model=I.AccountSelfDeleteInput,
            risk=RiskLevel.R3_DESTRUCTIVE,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="deletes_account",
            handler=h_account.self_delete_handler,
            rest_routes=("DELETE /api/auth/me",),
            mcp_exposed=False,
            admin_only=False,
            tags=("account", "destructive"),
        ),
        _cmd(
            "account.admin_soft_delete",
            title="管理员删除用户账号（移入回收站）",
            description="账号与其当前活跃项目一并移入 30 天保留期回收站；期间可恢复，到期自动彻底清理",
            input_model=I.AccountAdminDeleteInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:admin"},
            side_effect="soft_deletes_account",
            handler=h_account.admin_soft_delete_handler,
            rest_routes=("DELETE /api/system/users/{user_id}",),
            mcp_exposed=False,
            admin_only=True,
            tags=("account",),
        ),
        _cmd(
            "account.admin_restore",
            title="从回收站恢复用户账号",
            description="30 天保留期内清空账号软删除标记，随之恢复本次账号级联软删除带出的项目",
            input_model=I.AccountAdminRestoreInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:admin"},
            side_effect="restores_account",
            handler=h_account.admin_restore_handler,
            rest_routes=("POST /api/system/users/{user_id}/restore",),
            mcp_exposed=False,
            admin_only=True,
            tags=("account",),
        ),
        _cmd(
            "quota.grant_video_addon",
            title="发放视频加量包",
            description="管理员手工为账号发放视频加量包（每包 10 分钟，¥199）；加量包不随 30 天配额周期重置，发出即计费",
            input_model=I.QuotaGrantVideoAddonInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:admin"},
            side_effect="grants_paid_quota",
            handler=h_system.quota_grant_video_addon,
            rest_routes=("POST /api/system/users/{user_id}/video-addons",),
            mcp_exposed=False,
            admin_only=True,
            tags=("quota", "billing"),
        ),
    ]
