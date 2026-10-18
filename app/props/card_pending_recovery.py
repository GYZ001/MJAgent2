"""补建失败的展示与显式恢复。只复用现有 pending 表，不伪造世界书道具卡。"""
from __future__ import annotations

import re
from itertools import groupby
from typing import Any

from app import db
from app.db import now
from app.props.card_pending_scan import candidate_labels_without_card, load_episode_shot_rows
from app.props.card_pending_store import ensure_schema, ensure_tables_on_connection
from app.props.store import ensure_schema as ensure_reference_schema


def public_build_error(error: str | None) -> str:
    """兼容存量原始错误；只提取 HTTP 状态，不把供应商报文或凭据发给页面。"""
    match = re.search(r"HTTP\s+(\d{3})", error or "")
    if match:
        status = int(match.group(1))
        if status in (401, 403):
            return f"模型鉴权失败（HTTP {status}），请在模型中心检查文本模型和出图模型的密钥及访问权限"
        return f"模型接口调用失败（HTTP {status}），请在模型中心及调用记录中检查原因"
    return "道具外观分析或参考图生成失败，请检查模型中心配置及调用记录"


def failed_build_message(scope: str, rows: dict[str, Any]) -> str:
    reasons = list(dict.fromkeys(public_build_error(row.get("error")) for row in rows.values()))
    labels = "、".join(rows)
    return f"{scope}道具补建失败（{labels}）：{'；'.join(reasons)}。修复后到物件库点击「重试补建」，完成后再生成视频"


def builds_for_project(conn: Any, project_id: str) -> list[dict[str, Any]]:
    """返回仍被现有分镜使用的未完成补建，即使世界书尚未创建卡也可见。"""
    ensure_schema()
    pending = conn.execute(
        "SELECT label,status,attempts,error FROM prop_storyboard_card_pending "
        "WHERE project_id=? AND status!='ready' ORDER BY label", (project_id,),
    ).fetchall()
    if not pending:
        return []
    episodes_by_label: dict[str, list[dict[str, Any]]] = {}
    shots = conn.execute(
        "SELECT s.id,s.shot_no,s.shot_contract_json,e.id AS episode_id,e.episode_no "
        "FROM shots s JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=? "
        "ORDER BY e.episode_no,s.shot_no", (project_id,),
    ).fetchall()
    for (episode_id, episode_no), group in groupby(shots, key=lambda row: (row["episode_id"], row["episode_no"])):
        for label in candidate_labels_without_card(conn, project_id, int(episode_no), list(group)):
            episodes_by_label.setdefault(label, []).append({"id": episode_id, "episode_no": episode_no})
    return [
        {"label": row["label"], "status": row["status"], "attempts": row["attempts"],
         "error": public_build_error(row["error"]) if row["status"] == "failed" else None,
         "episodes": episodes_by_label[row["label"]]}
        for row in pending if row["label"] in episodes_by_label
    ]


async def retry_failed_build(project_id: str, episode_id: str, label: str) -> list[str]:
    """独立事务核对实际缺图候选并重置一条失败记录；重复点击不重置在途任务。"""
    ensure_schema()
    ensure_reference_schema()
    def operation(conn: Any) -> list[str]:
        ensure_tables_on_connection(conn)
        episode = conn.execute(
            "SELECT episode_no FROM episodes WHERE id=? AND project_id=?", (episode_id, project_id),
        ).fetchone()
        if episode is None:
            raise ValueError("分集不存在或不属于当前项目")
        shots = load_episode_shot_rows(conn, episode_id)
        candidates = candidate_labels_without_card(conn, project_id, int(episode["episode_no"]), shots)
        if label not in candidates:
            raise ValueError("该道具不是本集需要补建参考图的候选，请刷新物件库")
        updated = conn.execute(
            "UPDATE prop_storyboard_card_pending SET status='queued',attempts=0,error=NULL,updated_at=? "
            "WHERE project_id=? AND label=? AND status='failed'", (now(), project_id, label),
        ).rowcount
        if not updated:
            return []
        return [shot["id"] for shot in shots if int(shot["shot_no"]) in candidates[label]["shot_nos"]]
    return await db.run_write_transaction(operation)
