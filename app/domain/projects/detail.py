"""项目详情投影：任务计时、分集切换器（picker）窗口化查询、``GET /projects/{id}``。"""
from __future__ import annotations

import json

from fastapi import HTTPException

from app.db import get_conn, rows_to_dicts
from app.domain.common import _project_or_404, router
from app.domain.projects.bible_attachments import (
    _attach_character_portraits,
    _attach_scene_refs,
)
from app.domain.projects.evidence import _present_refs_error
from app.evidence import repository as evidence_repository
from app.media_urls import build_media_url
from app.planning import chapter_preview
from app.schemas import EpisodeScreenplay


def _project_task_timings(conn, project: dict) -> dict[str, dict[str, float | None]]:
    """项目级任务计时的服务端起止时间。

    前端曾把起点存在 localStorage：任务运行中刷新页面会让起点永久搁浅，下一个
    任务复用旧起点后显示出「已等待 1244 分」这类虚高时长，故一律以服务端为准。
    """
    project_id = project["id"]

    def run_timing(workflow_type: str) -> dict[str, float | None]:
        return evidence_repository.latest_run_timing(
            workflow_type=workflow_type,
            scope_type="project",
            scope_id=project_id,
            conn=conn,
        )

    def batch_timing(workflow_type: str, batch_column: str) -> dict[str, float | None]:
        """批次任务的计时：起点取批次列，结束沿用最近一次 run。

        这类任务续跑时会新建 workflow_run，只看最近一次 run 会让计时在每次
        续跑后归零（剧本台曾表现为跑了 43 分钟却显示 3 分钟）。批次列在续跑时
        由 resume 分支保留，才是任务级起点。
        """
        timing = run_timing(workflow_type)
        batch_started_at = project.get(batch_column)
        if batch_started_at is not None:
            timing["started_at"] = batch_started_at
        return timing

    # 批量分镜没有父 run，只能按活跃子 run 聚合；全部结束后不再有「本次耗时」可言。
    marks = ",".join("?" for _ in evidence_repository.ACTIVE_RUN_STATUSES)
    storyboard_batch = conn.execute(
        f"""SELECT MIN(run.started_at) AS started_at
              FROM workflow_runs AS run
              JOIN episodes AS episode ON episode.id=run.scope_id
             WHERE run.workflow_type='storyboard' AND run.scope_type='episode'
               AND episode.project_id=? AND run.started_at IS NOT NULL
               AND run.status IN ({marks})""",
        (project_id, *sorted(evidence_repository.ACTIVE_RUN_STATUSES)),
    ).fetchone()

    return {
        # 人物谱没有批次级起点列，只能取最近一次 run；续跑会让它归零，是已知限制。
        "bible": run_timing("character_bible"),
        "refs": batch_timing("character_references", "refs_batch_started_at"),
        "scene_refs": batch_timing("scene_references", "scene_refs_batch_started_at"),
        "screenplay_batch": run_timing("screenplay_batch"),
        "storyboard_batch": {
            "started_at": storyboard_batch["started_at"] if storyboard_batch else None,
            "finished_at": None,
        },
        # 分集规划不在此列：run_regex_plan 是确定性正则切分，毫秒级完成，无需计时。
    }


_PICKER_COLUMNS = "id, episode_no, title, status, screenplay_status"

_PICKER_GENERATION_COLUMNS = """e.id, e.episode_no, e.title, e.status, e.screenplay_status,
    (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id) AS shot_count,
    (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id AND s.adopted_version_id IS NOT NULL) AS video_count,
    (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id AND s.adopted_version_id IS NULL
       AND EXISTS(SELECT 1 FROM shot_versions v WHERE v.shot_id=s.id AND v.status='succeeded')) AS pending_adoption_count,
    (SELECT COUNT(*) FROM shot_versions v JOIN shots s ON s.id=v.shot_id
       WHERE s.episode_id=e.id AND v.status='failed') AS failed_count"""

# 与前端 episodePicker.filterEpisodeOptions 的制作状态筛选一一对应。
_PRODUCTION_FILTER_SQL = {
    "with_video": "video_count > 0",
    "pending_adoption": "pending_adoption_count > 0",
    "failed": "failed_count > 0",
    "unproduced": "(shot_count = 0 OR video_count = 0)",
}

_PICKER_MAX_LIMIT = 200


