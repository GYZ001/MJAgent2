"""对话 Agent 的 Pydantic 合同（PRD §7.2 / §10.2）。"""
from __future__ import annotations

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


class CreateConversationRequest(BaseModel):
    title: str | None = None
    project_id: str | None = None
    created_by: str | None = None


class SendMessageRequest(BaseModel):
    content: str
    context: ContextEnvelope | None = None


class ApprovalDecisionRequest(BaseModel):
    decided_by: str | None = None
    reason: str | None = None
