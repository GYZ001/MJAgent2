"""Production Repair：一次 Baseline 生成 + 局部 Patch 自愈交付。

实现 PRD《剧本分镜一次生成与Agent局部自愈交付方案》的核心不变量：
- I1 完整生成只发生一次
- I2 QA 后只允许 Patch
- I3 未通过不发布
- I4 完成凭证绑定精确版本
- I5 Agent 不能降级标准
"""
from __future__ import annotations

from app.production.certificate import (
    CompletionCertificate,
    issue_completion_certificate,
    verify_completion_certificate,
)
from app.production.metrics import (
    record_baseline_generation,
    record_full_regen_denied,
    record_patch,
)
from app.production.policy import (
    FullRegenDenied,
    assert_baseline_allowed,
    assert_patch_ops_allowed,
    deny_full_regen_after_qa,
)
from app.production.revision import (
    ProductionRevision,
    ensure_production_revision,
    get_production_revision,
    mark_baseline_generated,
    mark_first_evaluation,
)

__all__ = [
    "CompletionCertificate",
    "FullRegenDenied",
    "ProductionRevision",
    "assert_baseline_allowed",
    "assert_patch_ops_allowed",
    "deny_full_regen_after_qa",
    "ensure_production_revision",
    "get_production_revision",
    "issue_completion_certificate",
    "mark_baseline_generated",
    "mark_first_evaluation",
    "record_baseline_generation",
    "record_full_regen_denied",
    "record_patch",
    "verify_completion_certificate",
]
