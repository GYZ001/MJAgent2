"""合成结果里标出"哪些镜头是系统自动采纳、未经人工复核"——供成片台如实展示
（CLAUDE.md「User-Facing Behavior」：界面不得隐瞒生成过程里发生过什么）。

判据不是"这次 concatenate_episode 调用里新采过谁"：命令总线路径
（app.capabilities.handlers.delivery.concatenate）在冻结发布快照之前已经单独
调用过 app.media_exec.concat._auto_adopt_playable_candidates_before_mix，
它的返回值没有传进 concatenate_episode（两处调用点都丢弃了返回值，见
2026-09-23 AI 批量成片对标差距分析第 3 条）；这里改成从
shot_versions.adoption_reason 是否带自动采纳的落库文案反查，覆盖两条调用路
径，不需要再给 concatenate_episode 加新参数、也不用碰
app/capabilities/handlers/delivery.py（不在本次改动的文件所有权范围内）。一
旦这一镜后来被人工重新确认/采纳，adoption_reason 会被那次人工提交覆盖，标记
随之自然消失，不会永远挂着。
"""
from __future__ import annotations

from typing import Any

AUTO_ADOPT_REASON_MARKER = "成片合成时自动采纳该镜最新的成功技术校验候选"


def auto_adopted_shot_nos(
    conn: Any, shot_id_by_no: dict[int, str], included_shot_nos: list[int],
) -> list[int]:
    """本次成片实际含有的镜头里，哪些镜的当前采纳版本是系统自动代采——不是
    人工在生成台点过"采纳"。只查 included_shot_nos（真正混进这版成片的镜
    头），被跳过的镜头不算数。"""
    out = []
    for shot_no in included_shot_nos:
        shot_id = shot_id_by_no.get(shot_no)
        if shot_id is None:
            continue
        row = conn.execute(
            """SELECT sv.adoption_reason FROM shots s
                 JOIN shot_versions sv ON sv.id = s.adopted_version_id
                WHERE s.id=?""",
            (shot_id,),
        ).fetchone()
        if row and row["adoption_reason"] and AUTO_ADOPT_REASON_MARKER in row["adoption_reason"]:
            out.append(shot_no)
    return sorted(out)
