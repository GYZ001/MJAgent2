"""分镜场景绑定同步与场景投影重建。

从 app/domain/storyboard_ops.py 按原样搬移；被 evidence 依赖。
"""
from __future__ import annotations

from app.schemas import (
    Bible,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
)


def _sync_storyboard_scene_bindings(conn, episode_id: str, board: Storyboard) -> int:
    """回写分离后的时间、规范场景图身份及兼容显示文案。

    模糊/旧式输入只允许出现在校验入口；一旦命中，正式投影必须固化为
    ``scene_name`` 规范名，以保证后续选图一一对应。
    """
    rows = conn.execute(
        "SELECT id,shot_no,scene_time,scene_setting,scene_name FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    by_shot_no = {int(row["shot_no"]): row for row in rows}
    changed = 0
    for shot in board.shots:
        row = by_shot_no.get(int(shot.shot_no))
        if row is None:
            continue
        current = (
            str(row["scene_time"] or "").strip(),
            str(row["scene_setting"] or "").strip(),
            str(row["scene_name"] or "").strip(),
        )
        resolved_time = str(shot.scene_time or "").strip()
        resolved = str(shot.scene_name or "").strip()
        resolved_setting = str(shot.scene_setting or "").strip()
        if current == (resolved_time, resolved_setting, resolved):
            continue
        conn.execute(
            "UPDATE shots SET scene_time=?,scene_setting=?,scene_name=? WHERE id=?",
            (resolved_time, resolved_setting, resolved or None, row["id"]),
        )
        changed += 1
    return changed

def _reconcile_storyboard_scene_projection(conn, episode_id: str, bible: Bible) -> dict[str, int]:
    """幂等对账正式镜头与分镜大纲的场景投影。

    场景归一是确定性派生过程，不应依赖「整集所有门禁通过」才落库。
    否则只要台词、连续性等任一无关问题存在，已经判定正确的 scene_name
    仍会长期停留在内存副本，导致页面、选图和暂停检查点持续读到旧绑定。
    """
    from types import SimpleNamespace
    from app.validators import canonicalize_storyboard_scene

    scenes = getattr(bible, "scenes", None) or []
    if not scenes:
        return {"shots": 0, "outline_shots": 0}

    outline_changes = 0
    outline_by_no: dict[int, StoryboardOutlineShot] = {}
    episode = conn.execute(
        "SELECT storyboard_outline_json FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    raw_outline = episode["storyboard_outline_json"] if episode else None
    if raw_outline:
        try:
            outline = StoryboardOutline.model_validate_json(raw_outline)
        except (TypeError, ValueError):
            outline = None
        if outline is not None:
            for brief in outline.shots:
                before = (brief.scene_time, brief.scene_setting, brief.scene_name)
                if not canonicalize_storyboard_scene(brief, bible):
                    continue
                outline_by_no[int(brief.shot_no)] = brief
                if (brief.scene_time, brief.scene_setting, brief.scene_name) != before:
                    outline_changes += 1
            if outline_changes:
                from app.storyboard_authority import (
                    persist_storyboard_outline_projection,
                )

                persist_storyboard_outline_projection(
                    episode_id,
                    outline,
                    conn=conn,
                )

    shot_changes = 0
    rows = conn.execute(
        "SELECT id,shot_no,scene_time,scene_setting,scene_name FROM shots WHERE episode_id=?",
        (episode_id,),
    ).fetchall()
    for row in rows:
        brief = outline_by_no.get(int(row["shot_no"]))
        target = SimpleNamespace(
            scene_time=str(
                (brief.scene_time if brief is not None else row["scene_time"])
                or ""
            ),
            scene_setting=str(
                (brief.scene_setting if brief is not None else row["scene_setting"])
                or ""
            ),
            scene_name=str(
                (brief.scene_name if brief is not None else row["scene_name"])
                or ""
            ),
        )
        before = (
            str(row["scene_time"] or ""),
            str(row["scene_setting"] or ""),
            str(row["scene_name"] or ""),
        )
        if not canonicalize_storyboard_scene(target, bible):
            continue
        after = (target.scene_time, target.scene_setting, target.scene_name)
        if after == before:
            continue
        conn.execute(
            "UPDATE shots SET scene_time=?,scene_setting=?,scene_name=? WHERE id=?",
            (*after, row["id"]),
        )
        shot_changes += 1

    if shot_changes or outline_changes:
        conn.commit()
    return {"shots": shot_changes, "outline_shots": outline_changes}
