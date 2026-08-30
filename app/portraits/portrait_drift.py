"""已有角色按集外观漂移的判定与定妆照刷新，以及剧本触发的人物卡/
定妆照批量保证 ensure_cards_for_screenplay。
"""

from __future__ import annotations

import json

from pathlib import Path

from app.db import get_conn, new_id, now
from app.errors import ContentGenerationError, code_ref
from app.evidence.media import record_reference_asset
from app.ingest import chapter_is_stub, chapter_titles_match
from app.schemas import Bible

from ._db_probe import _has_column
from .discovery_fragments import _card_lock
from .discovery_resample import (
    extract_character_fragments,
    screen_appearance_changes,
)
from .portrait_io import (
    _generate_discovered_character_portrait,
    _open_portrait,
    _redraw_portrait,
    _review_portrait_asset,
    _update_bible_appearance,
)

def _episode_source_text(conn, project_id: str, episode_no: int) -> str:
    """本集对应源章节的正文（按集做漂移判定的依据）。"""
    ep = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, episode_no)).fetchone()
    src = json.loads(ep["source_chapters"] or "[]") if ep and ep["source_chapters"] else []
    if not src:
        return ""
    has_title = _has_column(conn, "chapters", "title")
    select_cols = "idx, content" + (", title" if has_title else "")
    rows = conn.execute(
        f"SELECT {select_cols} FROM chapters WHERE project_id=? AND idx>=? AND idx<=? ORDER BY idx",
        (project_id, min(src), max(src))).fetchall()
    if has_title and len(rows) == 1 and chapter_is_stub(dict(rows[0])):
        following = conn.execute(
            "SELECT idx, title, content FROM chapters WHERE project_id=? AND idx>? ORDER BY idx LIMIT 1",
            (project_id, rows[0]["idx"]),
        ).fetchone()
        if following and not chapter_is_stub(dict(following)) and chapter_titles_match(dict(rows[0]), dict(following)):
            rows = [following]
    return "\n".join((r["content"] or "") for r in rows)



def reconcile_bible_display_appearances(conn, project_id: str) -> list[str]:
    """Keep the project card on each character's current persistent portrait segment."""
    row = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    if not row or not row["bible_json"]:
        return []
    data = json.loads(row["bible_json"])
    changed: list[str] = []
    for character in data.get("characters", []):
        name = str(character.get("name") or "").strip()
        if not name:
            continue
        portrait = _open_portrait(conn, project_id, name)
        if portrait is None:
            continue
        appearance = str(portrait["appearance"] or "").strip()
        image_path = str(portrait["image_path"] or "").strip()
        if appearance and character.get("appearance_canonical") != appearance:
            character["appearance_canonical"] = appearance
            changed.append(name)
        if image_path and character.get("ref_image_path") != image_path:
            character["ref_image_path"] = image_path
            if name not in changed:
                changed.append(name)
    if changed:
        conn.execute(
            "UPDATE projects SET bible_json=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), project_id),
        )
        conn.commit()
    return changed


