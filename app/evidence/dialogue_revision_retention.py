"""app/artifacts.py::stage_shot_artifact_cleanup 的台词修订保留分支。

用户 2026-09-23 拍板：分镜台「修订台词」保存时不再对该镜 shot_versions 无条件硬删——
花过额度的视频不可恢复。改为：修订前正在采用的那一版保留、置 stale 供生成台对照；其余
未采用的候选照旧硬删（走调用方既有的 media_cleanup_outbox，不在这里碰文件）。为控制
磁盘占用，每镜最多保留 1 个旧版本：上一次修订保留下来的版本这次不在
``preserve_version_id`` 排除名单里，会随下面的 DELETE 一并清掉，天然替换。

与 app/domain/storyboard_ops/identity_workspace.py::save_identity_candidate 的保留写法
保持一致（status='stale' + error 留痕 + video_slot_active=0），唯一差异是那条路径保留
「全部 succeeded 版本」、这里只保留「修订前采用的那一版」——语义不同（那是身份/发声
复核，这是纯台词措辞修订），不合并。

放进 app.evidence 包只是为了复用其既有 LAYERS.toml 前缀声明（"app.evidence" = 2，覆盖
包内全部子模块，无需再补声明）：app/artifacts.py 已卡在 line_count 棘轮基线（1375）
上，新逻辑要放进「已声明层号包里的新模块」（CLAUDE.md），本次改动禁止再碰
LAYERS.toml/FILE_CONVENTIONS.toml。

2026-09-23 返工（验收发现连续两次修订会丢保留版本）：``dialogue_revision_preserved_
version`` 是「是否台词修订」判据与「保留哪一版」解析的唯一入口，
app/domain/storyboard_ops/mutation_primitives.py::stage_edit_media_cleanup（保存）与
shot_edit_session.py::preview_shot_edit_impact（预览）必须共用这一个函数，不得各自
判断——否则预览数字与保存后的真实结果会对不上（CLAUDE.md「界面承诺必须与实际行为
一致」）。
"""
from __future__ import annotations

import json

#: 生成台候选列表（frontend/src/pages/wall/AttemptList.tsx）与采纳闸门共同读取的
#: 中文原因：写进 shot_versions.error，两端都不需要另起一套文案/枚举。
DIALOGUE_REVISION_STALE_REASON = "台词修订前的版本（已过期，仅供对照，不可采纳）"


def dialogue_revision_preserved_version(conn, shot_id: str, shot_row, changed_fields) -> str | None:
    """本次保存/预览要保留哪一版（没有则 None）。

    判「是否台词修订」= changed_fields 含 dialogues 且该镜 shot_contract_json 里有
    storyboard_pack_segment（是否有 2.x 段落这个事实不会被本次编辑改变，用编辑前的
    shot_row 判断即可，不需要构造 Shot 实例——预览端没有 instance）。

    解析「保留哪一版」必须用调用方事务内的 conn 现查 shots.adopted_version_id，不能
    信调用方早先缓存的 shot_row：
    1) 本镜当前有采用版本 → 保留它。
    2) 没有采用版本（常见于「修订→不生成不采纳→再修订」）→ 回退到上一次台词修订已经
       保留下来的那一版（status='stale' 且 error 是本模块常量，取 version_no 最大的
       一条），继续保留，不当成候选一并删掉。
    3) 都没有 → None，本次全删（含改场景/景别等非台词修订，调用方不会走到这里）。
    """
    if "dialogues" not in changed_fields:
        return None
    contract = json.loads(shot_row["shot_contract_json"] or "{}")
    if contract.get("storyboard_pack_segment") is None:
        return None
    current = conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id=?", (shot_id,),
    ).fetchone()
    if current and current["adopted_version_id"]:
        return str(current["adopted_version_id"])
    previous = conn.execute(
        """SELECT id FROM shot_versions
             WHERE shot_id=? AND status='stale' AND error=?
             ORDER BY version_no DESC LIMIT 1""",
        (shot_id, DIALOGUE_REVISION_STALE_REASON),
    ).fetchone()
    return str(previous["id"]) if previous else None


def dialogue_revision_video_counts(
    conn, shot_id: str, shot_row, changed_fields, total_version_count: int,
) -> tuple[int, int]:
    """返回 (将被永久删除的视频版本数, 转过期保留的视频版本数)；预览与保存共用
    ``dialogue_revision_preserved_version`` 的同一次解析，保证数字对得上。"""
    preserved = dialogue_revision_preserved_version(conn, shot_id, shot_row, changed_fields)
    retained = 1 if preserved else 0
    return total_version_count - retained, retained


def apply_dialogue_revision_retention(
    conn, shot_id: str, preserve_version_id: str | None,
) -> None:
    """按 preserve_version_id 删除本镜 shot_versions；给定时排除该行并转 stale。"""
    if not preserve_version_id:
        conn.execute("DELETE FROM shot_versions WHERE shot_id=?", (shot_id,))
        return
    conn.execute(
        "DELETE FROM shot_versions WHERE shot_id=? AND id!=?",
        (shot_id, preserve_version_id),
    )
    conn.execute(
        """UPDATE shot_versions SET status='stale',error=?,video_slot_active=0
            WHERE id=?""",
        (DIALOGUE_REVISION_STALE_REASON, preserve_version_id),
    )
