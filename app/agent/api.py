"""对话 Agent HTTP API（PRD §10.1）。挂载后最终路径为 `/api/agent/...`。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.agent import approvals, events, orchestrator, store
from app.agent.schemas import (
    ApprovalDecisionRequest,
    CreateConversationRequest,
    SendMessageRequest,
)

router = APIRouter(prefix="/agent", tags=["agent"])

# SSE 补看历史后若 turn 仍在处理中，最多再轮询这么多次后关闭连接，避免长期悬挂请求。
_SSE_TAIL_POLL_ROUNDS = 20
_SSE_TAIL_POLL_INTERVAL_S = 0.25


@router.post("/conversations")
def create_conversation(body: CreateConversationRequest):
    return store.create_conversation(title=body.title, project_id=body.project_id, created_by=body.created_by)


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    conversation = store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(404, "会话不存在")
    return {
        "conversation": conversation,
        "messages": store.list_messages(conversation_id, model_visible_only=True),
    }


@router.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, body: SendMessageRequest):
    try:
        outcome = await orchestrator.handle_user_message(conversation_id, body.content, body.context)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    turn = outcome["turn"]
    return {
        "turn_id": turn["id"],
        "status": turn["status"],
        "message": outcome["user_message"],
    }


@router.post("/turns/{turn_id}/cancel")
def cancel_turn(turn_id: str):
    try:
        return orchestrator.cancel_turn(turn_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/turns/{turn_id}/events")
async def stream_turn_events(
    turn_id: str,
    request: Request,
    last_event_id: int | None = Query(default=None, alias="last_event_id"),
):
    """SSE：按 `Last-Event-ID`（header 或 query）续传，不因断线重复创建 Tool Call。"""
    turn = store.get_turn(turn_id)
    if not turn:
        raise HTTPException(404, "turn 不存在")

    header_last_id = request.headers.get("last-event-id")
    after_id = last_event_id
    if after_id is None and header_last_id:
        try:
            after_id = int(header_last_id)
        except ValueError:
            after_id = None

    async def generator():
        cursor = after_id
        rounds = 0
        while True:
            batch = events.list_events(turn_id, after_event_id=cursor)
            for event in batch:
                cursor = event["event_id"]
                yield events.format_sse(event)
            current = store.get_turn(turn_id)
            still_running = bool(current) and current["status"] in ("running", "waiting_approval")
            if not still_running:
                return
            rounds += 1
            if rounds >= _SSE_TAIL_POLL_ROUNDS:
                return
            await asyncio.sleep(_SSE_TAIL_POLL_INTERVAL_S)

    return StreamingResponse(generator(), media_type="text/event-stream")


@router.post("/tool-calls/{tool_call_id}/approve")
async def approve_tool_call(tool_call_id: str, body: ApprovalDecisionRequest | None = None):
    body = body or ApprovalDecisionRequest()
    try:
        return await orchestrator.approve_tool_call(tool_call_id, decided_by=body.decided_by, reason=body.reason)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/tool-calls/{tool_call_id}/reject")
async def reject_tool_call(tool_call_id: str, body: ApprovalDecisionRequest | None = None):
    body = body or ApprovalDecisionRequest()
    try:
        return await orchestrator.reject_tool_call(tool_call_id, decided_by=body.decided_by, reason=body.reason)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
