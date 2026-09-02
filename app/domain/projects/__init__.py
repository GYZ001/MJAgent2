"""项目（Project）生命周期与详情投影的路由与业务逻辑。

``app/domain/projects.py``（1,999 行）拆成本包，按关注点分 10 个子模块：
回收站保留期常量（constants，被 listing/lifecycle 与包外的
``app.domain.account_deletion`` 共用）、SQLite ``IN`` 子句分块原语
（sql_helpers，纯 SQL 拼接，与具体删哪张表无关）、Harness 证据批量删除
（evidence，按 scope 递归收集 run/step/artifact/provider_call，供单集删除与
项目彻底清理共用同一套表清单）、小说导入与项目创建（create，含幂等回执与
三个 REST 路由）、项目/回收站列表 + 分段文本模型设置 + 单章节正文读取
（listing）、定妆照/场景图分段挂载（bible_attachments，供详情投影使用）、
项目详情投影（detail，含任务计时与分集切换器窗口化查询，单函数
``project_detail`` 201 行不拆——它是一个视图路由的唯一权威组装顺序，拆开
会打散 view 参数分支与提前返回之间的耦合）、分集删除后的集号压缩（
episode_renumber，含 SQLite 事务 + 磁盘目录搬迁的原子性回滚）、单集彻底
删除（episode_delete）、项目回收站生命周期——软删/恢复/彻底清理/清空/到期
自动清理（lifecycle）。移动未重写，逻辑/签名/格式不变。

调用图是一个 DAG，拓扑序即上面列出的顺序：``evidence``/``listing``/
``lifecycle`` 依赖 ``constants``；``evidence`` 依赖 ``sql_helpers``；
``detail`` 依赖 ``bible_attachments`` 和 ``evidence``（``_present_refs_error``）；
``episode_delete`` 依赖 ``evidence`` 和 ``episode_renumber``；``lifecycle``
依赖 ``evidence``（``_delete_project_evidence``）和 ``listing``
（``_listing_owner_scope``）。不存在跨子模块的延迟导入，也不存在循环。

``_delete_project_core`` / ``_purge_project_core`` / ``_restore_project_core`` /
``_purge_all_deleted_projects_core``（均在 ``lifecycle`` 子模块）被
``app.domain.account_deletion``（账号级联软删除/恢复）复用，签名与行为不变。

2026-08-30 新增第 11 个子模块 ``downgrade``（会员到期后的降级：裁剪超额项目
到新档位上限 + 周期性到期扫描），依赖 ``lifecycle``（复用 ``_delete_project_
core`` 做实际删除，软删除进回收站，不是硬删）——见该子模块文档字符串。

本文件是稳定入口：所有既有 ``from app.domain.projects import X`` /
``app.domain.projects.X`` 调用点必须原样可用——每个符号（含每个子模块自己
``import`` 进来的名字，不只是它原生定义的）用 ``name as name`` 显式再导出
（PEP 484 显式重导出写法，与 ``app/validators/__init__.py``、``app/domain/
bible_ops/__init__.py``、``app/domain/storyboard_ops/__init__.py``、
``app/domain/video_ops/__init__.py`` 同一先例），而不是 ``from .x import *``
（``app/FILE_CONVENTIONS.toml`` 的 ``star_import`` 闸门禁止后者）。这 87 个
名字是原 ``projects.py`` 拆分前 ``__all__ = [name for name in globals() if
not name.startswith("__")]`` 的完整快照（含它自己 import 进来但从未重新赋值
的标准库/第三方名字，如 ``json``/``Path``/``HTTPException``），不是手打的。
新增项目相关逻辑请加进对应关注点的子模块，不要加回本文件。

包拆分后 ``monkeypatch.setattr(projects, name, stub)``（``projects`` 指
``app.domain.projects`` 这个包对象）只重绑定这里的再导出属性，不影响任何
子模块自己保有的独立绑定——``app.domain.__init__.py`` 的 ``patch_api_
everywhere`` 已经会递归进本包的每个子模块（见其 docstring「recurses into any
chunk that has a __path__」），但那个通用助手无法覆盖测试里常见的重命名别名
（``projects_mod``/``projects_api`` 等，``tests/test_api_monkeypatch_guard.py``
的别名识别只认与 chunk 同名的绑定）。因此额外提供 ``tests/conftest.py`` 的
``patch_projects_everywhere(monkeypatch, name, value)``——它直接遍历本包的
每个子模块并逐一打桩，覆盖任意本地别名；``tests/test_projects_monkeypatch_
guard.py`` 是它的 AST 守卫。
"""
from __future__ import annotations

from app.domain.projects.constants import (
    ACCOUNT_DELETE_RETENTION_S as ACCOUNT_DELETE_RETENTION_S,
    PROJECT_RECYCLE_BIN_RETENTION_S as PROJECT_RECYCLE_BIN_RETENTION_S,
    annotations as annotations,
)

