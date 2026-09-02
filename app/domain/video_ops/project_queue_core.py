"""整项目视频补齐队列的共享原语：暂停请求集合、状态常量、子任务权威判定与花费统计。

从 app/domain/video_ops.py 按原样搬移；``_project_video_queue_pause_requests`` 是本包内多个子模块共同读写的同一个
set 对象（模块级可变单例，各处只调用 .add()/.discard()，不做 global 重绑定），定义与所有直接写入方都在本文件。
"""
from __future__ import annotations

import json

from app.db import (
    get_conn,
    now,
)


def _persist_project_video_queue(run_id: str, state: dict) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE workflow_runs SET config_snapshot_json=?,updated_at=? WHERE id=?",
        (json.dumps({"queue_state": state}, ensure_ascii=False), now(), run_id),
    )
    conn.commit()

_project_video_queue_pause_requests: set[str] = set()

_PROJECT_VIDEO_CHILD_WAIT_STATUSES = {
    "CREATED",
    "RUNNING",
    "WAITING_RETRY",
    "WAITING_HUMAN",
    "WAITING_AUTHORIZATION",
    "PAUSED_EXTERNAL",
}

_PROJECT_VIDEO_ITEM_SUCCESS_STATUSES = {
    "success",
    "finished",  # Compatibility with queue snapshots persisted before status propagation.
    "already_covered",
}

def request_project_video_queue_pause(project_id: str) -> None:
    _project_video_queue_pause_requests.add(project_id)

def clear_project_video_queue_pause(project_id: str) -> None:
    _project_video_queue_pause_requests.discard(project_id)

def _authoritative_project_video_child_run(run_id: str | None) -> dict | None:
    """Follow recovery links and return the latest persisted child attempt."""
    if not run_id:
        return None
    conn = get_conn()
    current_id = run_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        row = conn.execute(
            """SELECT id,status,failure_code,failure_message,recovered_by_run_id
               FROM workflow_runs WHERE id=?""",
            (current_id,),
        ).fetchone()
        if not row:
            return None
        current = dict(row)
        recovered_by = current.get("recovered_by_run_id")
        if not recovered_by:
            return current
        current_id = recovered_by
    return None

def _propagate_project_video_child_status(item: dict) -> None:
    child = _authoritative_project_video_child_run(item.get("run_id"))
    if child is None:
        item["status"] = "failed"
        item["error"] = "单集补齐运行记录缺失，无法确认完成状态"
        return

    child_status = str(child["status"] or "").upper()
    item["run_id"] = child["id"]
    item["child_run_status"] = child_status
    if child.get("failure_code"):
        item["child_failure_code"] = child["failure_code"]
    if child.get("failure_message"):
        item["child_message"] = child["failure_message"]

    if child_status == "SUCCEEDED":
        item["status"] = "success"
    elif child_status == "PARTIAL":
        item["status"] = "partial"
    elif child_status == "FAILED":
        item["status"] = "failed"
    elif child_status == "CANCELLED":
        item["status"] = "cancelled"
    elif child_status in _PROJECT_VIDEO_CHILD_WAIT_STATUSES:
        item["status"] = "waiting"
    else:
        item["status"] = "failed"
        item["error"] = f"单集补齐返回未知运行状态：{child_status or 'EMPTY'}"

    if item["status"] == "failed" and child.get("failure_message"):
        item["error"] = str(child["failure_message"])[:500]

def _finish_project_video_completion_queue(plan: list[dict], recorder) -> None:
    from app.evidence import repository as evidence_repository
    from app.orchestration.state_machine import transition_run

    statuses = [str(item.get("status") or "") for item in plan]
    waiting_items = [item for item in plan if item.get("status") == "waiting"]
    if waiting_items:
        waiting_statuses = [
            str(item.get("child_run_status") or "")
            for item in waiting_items
        ]
        target = next(
            (
                status for status in waiting_statuses
                if status in _PROJECT_VIDEO_CHILD_WAIT_STATUSES
                and status not in {"CREATED", "RUNNING"}
            ),
            "WAITING_HUMAN",
        )
        source = next(
            (
                item for item in waiting_items
                if item.get("child_run_status") == target
            ),
            waiting_items[0],
        )
        message = f"项目补齐队列有 {len(waiting_items)} 集等待继续处理"
        transition_run(
            recorder.run_id,
            "RUNNING",
            target,
            message,
            failure_code=source.get("child_failure_code"), conn=None,
        )
        evidence_repository.append_event(
            recorder.run_id,
            "PROJECT_VIDEO_QUEUE_WAITING",
            "warning",
            message,
            payload={"waiting": len(waiting_items), "status": target},
        )
        return

    unsuccessful = [
        status for status in statuses
        if status not in _PROJECT_VIDEO_ITEM_SUCCESS_STATUSES
    ]
    if not unsuccessful:
        recorder.succeed("项目补齐队列已全部处理", conn=None)
        return
    if all(status == "cancelled" for status in unsuccessful) and all(
        status == "cancelled" for status in statuses
    ):
        recorder.cancel("项目补齐队列中的单集任务均已取消", conn=None)
        return
    if statuses and all(status == "failed" for status in statuses):
        recorder.fail_result(
            f"项目补齐队列失败，{len(statuses)} 集均未完成",
            failure_code="PROJECT_VIDEO_CHILD_FAILED", conn=None,
        )
        return
    recorder.partial(
        f"项目补齐队列已结束，{len(unsuccessful)} 集未成功完成", conn=None
    )
