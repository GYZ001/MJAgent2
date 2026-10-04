"""按实体成组的参考资产更新报告——``asset_drift.py`` 判据的组装层，供
``asset_refresh.py`` 的只读分析端点与批量采纳校验共用。"""
from __future__ import annotations

import json
from typing import Any

from app.compiler import clip_duration_value
from app.media_urls import build_media_url

from . import asset_drift as drift


def _succeeded_candidate_rows(conn: Any, shot_id: str, *, exclude_version_id: str | None) -> list[Any]:
    rows = conn.execute(
        "SELECT id, version_no, image_inputs, video_path, technical_validation_json, created_at "
        "FROM shot_versions WHERE shot_id=? AND status='succeeded' ORDER BY created_at DESC",
        (shot_id,),
    ).fetchall()
    return [r for r in rows if r["id"] != exclude_version_id]


def _matching_candidates(conn: Any, shot_id: str, current: dict[str, Any], *, exclude_version_id: str | None) -> list[dict[str, Any]]:
    """候选列表按 ``created_at`` 降序（继承 ``_succeeded_candidate_rows`` 的
    排序），前端据此把 ``candidates[0]`` 当作默认预选的最新候选——这里不能
    改排序，否则默认预选会悄悄选错。"""
    out: list[dict[str, Any]] = []
    for row in _succeeded_candidate_rows(conn, shot_id, exclude_version_id=exclude_version_id):
        if not drift.candidate_usable(row):
            continue
        if drift.candidate_matches_current(row, current):
            out.append({
                "version_id": row["id"], "version_no": row["version_no"], "created_at": row["created_at"],
                "video_url": build_media_url(row["video_path"]),
            })
    return out


