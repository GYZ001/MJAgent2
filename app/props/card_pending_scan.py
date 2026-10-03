"""分镜阶段补卡的判据与扫描：按本集分镜 ``resources.props`` 推导"哪些道具
label 该补卡"，不看原文提及次数、不用词表（CLAUDE.md「禁止黑白名单修复」，
判据从本集分镜数据推导）。

判据（P0，与派单口径一致）：某个道具 label 按 ``app.props.store.
prop_reference_for_episode`` 的同一口径解析不到 ready 参考图，且在本集分镜
里出现在 ``MIN_SHOT_COUNT`` 个及以上不同段落——跨段反复出现、没有卡锁定外观，
正是用户实测复查发现的「白色陶瓷杯」「米白色平底单鞋」漂移根因。只出现 1 段
的 label 不补（可能确实只是一次性背景物件，建卡/出图成本不值得）。

与 ``app.video_modes.scene_state_views`` 的 ``scene_entries_for_shot``/
``load_episode_shot_rows`` 同一类小解析器各自持有一份（CLAUDE.md 同docstring
先例理由：导入 ``app.video_modes`` 包会连带触发其 ``__init__.py`` 的整条
再导出链，``app.props`` 不需要那条耦合面）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.props.store import prop_reference_for_episode

MIN_SHOT_COUNT = 2


def _segment_from_shot_row(row: Any) -> dict[str, Any] | None:
    try:
        raw = row["shot_contract_json"]
    except (IndexError, KeyError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    segment = data.get("storyboard_pack_segment")
    return segment if isinstance(segment, dict) else None


def prop_entries_for_shot(row: Any) -> list[dict[str, Any]]:
    """本段 ``resources.props[]``；没有分镜段/解析失败返回空列表。"""
    segment = _segment_from_shot_row(row)
    if not segment:
        return []
    resources = segment.get("resources") or {}
    return [entry for entry in (resources.get("props") or []) if isinstance(entry, dict)]


def load_episode_shot_rows(conn: Any, episode_id: str) -> list[Any]:
    return conn.execute(
        "SELECT id, shot_no, shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()


def label_shot_occurrences(shot_rows: list[Any]) -> dict[str, dict[str, Any]]:
    """label -> {"shot_nos": set[int], "descriptions": list[str]}（逐段收集，
    保留重复以供上游判断"段与段描述是否冲突"，不在这里去重语义）。"""
    out: dict[str, dict[str, Any]] = {}
    for row in shot_rows:
        shot_no = int(row["shot_no"])
        for entry in prop_entries_for_shot(row):
            label = str(entry.get("label") or "").strip()
            if not label:
                continue
            info = out.setdefault(label, {"shot_nos": set(), "descriptions": []})
            info["shot_nos"].add(shot_no)
            description = str(entry.get("description") or "").strip()
            if description:
                info["descriptions"].append(description)
    return out


def _prop_has_ready_reference(conn: Any, project_id: str, label: str, episode_no: int) -> bool:
    """``prop_reference_for_episode`` 的同一口径：ready 且文件仍在磁盘上才算
    "有卡"，与 ``app.video_modes.prop_references.resolve_segment_prop_
    manifest_entries`` 判定"这个道具有没有可用参考图"用的是同一条件，避免
    两处判据漂移。"""
    row = prop_reference_for_episode(conn, project_id, label, episode_no)
    if row is None or str(row["status"] or "") != "ready":
        return False
    path = str(row["image_path"] or "")
    return bool(path) and Path(path).is_file()


def candidate_labels_without_card(
    conn: Any, project_id: str, episode_no: int, shot_rows: list[Any],
) -> dict[str, dict[str, Any]]:
    """过滤出满足补卡判据的 label：跨 ``MIN_SHOT_COUNT`` 段及以上、且当前
    解析不到 ready 参考图。分组始终按整集计算（与 ``scene_state_views.
    group_scene_state_runs`` 同一取舍），范围收窄留给调用方按 ``shot_ids``
    过滤最终结果。"""
    occurrences = label_shot_occurrences(shot_rows)
    out: dict[str, dict[str, Any]] = {}
    for label, info in occurrences.items():
        if len(info["shot_nos"]) < MIN_SHOT_COUNT:
            continue
        if _prop_has_ready_reference(conn, project_id, label, episode_no):
            continue
        out[label] = info
    return out
