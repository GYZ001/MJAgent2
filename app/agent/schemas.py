"""对话 Agent 的 Pydantic 合同（PRD §7.2 / §10.2）。"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ContextEnvelope(BaseModel):
    """前端每轮提交的页面上下文（PRD §7.2）。禁止塞入整页 DOM/表单值。"""

    route: str | None = None
    project_id: str | None = None
    episode_id: str | None = None
    selected_shot_id: str | None = None
    selected_version_id: str | None = None
    active_tab: str | None = None
    unsaved_draft: bool = False
    visible_issue_ids: list[str] = Field(default_factory=list)


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class TurnStatus(str, Enum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatus(str, Enum):
    """PRD §13.3 状态机的落库子集。"""

    PROPOSED = "proposed"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    ACCEPTED_ASYNC = "accepted_async"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SseEventType(str, Enum):
    """PRD §10.2 SSE 事件类型。"""

    TURN_STARTED = "turn.started"
    ASSISTANT_DELTA = "assistant.delta"
    PLAN_UPDATED = "plan.updated"
    TOOL_PROPOSED = "tool.proposed"
    APPROVAL_REQUIRED = "approval.required"
    TOOL_STARTED = "tool.started"
    TOOL_PROGRESS = "tool.progress"
    RUN_LINKED = "run.linked"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    UI_INTENT = "ui.intent"
    TURN_COMPLETED = "turn.completed"
    TURN_CANCELLED = "turn.cancelled"


class CreateConversationRequest(BaseModel):
    title: str | None = None
    project_id: str | None = None
    created_by: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str | None = None
    project_id: str | None = None
    created_by: str | None = None
    status: str
    created_at: float
    updated_at: float


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    turn_id: str | None = None
    role: str
    content: Any
    model_visible: bool
    created_at: float


class SendMessageRequest(BaseModel):
    content: str
    context: ContextEnvelope | None = None


class SendMessageResponse(BaseModel):
    turn_id: str
    status: TurnStatus
    message: MessageOut


class TurnOut(BaseModel):
    id: str
    conversation_id: str
    status: TurnStatus
    context_envelope: dict[str, Any] | None = None
    model_provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    started_at: float
    finished_at: float | None = None
    failure_code: str | None = None
    failure_message: str | None = None


class ToolCallOut(BaseModel):
    id: str
    turn_id: str
    command_name: str
    command_version: str | None = None
    arguments: dict[str, Any]
    risk: str | None = None
    status: ToolCallStatus
    idempotency_key: str | None = None
    approval_id: str | None = None
    command_id: str | None = None
    run_id: str | None = None
    result_summary: dict[str, Any] | None = None
    error_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class ApprovalDecisionRequest(BaseModel):
    decided_by: str | None = None
    reason: str | None = None


class TurnEventOut(BaseModel):
    event_id: int
    turn_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: float
