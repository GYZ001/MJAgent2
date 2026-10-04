"""参考资产更新影响分析（只读）：人物/场景/道具的库版本相对某集已采用视频
是否漂移，按实体成组，供「参考资产已更新」面板（``asset_refresh.py``）与批量
采纳的候选校验共用。

判据刻意只比较**版本 ID**（人物 ``look_revision_id``、场景 ``scene_revision_id``、
道具 ``ready``+``prop_revision_id``），不比较视角内容指纹（``selected_views``
的 fingerprint）——2026-10-03 在 B 生产库上实测《顾念长安》第 1 集 35 段：
直接复用 ``app.multiview.manifest_revisions_match`` 会判 32/35 段"变了"，但
其中大多数是"同一张定妆照新增了一个补充视角，选中视角从 front_full 切到
face_closeup"这类纯选型噪声（revision id 从未变化），只有新建的 8 张道具卡
是真实资产更新。只认 revision id 层级的变化，能精确复现这条真实边界，不把
选型噪声灌给用户。

与 ``app.domain.storyboard_ops.staleness._shot_adopted_assets_stale``（生成台
"过期"提示信号）和 ``app.media_exec.authority._review_shot_manifest_equal``
（生产闸门 ``REVIEW_DEPENDENCY_STALE``）是三套不同粒度的判据，不是同一套：
后两者各自服务"要不要提醒用户"和"能不能发起付费生成"两件不同的事，本模块
服务"按哪个实体分组、该重生成哪些段、是否已有可用候选"——合并成一套会让
三件事的误报/漏报口径互相拖累（CLAUDE.md「Gates and Criteria」）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.media_exec.enqueue import _load_shot_model
from app.multiview import resolve_shot_asset_dependencies

_CATEGORY_LABELS = {
    "added": "新增参考图",
    "updated": "参考图已更新",
    "removed": "参考被移除",
}


def _diff_item(entity_type: str, name: str, category: str) -> dict[str, Any]:
    return {
        "entity_key": f"{entity_type}:{name}",
        "entity_type": entity_type,
        "entity_name": name,
        "category": category,
        "category_label": _CATEGORY_LABELS[category],
    }


def _character_revision_map(manifest: dict[str, Any] | None) -> dict[str, Any]:
    return {
        str(ch.get("name")): ch.get("look_revision_id")
        for ch in (manifest or {}).get("characters") or []
        if isinstance(ch, dict) and ch.get("name")
    }


def _scene_revision_map(manifest: dict[str, Any] | None) -> dict[str, Any]:
    scenes = [(manifest or {}).get("scene") or {}, *((manifest or {}).get("additional_scenes") or [])]
    return {
        str(s.get("name")): s.get("scene_revision_id")
        for s in scenes if isinstance(s, dict) and s.get("name")
    }


def _prop_state_map(manifest: dict[str, Any] | None) -> dict[str, tuple[bool, Any, bool]]:
    """值形状 ``(ready, prop_revision_id, has_revision_key)``——第三项标记这条
    冻结记录是否来自 2026-10-01 之后的新字段版本（见模块 docstring 的向后
    兼容说明），缺键时第三项为 False，比较时据此跳过 revision id 这一维。"""
    out: dict[str, tuple[bool, Any, bool]] = {}
    for p in (manifest or {}).get("props") or []:
        if not isinstance(p, dict) or not p.get("label"):
            continue
        out[str(p["label"])] = (bool(p.get("ready")), p.get("prop_revision_id"), "prop_revision_id" in p)
    return out


def _revision_diff(entity_type: str, name: str, frozen_present: bool, frozen_rev: Any, current_present: bool, current_rev: Any) -> list[dict[str, Any]]:
    if not current_present:
        return [_diff_item(entity_type, name, "removed")] if frozen_present and frozen_rev else []
    if not frozen_present or frozen_rev is None:
        return [_diff_item(entity_type, name, "added")] if current_rev else []
    if frozen_rev != current_rev:
        return [_diff_item(entity_type, name, "updated")]
    return []


def _prop_diff(label: str, frozen: tuple[bool, Any, bool] | None, current: tuple[bool, Any, bool] | None) -> list[dict[str, Any]]:
    if current is None:
        return [_diff_item("prop", label, "removed")] if frozen and frozen[0] else []
    c_ready, c_rev, c_has_key = current
    if frozen is None:
        return [_diff_item("prop", label, "added")] if c_ready else []
    f_ready, f_rev, f_has_key = frozen
    if f_ready != c_ready:
        return [_diff_item("prop", label, "added" if c_ready else "removed")]
    if f_ready and c_ready and f_has_key and c_has_key and f_rev != c_rev:
        return [_diff_item("prop", label, "updated")]
    return []


def entity_diff(frozen: dict[str, Any] | None, current: dict[str, Any] | None) -> list[dict[str, Any]]:
    """两份 reference manifest 之间，按实体给出的差异列表（见模块 docstring
    的判据范围：只认 revision id，不认视角选型）。"""
    diffs: list[dict[str, Any]] = []
    f_ch, c_ch = _character_revision_map(frozen), _character_revision_map(current)
    for name in sorted(set(f_ch) | set(c_ch)):
        diffs.extend(_revision_diff("character", name, name in f_ch, f_ch.get(name), name in c_ch, c_ch.get(name)))
    f_sc, c_sc = _scene_revision_map(frozen), _scene_revision_map(current)
    for name in sorted(set(f_sc) | set(c_sc)):
        diffs.extend(_revision_diff("scene", name, name in f_sc, f_sc.get(name), name in c_sc, c_sc.get(name)))
    f_pr, c_pr = _prop_state_map(frozen), _prop_state_map(current)
    for label in sorted(set(f_pr) | set(c_pr)):
        diffs.extend(_prop_diff(label, f_pr.get(label), c_pr.get(label)))
    return diffs


def current_entity_keys(manifest: dict[str, Any] | None) -> set[str]:
    keys = {f"character:{n}" for n in _character_revision_map(manifest)}
    keys |= {f"scene:{n}" for n in _scene_revision_map(manifest)}
    keys |= {f"prop:{n}" for n in _prop_state_map(manifest)}
    return keys


def frozen_manifest_of(version_row: Any) -> dict[str, Any] | None:
    """从采用/候选版本的 ``image_inputs`` 里取出冻结时的 reference_manifest；
    与 ``app.domain.storyboard_ops.staleness._adopted_reference_manifest`` 同
    一份判据（含旧记录落在首张参考图 ``dependency_manifest`` 里的回退），不
    另起一套（CLAUDE.md「判据从数据推导，复用现有机制」）。"""
    # 函数内导入：app.domain.storyboard_ops 与 app.domain.video_ops 是
    # app.domain 包级别的真实双向依赖（见 app/domain/video_ops/__init__.py
    # 模块 docstring），模块级互相 import 会在包初始化期成环。
    from app.domain.storyboard_ops.staleness import _adopted_reference_manifest

    return _adopted_reference_manifest(version_row)


def candidate_usable(version_row: Any) -> bool:
    """候选是否"真的可以被采用"：文件存在 + 技术校验未失败（空校验视为未校验
    过，不拦）。与 ``app.domain.video_ops.generate._adopt_reused_completed_
    version`` 的既有判据同源。"""
    video_path = version_row["video_path"] if "video_path" in version_row.keys() else None
    if not video_path or not Path(str(video_path)).is_file():
        return False
    try:
        technical = json.loads(version_row["technical_validation_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (not technical) or bool(technical.get("passed"))


def candidate_matches_current(version_row: Any, current: dict[str, Any] | None) -> bool:
    """候选自己冻结的参考清单是否已是当前最新（不存在任何实体级差异）。"""
    frozen = frozen_manifest_of(version_row)
    if frozen is None:
        return False
    return not entity_diff(frozen, current)


def episode_bible_and_screenplay(conn: Any, episode_row: Any, project_id: str) -> tuple[Any, Any]:
    """本集 bible/screenplay，供全集逐镜复用，避免每镜各查一次。"""
    # 函数内导入：与 app.media_exec.authority._review_shot_manifest_equal
    # 取 bible/screenplay 同一组依赖、同一条理由——模块级 import 这些模块会
    # 在 app.domain 包初始化期与 app.schemas/app.portraits/
    # app.production.screenplay_authority 之间成环（同款先例见该函数
    # docstring）。
    from app.schemas import Bible
    from app.portraits import bible_for_episode  # 同上：同一条函数内导入理由
    from app.production.screenplay_authority import resolve_downstream_screenplay  # 同上：同一条函数内导入理由

    project = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ValueError("项目不存在")
    bible = bible_for_episode(
        project_id, Bible.model_validate(json.loads(project["bible_json"] or "{}")),
        episode_row["episode_no"],
    )
    screenplay = resolve_downstream_screenplay(str(episode_row["id"]), conn=conn).screenplay
    return bible, screenplay


def shot_current_manifest(
    conn: Any, shot_row: Any, *, project_id: str, episode_no: int, bible: Any, screenplay: Any,
    frozen_contract_json: Any = None,
) -> dict[str, Any]:
    """按当前库状态重新解析一镜的参考依赖。``frozen_contract_json`` 非 None 时
    会覆盖分镜合同后再解析——与 ``_review_shot_manifest_equal`` 同一手法：
    必须用候选/采用版本冻结时的合同覆盖 ``shots`` 行当前值，否则单镜编辑会
    被误判成"资产漂移"（CLAUDE.md「拿猜测换猜测」同款教训，这里是复用而不是
    重新踩一遍）。"""
    # 函数内导入：app.continuity.apply_shot_contract 同一延迟导入理由见
    # app.media_exec.authority._review_shot_manifest_equal 对同一个函数的
    # 调用处注释（避免本包初始化期卷入 app.continuity 的依赖链）。
    from app.continuity import apply_shot_contract

    shot_model = _load_shot_model(shot_row)
    if frozen_contract_json is not None:
        apply_shot_contract(shot_model, frozen_contract_json)
    return resolve_shot_asset_dependencies(
        project_id=project_id, episode_no=episode_no, shot_id=shot_row["id"],
        shot=shot_model, scene_name=getattr(shot_model, "scene_name", None) or None,
        conn=conn, bible=bible, screenplay=screenplay,
    )