async def _refresh_portrait_on_drift(project_id: str, name: str, episode_no: int,
                                     new_appearance: str, style: str, bible_version: int,
                                     *, change_meta: dict | None = None) -> dict | None:
    """外观明显变化：先在临时状态生成完整多视角包，整包 QA 通过后同一事务关闭旧区间并启用新区间。
    返回 {ep_start, image_path, pack_status} 或 None。"""
    lock = await _card_lock(project_id, name)
    async with lock:
        conn = get_conn()
        cur = _open_portrait(conn, project_id, name)
        if not cur or cur["ep_start"] >= episode_no:
            return None  # 并发已处理，或本集（之后）才登场的图，无需切分
        new_path, new_prompt = await _redraw_portrait(
            project_id, name, style, new_appearance, base_path=cur["image_path"], ep_start=episode_no)
        persistence = (change_meta or {}).get("persistence") or "persistent"
        artifact_supported = _has_column(conn, "character_portraits", "artifact_id")
        pack_supported = _has_column(conn, "character_portraits", "pack_status")
        artifact = None
        qa = None
        if artifact_supported:
            project = conn.execute(
                "SELECT bible_artifact_id FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            parent_ids = [
                artifact_id for artifact_id in (
                    cur["artifact_id"], project["bible_artifact_id"] if project else None,
                ) if artifact_id
            ]
            for attempt in range(1, 3):
                qa = await _review_portrait_asset(new_path, new_appearance)
                artifact = record_reference_asset(
                    asset_type="character_portrait",
                    scope_id=f"{project_id}:{name}:{episode_no}",
                    file_path=new_path,
                    content={"character_name": name, "appearance": new_appearance,
                             "prompt": new_prompt, "episode_start": episode_no,
                             "attempt": attempt, "change": change_meta or {}},
                    parent_artifact_ids=parent_ids,
                    qa=qa,
                )
                if artifact["status"] == "approved":
                    break
                if attempt < 2:
                    new_path, new_prompt = await _redraw_portrait(
                        project_id, name, style, new_appearance,
                        base_path=cur["image_path"], ep_start=episode_no,
                    )
            if not artifact or artifact["status"] not in {"approved", "validated"}:
                # 新主图确实不可读时继续使用旧造型；不把内容 QA 变成终态。
                return {
                    "ep_start": int(cur["ep_start"] or 1),
                    "image_path": cur["image_path"],
                    "pack_status": cur["pack_status"] if pack_supported else "ready",
                    "portrait_id": cur["id"],
                    "gate_retry_exhausted": True,
                }

        stale_segment = conn.execute(
            "SELECT id,ep_end FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start=?",
            (project_id, name, episode_no),
        ).fetchone()
        if stale_segment and stale_segment["id"] != cur["id"]:
            stale_end = stale_segment["ep_end"]
            if stale_end is None or int(stale_end) >= episode_no:
                return None
            conn.execute(
                "DELETE FROM character_portraits WHERE id=?",
                (stale_segment["id"],),
            )

        new_portrait_id = new_id("portrait")
        change_json = json.dumps(change_meta or {}, ensure_ascii=False) if change_meta else None
        # 先插入临时段（不关闭旧区间）；整包通过后再原子切换
        if artifact_supported and pack_supported:
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, artifact_id, pack_status, change_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_portrait_id, project_id, name, episode_no, episode_no,  # 临时：仅占本集，未生效
                 new_appearance, new_prompt, new_path, cur["id"], bible_version,
                 artifact["id"] if artifact else None, "generating", change_json, now()))
        elif artifact_supported:
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, artifact_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_portrait_id, project_id, name, episode_no, None, new_appearance,
                 new_prompt, new_path, cur["id"], bible_version, artifact["id"] if artifact else None, now()))
        else:
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (new_portrait_id, project_id, name, episode_no, None, new_appearance,
                 new_prompt, new_path, cur["id"], bible_version, now()))
        conn.commit()

        pack_status = "ready"
        if pack_supported:
            from app.multiview import (
                PACK_STATUS_FAILED,
                ensure_character_multiview_pack,
                pack_result_ok,
            )
            try:
                pack = await ensure_character_multiview_pack(
                    project_id=project_id,
                    portrait_id=new_portrait_id,
                    character_name=name,
                    appearance=new_appearance,
                    visual_style=style,
                    ep_start=episode_no,
                    base_portrait_id=cur["id"],
                    primary_qa=qa,
                )
            except Exception:
                conn.execute(
                    "UPDATE character_portraits SET ep_end=?,pack_status=? WHERE id=?",
                    (episode_no - 1, PACK_STATUS_FAILED, new_portrait_id),
                )
                conn.commit()
                raise
            if not pack_result_ok(pack):
                conn.execute(
                    "UPDATE character_portraits SET ep_end=?,pack_status=? WHERE id=?",
                    (episode_no - 1, PACK_STATUS_FAILED, new_portrait_id),
                )
                conn.commit()
                raise ContentGenerationError(f"角色多视角包结构不完整：{name}")
            pack_status = "ready"
            # 原子切换：关闭旧区间，开放新区间
            conn.execute("UPDATE character_portraits SET ep_end=? WHERE id=?", (episode_no - 1, cur["id"]))
            new_ep_end = episode_no if persistence == "episode" else None
            conn.execute(
                "UPDATE character_portraits SET ep_end=?, pack_status=? WHERE id=?",
                (new_ep_end, pack_status, new_portrait_id),
            )
            # 若仅本集有效，结束后零付费重新绑定完整旧包（含全部视角，pack_status=ready）
            if persistence == "episode":
                from app.multiview import bind_ready_portrait_reuse
                bind_ready_portrait_reuse(
                    conn,
                    project_id=project_id,
                    character_name=name,
                    source_portrait_id=cur["id"],
                    ep_start=episode_no + 1,
                    bible_version=bible_version,
                )
            conn.commit()
        else:
            conn.execute("UPDATE character_portraits SET ep_end=? WHERE id=?", (episode_no - 1, cur["id"]))
            conn.commit()

        if persistence == "episode":
            _update_bible_appearance(
                conn,
                project_id,
                name,
                str(cur["appearance"] or ""),
                str(cur["image_path"] or ""),
            )
        else:
            _update_bible_appearance(conn, project_id, name, new_appearance, new_path)
        conn.commit()
        return {"ep_start": episode_no, "image_path": new_path, "pack_status": pack_status,
                "portrait_id": new_portrait_id}