def _shot_record(conn: Any, shot_row: Any, *, project_id: str, episode_no: int, bible: Any, screenplay: Any) -> dict[str, Any]:
    adopted_id = shot_row["adopted_version_id"]
    if not adopted_id:
        current = drift.shot_current_manifest(
            conn, shot_row, project_id=project_id, episode_no=episode_no, bible=bible, screenplay=screenplay,
        )
        return {
            "shot": shot_row, "current": current, "diff": [], "adopted": False,
            "adopted_version_id": None, "adopted_version_no": None,
        }
    version = conn.execute(
        "SELECT id, image_inputs, version_no FROM shot_versions WHERE id=?", (adopted_id,),
    ).fetchone()
    adopted_version_no = version["version_no"] if version else None
    frozen = drift.frozen_manifest_of(version) if version else None
    frozen_contract = None
    if version is not None:
        try:
            meta = json.loads(version["image_inputs"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            meta = {}
        frozen_contract = meta.get("shot_contract_json")
    current = drift.shot_current_manifest(
        conn, shot_row, project_id=project_id, episode_no=episode_no, bible=bible, screenplay=screenplay,
        frozen_contract_json=frozen_contract,
    )
    diff = drift.entity_diff(frozen, current) if frozen is not None else []
    return {
        "shot": shot_row, "current": current, "diff": diff, "adopted": True,
        "adopted_version_id": adopted_id, "adopted_version_no": adopted_version_no,
    }


def _build_member(conn: Any, record: dict[str, Any], own_diff: dict[str, Any] | None) -> dict[str, Any]:
    shot = record["shot"]
    duration_s = clip_duration_value(shot["duration_s"])
    base = {
        "shot_id": shot["id"], "shot_no": shot["shot_no"], "duration_s": duration_s,
        "entity_category": own_diff["category"] if own_diff else None,
        "adopted_version_no": record.get("adopted_version_no"),
        "candidates": [],
    }
    if not record["adopted"]:
        return {**base, "status": "not_adopted", "status_label": "本段尚未采用任何视频版本",
                "reason": "没有已采用版本，不受本次参考更新影响"}
    # 判定状态只看 own_diff（本分组对应的那一条实体差异），不看
    # record["diff"]（这一段引用的全部实体差异）：同一段可能同时引用一个变了
    # 的实体和一个没变的实体，它在"没变的那个实体"的分组下必须是 latest，不
    # 能被另一个实体的变化带偏成 needs_regen 并挂上错误的原因（2026-10-03
    # 复现：一段同时引用马克杯与温念，温念变了而马克杯没变，之前的代码会在
    # 「马克杯」分组下把这段标成 needs_regen、理由写成"参考被移除：温念"）。
    if own_diff is None:
        return {**base, "status": "latest", "status_label": "采用版本已是最新参考",
                "reason": "本段采用版本的参考资产与当前一致"}
    candidates = _cached_matching_candidates(conn, record)
    if candidates:
        return {**base, "status": "has_candidate", "candidates": candidates,
                "status_label": f"已有按最新参考生成的成功候选（{len(candidates)} 个）",
                "reason": "存在未采用的候选版本，其参考资产已是当前最新"}
    reason = f"{own_diff['category_label']}：{own_diff['entity_name']}"
    return {**base, "status": "needs_regen", "status_label": "需要重生成", "reason": reason}


def _cached_matching_candidates(conn: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
    """同一个 shot 若同时引用多个发生变化的实体，会在多个分组下各被问一次
    "有没有匹配当前参考的候选"——这个问题的答案只取决于 shot 本身（候选是否
    匹配*完整*当前 manifest），与具体哪个实体无关，算一次缓存进 record 即可，
    不必对同一个 shot 重复查库+重复比较（2026-10-03 B 库实测：本轮 8 张新道具
    卡让不少镜头同时命中多个分组，原实现会重复计算完全相同的结果）。"""
    if "_candidates" not in record:
        shot = record["shot"]
        record["_candidates"] = _matching_candidates(
            conn, shot["id"], record["current"], exclude_version_id=record["adopted_version_id"],
        )
    return record["_candidates"]


def _build_group(conn: Any, entity_key: str, head: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    members = []
    for record in records:
        if entity_key not in drift.current_entity_keys(record["current"]):
            continue
        own_diff = next((d for d in record["diff"] if d["entity_key"] == entity_key), None)
        members.append(_build_member(conn, record, own_diff))
    needs_regen = [m for m in members if m["status"] == "needs_regen"]
    return {
        "entity_key": entity_key, "entity_type": head["entity_type"], "entity_name": head["entity_name"],
        "category": head["category"], "category_label": head["category_label"],
        "members": members,
        "needs_regen_shot_ids": [m["shot_id"] for m in needs_regen],
        "needs_regen_seconds": sum(m["duration_s"] for m in needs_regen),
    }


def _build_groups(conn: Any, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    heads: dict[str, dict[str, Any]] = {}
    for record in records:
        for item in record["diff"]:
            heads.setdefault(item["entity_key"], item)
    return [_build_group(conn, key, heads[key], records) for key in sorted(heads)]


def episode_asset_refresh_groups(conn: Any, episode_row: Any) -> dict[str, Any]:
    """本集按实体成组的参考资产更新报告（只读）。"""
    episode_id = str(episode_row["id"])
    project_id, episode_no = episode_row["project_id"], episode_row["episode_no"]
    bible, screenplay = drift.episode_bible_and_screenplay(conn, episode_row, project_id)
    shots = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    records = [
        _shot_record(conn, row, project_id=project_id, episode_no=episode_no, bible=bible, screenplay=screenplay)
        for row in shots
    ]
    groups = _build_groups(conn, records)
    needs_regen_shot_ids: set[str] = set()
    for group in groups:
        needs_regen_shot_ids.update(group["needs_regen_shot_ids"])
    needs_regen_seconds = sum(
        clip_duration_value(record["shot"]["duration_s"])
        for record in records if record["shot"]["id"] in needs_regen_shot_ids
    )
    return {
        "episode_id": episode_id,
        "groups": groups,
        "needs_regen_shot_count": len(needs_regen_shot_ids),
        "needs_regen_seconds": needs_regen_seconds,
        "bible": bible, "screenplay": screenplay,
    }


def candidate_adopt_check(
    conn: Any, shot_row: Any, version_id: str, *, project_id: str, episode_no: int, bible: Any, screenplay: Any,
) -> tuple[bool, str]:
    """整组采纳前逐段核验：版本归属本镜、已成功、文件与技术校验可用，且其
    冻结参考清单确实已是当前最新——四条任一不满足就整组拒绝（CLAUDE.md
    「不做部分采用」）。"""
    version = conn.execute(
        "SELECT id, shot_id, status, image_inputs, video_path, technical_validation_json "
        "FROM shot_versions WHERE id=?", (version_id,),
    ).fetchone()
    if not version or version["shot_id"] != shot_row["id"]:
        return False, "该版本不存在或不属于这一镜"
    if version["status"] != "succeeded":
        return False, "该版本未成功生成，不能采用"
    if not drift.candidate_usable(version):
        return False, "该版本视频文件缺失或技术校验未通过"
    try:
        meta = json.loads(version["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    current = drift.shot_current_manifest(
        conn, shot_row, project_id=project_id, episode_no=episode_no, bible=bible, screenplay=screenplay,
        frozen_contract_json=meta.get("shot_contract_json"),
    )
    if not drift.candidate_matches_current(version, current):
        return False, "该版本的参考资产不是当前最新，不能作为本次整组采用的目标"
    return True, ""