def _attach_picker_episodes(
    conn,
    payload: dict,
    project_id: str,
    *,
    with_production_counts: bool,
    limit: int = 0,
    keyword: str = "",
    cursor: str = "",
    production_filter: str = "all",
) -> None:
    """分集切换器的数据源。

    ``limit<=0`` 返回整份分集，保持旧契约。``limit>0`` 只返回一个窗口：
    1616 集的项目整份 payload 未压缩 250KB，其中中文标题占 72KB 且 gzip 压不动，
    而下拉最多只展示 60 条——搜索、制作状态筛选、取窗因此全部下沉到服务端。

    窗口之外仍要保证三件事可用，故一并返回：总集数、光标所在序号，以及
    上一集/下一集（按全量顺序算，不受搜索与筛选影响）。光标分集本身始终包含
    在 ``episodes`` 里，这样前端 ``resolveWindowedEpisodeId`` 的语义不用改。
    """
    base = (
        f"SELECT {_PICKER_GENERATION_COLUMNS} FROM episodes e WHERE e.project_id=?"
        if with_production_counts
        else f"SELECT {_PICKER_COLUMNS} FROM episodes WHERE project_id=?"
    )
    if limit <= 0:
        payload["episodes"] = rows_to_dicts(
            conn.execute(f"{base} ORDER BY episode_no", (project_id,)).fetchall()
        )
        return

    limit = max(1, min(int(limit), _PICKER_MAX_LIMIT))
    kw = (keyword or "").strip().lower()
    predicate = (
        _PRODUCTION_FILTER_SQL.get(production_filter or "all")
        if with_production_counts
        else None
    )

    clauses: list[str] = []
    params: list[object] = [project_id]
    if kw:
        clauses.append("(LOWER(title) LIKE ? OR CAST(episode_no AS TEXT) LIKE ?)")
        params.extend([f"%{kw}%", f"%{kw}%"])
    if predicate:
        clauses.append(predicate)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    windowed = f"SELECT * FROM ({base}){where}"

    total = int(conn.execute(
        "SELECT COUNT(*) AS c FROM episodes WHERE project_id=?", (project_id,)
    ).fetchone()["c"])
    if predicate:
        # 制作状态筛选依赖派生列，只能包一层统计；无筛选时走轻量的直接统计。
        match_total = int(conn.execute(
            f"SELECT COUNT(*) AS c FROM ({windowed})", params
        ).fetchone()["c"])
    elif kw:
        match_total = int(conn.execute(
            "SELECT COUNT(*) AS c FROM episodes WHERE project_id=? "
            "AND (LOWER(title) LIKE ? OR CAST(episode_no AS TEXT) LIKE ?)",
            (project_id, f"%{kw}%", f"%{kw}%"),
        ).fetchone()["c"])
    else:
        match_total = total

    cursor_row = None
    index = prev_row = next_row = None
    if cursor:
        cursor_row = conn.execute(
            f"SELECT * FROM ({base}) WHERE id=?", (project_id, cursor)
        ).fetchone()
    if cursor_row is not None:
        episode_no = cursor_row["episode_no"]
        index = int(conn.execute(
            "SELECT COUNT(*) AS c FROM episodes WHERE project_id=? AND episode_no < ?",
            (project_id, episode_no),
        ).fetchone()["c"])
        prev_row = conn.execute(
            "SELECT id, episode_no, title FROM episodes WHERE project_id=? AND episode_no < ? "
            "ORDER BY episode_no DESC LIMIT 1",
            (project_id, episode_no),
        ).fetchone()
        next_row = conn.execute(
            "SELECT id, episode_no, title FROM episodes WHERE project_id=? AND episode_no > ? "
            "ORDER BY episode_no LIMIT 1",
            (project_id, episode_no),
        ).fetchone()

    # 有搜索或筛选时从头给结果；否则把窗口落在光标附近，保留「打开即定位当前集」。
    if kw or predicate or index is None:
        offset = 0
    else:
        offset = max(0, min(index - limit // 3, max(0, match_total - limit)))

    rows = rows_to_dicts(conn.execute(
        f"{windowed} ORDER BY episode_no LIMIT ? OFFSET ?", (*params, limit, offset)
    ).fetchall())
    if cursor_row is not None and all(row["id"] != cursor for row in rows):
        rows.append(dict(cursor_row))
        rows.sort(key=lambda row: row["episode_no"])

    payload["episodes"] = rows
    payload["episode_total"] = total
    payload["episode_match_total"] = match_total
    payload["episode_offset"] = offset
    payload["episode_index"] = index
    payload["episode_current"] = dict(cursor_row) if cursor_row is not None else None
    payload["episode_prev"] = dict(prev_row) if prev_row is not None else None
    payload["episode_next"] = dict(next_row) if next_row is not None else None


@router.get("/projects/{project_id}")
def project_detail(
    project_id: str,
    view: str | None = None,
    page: int = 1,
    page_size: int = 15,
    query: str = "",
    status_filter: str = "all",
    episode_limit: int = 0,
    episode_query: str = "",
    episode_cursor: str = "",
    episode_filter: str = "all",
):
    if view not in (None, "bible", "scenes", "episodes", "picker", "picker_generation"):
        raise HTTPException(400, f"未知项目视图：{view}")
    full = view is None
    p = dict(_project_or_404(project_id))
    conn = get_conn()
    from app import model_registry

    # 世界书/映射台/分镜台分环节文本模型下拉的可选清单；p 里已经带着三个环节各自
    # 保存的选择（bible_text_provider/script_text_provider/board_text_provider，
    # 空串＝未设置，直接来自 projects 表原始行，不需要额外处理）。清单本身很小，
    # 各视图都带上，不按 view 特判。
    p["text_model_choices"] = model_registry.text_model_choices()
    p["refs_error"] = _present_refs_error(conn, p.get("refs_error"))
    if full or view in ("bible", "scenes", "episodes"):
        p["task_timings"] = _project_task_timings(conn, p)
    include_bible = full or view in ("bible", "scenes")
    p["bible"] = json.loads(p["bible_json"]) if include_bible and p["bible_json"] else None
    bible_artifact = (
        evidence_repository.get_artifact(p.get("bible_artifact_id"))
        if p.get("bible_artifact_id") and (full or view == "bible") else None
    )
    if bible_artifact:
        bible_artifact.pop("content_json", None)
        bible_artifact.pop("content", None)
        bible_artifact["evaluations"] = evidence_repository.get_evaluations(
            bible_artifact["id"]
        )
    p["bible_evidence"] = bible_artifact
    p.pop("bible_json", None)
    if p["bible"]:
        from app.refs import effective_portrait_prompt
        style = p["bible"].get("world", {}).get("visual_style_canonical", "")
        import os
        for c in p["bible"].get("characters", []):
            path_str = c.get("ref_image_path")
            if path_str and os.path.exists(path_str):
                c["ref_image_url"] = build_media_url(path_str, version=int(os.path.getmtime(path_str)))
            else:
                c["ref_image_url"] = None
            override = (c.get("portrait_prompt_override") or "").strip()
            c["portrait_prompt_effective"] = effective_portrait_prompt(
                style, c.get("appearance_canonical", ""), override or None,
            )
        # 场景图素材库：为每个规范场景挂上落盘图 url + QA + 有效生成词，供「场景图」菜单页展示。
        from app.scenes import scene_ref_prompt
        for s in p["bible"].get("scenes", []):
            spath = s.get("ref_image_path")
            if spath and os.path.exists(spath):
                s["ref_image_url"] = build_media_url(spath, version=int(os.path.getmtime(spath)))
            else:
                s["ref_image_url"] = None
            soverride = (s.get("scene_prompt_override") or "").strip()
            s["scene_prompt_effective"] = soverride or scene_ref_prompt(
                style,
                s.get("scene_canonical", ""),
                scene_name=s.get("name", ""),
            )
    p["key_timeline"] = (
        json.loads(p["key_timeline"]) if p["key_timeline"] and (full or view == "bible") else []
    )
    p["chapter_count"] = int(conn.execute(
        "SELECT COUNT(*) AS c FROM chapters WHERE project_id=?", (project_id,)
    ).fetchone()["c"])
    if full:
        p["chapters"] = rows_to_dicts(conn.execute(
            "SELECT idx, title, char_count, summary IS NOT NULL AS has_summary, substr(content,1,200) AS preview "
            "FROM chapters WHERE project_id=? ORDER BY idx",
            (project_id,)).fetchall())
        for ch in p["chapters"]:
            ch["preview"] = chapter_preview(ch.pop("preview", ""))
    else:
        p["chapters"] = []
    # 把每个角色的定妆照分段（适用集区间 + 图生图谱系）挂到 bible.characters 上，供横向预览。
    if p["bible"] and (full or view == "bible"):
        _attach_character_portraits(conn, project_id, p["bible"], p.get("bible_auto_changes_json"))
    # The prep navigation is also shown on the character page. Attach current
    # scene-reference status there so it can report actual video usability
    # instead of a stale project-level warning from an older multi-view run.
    if p["bible"] and (full or view in ("bible", "scenes")):
        _attach_scene_refs(conn, project_id, p["bible"])

    if view in ("picker", "picker_generation"):
        # 切换器用不到自动改动流水（13KB），前端也没有任何消费点，别跟着每次切集来回传。
        p.pop("bible_auto_changes_json", None)
        _attach_picker_episodes(
            conn,
            p,
            project_id,
            with_production_counts=view == "picker_generation",
            limit=episode_limit,
            keyword=episode_query,
            cursor=episode_cursor,
            production_filter=episode_filter,
        )
        return p
    if view not in (None, "episodes"):
        p["episodes"] = []
        return p

    if view == "episodes":
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        clauses = ["project_id=?"]
        params: list[object] = [project_id]
        keyword = query.strip().lower()
        if keyword:
            clauses.append("(LOWER(title) LIKE ? OR CAST(episode_no AS TEXT) LIKE ? OR source_chapters LIKE ?)")
            needle = f"%{keyword}%"
            params.extend((needle, needle, needle))
        if status_filter == "running":
            clauses.append("(screenplay_status='running' OR status IN ('scripting','generating'))")
        elif status_filter == "failed":
            clauses.append("(screenplay_status IN ('failed','repairing') OR status LIKE '%failed%')")
        elif status_filter == "done":
            clauses.append("status='done'")
        elif status_filter == "pending":
            clauses.append("(screenplay_status='pending' OR status IN ('planned','drafting'))")
        elif status_filter != "all":
            raise HTTPException(400, f"未知分集状态筛选：{status_filter}")
        where = " AND ".join(clauses)
        filtered_total = int(conn.execute(
            f"SELECT COUNT(*) AS c FROM episodes WHERE {where}", params
        ).fetchone()["c"])
        offset = (page - 1) * page_size
        p["episodes"] = rows_to_dicts(conn.execute(
            f"SELECT e.*, (SELECT COUNT(*) FROM shots s WHERE s.episode_id=e.id) AS shot_count "
            f"FROM episodes e WHERE {where} ORDER BY episode_no LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall())
        p["episodes_total"] = filtered_total
        p["episodes_page"] = page
        p["episodes_page_count"] = max(1, (filtered_total + page_size - 1) // page_size)
        p["episodes_query"] = keyword
        p["episodes_status_filter"] = status_filter
        counts = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN screenplay_status='queued' THEN 1 ELSE 0 END) AS screenplay_queued,
                      SUM(CASE WHEN screenplay_status='running' THEN 1 ELSE 0 END) AS screenplay_running,
                      SUM(CASE WHEN status='scripting' THEN 1 ELSE 0 END) AS scripting,
                      SUM(CASE WHEN screenplay_status IN ('pending','failed','repairing')
                                OR (
                                    screenplay_json IS NULL
                                    AND screenplay_status NOT IN ('queued','running')
                                ) THEN 1 ELSE 0 END) AS screenplay_todo,
                      SUM(CASE WHEN screenplay_status='ready'
                                AND status IN ('planned','script_failed') THEN 1 ELSE 0 END) AS storyboard_ready
               FROM episodes WHERE project_id=?""",
            (project_id,),
        ).fetchone()
        p["episode_counts"] = {key: int(counts[key] or 0) for key in counts.keys()}
        p["episodes_busy"] = bool(
            p["plan_status"] == "running"
            or p["episode_counts"]["screenplay_queued"]
            or p["episode_counts"]["screenplay_running"]
            or p["episode_counts"]["scripting"]
        )
    else:
        p["episodes"] = rows_to_dicts(conn.execute(
            "SELECT * FROM episodes WHERE project_id=? ORDER BY episode_no", (project_id,)).fetchall())
    for ep in p["episodes"]:
        ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
        if ep.get("screenplay_json"):
            try:
                script = EpisodeScreenplay.model_validate(json.loads(ep["screenplay_json"]))
                ep["screenplay_title"] = script.title or ep["title"]
            except (json.JSONDecodeError, TypeError, ValueError):
                ep["screenplay_title"] = ep["title"]
        else:
            ep["screenplay_title"] = ep["title"]
        ep.pop("screenplay_json", None)
        outline_raw = ep.pop("storyboard_outline_json", None)
        try:
            _outline = json.loads(outline_raw) if outline_raw else None
        except (TypeError, ValueError):
            _outline = None
        ep["storyboard_planned_shots"] = len(_outline["shots"]) if _outline and _outline.get("shots") else None
    if view == "episodes":
        chapter_ids = sorted({
            int(ep["source_chapters"][0])
            for ep in p["episodes"] if ep.get("source_chapters")
        })
        if chapter_ids:
            marks = ",".join("?" for _ in chapter_ids)
            p["chapters"] = rows_to_dicts(conn.execute(
                f"SELECT idx, title, char_count, substr(content,1,200) AS preview "
                f"FROM chapters WHERE project_id=? AND idx IN ({marks}) ORDER BY idx",
                [project_id, *chapter_ids],
            ).fetchall())
            for chapter in p["chapters"]:
                chapter["preview"] = chapter_preview(chapter.get("preview") or "")
        first = conn.execute(
            "SELECT MIN(idx) AS idx FROM chapters WHERE project_id=?", (project_id,)
        ).fetchone()
        p["first_chapter_idx"] = first["idx"]
    return p
