"""Capability 公共合同：风险、预检、批准、执行结果、标准输入字段。"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(str, Enum):
    """PRD §8.1 风险等级。"""

    R0_READ = "R0"
    R1_REVERSIBLE = "R1"
    R2_MATERIAL = "R2"
    R3_DESTRUCTIVE = "R3"
    R4_SECRET = "R4"


class ConfirmationPolicy(str, Enum):
    NEVER = "never"
    OPTIONAL = "optional"
    WHEN_IMPACT = "when_impact"
    ALWAYS = "always"


class IdempotencyPolicy(str, Enum):
    NONE = "none"
    RECOMMENDED = "recommended"
    REQUIRED = "required"


class CommandStatus(str, Enum):
    ACCEPTED = "accepted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class StandardCommandInput(BaseModel):
    """所有写命令共用的标准字段（PRD §6.2）。

    ``extra="forbid"``：REST/Agent/MCP 任一调用方发来命令总线的输入模型未声明
    的字段，一律在这里直接报错（经 ``app.capabilities.dispatch.dispatch`` 的
    ``ValueError -> 422`` 映射），而不是被 pydantic 默认的 ``extra="ignore"``
    静默吞掉。REST 包装层此前曾把 ``only_incomplete``/``qualification_version``
    等有意义的参数在 handler 重建请求体时悄悄弄丢，界面弹窗承诺的语义在总线层
    完全失效却无人报错；这道全局保险丝把「模型没声明的字段」从「静默丢弃」
    改成「显式 422」。翻转前已对全部 62 个命令的输入模型与其 REST 包装层
    实际转发的字段做过逐一比对，确认现有转发字段都已在各自模型中声明。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    idempotency_key: str | None = None
    expected_version: int | str | None = None
    dry_run: bool = False
    approval_token: str | None = None
    reason: str | None = None


class PreconditionCheck(BaseModel):
    key: str
    passed: bool
    message: str = ""


class AffectedScope(BaseModel):
    projects: list[str] = Field(default_factory=list)
    episodes: list[str] = Field(default_factory=list)
    shots: list[str] = Field(default_factory=list)
    shot_count: int = 0
    invalidated_artifacts: int = 0
    versions: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class PreflightResult(BaseModel):
    """标准预检结果（PRD §6.3）。"""

    command: str
    allowed: bool
    risk: RiskLevel
    summary: str
    estimated_cost_cny: float | None = None
    # 含重试余量的授权上限（见 app.completion_grant.
    # preview_episode_video_budget_authorization_cap）。只在会触发真实预算
    # 授权的付费视频命令上填充；跟 estimated_cost_cny（首轮预估）是两个不同
    # 的数，审批卡必须把两者都亮出来，不能只显示预估让人误以为那就是上限。
    authorized_cap_cny: float | None = None
    affected: AffectedScope = Field(default_factory=AffectedScope)
    preconditions: list[PreconditionCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    state_fingerprint: str
    requires_confirmation: bool
    confirmation_policy: ConfirmationPolicy = ConfirmationPolicy.NEVER
    denial_code: str | None = None
    denial_message: str | None = None


class UiIntent(BaseModel):
    """白名单 UI Bridge 意图（PRD §11.3）。服务端与前端双重校验。"""

    type: Literal[
        "navigate",
        "select_shot",
        "select_version",
        "open_evidence",
        "open_delivery",
        "open_download",
        "open_credentials",
        "preview",
        "request_directory_grant",
    ]
    view: str | None = None
    project_id: str | None = None
    episode_id: str | None = None
    chapter_idx: int | None = None
    shot_id: str | None = None
    version_id: str | None = None
    artifact_id: str | None = None
    package_id: str | None = None
    model_id: str | None = None
    tab: Literal["preview", "readiness", "records"] | None = None
    artifact: Literal["report", "archive"] | None = None
    auto_follow: bool = False


class CommandResult(BaseModel):
    """标准执行结果（PRD §6.4）。"""

    status: CommandStatus
    summary: str
    command: str | None = None
    command_id: str | None = None
    run_id: str | None = None
    resource_uris: list[str] = Field(default_factory=list)
    ui_intent: UiIntent | None = None
    preflight: PreflightResult | None = None
    error_id: str | None = None
    error_code: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ApprovalTokenPayload(BaseModel):
    """一次性批准 Token 绑定快照（PRD §6.5 / §8）。"""

    approval_id: str
    command: str
    args_hash: str
    state_fingerprint: str
    session_id: str | None = None
    expires_at: float
    used_at: float | None = None
    decision: ApprovalDecision | None = None
    reason: str | None = None
    impact_snapshot: dict[str, Any] = Field(default_factory=dict)
