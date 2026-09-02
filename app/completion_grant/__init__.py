"""视频补齐授权（VideoCompletionGrant）。

用户选择「补齐到全片可用」时签发；分镜始终由人工确认，不存在自动确认授权。
token 只存哈希，不存明文。

2026-08-31（#75）从单文件 app/completion_grant.py（2467 行 = 500 行上限的 5 倍，
且基线已收到零余量）拆成本包。子模块按依赖自底向上：

    models          常量、数据模型、错误类型、行转对象（不依赖包内其它模块）
    ledger          供应商预算认领台账建表/迁移 + 历史负债
    reconcile       清空/删除前的供应商任务对账
    budget_authority 单集预算上限与授权
    budget_claims   预算认领与负债关闭
    qualification   发布资格快照与指纹
    grants_issue    grant 建表与签发
    grants_api      grant 读取、校验、改绑、加额、撤销

本文件是全包唯一稳定入口：所有对外符号必须在这里显式再导出，新增符号也要补进
``__all__``——外部一律 ``from app.completion_grant import X``，不从子模块直接取。

⚠️ 拆包会让 ``monkeypatch.setattr(completion_grant, name, stub)`` 静默失效：每个
子模块在 import 时绑定了自己的副本，改包属性不影响它们，**测试照常变绿但被验证的
代码路径从未被替换**。测试请用 ``tests/conftest.py`` 的
``patch_completion_grant_everywhere()``，并有 AST 守卫测试盯着漏改。
"""
from __future__ import annotations

# app.provider_task_clearance 的再导出：拆包前这些名字就挂在本模块上，外部调用方
# （app/domain/projects/*、app/artifacts.py 等）一直从这里取，保持不变。
from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
    provider_task_clearance_snapshot as provider_task_clearance_snapshot,
)

from app.completion_grant.models import (
    DEFAULT_FALLBACK_QUOTA_FRACTION as DEFAULT_FALLBACK_QUOTA_FRACTION,
    DEFAULT_VIDEO_WALL_CLOCK_CAP_S as DEFAULT_VIDEO_WALL_CLOCK_CAP_S,
    GRANT_TTL_S as GRANT_TTL_S,
    GrantValidationError as GrantValidationError,
    VIDEO_PERMISSION as VIDEO_PERMISSION,
    VideoBudgetAuthorizationError as VideoBudgetAuthorizationError,
    VideoCompletionGrant as VideoCompletionGrant,
    VideoPlanGenerationError as VideoPlanGenerationError,
    _PROVIDER_CLAIM_LEDGER_COLUMNS as _PROVIDER_CLAIM_LEDGER_COLUMNS,
    _row_to_video_grant as _row_to_video_grant,
)

from app.completion_grant.ledger import (
    _create_provider_claim_ledger_table as _create_provider_claim_ledger_table,
    _historical_video_liability as _historical_video_liability,
    _legacy_claim_owner as _legacy_claim_owner,
    _legacy_video_liability_amount as _legacy_video_liability_amount,
    _migrate_provider_claim_ledger as _migrate_provider_claim_ledger,
    _provider_claim_ledger_is_current as _provider_claim_ledger_is_current,
    _unowned_historical_video_liabilities as _unowned_historical_video_liabilities,
    ensure_video_budget_authority_tables as ensure_video_budget_authority_tables,
    migrate_legacy_video_liabilities as migrate_legacy_video_liabilities,
)

from app.completion_grant.reconcile import (
    close_superseded_unclaimed_video_jobs as close_superseded_unclaimed_video_jobs,
    reconcile_project_provider_tasks_for_clear as reconcile_project_provider_tasks_for_clear,
    reconcile_provider_tasks_for_clear as reconcile_provider_tasks_for_clear,
)

from app.completion_grant.budget_authority import (
    project_video_budget_snapshot as project_video_budget_snapshot,
)

from app.completion_grant.budget_claims import (
    close_provider_video_budget_claim_liability as close_provider_video_budget_claim_liability,
    reserve_provider_video_budget as reserve_provider_video_budget,
)

