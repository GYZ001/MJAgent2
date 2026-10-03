"""「修订本段」保存时保留修订前采用版本，直至新版本被采用为止。

用户 2026-10-03 拍板：``app/domain/storyboard_ops/identity_workspace.py::
save_identity_candidate`` 保存修订后，该段原来正在采用的视频继续保持采用、
照常进入成片，直到这一段有新版本被采用为止——不这样做时，若新一轮生成连续
失败（供应商拒收等），这一段会在采用链路上彻底空缺（真实回归：EP1 段 33）。

与 ``app/evidence/dialogue_revision_retention.py`` 的「台词修订保留」语义不同
且不合并：那条路径故意让旧版本转 ``stale``、不可再采纳，只留作人工对照；这里
恰恰要让它继续是合法的交付依据，直到被替换——保留版本的 ``status`` 不变
（仍是 ``succeeded``），只在 ``adoption_reason`` 里打标记，不新增列、不改
``shots.adopted_version_id`` 指向。

2026-10-03 修复：``app.evidence.media.select_best_video_candidate`` 对普通
镜头是 sticky 的（当前采用版本只要还合格就不换），带本模块标记的版本必须
豁免这条 sticky——否则修订后新生成成功的版本永远不会被自动采用，旧视频会
一直顶着而不是「新版本到位前顶着」。选择逻辑收在本模块的
``pick_auto_adopt_candidate``，供 ``select_best_video_candidate`` 调用，
判据读 ``is_retained_after_revision``，不新增字符串判断。

放进 app.evidence 包只是为了复用其既有 LAYERS.toml 前缀声明（"app.evidence" = 2），
不新增声明。
"""
from __future__ import annotations

import json
from typing import Any

#: 与 app/media_exec/concat_auto_adopt.py::AUTO_ADOPT_REASON_MARKER 同一种
#: 「adoption_reason 文案子串即标记」写法：人工重新采纳时 adoption_reason 会被
#: 那次提交整段覆盖，标记随之自然消失，不需要专门的「清除标记」步骤。
RETAINED_AFTER_REVISION_MARKER = "片段修订后保留原采用版本：新版本生成并通过采用前，继续作为交付依据"


def is_retained_after_revision(adoption_reason: str | None) -> bool:
    """供 downstream_authority/前端投影判断某条版本是不是「修订前保留的采用版」。"""
    return bool(adoption_reason) and RETAINED_AFTER_REVISION_MARKER in adoption_reason


def pick_auto_adopt_candidate(
    pool: list[dict[str, Any]], adopted_id: str | None,
) -> dict[str, Any]:
    """供 ``app.evidence.media.select_best_video_candidate`` 调用：技术合格池
    （已按 ``version_no`` 升序排好、每条至少带 ``id``/``adoption_reason``）里
    选出应该采用的那一条。

    普通镜头（当前采用版本不带保留标记，或没有采用版本）维持既有 sticky
    行为：已采用版本只要还在池里就不换，没有就取池里第一条。

    带本模块标记的保留版本不享受 sticky：池里一旦有别的版本——必是修订后
    新生成的（修订时其它旧版本都已置 stale）——就优先换成它，不然「旧视频
    只是在新版本到位前顶着」这句承诺永远不会生效；池里没有别的版本（还没
    有新版本生成成功）才继续沿用它顶着，不能因为不享受 sticky 就被换成
    不存在的替代品。"""
    adopted_entry = next((entry for entry in pool if entry["id"] == adopted_id), None)
    if adopted_entry is not None and is_retained_after_revision(adopted_entry.get("adoption_reason")):
        return next((entry for entry in pool if entry["id"] != adopted_id), adopted_entry)
    return adopted_entry if adopted_entry is not None else pool[0]