def _backfill_matching_future_portrait(
    conn,
    *,
    project_id: str,
    name: str,
    episode_no: int,
    appearance: str,
) -> dict | None:
    """Extend an identical ready pack when discovery assigned a future start."""
    covered = conn.execute(
        "SELECT id FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start<=? "
        "AND (ep_end IS NULL OR ep_end>=?) LIMIT 1",
        (project_id, name, episode_no, episode_no),
    ).fetchone()
    if covered:
        return None
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    pack_clause = "AND pack_status='ready'" if pack_supported else ""
    future = conn.execute(
        "SELECT * FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start>? "
        f"{pack_clause} ORDER BY ep_start ASC LIMIT 1",
        (project_id, name, episode_no),
    ).fetchone()
    if not future:
        return None
    if (future["appearance"] or "").strip() != (appearance or "").strip():
        return None
    image_path = str(future["image_path"] or "")
    if not image_path or not Path(image_path).is_file():
        return None
    same_start = conn.execute(
        "SELECT id FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start=? AND id<>? LIMIT 1",
        (project_id, name, episode_no, future["id"]),
    ).fetchone()
    if same_start:
        return None
    original_start = int(future["ep_start"])
    conn.execute(
        "UPDATE character_portraits SET ep_start=? WHERE id=? AND ep_start=?",
        (episode_no, future["id"], original_start),
    )
    conn.commit()
    return {
        "name": name,
        "portrait_id": future["id"],
        "ep_start": episode_no,
        "previous_ep_start": original_start,
        "image_path": image_path,
        "pack_status": future["pack_status"] if pack_supported else "ready",
        "reused": True,
    }


