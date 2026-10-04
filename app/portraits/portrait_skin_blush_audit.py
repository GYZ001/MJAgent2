"""存量定妆照肤色局部色块核验：对一个项目当前写实画风人物定妆照跑同一视觉
判定，结果按图片内容哈希 + 规则版本持久化缓存（见
``app.portraits.portrait_skin_audit_store``）。只读检查——不自动替换定妆照，
换定妆照是用户的决定（CLAUDE.md「只改产品不改数据」）；人物谱前端据此在
不合格的人物卡上给出可见标记与「重新生成」入口，复用既有重生成路径。

非写实画风项目（或解析不出画风）直接返回空列表：局部色块不是那些画风的
问题（口径与生成侧 ``app.portraits.portrait_skin_blush`` 同源）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.portraits.portrait_skin_blush import PORTRAIT_SKIN_BLUSH_RULE_VERSION
from app.portraits.portrait_skin_blush_check import judge_face_local_color
from app.portraits.portrait_skin_audit_store import content_sha256, get_cached_audit, set_cached_audit
from app.visual_styles import is_photographic_style_prompt


def _current_portrait_rows(
    conn: sqlite3.Connection, project_id: str, only_character: str | None,
) -> list[sqlite3.Row]:
    if only_character:
        return conn.execute(
            "SELECT id, character_name, image_path FROM character_portraits "
            "WHERE project_id=? AND ep_end IS NULL AND character_name=?",
            (project_id, only_character),
        ).fetchall()
    return conn.execute(
        "SELECT id, character_name, image_path FROM character_portraits "
        "WHERE project_id=? AND ep_end IS NULL",
        (project_id,),
    ).fetchall()


async def _audit_one_portrait(project_id: str, row: sqlite3.Row) -> dict[str, Any]:
    """单个角色当前定妆照：命中缓存直接返回，否则真实判定一次并落缓存。判定
    失败（``checked=False``）不写缓存，原样把 ``checked=False`` 返给调用方
    （前端据此按「未判定」处理，不展示色块告警，也不阻断任何流程）。"""
    image_path = row["image_path"]
    content_hash = content_sha256(image_path)
    cached = get_cached_audit(content_hash, PORTRAIT_SKIN_BLUSH_RULE_VERSION)
    if cached is not None:
        return {
            "character_name": row["character_name"], "portrait_id": row["id"],
            "image_path": image_path, "checked": True, "cached": True,
            "rule_version": PORTRAIT_SKIN_BLUSH_RULE_VERSION, **cached,
        }
    verdict = await judge_face_local_color(
        image_path, call_meta={"project_id": project_id, "character_name": row["character_name"]},
    )
    if verdict["checked"] is True:
        set_cached_audit(
            content_hash=content_hash, rule_version=PORTRAIT_SKIN_BLUSH_RULE_VERSION,
            project_id=project_id, character_name=row["character_name"], portrait_id=row["id"],
            image_path=image_path, has_local_color=bool(verdict["has_local_color"]),
            reason=verdict.get("reason") or "",
        )
    return {
        "character_name": row["character_name"], "portrait_id": row["id"],
        "image_path": image_path, "cached": False,
        "checked": verdict["checked"], "has_local_color": verdict["has_local_color"],
        "reason": verdict.get("reason") or "", "rule_version": PORTRAIT_SKIN_BLUSH_RULE_VERSION,
    }


async def audit_project_portraits(
    conn: sqlite3.Connection, project_id: str, project_row, *, only_character: str | None = None,
) -> list[dict[str, Any]]:
    """项目不是写实画风（或画风解析不出来）直接返回空列表；否则逐个角色当前
    定妆照跑核验（命中缓存的不重复真实调用模型）。缺图片文件的行跳过（不报错，
    技术性缺陷留给其它闸门），不计入返回列表。"""
    try:
        bible = json.loads((project_row["bible_json"] if project_row else None) or "{}")
    except (TypeError, ValueError):
        bible = {}
    style = (bible.get("world") or {}).get("visual_style_canonical") or ""
    if not is_photographic_style_prompt((style or "").strip()):
        return []
    results: list[dict[str, Any]] = []
    for row in _current_portrait_rows(conn, project_id, only_character):
        if not row["image_path"] or not Path(row["image_path"]).is_file():
            continue
        results.append(await _audit_one_portrait(project_id, row))
    return results


__all__ = ["audit_project_portraits"]