from app.completion_grant.qualification import (
    RELEASE_QUALIFICATION_VERSION as RELEASE_QUALIFICATION_VERSION,
    _canonical_json as _canonical_json,
    _content_fingerprint as _content_fingerprint,
    _generation_plan_material as _generation_plan_material,
    _legacy_screenplay_projection_material as _legacy_screenplay_projection_material,
    _narrative_review_material as _narrative_review_material,
    _screenplay_release_material as _screenplay_release_material,
    _storyboard_release_material as _storyboard_release_material,
    current_video_completion_qualification as current_video_completion_qualification,
)

from app.completion_grant.grants_issue import (
    _hash_token as _hash_token,
    _idempotent_video_grant as _idempotent_video_grant,
    _record_video_budget_authority_event as _record_video_budget_authority_event,
    _video_budget_authority_operation_id as _video_budget_authority_operation_id,
    default_max_fallback_shots as default_max_fallback_shots,
    ensure_completion_grants_table as ensure_completion_grants_table,
    issue_video_completion_grant as issue_video_completion_grant,
)

from app.completion_grant.grants_api import (
    bind_video_grant_generation_plan as bind_video_grant_generation_plan,
    bump_video_grant_wall_clock as bump_video_grant_wall_clock,
    consume_grant as consume_grant,
    get_video_grant as get_video_grant,
    revoke_active_video_grants_for_episode as revoke_active_video_grants_for_episode,
    revoke_grant as revoke_grant,
    validate_video_grant as validate_video_grant,
)


# 表 bootstrap 通过 app.db_schema 的名字注册表暴露：app/db.py 不能反向 import 业务
# 模块（分层反转，docs/coupling_review_2026-08-29.md 第 2 步），因此按名字查找。
# 在包 __init__ 里注册，保证只要有人 import 过本包一次，名字即可解析。
from app.db_schema import register_table as _register_table  # noqa: E402

_register_table("video_budget_authority_tables", ensure_video_budget_authority_tables)
_register_table("completion_grants_table", ensure_completion_grants_table)
_register_table("legacy_video_liabilities_migration", migrate_legacy_video_liabilities)

__all__ = [
    "DEFAULT_FALLBACK_QUOTA_FRACTION",
    "DEFAULT_VIDEO_WALL_CLOCK_CAP_S",
    "GRANT_TTL_S",
    "GrantValidationError",
    "ProviderTasksNotTerminalError",
    "RELEASE_QUALIFICATION_VERSION",
    "VIDEO_PERMISSION",
    "VideoBudgetAuthorizationError",
    "VideoCompletionGrant",
    "VideoPlanGenerationError",
    "_PROVIDER_CLAIM_LEDGER_COLUMNS",
    "_canonical_json",
    "_content_fingerprint",
    "_create_provider_claim_ledger_table",
    "_generation_plan_material",
    "_hash_token",
    "_historical_video_liability",
    "_idempotent_video_grant",
    "_legacy_claim_owner",
    "_legacy_screenplay_projection_material",
    "_legacy_video_liability_amount",
    "_migrate_provider_claim_ledger",
    "_narrative_review_material",
    "_provider_claim_ledger_is_current",
    "_record_video_budget_authority_event",
    "_row_to_video_grant",
    "_screenplay_release_material",
    "_storyboard_release_material",
    "_unowned_historical_video_liabilities",
    "_video_budget_authority_operation_id",
    "assert_provider_tasks_clearable",
    "bind_video_grant_generation_plan",
    "bump_video_grant_wall_clock",
    "close_provider_video_budget_claim_liability",
    "close_superseded_unclaimed_video_jobs",
    "consume_grant",
    "current_video_completion_qualification",
    "default_max_fallback_shots",
    "ensure_completion_grants_table",
    "ensure_video_budget_authority_tables",
    "get_video_grant",
    "issue_video_completion_grant",
    "migrate_legacy_video_liabilities",
    "prepare_provider_tasks_for_clear",
    "project_video_budget_snapshot",
    "provider_task_clearance_snapshot",
    "reconcile_project_provider_tasks_for_clear",
    "reconcile_provider_tasks_for_clear",
    "reserve_provider_video_budget",
    "revoke_active_video_grants_for_episode",
    "revoke_grant",
    "validate_video_grant",
]
