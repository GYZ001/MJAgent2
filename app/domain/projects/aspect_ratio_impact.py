"""切换项目画幅前的影响披露：只读接口，不改任何数据。

冻结契约（主会话 2026-09-23 与前端并行开发约定，不得另起形状）：

``GET /api/projects/{project_id}/aspect-ratio-impact?aspect_ratio=16:9`` 返回::

    {"project_id": str, "current_aspect_ratio": "9:16"|"16:9",
     "target_aspect_ratio": "9:16"|"16:9", "adopted_videos_total": int,
     "adopted_videos_mismatched": int, "scene_images_total": int,
     "scene_images_mismatched": int | null}

版本 meta 没有画幅快照的一律按 "9:16" 计（上线前生产全是 9:16）；场景图
（``scene_references``）没有存尺寸/画幅记录，``scene_images_mismatched``
如实返回 ``null``，不猜测——前端据此提示「场景图 N 张，画幅未记录」。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.db import get_conn
from app.domain.common import _project_or_404, router
from app.project_settings import ASPECT_RATIOS, resolve_aspect_ratio


@router.get("/projects/{project_id}/aspect-ratio-impact")
def get_project_aspect_ratio_impact(project_id: str, aspect_ratio: str):
    """只读评估：切到 ``aspect_ratio`` 会让当前项目里多少已采用视频/场景图画幅不同。"""
    _project_or_404(project_id)  # 归属校验，与本包既有只读端点同一写法（见 listing.py）
    target = str(aspect_ratio or "").strip()
    if target not in ASPECT_RATIOS:
        raise HTTPException(422, f"不支持的画幅：{aspect_ratio!r}")
    conn = get_conn()
    current = resolve_aspect_ratio(conn, project_id)
    video_row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN COALESCE(json_extract(v.image_inputs,'$.aspect_ratio'),'9:16')<>?
                           THEN 1 ELSE 0 END) AS mismatched
             FROM shots s
             JOIN episodes e ON e.id = s.episode_id
             JOIN shot_versions v ON v.id = s.adopted_version_id
            WHERE e.project_id = ? AND s.adopted_version_id IS NOT NULL""",
        (target, project_id),
    ).fetchone()
    scene_total = conn.execute(
        "SELECT COUNT(*) AS c FROM scene_references WHERE project_id=?", (project_id,),
    ).fetchone()["c"]
    return {
        "project_id": project_id,
        "current_aspect_ratio": current,
        "target_aspect_ratio": target,
        "adopted_videos_total": int(video_row["total"] or 0),
        "adopted_videos_mismatched": int(video_row["mismatched"] or 0),
        "scene_images_total": int(scene_total or 0),
        # scene_references 没有存尺寸/画幅字段，如实返回"未记录"而不是猜测。
        "scene_images_mismatched": None,
    }
