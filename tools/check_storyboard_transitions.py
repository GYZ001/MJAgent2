from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import get_conn, init_db
from app.schemas import Bible, Shot, Storyboard
from app.validators import SCENE_CUT_TRANSITIONS, validate_storyboard


TAIL_HINTS = {
    "叠化": ("叠化", "渐", "柔", "余韵", "模糊", "压低"),
    "淡出淡入": ("淡出", "淡入", "渐暗", "渐黑", "渐亮", "压暗"),
    "黑场": ("黑场", "黑", "暗"),
    "闪黑": ("闪黑", "黑", "暗"),
    "闪白": ("闪白", "白", "强光", "亮", "刺眼"),
    "甩镜": ("甩", "模糊", "横摇", "拖影", "运动"),
    "遮挡转场": ("遮挡", "掠过", "遮住", "挡住", "黑影", "衣袖", "门"),
    "匹配剪辑": ("匹配", "呼应", "相同", "同样", "圆", "构图"),
    "声音延续+叠化": ("叠化", "余音", "话音", "声音", "回响", "渐"),
    "声音先行+淡入": ("声音", "先行", "淡入", "渐", "传来"),
}

HEAD_HINTS = ("外景", "全景", "远景", "门口", "屋内", "堂内", "院中", "窗", "灯", "新", "独坐", "站在")


def _shot_from_row(row) -> Shot:
    return Shot(
        shot_no=row["shot_no"],
        duration_s=row["duration_s"],
        shot_size=row["shot_size"],
        camera_move=row["camera_move"],
        scene_setting=row["scene_setting"],
        characters=json.loads(row["characters"] or "[]"),
        action_desc=row["action_desc"],
        first_frame_desc=row["first_frame_desc"] or "",
        last_frame_desc=row["last_frame_desc"] or "",
        source_excerpt=row["source_excerpt"] or "",
        narration=row["narration"],
        dialogues=json.loads(row["dialogues"] or "[]"),
        transition=row["transition"] or "硬切",
        continuity_from_prev=bool(row["continuity_from_prev"]),
    )


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(h in text for h in hints)


def main(project_id: str) -> None:
    init_db()
    conn = get_conn()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    episodes = conn.execute(
        "SELECT * FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,),
    ).fetchall()
    report: list[dict] = []
    totals = {
        "episodes": len(episodes),
        "shots": 0,
        "scene_cuts": 0,
        "same_scene_cuts": 0,
        "errors": 0,
        "warnings": 0,
    }
    for ep in episodes:
        rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (ep["id"],),
        ).fetchall()
        shots = [_shot_from_row(r) for r in rows]
        totals["shots"] += len(shots)
        board = Storyboard(episode_no=ep["episode_no"], shots=shots)
        validation_errors = validate_storyboard(board, bible, ep["target_duration_s"])
        issues: list[str] = [f"校验错误：{e}" for e in validation_errors]
        warnings: list[str] = []
        cuts: list[dict] = []
        for i in range(1, len(shots)):
            prev = shots[i - 1]
            shot = shots[i]
            same_scene = shot.scene_setting.strip() == prev.scene_setting.strip()
            if same_scene:
                totals["same_scene_cuts"] += 1
                if shot.transition != "硬切" or not shot.continuity_from_prev:
                    issues.append(
                        f"镜{shot.shot_no:02d} 同场景应硬切且接上镜，当前 transition={shot.transition}, continuity={shot.continuity_from_prev}"
                    )
                continue
            totals["scene_cuts"] += 1
            tail = prev.last_frame_desc or ""
            head = shot.first_frame_desc or ""
            tail_ok = _contains_any(tail + shot.action_desc + (shot.narration or ""), TAIL_HINTS.get(shot.transition, (shot.transition,)))
            head_ok = bool(head.strip()) and (shot.scene_setting.split("，")[-1].strip()[:2] in head or _contains_any(head, HEAD_HINTS))
            cut = {
                "from_shot": prev.shot_no,
                "to_shot": shot.shot_no,
                "from_scene": prev.scene_setting,
                "to_scene": shot.scene_setting,
                "transition": shot.transition,
                "tail_ok": tail_ok,
                "head_ok": head_ok,
                "prev_tail": tail,
                "next_head": head,
            }
            cuts.append(cut)
            if shot.transition not in SCENE_CUT_TRANSITIONS:
                issues.append(f"镜{shot.shot_no:02d} 换场使用了不合适转场：{shot.transition}")
            if shot.transition == "硬切":
                issues.append(f"镜{shot.shot_no:02d} 换场仍为硬切")
            if shot.continuity_from_prev:
                issues.append(f"镜{shot.shot_no:02d} 换场 continuity_from_prev 不应为 true")
            if not tail_ok:
                warnings.append(f"镜{prev.shot_no:02d}->镜{shot.shot_no:02d} 上一镜尾帧转场视觉不够明确：{shot.transition}")
            if not head_ok:
                warnings.append(f"镜{shot.shot_no:02d} 换场首帧新场景建立感不够明确")
        totals["errors"] += len(issues)
        totals["warnings"] += len(warnings)
        report.append({
            "episode_no": ep["episode_no"],
            "title": ep["title"],
            "status": ep["status"],
            "script_error": ep["script_error"],
            "shot_count": len(shots),
            "scene_cuts": cuts,
            "issues": issues,
            "warnings": warnings,
        })
    print(json.dumps({"totals": totals, "episodes": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tools/check_storyboard_transitions.py <project_id>")
    main(sys.argv[1])