async def ensure_cards_for_screenplay(project_id: str, episode_no: int, screenplay, bible) -> dict:
    """剧本就绪后（分镜展开前）反应式维护本集出场角色的定妆照：
      ① 剧本外身份在这里只做快速阻断，不再延迟到分镜阶段建卡；
      ② 已有角色漂移：剧本里出现、本集之前已有定妆照的角色 → 用本集源文判断外观是否相比当前锚点
         明显变化，变了就图生图重绘新段并把 bible 锚点同步成最新。
    逐项吞错——单角色失败不阻断分镜。返回 {checked, added:[...], redrawn:[...], errors:[...]}。"""
    bible_names = {c.name for c in bible.characters}
    names: list[str] = []
    seen: set[str] = set()

    def _collect(lst) -> None:
        for n in lst or []:
            n = (n or "").strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)

    for sc in getattr(screenplay, "scene_outline", None) or []:
        _collect(getattr(sc, "characters", None))

    errors: list[str] = []

    # ① Narrative 路径只消费 typed resolver；legacy 仍保留旧分类器。
    narrative_authority = getattr(screenplay, "narrative_plan", None) is not None
    identity_by_token: dict[str, object] = {}
    resolver_error = ""
    if narrative_authority:
        from app.identity_contracts import (
            IdentityContractError,
            narrative_identity_resolver,
        )

        try:
            identity_resolver = narrative_identity_resolver(bible, screenplay)
            for name in names:
                identity_by_token[name] = identity_resolver.resolve(name, usage="visual")
        except IdentityContractError as exc:
            resolver_error = str(exc)
    unknown = (
        ([resolver_error] if resolver_error else [])
        if narrative_authority
        else [n for n in names if n not in bible_names]
    )
    added: list[dict] = []
    blocking_errors: list[str] = []
    if narrative_authority and resolver_error:
        blocking_errors.append(f"剧本 typed identity contract 未完成：{resolver_error}")
    elif not narrative_authority:
        blocking_errors.extend(
            f"剧本人物身份未完成：「{name}」未进入人物谱，也不是已编号的一次性角色；"
            "请回到剧本阶段重跑人物身份预检"
            for name in unknown
        )

    # 剧本阶段若遇到供应商短暂失败，人物卡已保留；分镜前对这些系统失败项
    # 自动补齐定妆包。这是内部自愈，不再转换为用户待审任务。
    conn = get_conn()
    # typed policy 要求资产的非 Bible 身份，直接使用合同的稳定视觉锚点
    # 建立本集定妆包。不需资产的一次性/群体/画外身份不会被名称规则误建卡。
    if narrative_authority and not resolver_error:
        project_row = conn.execute(
            "SELECT bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        bible_version = int(project_row["bible_version"] or 0) if project_row else 0
        generated_asset_ids: set[str] = set()
        for identity in identity_by_token.values():
            if not identity.requires_asset or identity.asset_name in bible_names:
                continue
            if identity.identity_id in generated_asset_ids:
                continue
            generated_asset_ids.add(identity.identity_id)
            try:
                card_lock = await _card_lock(project_id, identity.asset_name)
                async with card_lock:
                    portrait = await _generate_discovered_character_portrait(
                        project_id,
                        identity.asset_name,
                        bible.world.visual_style_canonical,
                        identity.visual_anchor(),
                        ep_start=episode_no,
                        bible_version=bible_version,
                    )
            except Exception as exc:  # noqa: BLE001 - required policy must fail closed
                public = code_ref(
                    exc,
                    action="ensure_narrative_identity_asset",
                    context={
                        "project_id": project_id,
                        "identity_id": identity.identity_id,
                        "episode_no": episode_no,
                    },
                )
                blocking_errors.append(
                    f"身份「{identity.display_name}」合同要求人物资产，但定妆包生成失败{public}"
                )
                continue
            added.append({
                "status": "added",
                "name": identity.display_name,
                "identity_id": identity.identity_id,
                "has_portrait": True,
                **portrait,
            })
    retry_changes: list[dict] = []
    if _has_column(conn, "projects", "bible_auto_changes_json"):
        change_row = conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        try:
            all_changes = json.loads(change_row["bible_auto_changes_json"] or "[]") if change_row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            all_changes = []
        retry_changes = [
            item for item in all_changes
            if item.get("kind") in {"new_character", "character_discovery", "new_bible_character"}
            and item.get("status") in {
                "auto_applied_asset_failed",
                "auto_applied_asset_pending",
            }
            and item.get("character") in names
        ]
    else:
        all_changes = []
    if retry_changes:
        refreshed_project = conn.execute(
            "SELECT bible_json,bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        refreshed_bible = Bible.model_validate(json.loads(refreshed_project["bible_json"]))
        refreshed_by_name = {character.name: character for character in refreshed_bible.characters}
        for change in retry_changes:
            retry_name = str(change.get("character") or "").strip()
            character = refreshed_by_name.get(retry_name)
            if character is None:
                continue
            try:
                retry_lock = await _card_lock(project_id, retry_name)
                async with retry_lock:
                    portrait = await _generate_discovered_character_portrait(
                        project_id,
                        retry_name,
                        refreshed_bible.world.visual_style_canonical,
                        character.appearance_canonical,
                        ep_start=max(1, int(change.get("ep_start") or episode_no)),
                        bible_version=int(refreshed_project["bible_version"] or 0),
                    )
            except Exception as exc:  # noqa: BLE001
                public = code_ref(
                    exc,
                    action="retry_auto_character_portrait",
                    context={"project_id": project_id, "name": retry_name, "episode_no": episode_no},
                )
                change["decision_reason"] = public
                blocking_errors.append(f"{retry_name}：自动定妆包生成失败，系统重试后仍未就绪")
                continue
            change["status"] = "auto_applied"
            change["decided_at"] = now()
            change["decision_reason"] = "系统已在分镜前自动补齐定妆包"
            change.setdefault("payload", {})["portrait_id"] = portrait.get("portrait_id")
            if not any(item.get("name") == retry_name for item in added):
                added.append({"status": "added", "name": retry_name, "has_portrait": True, **portrait})
        conn.execute(
            "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
            (json.dumps(all_changes, ensure_ascii=False), project_id),
        )
        conn.commit()

    # 未来章节扫描可能先发现真实姓名，但当前集剧本已经使用该角色。
    # 若完整包外观与人物谱锚点完全一致，零付费向前扩展适用区间。
    backfilled: list[dict] = []
    by_name = {c.name: c for c in bible.characters}
    for name in (item for item in names if item in bible_names):
        result = _backfill_matching_future_portrait(
            conn,
            project_id=project_id,
            name=name,
            episode_no=episode_no,
            appearance=by_name[name].appearance_canonical,
        )
        if result:
            backfilled.append(result)

    # ② 已有角色按集漂移（只判本集之前就已有定妆照的角色；本集新建的天然是最新）
    src_text = _episode_source_text(conn, project_id, episode_no)
    entries: list[dict] = []
    if src_text:
        for n in (x for x in names if x in bible_names):
            cur = _open_portrait(conn, project_id, n)
            if not cur or cur["ep_start"] >= episode_no:
                continue
            frags = extract_character_fragments(src_text, n)
            if not frags:
                continue  # 本集没正面提到 → 沿用，开区间自然覆盖
            entries.append({"name": n, "fragments": frags,
                            "current_appearance": cur["appearance"] or by_name[n].appearance_canonical})

    redrawn: list[dict] = []
    if entries:
        proj = conn.execute("SELECT bible_version FROM projects WHERE id=?", (project_id,)).fetchone()
        bible_version = (proj["bible_version"] if proj else 0) or 0
        style = bible.world.visual_style_canonical
        try:
            verdicts = await screen_appearance_changes(entries, f"第 {episode_no} 集")
        except Exception as exc:  # noqa: BLE001 判定失败不阻断分镜
            verdicts = {}
            errors.append(f"漂移判定失败@第{episode_no}集"
                          + code_ref(exc, action="screen_appearance_changes",
                                     context={"project_id": project_id, "episode_no": episode_no}))
        for name, v in verdicts.items():
            try:
                res = await _refresh_portrait_on_drift(
                    project_id, name, episode_no, v["new_appearance"], style, bible_version,
                    change_meta={
                        "change_dimensions": v.get("change_dimensions") or [],
                        "persistence": v.get("persistence") or "persistent",
                        "reason": v.get("reason") or "",
                        "evidence_excerpt": v.get("evidence_excerpt") or "",
                    },
                )
            except Exception as exc:  # noqa: BLE001 单角色重绘失败不阻断分镜
                errors.append(f"{name}@第{episode_no}集重绘失败"
                              + code_ref(exc, action="refresh_portrait_on_drift",
                                         context={"project_id": project_id, "name": name, "episode_no": episode_no}))
                continue
            if res:
                redrawn.append({"name": name, "reason": v["reason"], **res})

    reconcile_bible_display_appearances(conn, project_id)

    return {
        "checked": len(unknown),
        "added": added,
        "backfilled": backfilled,
        "redrawn": redrawn,
        "errors": errors,
        "blocking_errors": blocking_errors,
    }

