"""Capability Registry：项目领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import project as h_project
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [
        _cmd(
            "project.import_novel",
            title="导入小说",
            description="消费 attachment_token 创建项目并导入 TXT 或 EPUB，随后自动分集并生成人物谱与多视角素材库",
            input_model=I.ProjectImportNovelInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="creates_project",
            handler=h_project.import_novel,
            rest_routes=("POST /api/projects", "POST /api/projects/import"),
            tags=("project",),
        ),
        _cmd(
            # 软删除：项目移入回收站，数据与产物原样保留，24 小时后自动彻底
            # 清理（或用户随时手动恢复/彻底清理）——不再是不可逆操作，风险与
            # 确认策略随之降级；真正不可逆的是下面的 project.purge /
            # project.purge_all。
            "project.delete",
            title="删除项目（移入回收站）",
            description="取消后台任务并把项目移入回收站；数据与产物保留，24 小时后自动彻底清理，期间可随时恢复",
            input_model=I.ProjectDeleteInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="soft_deletes_project",
            handler=h_project.delete_project,
            rest_routes=("DELETE /api/projects/{project_id}",),
            tags=("project",),
        ),
        _cmd(
            "project.restore",
            title="从回收站恢复项目",
            description="清空软删除标记，项目恢复为正常项目，数据与产物本就未被改动",
            input_model=I.ProjectRestoreInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="restores_project",
            handler=h_project.restore_project,
            rest_routes=("POST /api/projects/{project_id}/restore",),
            tags=("project",),
        ),
        _cmd(
            "project.purge",
            title="彻底删除项目",
            description="仅对回收站中的项目生效：物理删除数据库行与全部磁盘产物，不可恢复",
            input_model=I.ProjectPurgeInput,
            risk=RiskLevel.R3_DESTRUCTIVE,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="deletes_project",
            handler=h_project.purge_project,
            rest_routes=("DELETE /api/projects/{project_id}/purge",),
            tags=("project",),
        ),
        _cmd(
            "project.purge_all",
            title="清空回收站",
            description="彻底删除回收站中的全部项目：物理删除数据库行与全部磁盘产物，不可恢复",
            input_model=I.ProjectPurgeAllInput,
            risk=RiskLevel.R3_DESTRUCTIVE,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="deletes_project",
            handler=h_project.purge_all_deleted_projects,
            rest_routes=("DELETE /api/projects/deleted",),
            tags=("project",),
        ),
    ]