def mark_retained_after_revision(
    conn: Any, version_id: str, *, dialogue_snapshot: dict,
) -> None:
    """保存修订时调用：status/video_path/artifact_id 全部不变——这条版本本身仍是
    技术有效的交付物，只是服务于已经被替换掉的旧合同。

    ``dialogue_snapshot`` 是修订前那份 ``storyboard_pack_segment`` 的
    ``{"dialogue":[...], "resources":{"characters":[...]}}`` 子集，写进
    ``image_inputs``（合并，不覆盖其余字段）供字幕/ASR 对齐按生成那一刻的台词
    取词，不读修订后的当前合同——否则字幕文本会跟视频里实际说的话不一致
    （``app.subtitles.episode.shot_line_specs`` 的 ``dialogue_snapshot`` 入参
    唯一消费点）。

    连续两次修订、期间该版本一直未被新版本替换（``shots.adopted_version_id``
    始终指向同一条）时，第二次调用传入的 ``dialogue_snapshot`` 是「第二次修订前
    的当前合同」——那已经是第一次修订后的新台词，不是这条物理视频文件实际生成
    时依据的最初台词。已经打过标记（即 ``adoption_reason`` 已是本标记）说明快照
    早就冻结过一次，此次只补发幂等的标记写入，不覆盖已冻结的快照——否则字幕会
    跟着「修订前」这个相对概念连续漂移，而视频文件从未重新生成。"""
    row = conn.execute(
        "SELECT adoption_reason, image_inputs FROM shot_versions WHERE id=?", (version_id,),
    ).fetchone()
    already_retained = bool(row) and is_retained_after_revision(row["adoption_reason"])
    try:
        meta = json.loads(row["image_inputs"] or "{}") if row else {}
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    if not already_retained:
        meta["retained_dialogue_snapshot"] = dialogue_snapshot
    conn.execute(
        "UPDATE shot_versions SET adoption_reason=?, image_inputs=? WHERE id=?",
        (RETAINED_AFTER_REVISION_MARKER, json.dumps(meta, ensure_ascii=False), version_id),
    )


def release_retained_marker_as_stale(conn: Any, version_id: str, *, reason: str) -> None:
    """新版本替换掉保留版本时调用：保留版本转普通 stale，标记随之消失——不能让它
    继续占着「仍在交付」的身份（它已经不再被 ``shots.adopted_version_id`` 指向）。

    ``adoption_reason`` 必须一起清掉：标记就存在这个字段里，只改 ``status``
    留着旧文案会让任何直接按 id 查这一行（不经过 ``shots.adopted_version_id``
    那层 JOIN）的调用方继续把它误判成「仍保留」。"""
    conn.execute(
        "UPDATE shot_versions SET status='stale', error=?, adoption_reason=?, video_slot_active=0 WHERE id=?",
        (reason, reason, version_id),
    )


def release_if_retained_after_revision(conn: Any, version_id: str | None, *, reason: str) -> None:
    """``version_id`` 不存在、或不是保留版本时安全空操作——人工采纳
    （``app.domain.video_ops.adopt``）与系统代采
    （``app.evidence.media.select_best_video_candidate``）两条「真的换掉了
    旧采用版本」路径共用，调用方不需要先自行查一次 ``adoption_reason``。"""
    if not version_id:
        return
    row = conn.execute(
        "SELECT adoption_reason FROM shot_versions WHERE id=?", (version_id,),
    ).fetchone()
    if row and is_retained_after_revision(row["adoption_reason"]):
        release_retained_marker_as_stale(conn, version_id, reason=reason)


def retained_dialogue_snapshot(conn: Any, version_id: str) -> dict | None:
    """读取某条版本保留时冻结的台词快照；没有（普通版本/旧数据）时返回 None，
    调用方据此回退到当前分镜合同的台词。"""
    row = conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?", (version_id,),
    ).fetchone()
    if not row or not row["image_inputs"]:
        return None
    try:
        meta = json.loads(row["image_inputs"])
    except (TypeError, ValueError):
        return None
    snapshot = meta.get("retained_dialogue_snapshot") if isinstance(meta, dict) else None
    return snapshot if isinstance(snapshot, dict) else None