from app.domain.projects.sql_helpers import (
    Iterable as Iterable,
    _SQLITE_IN_CHUNK_SIZE as _SQLITE_IN_CHUNK_SIZE,
    _delete_scope_rows as _delete_scope_rows,
    _execute_by_in as _execute_by_in,
    _ids_by_in as _ids_by_in,
    _in_chunks as _in_chunks,
    _marks as _marks,
    _scope_ids as _scope_ids,
)

from app.domain.projects.evidence import (
    _delete_episode_evidence as _delete_episode_evidence,
    _delete_project_evidence as _delete_project_evidence,
    _delete_scoped_evidence as _delete_scoped_evidence,
    _present_refs_error as _present_refs_error,
)

from app.domain.projects.create import (
    Body as Body,
    File as File,
    Form as Form,
    HTTPException as HTTPException,
    Path as Path,
    SUPPORTED_NOVEL_LABEL as SUPPORTED_NOVEL_LABEL,
    UploadFile as UploadFile,
    _LEGACY_NO_PRINCIPAL_OWNER as _LEGACY_NO_PRINCIPAL_OWNER,
    _create_project_core as _create_project_core,
    _creation_owner_user_id as _creation_owner_user_id,
    _novel_import_receipt as _novel_import_receipt,
    _novel_import_token_hash as _novel_import_token_hash,
    _read_novel_upload as _read_novel_upload,
    create_project as create_project,
    create_project_from_attachment as create_project_from_attachment,
    get_conn as get_conn,
    ingest_novel as ingest_novel,
    json as json,
    new_id as new_id,
    novel_file_suffix as novel_file_suffix,
    now as now,
    prepare_novel_bytes as prepare_novel_bytes,
    quota as quota,
    router as router,
    upload_novel_attachment as upload_novel_attachment,
    validate_novel_filename as validate_novel_filename,
)

from app.domain.projects.listing import (
    _TEXT_MODEL_STAGE_COLUMNS as _TEXT_MODEL_STAGE_COLUMNS,
    _as_body_dict as _as_body_dict,
    _listing_owner_scope as _listing_owner_scope,
    _project_or_404 as _project_or_404,
    _recover_orphan_bible_dicts as _recover_orphan_bible_dicts,
    list_deleted_projects as list_deleted_projects,
    list_projects as list_projects,
    read_chapter as read_chapter,
    rows_to_dicts as rows_to_dicts,
    set_project_text_models as set_project_text_models,
)

from app.domain.projects.bible_attachments import (
    _attach_character_portraits as _attach_character_portraits,
    _attach_scene_refs as _attach_scene_refs,
    _media_url as _media_url,
)

from app.domain.projects.detail import (
    EpisodeScreenplay as EpisodeScreenplay,
    _PICKER_COLUMNS as _PICKER_COLUMNS,
    _PICKER_GENERATION_COLUMNS as _PICKER_GENERATION_COLUMNS,
    _PICKER_MAX_LIMIT as _PICKER_MAX_LIMIT,
    _PRODUCTION_FILTER_SQL as _PRODUCTION_FILTER_SQL,
    _attach_picker_episodes as _attach_picker_episodes,
    _project_task_timings as _project_task_timings,
    build_media_url as build_media_url,
    chapter_preview as chapter_preview,
    evidence_repository as evidence_repository,
    project_detail as project_detail,
)

from app.domain.projects.episode_renumber import (
    _compact_asset_episode_ranges as _compact_asset_episode_ranges,
    _compact_project_episode_numbers as _compact_project_episode_numbers,
    _json_with_episode_number as _json_with_episode_number,
    _replace_episode_path_prefixes as _replace_episode_path_prefixes,
    config as config,
)

from app.domain.projects.episode_delete import (
    _assert_no_other_episode_work as _assert_no_other_episode_work,
    _delete_episode_core as _delete_episode_core,
    _episode_or_404 as _episode_or_404,
    delete_episode as delete_episode,
    task_registry as task_registry,
)

from app.domain.projects.lifecycle import (
    _assert_principal_owns as _assert_principal_owns,
    _deleted_project_or_404 as _deleted_project_or_404,
    _delete_project_core as _delete_project_core,
    _purge_all_deleted_projects_core as _purge_all_deleted_projects_core,
    _purge_project_core as _purge_project_core,
    _restore_project_core as _restore_project_core,
    delete_project as delete_project,
    purge_all_deleted_projects as purge_all_deleted_projects,
    purge_project as purge_project,
    restore_project as restore_project,
    sweep_expired_deleted_projects as sweep_expired_deleted_projects,
)

from app.domain.projects.downgrade import (
    sweep_expired_memberships as sweep_expired_memberships,
    trim_projects_to_tier_limit as trim_projects_to_tier_limit,
)
